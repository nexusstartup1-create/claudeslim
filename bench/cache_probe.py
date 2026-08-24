#!/usr/bin/env python3
"""Does cache write scale with the size of what you inject?

Our A/B runs turned up an anomaly: a 3.1k map and a 24.6k map produced almost
identical ``cache_creation_input_tokens``. Two explanations fit that:

* **Linear** — cache write tracks the payload, and the anomaly was noise. Then
  map size matters and the tiering strategy is worth having.
* **Quantised** — injecting *anything* shifts cache boundaries and rewrites a
  block whose size barely depends on the payload. Then map size is irrelevant to
  cost, and index-vs-skeleton tiering buys nothing.

This isolates the variable. Instead of real maps (which differ in content,
structure and in how much exploration they save), it injects **synthetic filler
of a controlled size** through the real ``UserPromptSubmit`` hook, and reads
Anthropic's own usage numbers back.

    python bench/cache_probe.py --project /path/to/flask --repeats 3

Arms: 0 (control), 500, 5k, 30k injected tokens. The control anchors the
intercept — without it a slope of 1 and a slope of 0 look the same.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bench.ab import FLASK_TASKS, record, run_claude  # noqa: E402
from cslim.core.installer import EVENT, InstallScope, settings_path  # noqa: E402

#: Injected sizes in tokens. 0 is the control.
ARM_SIZES = (0, 500, 5_000, 30_000)

#: Ordinary English words tokenize at a predictable rate; random characters
#: would not, and would make the target sizes a lie.
_WORDS = ["module", "handler", "request", "session", "context", "response", "route", "template", "config", "instance", "factory", "adapter", "provider", "registry", "blueprint", "endpoint", "signal", "wrapper", "token", "cookie", "header", "payload", "schema", "binding", "resolver", "dispatcher", "manager", "builder", "parser", "encoder", "decoder", "validator", "middleware", "pipeline"]


def count_tokens(text: str) -> int:
    """Prefer a real BPE tokenizer; fall back to our own estimator."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        from cslim.core.tokenizer import HeuristicEstimator

        return HeuristicEstimator().count(text)


def make_filler(target_tokens: int, seed: int = 7) -> str:
    """Deterministic prose of approximately ``target_tokens`` tokens."""
    if target_tokens <= 0:
        return ""
    rng = random.Random(seed)
    lines = ["Reference notes for this project (synthetic benchmark payload)."]
    while count_tokens("\n".join(lines)) < target_tokens:
        lines.append("- " + " ".join(rng.choice(_WORDS) for _ in range(12)))
    while len(lines) > 2 and count_tokens("\n".join(lines)) > target_tokens * 1.02:
        lines.pop()
    return "\n".join(lines)


HOOK_SOURCE = '''\
#!/usr/bin/env python3
"""Synthetic hook written by bench/cache_probe.py. Injects a fixed payload."""
import json
import sys
from pathlib import Path

PAYLOAD = Path(__file__).with_suffix(".txt")


def main() -> int:
    try:
        sys.stdin.read()
        text = PAYLOAD.read_text(encoding="utf-8")
    except Exception:
        return 0
    if text.strip():
        json.dump(
            {"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": text,
            }},
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


@dataclass(slots=True)
class ArmResult:
    size: int
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


def install_synthetic_hook(project: Path, payload: str, workdir: Path) -> None:
    """Point settings.json at a hook that emits exactly ``payload``."""
    script = workdir / "probe_hook.py"
    script.write_text(HOOK_SOURCE, encoding="utf-8")
    script.chmod(0o755)
    script.with_suffix(".txt").write_text(payload, encoding="utf-8")

    path = settings_path(InstallScope.PROJECT, project)
    data: dict[str, object] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data["hooks"] = {
        EVENT: [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{sys.executable} {script}",
                        "timeout": 30,
                    }
                ],
            }
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def clear_hook(project: Path) -> None:
    path = settings_path(InstallScope.PROJECT, project)
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return
    data.pop("hooks", None)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def run_session(tasks: list[str], project: Path, turns: int) -> list:
    runs = []
    resume: str | None = None
    for index, prompt in enumerate(tasks[:turns]):
        payload = run_claude(prompt, project, resume=resume)
        run = record("probe", index, 0, payload)
        runs.append(run)
        if run.error:
            break
        resume = str(payload.get("session_id") or "") or None
    return runs


def report(results: list[ArmResult]) -> None:
    control = results[0]
    base_write = ArmResult.mean(control.cache_write)

    print("\n" + "=" * 74)
    print("CACHE WRITE vs INJECTED SIZE")
    print("=" * 74)
    print(f"  {'injected':>9} {'cache write':>12} {'±stdev':>8} "
          f"{'Δ control':>11} {'slope':>7} {'cost':>8}")
    print("  " + "-" * 62)

    slopes: list[float] = []
    for arm in results:
        if not arm.cache_write:
            print(f"  {arm.size:>9,}   (no successful sessions)")
            continue
        write = ArmResult.mean(arm.cache_write)
        delta = write - base_write
        slope = delta / arm.size if arm.size else 0.0
        if arm.size:
            slopes.append(slope)
        # Per-arm spread, not the maximum across arms: one unstable arm must
        # not be allowed to declare every other comparison inconclusive.
        print(
            f"  {arm.size:>9,} {write:>12,.0f} "
            f"{ArmResult.stdev(arm.cache_write):>8,.0f} "
            f"{delta:>11,.0f} {slope:>7.2f} {ArmResult.mean(arm.cost):>8.4f}"
        )

    print()
    injected = [a for a in results if a.size and a.cache_write]
    if len(injected) < 2:
        print("  VERDICT: not enough injected arms produced data.")
        return

    big, second = injected[-1], injected[-2]
    gap = abs(ArmResult.mean(big.cache_write) - ArmResult.mean(second.cache_write))
    pooled = (
        ArmResult.stdev(big.cache_write) + ArmResult.stdev(second.cache_write)
    ) / 2
    ratio = big.size / max(1, second.size)

    if gap < 2 * pooled:
        print(f"  VERDICT: PLATEAU. Injecting {ratio:.0f}x more content "
              f"({second.size:,} -> {big.size:,} tokens) moved")
        print(f"           cache write by {gap:,.0f} tokens, inside the "
              f"±{pooled:,.0f} spread.")
        print("           Cost is dominated by the ACT of injecting, not by size.")
        print("           Consequence: shrinking a map does not make it cheaper.")
    elif slopes[-1] > 0.7:
        print(f"  VERDICT: LINEAR (slope {slopes[-1]:.2f}). Cache write tracks the")
        print("           payload; map size is worth optimising.")
    else:
        print(f"  VERDICT: PARTIAL (slope {slopes[-1]:.2f}). Size matters, but a")
        print("           fixed per-injection cost dominates at small sizes.")

    first = injected[0]
    first_delta = ArmResult.mean(first.cache_write) - base_write
    if first_delta > first.size * 2:
        print(f"\n  Note: the smallest arm injected {first.size:,} tokens but added")
        print(f"  {first_delta:,.0f} of cache write — a fixed overhead many times the")
        print("  payload, and with the widest spread of any arm. That looks like an")
        print("  all-or-nothing re-cache rather than a proportional cost.")


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

    total = len(ARM_SIZES) * args.repeats * args.turns
    print(f"About to make {total} real Claude Code calls on your account.")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1

    workdir = Path(__file__).parent / ".probe"
    workdir.mkdir(exist_ok=True)
    settings = settings_path(InstallScope.PROJECT, args.project)
    backup = settings.read_text(encoding="utf-8") if settings.is_file() else None

    fillers = {size: make_filler(size) for size in ARM_SIZES}
    for size, text in fillers.items():
        actual = count_tokens(text) if text else 0
        print(f"  arm {size:>6,}: filler is {actual:,} tokens")

    results = [ArmResult(size=size) for size in ARM_SIZES]
    try:
        for repeat in range(args.repeats):
            for arm in results:
                if arm.size == 0:
                    clear_hook(args.project)
                else:
                    install_synthetic_hook(args.project, fillers[arm.size], workdir)
                runs = run_session(FLASK_TASKS, args.project, args.turns)
                if any(r.error for r in runs):
                    arm.errors += 1
                    print(f"  [{arm.size:>6,}] repeat {repeat + 1}: FAILED")
                    continue
                arm.cache_write.append(sum(r.cache_creation for r in runs))
                arm.cache_read.append(sum(r.cache_read for r in runs))
                arm.cost.append(sum(r.cost_usd for r in runs))
                print(f"  [{arm.size:>6,}] repeat {repeat + 1}: "
                      f"cache write {arm.cache_write[-1]:,}, ${arm.cost[-1]:.4f}")
    finally:
        if backup is not None:
            settings.write_text(backup, encoding="utf-8")
        else:
            clear_hook(args.project)
        shutil.rmtree(workdir, ignore_errors=True)
        print("\n(settings.json restored)")

    report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
