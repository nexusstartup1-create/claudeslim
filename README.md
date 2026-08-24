# ClaudeSlim

**Context-window optimizer and noise reducer for Claude Code.**

`cslim` strips the metadata out of git diffs and terminal output, and builds
compact architectural maps of a codebase from its AST — so Claude Code spends
its context on your code instead of on blob hashes, lockfile churn and blind
exploration.

Every number below comes from our own benchmarks. Where a claim isn't supported
by them — or was contradicted by them — it says so.

---

## The problem

Two things quietly eat a Claude Code session:

**Verbose diffs.** A `git diff` is half metadata — blob hashes, `--- a/ +++ b/`
pairs, mode bits, lockfile churn, whitespace-only reformatting. None of it helps
the model, all of it occupies context.

**Blind exploration.** Claude doesn't know where anything lives. Asked *"where's
the session logic?"*, it greps, guesses, and opens a dozen files in full — every
loop and error handler included.

## The solution, and its two very different halves

This is the distinction most tools in this space skip, and it decides everything:

| | What it does | Cost |
| --- | --- | --- |
| **Subtractive** — `diff`, `log`, `clean` | Removes tokens, adds nothing | Cannot cost more than not using it |
| **Additive** — the codebase map | Injects context to prevent exploration | **Measured ~20% *more* per session** |

The subtractive half is an unambiguous win. The additive half buys you a repo
that fits in the context window, and you pay for it.

That price turns out to depend almost entirely on *how* the map is delivered,
not on how big it is: the hook mechanism cslim ships costs roughly five times
what the same payload costs when placed in `CLAUDE.md`. See
[Where you put the map is what it costs](#where-you-put-the-map-is-what-it-costs).

---

## Features

### `cslim diff` / `cslim clean` — pure subtraction

```console
$ git diff --cached | wc -l
26
$ cslim diff --staged | wc -l
12
```

`diff` drops blob hashes, `--- a/ +++ b/` headers, mode bits, lockfiles,
generated files, minified bundles and whitespace-only hunks, while keeping the
change fully readable. Measured on a small staged change: **26 → 12 lines,
224 → 96 tokens (−57%)**.

`clean` de-noises terminal and CI output: ANSI codes, progress bars collapsed to
their final state, timestamps, repeated lines counted (`FAIL src/cart.test.ts
(×3)`), npm and pip chatter. Measured on a sample build log: **101 → 40 tokens
(−60%)**.

```bash
git diff | cslim clean | claude -p "review this diff"
pytest 2>&1 | cslim clean | claude -p "why is this failing?"
```

### `--index-only` (`-I`) — the ultralight map

One line per file naming what it defines. No signatures, no types, no bodies.

```
core/hook.py: HookConfig, HookOutcome, state_dir, build_map, run_hook
core/ranking.py: FileRank, build_reference_graph, rank_files
```

| project | files | source | index map |
| --- | --- | --- | --- |
| cslim | 18 | 43.9k tokens | **461 tokens** |
| pallets/flask | 80 | 143.9k tokens | **3.1k tokens** |

### Full AST skeletons — the detailed map

Signatures, type hints, class hierarchies, imports and summary docstrings, with
every function body replaced by `...`:

```python
def copy_to_clipboard(text: str) -> str:
    """Copy ``text`` to the system clipboard; returns the backend used."""
    ...
```

| project | source | skeleton map |
| --- | --- | --- |
| cslim | 43.9k tokens | 9.8k (−77.7%) |
| pallets/flask | 143.9k tokens | 33.9k (−76.4%) |

Files are ranked by a reference graph — how many other files use what they
define — so a token budget buys skeletons for the modules that explain the
project and one-line index entries for the rest. Coverage stays at 100% of files
down to very tight budgets: files lose depth, not existence.

> **Honesty note.** The tiering exists to make maps cheaper. A controlled probe
> found that it doesn't: injecting 5k tokens and injecting 30k tokens cost the
> same, because the price comes from the delivery mechanism rather than the
> payload. See [Why map size doesn't matter](#why-map-size-doesnt-matter).
> Tiering remains useful for **fitting** a large repo into context; it is not a
> cost optimisation.

---

## Install

**Do not `pip install claudeslim`** — that name on PyPI belongs to a different,
unrelated project. `cslim` is not published to PyPI; install from source.

Requires **Python 3.10+** and git.

```bash
git clone <repo-url> cslim
cd cslim

# recommended: a global `cslim` command (needs https://docs.astral.sh/uv/)
uv tool install --editable . --with textual

# or a virtual environment
python3 -m venv .venv && .venv/bin/pip install -e '.[all]'
```

Verify with `cslim doctor`.

### Optional extras

Everything works without them.

| Extra | Enables |
| --- | --- |
| `textual` | the `cslim tui` interactive interface |
| `anthropic` | `--exact` token counts from the API |
| `tiktoken` | offline BPE counting, more accurate than the heuristic |
| `pathspec` | full `.gitignore` support |
| `pyperclip` | clipboard fallback where no native tool exists |

---

## Quickstart

**Measure first — this changes nothing:**

```bash
cd ~/your-project
cslim stats .
```

**Clean a diff for review — the safest win:**

```bash
cslim diff --staged | claude -p "review this"
```

**Give Claude a map for one question:**

```bash
cslim . --index-only | claude -p "where does authentication happen?"
```

**Register the hook to inject a map automatically, once per session:**

```bash
cslim install
cslim uninstall     # removes only our entry, leaves other hooks alone
```

`cslim install` prints a warning telling you what it costs. Read
[Benchmarks](#benchmarks) before enabling it.

---

## Prompt caching math

Anthropic bills a **cache write at ~1.25×** base input and a **cache read at
~0.1×**. Therefore:

> **One token of freshly injected context costs roughly what twelve cache-read
> tokens cost.**

Claude Code's exploration is almost entirely cache reads — re-reading a file it
already pulled is nearly free. Injecting a map is not. A map only pays for
itself if it prevents more than ~12× its own size in exploration.

### Why map size doesn't matter

We assumed the way out was a smaller map. `bench/cache_probe.py` tested that
directly, injecting synthetic filler of controlled size through the real hook
(3 sessions per arm, 2 turns, on Flask):

| injected | cache write | ±stdev | Δ vs control | cost |
| --- | --- | --- | --- | --- |
| 0 (control) | 6,221 | 531 | — | $0.1682 |
| 500 | 13,235 | 9,307 | +7,014 | $0.2448 |
| 5,000 | 23,605 | 894 | +17,384 | $0.3315 |
| 30,000 | 22,955 | 667 | +16,734 | $0.3442 |

**5k and 30k are indistinguishable.** Six times more content moved cache write
by 650 tokens, inside the ±780 spread. And 500 tokens already triggered +7,014
of cache write — fourteen times the payload, with by far the widest spread of
any arm, which looks like an all-or-nothing re-cache rather than a proportional
cost.

**Cost is dominated by the *act* of injecting, not by size.** Shrinking a map
does not make it cheaper — which demotes the tiering from a cost optimisation to
a context-fitting tool.

### Where you put the map is what it costs

If size isn't the lever, the delivery mechanism might be. `bench/inject_probe.py`
holds the payload identical (5,026 synthetic tokens) and varies only how it
reaches the model — 6 sessions per arm, each arm verified to actually be seen
using a sentinel fact:

| delivery | cache write | Δ vs control | cost / session | vs control |
| --- | --- | --- | --- | --- |
| nothing (control) | 2,576 | — | $0.0843 | — |
| `additionalContext` (the hook) | 16,835 | **+14,258** | $0.2328 | **+176%** |
| `CLAUDE.md` | 1,578 | **−999** | $0.1147 | **+36%** |

The gap between the two mechanisms is **6.9× the run-to-run spread**.

`additionalContext` is inserted per session and invalidates a cache boundary,
forcing a ~14k block rewrite whatever the payload — precisely the plateau above.
`CLAUDE.md` is part of the stable prefix Claude Code already caches, so it adds
**no measurable cache write at all**; its remaining +36% is the honest price of
the content being cache-read each turn at 0.1×.

**This reframes every benchmark on this page.** The overhead they measured is
not inherent to giving Claude a map — it is specific to the mechanism cslim
currently uses. The same map through `CLAUDE.md` costs about a fifth.

> **Not implemented.** cslim does not yet deliver maps this way, because doing
> so writes into your repository rather than your cache directory — a different
> trade-off from the hook, which writes nothing. See
> [Security](#security).

### Which mode to use

| Situation | Use | Confidence |
| --- | --- | --- |
| Reviewing a diff | `cslim diff` | **Measured.** Pure subtraction. |
| Debugging test/CI output | `cslim clean` | **Measured.** Same. |
| Repo doesn't fit the context window | `--index-only` | **Measured** to fit. Buys coverage, costs money. |
| You want to spend less money | no map | **Measured.** No map configuration beat the control — but every one of those runs used the hook, whose delivery cost is ~5× the alternative. |
| Long sessions on a large repo | no map | **Measured, hypothesis falsified.** 12-turn sessions showed the same ~18% penalty as 2-turn ones. |

---

## Benchmarks

`bench/ab.py` runs identical tasks through real Claude Code sessions with and
without the hook, and reads Anthropic's own `total_cost_usd` back out of
`claude -p --output-format json`. Nothing is estimated.

```bash
python bench/ab.py --tasks 3 --turns 3 --repeats 3
python bench/ab.py --project /path/to/flask --task-set flask --turns 12 --repeats 3
python bench/cache_probe.py --project /path/to/flask --repeats 3
```

### Results

**cslim itself** — 18 files, 3 sessions per arm, 3 turns:

| map | total tokens | cost / session |
| --- | --- | --- |
| none | — | $0.3187 |
| 10.5k skeleton | −13.2% | **+33.2%** |
| 2.8k tiered | −2.2% | **+21.2%** |

**pallets/flask** — 80 files, 3 sessions per arm, **12 turns each**:

| arm | total tokens | cache write | cost / session | vs control |
| --- | --- | --- | --- | --- |
| no hook | 1,065,226 | 18,518 | $0.7814 | — |
| full AST map | 1,055,139 | 37,966 | $0.9729 | +24.5% |
| index only | 1,013,816 | 37,574 | $0.9213 | **+17.9%** |

Control spread ±$0.0820 — both gaps clear it, so this one is conclusive.

The 12-turn run was the experiment most likely to vindicate the map: pay once,
amortize over twelve turns. It didn't. The penalty at 12 turns matches the
penalty at 2. A per-turn probe explains why: after the first turn each turn costs
a flat ~$0.055, so there is no growing exploration cost for a map to displace.

The harness enforces its own statistics — it compares each effect against the
run-to-run spread and refuses to report a percentage when noise dominates, in
either direction.

### Not yet measured

1. **Repos too large to explore at all.** Flask fits comfortably in context. On
   a 1000-file monorepo a map might be the only way to get a useful answer;
   cost would likely still be higher.
2. **Editing tasks.** All our questions are read-only lookups with short
   answers.
3. **Whether `CLAUDE.md` delivery beats no map at all** on a repo big enough to
   matter. It costs +36% against a Flask that Claude can already explore
   cheaply; on a codebase where exploration is genuinely expensive, +36% of a
   small number may be less than what it prevents. This is now the most
   promising open question — and the one change that could make the map pay
   for itself.

**Run it on your own repository.** `bench/README.md` has the full method.
Results from real-world codebases are the most useful contribution right now.

---

## Architecture

```
cslim/
├── main.py            entry point, implicit `pack` command
├── cli.py             Typer + Rich
└── core/
    ├── models.py      shared dataclasses
    ├── discovery.py   file walking, .gitignore, ignore lists
    ├── compressor.py  the extraction engine
    ├── ranking.py     reference graph, file importance
    ├── tokenizer.py   token estimation + context budgeting
    ├── renderer.py    md / plain / xml layout
    ├── git_cleaner.py diff, log and terminal sanitizers
    ├── claude_pipe.py stdout / clipboard / `claude` delivery
    ├── hook.py        automatic mode
    ├── installer.py   settings.json surgery
    └── service.py     orchestration facade
```

`core/` never prints, never exits, never touches a terminal. The CLI and the
Textual TUI both go through one facade:

```python
from cslim.core import CompressionService, CompressRequest

bundle = CompressionService().run(CompressRequest(paths=(Path("src"),)))
bundle.stats.ratio   # 0.771
bundle.payload       # the text you'd send to Claude
```

### Language support

| Language | Method | Fidelity |
| --- | --- | --- |
| Python | native `ast` module | exact — output re-parses as valid Python |
| TypeScript / JavaScript | brace-aware scanner | heuristic, string- and comment-safe |
| Go | brace-aware scanner | heuristic |
| Markdown | outline extractor | heuristic |
| Anything else | whitespace normalisation | safe fallback |

Only Python uses a real parser today. The brace scanner tracks strings, template
literals and comments so a `{` inside a string can't confuse it, and it keeps
multi-line signatures intact — but it is a heuristic, and it has **not** been
validated against large real-world TypeScript. A tree-sitter backend is a
drop-in replacement via `register_compressor()`; nothing outside
`compressor.py` would change.

### Security

- **Runs locally.** No proxy, no daemon, no background process.
- **No account connection.** `cslim` never asks for Claude credentials. Claude
  Code keeps its own authentication; the hook is a plain local subprocess.
- **No network**, unless you pass `--exact`.
- **Deterministic.** Same input, same output.
- **Nothing written to your repo.** Cache and session state live in your user
  cache directory. `cslim install` touches only `.claude/settings.json`, backs
  it up first, and refuses to write if it isn't valid JSON.

The cheaper delivery mechanism we measured — putting the map in `CLAUDE.md` —
would break that last property: the map would live in your repository and be
committed to git. That is why it is measured but not implemented. Whether the
~5× cost reduction is worth writing into your tree is a decision for you, not a
default we should pick.

---

## Development

```bash
uv venv && uv pip install -e '.[dev,all]'
.venv/bin/pytest        # 66 tests
.venv/bin/mypy cslim    # strict
.venv/bin/ruff check cslim bench
```

---

## License

MIT.
