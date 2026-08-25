"""ClaudeSlim CLI — Typer + Rich, warm-amber palette.

Ergonomics borrowed from the Kali toolchain: terse verbs, composable flags,
machine-readable ``--json``, and a strict stdout/stderr split so every command
is pipeable::

    cslim ./src | claude -p "map the architecture"
    cslim diff --staged | claude -p "review my staged changes"
    npm test 2>&1 | cslim clean | claude -p "why does this fail?"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.box import ROUNDED
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.table import Table

from .core import (
    MODELS,
    Bundle,
    ClipboardError,
    CompressionOptions,
    CompressionService,
    CompressRequest,
    DiscoveryOptions,
    GitCleanOptions,
    GitError,
    Language,
    OutputMode,
    RenderOptions,
    clean_diff,
    clean_log,
    clean_terminal,
    clipboard_backend,
    deliver,
    git_diff,
    git_log,
    humanize,
    make_console,
    read_stdin,
    resolve_model,
    stdin_has_data,
    stdout_is_pipe,
)
from .core.theme import banner
from .core.tokenizer import EstimatorUnavailable

__version__ = "0.1.0"

app = typer.Typer(
    name="cslim",
    help="Compress codebases and git diffs into AST skeletons for Claude Code.",
    add_completion=True,
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

console = make_console(stderr=True)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _fail(message: str, code: int = 1) -> None:
    # escape(): error text carries paths, exception strings and extras syntax
    # like `pkg[tui]`, all of which Rich would otherwise eat as markup tags.
    console.print(f"[cslim.danger]✖[/] {escape(message)}")
    raise typer.Exit(code)


def _tuple(value: Optional[List[str]]) -> tuple[str, ...]:
    return tuple(value) if value else ()


def _languages(values: Optional[List[str]]) -> tuple[Language, ...]:
    out: list[Language] = []
    for raw in values or []:
        key = raw.strip().lower()
        aliases = {"js": "javascript", "ts": "typescript", "py": "python", "golang": "go"}
        key = aliases.get(key, key)
        try:
            out.append(Language(key))
        except ValueError:
            _fail(f"unknown language: {raw}")
    return tuple(out)


def _resolve_mode(
    out: Optional[Path], clip: bool, to_claude: bool, force_stdout: bool
) -> OutputMode:
    if to_claude:
        return OutputMode.CLAUDE
    if out is not None:
        return OutputMode.FILE
    if clip:
        return OutputMode.CLIPBOARD
    if force_stdout:
        return OutputMode.STDOUT
    return OutputMode.AUTO


def _emit(
    text: str,
    *,
    mode: OutputMode,
    out: Optional[Path],
    prompt: Optional[str],
    model: Optional[str],
    quiet: bool,
) -> int:
    try:
        result = deliver(
            text, mode, out_file=out, prompt=prompt, model=model
        )
    except FileNotFoundError as exc:
        _fail(str(exc))
        return 1
    except ClipboardError as exc:  # pragma: no cover - platform dependent
        _fail(str(exc))
        return 1
    if not quiet:
        if result.mode is OutputMode.CLIPBOARD:
            console.print(
                f"[cslim.ok]✔[/] [cslim.text]copied to clipboard[/] "
                f"[cslim.dim]via {result.detail}[/]"
            )
        elif result.mode is OutputMode.FILE:
            console.print(
                f"[cslim.ok]✔[/] [cslim.text]written to[/] "
                f"[cslim.path]{escape(result.detail)}[/]"
            )
        elif result.mode is OutputMode.CLAUDE:
            console.print("[cslim.ok]✔[/] [cslim.text]sent to Claude Code[/]")
    return result.exit_code


def _require_git_repo(command: str) -> None:
    """Fail early and clearly outside a repository.

    Without this, git quietly switches to ``--no-index`` mode and answers
    ``cslim diff --staged`` with an unknown-option error plus its entire usage
    page — which explains nothing about the actual problem.
    """
    from .core.git_cleaner import is_git_repo

    if is_git_repo():
        return
    _fail(
        f"not a git repository, so `cslim {command}` has nothing to read.\n"
        f"  Run it inside a repo, pipe one in (git diff | cslim {command} --stdin),\n"
        "  or start tracking this project with:  git init"
    )


def _stats_panel(bundle: Bundle) -> Panel:
    s = bundle.stats
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="cslim.dim", justify="right")
    grid.add_column(style="cslim.value")

    detail = f"{s.files}"
    if s.indexed_files:
        skeletons = s.files - s.indexed_files
        detail += f"  [cslim.dim]({skeletons} skeleton + {s.indexed_files} index)[/]"
    if s.dropped_files:
        detail += f"  [cslim.warn](+{s.dropped_files} dropped)[/]"
    grid.add_row("files", detail)
    grid.add_row("original", f"{humanize(s.original)} tokens")
    grid.add_row("compressed", f"{humanize(s.compressed)} tokens")
    grid.add_row("saved", f"[cslim.ok]{humanize(s.saved)}  (-{s.ratio * 100:.1f}%)[/]")
    if s.context_window:
        grid.add_row(
            "context",
            f"{s.context_pct * 100:.1f}% of {humanize(s.context_window)} "
            f"[cslim.dim]({s.model_id})[/]  "
            f"[cslim.dim]was {s.original_context_pct * 100:.1f}%[/]",
        )
    grid.add_row("counter", "exact (API)" if s.exact else "estimated")
    return Panel(
        grid,
        title="[cslim.title]token budget[/]",
        border_style="cslim.border",
        box=ROUNDED,
        padding=(0, 1),
    )


def _files_table(bundle: Bundle, limit: int = 40) -> Table:
    table = Table(
        box=ROUNDED,
        border_style="cslim.border",
        header_style="cslim.title",
        expand=False,
        padding=(0, 1),
    )
    table.add_column("file", style="cslim.path", overflow="fold", max_width=52)
    table.add_column("lang", style="cslim.lang")
    table.add_column("sym", style="cslim.dim", justify="right")
    table.add_column("before", style="cslim.dim", justify="right")
    table.add_column("after", style="cslim.accent", justify="right")
    table.add_column("saved", style="cslim.ok", justify="right")

    files = sorted(bundle.files, key=lambda f: -f.saved_tokens)
    for f in files[:limit]:
        marker = "" if f.included else " [cslim.warn]⊘[/]"
        note = " [cslim.warn]~[/]" if f.fallback else ""
        table.add_row(
            # escape(): `pages/[id].tsx` and friends are valid paths, not markup.
            escape(f.rel_path) + marker + note,
            f.language.value,
            str(len(f.symbols)),
            humanize(f.original_tokens),
            humanize(f.compressed_tokens),
            f"{f.ratio * 100:.0f}%",
        )
    if len(files) > limit:
        table.add_row(f"[cslim.dim]… {len(files) - limit} more[/]", "", "", "", "", "")
    return table


def _run_with_progress(request: CompressRequest, quiet: bool) -> Bundle:
    try:
        return _run_uncaught(request, quiet)
    except EstimatorUnavailable as exc:
        _fail(f"{exc}\n  Drop --exact to use the calibrated estimator instead.")
        raise  # unreachable: _fail exits


def _run_uncaught(request: CompressRequest, quiet: bool) -> Bundle:
    if quiet or not console.is_terminal:
        return CompressionService().run(request)

    with Progress(
        SpinnerColumn(style="cslim.accent"),
        TextColumn("[cslim.text]{task.description}[/]"),
        BarColumn(
            complete_style="cslim.bar.complete",
            finished_style="cslim.bar.finished",
            pulse_style="cslim.bar.pulse",
        ),
        MofNCompleteColumn(),
        TextColumn("[cslim.dim]{task.fields[current]}[/]"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("compressing", total=None, current="")

        def on_progress(done: int, total: int, current: str) -> None:
            progress.update(task, completed=done, total=total, current=current)

        return CompressionService(progress=on_progress).run(request)


# --------------------------------------------------------------------------- #
# root callback
# --------------------------------------------------------------------------- #


def _version_callback(value: bool) -> None:
    if value:
        console.print(banner(__version__))
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """[cslim.brand]cslim[/] — squeeze your codebase into Claude's context window."""


# --------------------------------------------------------------------------- #
# pack (default command)
# --------------------------------------------------------------------------- #


@app.command()
def pack(
    paths: Optional[List[Path]] = typer.Argument(
        None, help="Files or directories to compress (default: current directory)."
    ),
    include: Optional[List[str]] = typer.Option(
        None, "--include", "-i", help="Glob whitelist, repeatable."
    ),
    exclude: Optional[List[str]] = typer.Option(
        None, "--exclude", "-e", help="Glob blacklist, repeatable."
    ),
    lang: Optional[List[str]] = typer.Option(
        None, "--lang", "-l", help="Restrict to languages: python|javascript|typescript|go."
    ),
    docstrings: str = typer.Option(
        "summary", "--docstrings", "-d", help="full | summary | none."
    ),
    private: bool = typer.Option(False, "--private/--no-private", help="Keep _private symbols."),
    imports: bool = typer.Option(True, "--imports/--no-imports", help="Keep import statements."),
    comments: bool = typer.Option(False, "--comments/--no-comments", help="Keep JS/TS doc-comments."),
    aggressive: bool = typer.Option(
        False, "--aggressive", "-a", help="Signatures only: no docstrings, imports or constants."
    ),
    index_only: bool = typer.Option(
        False, "--index-only", "-I",
        help="Ultralight map: one line per file naming what it defines.",
    ),
    all_files: bool = typer.Option(False, "--all", help="Include markdown/config files too."),
    hidden: bool = typer.Option(False, "--hidden", help="Do not skip dot-directories."),
    no_gitignore: bool = typer.Option(False, "--no-gitignore", help="Ignore .gitignore rules."),
    fmt: str = typer.Option("md", "--format", "-f", help="md | plain | xml."),
    tree: bool = typer.Option(False, "--tree", help="Prepend a file tree."),
    no_header: bool = typer.Option(False, "--no-header", help="Omit the legend header."),
    instructions: Optional[str] = typer.Option(
        None, "--instructions", help="Text injected right after the header."
    ),
    model: str = typer.Option("sonnet", "--model", "-m", help="Model used for budgeting."),
    max_tokens: Optional[int] = typer.Option(
        None, "--max-tokens", "-t", help="Drop files until the payload fits this budget."
    ),
    context_window: Optional[int] = typer.Option(
        None, "--context-window", help="Override the model context window."
    ),
    exact: bool = typer.Option(False, "--exact", help="Exact token counts via Anthropic API."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write the payload to a file."),
    clip: bool = typer.Option(False, "--clip", "-c", help="Copy the payload to the clipboard."),
    to_claude: bool = typer.Option(False, "--claude", help="Pipe straight into `claude`."),
    prompt: Optional[str] = typer.Option(
        None, "--prompt", "-p", help="Prompt passed to `claude -p` (implies --claude)."
    ),
    stdout_flag: bool = typer.Option(False, "--stdout", help="Force payload on stdout."),
    show_files: bool = typer.Option(False, "--files", help="Show the per-file table."),
    json_out: bool = typer.Option(False, "--json", help="Emit a JSON report on stdout."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Payload only, no UI."),
) -> None:
    """Compress a codebase into an AST skeleton bundle."""
    if docstrings not in ("full", "summary", "none"):
        _fail("--docstrings must be one of: full, summary, none")
    if fmt not in ("md", "plain", "xml"):
        _fail("--format must be one of: md, plain, xml")

    targets = tuple(paths) if paths else (Path.cwd(),)
    request = CompressRequest(
        paths=targets,
        discovery=DiscoveryOptions(
            include=_tuple(include),
            exclude=_tuple(exclude),
            languages=_languages(lang),
            hidden=hidden,
            use_gitignore=not no_gitignore,
            all_files=all_files,
        ),
        compression=CompressionOptions(
            docstrings=docstrings,  # type: ignore[arg-type]
            keep_private=private,
            keep_imports=imports,
            keep_comments=comments,
            aggressive=aggressive,
        ),
        render=RenderOptions(
            format=fmt,  # type: ignore[arg-type]
            header=not no_header,
            tree=tree,
            instructions=instructions or "",
        ),
        model=model,
        max_tokens=max_tokens,
        context_window=context_window,
        exact_tokens=exact,
        index_only=index_only,
    )

    if not quiet and not json_out and console.is_terminal and not stdout_is_pipe():
        console.print(banner(__version__))

    bundle = _run_with_progress(request, quiet or json_out)

    if not bundle.files:
        _fail("no source files found — check your paths, --lang or --include filters")

    if json_out:
        sys.stdout.write(json.dumps(bundle.to_dict(), indent=2) + "\n")
        raise typer.Exit()

    mode = _resolve_mode(out, clip, to_claude or prompt is not None, stdout_flag)
    code = _emit(
        bundle.payload,
        mode=mode,
        out=out,
        prompt=prompt,
        model=resolve_model(model).id if to_claude or prompt else None,
        quiet=quiet,
    )

    if not quiet:
        if show_files:
            console.print(_files_table(bundle))
        console.print(_stats_panel(bundle))
        for warning in bundle.warnings[:6]:
            console.print(f"[cslim.warn]![/] [cslim.dim]{escape(warning)}[/]")
        _warn_if_automatic(mode)
    raise typer.Exit(code)


def _warn_if_automatic(mode: OutputMode) -> None:
    """Piping by hand while the hook is installed pays for the map twice."""
    if mode not in (OutputMode.STDOUT, OutputMode.CLAUDE, OutputMode.AUTO):
        return
    from .core.installer import hook_status

    try:
        installed = any(command for _scope, _path, command in hook_status())
    except Exception:
        return
    if installed:
        console.print(
            "[cslim.warn]![/] [cslim.dim]automatic mode is installed here — "
            "piping by hand can send the map twice[/]"
        )


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #


@app.command()
def stats(
    paths: Optional[List[Path]] = typer.Argument(None, help="Files or directories."),
    lang: Optional[List[str]] = typer.Option(None, "--lang", "-l"),
    model: str = typer.Option("sonnet", "--model", "-m"),
    aggressive: bool = typer.Option(False, "--aggressive", "-a"),
    exact: bool = typer.Option(False, "--exact"),
    limit: int = typer.Option(40, "--limit", help="Rows in the per-file table."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Report token savings without producing a payload."""
    request = CompressRequest(
        paths=tuple(paths) if paths else (Path.cwd(),),
        discovery=DiscoveryOptions(languages=_languages(lang)),
        compression=CompressionOptions(aggressive=aggressive),
        model=model,
        exact_tokens=exact,
        render_payload=False,
    )
    bundle = _run_with_progress(request, json_out)
    if not bundle.files:
        _fail("no source files found")
    if json_out:
        report = bundle.to_dict()
        report.pop("payload", None)
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        raise typer.Exit()
    console.print(_files_table(bundle, limit=limit))
    console.print(_stats_panel(bundle))


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


@app.command()
def diff(
    paths: Optional[List[str]] = typer.Argument(None, help="Limit the diff to these paths."),
    base: Optional[str] = typer.Option(
        None, "--base", "-b", help="Diff against a ref (e.g. main, HEAD~3)."
    ),
    staged: bool = typer.Option(False, "--staged", "-s", help="Diff the index."),
    context: int = typer.Option(3, "--context", "-C", help="Context lines requested from git."),
    keep_context: int = typer.Option(
        0, "--keep-context", "-k", help="Context lines kept around each change."
    ),
    max_hunk: int = typer.Option(400, "--max-hunk", help="Truncate hunks longer than this."),
    keep_whitespace: bool = typer.Option(
        False, "--keep-whitespace", help="Keep whitespace-only hunks."
    ),
    keep_generated: bool = typer.Option(
        False, "--keep-generated", help="Keep lockfiles / build output."
    ),
    stdin_input: bool = typer.Option(False, "--stdin", help="Read a diff from stdin instead of git."),
    model: str = typer.Option("sonnet", "--model", "-m"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    clip: bool = typer.Option(False, "--clip", "-c"),
    to_claude: bool = typer.Option(False, "--claude"),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p"),
    stdout_flag: bool = typer.Option(False, "--stdout"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Sanitize a git diff: drop blob hashes, lockfiles and whitespace churn."""
    if stdin_input or (stdin_has_data() and not staged and base is None):
        raw = read_stdin()
        if not raw.strip():
            _fail("no diff on stdin")
    else:
        _require_git_repo("diff")
        try:
            raw = git_diff(
                base=base, staged=staged, paths=paths or (), context=context
            )
        except GitError as exc:
            _fail(str(exc))
            return

    if not raw.strip():
        console.print("[cslim.warn]![/] [cslim.dim]empty diff — nothing to compress[/]")
        raise typer.Exit()

    options = GitCleanOptions(
        keep_context=keep_context,
        max_hunk_lines=max_hunk,
        ignore_whitespace_only=not keep_whitespace,
        drop_excluded=not keep_generated,
    )
    result = clean_diff(raw, options)
    if not result.text:
        console.print("[cslim.warn]![/] [cslim.dim]diff reduced to nothing (all noise)[/]")
        raise typer.Exit()

    payload = (
        "# Sanitized git diff (metadata stripped by ClaudeSlim)\n"
        f"# {len(result.kept_files)} file"
        f"{'s' if len(result.kept_files) != 1 else ''} · "
        f"+{result.added}/-{result.removed}\n\n"
        f"{result.text}\n"
    )

    from .core.tokenizer import resolve_estimator  # local: avoids import cost when unused

    estimator = resolve_estimator(model=resolve_model(model).id)
    before, after = estimator.count(raw), estimator.count(payload)

    mode = _resolve_mode(out, clip, to_claude or prompt is not None, stdout_flag)
    code = _emit(payload, mode=mode, out=out, prompt=prompt, model=None, quiet=quiet)

    if not quiet:
        saved = (1 - after / before) * 100 if before else 0.0
        console.print(
            f"[cslim.text]diff[/] [cslim.dim]{result.original_lines} → "
            f"{result.cleaned_lines} lines ·[/] "
            f"[cslim.value]{humanize(before)} → {humanize(after)}[/] "
            f"[cslim.ok](-{saved:.1f}%)[/]"
        )
        if result.skipped_files:
            console.print(
                f"[cslim.dim]skipped {len(result.skipped_files)} noisy file(s)[/]"
            )
    raise typer.Exit(code)


# --------------------------------------------------------------------------- #
# log
# --------------------------------------------------------------------------- #


@app.command("log")
def log_cmd(
    count: int = typer.Option(30, "--count", "-n", help="Number of commits."),
    since: Optional[str] = typer.Option(None, "--since", help="e.g. '2 weeks ago'."),
    author: Optional[str] = typer.Option(None, "--author"),
    paths: Optional[List[str]] = typer.Argument(None),
    body: bool = typer.Option(False, "--body", help="Keep the first lines of commit bodies."),
    merges: bool = typer.Option(False, "--merges", help="Keep merge commits."),
    flat: bool = typer.Option(
        False, "--flat", help="One self-contained line per commit, no grouping."
    ),
    stdin_input: bool = typer.Option(False, "--stdin"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    clip: bool = typer.Option(False, "--clip", "-c"),
    to_claude: bool = typer.Option(False, "--claude"),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p"),
    stdout_flag: bool = typer.Option(False, "--stdout"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Flatten git log into one dense line per commit."""
    if stdin_input or stdin_has_data():
        raw = read_stdin()
    else:
        _require_git_repo("log")
        try:
            raw = git_log(count=count, since=since, author=author, paths=paths or ())
        except GitError as exc:
            _fail(str(exc))
            return
    text = clean_log(raw, keep_body=body, drop_merges=not merges, group=not flat)
    if not text:
        console.print("[cslim.warn]![/] [cslim.dim]no commits[/]")
        raise typer.Exit()
    payload = f"# git log (sanitized by ClaudeSlim)\n{text}\n"
    mode = _resolve_mode(out, clip, to_claude or prompt is not None, stdout_flag)
    code = _emit(payload, mode=mode, out=out, prompt=prompt, model=None, quiet=quiet)
    if not quiet:
        console.print(
            f"[cslim.text]log[/] [cslim.dim]{len(raw.splitlines())} → "
            f"{len(text.splitlines())} lines[/]"
        )
    raise typer.Exit(code)


# --------------------------------------------------------------------------- #
# clean
# --------------------------------------------------------------------------- #


@app.command()
def clean(
    file: Optional[Path] = typer.Argument(None, help="File to clean (default: stdin)."),
    keep_timestamps: bool = typer.Option(False, "--keep-timestamps"),
    keep_repeats: bool = typer.Option(False, "--keep-repeats"),
    keep_frames: bool = typer.Option(
        False, "--keep-frames", help="Do not fold recursive/vendor stack frames."
    ),
    max_lines: int = typer.Option(4000, "--max-lines"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    clip: bool = typer.Option(False, "--clip", "-c"),
    to_claude: bool = typer.Option(False, "--claude"),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p"),
    stdout_flag: bool = typer.Option(False, "--stdout"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """De-noise terminal / CI output: ANSI codes, progress bars, repeated lines."""
    if file is not None:
        if not file.is_file():
            _fail(f"{file}: not a file")
        # newline="": keep bare \r intact so progress bars can be collapsed.
        with file.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            raw = handle.read()
    else:
        raw = read_stdin()
    if not raw.strip():
        _fail("nothing to clean (pipe some output in, or pass a file)")

    text = clean_terminal(
        raw,
        strip_timestamps=not keep_timestamps,
        collapse_repeats=not keep_repeats,
        collapse_frames=not keep_frames,
        max_lines=max_lines,
    )
    mode = _resolve_mode(out, clip, to_claude or prompt is not None, stdout_flag)
    code = _emit(text + "\n", mode=mode, out=out, prompt=prompt, model=None, quiet=quiet)
    if not quiet:
        console.print(
            f"[cslim.text]clean[/] [cslim.dim]{len(raw.splitlines())} → "
            f"{len(text.splitlines())} lines[/]"
        )
    raise typer.Exit(code)


# --------------------------------------------------------------------------- #
# models / doctor / tui
# --------------------------------------------------------------------------- #


@app.command()
def models() -> None:
    """List known Claude models and their context windows."""
    table = Table(
        box=ROUNDED, border_style="cslim.border", header_style="cslim.title", padding=(0, 1)
    )
    table.add_column("alias", style="cslim.accent")
    table.add_column("model id", style="cslim.text")
    table.add_column("context", style="cslim.value", justify="right")
    table.add_column("max output", style="cslim.dim", justify="right")
    for spec in MODELS.values():
        table.add_row(spec.alias, spec.id, humanize(spec.context_window), humanize(spec.max_output))
    console.print(table)


@app.command()
def doctor() -> None:
    """Check the environment: clipboard, Claude Code, optional extras."""
    from .core.claude_pipe import claude_binary
    from .core.tokenizer import resolve_estimator

    table = Table(
        box=ROUNDED, border_style="cslim.border", header_style="cslim.title", padding=(0, 1)
    )
    table.add_column("check", style="cslim.text")
    table.add_column("status")

    def row(name: str, ok: bool, detail: str) -> None:
        mark = "[cslim.ok]✔[/]" if ok else "[cslim.warn]○[/]"
        table.add_row(name, f"{mark} [cslim.dim]{detail}[/]")

    binary = claude_binary()
    row("claude code", binary is not None, binary or "not in PATH (piping still works)")
    backend = clipboard_backend()
    row("clipboard", backend is not None, backend or "install xclip / wl-clipboard")
    estimator = resolve_estimator()
    row("tokenizer", True, f"{estimator.name} ({'exact' if estimator.exact else 'estimated'})")
    for module, purpose in (
        ("anthropic", "--exact token counts"),
        ("tiktoken", "offline BPE counts"),
        ("pathspec", ".gitignore support"),
        ("textual", "cslim tui"),
        ("pyperclip", "clipboard fallback"),
    ):
        try:
            __import__(module)
            row(module, True, purpose)
        except ImportError:
            row(module, False, f"missing — {purpose}")
    console.print(table)

    from .core.installer import hook_status

    hooks = Table(
        box=ROUNDED, border_style="cslim.border", header_style="cslim.title", padding=(0, 1)
    )
    hooks.add_column("hook scope", style="cslim.text")
    hooks.add_column("status")
    for scope, path, command in hook_status():
        if command:
            hooks.add_row(scope, f"[cslim.ok]✔[/] [cslim.dim]{escape(str(path))}[/]")
        else:
            hooks.add_row(scope, "[cslim.dim]○ not installed[/]")
    console.print(hooks)
    console.print("[cslim.dim]  enable automatic mode with:  cslim install[/]")


@app.command()
def hook(
    path: Optional[List[Path]] = typer.Option(None, "--path", help="Paths to map."),
    lang: Optional[List[str]] = typer.Option(None, "--lang", "-l"),
    max_tokens: Optional[int] = typer.Option(25_000, "--max-tokens", "-t"),
    aggressive: bool = typer.Option(False, "--aggressive", "-a"),
    min_files: int = typer.Option(8, "--min-files", help="Skip smaller projects."),
    index_only: Optional[bool] = typer.Option(
        None, "--index-only/--full-map",
        help="Force a tier. Default: chosen per repository size.",
    ),
    index_threshold: int = typer.Option(
        30, "--index-threshold",
        help="At or below this many files, prefer the ultralight index.",
    ),
    every_prompt: bool = typer.Option(
        False, "--every-prompt", help="Re-inject on every prompt (multiplies cost)."
    ),
    model: str = typer.Option("sonnet", "--model", "-m"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="No systemMessage."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the decision without marking the session."
    ),
) -> None:
    """Claude Code hook entry point: reads the event on stdin, injects the map.

    Not meant to be typed by hand — `cslim install` registers it. It always
    exits 0: a hook must degrade, never break a session.
    """
    from .core.hook import HookConfig, run_hook

    config = HookConfig(
        paths=tuple(path) if path else (),
        max_tokens=max_tokens,
        aggressive=aggressive,
        model=model,
        every_prompt=every_prompt,
        min_files=min_files,
        languages=_tuple(lang),
        quiet=quiet,
        index_only=index_only,
        index_threshold=index_threshold,
    )
    try:
        outcome = run_hook(sys.stdin.read(), config, dry_run=dry_run)
    except Exception as exc:
        if dry_run:
            console.print(f"[cslim.danger]✖[/] {escape(str(exc))}")
        raise typer.Exit(0) from None

    if dry_run:
        mark = "[cslim.ok]✔ inject[/]" if outcome.injected else "[cslim.dim]○ skip[/]"
        console.print(f"{mark} [cslim.dim]{escape(outcome.reason)}[/]")
        raise typer.Exit(0)

    if outcome.stdout:
        sys.stdout.write(outcome.stdout)
    raise typer.Exit(0)


@app.command("graph")
def graph_cmd(
    paths: Optional[List[Path]] = typer.Argument(None, help="Paths to analyse."),
    fmt: str = typer.Option("graphml", "--format", "-f", help="graphml | json | dot."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write to a file."),
    symbols: bool = typer.Option(True, "--symbols/--no-symbols", help="Include symbol nodes."),
    lang: Optional[List[str]] = typer.Option(None, "--lang", "-l"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Export the reference graph — who defines what, and who uses it.

    The same graph that decides which files earn a full skeleton under a token
    budget, in a form Gephi, Graphviz, networkx or D3 can open.
    """
    from .core.graphexport import build_graph, to_dot, to_graphml, to_json

    if fmt not in ("graphml", "json", "dot"):
        _fail("--format must be one of: graphml, json, dot")

    request = CompressRequest(
        paths=tuple(paths) if paths else (Path.cwd(),),
        discovery=DiscoveryOptions(languages=_languages(lang)),
        render_payload=False,
    )
    bundle = _run_with_progress(request, quiet)
    if not bundle.files:
        _fail("no source files found")

    graph = build_graph(bundle.files, symbols=symbols)
    if fmt == "graphml":
        text = to_graphml(graph)
    elif fmt == "json":
        text = to_json(graph)
    else:
        text = to_dot(graph, symbols=symbols)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    if not quiet:
        refs = sum(1 for e in graph.edges if e.kind == "references")
        console.print(
            f"[cslim.ok]✔[/] [cslim.text]{len(graph.file_nodes)} files, "
            f"{len(graph.symbol_nodes)} symbols, {refs} references[/]"
            + (f" [cslim.dim]→[/] [cslim.path]{escape(str(out))}[/]" if out else "")
        )
        if len(graph.file_nodes) != len(bundle.files):
            console.print("[cslim.danger]![/] node count does not match file count")


@app.command("test")
def test_cmd(
    command: Optional[List[str]] = typer.Argument(
        None, help="Test command (default: pytest -q). Use -- before its flags."
    ),
    context: int = typer.Option(3, "--context", "-C", help="Lines kept around a failure."),
    timeout: Optional[float] = typer.Option(None, "--timeout", help="Seconds before giving up."),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    clip: bool = typer.Option(False, "--clip", "-c"),
    to_claude: bool = typer.Option(False, "--claude"),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p"),
    stdout_flag: bool = typer.Option(False, "--stdout"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Run a test command and emit only what failed.

    Pure subtraction — nothing is injected, so it cannot cost more than not
    using it. Measured on a real 91-test run with one failure: -51.5% tokens.
    """
    from .core.testrun import run_tests
    from .core.traces import collapse_traces

    argv = list(command) if command else ["pytest", "-q"]
    result = run_tests(argv, context=context, timeout=timeout)

    if result.exit_code == 127:
        _fail(f"{result.output}\n  Pass the command explicitly, e.g. cslim test -- pytest -q")
        return

    body = collapse_traces(result.output)
    payload = body if body.strip() else "all tests passed"

    mode = _resolve_mode(out, clip, to_claude or prompt is not None, stdout_flag)
    code = _emit(payload + "\n", mode=mode, out=out, prompt=prompt, model=None, quiet=quiet)

    if not quiet:
        verdict = (
            "[cslim.ok]✔ passed[/]" if result.passed else "[cslim.danger]✖ failed[/]"
        )
        console.print(
            f"{verdict} [cslim.dim]{' '.join(argv)} · "
            f"{result.raw_lines} → {len(payload.splitlines())} lines[/]"
        )
        for warning in result.warnings:
            console.print(f"  [cslim.warn]![/] [cslim.dim]{escape(warning)}[/]")
    # the command's own verdict is the useful exit code
    raise typer.Exit(result.exit_code if result.exit_code else code)


@app.command("map")
def map_cmd(
    paths: Optional[List[Path]] = typer.Argument(None, help="Paths to map (default: cwd)."),
    lang: Optional[List[str]] = typer.Option(None, "--lang", "-l"),
    max_tokens: int = typer.Option(25_000, "--max-tokens", "-t"),
    aggressive: bool = typer.Option(False, "--aggressive", "-a"),
    index_only: Optional[bool] = typer.Option(
        None, "--index-only/--full-map", help="Force a tier. Default: per repository size."
    ),
    index_threshold: int = typer.Option(30, "--index-threshold"),
    model: str = typer.Option("sonnet", "--model", "-m"),
    remove: bool = typer.Option(False, "--remove", help="Strip the section and exit."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the section, write nothing."),
) -> None:
    """Write the repository map into CLAUDE.md.

    Measured on flask, this delivery costs about a fifth of what the
    UserPromptSubmit hook costs for an identical payload — CLAUDE.md rides the
    prefix Claude Code already caches. See bench/README.md.
    """
    from .core.delivery import remove_map, render_section, write_map
    from .core.hook import HookConfig, build_map

    root = Path.cwd()

    if remove:
        result = remove_map(root)
        if result.action == "removed":
            console.print(
                f"[cslim.ok]✔[/] [cslim.text]map removed from[/] "
                f"[cslim.path]{escape(str(result.path))}[/]"
            )
        else:
            console.print("[cslim.dim]○ no cslim map in CLAUDE.md[/]")
        raise typer.Exit()

    config = HookConfig(
        paths=tuple(paths) if paths else (),
        max_tokens=max_tokens,
        aggressive=aggressive,
        model=model,
        languages=_tuple(lang),
        index_only=index_only,
        index_threshold=index_threshold,
        min_files=0,  # an explicit `cslim map` is a decision, not a heuristic
    )
    try:
        payload, tokens, files, _cached, tier_index = build_map(config, root)
    except Exception as exc:
        _fail(f"could not build the map: {type(exc).__name__}: {exc}")
        return
    if not payload:
        _fail(f"no source files found under {root}")
        return

    if dry_run:
        sys.stdout.write(render_section(payload) + "\n")
        raise typer.Exit()

    result = write_map(payload, project_dir=root, tokens=tokens, files=files)
    if result.action == "absent":
        _fail("; ".join(result.warnings) or "could not write CLAUDE.md")
        return

    tier = "index" if tier_index else "skeleton"
    verb = {"created": "written to", "updated": "refreshed in", "unchanged": "already current in"}
    mark = "○" if result.action == "unchanged" else "✔"
    console.print(
        f"[cslim.ok]{mark}[/] [cslim.text]{tier} map ({humanize(tokens)} tokens, "
        f"{files} files) {verb[result.action]}[/] [cslim.path]{escape(str(result.path))}[/]"
    )
    for warning in result.warnings:
        for line in warning.splitlines():
            console.print(f"  [cslim.warn]![/] [cslim.dim]{escape(line)}[/]" if line.strip()
                          and not line.startswith("    ") else f"    [cslim.accent]{escape(line.strip())}[/]")
    console.print(
        "  [cslim.dim]refresh it when the architecture changes — not on a timer:\n"
        "    rewriting CLAUDE.md changes the cached prefix that makes this cheap.[/]"
    )


@app.command()
def install(
    scope: str = typer.Option(
        "project", "--scope", "-s", help="project | local | user."
    ),
    path: Optional[List[Path]] = typer.Option(None, "--path", help="Paths to map."),
    lang: Optional[List[str]] = typer.Option(None, "--lang", "-l"),
    max_tokens: int = typer.Option(25_000, "--max-tokens", "-t"),
    aggressive: bool = typer.Option(False, "--aggressive", "-a"),
    min_files: int = typer.Option(8, "--min-files"),
    index_only: Optional[bool] = typer.Option(
        None, "--index-only/--full-map",
        help="Force a tier. Default: chosen per repository size.",
    ),
    index_threshold: int = typer.Option(30, "--index-threshold"),
    delivery: str = typer.Option(
        "hook", "--delivery", "-d",
        help="hook | claude-md | none. claude-md measured ~5x cheaper than hook.",
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Set up automatic mode, choosing how the map reaches Claude."""
    from .core.delivery import DeliveryMode, remove_map
    from .core.installer import InstallScope, hook_command, install_hook, uninstall_hook

    if scope not in (InstallScope.PROJECT, InstallScope.LOCAL, InstallScope.USER):
        _fail("--scope must be one of: project, local, user")
    try:
        mode = DeliveryMode(delivery)
    except ValueError:
        _fail("--delivery must be one of: hook, claude-md, none")
        return

    if mode is DeliveryMode.NONE:
        hook_result = uninstall_hook(scope)
        map_result = remove_map(Path.cwd())
        console.print(
            f"[cslim.ok]✔[/] [cslim.text]delivery disabled[/] [cslim.dim]— hook "
            f"{hook_result.action}, CLAUDE.md map {map_result.action}[/]"
        )
        raise typer.Exit()

    if mode is DeliveryMode.CLAUDE_MD:
        # Two live deliveries would inject the map twice and pay for it twice,
        # so adopting claude-md retires the hook rather than stacking on it.
        removed = uninstall_hook(scope)
        if removed.action == "removed":
            console.print("[cslim.dim]○ removed the UserPromptSubmit hook (delivery moved)[/]")
        console.print(
            "  [cslim.dim]now run[/] [cslim.accent]cslim map[/] "
            "[cslim.dim]to write it, and again when the architecture changes.[/]\n"
            "  [cslim.dim]measured on flask: +36% vs no map, against +176% "
            "through the hook (bench/README.md).[/]"
        )
        raise typer.Exit()

    command = hook_command(
        paths=tuple(str(p) for p in (path or ())),
        max_tokens=max_tokens,
        aggressive=aggressive,
        min_files=min_files,
        languages=_tuple(lang),
        quiet=quiet,
        index_only=index_only,
        index_threshold=index_threshold,
    )
    try:
        result = install_hook(scope, command=command)
    except (ValueError, OSError) as exc:
        _fail(str(exc))
        return

    symbol = {"installed": "✔", "updated": "✔", "unchanged": "○"}[result.action]
    console.print(
        f"[cslim.ok]{symbol}[/] [cslim.text]hook {result.action}[/] "
        f"[cslim.dim]in[/] [cslim.path]{escape(str(result.path))}[/]"
    )
    console.print(f"  [cslim.dim]{escape(result.command)}[/]")
    if result.backup:
        console.print(f"  [cslim.dim]backup: {escape(str(result.backup))}[/]")
    console.print(
        "  [cslim.dim]restart Claude Code (or run /hooks) to pick it up[/]"
    )
    # Our own A/B benchmarks say this costs money. Shipping the feature while
    # hiding that would make the tool's headline claim its least honest part.
    console.print(
        "\n  [cslim.warn]![/] [cslim.dim]automatic mode is a "
        "[/][cslim.text]context-window[/][cslim.dim] tool, not a cost saver.\n"
        "    Measured on flask: ~18-25% more $ per session, at 2 and at 12 turns.\n"
        "    See bench/README.md. `cslim diff` and `cslim clean` are the net wins.[/]"
    )


@app.command()
def uninstall(
    scope: str = typer.Option("project", "--scope", "-s", help="project | local | user."),
) -> None:
    """Remove the cslim hook, leaving every other hook untouched."""
    from .core.installer import uninstall_hook

    try:
        result = uninstall_hook(scope)
    except (ValueError, OSError) as exc:
        _fail(str(exc))
        return
    if result.action == "absent":
        console.print(f"[cslim.dim]○ no cslim hook in {escape(str(result.path))}[/]")
    else:
        console.print(
            f"[cslim.ok]✔[/] [cslim.text]hook removed from[/] "
            f"[cslim.path]{escape(str(result.path))}[/]"
        )


@app.command()
def tui(
    paths: Optional[List[Path]] = typer.Argument(None, help="Initial paths to load."),
) -> None:
    """Launch the interactive Textual UI."""
    try:
        from .tui.app import ClaudeSlimApp
    except ImportError as exc:
        _fail(
            f"Textual UI unavailable: {exc}\n"
            "  Install it with:  pip install textual\n"
            "  Or, if cslim runs as a uv tool:  "
            "uv tool install --editable . --with textual"
        )
        return
    ClaudeSlimApp(paths=[Path(p) for p in (paths or [Path.cwd()])]).run()
