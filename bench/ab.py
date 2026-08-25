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
import random
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cslim.core.delivery import remove_map, write_map  # noqa: E402
from cslim.core.hook import HookConfig, build_map  # noqa: E402
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
def generate_tasks(project: Path, count: int = 4, seed: int = 7) -> list[str]:
    """Build lookup questions from the repository under test.

    Hand-written task sets are why this harness only ran against two repos. The
    questions here come from cslim's own reference ranking: the most-referenced
    files, and a distinctive symbol defined in each. They are answerable by
    reading the code and hard to answer without finding it first, which is the
    exploration a map claims to replace.

    Deterministic for a given repo and seed, so repeats and arms compare.
    """
    from cslim.core import SymbolKind, compress_paths

    # Only things a file *defines*. Imports name symbols that live elsewhere, so
    # asking "which file defines rich.box" has no answer in this repository.
    definitions = {
        SymbolKind.FUNCTION,
        SymbolKind.CLASS,
        SymbolKind.METHOD,
        SymbolKind.INTERFACE,
        SymbolKind.STRUCT,
        SymbolKind.TYPE,
    }
    # Fixtures and vendored corpora are in the tree but are not what anyone
    # asks a codebase about.
    skip = ("fixtures/", "vendor/", "third_party/", "/testdata/", "node_modules/")

    bundle = compress_paths([project])
    candidates = [
        f
        for f in bundle.files
        if not any(part in f.rel_path for part in skip)
        and not Path(f.rel_path).name.startswith("test_")
    ]
    ranked = sorted(candidates, key=lambda f: -f.rank)[: count * 4]
    if not ranked:
        raise SystemExit(f"no source files under {project}: nothing to ask about")

    rng = random.Random(seed)
    tasks: list[str] = []
    seen: set[str] = set()
    for file in ranked:
        named = [
            sym
            for sym in file.symbols
            if sym.kind in definitions
            and len(sym.name) > 3
            and not sym.name.startswith("_")
            and sym.name not in seen
        ]
        if not named:
            continue
        symbol = rng.choice(named)
        seen.add(symbol.name)
        tasks.append(
            f"Which file defines `{symbol.name}`, and what is it for? "
            "Answer with the path and one sentence only."
        )
        if len(tasks) >= count:
            break

    while len(tasks) < count and ranked:
        file = ranked[len(tasks) % len(ranked)]
        tasks.append(
            f"What is `{file.rel_path}` responsible for, and which other module "
            "uses it most? Answer in one sentence."
        )
    return tasks[:count]


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

    def median(self, attr: str) -> float:
        values = self.totals(attr)
        return statistics.median(values) if values else 0.0

    def robust_spread(self, attr: str) -> float:
        """Median absolute deviation, scaled to compare with a stdev.

        The standard deviation is what made two runs inconclusive: one session
        four times its siblings inflates it enough to swallow every effect. MAD
        ignores that session instead of being dominated by it, and 1.4826 is the
        factor that makes it estimate the same quantity for normal data — so the
        two are directly comparable rather than a different scale.
        """
        values = self.totals(attr)
        if len(values) < 2:
            return 0.0
        med = statistics.median(values)
        return 1.4826 * statistics.median([abs(v - med) for v in values])

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
    "no map": None,
    "hook · full AST": {"delivery": "hook", "index_only": False},
    "hook · index": {"delivery": "hook", "index_only": True},
    "CLAUDE.md · full AST": {"delivery": "claude-md", "index_only": False},
    "CLAUDE.md · index": {"delivery": "claude-md", "index_only": True},
    "CLAUDE.md · outline": {"delivery": "claude-md", "outline_only": True},
}


def set_arm(spec: dict[str, object] | None, project: Path, max_tokens: int) -> None:
    """Put the repository into exactly one delivery state.

    Every arm tears down *both* delivery mechanisms before building its own.
    Leaving the previous arm's CLAUDE.md section or hook in place would make
    each arm measure the sum of itself and its predecessor.
    """
    uninstall_hook(InstallScope.PROJECT, project_dir=project)
    remove_map(project)

    if spec is None:
        return

    options = dict(spec)
    delivery = str(options.pop("delivery", "hook"))

    if delivery == "claude-md" and "file" in options:
        # An externally produced map — repomix, code2prompt, anything that
        # writes a file. Delivered through the same mechanism as ours, so the
        # comparison is about what the map contains, not how it arrives.
        payload = Path(str(options["file"])).read_text(encoding="utf-8", errors="replace")
        result = write_map(payload, project_dir=project)
        if result.action == "absent":
            raise SystemExit("; ".join(result.warnings) or "could not write CLAUDE.md")
        return

    if delivery == "claude-md":
        config = HookConfig(
            max_tokens=max_tokens,
            min_files=0,
            index_only=bool(options.get("index_only", False)),
            outline_only=bool(options.get("outline_only", False)),
        )
        payload, tokens, files, _cached, _tier = build_map(config, project)
        if not payload:
            raise SystemExit(f"no source files under {project}: nothing to map")
        result = write_map(payload, project_dir=project, tokens=tokens, files=files)
        if result.action == "absent":
            raise SystemExit("; ".join(result.warnings) or "could not write CLAUDE.md")
        return

    install_hook(
        InstallScope.PROJECT,
        command=hook_command(max_tokens=max_tokens, **options),  # type: ignore[arg-type]
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


def write_result_file(path: Path, arms: list[Arm], args: Any, tasks: list[str]) -> None:
    """A run, in a form someone else's run can be compared against.

    The point of bench/RESULTS.md is accumulating runs from repositories we do
    not have, including ones that contradict us. That only works if a run is a
    file rather than a screenshot of a terminal.
    """
    control = arms[0]
    base = control.mean("cost_usd")
    # Mirror report_arms exactly: the run-to-run spread is the widest arm, not
    # the control's alone. If the JSON and the terminal disagreed about what
    # counts as noise, RESULTS.md would accumulate claims the harness refused
    # to print.
    control_spread = control.stdev("cost_usd")
    spread = max((a.stdev("cost_usd") for a in arms if a.ok), default=0.0)

    payload = {
        "schema": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cslim_version": _cslim_version(),
        "project": {
            "path": str(args.project),
            "name": Path(args.project).name,
            "commit": _git_head(Path(args.project)),
        },
        "design": {
            "turns": args.turns,
            "repeats": args.repeats,
            "task_set": args.task_set,
            "tasks": tasks,
            "hook_max_tokens": args.hook_max_tokens,
        },
        "control_spread_usd": round(control_spread, 6),
        "run_to_run_spread_usd": round(spread, 6),
        "arms": [
            {
                "name": arm.name,
                "sessions": len(arm.sessions),
                "cost_usd_mean": round(arm.mean("cost_usd"), 6),
                "cost_usd_per_session": [
                    round(sum(r.cost_usd for r in s), 6) for s in arm.sessions
                ],
                "cache_creation_mean": round(arm.mean("cache_creation"), 1),
                "total_tokens_mean": round(arm.mean("input_tokens"), 1),
                "vs_control_pct": (
                    None
                    if not base
                    else round((arm.mean("cost_usd") - base) / base * 100, 2)
                ),
                # The only field that matters when quoting: an effect smaller
                # than the spread is not an effect.
                "exceeds_spread": abs(arm.mean("cost_usd") - base) > spread,
            }
            for arm in arms
        ],
    }
    payload["verdict"] = (
        "measurable"
        if any(a["exceeds_spread"] for a in payload["arms"][1:])  # type: ignore[index]
        else "no measurable difference"
    )
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _cslim_version() -> str:
    try:
        from importlib.metadata import version

        return version("cslim")
    except Exception:
        return "unknown"


def _git_head(project: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        return proc.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


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

    # Both estimators, always, never one swapped for the other.
    #
    # Two runs came out inconclusive on the stdev rule because a single session
    # costing four times its siblings inflates it enough to swallow every
    # effect. The median absolute deviation ignores that session rather than
    # being dominated by it. On demonstrably bimodal data it is the right
    # estimator — but it was adopted after seeing data it would flip, so this
    # prints both and lets the reader see that the verdict depends on which one
    # you trust. When they disagree, that disagreement is the finding.
    robust = max((a.robust_spread("cost_usd") for a in arms if a.ok), default=0.0)
    base_median = control.median("cost_usd")
    print(f"  control spread (± MAD):   ${control.robust_spread('cost_usd'):.4f}")
    if robust and abs(robust - spread) / max(robust, spread) > 0.25:
        print("\n  ROBUST VIEW — medians, and spread as scaled MAD:")
        print(f"    run-to-run spread: ${robust:.4f}  (stdev said ${spread:.4f})")
        for a in arms[1:]:
            if not a.ok:
                continue
            gap = a.median("cost_usd") - base_median
            mark = "exceeds" if abs(gap) > robust else "within"
            pct = (gap / base_median * 100) if base_median else 0.0
            print(f"    {a.name:22} median ${a.median('cost_usd'):.4f} "
                  f"({pct:+.1f}%)  {mark} the spread")
        print("    The two views disagree because one arm is bimodal, not noisy.")
        print("    Treat neither as a significance test: see bench/RESULTS.md.")

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
        "--task-set", choices=("auto", "cslim", "flask"), default="auto",
        help="auto derives questions from the repo under test, so this runs "
             "anywhere. cslim/flask are the fixed sets used for the published runs.",
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
    parser.add_argument(
        "--arms",
        default="",
        help="Comma-separated arm names to run (default: all). --list-arms shows them.",
    )
    parser.add_argument("--list-arms", action="store_true", help="Print arm names and exit.")
    parser.add_argument(
        "--compare-map",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Add an arm carrying a map produced by another tool, delivered "
             "through CLAUDE.md like ours. Repeatable. Example: "
             "--compare-map 'repomix=/tmp/repomix.md'",
    )
    parser.add_argument(
        "--json", dest="json_out", type=Path, default=None,
        help="Write a machine-readable result file (for bench/RESULTS.md).",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the cost confirmation.")
    args = parser.parse_args()

    if not shutil.which("claude"):
        print("`claude` not found in PATH", file=sys.stderr)
        return 1

    if args.task_set == "auto":
        pool = generate_tasks(args.project, max(args.tasks, 1))
        print(f"Generated {len(pool)} task(s) from {args.project.name}:")
        for task in pool:
            print(f"  - {task[:96]}")
        print()
    else:
        pool = FLASK_TASKS if args.task_set == "flask" else DEFAULT_TASKS
    tasks = pool[: max(1, args.tasks)]
    sessions_per_arm = args.repeats if args.turns > 1 else args.repeats * len(tasks)
    if args.list_arms:
        for name in ARMS:
            print(name)
        return 0

    selected = dict(ARMS)
    if args.arms:
        wanted = [a.strip() for a in args.arms.split(",") if a.strip()]
        unknown = [a for a in wanted if a not in ARMS]
        if unknown:
            print(f"unknown arm(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"available: {', '.join(ARMS)}", file=sys.stderr)
            return 2
        selected = {name: ARMS[name] for name in wanted}

    for spec in args.compare_map:
        if "=" not in spec:
            print(f"--compare-map wants NAME=PATH, got {spec!r}", file=sys.stderr)
            return 2
        name, _, path = spec.partition("=")
        if not Path(path).is_file():
            print(f"--compare-map: no such file: {path}", file=sys.stderr)
            return 2
        selected[f"CLAUDE.md · {name.strip()}"] = {
            "delivery": "claude-md",
            "file": path.strip(),
        }

    total_calls = sessions_per_arm * len(selected) * (args.turns if args.turns > 1 else 1)

    print(f"About to make ~{total_calls} real Claude Code calls on your account.")
    print("At roughly $0.15-0.60 each, expect a few dollars.\n")
    if not args.yes:
        reply = input("Proceed? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            return 1

    settings = settings_path(InstallScope.PROJECT, args.project)
    backup = settings.read_text(encoding="utf-8") if settings.is_file() else None

    arms = {name: Arm(name) for name in selected}
    try:
        for repeat in range(args.repeats):
            # Interleave arms so prompt-cache warming doesn't favour one side.
            for name, spec in selected.items():
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

    if args.json_out:
        write_result_file(args.json_out, list(arms.values()), args, pool)
        print(f"\nWrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
