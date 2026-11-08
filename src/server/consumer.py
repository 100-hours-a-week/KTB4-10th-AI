"""
src/server/consumer.py

RabbitMQ 큐(guidebook.generation)에서 생성·재생성 요청을 꺼내 작업으로 돌린다.
계약은 BE 문서(docs/mq-transition.md 3·4장). HTTP 접수(src/main.py)는 전환이 끝날 때까지 같이 둔다.

**처리 칸이 빌 때만 꺼낸다.** 지금은 BE 가 동시 3건만 보내 주지만, MQ 로 바뀌면 그 제한이 없어진다.
받는 대로 꺼내 메모리에 쌓으면 재시작할 때 잃는다 — 기다리는 요청은 RabbitMQ 에 남겨 둔다.
prefetch 1 이라 칸을 기다리는 동안 브로커는 다음 메시지를 주지 않는다.

**ACK 는 작업을 받아 둔 뒤, 일정 생성 전에 보낸다.** 생성(최대 10분) 동안 메시지를 잡고 있으면
연결이 끊길 때 재배달돼 LLM 비용이 두 번 나간다. ACK 뒤의 실패는 FAILED 콜백으로 알린다.

큐·교환기는 BE 가 선언한다 (quorum · retry · DLX 설정). 여기서 다른 설정으로 선언하면 브로커가
채널을 닫으므로, 이름으로 찾기만 한다. 로컬에서는 scripts/mq_local.py setup 이 BE 대신 만든다.

RABBITMQ_URL 이 비면 켜지 않는다 — HTTP 로만 받는 지금 환경 그대로다.
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
from src.server import adapter, callback, jobs

URL_VAR = "RABBITMQ_URL"
QUEUE = "guidebook.generation"
DEAD_LETTER_EXCHANGE = "guidebook.generation.dlx"
DEAD_LETTER_ROUTING_KEY = "guidebook.generation.failed"
SCHEMA_VERSION = 1
JOB_TYPES = ("INITIAL", "REGENERATION")

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

    # 이미 받은 메시지 (ACK 가 유실돼 재배달됐다). 일정을 두 번 만들지 않는다.
    # 지금은 메모리로만 거른다 — 재시작을 넘는 중복은 DB 를 붙이는 5단계에서 막는다
    if envelope.message_id in jobs.jobs:
        logs.write("INFO", "mq_duplicate", message_id=envelope.message_id)
        await message.ack()
        return

    # 칸이 빌 때까지 여기서 기다린다. 이 메시지는 아직 ACK 전이라 브로커에 남아 있다
    await _slots.acquire()
    try:
        job = _accept(envelope)
        await message.ack()
    except BaseException:
        # ACK 가 안 됐으면 브로커가 다시 보낸다. 넣어 둔 작업을 남기면 그때 중복으로 보고 버린다
        jobs.jobs.pop(envelope.message_id, None)
        _slots.release()
        raise

    if job is None:  # 요청 내용이 틀려 실패로 알렸다
        _slots.release()
        return

    task = asyncio.create_task(_run(job))
    _running.add(task)
    task.add_done_callback(_running.discard)


def _accept(envelope: Envelope) -> dict | None:
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
        return None

    return jobs.accept_message(
        trip, places, events, envelope.message_id, payload, regeneration
    )


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
        await jobs.run(job)
    finally:
        _slots.release()


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
