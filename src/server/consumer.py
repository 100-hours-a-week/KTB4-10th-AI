"""
src/server/consumer.py

RabbitMQ 큐(guidebook.generation)에서 생성·재생성 요청을 꺼내 작업으로 돌린다.
계약은 BE 문서(docs/mq-transition.md 3·4장). HTTP 접수(src/main.py)는 전환이 끝날 때까지 같이 둔다.

**처리 칸이 빌 때만 꺼낸다.** 지금은 BE 가 동시 3건만 보내 주지만, MQ 로 바뀌면 그 제한이 없어진다.
받는 대로 꺼내 메모리에 쌓으면 재시작할 때 잃는다 — 기다리는 요청은 RabbitMQ 에 남겨 둔다.
prefetch 1 이라 칸을 기다리는 동안 브로커는 다음 메시지를 주지 않는다.

**ACK 는 작업을 DB 에 저장한 뒤, 일정 생성 전에 보낸다.** 생성(최대 10분) 동안 메시지를 잡고 있으면
연결이 끊길 때 재배달돼 LLM 비용이 두 번 나간다. ACK 뒤의 실패는 FAILED 콜백으로 알린다.

큐·교환기는 BE 가 선언한다 (quorum · retry · DLX 설정). 여기서 다른 설정으로 선언하면 브로커가
채널을 닫으므로, 이름으로 찾기만 한다. 로컬에서는 scripts/mq_local.py setup 이 BE 대신 만든다.

작업 상태·중복·재생성 후보(A안)는 MySQL 에 남긴다 (src/server/store.py).
RABBITMQ_URL 을 넣으면 MYSQL_URL 도 있어야 한다.
RABBITMQ_URL 이 비면 둘 다 켜지 않는다 — HTTP 로만 받는 지금 환경 그대로다.
"""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from dataclasses import dataclass

import aio_pika
from aio_pika.abc import (
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractRobustConnection,
)

from src import logs
from src.models import Candidate
from src.server import adapter, callback, jobs, store

URL_VAR = "RABBITMQ_URL"
QUEUE = "guidebook.generation"
DEAD_LETTER_EXCHANGE = "guidebook.generation.dlx"
DEAD_LETTER_ROUTING_KEY = "guidebook.generation.failed"
SCHEMA_VERSION = 1
JOB_TYPES = ("INITIAL", "REGENERATION")
# DB 의 lease_until = 받은 시각 + 이만큼. 작업 제한(10분)에 콜백·기록 여유 5분
LEASE_SECONDS = jobs.JOB_TIMEOUT + 300

_connection: AbstractRobustConnection | None = None
_dead_letter: AbstractExchange | None = None
# 처리 칸. 수는 JOB_WORKERS 지만 HTTP 워커와 칸을 나눠 쓰지는 않는다 — 두 경로로 동시에 받으면
# 최대 2배가 돈다. BE 는 환경마다 한 경로만 쓰고, 전환이 끝나면 HTTP 쪽을 지운다

_slots: asyncio.Semaphore | None = None
# 돌고 있는 작업. stop() 이 끝날 때까지 기다린다
_running: set[asyncio.Task] = set()


class InvalidMessage(Exception):
    """메시지 겉봉(JSON·schemaVersion·jobType·번호)이 틀렸다. 다시 받아도 같으니 DLQ 로 보낸다."""


@dataclass
class Envelope:
    message_id: str
    job_id: int  # BE generation_jobs.id. 콜백의 backend_job_id (6단계)
    attempt: int
    job_type: str
    input: dict


def rabbitmq_url() -> str | None:
    return os.environ.get(URL_VAR, "").strip() or None


async def start() -> None:
    """
    서버가 켜질 때. 연결이 안 되거나 큐가 없으면 여기서 멈춘다 — 주소를 넣었는데 못 받는 채로
    떠 있으면 모든 요청이 BE 제한 시간(15분)까지 묵는다. 켜진 뒤 끊기면 aio-pika 가 다시 붙는다.
    """
    global _connection, _dead_letter, _slots
    url = rabbitmq_url()
    if url is None:
        logs.write("INFO", "mq_disabled")
        return

    await store.start()
    _slots = asyncio.Semaphore(jobs.job_workers())
    _connection = await aio_pika.connect_robust(url)
    # publisher_confirms: DLQ 로 옮길 때 브로커가 받았다고 답한 뒤에만 원본을 ACK 한다 (유실 방지)
    channel = await _connection.channel(publisher_confirms=True)
    await channel.set_qos(prefetch_count=1)
    queue = await channel.get_queue(QUEUE, ensure=True)
    _dead_letter = await channel.get_exchange(DEAD_LETTER_EXCHANGE, ensure=True)
    await queue.consume(_on_message)
    logs.write("INFO", "mq_consuming", queue=QUEUE, slots=jobs.job_workers())


async def stop() -> None:
    """
    서버가 꺼질 때. 먼저 연결을 닫아 더 꺼내지 않는다 — 받아 두고 ACK 안 한 메시지는 브로커가
    다시 넣는다. 그다음 돌고 있는 작업이 끝나길 기다린다 (돈이 나간 호출을 버리지 않으려고).
    """
    if _connection is None:
        return
    await _connection.close()
    await asyncio.gather(*_running, return_exceptions=True)
    await store.stop()


def parse(body: bytes) -> Envelope:
    """메시지 겉봉을 읽는다. input 안쪽(여행 조건·후보)은 adapter 가 본다."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidMessage(f"JSON 이 아닙니다: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidMessage("메시지가 객체가 아닙니다")

    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise InvalidMessage(f"모르는 schemaVersion: {data.get('schemaVersion')!r}")
    if data.get("jobType") not in JOB_TYPES:
        raise InvalidMessage(f"모르는 jobType: {data.get('jobType')!r}")

    message_id = data.get("messageId")
    job_id = data.get("jobId")
    attempt = data.get("attempt")
    payload = data.get("input")
    if not isinstance(message_id, str) or not message_id:
        raise InvalidMessage("messageId 가 없습니다")
    # bool 은 int 의 하위 타입이라 따로 막는다
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        raise InvalidMessage(f"jobId 가 정수가 아닙니다: {job_id!r}")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        raise InvalidMessage(f"attempt 가 0 이상의 정수가 아닙니다: {attempt!r}")
    if not isinstance(payload, dict):
        raise InvalidMessage("input 이 없습니다")

    return Envelope(message_id, job_id, attempt, data["jobType"], payload)


async def _on_message(message: AbstractIncomingMessage) -> None:
    """
    메시지 하나. 여기서 예외가 새면 ACK 도 NACK 도 안 된 채 남아, prefetch 1 인 이 소비자가
    멈춘다. 그래서 예상 못 한 오류도 전부 잡아 NACK 한다 (retry 큐로 갔다가 돌아온다).
    """
    try:
        await _handle(message)
    except Exception as exc:
        logs.write(
            "ERROR",
            "mq_message_error",
            message_id=message.message_id,
            error=str(exc),
            traceback="".join(traceback.format_exception(exc)),
        )
        try:
            await message.nack(requeue=False)
        except Exception:
            pass  # 연결이 끊겼다. ACK 안 한 메시지는 브로커가 다시 넣는다


async def _handle(message: AbstractIncomingMessage) -> None:
    try:
        envelope = parse(message.body)
    except InvalidMessage as exc:
        await _to_dead_letter(message, str(exc))
        return

    # 칸이 빌 때까지 여기서 기다린다. 이 메시지는 아직 ACK 전이라 브로커에 남아 있다.
    # DB 에는 칸을 잡은 뒤에 넣는다 — 먼저 넣고 기다리다 죽으면,
    # 재배달된 메시지를 중복으로 보고 버린다
    await _slots.acquire()
    inserted = False
    try:
        inserted = await store.insert(
            envelope.message_id,
            envelope.job_id,
            envelope.attempt,
            envelope.job_type,
            LEASE_SECONDS,
        )
        job = None
        if inserted:
            job = await _accept(envelope)
        else:
            # 이미 받은 메시지 (ACK 가 유실돼 재배달됐다). 일정을 두 번 만들지 않는다
            logs.write("INFO", "mq_duplicate", message_id=envelope.message_id)
        await message.ack()
    except BaseException:
        # ACK 가 안 됐으면 브로커가 다시 보낸다. 넣어 둔 줄을 남기면 그때 중복으로 보고 버린다
        jobs.jobs.pop(envelope.message_id, None)
        if inserted:
            await _forget(envelope.message_id)
        _slots.release()
        raise

    if job is None:  # 중복이거나, 요청 내용이 틀려 실패로 알렸다
        _slots.release()
        return

    task = asyncio.create_task(_run(job))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _accept(envelope: Envelope) -> dict | None:
    """
    메시지를 작업으로 바꿔 저장소에 넣는다. 요청 내용이 틀렸으면(지역·취향·후보 등) None.

    내용이 틀린 건 DLQ 가 아니라 FAILED 콜백으로 알린다 — HTTP 에서 400 으로 거절하던 것과 같고,
    DLQ 로 보내면 BE 는 제한 시간(15분)이 지나서야 실패를 안다.
    """
    payload = envelope.input
    try:
        trip = adapter.to_trip_request(payload)
        places, events = adapter.to_candidates(payload.get("contents"))
        regeneration = None
        if envelope.job_type == "REGENERATION":
            feedback, title = adapter.to_feedback(payload)
            regeneration = {
                "source_job_id": payload.get("source_job_id"),
                "previous_plan": adapter.to_previous_plan(payload.get("previous_plan")),
                "feedback": feedback,
                "title": title,
            }
    except adapter.InputError as exc:
        _reject(envelope, exc)
        await store.finish(envelope.message_id, "FAILED", exc.code)
        return None

    if regeneration is not None:
        places, events = await _narrow_to_kept(
            envelope, regeneration["source_job_id"], places, events
        )

    return jobs.accept_message(
        trip, places, events, envelope.message_id, payload, regeneration
    )


async def _narrow_to_kept(
    envelope: Envelope,
    source_job_id,
    places: list[Candidate],
    events: list[Candidate],
) -> tuple[list[Candidate], list[Candidate]]:
    """
    재생성 후보를 원본 작업이 남긴 후보로 좁히고 notice 를 다시 붙인다 (A안).
    재생성은 filter_closed 를 돌리지 않는다 (#74) — 메시지의 contents 는 거르기 전 목록이다.

    원본 기록이 없으면(DB 를 붙이기 전에 만든 작업 등) 받은 후보를 그대로 쓴다. 실패로 내면
    사용자가 재생성을 못 한다. 대신 처음에 뺀 곳이 다시 들어올 수 있어 WARNING 으로 남긴다.
    """
    kept = None
    if isinstance(source_job_id, int) and not isinstance(source_job_id, bool):
        kept = await store.kept_candidates(source_job_id)
    if kept is None:
        logs.write(
            "WARNING",
            "mq_kept_missing",
            message_id=envelope.message_id,
            source_job_id=source_job_id,
        )
        return places, events

    # 원본 작업이 남긴 순서대로 — HTTP 재생성이 원래 작업의 후보 목록을 그대로 쓰는 것과 같게
    def narrow(candidates: list[Candidate]) -> list[Candidate]:
        by_id = {c.content_id: c for c in candidates}
        return [
            by_id[item["content_id"]].model_copy(update={"notice": item["notice"]})
            for item in kept
            if item["content_id"] in by_id
        ]

    return narrow(places), narrow(events)


def _reject(envelope: Envelope, exc: adapter.InputError) -> None:
    logs.write(
        "WARNING",
        "request_rejected",
        request_id=envelope.message_id,
        code=exc.code,
        reason=exc.reason,
    )
    job = {
        "job_id": envelope.message_id,
        "request_id": envelope.message_id,
        "status": "failed",
        "result": None,
        "error": {"code": exc.code, "message": exc.reason},
    }
    callback.notify_final(job)


async def _run(job: dict) -> None:
    try:
        await jobs.run(job, _record)
    finally:
        _slots.release()


async def _record(job: dict) -> None:
    """
    끝난 상태를 DB 에 적는다. 성공한 작업은 지금 후보(처음 생성은 filter_closed 가 남긴 것,
    재생성은 좁혀 받은 것)를 같이 남긴다 — 다음 재생성이 source_job_id 로 찾는다.
    실패해도 콜백은 보낸다. 줄은 PROCESSING 으로 남는다.
    """
    kept = None
    if job["status"] == "completed":
        kept = [
            {"content_id": c.content_id, "notice": c.notice}
            for c in job["places"] + job["events"]
        ]
    error_code = job["error"]["code"] if job["error"] else None
    try:
        await store.finish(job["job_id"], job["status"].upper(), error_code, kept)
    except Exception as exc:
        logs.write("ERROR", "mq_store_failed", message_id=job["job_id"], error=str(exc))


async def _forget(message_id: str) -> None:
    """ACK 전에 멈춘 메시지의 줄을 지운다. 재배달되면 처음부터 다시 받는다."""
    try:
        await store.remove(message_id)
    except Exception as exc:
        # 줄이 PROCESSING 으로 남는다. 기한이 지나면 6단계 복구가 FAILED 로 알린다
        logs.write("ERROR", "mq_store_failed", message_id=message_id, error=str(exc))


async def _to_dead_letter(message: AbstractIncomingMessage, reason: str) -> None:
    """원본을 그대로 DLQ 로 옮긴다. 브로커가 받았다고 답한 뒤에만 원본을 ACK 한다."""
    logs.write("ERROR", "mq_dead_letter", message_id=message.message_id, reason=reason)
    await _dead_letter.publish(
        aio_pika.Message(
            body=message.body,
            headers={**(message.headers or {}), "x-ai-reason": reason},
            content_type=message.content_type,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=DEAD_LETTER_ROUTING_KEY,
    )
    await message.ack()
