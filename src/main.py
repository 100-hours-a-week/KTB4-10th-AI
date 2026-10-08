"""
src/main.py

백엔드가 부르는 엔드포인트. 접수하면 큐에 넣고 바로 202 로 끝나고,
일정은 워커가 뒤에서 만든다 (src/server/jobs.py).

계약서는 docs/backend/backend-AI-API.md. 응답은 전부 {"message", "data"} 봉투다.
재시도 엔드포인트는 두지 않는다 — 백엔드가 새 request_id 로 생성을 다시 부른다 (계약 9장).

띄우기:
    uv run uvicorn src.main:app --reload

서버는 **기본으로 실제 모델을 부른다** (요청 1건마다 과금). LLM_MODE=fake 일 때만 가짜로 돈다 —
클라우드팀 부하 테스트용. 켜져 있으면 시작 로그 · /health · /metrics 에 드러나게 했다.

/metrics 는 Prometheus 가 긁어 간다. 숫자 정의는 src/metrics.py, 로그 형식은 src/logs.py.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src import logs, metrics
from src.llm import calllog, fake_llm, llm_plan
from src.server import adapter, jobs


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 설정이 틀렸으면 첫 요청에서야 실패한다. 켜기 전에 막는다
    mode = llm_plan.llm_mode()  # real or fake
    save = calllog.save_mode()  # True or False (ON or OFF)
    if mode == "fake":
        # 운영 서버에서 부하 테스트를 한다. 가짜로 켜진 걸 모르고 지나가면 안 된다
        logs.write(
            "WARNING",
            "fake_llm_mode",
            message=(
                "가짜 LLM 모드 — 실제 모델을 부르지 않는다. "
                "테스트 뒤 LLM_MODE 를 real로 바꾸고 재기동할 것"
            ),
            delay_sec=fake_llm.delay_seconds(),
        )
    else:
        llm_plan.load_api_key(llm_plan.ANTHROPIC_KEY_VAR)
    metrics.LLM_MODE.labels(llm_mode=mode).set(1)

    # 기록이 켜졌는지는 여기서 본다 (서버는 OFF 여야 한다)
    save_label = "ON" if save else "OFF"
    logs.write("INFO", "server_start", llm_mode=mode, save_mode=save_label)

    workers = jobs.start()  # 큐 생성 + 워커 3개

    # 여기서 서버가 요청을 받는 동안 멈춰 있다. (yield 앞이 서버 켤 때, 뒤가 끌 때)
    yield
    # 끌 때 : 큐가 빌 때까지 기다린 뒤 워커 취소하기 (이미 시작된 호출을 버리지 않으려고)
    await jobs.stop(workers)


app = FastAPI(title="가이드북 생성 서버", lifespan=lifespan)


@app.middleware("http")
async def _count_requests(request: Request, call_next):
    """
    HTTP 요청 수 · 시간 · 응답 코드를 센다.

    path 는 실제 주소가 아니라 **경로 틀**(/guidebooks-generations/{job_id})로 적는다.
    job_id 마다 따로 세면 숫자 종류가 요청 수만큼 늘어 Prometheus 가 감당하지 못한다.
    /metrics 자체는 세지 않는다 — Prometheus 가 몇 초마다 긁어 가서 숫자가 그것으로 덮인다.
    """
    started = time.monotonic()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        # 예상 못 한 오류는 이 바깥(_unexpected_error)에서 500 으로 바뀐다. 여기선 500 으로 센다
        status = 500
        raise
    finally:
        route = request.scope.get("route")
        path = route.path if route is not None else "unmatched"
        if path != "/metrics":
            elapsed = time.monotonic() - started
            metrics.HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
            metrics.HTTP_DURATION.labels(request.method, path).observe(elapsed)
    return response


# 응답 형태를 통일시켜 준다.
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
    logs.write(
        "ERROR",
        "application_exception",
        path=request.url.path,
        error=str(exc),
        traceback="".join(traceback.format_exception(exc)),
    )
    return JSONResponse(status_code=500, content=_envelope("internal_server_error"))


@app.get("/health")
async def health() -> dict:
    """
    계약에 없는 우리 것. 큐가 밀리는지, 가짜 모드로 켜져 있지 않은지 보려고 둔다.
    부하 테스트가 끝나면 llm_mode 가 real 인지 여기서 확인한다.
    """
    return {
        "status": "ok",
        "llm_mode": llm_plan.llm_mode(),
        "jobs": len(jobs.jobs),
        "queue_size": jobs.job_queue.qsize(),
        "running": jobs.running_count(),
    }


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    """
    Prometheus 형식의 숫자. 인프라의 Prometheus 가 긁어 간다 (외부 접근은 인프라가 막는다).

    make_asgi_app() 을 붙이지 않는 이유: 하위 앱으로 달면 /metrics 가 /metrics/ 로
    넘겨지는(307) 한 단계가 끼어서, 긁는 쪽 설정에 따라 실패한다.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/guidebooks-generations", status_code=202)
async def create_generation(payload: dict) -> dict:
    """
    본문을 dict 로 그대로 받는다. 계약서 2장의 필드가 **최상위에 평평하게** 온다.

    스키마를 pydantic 으로 박지 않는 이유: 계약서는 틀린 곳마다 다른 오류 코드를 요구한다
    (invalid_date_range · invalid_preference_mapping · invalid_request).
    pydantic 에 맡기면 전부 한 가지 검증 오류로 뭉쳐 온다.
    그래서 검증은 adapter 가 항목별로 한다.

    엔드포인트 넷이 모두 async def 인 이유: def 면 FastAPI 가 스레드 여러 개에서 동시에
    돌린다. 그러면 같은 request_id 두 개가 existing() 을 함께 통과해 작업이 둘 생기고
    (= 두 번 과금), 이벤트 루프 전용인 asyncio.Queue 를 다른 스레드가 만지게 된다.
    async def 는 이벤트 루프 하나에서 await 없이 끝까지 도므로 끼어들 틈이 없다.
    **안에서 오래 걸리는 일을 하면 안 된다** — 그동안 서버 전체가 멈춘다.
    """
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:  # 없거나 빈 문자열이면
        raise HTTPException(400, detail="invalid_request")

    mark = jobs.fingerprint(payload)

    # 응답이 유실되면 백엔드가 같은 번호로 다시 보낸다. 새 작업을 만들지 않는다
    try:
        already = jobs.existing(request_id, mark)  # return job
    except jobs.RequestIdConflict:
        raise HTTPException(409, detail="request_id_conflict") from None
    if already is not None:
        return _envelope("guidebook_accepted", jobs.accepted(already))

    # 양식 바꾸기와 검증
    try:
        trip = adapter.to_trip_request(payload)
        places, events = adapter.to_candidates(payload.get("contents"))
    except adapter.InputError as exc:
        # 계약 응답에는 code 만 나가므로 이유는 서버 로그에 남긴다
        logs.write(
            "WARNING",
            "request_rejected",
            request_id=request_id,
            code=exc.code,
            reason=exc.reason,
        )
        raise HTTPException(400, detail=exc.code) from exc

    # 큐에 작업 넣기
    try:
        job = jobs.accept(trip, places, events, request_id, mark, payload)
    except asyncio.QueueFull:
        # 부하 테스트에서 가장 먼저 볼 숫자다 (클라우드팀 요청: 거절 건수)
        metrics.JOBS_TOTAL.labels(result="rejected").inc()
        logs.write("WARNING", "queue_full", request_id=request_id)
        raise HTTPException(
            503, detail="service_unavailable", headers={"Retry-After": "60"}
        ) from None

    return _envelope("guidebook_accepted", jobs.accepted(job))


@app.get("/guidebooks-generations/{job_id}")
async def get_generation(job_id: str) -> dict:
    job = jobs.jobs.get(job_id)  # 저장소 jobs에서 꺼낸다.
    if job is None:  # 없으면
        raise HTTPException(404, detail="job_not_found")

    # pending -> guidebook_pending, completed -> guidebook_completed ... (계약 5~8장)
    return _envelope(f"guidebook_{job['status']}", jobs.public(job))
