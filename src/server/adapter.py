"""
src/server/adapter.py

백엔드 말 ↔ v0 말. 번역만 한다. 계약서는 docs/backend/backend-AI-API.md.

따로 둔 이유: 막혔을 때 원인이 번역인지 그래프인지 바로 갈린다.
"""

from __future__ import annotations

from pydantic import ValidationError

from src import dates
from src.engine import data
from src.engine.data import DataError
from src.models import Candidate, Itinerary, TripRequest
from src.output import book_html

MAX_DAYS = 7  # 계약 2장
# 백엔드는 그 지역 후보를 개수 제한 없이 전부 보낸다 (2026-10-07 결정). 덤프 기준 최대는
# 제주시 441개 — 정상 요청은 걸리지 않고, 고장 난 요청만 막는 값이다
MAX_CONTENTS = 1000
MAX_TITLE = 15  # 계약 7장
MIN_PEOPLE = 1
MAX_PEOPLE = 10


class InputError(Exception):
    """
    요청이 잘못됐다. 작업 실패가 아니라 접수 거절(400)이다.

    code 는 계약서의 오류 message 값이고, reason 은 우리가 로그에서 볼 말이다 —
    계약 응답에는 code 만 나가므로 이유를 여기 따로 들고 있어야 한다.
    """

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def to_trip_request(payload: dict) -> TripRequest:
    """
    접수 요청을 src의 조건으로 바꾼다.

    지역명·날짜·취향을 여기서 확인한다. 코드표에 없는 값은 작업이 실패한 게 아니라
    요청이 틀린 것이다 — 큐에 넣고 1분 뒤에 실패로 알릴 일이 아니라 접수 시점에 거절할 일이다.
    """
    region = payload.get("region")
    if not isinstance(region, dict):
        raise InputError("invalid_request", "region 이 없습니다")

    # 계약 필수다. 빠진 채 받으면 시/도 전체 여행으로 조용히 바뀐다
    # (시/도 전체는 CLI 에서만 쓴다 — 여기를 거치지 않는다)
    if not region.get("city"):
        raise InputError("invalid_request", "region.city 가 없습니다")

    # 중분류와 여행스타일을 코드로 변환
    detail_codes, travel_styles = _to_preference_codes(payload.get("preferences"))

    try:
        trip = TripRequest(
            province=region.get("province"),
            city=region.get("city"),
            start_date=payload.get("start_date"),
            end_date=payload.get("end_date"),
            companion=payload.get("companion"),
            # 계약 필수다. 기본값을 두면 빠진 요청이 조용히 1명으로 짜인다
            people_count=payload.get("people_count"),
            detail_codes=detail_codes,
            travel_styles=travel_styles,
        )
    except ValidationError as exc:
        raise InputError("invalid_request", str(exc)) from exc

    if not MIN_PEOPLE <= trip.people_count <= MAX_PEOPLE:
        raise InputError(
            "invalid_request", f"people_count 는 {MIN_PEOPLE}~{MAX_PEOPLE} 입니다"
        )

    _check_dates(trip)  # 날짜 형식과 기간 체크

    try:
        data.find_region_codes(trip.province, trip.city)
    except DataError as exc:
        raise InputError("invalid_request", str(exc)) from exc

    return trip


def _check_dates(trip: TripRequest) -> None:
    """날짜 형식은 우리 것(YY.MM.DD)으로 맞추기로 했다."""
    start = _date(trip.start_date)
    end = _date(trip.end_date)

    if end < start:
        raise InputError("invalid_date_range", "end_date 가 start_date 보다 앞섭니다")
    if (end - start).days + 1 > MAX_DAYS:
        raise InputError("invalid_date_range", f"여행은 최대 {MAX_DAYS}일입니다")


def _date(value: object, code: str = "invalid_date_range"):
    """날짜 형식은 요청·응답·행사기간 전부 YY.MM.DD 다 (백엔드와 합의)."""
    if not isinstance(value, str):
        raise InputError(code, f"날짜가 비었습니다: {value!r}")
    try:
        return dates.parse_backend(value)
    except ValueError as exc:
        raise InputError(code, f"날짜는 YY.MM.DD 형식이어야 합니다: {value!r}") from exc


def _to_preference_codes(preferences: object) -> tuple[list[str], list[str]]:
    """
    백엔드가 보낸 **한글 라벨**을 우리 취향 코드로 바꾼다 (계약 2장).

        {"mid_category": {"자연": ["공원"]}, "travel_style": ["여유롭게"]}
            -> (["NATURE_PARK"], ["RELAXING"])

    `large_category` 는 **있는지만 본다** (계약 필수, 2026-09-28). 값은 쓰지 않는다 —
    중분류 코드 앞 두 글자가 곧 대분류라 (`NA04` -> `NA`) 우리 엔진이 거기서 대분류를
    얻는다. 대분류만 오는 요청은 없다는 것이 백엔드와의 합의다 (2026-09-27).

    모르는 라벨은 거절한다. prepare 가 모르는 코드를 조용히 건너뛰기 때문에,
    통과시키면 관심사를 하나도 못 읽은 채 "성공"한 일정이 나온다.
    """
    if not isinstance(preferences, dict):
        raise InputError("invalid_preference_mapping", "preferences 가 없습니다")

    large = preferences.get("large_category")
    if not isinstance(large, list) or not large:
        raise InputError("invalid_preference_mapping", "large_category 가 비었습니다")

    mid = preferences.get("mid_category")
    if not isinstance(mid, dict) or not mid:
        raise InputError("invalid_preference_mapping", "mid_category 가 비었습니다")

    detail_codes: list[str] = []
    for labels in mid.values():
        for label in labels if isinstance(labels, list) else []:
            detail_codes.append(_code(label, "DETAIL"))  # 코드로 변환

    if not detail_codes:
        raise InputError("invalid_preference_mapping", "고른 중분류가 없습니다")

    travel_styles = [
        _code(label, "TRAVEL_STYLE") for label in preferences.get("travel_style") or []
    ]  # 코드로 변환

    return detail_codes, travel_styles


def _code(label: object, preference_type: str) -> str:
    if not isinstance(label, str):
        raise InputError(
            "invalid_preference_mapping", f"취향이 글자가 아닙니다: {label!r}"
        )

    code = data.preference_code(label, preference_type)
    if code is None:
        raise InputError("invalid_preference_mapping", f"표에 없는 취향: {label!r}")
    return code


def to_candidates(contents: object) -> tuple[list[Candidate], list[Candidate]]:
    """
    백엔드가 보낸 그 지역의 후보를 장소와 행사로 가른다 (계약 2장).

    고르는 일은 여기서 하지 않는다 — 들어온 것에서 취향 티어로 추리는 것은
    find_places 이고, 그 규칙은 후보 출처와 무관하다.
    """
    if not isinstance(contents, list) or not contents:
        raise InputError("invalid_request", "contents 가 비었습니다")
    if len(contents) > MAX_CONTENTS:  # 최대 후보 개수 이상인지 체크
        raise InputError("invalid_request", f"contents 는 최대 {MAX_CONTENTS}개입니다")

    places: list[Candidate] = []
    events: list[Candidate] = []

    for item in contents:
        if not isinstance(item, dict):
            raise InputError(
                "invalid_request", f"contents 항목이 객체가 아닙니다: {item!r}"
            )

        kind = item.get("content_type")
        if kind == "PLACE":
            places.append(_to_candidate(item))
        elif kind == "EVENT":
            events.append(_to_event(item))
        else:
            raise InputError("invalid_request", f"잘못된 content_type: {kind!r}")

    return places, events


def _to_candidate(item: dict) -> Candidate:
    content_id = item.get("content_id")
    if not content_id:
        raise InputError("invalid_request", "content_id 가 없습니다")

    coordinates = item.get("coordinates") or {}
    return Candidate(
        content_id=str(content_id),
        name=item.get("title") or "",
        # 중분류(HS01)가 취향 티어의 기준이고, 소분류(HS010100)는 이름 표시에 쓴다
        category_code=item.get("classification_code_2") or "",  # 중분류
        subcategory_code=item.get("classification_code_3") or "",  # 소분류
        address=item.get("address") or "",
        lat=coordinates.get("lat"),
        lng=coordinates.get("lng"),
        image_url=item.get("image_url"),
    )


def _to_event(item: dict) -> Candidate:
    """행사는 기간이 있어야 한다 — 없으면 열리지 않는 날에 배정된다 (계약 2장)."""
    period = item.get("event_period")
    if not isinstance(period, dict):
        raise InputError(
            "invalid_request",
            f"행사에 event_period 가 없습니다: {item.get('content_id')}",
        )

    candidate = _to_candidate(item)
    candidate.pick = "event"
    candidate.event_start_date = _to_compare_date(period.get("start_date"))
    candidate.event_end_date = _to_compare_date(period.get("end_date"))
    return candidate


def _to_compare_date(value: object) -> str:
    """
    '26.10.10' -> '20261010'.

    행사 기간이 여행 기간과 겹치는지 보려면 두 날짜의 형식이 같아야 한다. 엔진은
    TourAPI 형식으로 비교하므로(nodes.prepare 의 start_yyyymmdd) 여기서 맞춰 준다.
    """
    return dates.to_tourapi(_date(value, "invalid_request"))


def _to_backend_date(iso_date: str) -> str:
    """
    '2026-10-12' -> '26.10.12'.

    안쪽 Itinerary 는 ISO 날짜를 쓴다 (골든 파일과 가이드북 HTML 이 그 형식에 기대고 있다).
    백엔드 형식으로 맞추는 일은 나가는 자리인 여기서 한 번만 한다.
    """
    return dates.to_backend(dates.parse_iso(iso_date))


def _title(itinerary: Itinerary) -> str:
    """
    모델이 쓴 제목. 계약 상한 15자를 넘으면 자른다.

    넘쳤다고 작업을 실패로 돌리지 않는다 — 사실 오류가 아니고, 프롬프트가 이미
    15자 이내를 요구한다. 잘린 제목이 보이면 프롬프트를 고칠 일이다.
    """
    return itinerary.title[:MAX_TITLE]


def to_response(trip: TripRequest, itinerary: Itinerary) -> dict:
    """
    계약서 7장의 result 모양으로 만든다.

    장소 이름·주소·좌표·이미지는 **일정 목록에** 넣지 않는다 — 백엔드가 content_id 로
    자기 DB 에서 꺼내 쓰고, 우리가 또 보내면 '어느 쪽이 맞나'가 생긴다 (계약 7장).

    `content_html` 은 사람이 읽는 가이드북이라 그 안에는 이름·사진이 들어 있다.
    **같은 사실을 두 모양으로 보내는 것**이고, 쓰는 쪽이 다르다 —
    itinerary 는 백엔드가 저장하는 데이터, content_html 은 화면에 그대로 띄우는 것.
    """
    return {
        "title": _title(itinerary),
        "summary": itinerary.intro,
        "content_html": book_html.render(trip, itinerary),
        "itinerary": [
            {
                "day": day.day,
                "date": _to_backend_date(day.date),
                # 안쪽 이름은 summary 지만, 계약에서는 result.summary(여행 소개)와
                # 겹치지 않게 day_summary 로 나간다.
                "day_summary": day.summary,
                "places": [
                    {
                        "order": place.order,
                        "time": place.start_time,
                        "content_id": place.content_id,
                        "duration_minutes": place.duration_minutes,
                        "recommend_reason": place.recommend_reason,
                    }
                    for place in day.places
                ],
            }
            for day in itinerary.itinerary
        ],
    }
