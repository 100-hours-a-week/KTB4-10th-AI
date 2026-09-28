"""
src/engine/nodes.py

그래프의 노드 5개 — prepare · find_places · find_events · plan · build.
전부 "State를 받아 자기가 갱신할 칸만 담은 dict를 돌려준다".
노드가 노드를 직접 부르지 않는다 — 연결은 graph.py가 한다.
"""

from __future__ import annotations

import collections
from datetime import timedelta

from src import dates
from src.engine import data, distance
from src.llm import llm_plan
from src.llm.llm_plan import LLMError
from src.models import (
    Candidate,
    Coordinates,
    GraphState,
    Itinerary,
    ItineraryDay,
    MissedEvent,
    PlanDraftItem,
    PlanItem,
    PlannedPlace,
    TripRequest,
    TripSummary,
)

# 여행스타일이 v0에 영향을 주는 곳은 여기뿐이다.
PER_DAY_BY_STYLE = {"RELAXING": 3, "TIME_EFFICIENCY": 5}
DEFAULT_PER_DAY = 4


# ============================================================
# 1. prepare — 입력을 코드와 숫자로 바꾼다
# ============================================================


def prepare(state: GraphState) -> dict:
    """날짜->일수, DETAIL 코드->lclsSystm2."""
    request = state["request"]

    start = dates.parse_backend(request.start_date)
    end = dates.parse_backend(request.end_date)

    wanted = [
        data.DETAIL_TO_LCLS2[code]
        for code in request.detail_codes
        if code in data.DETAIL_TO_LCLS2
    ]

    # 스타일을 여러 개 골랐으면 적게 넣는 쪽을 따른다
    per_day_choices = [
        PER_DAY_BY_STYLE[s] for s in request.travel_styles if s in PER_DAY_BY_STYLE
    ]

    return {
        "start_yyyymmdd": dates.to_tourapi(start),
        "end_yyyymmdd": dates.to_tourapi(end),
        "day_count": (end - start).days + 1,
        "wanted_lcls2": wanted,
        "per_day": min(per_day_choices) if per_day_choices else DEFAULT_PER_DAY,
    }


# ============================================================
# 2. find_places — 받은 후보에서 취향에 맞는 것을 고른다
# ============================================================


def _place_pool(state: GraphState) -> list[Candidate]:
    """
    장소 후보. 바깥(서버는 백엔드, 테스트는 덤프)에서 넣어 준 것을 쓴다.
    넘겨받은 객체는 고치지 않는다 — 티어 표시는 _take 가 복사본에 붙인다.

    끼니(FD)·숙박(AC)·쇼핑(SH)·추천코스(C01)와 행사(EV)는 여기서 뺀다. 행사를 안 빼면
    같은 항목이 장소로 한 번, 행사로 한 번 들어온다. 바깥을 믿을 수 없어 여기서 거른다.
    """
    given = state["given_places"]
    pool = [c for c in given if c.category_code[:2] in data.PLACE_THEMES]

    # content_id 순 — 같은 입력이면 같은 결과가 나오게
    return sorted(pool, key=lambda candidate: candidate.content_id)


def _event_pool(state: GraphState) -> list[Candidate]:
    """
    행사 후보. 장소와 같은 방식이되 **여행 기간과 겹치는 것만** 남긴다.
    지난 행사가 섞여 오면 열리지 않는 날에 배정된다.

    **복사해서** 쓴다. 바로 아래에서 pick 을 고치는데, 부르는 쪽이 넘긴 객체를 우리가
    바꿔 놓으면 그쪽에서 원인을 찾을 수 없다 — 서버라면 다음 요청에 남는다.
    """
    pool = [candidate.model_copy(deep=True) for candidate in state["given_events"]]
    for candidate in pool:
        candidate.pick = "event"

    start, end = state["start_yyyymmdd"], state["end_yyyymmdd"]
    return [
        candidate
        for candidate in pool
        if (candidate.event_start_date or "") <= end
        and (candidate.event_end_date or "") >= start
    ]


def find_places(state: GraphState) -> dict:
    """
    취향에 맞는 장소를 먼저 담고, 모자라면 세 단계로 넓힌다.

    넓히는 단계가 필요한 이유: 관심사 하나만 고르면 시군구당 후보가 중앙값 4~19건이라
    3박4일치가 안 나오는 지역이 흔하다.

    넓히는 순서 (2026-09-23 사용자 결정):
      1. interest — 고른 중분류 그대로 (LS03 항공 레저)
      2. related  — **같은 대분류**의 다른 중분류 (LS02 수상 · LS01 육상)
      3. filler   — 그 지역의 나머지 아무거나

    고른 것을 **전부** 담은 뒤에 넓힌다. 관심사를 둘 이상 골랐을 때 고른 적 없는
    중분류가 고른 중분류를 밀어내지 않게 하려는 것이다.
    """
    wanted = set(state["wanted_lcls2"])
    # 코드 앞 두 글자가 대분류다 (LS03 → LS)
    wanted_families = {code[:2] for code in wanted}
    needed = state["day_count"] * state["per_day"]

    pool = _place_pool(state)

    matched = [c for c in pool if c.category_code in wanted]
    chosen = _take(matched, needed, "interest")

    if len(chosen) < needed:
        already = {c.content_id for c in chosen}
        related = [
            c
            for c in pool
            if c.content_id not in already and c.category_code[:2] in wanted_families
        ]
        chosen += _take(related, needed - len(chosen), "related")

    if len(chosen) < needed:
        already = {c.content_id for c in chosen}
        fillers = [c for c in pool if c.content_id not in already]
        chosen += _take(fillers, needed - len(chosen), "filler")

    return {"places": chosen}


def _take(pool: list[Candidate], count: int, pick: str) -> list[Candidate]:
    """앞에서 count 개를 떼어 오며 어느 티어로 담겼는지 표시한다."""
    return [candidate.model_copy(update={"pick": pick}) for candidate in pool[:count]]


# ============================================================
# 3. find_events — 기간에 겹치는 행사
# ============================================================


def find_events(state: GraphState) -> dict:
    """
    여행 기간과 겹치는 행사. 관심사에 맞는 것을 앞에 두되, **거르지는 않는다** —
    그 기간에만 있는 것이라 놓치면 아깝다. 0건은 실패가 아니다.

    plan 노드가 이 목록을 앞에서부터 날짜에 배정하므로, 자리가 모자랄 때
    관심사 쪽이 살아남는다. 장소(find_places)의 "맞는 것 먼저, 모자라면 채움"과 같은 방식이다.
    """
    wanted = set(state["wanted_lcls2"])

    # 관심사 불일치를 뒤로 민다. content_id는 같은 입력에 같은 결과를 내려는 것이다.
    events = sorted(
        _event_pool(state),
        key=lambda c: (c.category_code not in wanted, c.content_id),
    )

    return {"events": events}


# ============================================================
# 4. plan — 모델에게 날짜·시각·문구를 받는다. 노드 중 이것만 모델을 부른다
# ============================================================


class InsufficientCandidates(Exception):
    """후보가 여행 일수보다 적어 날마다 한 곳씩도 못 둔다. 작업 실패 insufficient_candidates."""


def plan(state: GraphState) -> dict:
    """
    후보를 전부 넘기고 무엇을 고를지·어느 날에 둘지까지 모델이 정한다.

    행사를 앞에 두고 넘긴다 — 기간이 정해진 것부터 자리를 잡으라는 힌트다.
    규칙으로 강제하지는 않는다 (프롬프트의 「갈 수 있는 날이 적은 행사부터」).

    모델은 **order 가 없는 초안**을 돌려준다. 방문 차례는 여기서 시각을 보고 매긴다.
    제목·소개·하루 요약은 손대지 않고 build 로 넘긴다.
    """
    request = state["request"]
    candidates = state["events"] + state["places"]

    # 날마다 한 곳도 못 채우면 가이드북이 안 된다. 다시 시도해도 같은 후보라 같은 결과다
    if len(candidates) < state["day_count"]:
        raise InsufficientCandidates(
            f"후보 {len(candidates)}곳으로 {state['day_count']}일을 채울 수 없습니다"
        )

    draft = llm_plan.plan_all(
        candidates,
        start=dates.parse_backend(request.start_date),
        day_count=state["day_count"],
        per_day=state["per_day"],
        conditions=trip_conditions(request),
        trip=request,
    )

    day_summaries = {day.day: day.summary for day in draft.days}
    return {
        "plan": number_by_time(draft.items),
        "title": draft.title,
        "intro": draft.intro,
        "day_summaries": day_summaries,
    }


def trip_conditions(request: TripRequest) -> dict:
    """
    모델에게 보내는 여행 조건. 코드가 아니라 **한글 라벨**로 보낸다.

    'HISTORY_HERITAGE_SITE' 를 그대로 주면 모델이 뜻을 짐작해야 하고, 그 짐작이
    제목과 추천 이유에 그대로 묻어난다.
    """
    region = f"{request.province} {request.city}" if request.city else request.province
    interests = [data.preference_label(code) for code in request.detail_codes]
    styles = [data.preference_label(code) for code in request.travel_styles]
    return {
        "region": region,
        "companion": data.companion_label(request.companion),
        "people_count": request.people_count,
        "interests": interests,
        "travel_styles": styles,
    }


def _minutes(hhmm: str) -> int:
    try:
        hour, minute = hhmm.split(":")
        return int(hour) * 60 + int(minute)
    except ValueError as exc:
        raise LLMError(f"시각 형식이 HH:MM 이 아닙니다: {hhmm!r}") from exc


def number_by_time(drafts: list[PlanDraftItem]) -> list[PlanItem]:
    """
    그 날 안에서 **시각이 이른 것부터** 1·2·3 을 매긴다.

    order 를 모델에게 받지 않는 이유: start_time 에서 그냥 나오는 값이다.
    받으면 order 1 이 19:00, order 2 가 14:00 으로 오는 일이 생기고
    (2026-09-25 gemini 응답에서 실제로 겪었다), 거리 합계도 화면도 order 순서라
    일정이 통째로 뒤집힌다. 검사로 막는 것보다 **아예 못 생기게** 하는 쪽이 낫다.
    """
    by_day: dict[int, list[PlanDraftItem]] = collections.defaultdict(list)
    for draft in drafts:
        by_day[draft.day].append(draft)

    items: list[PlanItem] = []
    for day in sorted(by_day):
        of_day = sorted(by_day[day], key=lambda d: _minutes(d.start_time))
        for order, draft in enumerate(of_day, start=1):
            items.append(
                PlanItem(
                    content_id=draft.content_id,
                    day=draft.day,
                    order=order,
                    start_time=draft.start_time,
                    duration_minutes=draft.duration_minutes,
                    reason=draft.reason,
                )
            )
    return items


# ============================================================
# 5. build — 사실 데이터를 붙여 최종 일정을 만든다
# ============================================================


def _to_planned_place(item: PlanItem, candidate: Candidate) -> PlannedPlace:
    """LLM의 배치값과 우리 사실 데이터를 한 건으로 합친다. 출력 필드가 늘면 고칠 곳."""
    coordinates = None
    if candidate.lat is not None and candidate.lng is not None:
        coordinates = Coordinates(lat=candidate.lat, lng=candidate.lng)

    return PlannedPlace(
        # LLM이 정한 것
        order=item.order,
        start_time=item.start_time,
        duration_minutes=item.duration_minutes,
        recommend_reason=item.reason,
        # 우리가 갖고 있던 것 — LLM을 거치지 않는다
        content_id=candidate.content_id,
        name=candidate.name,
        category_code=candidate.category_code,
        category_name=data.category_name(candidate.category_code),
        category_path=data.category_path(
            candidate.category_code, candidate.subcategory_code
        ),
        address=candidate.address,
        coordinates=coordinates,
        image_url=candidate.image_url,
        event_end_date=candidate.event_end_date,
    )


def _fill_distances(day: ItineraryDay) -> None:
    """
    그날 구간별 직선거리를 채우고 합계를 낸다.

    좌표가 없는 장소(전체의 0.1%)가 끼면 그 구간만 None 으로 두고 합계에서 뺀다.
    한 곳 때문에 그날 거리가 통째로 사라지면 오히려 읽는 사람이 헷갈린다.
    """
    total = 0.0
    previous = None

    for place in day.places:
        if previous is not None:
            km = distance.distance_km(previous, place)
            if km is not None:
                place.distance_from_previous_km = round(km, 2)
                total += km
        previous = place

    day.total_distance_km = round(total, 2)


def _pick_cover(
    itinerary: list[ItineraryDay], by_id: dict[str, Candidate]
) -> tuple[str | None, str | None]:
    """
    표지 대표 사진을 고른다. 돌려주는 값은 (사진 주소, 장소 이름).

    사용자가 고른 관심사에 정확히 맞는 장소를 우선한다 — 표지는 "이 여행이 무엇인가"를
    한 장으로 말하는 자리라, 기간 한정 행사보다 관심사가 앞선다. 서울 중구 역사 여행의
    경우 사흘 내내 그날 첫 장소가 행사여서, 순서만 보면 표지가 행사 사진이 된다.

    관심사를 아예 안 고른 요청(pick 이 전부 filler)도 있으므로 아래로 세 번 물러선다.
    """
    places = [place for day in itinerary for place in day.places]
    with_image = [place for place in places if place.image_url]

    def first(candidates: list[PlannedPlace]) -> PlannedPlace | None:
        return candidates[0] if candidates else None

    by_interest = [
        place for place in with_image if by_id[place.content_id].pick == "interest"
    ]
    not_event = [place for place in with_image if not place.event_end_date]

    chosen = first(by_interest) or first(not_event) or first(with_image)
    if chosen is None:
        return None, None  # 사진 있는 장소가 하나도 없다. 표지는 사진 없이 나간다
    return chosen.image_url, chosen.name


def build(state: GraphState) -> dict:
    """
    LLM이 돌려준 content_id에 사실 데이터를 붙이고, 날짜별로 묶어 최종 일정을 만든다.

    이름·주소·좌표가 LLM을 거치지 않는 지점이 여기다.
    """
    request = state["request"]
    by_id = {c.content_id: c for c in state["events"] + state["places"]}

    start = dates.parse_backend(request.start_date)
    days: dict[int, list[PlannedPlace]] = {}

    # 없는 id 는 llm_plan 의 검사가 이미 막는다. 여기서 조용히 버리지 않고 멈추게 둔다
    for item in sorted(state["plan"], key=lambda i: (i.day, i.order)):
        candidate = by_id[item.content_id]
        days.setdefault(item.day, []).append(_to_planned_place(item, candidate))

    day_summaries = state["day_summaries"]
    itinerary = [
        ItineraryDay(
            day=day,
            date=(start + timedelta(days=day - 1)).isoformat(),
            places=places,
            # 모든 일차에 요약이 있는지는 llm_plan 의 검사가 이미 본다
            summary=day_summaries[day],
        )
        for day, places in sorted(days.items())
    ]

    for day in itinerary:
        _fill_distances(day)

    scheduled = {p.content_id for d in itinerary for p in d.places}
    unscheduled = [
        MissedEvent(
            name=event.name,
            start_date=event.event_start_date or "",
            end_date=event.event_end_date or "",
            address=event.address,
            image_url=event.image_url,
        )
        for event in state["events"]
        if event.content_id not in scheduled
    ]

    summary = TripSummary(
        province=request.province,
        city=request.city,
        start_date=start.isoformat(),
        end_date=dates.parse_backend(request.end_date).isoformat(),
        day_count=state["day_count"],
        people_count=request.people_count,
    )

    cover_image_url, cover_place_name = _pick_cover(itinerary, by_id)

    return {
        "result": Itinerary(
            summary=summary,
            itinerary=itinerary,
            unscheduled_events=unscheduled,
            title=state["title"],
            intro=state["intro"],
            cover_image_url=cover_image_url,
            cover_place_name=cover_place_name,
        )
    }
