"""CLAUDE.md delivery: splicing, idempotence, and the git hazards."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cslim.core.delivery import (
    BEGIN_MARKER,
    END_MARKER,
    DeliveryMode,
    claude_md_path,
    git_state,
    read_section,
    remove_map,
    render_section,
    write_map,
)

MAP = "## src/app.py\n```python\ndef main() -> None:\n    ...\n```"


def git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q"], ["config", "user.email", "t@t.t"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


# --------------------------------------------------------------------------- #
# splicing
# --------------------------------------------------------------------------- #


def test_creates_the_file_when_absent(tmp_path: Path) -> None:
    result = write_map(MAP, project_dir=tmp_path, tokens=120, files=3)
    assert result.action == "created"
    text = claude_md_path(tmp_path).read_text()
    assert BEGIN_MARKER in text and END_MARKER in text
    assert "def main() -> None:" in text


def test_never_touches_text_outside_the_markers(tmp_path: Path) -> None:
    """The whole contract: a user's own CLAUDE.md must survive verbatim."""
    path = claude_md_path(tmp_path)
    before = "# My project\n\nAlways run `make check` before committing.\n"
    path.write_text(before, encoding="utf-8")

    write_map(MAP, project_dir=tmp_path)
    write_map(MAP.replace("main", "run"), project_dir=tmp_path)
    after = path.read_text()

    assert after.startswith(before.rstrip("\n"))
    assert "Always run `make check` before committing." in after
    assert after.count(BEGIN_MARKER) == 1, "refresh replaces, never appends a second block"
    assert "def run() -> None:" in after
    assert "def main() -> None:" not in after


def test_refresh_is_idempotent(tmp_path: Path) -> None:
    """An identical rewrite would dirty the very prefix that makes this cheap."""
    write_map(MAP, project_dir=tmp_path)
    mtime = claude_md_path(tmp_path).stat().st_mtime_ns
    result = write_map(MAP, project_dir=tmp_path)
    assert result.action == "unchanged"
    assert not result.changed
    assert claude_md_path(tmp_path).stat().st_mtime_ns == mtime, "file was not rewritten"


def test_removal_leaves_the_rest_intact(tmp_path: Path) -> None:
    path = claude_md_path(tmp_path)
    path.write_text("# Notes\n\nkeep me\n", encoding="utf-8")
    write_map(MAP, project_dir=tmp_path)

    result = remove_map(tmp_path)
    assert result.action == "removed"
    text = path.read_text()
    assert "keep me" in text
    assert BEGIN_MARKER not in text and "def main" not in text


def test_removal_deletes_a_file_that_was_only_ours(tmp_path: Path) -> None:
    write_map(MAP, project_dir=tmp_path)
    assert remove_map(tmp_path).action == "removed"
    assert not claude_md_path(tmp_path).exists()


def test_removal_on_a_file_without_our_section(tmp_path: Path) -> None:
    path = claude_md_path(tmp_path)
    path.write_text("# Untouched\n", encoding="utf-8")
    assert remove_map(tmp_path).action == "absent"
    assert path.read_text() == "# Untouched\n"


def test_truncated_markers_do_not_corrupt_the_file(tmp_path: Path) -> None:
    """A half-written block (interrupted run, manual edit) must not be spliced."""
    path = claude_md_path(tmp_path)
    path.write_text(f"# Notes\n\n{BEGIN_MARKER}\nhalf a map, no end marker\n", encoding="utf-8")
    result = write_map(MAP, project_dir=tmp_path)
    assert result.action == "updated"
    text = path.read_text()
    assert "# Notes" in text
    assert text.count(END_MARKER) == 1


def test_render_section_round_trips(tmp_path: Path) -> None:
    section = render_section(MAP)
    assert read_section(f"lead\n{section}\ntrail") == section


# --------------------------------------------------------------------------- #
# git hazards
# --------------------------------------------------------------------------- #


def test_no_git_is_not_a_hazard(tmp_path: Path) -> None:
    result = write_map(MAP, project_dir=tmp_path)
    assert result.git == "no-git"
    assert result.warnings == []


def test_untracked_file_warns_about_committing_it(tmp_path: Path) -> None:
    repo = git_repo(tmp_path / "repo")
    result = write_map(MAP, project_dir=repo)
    assert result.git == "untracked"
    assert any(".gitignore" in w for w in result.warnings)


def test_gitignored_file_is_the_clean_case(tmp_path: Path) -> None:
    repo = git_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("CLAUDE.md\n", encoding="utf-8")
    result = write_map(MAP, project_dir=repo)
    assert result.git == "ignored"
    assert result.warnings == []


def test_tracked_file_warns_about_diff_noise(tmp_path: Path) -> None:
    repo = git_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("# Project\n", encoding="utf-8")
    subprocess.run(["git", "add", "CLAUDE.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=repo, check=True, capture_output=True)

    result = write_map(MAP, project_dir=repo)
    assert result.git == "tracked"
    assert any("diffs" in w for w in result.warnings)
    assert any("git rm --cached" in w for w in result.warnings)


def test_git_state_helper_matches(tmp_path: Path) -> None:
    repo = git_repo(tmp_path / "repo")
    assert git_state(claude_md_path(repo)) == "untracked"


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["hook", "claude-md", "none"])
def test_delivery_modes_parse(value: str) -> None:
    assert DeliveryMode(value).value == value


def test_unknown_delivery_mode_rejected() -> None:
    with pytest.raises(ValueError):
        DeliveryMode("proxy")
