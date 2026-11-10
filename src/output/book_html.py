"""
src/output/book_html.py

일정 하나를 **옆으로 넘기는 가이드북** HTML 한 장으로 만든다. 팀원 렌더러
(imsi/scripts/model/assemble.py 의 render_book)의 쪽 구성·색·CSS·넘김 스크립트를 옮겼다
(2026-09-28). 색은 팀 피그마(차콜 #222 · 레드 #FF383C · 배경 #EFEFF0)다.

    표지 → 여행 정보·목차 → 한눈에 보기 → (일차 챕터 → 장소 한 곳씩) × N → 행사 → 뒷표지
    (3일 이하는 목차 대신 한눈에 보기를 여행 정보 쪽에 넣어 두 쪽을 하나로)

팀원 것과 다른 점 — 우리가 먼저 정한 것을 따랐다:
- **직선거리를 싣지 않는다.** 실제 이동거리가 아니라 오해를 부른다 (2026-09-26 결정).
- 표지 사진은 build 가 고른 것(cover_image_url)을 쓴다. 고르는 규칙은 nodes._pick_cover 에 있다.
- 행사 쪽에는 **일정에 못 넣은** 행사만 싣는다. 넣은 행사는 이미 장소 쪽에 있다.

서버 content_html 과 CLI --html 이 이것을 쓴다.
이전 렌더러(tools/guidebook_html.py)는 비교용으로 남겼다.
바깥에서 부르는 것은 `render` 하나뿐이다.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from src.engine import data
from src.models import Itinerary, ItineraryDay, MissedEvent, PlannedPlace, TripRequest

# 행사 쪽 한 장에 들어가는 수. 넘치면 "외 N건" 으로 접는다
MAX_EVENTS_ON_PAGE = 8

# book.css · book.js 가 이 파일 옆에 있다
HERE = Path(__file__).parent

PRETENDARD = (
    "https://cdn.jsdelivr.net/npm/pretendard@1.3.9/dist/web/static/pretendard.min.css"
)


def render(request: TripRequest, itinerary: Itinerary) -> str:
    """일정 하나 -> HTML 문서 한 장."""
    # (클래스, 안쪽 내용, 배경 사진). 껍데기와 쪽 번호는 _page 가 한 곳에서 붙인다
    contents = [("cover", _cover(request, itinerary), itinerary.cover_image_url)]
    # 3일 이하는 한눈에 보기가 짧아 목차 자리에 넣고 쪽 하나를 줄인다
    day_count = itinerary.summary.day_count
    if day_count <= 3:
        small_title = f'<div class="gtitle">한눈에 보는 {day_count}일</div>'
        glance_part = small_title + _glance_rows(itinerary)
        contents.append(("info merged", _info(request, itinerary, glance_part), None))
    else:
        contents.append(("info", _info(request, itinerary, _toc(itinerary)), None))
        contents.append(("glance", _glance(itinerary), None))
    for day in itinerary.itinerary:
        contents.append(("chapter", _chapter(day), None))
        for place in day.places:
            contents.append(("place", _place(day, place), None))
    if itinerary.unscheduled_events:
        contents.append(("events", _events(itinerary.unscheduled_events), None))
    contents.append(("back", _back(), None))

    pages = [
        _page(cls, inner, number, background)
        for number, (cls, inner, background) in enumerate(contents, start=1)
    ]

    title = _text(itinerary.title or "가이드북")
    return (
        '<!DOCTYPE html>\n<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<link rel="stylesheet" href="{PRETENDARD}">'
        f"<title>{title}</title><style>{CSS}</style></head>"
        f'<body><main class="book">{"".join(pages)}</main>{NAV}</body></html>\n'
    )


def _page(cls: str, inner: str, number: int, background: str | None) -> str:
    """쪽 껍데기. 표지·뒷표지에는 쪽 번호를 찍지 않는다."""
    number_html = "" if cls in ("cover", "back") else f'<div class="pno">{number}</div>'
    return (
        f'<section class="page {cls}"{_bg(background)}>{inner}{number_html}</section>'
    )


# ============================================================
# 쪽마다
# ============================================================


def _cover(request: TripRequest, itinerary: Itinerary) -> str:
    summary = itinerary.summary
    region = _short_region(itinerary)
    period = _period(summary.start_date, summary.end_date)
    companion = data.companion_label(request.companion)
    subtitle = f"{companion} 떠나는 {period}의 {region}"
    tags = "".join(f"<span>{_text(label)}</span>" for label in _interests(request))

    return (
        f'<div class="top"><h1>{_text(itinerary.title)}</h1>'
        f'<div class="subtitle">{_text(subtitle)}</div>'
        f'<div class="tags">{tags}</div></div>'
        f'<div class="bottom"><div class="big">{summary.day_count}</div>'
        f'<div class="lbl">DAYS IN<br>{_text(region)}</div></div>'
    )


def _info(request: TripRequest, itinerary: Itinerary, lower: str) -> str:
    """여행 정보. 아래에는 목차(4일 이상)나 한눈에 보기(3일 이하)가 들어간다."""
    summary = itinerary.summary
    facts = [
        ("지역", _full_region(itinerary)),
        ("기간", f"{summary.start_date} ~ {summary.end_date} ({summary.day_count}일)"),
        (
            "동행",
            f"{data.companion_label(request.companion)} · {summary.people_count}명",
        ),
        ("여행 방식", ", ".join(_styles(request))),
        ("취향", ", ".join(_interests(request))),
    ]
    fact_html = "".join(
        f"<dt>{label}</dt><dd>{_text(value)}</dd>" for label, value in facts if value
    )

    return (
        '<div class="pad"><div class="eyebrow">Trip Info</div><h2>가이드북 여행 정보</h2>'
        f'<dl class="facts">{fact_html}</dl>{lower}</div>'
    )


def _toc(itinerary: Itinerary) -> str:
    """목차 — 일차마다 한 줄. 못 넣은 행사 줄은 11-10 뺐다 (행사 쪽은 그대로)."""
    rows = "".join(_toc_row(day) for day in itinerary.itinerary)
    return f'<div class="eyebrow">Contents</div><ul class="toc">{rows}</ul>'


def _toc_row(day: ItineraryDay) -> str:
    """목차 한 줄 — 그날 앞의 두 곳. 더 있으면 '외' 를 붙인다."""
    names = " · ".join(place.name for place in day.places[:2])
    more = " 외" if len(day.places) > 2 else ""
    return (
        f'<li><span class="d">DAY {day.day}</span>'
        f'<span class="t">{_text(names)}{more}</span>'
        f'<span class="n">{_month_day(day.date)}</span></li>'
    )


def _glance(itinerary: Itinerary) -> str:
    """한눈에 보기 쪽 (4일 이상)."""
    day_count = itinerary.summary.day_count
    return (
        '<div class="pad"><div class="eyebrow">At a Glance</div>'
        f"<h2>한눈에 보는 {day_count}일</h2>{_glance_rows(itinerary)}</div>"
    )


def _glance_rows(itinerary: Itinerary) -> str:
    """한눈에 보기 일정 — 일차마다 시각과 장소를 잇는다."""
    rows = []
    for day in itinerary.itinerary:
        stops = "<i>→</i>".join(
            f"<span>{_text(place.start_time)} {_text(place.name)}</span>"
            for place in day.places
        )
        rows.append(
            f'<div class="gday"><div class="h">DAY {day.day}'
            f"<small>{_month_day(day.date)}</small></div>"
            f'<div class="chain">{stops}</div></div>'
        )
    return f'<div class="glance">{"".join(rows)}</div>'


def _chapter(day: ItineraryDay) -> str:
    """일차 표지 — 그날 마지막 사진 · 하루 요약 · 동선."""
    with_image = [place.image_url for place in day.places if place.image_url]
    hero = with_image[-1] if with_image else None
    route = " → ".join(place.name for place in day.places)

    return (
        f'<div class="hero"{_bg(hero)}><div class="day">'
        f"<small>{_text(day.date)}</small>DAY {day.day}</div></div>"
        f'<div class="pad"><div class="eyebrow">Day {day.day} Story</div>'
        f'<div class="summary">{_text(day.summary)}</div>'
        f'<div class="route">동선 · {_text(route)}</div></div>'
    )


def _place(day: ItineraryDay, place: PlannedPlace) -> str:
    """장소 한 곳 = 한 쪽."""
    if place.image_url:
        photo = f'<div class="photo"{_bg(place.image_url)}></div>'
    else:
        photo = '<div class="photo noimg">사진 없음</div>'

    when = f"{place.start_time} · {place.duration_minutes}분"
    category = _text(place.category_path or place.category_name)
    if place.event_end_date:
        # 따로 한 줄을 두면 긴 행사 이름(제목 2줄)과 겹쳐 쪽이 넘친다
        category += f" · 행사 {_month_day(place.event_end_date)}까지"

    # vLLM 글쓰기 · overview 없는 곳은 요약이 비어 있다 — 빈 단락을 그리지 않는다
    about = ""
    if place.overview_summary:
        about = f'<div class="about">{_text(place.overview_summary)}</div>'

    return (
        f'{photo}<div class="pad"><span class="badge">DAY {day.day} · {_text(when)}</span>'
        f"<h3>{_text(place.name)}</h3>"
        f'<div class="info">{category}</div>'
        f'<div class="info">{_text(place.address)}</div>'
        f"{about}"
        f'<div class="why"><b>WHY HERE</b>{_text(place.recommend_reason)}</div></div>'
    )


def _events(events: list[MissedEvent]) -> str:
    """일정에 못 넣은 행사. 대개 하루 행사 1건 상한 때문이다."""
    rows = "".join(
        f'<div class="ev"><div class="th"{_bg(event.image_url)}></div><div>'
        f'<div class="nm">{_text(event.name)}</div>'
        f'<div class="sm">{_event_period(event.start_date, event.end_date)}'
        f" · {_text(event.address)}</div></div></div>"
        for event in events[:MAX_EVENTS_ON_PAGE]
    )
    hidden = len(events) - MAX_EVENTS_ON_PAGE
    more = f'<div class="note">외 {hidden}건</div>' if hidden > 0 else ""

    return (
        '<div class="pad"><div class="eyebrow">Events</div><h2>여행 기간 중 열리는 행사</h2>'
        f"{rows}{more}"
        '<div class="note">여행 기간에 열리지만 일정에 넣지 못한 행사입니다. '
        "하루에 행사는 한 건까지 넣습니다.</div></div>"
    )


def _back() -> str:
    return (
        '<div class="pad" style="padding:56px 28px"><div class="logo">KGB</div>'
        "<p>Korean-culture Guide Book</p>"
        '<p style="margin-top:24px">일정·추천 이유·하루 요약은 AI 모델이 작성했습니다</p>'
        "<p>장소·행사 정보와 사진 출처: 한국관광공사 (TourAPI)</p></div>"
    )


# ============================================================
# 작은 도구
# ============================================================


def _text(value: str | None) -> str:
    return escape(value or "")


def _bg(url: str | None) -> str:
    if not url:
        return ""
    # https 페이지에서 http 사진은 혼합 콘텐츠로 막힌다 (FE #167). TourAPI 는 https 로도 열린다
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    return f" style=\"background-image:url('{escape(url)}')\""


def _short_region(itinerary: Itinerary) -> str:
    """표지용 — '중구' 처럼 짧게. 시/도 전체 여행이면 시/도 이름."""
    summary = itinerary.summary
    return summary.city or summary.province


def _full_region(itinerary: Itinerary) -> str:
    summary = itinerary.summary
    if summary.city:
        return f"{summary.province} {summary.city}"
    return summary.province


def _interests(request: TripRequest) -> list[str]:
    return [data.preference_label(code) for code in request.detail_codes]


def _styles(request: TripRequest) -> list[str]:
    return [data.preference_label(code) for code in request.travel_styles]


def _month_day(value: str) -> str:
    """'2026-10-12' 또는 '20261012' -> '10.12'. 형식이 다르면 그대로 둔다."""
    digits = value.replace("-", "")
    if len(digits) != 8 or not digits.isdigit():
        return value
    return f"{int(digits[4:6])}.{int(digits[6:8])}"


def _event_period(start: str, end: str) -> str:
    """
    행사 기간은 연도까지 — '2026.5.6 – 12.31', 해를 넘기면 '2025.12.1 – 2026.1.31'.

    여행 기간(표지)과 달리 행사는 여행 전해에 시작한 것도 섞여 있어, 월.일만 적으면
    언제 시작했는지 알 수 없다.
    """
    start_digits = start.replace("-", "")
    end_digits = end.replace("-", "")
    if not (start_digits[:4].isdigit() and end_digits[:4].isdigit()):
        return _period(start, end)

    start_year = start_digits[:4]
    end_year = end_digits[:4]
    head = f"{start_year}.{_month_day(start)}"
    if start_year != end_year:
        return f"{head} – {end_year}.{_month_day(end)}"
    if start_digits == end_digits:
        return head
    return f"{head} – {_month_day(end)}"


def _period(start: str, end: str) -> str:
    """'10.12 – 10.14'. 하루짜리면 한쪽만."""
    head = _month_day(start)
    tail = _month_day(end)
    if head == tail:
        return head
    return f"{head} – {tail}"


# ============================================================
# 스타일 · 넘김 — 팀원 렌더러에서 옮겼다. 직선거리(.hop) 규칙만 뺐다
# 편집기가 CSS · JS 로 알아보게 파일로 따로 둔다
# ============================================================

CSS = (HERE / "book.css").read_text(encoding="utf-8")
SCRIPT = (HERE / "book.js").read_text(encoding="utf-8")

# 쪽 넘기기 버튼·키보드. 인쇄(PDF)에서는 숨긴다
NAV = (
    '<nav class="nav">\n'
    '<button id="prev" aria-label="이전 쪽">‹</button>\n'
    '<span id="pos"></span>\n'
    '<button id="next" aria-label="다음 쪽">›</button>\n'
    "</nav>\n"
    f"<script>\n{SCRIPT}</script>"
)
