"""
src/models.py

일정 만들기(engine)가 주고받는 값의 모양. 백엔드 응답 계약과는 분리해 둔다 —
계약 모양으로 깎는 일은 server/adapter.py 가 나가는 자리에서 한 번만 한다.
"""

from __future__ import annotations

from typing import Literal, Optional, TypedDict

from pydantic import BaseModel

# 동행. 계약 필수 값 다섯 가지 그대로 (docs/backend/backend-AI-API.md 2장)
Companion = Literal["alone", "friend", "couple", "family", "group"]


class TripRequest(BaseModel):
    """사용자 입력 조건."""

    province: str  # TourAPI 표기 그대로
    city: Optional[str] = None  # 없으면 시/도 전체
    start_date: str  # YY.MM.DD (백엔드 형식)
    end_date: str
    # 기본값을 두지 않는다 — 빠진 요청이 조용히 '혼자'로 짜이면 안 된다.
    # 인원과 맞는지는 보지 않는다 (백엔드가 거른다, 사용자 결정 2026-09-28)
    companion: Companion
    people_count: int = 1
    detail_codes: list[str] = []  # user_category_enum.csv 의 DETAIL 코드
    travel_styles: list[str] = []  # TRAVEL_STYLE 코드


class Candidate(BaseModel):
    """
    일정에 넣을 수 있는 것 하나. 장소와 행사를 같은 모양으로 담는다.
    TourAPI 응답 키(title, mapx, firstimage ...)는 여기서 끝난다.
    """

    content_id: str
    name: str
    category_code: str  # lclsSystm2 중분류 (예: 'HS01' 역사유적지)
    # lclsSystm3 소분류 (예: 'HS010100' 고궁). 덤프에 100% 채워져 있다.
    # 중분류만으로는 덕수궁도 숭례문도 똑같이 '역사유적지'라 구별이 안 된다.
    subcategory_code: str = ""
    address: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    image_url: Optional[str] = None  # 대상 5종의 7.6%는 이미지가 없다

    # 왜 후보에 들어왔나. 문장이 아니라 값으로 남겨야 나중에 추적이 된다
    #   interest 고른 중분류 그대로 · related 같은 대분류의 다른 중분류 · filler 그 밖
    pick: Literal["interest", "related", "filler", "event"] = "interest"

    # 행사만 값이 있다 (YYYYMMDD)
    event_start_date: Optional[str] = None
    event_end_date: Optional[str] = None

    # 장소 소개글. 백엔드가 보낸다. 글쓰기(write)에만 넣고 배치(arrange)에는 넣지 않는다 —
    # 후보 전부에 붙이면 입력이 몇 배가 된다. 없는 곳도 있어 비어도 된다
    overview: str = ""


class PlanItem(BaseModel):
    """
    배치 한 줄. 이름·주소·좌표는 받지 않는다 — 우리가 이미 갖고 있고,
    받으면 '어느 쪽이 맞나'라는 판단이 생긴다.

    추천 이유는 여기 없다 — 글쓰기(write)가 따로 쓰고 build 가 content_id 로 붙인다.
    """

    content_id: str
    day: int  # 1부터
    order: int  # 그날 안에서 1부터
    start_time: str  # HH:MM
    duration_minutes: int


class PlanDraftItem(BaseModel):
    """
    모델이 돌려주는 한 줄. PlanItem 과 달리 **order 가 없다.**

    방문 차례는 start_time 에서 그대로 나오므로 코드가 매긴다. 받아 보면
    order 1 이 19:00, order 2 가 14:00 으로 오는 일이 생기고(2026-09-25 실제로 겪었다),
    거리 합계도 화면도 order 순서라 일정이 통째로 뒤집힌다.

    번호를 매기는 곳은 nodes.number_by_time 하나다.
    """

    content_id: str
    day: int  # 1부터
    start_time: str  # HH:MM
    duration_minutes: int


class DayDraft(BaseModel):
    """
    하루의 흐름을 적은 글. 장소는 여기 담지 않는다 — PlanDraftItem 이 day 번호를 갖고
    있어서, 날 안에 장소를 넣으면 같은 정보가 두 곳에 생긴다.
    """

    day: int  # 1부터
    summary: str  # 그날 어디를 어떤 순서로 도는지


class DayPlanDraft(BaseModel):
    """
    배치(arrange) 응답의 겉껍데기.

    구조화 출력에 넘길 스키마라 최상위가 객체여야 해서 한 겹 감쌌다.
    PlanDraftItem 이 content_id 만 받도록 돼 있으므로, "사실 데이터는 모델을 거치지
    않는다"는 규칙이 프롬프트가 아니라 **스키마로** 강제된다.
    """

    items: list[PlanDraftItem]


class RegenerateDraft(BaseModel):
    """
    재생성 배치 응답. 피드백을 받을 수 있는지부터 답하게 한다 —
    applicable 이 false 면 items 는 비고 reject_reason 만 본다 (#44).
    """

    applicable: bool
    reject_reason: str
    items: list[PlanDraftItem]


class Reason(BaseModel):
    """장소 하나의 추천 이유. 이름은 자체 모델 학습 데이터(v7)의 스키마와 같게 둔다."""

    content_id: str
    reason: str


class WriterDraft(BaseModel):
    """
    글쓰기(write) 응답. 배치가 끝난 일정을 받아 문구만 쓴다.

    모양을 자체 모델 학습 데이터(samples/open_model/label_inputs_v7.jsonl)의 출력과
    같게 둔다 — write 를 자체 모델로 바꿀 때 받는 쪽을 고치지 않으려고.
    기본값이 없는 이유는 빠뜨린 칸이 빈 값으로 나가지 않고 검증에서 멈추게 하려는 것.
    """

    title: str  # 가이드북 제목. 계약 상한 15자
    intro: str  # 여행 하나를 한 문장으로. 계약의 result.summary 자리
    days: list[DayDraft]
    reasons: list[Reason]


class Coordinates(BaseModel):
    lat: float
    lng: float


class PlannedPlace(BaseModel):
    order: int
    start_time: str
    duration_minutes: int

    content_id: str
    name: str
    category_code: str
    category_name: str  # 중분류 이름. HTML 에서 category_path 가 비었을 때 쓴다
    category_path: str = ""  # '역사관광 › 역사유적지 › 고궁'

    address: str
    coordinates: Optional[Coordinates] = None
    image_url: Optional[str] = None

    # pick(interest/filler/event)은 응답에 넣지 않는다 — 후보를 고르는 내부 근거이지
    # 백엔드가 알아야 할 값이 아니다. Candidate.pick 은 그대로 두고 여기서만 뺀다.
    recommend_reason: str
    event_end_date: Optional[str] = None

    # 직전 장소에서 여기까지의 **직선** 거리. 그날 첫 장소는 None.
    # 실제 도보·차량 경로가 아니다 — TourAPI에 경로 데이터가 없다.
    distance_from_previous_km: Optional[float] = None


class ItineraryDay(BaseModel):
    day: int
    date: str  # YYYY-MM-DD
    places: list[PlannedPlace]
    total_distance_km: float = 0.0  # 그날 구간 직선거리의 합
    summary: str = ""  # 모델이 쓴 그날의 흐름. 가이드북 일차 지면에 들어간다


class MissedEvent(BaseModel):
    """
    기간 중에 열리지만 일정에 못 넣은 행사. 가이드북의 행사 쪽에 사진·기간과 함께 싣는다.

    못 넣은 이유는 대개 하루 행사 1건 상한이다 — 기간 중에 열리는 것은 맞다.
    """

    name: str
    start_date: str  # YYYYMMDD (TourAPI 형식 그대로)
    end_date: str
    address: str = ""
    image_url: Optional[str] = None


class TripSummary(BaseModel):
    province: str
    city: Optional[str]
    start_date: str  # YYYY-MM-DD
    end_date: str
    day_count: int
    people_count: int


class Itinerary(BaseModel):
    """엔진(graph.run)의 최종 산출물."""

    summary: TripSummary
    itinerary: list[ItineraryDay]
    unscheduled_events: list[MissedEvent] = []  # 일정에 못 넣은 행사

    # 모델이 쓴 제목과 소개 문장. summary 라는 이름은 위에서 이미 쓰고 있어
    # (지역·기간·인원을 담은 TripSummary) 소개 문장은 intro 로 둔다.
    # 서버가 계약의 result.title · result.summary 로 옮긴다.
    title: str = ""
    intro: str = ""

    # 표지에 쓸 대표 사진. **고른 결과만** 담는다.
    # 고르는 근거(Candidate.pick)는 build 노드 안에서 끝난다 — pick 을 PlannedPlace 에
    # 올리면 후보 선정의 내부 사정이 백엔드 응답까지 새어 나간다.
    cover_image_url: Optional[str] = None
    cover_place_name: Optional[str] = None  # 사진이 어디 것인지 표지에 적는다


class GraphState(TypedDict, total=False):
    """
    노드끼리 주고받는 칸. 노드는 '자기가 갱신할 칸만' 담은 dict를 돌려준다.
    total=False인 이유: 시작 시점에는 request만 있고 나머지는 노드가 차례로 채운다.
    """

    request: TripRequest

    # 바깥에서 넣어 준 후보 — 그 지역의 장소·행사 전부.
    # 서버는 백엔드 DB 가 보내 준 것을, 테스트·로컬 도구는 덤프로 만든 것을 넣는다
    # (tools/dump_candidates.py).
    # **고르는 일은 우리가 한다** — 취향 티어로 추리는 것은 find_places 다.
    given_places: list[Candidate]
    given_events: list[Candidate]

    # prepare
    start_yyyymmdd: str
    end_yyyymmdd: str
    day_count: int
    wanted_lcls2: list[str]
    wanted_families: list[str]  # 넓히기(related)에 쓰는 대분류 (LS03 → LS)
    per_day: int

    # find_places / find_events (병렬)
    places: list[Candidate]
    events: list[Candidate]

    # 재생성일 때만 — 바깥에서 넣어 준다 (graph.run_regeneration). arrange 가 이것을 보고 갈라진다
    feedback: str
    previous_plan: list[PlanItem]

    # arrange
    plan: list[PlanItem]

    # write — 모델이 쓴 가이드북 문구. build 가 plan 과 content_id 로 잇는다
    title: str
    intro: str
    day_summaries: dict[int, str]  # 일차 → 그날 요약
    reasons: dict[str, str]  # content_id → 추천 이유

    result: Itinerary  # build
