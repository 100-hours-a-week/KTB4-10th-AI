"""
src/server/jobs.py

작업 하나가 접수되어 끝날 때까지. **상태를 바꾸는 코드는 전부 여기 있다** —
엔드포인트(src/main.py)는 읽고 넣기만 한다.

접수는 즉시 202 로 끝나야 하는데 일정 하나 만드는 데 1분 가까이 걸린다.
그래서 큐에 넣고 워커가 순서대로 꺼내 돌린다.

상태 이름은 계약서(docs/backend/backend-AI-API.md 4장)를 그대로 쓴다 —
안에서 다른 이름을 쓰면 나갈 때마다 옮겨 적어야 한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

from src.engine import graph
from src.engine.nodes import InsufficientCandidates
from src.models import Candidate, Itinerary, TripRequest
from src.server import adapter

MAX_CONCURRENCY = 3  # 동시에 LLM 을 부르는 작업 수. 분당 한도를 아직 모른다
MAX_QUEUE = 100
JOB_TIMEOUT = 300  # 작업 하나에 최대 5분

# 작업 하나 = 딕셔너리 하나. 서버를 내리면 사라진다.
jobs: dict[str, dict] = {}

# request_id -> job_id. 응답이 유실되면 백엔드가 **같은 번호로 다시** 보낸다 (계약 9장).
# 그때 새 작업을 만들면 같은 일정을 두 번 만들게 된다.
request_map: dict[str, str] = {}

# 이벤트 루프가 돌기 시작한 뒤에야 만들 수 있어서 start() 에서 채운다.
job_queue: asyncio.Queue = None  # type: ignore[assignment]


class RequestIdConflict(Exception):
    """같은 request_id 로 다른 내용이 왔다 (계약 3장, 409)."""


def start() -> list[asyncio.Task]:
    """큐를 만들고 워커를 띄운다. 서버가 올라올 때 한 번."""
    global job_queue
    job_queue = asyncio.Queue(maxsize=MAX_QUEUE)
    return [asyncio.create_task(worker()) for _ in range(MAX_CONCURRENCY)]


async def stop(workers: list[asyncio.Task]) -> None:
    """큐가 빌 때까지 기다린 뒤 워커를 정리한다 — 돈이 나간 호출을 버리지 않으려고."""
    await job_queue.join()
    for task in workers:
        task.cancel()


def fingerprint(payload: dict) -> str:
    """요청 본문의 지문. 같은 request_id 에 다른 내용이 왔는지 이것으로 가른다."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def existing(request_id: str, mark: str) -> dict | None:
    """
    같은 request_id 로 이미 접수된 작업. 없으면 None.

    번호가 같은데 내용이 다르면 둘이 섞이지 않게 거절한다 (RequestIdConflict).
    """
    job_id = request_map.get(request_id)
    if job_id is None:
        return None

    job = jobs[job_id]
    if job["fingerprint"] != mark:
        raise RequestIdConflict

    return job


def accept(
    trip: TripRequest,
    places: list[Candidate],
    events: list[Candidate],
    request_id: str,
    mark: str,
) -> dict:
    """작업을 만들어 큐에 넣는다. 큐가 만원이면 QueueFull 이 올라간다."""
    job = {
        "job_id": str(uuid.uuid4()),
        "status": "pending",
        "result": None,
        "error": None,
        # 아래 넷은 워커와 멱등 판단만 본다. 응답에서는 뺀다
        "trip": trip,
        "places": places,
        "events": events,
        "fingerprint": mark,
    }

    # 저장소에 먼저 넣는다. 큐에 먼저 넣으면 워커가 꺼냈을 때 아직 없을 수 있다.
    jobs[job["job_id"]] = job
    try:
        job_queue.put_nowait(job["job_id"])
    except asyncio.QueueFull:
        del jobs[job["job_id"]]
        raise

    request_map[request_id] = job["job_id"]
    return job


def accepted(job: dict) -> dict:
    """계약서 3장 접수 응답의 data. 조회 응답(public)과 달리 result·error 가 없다."""
    return {"job_id": job["job_id"], "status": job["status"]}


def public(job: dict) -> dict:
    """계약서 5~8장의 data 부분. 우리만 쓰는 칸은 뺀다."""
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "result": job["result"],
        "error": job["error"],
    }


async def worker() -> None:
    """큐에서 하나 꺼내 일정을 만들고, 성공이든 실패든 작업에 적는다."""
    while True:
        job_id = await job_queue.get()
        try:
            await _process(jobs[job_id])
        finally:
            job_queue.task_done()


async def _process(job: dict) -> None:
    job["status"] = "processing"
    try:
        itinerary = await asyncio.wait_for(_generate(job), JOB_TIMEOUT)
    except asyncio.TimeoutError:
        # 실은 계속 돌고 있다. wait_for 는 스레드를 죽이지 못한다 —
        # 그래서 LLM 쪽 타임아웃을 JOB_TIMEOUT 보다 낮게 잡아 뒀다 (llm_plan.CLAUDE_TIMEOUT).
        _fail(job, "generation_timeout", "일정 생성 시간이 초과되었습니다.")
    except InsufficientCandidates as exc:
        _fail(job, "insufficient_candidates", str(exc))
    except Exception as exc:
        # 모델 호출·응답 검사 실패(LLMError)도 여기로 온다
        _fail(job, "generation_failed", str(exc))
    else:
        _succeed(job, itinerary)


async def _generate(job: dict) -> Itinerary:
    """
    graph.run 은 동기 함수다. 그대로 부르면 그 1분 동안 이벤트 루프가 멈춰
    워커 셋이 전부 선다 — 다른 실로 넘긴다.
    """
    return await asyncio.to_thread(
        graph.run, job["trip"], places=job["places"], events=job["events"]
    )


def _succeed(job: dict, itinerary: Itinerary) -> None:
    """
    빈 일정은 여기까지 오지 않는다 — 후보가 일수보다 적으면 nodes.plan 이,
    빈 날이 있으면 llm_plan 의 검사가 먼저 멈춘다.
    """
    job.update(status="completed", result=adapter.to_response(job["trip"], itinerary))


def _fail(job: dict, code: str, message: str) -> None:
    """
    왜 멈췄는지 적는다.

    재시도 가능 여부는 담지 않는다 — 계약서가 코드별로 정해 두었고(8장),
    다시 시도할지는 백엔드가 판단한다.
    """
    job.update(status="failed", error={"code": code, "message": message})
