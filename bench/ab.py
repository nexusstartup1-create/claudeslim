#!/usr/bin/env python3
"""A/B harness: does the cslim hook actually reduce what a session costs?

Every claim in the README about saving tokens compares a compressed payload to
the whole codebase serialized — a scenario that never happens, because Claude
Code reads selectively. This script measures the thing that does happen: the
same task, run twice, with and without the hook, billed by Anthropic.

Ground truth comes from ``claude -p --output-format json``, which reports the
real usage breakdown and ``total_cost_usd`` per run. We don't estimate anything.

    python bench/ab.py --tasks 3 --repeats 2          # one-shot sessions
    python bench/ab.py --turns 3                      # multi-turn sessions

Read METHOD below before quoting any number this prints.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cslim.core.installer import (  # noqa: E402
    InstallScope,
    hook_command,
    install_hook,
    settings_path,
    uninstall_hook,
)

METHOD = """\
METHOD & CAVEATS — read before quoting these numbers.

* Cost is Anthropic's own `total_cost_usd`, not our estimate. Cache reads are
  roughly a tenth the price of fresh input, so raw token counts overstate
  savings; cost is the honest headline.
* One-shot mode is cslim's WORST case. Each `claude -p` is a fresh session, so
  the map is paid for once per task and amortizes over nothing. Real interactive
  sessions ask many questions against one injection — use --turns for that.
* Claude explores differently run to run. With few repeats the spread can exceed
  the effect; the spread is printed, and if it swamps the difference the honest
  conclusion is "no measurable effect", not the mean.
* Arms are interleaved to spread prompt-cache warming evenly. Fixed per-run
  overhead (system prompt + tool definitions, ~16k cache-creation tokens) is
  identical in both arms and dilutes the measured percentage.
"""

#: Questions that require locating code — the case a map is supposed to help.
DEFAULT_TASKS = [
    "Which file and function decides whether a file gets a full skeleton or "
    "just an index line? Answer with the path and the function name only.",
    "Which module copies text to the clipboard, and which backends does it try "
    "on Linux? Answer with the path and the backend names only.",
    "How does the tool avoid injecting its map twice in the same session? "
    "Answer with the path and the mechanism in one sentence.",
    "Where are git diff hunks that only change whitespace discarded? Answer "
    "with the path and function name only.",
]

#: Location questions for pallets/flask, used by the large-repo benchmark.
#: Ordered so a long session walks the codebase the way a developer would.
FLASK_TASKS = [
    "Which file and class implements the application context, and which "
    "attribute holds the pushed context stack? Path and names only.",
    "Where is the blueprint's URL prefix combined with a route rule? Answer "
    "with the file and the method name only.",
    "Which file decides the session cookie's SameSite and Secure flags? "
    "Answer with the file and the method name only.",
    "Where does Flask load .env files, and which function does it? Path and "
    "function name only.",
    "Which module turns a view's return value into a Response object, and "
    "which method does it? Path and method name only.",
    "Where is the JSON provider configured, and which class implements the "
    "default one? Path and class name only.",
    "Which function registers error handlers, and where are they looked up "
    "when an exception is raised? Two paths and two names.",
    "Where is the `before_request` chain executed? Path and method name only.",
    "Which file defines the CLI entry point for `flask run`? Path and "
    "function name only.",
    "Where does Flask decide the static folder URL rule? Path and method only.",
    "Which class holds application configuration, and which method loads it "
    "from an object? Path, class and method name.",
    "Where is the request context popped, and what guarantees it happens on "
    "an exception? Path and mechanism in one sentence.",
    "Which module implements the test client, and which class does it "
    "subclass? Path and class names only.",
    "Where are template globals injected before rendering? Path and method "
    "name only.",
    "Which function generates a URL for an endpoint, and where does it read "
    "the url adapter from? Path and names only.",
]


@dataclass(slots=True)
class Run:
    arm: str
    task: int
    repeat: int
    cost_usd: float = 0.0
    input_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    output_tokens: int = 0
    turns: int = 0
    duration_s: float = 0.0
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation
            + self.cache_read
            + self.output_tokens
        )


@dataclass(slots=True)
class Arm:
    """Results for one arm, aggregated by SESSION.

    The session is the right unit of analysis: inside a multi-turn session the
    first turn costs far more than the rest (it builds the cache), so treating
    turns as independent samples reports a spread that is an artefact of turn
    order rather than real run-to-run variation.
    """

    name: str
    sessions: list[list[Run]] = field(default_factory=list)

    def add(self, runs: list[Run]) -> None:
        self.sessions.append(runs)

    @property
    def runs(self) -> list[Run]:
        return [r for session in self.sessions for r in session]

    @property
    def ok(self) -> list[list[Run]]:
        return [s for s in self.sessions if s and not any(r.error for r in s)]

    def totals(self, attr: str) -> list[float]:
        return [sum(getattr(r, attr) for r in s) for s in self.ok]

    def mean(self, attr: str) -> float:
        values = self.totals(attr)
        return statistics.fmean(values) if values else 0.0

    def stdev(self, attr: str) -> float:
        values = self.totals(attr)
        return statistics.stdev(values) if len(values) > 1 else 0.0


def run_claude(
    prompt: str, cwd: Path, resume: str | None = None, timeout: int = 300
) -> dict[str, object]:
    command = ["claude", "-p", prompt, "--output-format", "json"]
    if resume:
        command += ["--resume", resume]
    started = time.time()
    proc = subprocess.run(
        command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        return {"_error": (proc.stderr or proc.stdout)[:300]}
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return {"_error": f"unparseable output: {proc.stdout[:200]}"}
    payload["_duration"] = time.time() - started
    return payload


def record(arm: str, task: int, repeat: int, payload: dict[str, object]) -> Run:
    run = Run(arm=arm, task=task, repeat=repeat)
    if "_error" in payload:
        run.error = str(payload["_error"])
        return run
    usage = payload.get("usage") or {}
    assert isinstance(usage, dict)
    run.cost_usd = float(payload.get("total_cost_usd") or 0.0)
    run.input_tokens = int(usage.get("input_tokens") or 0)
    run.cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    run.cache_read = int(usage.get("cache_read_input_tokens") or 0)
    run.output_tokens = int(usage.get("output_tokens") or 0)
    run.turns = int(payload.get("num_turns") or 0)
    run.duration_s = float(payload.get("_duration") or 0.0)
    return run


#: The arms compared by default. `None` means no hook at all (the control).
ARMS: dict[str, dict[str, object] | None] = {
    "no hook": None,
    "full AST map": {"index_only": False},
    "index only": {"index_only": True},
}


def set_arm(spec: dict[str, object] | None, project: Path, max_tokens: int) -> None:
    if spec is None:
        uninstall_hook(InstallScope.PROJECT, project_dir=project)
        return
    install_hook(
        InstallScope.PROJECT,
        command=hook_command(max_tokens=max_tokens, **spec),  # type: ignore[arg-type]
        project_dir=project,
    )


def session(tasks: list[str], cwd: Path, turns: int, arm: str, repeat: int) -> list[Run]:
    """One session: `turns` prompts, resuming so the map is paid for once."""
    runs: list[Run] = []
    resume: str | None = None
    for index, prompt in enumerate(tasks[:turns]):
        payload = run_claude(prompt, cwd, resume=resume)
        run = record(arm, index, repeat, payload)
        runs.append(run)
        if run.error:
            break
        resume = str(payload.get("session_id") or "") or None
    return runs


def report_arms(arms: list[Arm], turns: int) -> None:
    """Compare every arm against the first one, which is the control."""
    control = arms[0]
    base_cost = control.mean("cost_usd")

    print("\n" + "=" * 78)
    print(f"RESULT — {turns} turn(s) per session, "
          f"{len(control.ok)} session(s) per arm")
    print("=" * 78)
    header = f"  {'arm':<15} {'tokens':>10} {'cache write':>12} {'cost/session':>13} {'vs control':>11}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for arm in arms:
        if not arm.ok:
            print(f"  {arm.name:<15} {'(no successful sessions)':>48}")
            continue
        cost = arm.mean("cost_usd")
        delta = (cost - base_cost) / base_cost * 100 if base_cost else 0.0
        marker = "" if arm is control else f"{delta:+.1f}%"
        print(f"  {arm.name:<15} {arm.mean('total_tokens'):>10,.0f} "
              f"{arm.mean('cache_creation'):>12,.0f} {cost:>13.4f} {marker:>11}")

    print(f"\n  control spread (± stdev): ${control.stdev('cost_usd'):.4f}")
    spread = max((a.stdev("cost_usd") for a in arms if a.ok), default=0.0)

    print()
    winners = [a for a in arms[1:] if a.ok and a.mean("cost_usd") < base_cost]
    # The spread test has to cut both ways: declaring "maps cost more" on a
    # difference smaller than the noise is the same error as declaring a win.
    biggest_gap = max(
        (abs(a.mean("cost_usd") - base_cost) for a in arms[1:] if a.ok), default=0.0
    )
    if not control.ok:
        print("  VERDICT: the control produced no usable sessions.")
    elif biggest_gap < spread:
        print("  VERDICT: NO MEASURABLE DIFFERENCE — every arm lands within the")
        print(f"           run-to-run spread (${spread:.4f}). Neither 'maps help' nor")
        print("           'maps cost more' is supported by this data. More repeats")
        print("           are needed before quoting any percentage.")
    elif not winners:
        print("  VERDICT: no arm beat the control, and the gap exceeds the spread.")
        print("           Every map cost more than the exploration it replaced, at")
        print("           this repo size and session length.")
    else:
        best = min(winners, key=lambda a: a.mean("cost_usd"))
        gain = (base_cost - best.mean("cost_usd"))
        pct = gain / base_cost * 100
        if gain < spread:
            print(f"  VERDICT: '{best.name}' is cheapest (-{pct:.1f}%) but the gain")
            print("           is smaller than the run-to-run spread. Not conclusive:")
            print("           collect more repeats before quoting it.")
        else:
            print(f"  VERDICT: '{best.name}' wins, -{pct:.1f}% per session, and the")
            print(f"           effect (${gain:.4f}) exceeds the spread (${spread:.4f}).")

    errors = [r for arm in arms for r in arm.runs if r.error]
    if errors:
        print(f"\n  {len(errors)} run(s) failed; first: {errors[0].error[:160]}")
    print("\n" + METHOD)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=REPO)
    parser.add_argument(
        "--task-set", choices=("cslim", "flask"), default="cslim",
        help="Which question set to ask (must match the repo under test).",
    )
    parser.add_argument("--tasks", type=int, default=2, help="How many tasks to use.")
    parser.add_argument("--repeats", type=int, default=2, help="Repetitions per arm.")
    parser.add_argument(
        "--turns", type=int, default=1,
        help="Prompts per session. >1 resumes, so the map amortizes (the realistic case).",
    )
    parser.add_argument(
        "--hook-max-tokens", type=int, default=25_000,
        help="Token budget given to the hook; lower means a cheaper injection.",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the cost confirmation.")
    args = parser.parse_args()

    if not shutil.which("claude"):
        print("`claude` not found in PATH", file=sys.stderr)
        return 1

    pool = FLASK_TASKS if args.task_set == "flask" else DEFAULT_TASKS
    tasks = pool[: max(1, args.tasks)]
    sessions_per_arm = args.repeats if args.turns > 1 else args.repeats * len(tasks)
    total_calls = sessions_per_arm * len(ARMS) * (args.turns if args.turns > 1 else 1)

    print(f"About to make ~{total_calls} real Claude Code calls on your account.")
    print("At roughly $0.15-0.60 each, expect a few dollars.\n")
    if not args.yes:
        reply = input("Proceed? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            return 1

    settings = settings_path(InstallScope.PROJECT, args.project)
    backup = settings.read_text(encoding="utf-8") if settings.is_file() else None

    arms = {name: Arm(name) for name in ARMS}
    try:
        for repeat in range(args.repeats):
            # Interleave arms so prompt-cache warming doesn't favour one side.
            for name, spec in ARMS.items():
                set_arm(spec, args.project, args.hook_max_tokens)
                target = arms[name]
                if args.turns > 1:
                    runs = session(tasks, args.project, args.turns, name, repeat)
                    target.add(runs)
                    print(f"  [{name:<13}] repeat {repeat + 1}: "
                          f"${sum(r.cost_usd for r in runs):.4f}")
                else:
                    for index, prompt in enumerate(tasks):
                        run = record(name, index, repeat,
                                     run_claude(prompt, args.project))
                        target.add([run])
                        status = run.error[:40] if run.error else f"${run.cost_usd:.4f}"
                        print(f"  [{name:<13}] repeat {repeat + 1} "
                              f"task {index + 1}: {status}")
    finally:
        if backup is not None:
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(backup, encoding="utf-8")
        else:
            uninstall_hook(InstallScope.PROJECT, project_dir=args.project)
        print("\n(settings.json restored to its original state)")

    report_arms(list(arms.values()), args.turns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
