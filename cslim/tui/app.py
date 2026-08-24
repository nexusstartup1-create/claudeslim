"""Reference Textual front-end.

It exists to prove the integration contract, not to hide it: the TUI *only*
touches :mod:`cslim.core`, exactly like ``cli.py`` does.

    CompressRequest(...)  →  CompressionService(progress=cb).run(request)  →  Bundle

The compression runs in a Textual thread worker, so the UI stays responsive on
large repositories; progress arrives through ``call_from_thread``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    ProgressBar,
    Static,
    TextArea,
)

from ..core import (
    PALETTE,
    Bundle,
    ClipboardError,
    CompressionOptions,
    CompressionService,
    CompressRequest,
    DiscoveryOptions,
    OutputMode,
    RenderOptions,
    copy_to_clipboard,
    deliver,
    humanize,
)

CSS = f"""
Screen {{
    background: #140b04;
    color: {PALETTE['cream']};
}}
Header, Footer {{
    background: {PALETTE['ember']};
    color: {PALETTE['flare']};
}}
#sidebar {{
    width: 34;
    border-right: solid {PALETTE['rust']};
}}
#stats {{
    height: 7;
    border: round {PALETTE['burnt']};
    padding: 0 1;
}}
#files {{
    border: round {PALETTE['burnt']};
}}
#preview {{
    border: round {PALETTE['rust']};
}}
DataTable > .datatable--header {{
    background: {PALETTE['ember']};
    color: {PALETTE['flare']};
    text-style: bold;
}}
DataTable > .datatable--cursor {{
    background: {PALETTE['rust']};
}}
Bar > .bar--bar {{
    color: {PALETTE['amber']};
}}
"""


class ClaudeSlimApp(App[None]):
    """Interactive compressor: browse, compress, inspect, ship."""

    CSS = CSS
    TITLE = "ClaudeSlim"
    SUB_TITLE = "AST compressor for Claude Code"

    BINDINGS = [
        Binding("r", "compress", "Recompress"),
        Binding("c", "copy", "Copy payload"),
        Binding("s", "send", "Send to Claude"),
        Binding("a", "toggle_aggressive", "Aggressive"),
        Binding("d", "cycle_docstrings", "Docstrings"),
        Binding("q", "quit", "Quit"),
    ]

    aggressive: reactive[bool] = reactive(False)
    docstrings: reactive[str] = reactive("summary")

    def __init__(self, paths: Iterable[Path] | None = None) -> None:
        super().__init__()
        self.paths = list(paths or [Path.cwd()])
        self.bundle: Bundle | None = None

    # -- layout ------------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield DirectoryTree(str(self.paths[0]), id="tree")
            with Vertical():
                yield Static("press [b]r[/] to compress", id="stats")
                yield ProgressBar(id="progress", show_eta=False)
                yield DataTable(id="files", cursor_type="row")
                yield TextArea.code_editor("", read_only=True, id="preview")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#files", DataTable)
        table.add_columns("file", "lang", "before", "after", "saved")
        self.action_compress()

    # -- actions ------------------------------------------------------------ #

    def action_compress(self) -> None:
        self.query_one("#stats", Static).update("[b]compressing…[/]")
        self.compress_worker()

    def action_toggle_aggressive(self) -> None:
        self.aggressive = not self.aggressive
        self.action_compress()

    def action_cycle_docstrings(self) -> None:
        order = ["summary", "full", "none"]
        self.docstrings = order[(order.index(self.docstrings) + 1) % len(order)]
        self.action_compress()

    def action_copy(self) -> None:
        if not self.bundle:
            return
        try:
            backend = copy_to_clipboard(self.bundle.payload)
            self.notify(f"copied via {backend}", title="clipboard")
        except ClipboardError as exc:
            self.notify(str(exc), severity="error")

    def action_send(self) -> None:
        if not self.bundle:
            return
        self.send_worker(self.bundle.payload)

    @on(DirectoryTree.DirectorySelected)
    def _on_directory(self, event: DirectoryTree.DirectorySelected) -> None:
        self.paths = [Path(event.path)]
        self.action_compress()

    @on(DataTable.RowHighlighted)
    def _on_row(self, event: DataTable.RowHighlighted) -> None:
        if not self.bundle or event.cursor_row is None:
            return
        files = sorted(self.bundle.files, key=lambda f: -f.saved_tokens)
        if 0 <= event.cursor_row < len(files):
            self.query_one("#preview", TextArea).text = files[event.cursor_row].skeleton

    # -- workers ------------------------------------------------------------ #

    @work(thread=True, exclusive=True)
    def compress_worker(self) -> None:
        """The whole integration: build a request, run the service, post back."""
        bar = self.query_one("#progress", ProgressBar)

        def progress(done: int, total: int, current: str) -> None:
            self.call_from_thread(bar.update, total=total, progress=done)

        request = CompressRequest(
            paths=tuple(self.paths),
            discovery=DiscoveryOptions(),
            compression=CompressionOptions(
                docstrings=self.docstrings,  # type: ignore[arg-type]
                aggressive=self.aggressive,
            ),
            render=RenderOptions(format="md"),
        )
        bundle = CompressionService(progress=progress).run(request)
        self.call_from_thread(self._render_bundle, bundle)

    @work(thread=True)
    def send_worker(self, payload: str) -> None:
        try:
            deliver(payload, OutputMode.CLAUDE, prompt="Analyse this compressed codebase.")
            self.call_from_thread(self.notify, "sent to Claude Code")
        except Exception as exc:  # pragma: no cover - environment dependent
            self.call_from_thread(self.notify, str(exc), severity="error")

    # -- rendering ---------------------------------------------------------- #

    def _render_bundle(self, bundle: Bundle) -> None:
        self.bundle = bundle
        stats = bundle.stats
        self.query_one("#stats", Static).update(
            f"[b]{stats.files}[/] files · "
            f"[b]{humanize(stats.original)}[/] → [b]{humanize(stats.compressed)}[/] tokens · "
            f"saved [b]{stats.ratio * 100:.1f}%[/] · "
            f"{stats.context_pct * 100:.1f}% of context\n"
            f"mode: docstrings={self.docstrings} aggressive={self.aggressive}"
        )
        table = self.query_one("#files", DataTable)
        table.clear()
        for f in sorted(bundle.files, key=lambda f: -f.saved_tokens):
            table.add_row(
                f.rel_path,
                f.language.value,
                humanize(f.original_tokens),
                humanize(f.compressed_tokens),
                f"{f.ratio * 100:.0f}%",
            )
        if bundle.files:
            self.query_one("#preview", TextArea).text = bundle.files[0].skeleton


def run(paths: Iterable[Path] | None = None) -> None:
    ClaudeSlimApp(paths=paths).run()


if __name__ == "__main__":
    run()
