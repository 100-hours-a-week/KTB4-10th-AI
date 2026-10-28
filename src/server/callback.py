"""
src/server/callback.py

작업의 진행 단계와 결과를 백엔드 콜백 API 로 알린다 (docs/backend/callback_message.txt).
백엔드는 받은 진행 상황을 SSE 로 프론트에 흘린다. GET 조회는 그대로 남아 있다.

**알림마다 따로 보낸다.** 한 줄로 세우면 진행 알림을 재시도하는 동안 완료 알림이 막힌다.
뒤바뀐 순서는 백엔드가 sequence 로 거른다.

**콜백이 끝내 실패해도 작업 상태는 바꾸지 않는다.** 백엔드는 자기 제한 시간이 지나면
실패로 처리한다.

주소·토큰 환경변수가 없으면 아무것도 보내지 않는다 — 로컬·테스트·CI 에는 받을 백엔드가 없다.
"""

from __future__ import annotations

import asyncio
import os
import traceback
import uuid

import httpx

from src import logs, metrics

CALLBACK_PATH = "/internal/v1/ai/generation-events"
HTTP_TIMEOUT = 5
# 재시도 사이 대기(초). 개수가 재시도 횟수다 (백엔드와 합의, 2026-10-28)
FINAL_RETRY_DELAYS = (1, 2, 4, 8)
PROGRESS_RETRY_DELAYS = (1,)
# 429 의 Retry-After 를 따르되 이보다 길게는 기다리지 않는다 — 서버 종료가 늦어진다
MAX_RETRY_AFTER = 30
# 서버를 끌 때 보내는 중인 알림을 기다리는 시간
SHUTDOWN_WAIT = 10

# 문구는 FE 와 정하면 바꾼다 (지금은 백엔드 예시 문구 기준)
PROGRESS_MESSAGES = {
    "arranging": "여행 일정을 구성하고 있어요.",
    "writing": "가이드북 문구를 작성하고 있어요.",
}

# start() 에서 주소·토큰을 넣어 만든다. None 이면 꺼진 상태
_client: httpx.AsyncClient | None = None
# 보내는 중인 알림. stop() 이 기다린다
_sending: set[asyncio.Task] = set()


def start() -> None:
    """서버가 켜질 때. 환경변수를 읽고, 있으면 HTTP 클라이언트를 연다."""
    global _client
    settings = _read_settings()
    if settings is None:
        return
    base_url, token = settings
    _client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=HTTP_TIMEOUT,
    )


def _read_settings() -> tuple[str, str] | None:
    base_url = os.environ.get("BE_CALLBACK_BASE_URL", "").strip()
    token = os.environ.get("AI_CALLBACK_TOKEN", "").strip()
    if base_url and token:
        return base_url, token

    # 운영에서 빠뜨리면 오류 없이 콜백만 안 나가고, 백엔드는 모든 작업을 타임아웃으로 본다.
    # 배포 뒤 로그로 알아차리게 남긴다
    if base_url or token:
        logs.write(
            "WARNING",
            "callback_misconfigured",
            has_base_url=bool(base_url),
            has_token=bool(token),
        )
    else:
        logs.write("INFO", "callback_disabled")
    return None


async def stop() -> None:
    """서버가 꺼질 때. 보내는 중인 알림을 잠깐 기다린 뒤 닫는다."""
    global _client
    if _sending:
        await asyncio.wait(_sending, timeout=SHUTDOWN_WAIT)
    if _client is not None:
        await _client.aclose()
        _client = None


def notify_progress(job: dict, step: str) -> None:
    """처리 단계가 바뀌었다. 이벤트 루프에서 불러야 한다 (sequence 를 루프 하나에서만 올리려고)."""
    if _client is None:
        return
    # 시간 초과로 먼저 failed 가 돼도 그래프 스레드는 계속 돈다. 끝난 작업엔 보내지 않는다
    if job["status"] != "processing":
        return
    progress = {"step": step, "message": PROGRESS_MESSAGES[step]}
    event = _event(job, "guidebook_processing", progress)
    _send_later(event, "progress", PROGRESS_RETRY_DELAYS)


def notify_final(job: dict) -> None:
    """작업이 끝났다 (completed · failed). 결과·오류는 GET 조회와 같은 모양으로 나간다."""
    if _client is None:
        return
    event = _event(job, f"guidebook_{job['status']}", None)
    _send_later(event, "final", FINAL_RETRY_DELAYS)


def _event(job: dict, message: str, progress: dict | None) -> dict:
    """
    알림 본문. event_id · sequence 를 **여기서 한 번** 정한다 —
    재전송할 때 같은 본문을 보내야 백엔드가 중복을 가린다.
    """
    sequence = job.get("callback_sequence", 0) + 1
    job["callback_sequence"] = sequence
    data = {
        "event_id": str(uuid.uuid4()),
        "request_id": job["request_id"],
        "sequence": sequence,
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": progress,
        "result": job["result"],
        "error": job["error"],
    }
    return {"message": message, "data": data}


def _send_later(event: dict, kind: str, delays: tuple[int, ...]) -> None:
    task = asyncio.create_task(_send(event, kind, delays))
    _sending.add(task)
    task.add_done_callback(_sending.discard)


async def _send(event: dict, kind: str, delays: tuple[int, ...]) -> None:
    """
    보내고, 정해진 만큼 재시도한다. **어떤 예외도 밖으로 내보내지 않는다** —
    create_task 로 띄운 작업의 예외는 아무도 받지 않아 조용히 묻힌다.
    """
    try:
        result = await _send_with_retry(event, delays)
    except Exception as exc:
        result = "gave_up"
        detail = "".join(traceback.format_exception(exc))
        logs.write(
            "ERROR", "callback_crashed", **_ids(event), error=str(exc), traceback=detail
        )
    metrics.CALLBACKS_TOTAL.labels(kind=kind, result=result).inc()

    if result == "gave_up":
        # 완료·실패를 못 보내면 백엔드는 이 작업을 타임아웃 실패로 본다
        level = "ERROR" if kind == "final" else "WARNING"
        logs.write(level, "callback_gave_up", **_ids(event))


async def _send_with_retry(event: dict, delays: tuple[int, ...]) -> str:
    """delivered · rejected · gave_up 중 하나를 돌려준다."""
    max_attempts = len(delays) + 1
    for attempt in range(1, max_attempts + 1):
        outcome, status, retry_after = await _post(event)
        _log_attempt(event, attempt, outcome, status)
        if outcome != "retry":
            return outcome
        if attempt == max_attempts:
            return "gave_up"
        wait = retry_after if retry_after is not None else delays[attempt - 1]
        await asyncio.sleep(wait)


async def _post(event: dict) -> tuple[str, int | None, float | None]:
    """한 번 보낸다. (결과, HTTP 상태, 429 일 때 기다릴 초)."""
    try:
        response = await _client.post(CALLBACK_PATH, json=event)
    except httpx.TransportError:
        # 연결 실패·타임아웃 (TimeoutException 도 TransportError 의 한 갈래)
        return "retry", None, None

    status = response.status_code
    if 200 <= status < 300:
        return "delivered", status, None
    if status == 429:
        return "retry", status, _retry_after(response)
    if status >= 500:
        return "retry", status, None
    # 그 밖의 4xx — 다시 보내도 같은 답이다
    return "rejected", status, None


def _retry_after(response: httpx.Response) -> float | None:
    """초로 온 Retry-After 만 읽는다. 날짜 형식이거나 없으면 None (정해 둔 간격을 쓴다)."""
    value = response.headers.get("Retry-After", "")
    if not value.isdigit():
        return None
    return min(int(value), MAX_RETRY_AFTER)


def _log_attempt(event: dict, attempt: int, outcome: str, status: int | None) -> None:
    # 401·403 은 토큰 설정이 틀렸다는 뜻이라 눈에 띄게
    if status in (401, 403):
        level = "ERROR"
    elif outcome == "delivered":
        level = "INFO"
    else:
        level = "WARNING"
    logs.write(
        level,
        "callback_sent",
        **_ids(event),
        attempt=attempt,
        outcome=outcome,
        http_status=status,
    )


def _ids(event: dict) -> dict:
    """로그에 남길 식별자. 토큰은 넣지 않는다."""
    data = event["data"]
    return {
        "event_id": data["event_id"],
        "request_id": data["request_id"],
        "job_id": data["job_id"],
        "sequence": data["sequence"],
    }
