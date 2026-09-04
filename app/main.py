"""
app/main.py

확정 명세서의 6개 엔드포인트를 실제로 호출 가능하게 연결한 스켈레톤
서버입니다. LLM/TourAPI 연동은 없고, 대신 백그라운드 태스크가 파이프
라인 진행을 흉내 냅니다 (실제 폴링 흐름을 curl로 확인해보기 위함).

테스트 편의를 위한 트리거: region.city에 '실패'라는 글자가 들어가면
building_itinerary 단계에서 일부러 실패하도록 만들어뒀습니다.
(예: city="실패시" 로 요청하면 실패 시나리오 재현 가능)
"""

from __future__ import annotations
import asyncio

from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from app import state
from app.errors import (
    InvalidDateRange,
    InvalidPreferenceMapping,
    validate_date_range,
    validate_preference_mapping,
)
from app.models import GenerateRequest, RegenerateRequest, RegionRecommendRequest

app = FastAPI(title="AI 서버 (스켈레톤)")


# ============================================================
# 예외 -> HTTP 응답 매핑
# 라우터 본문에서는 state.py/errors.py의 예외를 그냥 발생시키기만
# 하고, 실제 상태 코드/메시지 변환은 전부 여기 한곳에 모아둡니다.
# ============================================================

@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    # FastAPI 기본 동작은 422인데, 우리 명세서는 422를 quota_exceeded
    # 전용으로 예약해뒀으므로 여기서 400으로 바꿔치기합니다.
    return JSONResponse(status_code=400, content={"message": "missing_required_field", "data": None})


@app.exception_handler(InvalidDateRange)
async def handle_invalid_date_range(request: Request, exc: InvalidDateRange):
    return JSONResponse(status_code=400, content={"message": "invalid_date_range", "data": None})


@app.exception_handler(InvalidPreferenceMapping)
async def handle_invalid_preference_mapping(request: Request, exc: InvalidPreferenceMapping):
    return JSONResponse(status_code=400, content={"message": "invalid_preference_mapping", "data": None})


@app.exception_handler(state.JobNotFound)
async def handle_job_not_found(request: Request, exc: state.JobNotFound):
    return JSONResponse(status_code=404, content={"message": "guidebook_not_found", "data": None})


@app.exception_handler(state.InvalidState)
async def handle_invalid_state(request: Request, exc: state.InvalidState):
    return JSONResponse(status_code=409, content={"message": "invalid_state", "data": None})


@app.exception_handler(state.RetryLimitExceeded)
async def handle_retry_limit(request: Request, exc: state.RetryLimitExceeded):
    return JSONResponse(status_code=409, content={"message": "retry_limit_exceeded", "data": None})


@app.exception_handler(state.JobAlreadyRunning)
async def handle_job_running(request: Request, exc: state.JobAlreadyRunning):
    return JSONResponse(status_code=409, content={"message": "job_already_running", "data": {"job_id": exc.job_id}})


@app.exception_handler(state.QuotaExceeded)
async def handle_quota_exceeded(request: Request, exc: state.QuotaExceeded):
    return JSONResponse(status_code=422, content={"message": "quota_exceeded", "data": None})


# ============================================================
# 백그라운드 파이프라인 시뮬레이션
# ============================================================

STEP_ORDER = ["finding_places", "checking_events", "building_itinerary", "writing_reasons"]


def _build_mock_completed_payload(job: dict) -> dict:
    conditions = job["conditions"]
    region = conditions.get("region", {})
    return {
        "status": "completed",
        "summary": {
            "region": region.get("city", "경주"),
            "period": f"{conditions.get('start_date')} - {conditions.get('end_date')}",
            "duration_label": f"{conditions.get('people_count', 1)}명",
        },
        "days": [{"day": 1, "representative": "첨성대", "place_count": 1}],
        "itinerary": [{
            "day": 1,
            "date": "2026-10-12",
            "places": [{
                "order": 1, "time": "09:00", "content_id": "126508",
                "name": "첨성대", "category": "문화",
                "description": "동양에서 가장 오래된 천문대",
                "recommend_reason": "조용한 곳을 선호하셔서 이른 아침으로 배치했어요",
                "tip": "오전 9시 이전이면 한산해요", "duration_minutes": 60,
                "address": "경상북도 경주시 인왕동",
                "coordinates": {"lat": 35.8347, "lng": 129.2190},
                "image_url": "https://example.com/126508.jpg",
                "source": "tourapi", "event_end_date": None,
            }],
        }],
        "events": [{"event_id": "ev_221", "name": "신라문화제",
                     "period": "26.10.12 - 26.10.14", "venue": "경주엑스포대공원"}],
        "conditions": conditions,
    }


async def simulate_pipeline(job_id: str, should_fail: bool) -> None:
    for key in STEP_ORDER:
        state.advance_step(job_id, key, "running")
        await asyncio.sleep(1)
        if should_fail and key == "building_itinerary":
            state.fail_job(job_id, "timeout", "일정 생성 중 시간이 초과되었습니다.")
            return
        state.advance_step(job_id, key, "done")

    job = state.get_job(job_id)
    state.complete_job(job_id, _build_mock_completed_payload(job))


# ============================================================
# 라우터
# ============================================================

@app.get("/health")
def health():
    return {"message": "health_ok", "data": None}


@app.post("/guidebooks", status_code=202)
async def create_guidebook(req: GenerateRequest, background_tasks: BackgroundTasks):
    validate_date_range(req.start_date, req.end_date)
    validate_preference_mapping(req.preferences)

    job = state.create_job(req.model_dump())
    should_fail = "실패" in req.region.city
    background_tasks.add_task(simulate_pipeline, job["job_id"], should_fail)

    return {
        "message": "guidebook_accepted",
        "data": {"job_id": job["job_id"], "status": "pending", "remaining_quota": state.get_remaining_quota()},
    }


@app.get("/guidebooks/{job_id}")
def get_guidebook_status(job_id: str):
    job = state.get_job(job_id)  # 없으면 JobNotFound -> 404 핸들러가 처리

    if job["status"] == "completed":
        gb = state.get_guidebook(job["guidebook_id"])
        return {"message": "guidebook_completed", "data": gb}

    if job["status"] == "failed":
        return {
            "message": "guidebook_failed",
            "data": {
                "job_id": job["job_id"], "status": "failed",
                "steps": job["steps"], "error": job["error"], "retry_count": job["retry_count"],
            },
        }

    # pending 또는 processing
    return {
        "message": "guidebook_processing",
        "data": {"job_id": job["job_id"], "status": "processing", "progress": {"steps": job["steps"]}},
    }


@app.post("/guidebooks/{job_id}/retry", status_code=202)
async def retry_guidebook(job_id: str, background_tasks: BackgroundTasks):
    job = state.retry_job(job_id)  # 404/409/409 -> 각 핸들러가 처리
    conditions = job["conditions"]
    should_fail = "실패" in conditions.get("region", {}).get("city", "")
    background_tasks.add_task(simulate_pipeline, job_id, should_fail)

    return {"message": "retry_accepted", "data": {"job_id": job["job_id"], "status": "pending", "retry_count": job["retry_count"]}}


@app.post("/guidebooks/{guidebook_id}/regenerate", status_code=202)
async def regenerate_guidebook(guidebook_id: str, req: RegenerateRequest, background_tasks: BackgroundTasks):
    job = state.regenerate_guidebook(guidebook_id, req.feedback, req.title)  # 404 -> 핸들러 처리
    background_tasks.add_task(simulate_pipeline, job["job_id"], False)

    return {"message": "regenerate_accepted", "data": {"job_id": job["job_id"], "status": "pending"}}


@app.post("/guidebooks/regions-recommendations")
def recommend_regions(req: RegionRecommendRequest):
    validate_preference_mapping(req.preferences)

    picked = ", ".join(req.preferences.large_category)
    return {
        "message": "region_recommended",
        "data": {
            "regions": [
                {"name": "경주", "province": "경상북도", "city": "경주시",
                 "recommend_reason": f"{picked} 취향에 잘 맞는 역사·문화 도시예요."},
                {"name": "안동", "province": "경상북도", "city": "안동시",
                 "recommend_reason": "전통 마을이 잘 보존되어 있어요."},
                {"name": "강진", "province": "전라남도", "city": "강진군",
                 "recommend_reason": "유적과 바다가 가까이 있어요."},
            ]
        },
    }
