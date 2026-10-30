"""Compare how well Pagefind finds Korean words with and without supplementary indexing.

Usage: uv run python scripts/check_pagefind.py [--output report.md]
Needs the dev dependencies and `uv run playwright install chromium`.

Variants:
  none    plain Pagefind
  a       index Hangul bigrams of every word
  b       variant a's index, and the query is split into bigrams too
  c       index every suffix of Hangul words and of adjacent words joined together
  d       variant b, plus bigrams of adjacent words joined together
"""

import argparse
import html
import re
import subprocess
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

SAMPLES = {
    "s01": "이번 주 회의 일정 정리합니다",
    "s02": "회의를 목요일로 옮기면 어떨까요?",
    "s03": "목요일 좋아요. 회의실은 제가 잡을게요",
    "s04": "회의실 예약 완료했습니다",
    "s05": "지난주 회의록 올립니다 1. 1주차 범위 확정 2. 테스트 워크스페이스 준비",
    "s06": "회의록 감사합니다!",
    "s07": "다음주회의 안건 있으면 스레드에 남겨주세요",
    "s08": "운영 회의는 매월 첫째 주 월요일입니다",
    "s09": "스터디 3회차 발표자료 공유합니다",
    "s10": "발표 자료 잘 봤습니다",
    "s11": "발표자료 PDF 버전도 있을까요?",
    "s12": "검색 테스트: 데이터베이스, 데이터 베이스, DB",
    "s13": "검색 테스트: 아카이브, 아카이빙, 아카이브를",
    "s14": "띄어쓰기없이길게쓴문장도검색이되는지확인합니다",
    "s15": "English and 한국어 mixed message for search test",
    "s16": "오늘 스터디에서 배운 것: 증분 수집, 커서 페이지네이션",
    "s17": "비공개 채널 메시지도 수집되는지 확인용",
}

MEETING = {"s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08"}
SLIDES = {"s09", "s10", "s11"}

# (label, query, ids a user would expect to find)
CASES = [
    ("base word", "회의", MEETING),
    ("with particle", "회의를", {"s02"}),
    ("compound", "회의록", {"s05", "s06"}),
    ("compound", "회의실", {"s03", "s04"}),
    ("glued compound", "다음주회의", {"s07"}),
    ("no space", "발표자료", SLIDES),
    ("with space", "발표 자료", SLIDES),
    ("long word", "데이터베이스", {"s12"}),
    ("word prefix", "데이터", {"s12"}),
    ("inflection", "아카이브", {"s13"}),
    ("inflection", "아카이빙", {"s13"}),
    ("inside spaceless text", "검색", {"s12", "s13", "s14"}),
    ("verb stem", "수집", {"s16", "s17"}),
    ("inside spaceless text", "띄어쓰기", {"s14"}),
    ("latin", "english", {"s15"}),
    ("latin", "DB", {"s12"}),
    ("NFD input", unicodedata.normalize("NFD", "회의"), MEETING),
    ("mid-word, 3 chars", "주회의", {"s07"}),
    ("word end", "자료", SLIDES),
    ("mid spaceless, 3 chars", "되는지", {"s14", "s17"}),
    ("absent compound", "주간회의록", set()),
]

TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")
HANGUL = re.compile(r"[가-힣]")
MAX_TOKEN = 12
PIECE_WEIGHT = "0.1"


def tokens(text: str) -> list[str]:
    return TOKEN.findall(unicodedata.normalize("NFC", text))


def bigrams(word: str) -> list[str]:
    return [word[i : i + 2] for i in range(len(word) - 1)]


def suffixes(word: str) -> list[str]:
    word = word[:MAX_TOKEN]
    return [word[i:] for i in range(1, len(word))]


def bigram_pieces(text: str) -> set[str]:
    return {p for t in tokens(text) if HANGUL.search(t) for p in bigrams(t)}


def suffix_pieces(text: str) -> set[str]:
    words = tokens(text)
    pieces: set[str] = set()
    for word in words:
        if HANGUL.search(word):
            pieces.update(suffixes(word))
    for left, right in zip(words, words[1:], strict=False):
        if HANGUL.search(left) and HANGUL.search(right):
            joined = left + right
            pieces.add(joined)
            pieces.update(suffixes(joined))
    return pieces


def joined_bigram_pieces(text: str) -> set[str]:
    words = tokens(text)
    pieces = bigram_pieces(text)
    for left, right in zip(words, words[1:], strict=False):
        if HANGUL.search(left) and HANGUL.search(right):
            pieces.update(bigrams(left + right))
    return pieces


def bigram_query(query: str) -> str:
    out = []
    for word in tokens(query):
        out.extend(bigrams(word) if HANGUL.search(word) and len(word) > 2 else [word])
    return " ".join(out)


@dataclass(frozen=True)
class Variant:
    name: str
    site: str
    pieces: Callable[[str], set[str]] | None
    query: Callable[[str], str] = lambda q: q


VARIANTS = [
    Variant("none", "none", None),
    Variant("a: bigram", "bigram", bigram_pieces),
    Variant("b: bigram + query", "bigram", bigram_pieces, bigram_query),
    Variant("c: suffix + joined", "suffix", suffix_pieces),
    Variant("d: b + joined bigram", "joined", joined_bigram_pieces, bigram_query),
]

SEARCH_JS = """
async ([site, query]) => {
  const pf = await import(`/${site}/pagefind/pagefind.js`);
  const search = await pf.search(query);
  const data = await Promise.all(search.results.map((r) => r.data()));
  return data.map((d) => d.meta.id);
}
"""


def build_site(root: Path, pieces: Callable[[str], set[str]] | None) -> int:
    root.mkdir(parents=True)
    root.joinpath("index.html").write_text(
        '<!doctype html><html lang="ko"><meta charset="utf-8"><title>check</title></html>',
        encoding="utf-8",
    )
    for sid, text in SAMPLES.items():
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
    # The extended binary is the one with CJK segmentation
    subprocess.run(
        [sys.executable, "-m", "pagefind", "--site", str(root)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return sum(p.stat().st_size for p in root.joinpath("pagefind").rglob("*") if p.is_file())


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


@contextmanager
def serve(root: Path) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_searches(url: str) -> dict[str, list[set[str]]]:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url + "none/index.html")
            return {
                v.name: [set(page.evaluate(SEARCH_JS, [v.site, v.query(q)])) for _, q, _ in CASES]
                for v in VARIANTS
            }
        finally:
            browser.close()


def report(results: dict[str, list[set[str]]], sizes: dict[str, int]) -> str:
    names = [v.name for v in VARIANTS]
    rows = [
        "| Case | Query | " + " | ".join(names) + " |",
        "| --- | --- | " + " | ".join("---" for _ in names) + " |",
    ]
    totals = {n: [0, 0, 0] for n in names}  # hits, expected, unexpected
    for i, (label, query, expected) in enumerate(CASES):
        cells = []
        for name in names:
            found = results[name][i]
            hit, extra = len(expected & found), len(found - expected)
            totals[name][0] += hit
            totals[name][1] += len(expected)
            totals[name][2] += extra
            cells.append(f"{hit}/{len(expected)}" + (f" (+{extra})" if extra else ""))
        shown = query if unicodedata.is_normalized("NFC", query) else f"{query} (NFD)"
        rows.append(f"| {label} | {shown} | " + " | ".join(cells) + " |")
    rows.append(
        "| **recall** | | " + " | ".join(f"**{h}/{e}**" for h, e, _ in totals.values()) + " |"
    )
    rows.append("| **unexpected** | | " + " | ".join(f"{x}" for *_, x in totals.values()) + " |")
    size_cells = [f"{sizes[v.site] / 1024:.1f} KB" for v in VARIANTS]
    rows.append("| **index size** | | " + " | ".join(size_cells) + " |")
    rows.append("")
    rows.append("`(+n)` = n results that were not expected.")
    return "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, help="Also write the report to this file.")
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sizes = {
                v.site: build_site(root / v.site, v.pieces)
                for v in {v.site: v for v in VARIANTS}.values()
            }
            with serve(root) as url:
                results = run_searches(url)
    except subprocess.CalledProcessError as e:
        print(f"error: pagefind failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except PlaywrightError as e:
        print(f"error: browser run failed: {e}", file=sys.stderr)
        return 1
    text = report(results, sizes)
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    if args.output:
        args.output.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
