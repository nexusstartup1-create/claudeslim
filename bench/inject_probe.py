#!/usr/bin/env python3
"""Does the *injection point* change what a map costs?

`bench/cache_probe.py` established that cost is dominated by the act of
injecting, not by payload size: 5k and 30k tokens through the hook cost the
same. The natural reading is that `additionalContext` invalidates a cache
boundary and forces a block rewrite whose price barely depends on the payload.

If that's right, the way out isn't a smaller map — it's a different delivery
mechanism. Content placed in the **stable prefix** Claude Code already caches
(`CLAUDE.md`, loaded at session start) should extend a block that is being
written anyway, rather than invalidating one.

This holds the payload identical across arms and varies only how it arrives:

* **control**      — nothing
* **hook**         — `additionalContext` from a UserPromptSubmit hook
* **CLAUDE.md**    — the same text in the project's CLAUDE.md

    python bench/inject_probe.py --project /path/to/flask --repeats 3

Before measuring, it checks that each arm's payload actually reaches the model.
An arm the model never sees would measure nothing and look like a win.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bench.ab import FLASK_TASKS, record, run_claude  # noqa: E402
from bench.cache_probe import (  # noqa: E402
    clear_hook,
    count_tokens,
    install_synthetic_hook,
    make_filler,
)
from cslim.core.installer import InstallScope, settings_path  # noqa: E402

#: Payload size used by every injected arm. 5k is where the hook already showed
#: its full fixed overhead in cache_probe.py.
PAYLOAD_TOKENS = 5_000

#: A fact that exists nowhere else, used to prove the payload was delivered.
SENTINEL = "The internal build codename for this project is ORANGE-MERIDIAN-77."

ARMS = ("control", "hook", "CLAUDE.md")


@dataclass(slots=True)
class Arm:
    name: str
    cache_write: list[int] = field(default_factory=list)
    cache_read: list[int] = field(default_factory=list)
    cost: list[float] = field(default_factory=list)
    errors: int = 0

    @staticmethod
    def mean(values: list[float] | list[int]) -> float:
        return statistics.fmean(values) if values else 0.0

    @staticmethod
    def stdev(values: list[float] | list[int]) -> float:
        return statistics.stdev(values) if len(values) > 1 else 0.0


def claude_md(project: Path) -> Path:
    return project / "CLAUDE.md"


def apply_arm(name: str, project: Path, payload: str, workdir: Path) -> None:
    """Put the payload in place for one arm, and remove the other mechanisms."""
    clear_hook(project)
    claude_md(project).unlink(missing_ok=True)

    if name == "hook":
        install_synthetic_hook(project, payload, workdir)
    elif name == "CLAUDE.md":
        claude_md(project).write_text(payload, encoding="utf-8")


def verify_delivery(name: str, project: Path, payload: str, workdir: Path) -> bool:
    """Ask for the sentinel. If the model can't see it, this arm is invalid."""
    apply_arm(name, project, payload, workdir)
    result = run_claude(
        "What is the internal build codename for this project? "
        "Reply with the codename only, or NONE if you do not know it.",
        project,
    )
    if "_error" in result:
        print(f"    {name}: verification call failed")
        return False
    text = json.dumps(result)
    seen = "ORANGE-MERIDIAN-77" in text
    if name == "control":
        # The control must NOT see it — otherwise the payload is leaking in
        # from somewhere and every comparison is contaminated.
        print(f"    control: sentinel visible = {seen} (expected False)")
        return not seen
    print(f"    {name}: sentinel visible = {seen} (expected True)")
    return seen


def run_session(project: Path, turns: int) -> list:
    runs = []
    resume: str | None = None
    for index, prompt in enumerate(FLASK_TASKS[:turns]):
        payload = run_claude(prompt, project, resume=resume)
        run = record("probe", index, 0, payload)
        runs.append(run)
        if run.error:
            break
        resume = str(payload.get("session_id") or "") or None
    return runs


def report(arms: list[Arm], payload_tokens: int) -> None:
    control = arms[0]
    base_write = Arm.mean(control.cache_write)
    base_cost = Arm.mean(control.cost)

    print("\n" + "=" * 76)
    print(f"INJECTION POINT — same {payload_tokens:,}-token payload, different delivery")
    print("=" * 76)
    print(f"  {'arm':<12} {'cache write':>12} {'±stdev':>8} {'Δ write':>10} "
          f"{'cost':>9} {'vs control':>11}")
    print("  " + "-" * 68)

    for arm in arms:
        if not arm.cache_write:
            print(f"  {arm.name:<12}   (no successful sessions)")
            continue
        write = Arm.mean(arm.cache_write)
        cost = Arm.mean(arm.cost)
        delta_cost = (cost - base_cost) / base_cost * 100 if base_cost else 0.0
        marker = "" if arm is control else f"{delta_cost:+.1f}%"
        print(f"  {arm.name:<12} {write:>12,.0f} {Arm.stdev(arm.cache_write):>8,.0f} "
              f"{write - base_write:>10,.0f} {cost:>9.4f} {marker:>11}")

    # Means hide the thing that matters here. A mechanism that lands in the
    # cached prefix gets cheaper on later sessions; one that is re-injected
    # every time does not. That shows up as a trend, and averaging destroys it.
    print("\n  cost by repeat (does it get cheaper as the cache warms?)")
    for arm in arms:
        if not arm.cost:
            continue
        series = "  ".join(f"{c:.4f}" for c in arm.cost)
        trend = ""
        if len(arm.cost) >= 3:
            first, last = arm.cost[0], min(arm.cost[1:])
            if last < first * 0.6:
                trend = "  <- collapses: cache hit"
            elif max(arm.cost) - min(arm.cost) < 0.05:
                trend = "  <- flat: no reuse"
        print(f"  {arm.name:<12} {series}{trend}")

    print()
    injected = [a for a in arms[1:] if a.cache_write]
    if len(injected) < 2:
        print("  VERDICT: not enough arms produced data.")
        return

    best = min(injected, key=lambda a: Arm.mean(a.cost))
    worst = max(injected, key=lambda a: Arm.mean(a.cost))
    gap = Arm.mean(worst.cost) - Arm.mean(best.cost)
    pooled = (Arm.stdev(best.cost) + Arm.stdev(worst.cost)) / 2

    if gap < 2 * pooled:
        print("  VERDICT: NO DIFFERENCE BETWEEN INJECTION POINTS. Delivering the")
        print(f"           same payload via {best.name} and {worst.name} costs the")
        print(f"           same (gap ${gap:.4f}, spread ±${pooled:.4f}).")
        print("           The overhead is inherent to adding context, not to how.")
    else:
        saving = (Arm.mean(worst.cost) - Arm.mean(best.cost)) / Arm.mean(worst.cost)
        print(f"  VERDICT: '{best.name}' is cheaper than '{worst.name}' by "
              f"{saving * 100:.1f}%.")
        if Arm.mean(best.cost) < base_cost:
            print("           And it beats the no-map control — the injection point")
            print("           was the problem, and this one solves it.")
        else:
            over = (Arm.mean(best.cost) - base_cost) / base_cost * 100
            print(f"           But it still costs {over:+.1f}% vs no map at all.")
            print("           Cheaper delivery, still not free.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--turns", type=int, default=2)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if not shutil.which("claude"):
        print("`claude` not found in PATH", file=sys.stderr)
        return 1

    payload = SENTINEL + "\n\n" + make_filler(PAYLOAD_TOKENS)
    print(f"payload: {count_tokens(payload):,} tokens (identical in every arm)")

    total = len(ARMS) * args.repeats * args.turns + len(ARMS)
    print(f"About to make ~{total} real Claude Code calls on your account.\n")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1

    workdir = Path(__file__).parent / ".inject"
    workdir.mkdir(exist_ok=True)
    settings = settings_path(InstallScope.PROJECT, args.project)
    settings_backup = settings.read_text(encoding="utf-8") if settings.is_file() else None
    md = claude_md(args.project)
    md_backup = md.read_text(encoding="utf-8") if md.is_file() else None

    arms = [Arm(name) for name in ARMS]
    try:
        print("\nverifying each arm actually reaches the model:")
        valid = {name: verify_delivery(name, args.project, payload, workdir)
                 for name in ARMS}
        if not all(valid.values()):
            bad = [n for n, ok in valid.items() if not ok]
            print(f"\n  ABORT: {', '.join(bad)} did not deliver as expected.")
            print("  Measuring them would compare mechanisms that aren't equivalent.")
            return 1

        print("\nmeasuring:")
        for repeat in range(args.repeats):
            for arm in arms:
                apply_arm(arm.name, args.project, payload, workdir)
                runs = run_session(args.project, args.turns)
                if any(r.error for r in runs):
                    arm.errors += 1
                    print(f"  [{arm.name:<10}] repeat {repeat + 1}: FAILED")
                    continue
                arm.cache_write.append(sum(r.cache_creation for r in runs))
                arm.cache_read.append(sum(r.cache_read for r in runs))
                arm.cost.append(sum(r.cost_usd for r in runs))
                print(f"  [{arm.name:<10}] repeat {repeat + 1}: "
                      f"cache write {arm.cache_write[-1]:,}, ${arm.cost[-1]:.4f}")
    finally:
        clear_hook(args.project)
        if settings_backup is not None:
            settings.write_text(settings_backup, encoding="utf-8")
        md.unlink(missing_ok=True)
        if md_backup is not None:
            md.write_text(md_backup, encoding="utf-8")
        shutil.rmtree(workdir, ignore_errors=True)
        print("\n(settings.json and CLAUDE.md restored)")

    report(arms, count_tokens(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
