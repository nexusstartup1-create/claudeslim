# Results

Runs of `bench/ab.py`, ours and other people's. **Runs that contradict the
conclusions in [README.md](README.md) are the most valuable entries here** and
are published unchanged.

Flask is 80 files that Claude can already explore cheaply. The case none of
these runs covers is a repository large enough that exploration is genuinely
expensive — which is where a map has its best chance and where we cannot get
data on our own.

## How to add a run

```bash
python bench/ab.py --project /path/to/your/repo --turns 12 --repeats 5 \
                   --json my-run.json
```

`--task-set auto` (the default) derives the questions from your repository, so
this needs no per-repo setup. Open a PR adding your `.json` and a row below.
Please include the run whatever it says.

Two things make an entry usable:

* **Repeats.** Three per arm is too few, and six was not enough either — the
  6-repeat run below still came out inconclusive, because one arm turned out to
  be bimodal rather than noisy. Budget for ten if the answer matters to you.
* **The verdict, not the mean.** If `verdict` is `no measurable difference`,
  the percentages in that run are not results. Say so in the row.

## Reading a row

`vs control` is only meaningful when `exceeds spread` is true. The harness
computes the run-to-run spread as the widest arm's standard deviation and
refuses to call anything smaller an effect, in either direction.

---

## Our runs

### The OUTLINE tier — flask · 12 turns · 8 sessions/arm

`cslim` 0.1.0 · 2026-08-26 · [run4.json](run4.json)

Does the middle tier's extra content show up in the bill?

| arm | map tokens | total tokens | cache write | median $/session | vs control | p (rank) |
| --- | --- | --- | --- | --- | --- | --- |
| no map | — | 164,035 | 3,989 | $0.1493 | — | — |
| CLAUDE.md · index | 3,323 | 178,896 | 8,254 | $0.1522 | +1.9% | 0.505 |
| CLAUDE.md · outline | 6,580 | 159,492 | 5,989 | $0.1287 | −13.8% | 0.279 |

**Verdict: no measurable difference.** Both tiers sit inside the spread and
neither rank test comes close to significance. The −13.8% is not a result.

What the run does support: **the outline's extra 3,257 tokens do not show up as
cost.** There are now two tiers a session cannot distinguish from sending
nothing, and the more informative one is not the dearer one.

Per-session costs:

| arm | sessions |
| --- | --- |
| no map | 0.113 0.132 0.142 0.147 0.151 0.151 0.169 0.172 |
| CLAUDE.md · index | 0.132 0.137 0.143 0.146 0.159 0.174 **0.320 0.338** |
| CLAUDE.md · outline | 0.093 0.122 0.124 0.128 0.130 0.134 0.169 **0.363** |

Two unexplained things, recorded rather than smoothed over:

**The outline arm carried fewer total tokens than the control** (159,492 vs
164,035) — the only CLAUDE.md arm in this file to displace exploration rather
than add to it. One run, so it is an observation, not a finding.

**The smaller map wrote more cache.** Index is 3,323 tokens and wrote 8,254;
outline is 6,580 and wrote 5,989. That is backwards for any size-driven story,
and consistent with `cache_probe`'s conclusion that injection cost is not
about payload size. We still cannot say what it *is* about.

Run cost: 24 sessions, ~$4.

### cslim vs repomix — flask · 12 turns · 8 sessions/arm

`cslim` 0.1.0 · repomix 1.18.0 · 2026-08-26 · [run3.json](run3.json)

The first cost A/B in this category against another tool. Both maps are
delivered the same way — spliced into `CLAUDE.md` — so the only variable is
what the map contains, not how it arrives.

Same scope for both: flask's `**/*.py`.

| map | tokens | per file | cost/session (median) | vs control | p (rank) |
| --- | --- | --- | --- | --- | --- |
| none | — | — | $0.1575 | — | — |
| cslim `--index-only` | 3,093 | 39 | $0.1672 | +6.2% | 0.798 |
| cslim full AST | 33,895 | 424 | $0.1682 | +6.8% | **0.028** |
| repomix `--compress` | 86,880 | 1,034 | $0.3442 | **+118.5%** | **0.0002** |

Exact two-tailed Mann-Whitney against the control, n=8 per arm. Per-session
costs, sorted:

| arm | sessions |
| --- | --- |
| no map | 0.124 0.134 0.143 0.156 0.159 0.161 0.167 0.184 |
| cslim index | 0.099 0.119 0.138 0.159 0.175 0.177 0.178 0.303 |
| cslim full AST | 0.162 0.165 0.166 0.167 0.169 0.175 0.177 0.207 |
| repomix | 0.261 0.262 0.339 0.342 0.346 0.368 0.375 1.655 |

**repomix's cheapest session cost more than the control's dearest.** The two
distributions do not overlap at all, which is what p=0.0002 is describing.

Two findings, and they point in different directions:

**cslim's index tier is free.** p=0.798 — indistinguishable from sending no map.
A one-line-per-file map of all 80 files, at no measurable cost. This is the
first configuration in this file that a session cannot tell from the control.

**A large map in a cached prefix costs linearly.** `CLAUDE.md` is re-read every
turn, so 86,880 tokens are re-read every turn. The +118.5% is that arithmetic,
not a defect in repomix's compression.

> **This is not a fair test of repomix, and saying so matters.**
>
> repomix is built to pack a repository into one file for one-shot consumption
> — you paste it, you ask, you are done. It is not built to sit in a
> persistent prefix that is re-read on every turn, which is what we did to it.
> Used as designed, in a single-turn session, its per-turn cost is paid once
> and this result does not apply.
>
> What the run measures is **the shape a map needs for persistent delivery**: a
> locator, not an archive. That is a statement about delivery mechanics, which
> both tools are subject to, not about which tool compresses better. On
> compression density cslim is 2.6× repomix per file, and that is a separate
> claim resting on the token counts above, not on this benchmark.

Run cost: 32 sessions, $7.91.

### flask · 80 files · 12 turns · 6 sessions/arm · 4 arms

`cslim` 0.1.0 · task set `flask` · 2026-08-26 · [run2.json](run2.json)

The re-run with double the repeats, because three was too few.

| arm | tokens | cache write | cost/session | vs control | exceeds spread |
| --- | --- | --- | --- | --- | --- |
| no map | 159,904 | 9,356 | $0.1926 | — | — |
| hook · full AST | 150,308 | 18,874 | $0.2782 | +44.4% | no |
| CLAUDE.md · full AST | 268,557 | 19,830 | $0.3421 | +77.6% | no |
| CLAUDE.md · index | 156,204 | 9,335 | $0.1897 | −1.5% | no |

Control spread $0.0693; run-to-run spread $0.2594.
**Verdict: no measurable difference.** Doubling the repeats did not settle it.

A rank test agrees, and it is not swayed by outliers the way a standard
deviation is. Exact two-tailed Mann-Whitney against the control, n=6 per group:

| arm | U | p | |
| --- | --- | --- | --- |
| hook · full AST | 30 | 0.065 | not significant |
| CLAUDE.md · full AST | 28 | 0.132 | not significant |
| CLAUDE.md · index | 11 | 0.310 | not significant |

The hook comes closest and still misses. Per-session costs show why the noise
did not shrink:

| arm | sessions | stdev |
| --- | --- | --- |
| no map | 0.311 0.155 0.148 0.245 0.153 0.145 | $0.069 |
| hook · full AST | 0.298 0.270 0.289 0.269 0.252 0.291 | **$0.018** |
| CLAUDE.md · full AST | **0.691** 0.158 0.168 **0.660** 0.162 0.213 | **$0.259** |
| CLAUDE.md · index | 0.332 0.144 0.284 0.143 0.103 0.132 | $0.094 |

`CLAUDE.md · full AST` is bimodal — two sessions near $0.67, four near $0.17.
That is not dispersion, it is two behaviours, and that one arm produces the
$0.2594 spread that sinks the verdict for everything else. Why it happens is
unexplained and is the next thing worth instrumenting.

The opposite is also worth recording: `hook · full AST` is the steadiest arm on
the board (stdev $0.018), and its cheapest session, $0.252, is dearer than five
of the six control sessions.

#### The statistic was the problem, not the sample size

The verdict above uses the mean and a standard deviation. Both are wrecked by a
single session costing four times its siblings. Recomputed with the median and
a scaled median absolute deviation — the estimators for demonstrably bimodal
data:

| arm | median | vs control | vs robust spread |
| --- | --- | --- | --- |
| no map | $0.1537 | — | — |
| hook · full AST | $0.2798 | **+82.0%** | **exceeds** ($0.126 gap vs $0.045) |
| CLAUDE.md · full AST | $0.1905 | +23.9% | within |
| CLAUDE.md · index | $0.1436 | −6.5% | within |

Run-to-run spread: $0.2594 by stdev, **$0.0451 by MAD**.

So on the robust view there *is* a result: **the hook costs measurably more,
and neither CLAUDE.md arm is distinguishable from sending no map at all.** That
agrees with `inject_probe` (hook +176%, CLAUDE.md +36%), with the cache-write
replication below, and with the cost model in `cslim/core/costmodel.py`.

> **Read this before quoting the robust view.** The estimator was changed after
> seeing data it would flip. That is the manoeuvre that manufactures findings,
> and the justification — the distribution is visibly bimodal in the raw
> per-session numbers, and MAD is the standard estimator for that — does not
> erase the fact that it was chosen afterwards. The rank test, which was chosen
> before and is already robust, puts the hook at **p=0.065**: closest, still
> short of conventional significance. `bench/ab.py` now prints both views and
> never swaps one for the other, so the disagreement stays visible.

#### The one thing that replicated

Cache write, across two independent runs:

| arm | run 1 (3 reps) | run 2 (6 reps) |
| --- | --- | --- |
| no map | 14,044 | 9,356 |
| hook · full AST | 20,342 | 18,874 |
| CLAUDE.md · index | 9,518 | 9,335 |

**The hook roughly doubles cache write; CLAUDE.md leaves it at the control's
level.** Both runs, independently, on real maps in real sessions — which is the
mechanism `inject_probe` measured with a synthetic payload.

That is a claim about the *mechanism*, not about cost. Cost is still buried in
the noise. The two are separate statements and this file keeps them separate.

### flask · 80 files · 12 turns · 3 sessions/arm · 5 arms

`cslim` 0.1.0 · task set `flask` · 2026-08-25

| arm | cost/session | vs control | exceeds spread |
| --- | --- | --- | --- |
| no map | $0.2436 | — | — |
| hook · full AST | $0.2953 | +21.2% | no |
| hook · index | $0.3021 | +24.0% | no |
| CLAUDE.md · full AST | $0.3423 | +40.5% | no |
| CLAUDE.md · index | $0.2049 | −15.9% | no |

Control spread $0.0731; run-to-run spread $0.3013.
**Verdict: no measurable difference. None of those percentages is a result.**

Per-session costs, because they explain the verdict:

| arm | run 1 | run 2 | run 3 |
| --- | --- | --- | --- |
| no map | $0.3065 | $0.2610 | $0.1634 |
| CLAUDE.md · full AST | **$0.6901** | $0.1683 | $0.1684 |
| CLAUDE.md · index | $0.3266 | $0.1378 | $0.1503 |

One session at $0.69 against $0.168 for its two siblings is the entire +40.5%.
The control itself ranged 1.9× untouched. Three samples cannot separate those.

The one thing that is not a cost: `CLAUDE.md · index` wrote 9,518 cache tokens
against the control's 14,044 — the only arm below the control, consistent with
CLAUDE.md riding an already-cached prefix. The other CLAUDE.md arm did not
reproduce it (20,353).

### flask · 80 files · 12 turns · 3 sessions/arm · 3 arms

The earlier run, before CLAUDE.md delivery existed.

| arm | cost/session | vs control | exceeds spread |
| --- | --- | --- | --- |
| no hook | $0.7814 | — | — |
| full AST map | $0.9729 | +24.5% | yes |
| index only | $0.9213 | +17.9% | yes |

Control spread ±$0.0820. **Verdict: measurable.** The hook penalty cleared the
noise here, and the amortization hypothesis was falsified: 12 turns showed the
same penalty as 2.

> These two runs do not agree, and the difference is worth stating plainly.
> The hook arms landed at +21–24% in the newer run and +18–25% in the older
> one — the same direction and size — but the newer run's spread was four times
> wider, so it cannot certify what the older one did. Treat the hook penalty as
> *probable and replicated in magnitude*, not settled.

### flask · 80 files · 2 turns · 3 sessions/arm

| arm | cost/session | vs control | exceeds spread |
| --- | --- | --- | --- |
| no hook | $0.2515 | — | — |
| full AST map | $0.3038 | +20.8% | no |
| index only | $0.2940 | +16.9% | no |

Control spread ±$0.0967. **Verdict: no measurable difference.**

### cslim · 18 files · 3 turns · 3 sessions/arm

| arm | cost/session | vs control | exceeds spread |
| --- | --- | --- | --- |
| none | $0.3187 | — | — |
| 10.5k skeleton | +33.2% | +33.2% | yes |
| 2.8k tiered | +21.2% | +21.2% | yes |

**Verdict: measurable.** Token count went down while cost went up.

---

## Community runs

None yet. If you run this on a repository we do not have — especially a large
monorepo — this is the table that would change what cslim recommends.

| repo | files | turns | sessions/arm | best arm | verdict | file |
| --- | --- | --- | --- | --- | --- | --- |
| _yours_ | | | | | | |
