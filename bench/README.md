# Benchmarks — does the map actually pay for itself?

`bench/ab.py` runs the same tasks through real Claude Code sessions with and
without the cslim hook, and reads Anthropic's own `total_cost_usd` back out of
`claude -p --output-format json`. Nothing here is estimated.

```bash
python bench/ab.py --tasks 3 --turns 3 --repeats 3
python bench/ab.py --project /path/to/flask --task-set flask --turns 2 --repeats 3
```

---

## The economics that decide everything

Anthropic bills a **cache write at ~1.25×** base input and a **cache read at
~0.1×**. So:

> **One token of freshly injected context costs about what twelve cache-read
> tokens cost.**

Claude Code's exploration is almost entirely cache reads — re-reading a file it
already pulled is nearly free. Injecting a map is not. A map therefore only pays
for itself when it is **more than 12× smaller** than the exploration it prevents.

That single ratio explains every result below.

---

## Results

### Small repo — cslim itself (18 files, 43k tokens)

3 sessions per arm, 3 turns each.

| map size | total tokens | cost / session |
| --- | --- | --- |
| none (control) | — | $0.3187 |
| 10.5k skeleton | −13.2% | **+33.2%** |
| 2.8k tiered | −2.2% | **+21.2%** |

Both effects exceeded the run-to-run spread, so these are real. **Token count
went down; cost went up.** The map was nowhere near 12× cheaper than the
exploration it replaced.

### Large repo — pallets/flask (80 files, 144k tokens)

3 sessions per arm, 2 turns each.

| arm | total tokens | cache write | cost / session | vs control |
| --- | --- | --- | --- | --- |
| no hook | 165,172 | 15,004 | $0.2515 | — |
| full AST map (24.6k) | 161,830 | 20,804 | $0.3038 | +20.8% |
| index only (3.1k) | 170,584 | 19,219 | $0.2940 | +16.9% |

**Control spread: ±$0.0967 — larger than either difference.**

So the honest verdict on Flask is **no measurable difference**, not "maps cost
more". With three sessions per arm the noise swamps a ~$0.05 effect. Anyone
quoting −17% or +21% from this table is over-reading it.

One detail worth noting: the full map is 24.6k tokens, yet cache write rose only
5.8k over the control. The likely reading is that the map *did* displace file
reads that would themselves have been cached — the map is doing its job, it just
isn't yet cheap enough to come out ahead.

### Long sessions — pallets/flask, 12 turns

The amortization hypothesis: the map is written to cache once, so a long session
should spread that cost across many turns and come out ahead.

3 sessions per arm, **12 turns each**.

| arm | total tokens | cache write | cost / session | vs control |
| --- | --- | --- | --- | --- |
| no hook | 1,065,226 | 18,518 | $0.7814 | — |
| full AST map | 1,055,139 | 37,966 | $0.9729 | +24.5% |
| index only | 1,013,816 | 37,574 | $0.9213 | +17.9% |

Control spread: ±$0.0820. Both gaps ($0.19 and $0.14) **exceed** it, so unlike
the 2-turn run this is conclusive.

**The hypothesis is falsified.** Twelve turns did not rescue the map. The
penalty at 12 turns (+17.9% / +24.5%) is essentially the same as at 2 turns
(+16.9% / +20.8%) — amortization did not materialise.

Why the cost doesn't fall with session length: a per-turn cost probe showed the
marginal turn costs a flat ~$0.055 after the first. There is no growing
exploration cost for the map to displace; it is roughly constant, and the map's
own cost is constant too. The ratio doesn't move.

**One thing remains unexplained.** The index map is 3.1k tokens and the full AST
map 24.6k, yet both arms show almost identical cache write (37.6k vs 38.0k,
against a control of 18.5k). If the injection were the only difference, the two
should differ by ~21k. Either cache writes are quantised into blocks much larger
than the payload, or injected context shifts cache boundaries in a way that
costs the same regardless of size. We have not instrumented this, and it means
**the cost of a map may be dominated by the act of injecting rather than by its
size** — which would make shrinking the map a dead end.

### The injection point — `bench/inject_probe.py`

If cost is dominated by the act of injecting rather than by size, the way out
isn't a smaller map: it's a different delivery mechanism. This holds the payload
identical (5,026 synthetic tokens) and varies only how it arrives.

6 sessions per arm, 1 turn each, on Flask. Every arm was verified to actually
reach the model first, using a sentinel fact.

| arm | cache write | Δ vs control | cost / session | vs control |
| --- | --- | --- | --- | --- |
| control (no payload) | 2,576 | — | $0.0843 | — |
| `additionalContext` (hook) | 16,835 | **+14,258** | $0.2328 | **+176%** |
| `CLAUDE.md` | 1,578 | **−999** | $0.1147 | **+36%** |

**The injection point matters enormously.** The same payload costs **+176%**
through the hook and **+36%** through `CLAUDE.md`. The gap between the two is
6.9× the run-to-run spread, so this is not noise.

Why: `additionalContext` is inserted per session and invalidates a cache
boundary, forcing a ~14k block rewrite regardless of payload size — exactly the
plateau the size probe found. `CLAUDE.md` is part of the stable prefix Claude
Code already caches, so it adds **no measurable cache write at all**. Its
remaining +36% is the honest price of the content itself being cache-read each
turn at 0.1×.

**This overturns the earlier conclusion.** The overhead measured in every A/B run
above is not inherent to giving Claude a map — it is specific to the mechanism
cslim currently uses. A map delivered through `CLAUDE.md` costs roughly a fifth
of what the hook costs.

It is still not free: +36% against no map at all. And it moves cslim's output
into the user's repository, where it would be committed to git — a different
security posture from the hook, which writes nothing.

---

## Recommendations

| Situation | Use | Why |
| --- | --- | --- |
| You want to **spend less money** | **No map** | Every map configuration lost, in every condition tested. |
| Reviewing a diff | `cslim diff` | Removes noise without adding anything. Strictly a win. |
| Debugging test output | `cslim clean` | Same: pure subtraction, no injection. |
| Your repo **doesn't fit** in the context window | `--index-only` | 3.1k for all of Flask, 458 tokens for cslim. Buys coverage; costs money. |

**`cslim diff` and `cslim clean` have none of this problem.** They only remove
tokens; they never inject. Those two are unambiguously worth using.

### On automatic mode

The hook is a measured net cost in all four conditions tested:

| repo | turns | best map arm |
| --- | --- | --- |
| cslim (18 files) | 3 | +21.2% |
| flask (80 files) | 2 | +16.9% *(within noise)* |
| flask (80 files) | 12 | +17.9% *(conclusive)* |

Treat `cslim install` as a **context-window** tool, not a cost-saving one. It
buys the model a complete picture of a repository it could not otherwise hold —
and you pay roughly 20% more per session for that.

The tier threshold shipped by `cslim install` (`--index-threshold`, 30 files)
is **not supported by this data**: index and full AST land within a few points
of each other in every run. It remains a hypothesis.

## What has not been measured

Honest gaps, in the order they'd change the conclusions:

1. **Whether `CLAUDE.md` delivery beats the control on a repo big enough to
   matter.** It costs +36% here, against a Flask that Claude can already explore
   cheaply. On a repo where exploration is genuinely expensive, +36% of a small
   number may well be less than the exploration it prevents. This is now the
   most promising open question, and cslim does not yet implement it.
2. **Repos too large to explore at all.** Flask fits comfortably in context. A
   1000-file monorepo, where Claude cannot brute-force its way around, is the
   one case where a map might be the only way to get a useful answer. Cost
   would likely still be higher; the alternative might be no answer.
3. **Editing tasks.** All questions here are read-only lookups with short
   answers. Tasks that modify several files exercise the map differently.
4. **More repeats.** Three sessions per arm is thin, though the 12-turn effect
   cleared the spread comfortably.

## Reading the output

The harness refuses to over-claim. It compares the effect against the
run-to-run spread and prints `NO MEASURABLE DIFFERENCE` whenever the noise is
larger — in either direction. If you see a percentage without that warning, the
effect survived the check; if you see the warning, the percentage is not a
result.

Sessions, not turns, are the unit of analysis: inside a session the first turn
builds the cache and costs far more than the rest, so treating turns as
independent samples reports a spread that is an artefact of turn order.
