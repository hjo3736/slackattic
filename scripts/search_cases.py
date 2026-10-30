"""Test corpora for check_pagefind.py: hand-picked hard cases and a generated large corpus."""

import random
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    label: str
    query: str
    # Results that must be found
    must: frozenset[str]
    # Results that must not be found (precision traps); anything else found counts as "other"
    wrong: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Suite:
    name: str
    samples: dict[str, str]
    cases: list[Case]


def _case(label: str, query: str, must: set[str], wrong: set[str] | None = None) -> Case:
    return Case(label, query, frozenset(must), frozenset(wrong or ()))


CURATED_SAMPLES = {
    "c01": "회의는 몇 시에 시작하나요?",
    "c02": "오늘 회의가 길어졌네요",
    "c03": "회의에서 나온 이야기 정리할게요",
    "c04": "온라인 회의로 대체합시다",
    "c05": "회의까지 자료 준비 부탁해요",
    "c06": "다음주회의 안건 받습니다",
    "c07": "이번 주 회의 취소됐어요",
    "c08": "회의록은 노션에 있어요",
    "c09": "회의실 예약했어요",
    "c10": "회의를 미루자는 의견이 있어요",
    "c11": "사회의 변화에 대한 글 공유합니다",
    "c12": "국회의원 선거 이야기는 그만",
    "c13": "기회의 땅이라고들 하죠",
    "c14": "의회 민주주의 책 추천",
    "c15": "오늘 회식 장소 어디예요?",
    "c16": "발표자료 올렸습니다",
    "c17": "발표 자료 확인 부탁드려요",
    "c18": "발표 준비 자료는 내일 공유할게요",
    "c19": "코드리뷰 요청드립니다",
    "c20": "코드 리뷰 끝났어요",
    "c21": "PR리뷰 부탁해요",
    "c22": "API문서 업데이트했습니다",
    "c23": "API 문서 링크 공유",
    "c24": "데이터베이스 마이그레이션 완료",
    "c25": "데이터 베이스 백업 확인",
    "c26": "빅데이터 강의 추천해요",
    "c27": "자료 정리했어요",
    "c28": "내일까지 정리합니다",
    "c29": "정리해 주세요",
    "c30": "깔끔하게정리완료",
    "c31": "3회차 스터디 공지",
    "c32": "스터디 3회차 후기",
    "c33": "11월 2일 10시에 만나요",
    "c34": "ㅋㅋㅋ 너무 웃기네요",
    "c35": "ㅇㅋ 알겠습니다",
    "c36": "회의ㄱㄱ",
    "c37": "회의🔥 시작합니다",
    "c38": "(회의) 일정 변경 안내",
    "c39": "회의!!! 지금 바로요",
    "c40": "스터디그룹 모집합니다",
    "c41": "스터디 그룹 단톡방 링크",
    "c42": "신입회원 환영합니다",
    "c43": "신입 회원 소개 부탁드려요",
    "c44": (
        "지난 분기 활동을 돌아보면 여러 가지 일이 있었습니다. 처음에는 인원이 적었지만 "
        "점점 늘어났고, 마지막에는 정기모임을 매주 진행하게 되었습니다. "
        "앞으로도 꾸준히 이어가면 좋겠습니다."
    ),
    "c45": "정기 모임 장소 공지",
}

_MEETING = {f"c{i:02d}" for i in (*range(1, 11), 36, 37, 38, 39)}
_MEETING_TRAPS = {"c11", "c12", "c13", "c14", "c15"}

CURATED = Suite(
    "curated",
    CURATED_SAMPLES,
    [
        _case("base word", "회의", _MEETING, _MEETING_TRAPS),
        _case("particle query", "회의를", {"c10"}, _MEETING_TRAPS),
        _case("particle query", "회의가", {"c02"}, _MEETING_TRAPS),
        _case("compound", "회의록", {"c08"}),
        _case("mid-word", "주회의", {"c06", "c07"}),
        _case("no space", "발표자료", {"c16", "c17"}),
        _case("with space", "발표 자료", {"c16", "c17"}),
        _case("compound", "코드리뷰", {"c19", "c20"}),
        _case("word end", "리뷰", {"c19", "c20", "c21"}),
        _case("Korean + latin", "PR리뷰", {"c21"}),
        _case("Korean + latin", "API문서", {"c22", "c23"}),
        _case("Korean + latin, spaced", "API 문서", {"c22", "c23"}),
        _case("loanword", "데이터베이스", {"c24", "c25"}),
        _case("inside word", "데이터", {"c24", "c25", "c26"}),
        _case("verb stem", "정리", {"c03", "c27", "c28", "c29", "c30"}),
        _case("conjugated", "정리했어요", {"c27"}),
        _case("number + unit", "3회차", {"c31", "c32"}),
        _case("number + unit", "10시", {"c33"}),
        _case("jamo only", "ㅋㅋ", {"c34"}),
        _case("jamo only", "ㅇㅋ", {"c35"}),
        _case("jamo suffix", "회의ㄱㄱ", {"c36"}),
        _case("compound", "스터디그룹", {"c40", "c41"}),
        _case("with space", "스터디 그룹", {"c40", "c41"}),
        _case("word prefix", "스터디", {"c31", "c32", "c40", "c41"}),
        _case("compound", "신입회원", {"c42", "c43"}),
        _case("in long text", "정기모임", {"c44", "c45"}),
        _case("in long text", "분기", {"c44"}),
        _case("NFD input", unicodedata.normalize("NFD", "회의"), _MEETING, _MEETING_TRAPS),
        _case("trap word itself", "국회", {"c12"}),
        _case("trap word itself", "회식", {"c15"}),
        _case("absent compound", "주간회의록", set(), {"c08"}),
    ],
)


# Target words for the generated corpus: (glued form, spaced form or None)
_WORDS = [
    ("발표자료", "발표 자료"),
    ("코드리뷰", "코드 리뷰"),
    ("데이터베이스", "데이터 베이스"),
    ("스터디그룹", "스터디 그룹"),
    ("신입회원", "신입 회원"),
    ("정기모임", "정기 모임"),
    ("배포일정", "배포 일정"),
    ("체크리스트", "체크 리스트"),
    ("회의록", None),
    ("아카이브", None),
]
_TEMPLATES = [
    "{x} 다음 주에 다시 이야기해요",
    "어제 {x} 정리했어요",
    "혹시 {x} 어디서 볼 수 있을까요?",
    "{x} 관련해서 질문 있습니다",
    "오늘 저녁에 {x} 확인 부탁드려요",
    "{x} 공유드립니다 🙏",
    "이번 달 {x} 링크 남겨요",
]
_FILLERS = [
    "점심 뭐 먹을까요?",
    "좋아요 👍",
    "오늘 날씨 좋네요",
    "늦어서 죄송합니다",
    "확인했습니다",
    "내일 봬요",
    "감사합니다!",
    "링크 다시 보내 주실 수 있나요?",
    "ㅋㅋㅋㅋ",
    "다들 수고 많으셨어요",
]


def _has_final(word: str) -> bool:
    last = word[-1]
    return "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0


def _particle(word: str, rng: random.Random) -> str:
    final = _has_final(word)
    options = [
        "",
        "은" if final else "는",
        "이" if final else "가",
        "을" if final else "를",
        "에서",
        "으로" if final else "로",
        "까지",
        "도",
    ]
    return rng.choice(options)


def generated(size: int, seed: int = 42) -> Suite:
    """Build a corpus whose expected results are known by construction."""
    rng = random.Random(seed)
    samples: dict[str, str] = {}
    word_of: dict[str, str] = {}
    for i in range(size):
        sid = f"g{i:05d}"
        if rng.random() < 0.6:
            glued, spaced = rng.choice(_WORDS)
            form = spaced if spaced and rng.random() < 0.3 else glued
            text = rng.choice(_TEMPLATES).format(x=form + _particle(form, rng))
            word_of[sid] = glued
        else:
            text = " ".join(rng.sample(_FILLERS, rng.randint(1, 2)))
        samples[sid] = text

    cases = []
    for glued, spaced in _WORDS:
        queries = [("glued", glued)]
        if spaced:
            queries += [("spaced", spaced), ("second part", spaced.split()[1])]
        for label, query in queries:
            needle = query.replace(" ", "")
            must = {sid for sid, w in word_of.items() if needle in w}
            cases.append(_case(label, query, must))
    return Suite(f"generated-{size}", samples, cases)
