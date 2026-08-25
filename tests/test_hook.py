"""Automatic mode: hook decisions and settings.json surgery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cslim.core.hook import HookConfig, run_hook
from cslim.core.installer import (
    InstallScope,
    _is_ours,
    hook_status,
    install_hook,
    settings_path,
    uninstall_hook,
)

PY = "def f(x: int) -> int:\n    total = 0\n    for i in range(x):\n        total += i\n    return total\n"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the developer's real cache while testing."""
    monkeypatch.setenv("CSLIM_STATE_DIR", str(tmp_path / "state"))


def make_repo(root: Path, count: int = 12) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (root / f"m{i}.py").write_text(PY, encoding="utf-8")
    return root


def event(cwd: Path, session: str = "s1") -> str:
    return json.dumps(
        {
            "session_id": session,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(cwd),
            "prompt": "dove sta la logica di auth?",
        }
    )


# --------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------- #


def test_injects_a_valid_hook_payload(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    outcome = run_hook(event(repo), HookConfig(paths=(repo,), index_only=False))
    assert outcome.injected
    payload = json.loads(outcome.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "ClaudeSlim" in context and "def f(x: int) -> int:" in context
    assert "total += i" not in context, "bodies are elided"


def test_injects_only_once_per_session(tmp_path: Path) -> None:
    """The rule that decides whether this tool saves or wastes tokens.

    UserPromptSubmit fires on every prompt; re-injecting the map each time would
    multiply usage instead of reducing it.
    """
    repo = make_repo(tmp_path / "repo")
    config = HookConfig(paths=(repo,))
    first = run_hook(event(repo, "session-a"), config)
    second = run_hook(event(repo, "session-a"), config)
    third = run_hook(event(repo, "session-b"), config)

    assert first.injected
    assert not second.injected and "already injected" in second.reason
    assert second.stdout == "", "a skipped hook must print nothing"
    assert third.injected, "a different session gets its own map"


def test_every_prompt_opt_in(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    config = HookConfig(paths=(repo,), every_prompt=True)
    assert run_hook(event(repo), config).injected
    assert run_hook(event(repo), config).injected


def test_same_session_different_project_gets_its_own_map(tmp_path: Path) -> None:
    one = make_repo(tmp_path / "one")
    two = make_repo(tmp_path / "two")
    assert run_hook(event(one, "s"), HookConfig(paths=(one,))).injected
    assert run_hook(event(two, "s"), HookConfig(paths=(two,))).injected


def test_skips_small_projects(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "small", count=3)
    outcome = run_hook(event(repo), HookConfig(paths=(repo,), min_files=8))
    assert not outcome.injected
    assert "not worth it" in outcome.reason


def test_respects_the_token_budget(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", count=40)
    outcome = run_hook(event(repo), HookConfig(paths=(repo,), max_tokens=300))
    assert outcome.injected
    assert outcome.tokens <= 300


def test_second_build_hits_the_cache(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    config = HookConfig(paths=(repo,), every_prompt=True)
    assert not run_hook(event(repo), config).cached
    assert run_hook(event(repo), config).cached


def test_cache_invalidated_when_a_file_changes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    config = HookConfig(paths=(repo,), every_prompt=True, index_only=False)
    run_hook(event(repo), config)
    (repo / "brand_new.py").write_text("def added() -> None:\n    pass\n", encoding="utf-8")
    outcome = run_hook(event(repo), config)
    assert not outcome.cached
    assert "def added() -> None:" in json.loads(outcome.stdout)[
        "hookSpecificOutput"
    ]["additionalContext"]


def test_stands_down_when_a_map_is_already_in_the_prompt(tmp_path: Path) -> None:
    """`cslim . | claude -p "..."` with automatic mode on would pay twice."""
    repo = make_repo(tmp_path / "repo")
    piped = json.dumps(
        {
            "session_id": "piped",
            "cwd": str(repo),
            "prompt": "# ClaudeSlim skeleton bundle\n\n## m0.py\nwhat does this do?",
        }
    )
    outcome = run_hook(piped, HookConfig(paths=(repo,)))
    assert not outcome.injected
    assert "already in the prompt" in outcome.reason
    assert outcome.stdout == ""


def test_never_raises_on_bad_input(tmp_path: Path) -> None:
    for bad in ("", "not json", "[]", "null", '{"cwd": 12}'):
        outcome = run_hook(bad, HookConfig(paths=(tmp_path,)))
        assert not outcome.injected
        assert outcome.stdout == ""


def test_dry_run_does_not_consume_the_session(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    config = HookConfig(paths=(repo,))
    assert run_hook(event(repo, "s9"), config, dry_run=True).injected
    assert run_hook(event(repo, "s9"), config).injected, "dry run left no trace"


# --------------------------------------------------------------------------- #
# settings.json surgery
# --------------------------------------------------------------------------- #


def test_install_is_idempotent(tmp_path: Path) -> None:
    assert install_hook(InstallScope.PROJECT, project_dir=tmp_path).action == "installed"
    assert install_hook(InstallScope.PROJECT, project_dir=tmp_path).action == "unchanged"

    data = json.loads(settings_path(InstallScope.PROJECT, tmp_path).read_text())
    groups = data["hooks"]["UserPromptSubmit"]
    assert len(groups) == 1
    assert groups[0]["hooks"][0]["type"] == "command"
    assert _is_ours(groups[0]["hooks"][0]), groups[0]["hooks"][0]["command"]


def test_install_preserves_unrelated_settings(tmp_path: Path) -> None:
    path = settings_path(InstallScope.PROJECT, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "model": "opus",
                "permissions": {"allow": ["Bash(npm test)"]},
                "hooks": {
                    "UserPromptSubmit": [
                        {"matcher": "*", "hooks": [{"type": "command", "command": "echo hi"}]}
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}],
                },
            }
        ),
        encoding="utf-8",
    )
    install_hook(InstallScope.PROJECT, project_dir=tmp_path)
    data = json.loads(path.read_text())

    assert data["model"] == "opus"
    assert data["permissions"] == {"allow": ["Bash(npm test)"]}
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "notify"
    commands = [
        h["command"]
        for g in data["hooks"]["UserPromptSubmit"]
        for h in g["hooks"]
    ]
    assert "echo hi" in commands, "somebody else's hook survived"
    assert any(_is_ours({"command": c}) for c in commands)


def test_uninstall_removes_only_ours(tmp_path: Path) -> None:
    path = settings_path(InstallScope.PROJECT, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"matcher": "*", "hooks": [{"type": "command", "command": "echo hi"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    install_hook(InstallScope.PROJECT, project_dir=tmp_path)
    assert uninstall_hook(InstallScope.PROJECT, project_dir=tmp_path).action == "removed"

    data = json.loads(path.read_text())
    commands = [
        h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]
    ]
    assert commands == ["echo hi"]
    assert uninstall_hook(InstallScope.PROJECT, project_dir=tmp_path).action == "absent"


def test_install_backs_up_before_writing(tmp_path: Path) -> None:
    path = settings_path(InstallScope.PROJECT, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"model": "opus"}', encoding="utf-8")
    result = install_hook(InstallScope.PROJECT, project_dir=tmp_path)
    assert result.backup is not None and result.backup.is_file()
    assert json.loads(result.backup.read_text()) == {"model": "opus"}


def test_invalid_settings_json_is_reported_not_clobbered(tmp_path: Path) -> None:
    path = settings_path(InstallScope.PROJECT, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        install_hook(InstallScope.PROJECT, project_dir=tmp_path)
    assert path.read_text() == "{ this is not json", "left untouched"


def test_hook_status_reports_scopes(tmp_path: Path) -> None:
    install_hook(InstallScope.PROJECT, project_dir=tmp_path)
    status = dict((scope, cmd) for scope, _path, cmd in hook_status(tmp_path))
    assert status[InstallScope.PROJECT]
    assert _is_ours({"command": status[InstallScope.PROJECT]})
    assert status[InstallScope.LOCAL] is None


# --------------------------------------------------------------------------- #
# tier selection
# --------------------------------------------------------------------------- #


def test_small_repo_gets_the_ultralight_index(tmp_path: Path) -> None:
    """Cache pricing makes a skeleton map a bad deal on a small project.

    A freshly injected token costs roughly what twelve cache-read tokens cost,
    so on a repo Claude can explore cheaply only the index can come out ahead.
    """
    repo = make_repo(tmp_path / "repo", count=12)
    outcome = run_hook(event(repo), HookConfig(paths=(repo,)))
    context = json.loads(outcome.stdout)["hookSpecificOutput"]["additionalContext"]

    assert "index" in outcome.reason
    assert "No signatures" in context
    assert "def f(" not in context, "no signatures in the index tier"
    assert "m0.py" in context, "but every file is still named"


def test_large_repo_gets_full_skeletons(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", count=40)
    outcome = run_hook(event(repo), HookConfig(paths=(repo,)))
    context = json.loads(outcome.stdout)["hookSpecificOutput"]["additionalContext"]

    assert "skeleton" in outcome.reason
    assert "def f(x: int) -> int:" in context


def test_threshold_is_configurable(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", count=12)
    forced = run_hook(
        event(repo, "s-low"), HookConfig(paths=(repo,), index_threshold=5)
    )
    assert "skeleton" in forced.reason


def test_explicit_flag_overrides_the_heuristic(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", count=40)
    outcome = run_hook(event(repo), HookConfig(paths=(repo,), index_only=True))
    assert "index" in outcome.reason


def test_index_tier_stays_under_a_thousand_tokens(tmp_path: Path) -> None:
    """The whole point of the tier: cheap enough to clear the cache break-even."""
    repo = make_repo(tmp_path / "repo", count=25)
    outcome = run_hook(event(repo), HookConfig(paths=(repo,)))
    assert outcome.tokens < 1000, outcome.tokens


def test_preamble_matches_the_tier(tmp_path: Path) -> None:
    """A preamble promising signatures over an index sends Claude hunting."""
    repo = make_repo(tmp_path / "repo", count=12)
    index_ctx = json.loads(
        run_hook(event(repo, "a"), HookConfig(paths=(repo,))).stdout
    )["hookSpecificOutput"]["additionalContext"]
    skeleton_ctx = json.loads(
        run_hook(event(repo, "b"), HookConfig(paths=(repo,), index_only=False)).stdout
    )["hookSpecificOutput"]["additionalContext"]

    assert "No signatures" in index_ctx
    assert "signatures, types" in skeleton_ctx
    assert "Function bodies are elided" not in index_ctx


# --------------------------------------------------------------------------- #
# platform-independent hook matching
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "ours"),
    [
        ("/usr/local/bin/cslim hook --max-tokens 25000", True),
        (r"C:\Users\a\AppData\Roaming\Python\Scripts\cslim.exe hook --quiet", True),
        (r'"C:\Program Files\cslim.exe" hook', True),
        ("/usr/bin/python -m cslim.main hook --path src", True),
        ("echo hi", False),
        ("/usr/bin/cslimmer hookah", False),
    ],
)
def test_recognises_our_hook_whatever_the_executable_is_called(
    command: str, ours: bool
) -> None:
    """Regression: matching the literal "cslim hook" broke on Windows.

    There the executable is `cslim.exe`, so the command reads "cslim.exe hook",
    the marker never matched, install was not idempotent and uninstall found
    nothing to remove. CI on windows-latest is what surfaced it.
    """
    from cslim.core.installer import _is_ours

    assert _is_ours({"command": command}) is ours
