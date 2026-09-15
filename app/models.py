"""
app/models.py

AI 서버 API 명세서(최종 확정본)를 그대로 코드로 옮긴 스키마 파일입니다.
이 파일의 목적은 "명세서에 적힌 필드/타입/필수여부가 실제로 검증 가능한 형태로
옮겨지는가"를 확인하는 것입니다 (4번 항목 2단계).

구성 순서:
  1. 공통 Enum (여러 요청/응답에서 재사용되는 값들)
  2. 요청(Request) 스키마 - 클라이언트가 우리에게 보내는 것
  3. 응답 하위 조각(sub-schema) - 여러 응답에서 재사용되는 구성요소
  4. 응답(Response) 스키마 - 우리가 클라이언트에게 돌려주는 것
  5. 파이프라인 내부 모델 - TourAPI 조회 결과 (응답으로 직접 나가지 않음)
"""

from __future__ import annotations
from datetime import date
from typing import Optional, Literal
from pydantic import BaseModel, Field


# ============================================================
# 1. 공통 Enum
# ============================================================
# Literal을 쓰는 이유: 명세서에 "이 필드는 이 값들 중 하나여야 한다"고
# 명시된 부분(companion, status, state, error.type)을 Enum class 대신
# Literal로 표현하면, FastAPI의 자동 문서(/docs)에도 그대로 드롭다운
# 형태로 노출되어 명세서와 1:1 대응이 눈으로 확인하기 쉬워집니다.

CompanionType = Literal["alone", "friend", "couple", "family", "group"]
JobStatus = Literal["pending", "processing", "completed", "failed"]
StepState = Literal["done", "running", "pending", "failed"]
ErrorType = Literal["server_error", "network_disconnected", "timeout"]


# ============================================================
# 2. 요청(Request) 스키마
# ============================================================

class RegionInput(BaseModel):
    """POST /guidebooks 의 region 필드"""
    province: str
    city: str


class Preferences(BaseModel):
    """
    취향 정보. mid_category를 배열이 아니라 '대분류명 -> 배열' 형태의
    객체(dict)로 정의한 이유:

    대분류를 여러 개 선택했을 때 "대분류마다 중분류 최소 1개"라는 규칙을
    검증하려면, 각 중분류가 어느 대분류에 속하는지를 알아야 합니다.
    평평한 배열로 받으면 이 매핑 정보를 서버가 별도 테이블로 관리해야
    하지만, 대분류명을 key로 쓰면 그 자체로 매핑이 되어 있어 검증 로직이
    단순해집니다. (예: large_category의 모든 항목이 mid_category의
    key로 존재하고, 그 값(배열)이 비어있지 않은지만 확인하면 됨)
    """
    large_category: list[str] = Field(..., min_length=1)
    mid_category: dict[str, list[str]]
    travel_style: Optional[list[str]] = None


class GenerateRequest(BaseModel):
    """POST /guidebooks 요청 바디"""
    region: RegionInput
    start_date: str  # YYYY-MM-DD, 최대 7일 이내 / 과거 날짜 불가 (검증은 라우터 단에서)
    end_date: str
    companion: CompanionType
    people_count: int
    preferences: Preferences


class RegenerateRequest(BaseModel):
    """
    POST /guidebooks/{guidebook_id}/regenerate 요청 바디

    feedback이 필수, title은 선택인 이유: 명세서 E열에 "feedback: 필수,
    title: 선택"으로 명시되어 있습니다. 사용자가 내용 피드백 없이
    제목만 바꾸는 경우는 이 엔드포인트의 주된 용도(자연어 피드백 기반
    재생성)와 어긋나므로, feedback을 항상 받도록 강제합니다.
    """
    feedback: str = Field(..., max_length=200)
    title: Optional[str] = Field(default=None, max_length=15)


class RegionRecommendRequest(BaseModel):
    """POST /guidebooks/regions-recommendations 요청 바디"""
    preferences: Preferences
    duration_days: int


# retry 엔드포인트(POST /guidebooks/{job_id}/retry)는 body가 없습니다.
# 식별자(job_id)가 이미 URL 경로에 있어서, 별도 요청 스키마가 필요 없습니다.


# ============================================================
# 3. 응답 하위 조각 (여러 응답에서 재사용)
# ============================================================

class Coordinates(BaseModel):
    lat: float
    lng: float


class Step(BaseModel):
    """진행 단계 하나. steps 배열의 원소.
    이전 버전에 있던 current_step/percent/failed_step 같은 필드는
    전부 이 steps 배열에서 계산 가능해 제거했습니다
    (state가 'running'인 항목 = 현재 단계, 'failed'인 항목 = 실패 단계).
    """
    key: str
    label: str
    state: StepState


class ErrorInfo(BaseModel):
    """실패 시 error 필드. status 코드가 아니라 응답 바디 안에 담기는
    이유는, 'GET 요청 자체'는 성공(200)했고 다만 '조회해보니 그 작업이
    실패해 있었다'는, 서로 다른 층위의 정보이기 때문입니다."""
    type: ErrorType
    message: str


class Place(BaseModel):
    """itinerary 안의 장소 하나"""
    order: int
    time: str
    content_id: str          # TourAPI 원본 데이터 추적용
    name: str
    category: str
    description: str
    recommend_reason: str    # region 추천/장소 추천 전체에서 이름 통일됨
    tip: str
    duration_minutes: int
    address: str
    coordinates: Coordinates
    image_url: str
    source: Literal["tourapi", "llm_fallback"]
    event_end_date: Optional[str] = None  # 행사 성격 장소만 값 존재


class ItineraryDay(BaseModel):
    day: int
    date: str
    places: list[Place]


class DaySummary(BaseModel):
    """GUIDE-05 목록 카드용 요약 ("Day 1 첨성대 외 3곳")"""
    day: int
    representative: str
    place_count: int


class EventInfo(BaseModel):
    event_id: str
    name: str
    period: str
    venue: str


class GuidebookSummary(BaseModel):
    region: str
    period: str
    duration_label: str


class RegionRecommendation(BaseModel):
    name: str
    province: str
    city: str
    recommend_reason: str


class Progress(BaseModel):
    steps: list[Step]


# ============================================================
# 4. 응답(Response) 스키마
# ============================================================
# 명세서의 모든 응답이 { "message": str, "data": ... } 형태를 공통으로
# 씁니다. data의 내용물만 상황별로 다르므로, 각 상황별 data 모델을
# 따로 정의하고 message는 각 라우터에서 리터럴 값으로 고정합니다.

class GenerateAcceptedData(BaseModel):
    job_id: str
    status: Literal["pending"]
    remaining_quota: int


class RetryAcceptedData(BaseModel):
    job_id: str
    status: Literal["pending"]
    retry_count: int


class RegenerateAcceptedData(BaseModel):
    job_id: str
    status: Literal["pending"]


class ProcessingData(BaseModel):
    job_id: str
    status: Literal["processing"]
    progress: Progress


class CompletedData(BaseModel):
    guidebook_id: str
    job_id: str
    status: Literal["completed"]
    summary: GuidebookSummary
    days: list[DaySummary]
    itinerary: list[ItineraryDay]
    events: list[EventInfo]
    # content_html 필드는 의도적으로 넣지 않았습니다.
    # HTML 렌더링은 백엔드 책임이며, AI 서버는 순수 데이터만 반환합니다.


class FailedData(BaseModel):
    job_id: str
    status: Literal["failed"]
    steps: list[Step]
    error: ErrorInfo
    retry_count: int


class RegionRecommendData(BaseModel):
    regions: list[RegionRecommendation]


class HealthData(BaseModel):
    pass  # health 200 응답의 data는 항상 null


# ---- 에러 응답 (data는 항상 null 또는 최소 정보) ----

class JobConflictData(BaseModel):
    """409 job_already_running 전용 - 이미 실행 중인 job_id를 알려줌"""
    job_id: str


class ErrorResponse(BaseModel):
    """대부분의 4xx/5xx가 공통으로 쓰는 형태 (data: null)"""
    message: str
    data: None = None


# ============================================================
# 5. 파이프라인 내부 모델 (TourAPI 조회 결과)
# ============================================================
# 위 Place/EventInfo는 백엔드에 돌려주는 최종 응답 모양이고, 아래 두 모델은
# 파이프라인 안에서 TourAPIClient → 노드 → State로 흘러가는 조회 결과입니다.
# 일정 배치·추천 이유 같은 LLM 생성 값이 붙기 전 단계라 필드 구성이 다릅니다.
# 여기 담긴 이름·주소·좌표·이미지는 LLM을 거치지 않고 그대로 최종 응답으로 갑니다.
#
# 필드 이름은 우리 쪽 이름입니다. TourAPI 응답 키를 이 필드로 옮기는 일은
# TourAPIClient가 담당하고, 실제 응답 키는 아직 조사 중입니다 (tour_api.py TODO).
# 필수 여부도 실물 응답을 보기 전이라, 식별자·이름(행사는 기간까지) 외에는
# 일단 Optional로 둡니다.

class TourPlace(BaseModel):
    """TourAPI 장소 조회 결과 하나. 장소 추천 노드의 산출물."""

    # TourAPI 원본 식별자. LLM이 생성한 일정·추천 이유와 사실 데이터를
    # 병합할 때 기준 키로 쓴다 (이름은 동명 장소가 있을 수 있어 키로 부적합).
    content_id: str
    name: str

    # TODO: 주소가 한 필드로 오는지, 기본주소/상세주소로 나뉘어 오는지 확인
    address: Optional[str] = None

    # TODO: 좌표 값의 타입(문자열/숫자)과 경도·위도 필드 구분 확인
    coordinates: Optional[Coordinates] = None

    # TODO: 이미지 없는 장소가 얼마나 되는지 확인. 응답 Place.image_url은
    # 필수라서, 이미지 없는 장소를 필터링에서 뺄지 결정과 연결됨
    image_url: Optional[str] = None

    # TODO: TourAPI 카테고리 체계 확인 (단일 코드인지 대/중/소 계층인지,
    # 콘텐츠 유형 구분이 따로 있는지). 확인 후 필드 모양 자체가 바뀔 수 있음.
    # 응답 Place.category("문화" 같은 표시용 이름)로 바꾸는 규칙도 미정
    category_code: Optional[str] = None

    # 추천 이유 프롬프트의 "설명"에 들어갈 개요 텍스트.
    # TODO: 목록 조회 응답에 개요가 포함되는지, 장소별 상세 조회를 따로 해야
    # 하는지 확인. 따로 해야 하면 호출 수가 장소 수만큼 늘어 하루 1,000건
    # 한도에 직접 영향
    description: Optional[str] = None


class TourEvent(BaseModel):
    """TourAPI 행사 조회 결과 하나. 행사 추천 노드의 산출물."""

    content_id: str
    name: str

    # 여행 기간과 겹치는지 비교하고 일정의 해당 날짜에 배치할 때 쓰므로
    # 문자열이 아니라 date로 둔다. TourAPI 날짜 형식 → date 변환은 TourAPIClient 담당.
    # TODO: TourAPI 날짜 형식 확인
    start_date: date
    end_date: date

    # 응답 EventInfo.venue에 쓰임.
    # TODO: 행사 장소명을 별도 필드로 주는지, 주소만 주는지 확인
    venue: Optional[str] = None

    # 행사를 일정 안에 장소처럼 배치하는 경우(응답 Place.event_end_date에 값이
    # 들어가는 경우) 응답 Place의 주소·좌표·이미지가 필요해서 둔다.
    # TODO: 행사 조회 응답에 이 값들이 포함되는지 확인
    address: Optional[str] = None
    coordinates: Optional[Coordinates] = None
    image_url: Optional[str] = None
