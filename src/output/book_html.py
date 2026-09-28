"""
src/output/book_html.py

일정 하나를 **옆으로 넘기는 가이드북** HTML 한 장으로 만든다. 팀원 렌더러
(imsi/scripts/model/assemble.py 의 render_book)의 쪽 구성·색·CSS·넘김 스크립트를 옮겼다
(2026-09-28). 색은 팀 피그마(차콜 #222 · 레드 #FF383C · 배경 #EFEFF0)다.

    표지 → 여행 정보·목차 → 한눈에 보기 → (일차 챕터 → 장소 한 곳씩) × N → 행사 → 뒷표지

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

from src.engine import data
from src.models import Itinerary, ItineraryDay, MissedEvent, PlannedPlace, TripRequest

# 행사 쪽 한 장에 들어가는 수. 넘치면 "외 N건" 으로 접는다
MAX_EVENTS_ON_PAGE = 8

PRETENDARD = (
    "https://cdn.jsdelivr.net/npm/pretendard@1.3.9/dist/web/static/pretendard.min.css"
)


def render(request: TripRequest, itinerary: Itinerary) -> str:
    """일정 하나 -> HTML 문서 한 장."""
    # (클래스, 안쪽 내용, 배경 사진). 껍데기와 쪽 번호는 _page 가 한 곳에서 붙인다
    contents = [
        ("cover", _cover(request, itinerary), itinerary.cover_image_url),
        ("info", _info(request, itinerary), None),
        ("glance", _glance(itinerary), None),
    ]
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


def _info(request: TripRequest, itinerary: Itinerary) -> str:
    """여행 정보와 목차."""
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

    toc = "".join(_toc_row(day) for day in itinerary.itinerary)
    if itinerary.unscheduled_events:
        count = len(itinerary.unscheduled_events)
        toc += (
            '<li><span class="d">EVENT</span><span class="t">여행 기간 중 열리는 행사</span>'
            f'<span class="n">{count}건</span></li>'
        )

    return (
        '<div class="pad"><div class="eyebrow">Trip Info</div><h2>이 가이드북의 여행</h2>'
        f'<dl class="facts">{fact_html}</dl>'
        f'<div class="eyebrow">Contents</div><ul class="toc">{toc}</ul></div>'
    )


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
    """한눈에 보기 — 일차마다 시각과 장소를 잇는다."""
    rows = []
    for day in itinerary.itinerary:
        stops = "<i>→</i>".join(
            f"<span>{_text(place.start_time)} {_text(place.name)}</span>"
            for place in day.places
        )
        rows.append(
            f'<div class="gday"><div class="h">DAY {day.day}'
            f"<small>{_text(day.date)}</small></div>"
            f'<div class="chain">{stops}</div></div>'
        )

    day_count = itinerary.summary.day_count
    return (
        '<div class="pad"><div class="eyebrow">At a Glance</div>'
        f'<h2>한눈에 보는 {day_count}일</h2><div class="glance">{"".join(rows)}</div></div>'
    )


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
    category = place.category_path or place.category_name
    event = ""
    if place.event_end_date:
        event = f'<div class="info">행사 · {_month_day(place.event_end_date)}까지</div>'

    return (
        f'{photo}<div class="pad"><span class="badge">DAY {day.day} · {_text(when)}</span>'
        f"<h3>{_text(place.name)}</h3>"
        f'<div class="info">{_text(category)}</div>'
        f'<div class="info">{_text(place.address)}</div>{event}'
        f'<div class="why"><b>WHY HERE</b>{_text(place.recommend_reason)}</div></div>'
    )


def _events(events: list[MissedEvent]) -> str:
    """일정에 못 넣은 행사. 대개 하루 행사 1건 상한 때문이다."""
    rows = "".join(
        f'<div class="ev"><div class="th"{_bg(event.image_url)}></div><div>'
        f'<div class="nm">{_text(event.name)}</div>'
        f'<div class="sm">{_event_period(event.start_date, event.end_date)}</div>'
        f'<div class="sm">{_text(event.address)}</div></div></div>'
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
# ============================================================

CSS = """
:root {
  --ink:#222222;
  --sub:#61646B;
  --faint:#AFB1B6;
  --bg:#EFEFF0;
  --paper:#FFFFFF;
  --accent:#FF383C;
  --soft:#F1F1F3;
}
* { box-sizing:border-box; }
body {
  margin:0;
  background:var(--bg);
  color:var(--ink);
  font-family:"Pretendard",-apple-system,"Apple SD Gothic Neo",sans-serif;
  line-height:1.65;
}
/* 화면: 책처럼 한 쪽씩 옆으로 넘긴다 (스와이프·화살표 키·아래 버튼) */
.book {
  height:100vh;
  display:flex;
  align-items:flex-start;
  gap:24px;
  overflow-x:auto;
  overflow-y:hidden;
  scroll-snap-type:x mandatory;
  scrollbar-width:none;
  padding:24px max(16px, calc(50vw - 260px)) 80px;
}
.book::-webkit-scrollbar { display:none; }
/* 모든 쪽이 같은 크기다 — 표지 비율(3:4.3)로, 화면 높이를 넘지 않게.
   내용이 넘치면 그 쪽 안에서 스크롤 */
.book {
  --pw:min(520px, calc(100vw - 32px));
  --ph:min(calc(100vh - 104px), calc(var(--pw) * 4.3 / 3));
}
.page {
  flex:none;
  width:var(--pw);
  height:var(--ph);
  min-width:0;
  scroll-snap-align:center;
  scroll-snap-stop:always;
  overflow-y:auto;
  background:var(--paper);
  border-radius:6px;
  box-shadow:0 2px 14px rgba(0,0,0,.08);
  position:relative;
}
.nav {
  position:fixed;
  left:0;
  right:0;
  bottom:20px;
  display:flex;
  justify-content:center;
  align-items:center;
  gap:16px;
}
.nav button {
  width:40px;
  height:40px;
  border-radius:50%;
  border:0;
  background:var(--ink);
  color:#fff;
  font-size:18px;
  cursor:pointer;
}
.nav button:disabled { opacity:.25; cursor:default; }
.nav span { font-size:13px; color:var(--sub); min-width:56px; text-align:center; }
.pad { padding:32px 28px; }
.pno { position:absolute; bottom:12px; right:18px; font-size:11px; color:var(--faint); }
.eyebrow {
  font-size:11px;
  letter-spacing:.18em;
  color:var(--accent);
  font-weight:700;
  text-transform:uppercase;
}
h2 { font-size:24px; margin:6px 0 18px; line-height:1.3; }

/* 표지 */
.cover {
  aspect-ratio:3/4.3;
  color:#fff;
  background:#222 center/cover no-repeat;
  display:flex;
  flex-direction:column;
  justify-content:space-between;
}
.cover::before {
  content:"";
  position:absolute;
  inset:0;
  background:linear-gradient(180deg,rgba(0,0,0,.62) 0%,rgba(0,0,0,.05) 45%,rgba(0,0,0,.65) 100%);
}
.cover > * { position:relative; }
.cover .top { padding:28px 26px 0; }
.cover h1 { font-size:44px; line-height:1.08; margin:0; font-weight:900; letter-spacing:-.02em; }
.cover .subtitle { margin-top:10px; font-size:16px; opacity:.92; }
.tags { display:flex; flex-wrap:wrap; gap:6px; margin-top:16px; }
.tags span {
  font-size:12px;
  padding:3px 10px;
  border-radius:999px;
  background:rgba(255,255,255,.18);
  border:1px solid rgba(255,255,255,.35);
}
.cover .bottom { padding:0 26px 24px; display:flex; align-items:center; gap:12px; }
.cover .big { font-size:64px; font-weight:900; line-height:1; }
.cover .bottom .lbl { font-size:14px; font-weight:800; line-height:1.2; letter-spacing:.04em; }

/* 정보·목차 */
.facts {
  display:grid;
  grid-template-columns:auto 1fr;
  gap:8px 16px;
  font-size:14px;
  margin-bottom:28px;
}
.facts dt { color:var(--sub); } .facts dd { margin:0; font-weight:600; }
.toc { list-style:none; padding:0; margin:0; border-top:2px solid var(--ink); }
.toc li {
  display:flex;
  justify-content:space-between;
  gap:12px;
  padding:12px 0;
  border-bottom:1px solid var(--soft);
  font-size:15px;
}
.toc .d { font-weight:800; color:var(--accent); min-width:56px; }
.toc .t { flex:1; } .toc .n { color:var(--faint); font-size:13px; }

/* 한눈에 보기 */
.glance { display:flex; flex-direction:column; gap:18px; }
.gday .h {
  font-weight:800;
  font-size:15px;
}
.gday .h small {
  color:var(--sub);
  font-weight:500;
  margin-left:6px;
}
.chain {
  display:flex;
  flex-wrap:wrap;
  align-items:center;
  gap:6px;
  margin-top:6px;
  font-size:13px;
}
.chain span { background:var(--soft); padding:3px 10px; border-radius:999px; }
.chain i { color:var(--faint); font-style:normal; }

/* 챕터 */
.chapter .hero { aspect-ratio:4/3; background:#ccc center/cover; position:relative; }
.chapter .hero .day {
  position:absolute;
  left:20px;
  bottom:16px;
  color:#fff;
  font-size:56px;
  font-weight:900;
  line-height:1;
  text-shadow:0 2px 12px rgba(0,0,0,.4);
}
.chapter .hero .day small { display:block; font-size:14px; font-weight:600; letter-spacing:.06em; }
.summary { font-size:15px; color:#333; }
.route {
  margin-top:18px;
  font-size:12px;
  color:var(--sub);
  border-top:1px solid var(--soft);
  padding-top:12px;
}

/* 장소 */
.place .photo { aspect-ratio:16/10; background:var(--soft) center/cover; }
.place .noimg {
  display:flex;
  align-items:center;
  justify-content:center;
  color:var(--faint);
  font-size:13px;
}
.badge {
  display:inline-block;
  background:var(--accent);
  color:#fff;
  font-size:12px;
  font-weight:700;
  padding:2px 10px;
  border-radius:4px;
}
.place h3 { font-size:22px; margin:10px 0 4px; }
.place .info { font-size:13px; color:var(--sub); }
.why { margin-top:16px; padding-left:14px; border-left:3px solid var(--accent); font-size:15px; }
.why b {
  display:block;
  font-size:11px;
  letter-spacing:.14em;
  color:var(--accent);
  margin-bottom:2px;
}

/* 행사 */
.ev { display:flex; gap:14px; padding:14px 0; border-bottom:1px solid var(--soft); }
.ev .th { flex:0 0 84px; height:84px; border-radius:6px; background:var(--soft) center/cover; }
.ev .nm { font-weight:700; font-size:15px; } .ev .sm { font-size:12px; color:var(--sub); }
.note { font-size:12px; color:var(--faint); margin-top:14px; }

/* 뒷표지 */
.back {
  background:var(--ink);
  color:#ddd;
  text-align:center;
  display:flex;
  flex-direction:column;
  justify-content:center;
}
.back .logo { font-size:40px; font-weight:900; color:#fff; letter-spacing:.06em; }
.back p { font-size:12px; line-height:1.8; margin:6px 0; }

/* PDF(A4). 하루가 A4 한 장 — 챕터(사진·요약)와 그날 장소들을 한 쪽에 모은다.
   화면의 "장소 한 곳 = 한 쪽"을 그대로 인쇄하면 사진이 A4 폭으로 커져 쪽마다 절반이 빈다 */
@media print {
  @page { size:A4; margin:10mm 12mm; }
  * { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  body { background:#fff; font-size:13px; }
  .book { max-width:none; padding:0; gap:0; display:block; height:auto; overflow:visible; }
  .page { box-shadow:none; border-radius:0; overflow:visible; width:auto; height:auto; }
  .nav { display:none; }
  .pad { padding:12px 4px; }
  .pno { display:none; }
  .page.cover, .page.back { break-before:page; height:277mm; aspect-ratio:auto; border-radius:0; }
  .page.cover { break-before:auto; }
  .page.back { display:flex; align-items:center; justify-content:center; }
  .page.info, .page.events { break-before:page; }
  .page.glance { margin-top:10mm; }
  .page.chapter { break-before:page; }
  .chapter .hero { aspect-ratio:auto; height:48mm; border-radius:6px; }
  .chapter .hero .day { font-size:40px; }
  .summary { font-size:13px; }
  .route { margin-top:10px; }
  .place {
    display:flex;
    border:1px solid var(--soft);
    border-radius:6px;
    margin-bottom:3mm;
    break-inside:avoid;
    overflow:hidden;
  }
  .place .photo { flex:0 0 46mm; aspect-ratio:auto; min-height:34mm; }
  .place .pad { padding:8px 12px; }
  .place h3 { font-size:16px; margin:4px 0 0; }
  .place .info { font-size:12px; }
  .why { margin-top:6px; font-size:12.5px; }
  .ev { padding:8px 0; }
  .ev .th { flex-basis:64px; height:64px; }
}
"""

# 쪽 넘기기 버튼·키보드. 인쇄(PDF)에서는 숨긴다
NAV = """<nav class="nav">
<button id="prev" aria-label="이전 쪽">‹</button>
<span id="pos"></span>
<button id="next" aria-label="다음 쪽">›</button>
</nav>
<script>
const book = document.querySelector(".book");
const pages = [...book.querySelectorAll(".page")];
const pos = document.getElementById("pos");
const prev = document.getElementById("prev");
const next = document.getElementById("next");
let cur = 0;

const go = (i) => {
  const target = pages[Math.max(0, Math.min(pages.length - 1, i))];
  target.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
};
const show = () => {
  pos.textContent = `${cur + 1} / ${pages.length}`;
  prev.disabled = cur === 0;
  next.disabled = cur === pages.length - 1;
};
// 화면 가운데에 가장 가까운 쪽이 지금 쪽이다
const centerOf = (p) => p.offsetLeft + p.offsetWidth / 2;
book.addEventListener("scroll", () => {
  const mid = book.scrollLeft + book.clientWidth / 2;
  const nearer = (best, p, k) =>
    Math.abs(centerOf(p) - mid) < Math.abs(centerOf(pages[best]) - mid) ? k : best;
  const i = pages.reduce(nearer, 0);
  if (i !== cur) { cur = i; show(); }
}, { passive: true });

prev.onclick = () => go(cur - 1);
next.onclick = () => go(cur + 1);
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight") go(cur + 1);
  if (e.key === "ArrowLeft") go(cur - 1);
});
show();
</script>"""
