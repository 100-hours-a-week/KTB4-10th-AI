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
import time
import traceback
import uuid
from datetime import datetime, timedelta

from src import logs, metrics
from src.engine import graph, nodes
from src.engine.nodes import FeedbackNotApplicable, InsufficientCandidates
from src.llm import calllog
from src.llm.llm_client import LLMError
from src.models import Candidate, Itinerary, TripRequest
from src.server import adapter

MAX_CONCURRENCY = 3  # 동시에 LLM 을 부르는 작업 수. 분당 한도를 아직 모른다
MAX_QUEUE = 100
# 작업 하나에 최대 10분. 배치·글쓰기 호출의 최악을 더한 것보다 길어야 한다
# (계산은 llm_arrange.ARRANGE_TIMEOUT 주석, 확인은 test_observability)
JOB_TIMEOUT = 600
# 끝난 작업을 남겨 두는 시간. 결과 가져가기·같은 요청 재전송보다 넉넉하게
JOB_TTL = timedelta(hours=24)

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

    # 올리고 내리는 코드를 따로 두지 않고, 긁어 갈 때마다 지금 상태를 센다 —
    # 빠뜨린 경로가 있어도 숫자가 틀어지지 않는다
    metrics.JOBS_QUEUED.set_function(job_queue.qsize)
    metrics.JOBS_RUNNING.set_function(running_count)

    return [asyncio.create_task(worker()) for _ in range(MAX_CONCURRENCY)]


def running_count() -> int:
    """지금 만들고 있는 작업 수. /health 와 /metrics 가 같은 값을 본다."""
    return sum(1 for job in jobs.values() if job["status"] == "processing")


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
    request: dict,
) -> dict:
    """
    작업을 만들어 큐에 넣는다. 큐가 만원이면 QueueFull 이 올라간다.

    request 는 백엔드가 보낸 본문 그대로다 — 끝날 때 generations/ 기록에 넣는다.
    """
    _purge_expired()

    job = {
        "job_id": str(uuid.uuid4()),
        "status": "pending",
        "result": None,
        "error": None,
        # 아래는 워커·멱등 판단·기록만 본다. 응답에서는 뺀다
        "trip": trip,
        "places": places,
        "events": events,
        "fingerprint": mark,
        "request_id": request_id,
        "request": request,
        "accepted_at": datetime.now(calllog.KST),
    }
    _enqueue(job)
    return job


def accept_regeneration(
    source: dict,
    feedback: str,
    title: str | None,
    request_id: str,
    mark: str,
    request: dict,
) -> dict:
    """
    재생성 작업을 새로 만들어 큐에 넣는다. 원래 작업(source)은 고치지 않는다 —
    완료가 다시 진행 중이 되고, 실패하면 원래 데이터를 잃기 때문이다.
    """
    _purge_expired()

    job = {
        "job_id": str(uuid.uuid4()),
        "status": "pending",
        "result": None,
        "error": None,
        # 원래 작업의 조건·후보를 그대로 옮긴다
        "trip": source["trip"],
        "places": source["places"],
        "events": source["events"],
        "source_job_id": source["job_id"],
        "previous_itinerary": source["itinerary"],
        "feedback": feedback,
        "title": title,
        "fingerprint": mark,
        "request_id": request_id,
        "request": request,
        "accepted_at": datetime.now(calllog.KST),
    }
    _enqueue(job)
    return job


def _enqueue(job: dict) -> None:
    """저장소에 넣고 큐에 넣는다. 큐가 만원이면 저장소에서 다시 빼고 QueueFull 을 올린다."""
    # 저장소(jobs)에 먼저 넣는다. 큐에 먼저 넣으면 워커가 꺼냈을 때 저장소에 아직 없을 수 있다.
    jobs[job["job_id"]] = job
    try:
        job_queue.put_nowait(job["job_id"])
    except asyncio.QueueFull:
        del jobs[job["job_id"]]
        raise

    # 큐에 넣는 데 성공한 뒤에만 적는다. 먼저 적으면 QueueFull 로 jobs 에서 지워진 작업을
    # 가리키게 되어, 같은 request_id 의 재전송이 existing() 의 jobs[job_id] 에서
    # KeyError(500)가 난다
    request_map[job["request_id"]] = job["job_id"]


def _purge_expired() -> None:
    """
    접수한 지 JOB_TTL 이 지난 끝난 작업을 지운다.
    지우지 않으면 서버를 내릴 때까지 메모리에 쌓인다.
    """
    now = datetime.now(calllog.KST)
    for job_id, job in list(jobs.items()):
        if job["status"] not in ("completed", "failed"):
            continue
        if now - job["accepted_at"] > JOB_TTL:
            del jobs[job_id]
            del request_map[job["request_id"]]


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
        # 성공이든 예외든 반드시 "하나 끝났다"를 알려야
        # stop()의 job_queue.join()이 영원히 기다리지 않는다.


async def _process(job: dict) -> None:
    job["status"] = "processing"

    metrics.start_job_timing()
    # 이 작업의 LLM 기록 파일 이름을 받을 상자. 워커마다 제 문맥이라 작업끼리 섞이지 않고,
    # to_thread 로 넘긴 실도 같은 상자를 본다 (calllog._CURRENT_RUN 설명 참고)
    calllog.start_run()
    started = time.monotonic()

    # _succeed 도 try 안에 둔다. else 에 두면 그 안의 예외(응답 만들기·HTML 그리기)는
    # 아래 except 들이 못 잡고 worker() 의 루프를 끊는다 — 워커가 조용히 하나씩 사라진다
    try:
        itinerary = await asyncio.wait_for(_generate(job), JOB_TIMEOUT)
        _succeed(job, itinerary)
    except asyncio.TimeoutError:
        # 스레드는 계속 돌고 있다. wait_for 는 스레드를 죽이지 못한다
        # 그래서 LLM 쪽 최악 소요(재시도 포함)를 JOB_TIMEOUT 보다 짧게 잡아 뒀다
        # (llm_arrange.ARRANGE_TIMEOUT · llm_write.WRITE_TIMEOUT).
        _fail(
            job, "generation_timeout", "일정 생성 시간이 초과되었습니다.", "job_timeout"
        )
    except InsufficientCandidates as exc:
        _fail(job, "insufficient_candidates", str(exc), "insufficient_candidates")
    except FeedbackNotApplicable as exc:
        _reject(job, str(exc))
    except LLMError as exc:
        _fail(job, "generation_failed", str(exc), f"llm_{exc.stage}_{exc.kind}")
    except Exception as exc:
        # 예상 못 한 오류. 원인을 찾으려면 어디서 났는지가 필요하다
        detail = "".join(traceback.format_exception(exc))
        _fail(job, "generation_failed", str(exc), "application_exception", detail)
    finally:
        metrics.JOB_DURATION.observe(time.monotonic() - started)

    _save_generation(job)


def _save_generation(job: dict) -> None:
    """
    끝난 작업을 generations/ 에 남긴다 (SAVE_MODE=ON 일 때만).

    **어떤 예외든** 경고만 남긴다 (파일 쓰기 실패만이 아니다). 작업은 이미 끝났고,
    여기서 예외가 새면 worker() 의 루프가 끊겨 워커가 하나씩 사라진다 — 셋 다 사라지면
    새 작업이 영원히 pending 이고, 서버를 끌 때도 큐가 안 비어 멈춘다 (2026-10-01 재현).
    """
    try:
        # 백엔드가 조회(GET)로 받는 것과 같은 모양 (main._envelope 의 봉투)
        response = {"message": f"guidebook_{job['status']}", "data": public(job)}
        calllog.record_generation(
            request_id=job["request_id"],
            job_id=job["job_id"],
            accepted_at=job["accepted_at"],
            request=job["request"],
            llm_calls=calllog.call_files(),
            response=response,
        )
    except Exception as exc:
        # 파일 쓰기 실패가 아니면 우리 코드 문제다. 어디서 났는지 남긴다
        detail = "".join(traceback.format_exception(exc))
        logs.write(
            "WARNING",
            "generation_record_failed",
            job_id=job["job_id"],
            error=str(exc),
            traceback=detail,
        )


async def _generate(job: dict) -> Itinerary:
    """
    graph.run 은 동기 함수다. 이벤트 루프에서 그대로 부르면 그동안 서버 전체가 멈춘다.
    (다른 워커,접수,조회 모두)
    따라서 asyncio.to_thread로 별도 스레드로 넘겨서 이벤트 루프는 그동안 다른 일을 한다.
    """
    if "source_job_id" in job:
        return await asyncio.to_thread(_regenerate, job)
    return await asyncio.to_thread(
        graph.run, job["trip"], places=job["places"], events=job["events"]
    )


def _regenerate(job: dict) -> Itinerary:
    """
    재생성 1회. 후보는 원래 작업 것을 그대로 쓴다 — 피드백으로 뺀 곳은 이 작업의
    배치에서 이미 빠져 있고, 다음 재생성은 그 배치에서 시작한다 (10-17 다시 정함).
    """
    previous = job["previous_itinerary"]
    itinerary = graph.run_regeneration(
        job["trip"],
        places=job["places"],
        events=job["events"],
        previous_plan=nodes.plan_from_itinerary(previous),
        feedback=job["feedback"],
    )
    # 제목만 바꾸는 재생성은 없다 — title 이 없으면 이전 제목 그대로
    itinerary.title = job["title"] or previous.title
    return itinerary


def _succeed(job: dict, itinerary: Itinerary) -> None:
    """
    빈 일정은 여기까지 오지 않는다 — 후보가 일수보다 적으면 nodes.arrange 가,
    빈 날이 있으면 llm_arrange 의 검사가 먼저 멈춘다.
    """
    job.update(status="completed", result=adapter.to_response(job["trip"], itinerary))
    # 다음 재생성의 「이전 배치」 · 이전 제목 (#44)
    job["itinerary"] = itinerary
    metrics.observe_post_processing()
    metrics.JOBS_TOTAL.labels(result="success").inc()


def _reject(job: dict, reason: str) -> None:
    """
    재생성 피드백을 받을 수 없다. 실패로 알리되 오류로 세지 않는다 — 고장이 아니다
    (_fail 은 ERROR 로그와 오류 숫자를 올려 알림이 울릴 수 있다).
    거절한 피드백은 로그에 남긴다 — 재생성 사유 분포를 본다 (docs/design/05 · 07).
    """
    error = {"code": "feedback_not_applicable", "message": reason}
    job.update(status="failed", error=error)
    metrics.JOBS_TOTAL.labels(result="failed").inc()
    logs.write(
        "INFO",
        "feedback_rejected",
        job_id=job["job_id"],
        feedback=job["feedback"],
        reason=reason,
    )


def _fail(
    job: dict, code: str, message: str, kind: str, detail: str | None = None
) -> None:
    """
    왜 멈췄는지 작업에 적고, 로그와 /metrics 에도 남긴다.

    code 는 계약서의 오류 코드(백엔드가 본다), kind 는 우리가 원인을 가르는 이름이다
    (로그·metrics 만 본다). generation_failed 하나에 LLM 오류·파싱 오류·예상 못 한 오류가
    뭉쳐 있어서 따로 둔다. detail 은 오류 경로(traceback) — 예상 못 한 오류에만 붙인다.

    재시도 가능 여부는 담지 않는다 — 계약서가 코드별로 정해 두었고(8장),
    다시 시도할지는 백엔드가 판단한다.
    """
    job.update(status="failed", error={"code": code, "message": message})
    metrics.JOBS_TOTAL.labels(result="failed").inc()
    metrics.GENERATION_ERRORS.labels(kind=kind).inc()
    logs.write(
        "ERROR",
        "generation_failed",
        job_id=job["job_id"],
        code=code,
        kind=kind,
        error=message,
        traceback=detail,
    )
