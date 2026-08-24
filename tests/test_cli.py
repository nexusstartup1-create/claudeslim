"""CLI-level tests: argument routing and terminal-output safety."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from cslim.cli import app
from cslim.main import _normalize

runner = CliRunner()


def test_exact_fails_loudly_instead_of_silently_estimating(tmp_path: Path) -> None:
    """`--exact` used to fall back to the heuristic without saying so.

    A budget computed from estimates while the user explicitly asked for API
    counts is a silent lie, so an unavailable exact estimator is now an error.
    """
    (tmp_path / "m.py").write_text("def f() -> None:\n    pass\n", encoding="utf-8")
    result = runner.invoke(app, ["stats", str(tmp_path), "--exact"])
    if "exact token counting needs" in result.output:
        assert result.exit_code == 1
        assert "Drop --exact" in result.output
    else:  # anthropic is installed and configured: counts must be real
        assert result.exit_code == 0


def test_implicit_pack_command() -> None:
    assert _normalize(["./src"]) == ["pack", "./src"]
    assert _normalize(["main.py", "-q"]) == ["pack", "main.py", "-q"]
    assert _normalize(["diff", "--staged"]) == ["diff", "--staged"]
    assert _normalize(["--version"]) == ["--version"]
    assert _normalize([]) == []


def test_error_messages_are_not_eaten_by_rich_markup(tmp_path: Path) -> None:
    """Regression: `pip install 'pkg[tui]'` used to print as `pip install 'pkg'`.

    Rich parses `[tui]` as a style tag, so every error string must be escaped
    before printing — otherwise cslim hands out install commands that silently
    install the wrong package.
    """
    result = runner.invoke(app, ["pack", str(tmp_path), "--lang", "rust[tui]"])
    assert result.exit_code != 0
    assert "unknown language: rust[tui]" in result.output


def test_bracketed_paths_survive_the_files_table(tmp_path: Path) -> None:
    """Next.js dynamic routes (`pages/[id].tsx`) are paths, not markup."""
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "[id].tsx").write_text(
        "export function Page(): null { return null; }\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["pack", str(tmp_path), "--files", "--stdout"])
    assert result.exit_code == 0
    assert "[id].tsx" in result.output


def test_no_sources_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["pack", str(tmp_path)])
    assert result.exit_code == 1
    assert "no source files found" in result.output


def test_stats_json_is_machine_readable(tmp_path: Path) -> None:
    import json

    (tmp_path / "m.py").write_text("def f(x: int) -> int:\n    return x\n", encoding="utf-8")
    result = runner.invoke(app, ["stats", str(tmp_path), "--json"])
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["stats"]["files"] == 1
    assert "payload" not in report


def test_clean_collapses_progress_bars_read_from_a_file(tmp_path: Path) -> None:
    """Regression: text-mode reads rewrite bare \\r into \\n.

    That silently defeated the progress-bar collapsing, because by the time
    clean_terminal saw the text every redraw had already become its own line.
    """
    log = tmp_path / "build.log"
    log.write_bytes(
        b"building\n"
        b"Progress: [####      ] 40%\rProgress: [##########] 100%\n"
        b"done\n"
    )
    result = runner.invoke(app, ["clean", str(log), "--stdout"])
    assert result.exit_code == 0
    assert "40%" not in result.stdout, "only the final redraw survives"
    assert "Progress: [##########] 100%" in result.stdout
    assert "building" in result.stdout and "done" in result.stdout


def test_diff_outside_a_repo_explains_itself(tmp_path: Path, monkeypatch: object) -> None:
    """Regression: git answered this with `unknown option 'cached'` + 100 usage lines.

    Outside a working tree git falls back to --no-index mode, where --cached
    doesn't exist, so its error describes the wrong problem entirely.
    """
    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)  # a directory with no .git anywhere above it
        result = runner.invoke(app, ["diff", "--staged"])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 1
    assert "not a git repository" in result.output
    assert "git init" in result.output
    assert "unknown option" not in result.output
    assert len(result.output.splitlines()) < 8, "no wall of git usage text"


def test_models_and_doctor_run() -> None:
    assert runner.invoke(app, ["models"]).exit_code == 0
    assert runner.invoke(app, ["doctor"]).exit_code == 0
