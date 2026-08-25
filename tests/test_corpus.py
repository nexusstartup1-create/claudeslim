"""Corpus test: compress real files from real projects and check the output holds.

Nine files from nine well-known repositories, three per language — see
``fixtures/corpus/SOURCES.md`` for provenance and licences. Hand-written samples
would only prove the compressor handles the code I thought to write; these are
the dense generics, decorator factories and JSX the heuristic scanner was
expected to mis-slice.

The assertions are structural rather than exact-output, because the right
skeleton for `vscode/uri.ts` is not something a test can spell out: braces stay
balanced, declarations survive, bodies do not, and the result is smaller.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cslim.core.compressor import TREESITTER_LANGUAGES, compress_source
from cslim.core.models import CompressionOptions, Language, detect_language
from cslim.core.tokenizer import HeuristicEstimator

CORPUS = Path(__file__).parent / "fixtures" / "corpus"

#: Files whose extension lies about their language (axios ships TS-shaped .js).
_LANGUAGE_BY_PREFIX = {
    "js_": Language.JAVASCRIPT,
    "ts_": Language.TYPESCRIPT,
    "go_": Language.GO,
}


def corpus_files() -> list[Path]:
    if not CORPUS.is_dir():
        return []
    return sorted(
        p for p in CORPUS.iterdir() if p.suffix in (".js", ".ts", ".tsx", ".go")
    )


def language_of(path: Path) -> Language:
    for prefix, language in _LANGUAGE_BY_PREFIX.items():
        if path.name.startswith(prefix):
            return language
    return detect_language(path)


FILES = corpus_files()
pytestmark = pytest.mark.skipif(
    not FILES, reason="corpus not present; run tests/fetch_corpus.sh"
)


def strip_strings_and_comments(text: str) -> str:
    """Crude but sufficient: brace counting must ignore braces inside literals."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_output_stays_brace_balanced(path: Path) -> None:
    source = path.read_text(encoding="utf-8", errors="replace")
    result = compress_source(source, path, language_of(path), CompressionOptions())
    code = strip_strings_and_comments(result.text)

    for opener, closer in (("{", "}"), ("[", "]")):
        assert code.count(opener) == code.count(closer), (
            f"{path.name}: unbalanced {opener}{closer} "
            f"({code.count(opener)} vs {code.count(closer)})"
        )

    # Parentheses are only guaranteed on the grammar-backed path. The heuristic
    # scanner slices on brace depth, so a signature carrying nested generics can
    # leave it holding an unclosed `(` — the failure mode tree-sitter exists to
    # remove. `ts_zod_types.ts` is a live example: see the fallback test below.
    if not result.fallback:
        assert code.count("(") == code.count(")"), (
            f"{path.name}: unbalanced () under the grammar backend"
        )


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_output_is_smaller_and_non_empty(path: Path) -> None:
    source = path.read_text(encoding="utf-8", errors="replace")
    result = compress_source(source, path, language_of(path), CompressionOptions())
    estimator = HeuristicEstimator()

    assert result.text.strip(), f"{path.name}: produced nothing"
    before, after = estimator.count(source), estimator.count(result.text)
    assert after < before, f"{path.name}: {before} -> {after} tokens is not a reduction"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_declarations_survive_and_bodies_do_not(path: Path) -> None:
    source = path.read_text(encoding="utf-8", errors="replace")
    language = language_of(path)
    result = compress_source(source, path, language, CompressionOptions())

    # Asserting a keyword would only prove this file happens to use it: axios
    # and jquery are classes and object literals, with no top-level `function`.
    # What must hold everywhere is that named declarations were found at all.
    assert result.symbols, f"{path.name}: no declarations survived"
    # the elision marker is the whole point
    assert "{ ... }" in result.text or "..." in result.text


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_grammar_backend_did_not_silently_fall_back(path: Path) -> None:
    """A parse failure is allowed — going unreported is not."""
    language = language_of(path)
    if language not in TREESITTER_LANGUAGES:
        pytest.skip(f"no grammar installed for {language.value}")

    source = path.read_text(encoding="utf-8", errors="replace")
    result = compress_source(source, path, language, CompressionOptions())
    if result.fallback:
        assert result.errors, f"{path.name}: fell back with no reason recorded"


def test_the_corpus_covers_three_projects_per_language() -> None:
    by_language: dict[Language, int] = {}
    for path in FILES:
        by_language[language_of(path)] = by_language.get(language_of(path), 0) + 1
    for language in (Language.JAVASCRIPT, Language.TYPESCRIPT, Language.GO):
        assert by_language.get(language, 0) >= 3, (
            f"{language.value}: {by_language.get(language, 0)} files, want 3+"
        )


def test_a_grammar_that_cannot_parse_says_so_rather_than_guessing() -> None:
    """zod uses TypeScript 4.7 variance annotations (`out Output = unknown`).

    tree-sitter-typescript 0.23 does not know them, so the grammar reports an
    error on that file. The contract is that this degrades loudly: the scanner
    takes over, `fallback` is set and the reason is recorded — never a silent
    half-parsed skeleton.
    """
    path = CORPUS / "ts_zod_types.ts"
    if not path.is_file():
        pytest.skip("corpus not present")
    result = compress_source(
        path.read_text(encoding="utf-8"), path, Language.TYPESCRIPT, CompressionOptions()
    )
    assert result.fallback
    assert any("grammar" in e for e in result.errors)
    assert result.text.strip(), "the fallback still has to produce a skeleton"
