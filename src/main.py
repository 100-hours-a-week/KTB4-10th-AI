"""
src/main.py

백엔드가 부르는 엔드포인트. 접수하면 큐에 넣고 바로 202 로 끝나고,
일정은 워커가 뒤에서 만든다 (src/server/jobs.py).

계약서는 docs/backend/backend-AI-API.md. 응답은 전부 {"message", "data"} 봉투다.
재시도 엔드포인트는 두지 않는다 — 백엔드가 새 request_id 로 생성을 다시 부른다 (계약 9장).

띄우기:
    uv run uvicorn src.main:app --reload

서버는 **항상 실제 모델을 부른다** (요청 1건마다 과금). 가짜는 src 에 없다 — tools/fake_llm.py.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.llm import llm_plan
from src.server import adapter, jobs


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버는 항상 실제 모델을 부른다. 키가 없으면 첫 요청에서야 실패하므로 켜기 전에 막는다
    llm_plan.load_api_key(llm_plan.ANTHROPIC_KEY_VAR)
    workers = jobs.start()
    yield
    await jobs.stop(workers)


app = FastAPI(title="가이드북 생성 서버", lifespan=lifespan)


def _envelope(message: str, data: dict | None = None) -> dict:
    return {"message": message, "data": data}


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    """오류도 같은 봉투로 낸다 (계약 3장). detail 에 계약서의 message 값을 담아 둔다."""
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(exc.detail),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
def _body_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """바디 형식이 틀린 것. 계약에 422 가 없어 400 invalid_request 로 낸다."""
    return JSONResponse(status_code=400, content=_envelope("invalid_request"))


@app.exception_handler(Exception)
def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """
    예상 못 한 오류도 같은 봉투로 낸다 (계약 3장 500 internal_server_error).

    FastAPI 기본값은 {"detail": ...} 라 백엔드가 message 를 못 읽는다. 원인은 계약
    응답에 담지 않고 서버 로그에 남긴다 — 내부 사정이 바깥으로 새지 않게.
    """
    traceback.print_exception(exc, file=sys.stderr)
    return JSONResponse(status_code=500, content=_envelope("internal_server_error"))


@app.get("/health")
def health() -> dict:
    """계약에 없는 우리 것. 큐가 밀리는지 보려고 둔다."""
    running = sum(1 for job in jobs.jobs.values() if job["status"] == "processing")
    return {
        "status": "ok",
        "jobs": len(jobs.jobs),
        "queue_size": jobs.job_queue.qsize(),
        "running": running,
    }


@app.post("/guidebooks-generations", status_code=202)
def create_generation(payload: dict) -> dict:
    """
    본문을 dict 로 그대로 받는다. 계약서 2장의 필드가 **최상위에 평평하게** 온다.

    스키마를 pydantic 으로 박지 않는 이유: 계약서는 틀린 곳마다 다른 오류 코드를 요구한다
    (invalid_date_range · invalid_preference_mapping · invalid_request). pydantic 에 맡기면
    전부 한 가지 검증 오류로 뭉쳐 온다. 그래서 검증은 adapter 가 항목별로 한다.
    """
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise HTTPException(400, detail="invalid_request")

    mark = jobs.fingerprint(payload)

    # 응답이 유실되면 백엔드가 같은 번호로 다시 보낸다. 새 작업을 만들지 않는다
    try:
        already = jobs.existing(request_id, mark)
    except jobs.RequestIdConflict:
        raise HTTPException(409, detail="request_id_conflict") from None
    if already is not None:
        return _envelope("guidebook_accepted", jobs.accepted(already))

    try:
        trip = adapter.to_trip_request(payload)
        places, events = adapter.to_candidates(payload.get("contents"))
    except adapter.InputError as exc:
        # 계약 응답에는 code 만 나가므로 이유는 서버 로그에 남긴다
        print(f"[접수 거절] {exc.code} · {exc.reason}", file=sys.stderr)
        raise HTTPException(400, detail=exc.code) from exc

    try:
        job = jobs.accept(trip, places, events, request_id, mark)
    except asyncio.QueueFull:
        raise HTTPException(
            503, detail="service_unavailable", headers={"Retry-After": "60"}
        ) from None

    return _envelope("guidebook_accepted", jobs.accepted(job))


@app.get("/guidebooks-generations/{job_id}")
def get_generation(job_id: str) -> dict:
    job = jobs.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, detail="job_not_found")

    # pending -> guidebook_pending, completed -> guidebook_completed ... (계약 5~8장)
    return _envelope(f"guidebook_{job['status']}", jobs.public(job))
