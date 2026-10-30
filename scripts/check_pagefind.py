"""Compare how well Pagefind finds Korean words with and without supplementary indexing.

Usage: uv run python scripts/check_pagefind.py [--suite curated|generated|all] [--size N]
                                               [--output report.md]
Needs the dev dependencies and `uv run playwright install chromium`.

Variants:
  none    plain Pagefind
  a       index Hangul bigrams of every word
  b       variant a's index, and the query is split into bigrams too
  c       index every suffix of Hangul words and of adjacent words joined together
  d       variant b, plus bigrams of adjacent words joined together
  e       variant d, also treating jamo-only text (ㅋㅋ, ㅇㅋ) as Hangul
  f       variant e, also splitting words where Latin/digits meet Hangul (API문서 -> API 문서)
"""

import argparse
import html
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Browser, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from search_cases import CURATED, Suite, generated

SYLLABLES = "가-힣"
JAMO = "ㄱ-ㆎ"
MAX_SUFFIX_WORD = 12
PIECE_WEIGHT = "0.1"


@dataclass(frozen=True)
class Script:
    token: re.Pattern[str]
    hangul: re.Pattern[str]

    def tokens(self, text: str) -> list[str]:
        return self.token.findall(unicodedata.normalize("NFC", text))

    def is_hangul(self, word: str) -> bool:
        return bool(self.hangul.search(word))


SYLLABLE_ONLY = Script(re.compile(f"[0-9A-Za-z{SYLLABLES}]+"), re.compile(f"[{SYLLABLES}]"))
WITH_JAMO = Script(re.compile(f"[0-9A-Za-z{SYLLABLES}{JAMO}]+"), re.compile(f"[{SYLLABLES}{JAMO}]"))
# Latin/digit runs and Hangul runs become separate words, so "API문서" -> "API", "문서"
SPLIT_SCRIPTS = Script(
    re.compile(f"[0-9A-Za-z]+|[{SYLLABLES}{JAMO}]+"), re.compile(f"[{SYLLABLES}{JAMO}]")
)


def bigrams(word: str) -> list[str]:
    return [word[i : i + 2] for i in range(len(word) - 1)]


def suffixes(word: str) -> list[str]:
    word = word[:MAX_SUFFIX_WORD]
    return [word[i:] for i in range(1, len(word))]


def neighbours(script: Script, text: str) -> list[str]:
    words = script.tokens(text)
    return [
        left + right
        for left, right in zip(words, words[1:], strict=False)
        if script.is_hangul(left) and script.is_hangul(right)
    ]


def bigram_pieces(script: Script, text: str) -> set[str]:
    return {p for t in script.tokens(text) if script.is_hangul(t) for p in bigrams(t)}


def joined_bigram_pieces(script: Script, text: str) -> set[str]:
    pieces = bigram_pieces(script, text)
    for joined in neighbours(script, text):
        pieces.update(bigrams(joined))
    return pieces


def suffix_pieces(script: Script, text: str) -> set[str]:
    pieces = {p for t in script.tokens(text) if script.is_hangul(t) for p in suffixes(t)}
    for joined in neighbours(script, text):
        pieces.add(joined)
        pieces.update(suffixes(joined))
    return pieces


def bigram_query(script: Script, query: str) -> str:
    out: list[str] = []
    for word in script.tokens(query):
        long_hangul = script.is_hangul(word) and len(word) > 2
        out.extend(bigrams(word) if long_hangul else [word])
    return " ".join(out)


Pieces = Callable[[str], set[str]]


@dataclass(frozen=True)
class Variant:
    name: str
    site: str
    pieces: Pieces | None
    query: Callable[[str], str] = lambda q: q


VARIANTS = [
    Variant("none", "none", None),
    Variant("a", "bigram", partial(bigram_pieces, SYLLABLE_ONLY)),
    Variant(
        "b", "bigram", partial(bigram_pieces, SYLLABLE_ONLY), partial(bigram_query, SYLLABLE_ONLY)
    ),
    Variant("c", "suffix", partial(suffix_pieces, SYLLABLE_ONLY)),
    Variant(
        "d",
        "joined",
        partial(joined_bigram_pieces, SYLLABLE_ONLY),
        partial(bigram_query, SYLLABLE_ONLY),
    ),
    Variant(
        "e", "jamo", partial(joined_bigram_pieces, WITH_JAMO), partial(bigram_query, WITH_JAMO)
    ),
    Variant(
        "f",
        "split",
        partial(joined_bigram_pieces, SPLIT_SCRIPTS),
        partial(bigram_query, SPLIT_SCRIPTS),
    ),
]
SITES = {v.site: v.pieces for v in VARIANTS}

# Only the search call is timed; loading result data is excluded
SEARCH_JS = """
async ([site, query]) => {
  const pf = await import(`/${site}/pagefind/pagefind.js`);
  const start = performance.now();
  const search = await pf.search(query);
  const ms = performance.now() - start;
  // Load result data in small batches; thousands of parallel fetches overwhelm the local server
  const ids = [];
  for (let i = 0; i < search.results.length; i += 50) {
    const batch = await Promise.all(search.results.slice(i, i + 50).map((r) => r.data()));
    ids.push(...batch.map((d) => d.meta.id));
  }
  return { ms, ids };
}
"""


@dataclass
class Build:
    size_bytes: int
    seconds: float


@dataclass
class Run:
    found: list[set[str]]
    times_ms: list[float]
    fetched_bytes: int
    requests: int


def build_site(root: Path, samples: dict[str, str], pieces: Pieces | None) -> Build:
    root.mkdir(parents=True)
    root.joinpath("index.html").write_text(
        '<!doctype html><html lang="ko"><meta charset="utf-8"><title>check</title></html>',
        encoding="utf-8",
    )
    for sid, text in samples.items():
        extra = ""
        if pieces is not None:
            joined = html.escape(" ".join(sorted(pieces(text))))
            extra = f'<span data-pagefind-weight="{PIECE_WEIGHT}">{joined}</span>'
        page = (
            '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f"<title>{sid}</title></head><body><main data-pagefind-body>"
            f'<p data-pagefind-meta="id:{sid}">{html.escape(text)}</p>{extra}</main></body></html>'
        )
        root.joinpath(f"{sid}.html").write_text(page, encoding="utf-8")
    start = time.perf_counter()
    # The extended binary is the one with CJK segmentation
    subprocess.run(
        [sys.executable, "-m", "pagefind", "--site", str(root)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    seconds = time.perf_counter() - start
    size = sum(p.stat().st_size for p in root.joinpath("pagefind").rglob("*") if p.is_file())
    return Build(size, seconds)


def build_all(root: Path, suite: Suite, parallel: bool) -> dict[str, Build]:
    if not parallel:
        return {site: build_site(root / site, suite.samples, p) for site, p in SITES.items()}
    with ThreadPoolExecutor(max_workers=len(SITES)) as pool:
        futures = {
            site: pool.submit(build_site, root / site, suite.samples, p)
            for site, p in SITES.items()
        }
        # result() re-raises any build failure here instead of losing it
        return {site: f.result() for site, f in futures.items()}


class ServedLog:
    """Bytes served per path, counted on the server so browser workers are included."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[tuple[str, int]] = []

    def add(self, path: str, size: int) -> None:
        with self._lock:
            self._entries.append((path, size))

    def mark(self) -> int:
        with self._lock:
            return len(self._entries)

    def since(self, mark: int) -> list[tuple[str, int]]:
        with self._lock:
            return self._entries[mark:]


SERVED = ServedLog()


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def send_head(self):  # noqa: ANN201 - matches the base signature
        f = super().send_head()
        if f is not None and hasattr(f, "fileno"):
            SERVED.add(self.path.split("?")[0], os.fstat(f.fileno()).st_size)
        return f


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        # The browser may drop connections it no longer needs; that is not a test failure
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


@contextmanager
def serve(root: Path) -> Iterator[str]:
    server = QuietServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_variant(browser: Browser, url: str, suite: Suite, variant: Variant) -> Run:
    # A fresh context per variant so cached index chunks never leak between variants
    context = browser.new_context()
    try:
        page = context.new_page()
        mark = SERVED.mark()
        page.goto(f"{url}{variant.site}/index.html")
        page.evaluate(SEARCH_JS, [variant.site, "warmup"])
        found, times = [], []
        # Sequential on purpose: concurrent searches would distort the timings
        for case in suite.cases:
            result = page.evaluate(SEARCH_JS, [variant.site, variant.query(case.query)])
            found.append(set(result["ids"]))
            times.append(result["ms"])
        # Result fragments are excluded: this script loads every result, a viewer loads a page
        index_files = [
            size
            for path, size in SERVED.since(mark)
            if path.startswith(f"/{variant.site}/pagefind/") and "/fragment/" not in path
        ]
        return Run(found, times, sum(index_files), len(index_files))
    finally:
        context.close()


def run_suite(suite: Suite, measure: bool) -> tuple[dict[str, Run], dict[str, Build]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        builds = build_all(root, suite, parallel=not measure)
        with serve(root) as url, sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                runs = {v.name: run_variant(browser, url, suite, v) for v in VARIANTS}
            finally:
                browser.close()
    return runs, builds


@dataclass
class Score:
    hit: int = 0
    must: int = 0
    wrong: int = 0
    other: int = 0


def score(suite: Suite, run: Run) -> tuple[Score, list[str]]:
    total, cells = Score(), []
    for case, found in zip(suite.cases, run.found, strict=True):
        hit = len(case.must & found)
        wrong = len(case.wrong & found)
        other = len(found - case.must - case.wrong)
        total.hit += hit
        total.must += len(case.must)
        total.wrong += wrong
        total.other += other
        cell = f"{hit}/{len(case.must)}"
        if wrong:
            cell += f" ✗{wrong}"
        if other:
            cell += f" (+{other})"
        cells.append(cell)
    return total, cells


def shown(query: str) -> str:
    return query if unicodedata.is_normalized("NFC", query) else f"{query} (NFD)"


def report_cases(suite: Suite, runs: dict[str, Run]) -> list[str]:
    names = [v.name for v in VARIANTS]
    scored = {n: score(suite, runs[n]) for n in names}
    lines = [
        f"### {suite.name}: cases",
        "",
        "| Case | Query | " + " | ".join(names) + " |",
        "| --- | --- | " + " | ".join("---" for _ in names) + " |",
    ]
    for i, case in enumerate(suite.cases):
        cells = " | ".join(scored[n][1][i] for n in names)
        lines.append(f"| {case.label} | {shown(case.query)} | {cells} |")
    lines.append("")
    return lines


def report_summary(suite: Suite, runs: dict[str, Run], builds: dict[str, Build]) -> list[str]:
    lines = [
        f"### {suite.name}: summary ({len(suite.samples)} messages, {len(suite.cases)} queries)",
        "",
        "| Variant | Recall | Wrong | Other | Index | Build | Search median | Search p95 "
        "| Index fetched (all queries) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for v in VARIANTS:
        run, build = runs[v.name], builds[v.site]
        total, _ = score(suite, run)
        recall = f"{total.hit}/{total.must} ({total.hit / max(total.must, 1):.0%})"
        times = sorted(run.times_ms)
        p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
        lines.append(
            f"| {v.name} | {recall} | {total.wrong} | {total.other} | "
            f"{build.size_bytes / 1024:.0f} KB | {build.seconds:.1f} s | "
            f"{statistics.median(times):.1f} ms | {p95:.1f} ms | "
            f"{run.fetched_bytes / 1024:.0f} KB in {run.requests} requests |"
        )
    lines.append("")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", choices=["curated", "generated", "all"], default="all")
    parser.add_argument("--size", type=int, default=2000, help="Generated corpus size.")
    parser.add_argument("--output", type=Path, help="Also write the report to this file.")
    args = parser.parse_args()
    if args.size < 1:
        parser.error("--size must be positive")

    plan = []
    if args.suite in ("curated", "all"):
        # Small corpus: build in parallel; its timings are not meaningful anyway
        plan.append((CURATED, False))
    if args.suite in ("generated", "all"):
        # Large corpus: build sequentially so build times are not inflated
        plan.append((generated(args.size), True))

    lines = [
        "`n/m` found n of m expected; `✗w` hit w precision traps; "
        "`(+o)` o unlabeled extra results.",
        "",
    ]
    try:
        for suite, measure in plan:
            runs, builds = run_suite(suite, measure)
            lines += report_summary(suite, runs, builds)
            if suite is CURATED:
                lines += report_cases(suite, runs)
    except subprocess.CalledProcessError as e:
        print(f"error: pagefind failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except PlaywrightError as e:
        print(f"error: browser run failed: {e}", file=sys.stderr)
        return 1
    text = "\n".join(lines)
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    if args.output:
        args.output.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
