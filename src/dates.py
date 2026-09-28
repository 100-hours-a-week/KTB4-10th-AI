"""
src/dates.py

날짜 형식은 셋이고, 문자열↔날짜 변환은 여기서만 한다.

    백엔드    26.10.12      요청·응답에 오가는 형식 (계약서 2·7장)
    TourAPI   20261012      행사 기간이 여행 기간과 겹치는지 비교하는 형식
    ISO       2026-10-12    안쪽에서 들고 다니는 형식

모아 둔 이유: 같은 변환이 nodes·adapter·llm_plan·guidebook_html 네 곳에 따로 있었고,
llm_plan 은 순환 import 를 피하려고 형식 문자열을 베껴 갖고 있었다.

**화면에 보이는 모양(`10/12`·`2026.10.12`)은 여기 없다.** 그건 형식 변환이 아니라
표시 방식이고, 바뀌는 이유도 다르다 (output/book_html.py 의 일이다).

틀린 값은 ValueError 로 올린다. 무엇으로 바꿀지는 부르는 쪽이 정한다 —
접수는 400 으로 거절하고, 화면은 원문을 그대로 찍는다.
"""

from __future__ import annotations

from datetime import date, datetime

BACKEND_FORMAT = "%y.%m.%d"
TOURAPI_FORMAT = "%Y%m%d"
ISO_FORMAT = "%Y-%m-%d"


def parse_backend(value: str) -> date:
    """'26.10.12' -> date(2026, 10, 12)."""
    return datetime.strptime(value, BACKEND_FORMAT).date()


def parse_iso(value: str) -> date:
    """'2026-10-12' -> date(2026, 10, 12)."""
    return datetime.strptime(value, ISO_FORMAT).date()


def to_backend(day: date) -> str:
    """date(2026, 10, 12) -> '26.10.12'."""
    return day.strftime(BACKEND_FORMAT)


def to_tourapi(day: date) -> str:
    """date(2026, 10, 12) -> '20261012'."""
    return day.strftime(TOURAPI_FORMAT)


# ISO 로 만드는 함수는 두지 않았다 — date.isoformat() 이 이미 그것이다.
