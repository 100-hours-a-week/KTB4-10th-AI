"""
src/server/store.py

MQ 로 받은 작업의 상태를 MySQL(RDS) 테이블 하나에 남긴다 (docs/mq-transition.md 5장).
입력·결과는 저장하지 않는다 — BE 가 갖고 있다. 남기는 것은 셋이다.

- **중복 판별.** 같은 messageId 는 한 줄만 들어간다 (UNIQUE). 서버를 다시 켠 뒤 재배달돼도 거른다
- **상태와 기한.** PROCESSING 인데 lease_until 이 지난 줄은 처리하다 죽은 작업이다 (복구는 6단계)
- **남긴 후보(A안).** 처음 생성 때 filter_closed 가 남긴 후보와 notice. 재생성이 이걸로
  후보를 좁힌다 — 메시지의 contents 는 거르기 전 목록이라, 그대로 쓰면 처음에 뺀 곳이 다시 들어온다

consumer 가 켜질 때만 쓴다. MYSQL_URL 예: mysql://ai:ai@localhost:3307/kgb_ai
"""

from __future__ import annotations

import json
import os
from urllib.parse import unquote, urlparse

import aiomysql
import pymysql

URL_VAR = "MYSQL_URL"
TABLE = "ai_generation_jobs"
DUPLICATE_KEY = 1062  # MySQL 오류 번호: UNIQUE 칸에 같은 값

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    message_id VARCHAR(64) NOT NULL,
    job_id BIGINT NOT NULL,
    attempt INT NOT NULL,
    job_type VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    lease_until DATETIME NOT NULL,
    kept_candidates JSON NULL,
    error_code VARCHAR(64) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_message_id (message_id),
    KEY idx_job_status (job_id, status)
)
"""

_pool: aiomysql.Pool | None = None


def mysql_url() -> str | None:
    return os.environ.get(URL_VAR, "").strip() or None


async def start() -> None:
    """연결을 열고 테이블이 없으면 만든다. 주소가 없거나 연결이 안 되면 서버 시작을 멈춘다."""
    global _pool
    url = mysql_url()
    if url is None:
        raise RuntimeError(f"RABBITMQ_URL 을 쓰려면 {URL_VAR} 도 필요합니다")
    parts = urlparse(url)
    _pool = await aiomysql.create_pool(
        host=parts.hostname,
        port=parts.port or 3306,
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        db=parts.path.lstrip("/"),
        autocommit=True,
        charset="utf8mb4",
        # 시각은 DB 기준으로 맞춘다 (lease_until 을 DB 의 NOW() 로 계산)
        init_command="SET time_zone = '+09:00'",
        # 처리 칸(기본 3)마다 끝날 때 한 번씩 쓴다. 넉넉하게
        maxsize=10,
    )
    await _execute(SCHEMA)


async def stop() -> None:
    if _pool is None:
        return
    _pool.close()
    await _pool.wait_closed()


async def insert(
    message_id: str, job_id: int, attempt: int, job_type: str, lease_seconds: int
) -> bool:
    """
    PROCESSING 으로 한 줄 넣는다. 이미 있는 messageId 면 False (중복 — 실행하지 않는다).
    autocommit 이라 돌아오면 이미 저장돼 있다. 그다음에 ACK 한다.
    """
    try:
        await _execute(
            f"INSERT INTO {TABLE} "
            "(message_id, job_id, attempt, job_type, status, lease_until) "
            "VALUES (%s, %s, %s, %s, 'PROCESSING', NOW() + INTERVAL %s SECOND)",
            (message_id, job_id, attempt, job_type, lease_seconds),
        )
    except pymysql.err.IntegrityError as exc:
        if exc.args[0] == DUPLICATE_KEY:
            return False
        raise
    return True


async def finish(
    message_id: str,
    status: str,
    error_code: str | None,
    kept: list[dict] | None = None,
) -> None:
    """끝난 상태를 적는다. kept 는 남긴 후보 [{content_id, notice}] — 성공한 작업만 넘긴다."""
    await _execute(
        f"UPDATE {TABLE} SET status = %s, error_code = %s, kept_candidates = %s "
        "WHERE message_id = %s",
        (
            status,
            error_code,
            None if kept is None else json.dumps(kept, ensure_ascii=False),
            message_id,
        ),
    )


async def remove(message_id: str) -> None:
    """넣은 줄을 지운다. ACK 전에 멈춰 메시지가 재배달될 때만 (consumer._forget)."""
    await _execute(f"DELETE FROM {TABLE} WHERE message_id = %s", (message_id,))


async def kept_candidates(job_id: int) -> list[dict] | None:
    """
    BE 작업 하나(jobId)가 남긴 후보. 재생성의 source_job_id 로 찾는다.
    사용자 재시도로 attempt 가 여럿이면 성공한 것 중 마지막. 없으면 None.
    """
    rows = await _execute(
        f"SELECT kept_candidates FROM {TABLE} "
        "WHERE job_id = %s AND status = 'COMPLETED' AND kept_candidates IS NOT NULL "
        "ORDER BY attempt DESC LIMIT 1",
        (job_id,),
    )
    if not rows:
        return None
    return json.loads(rows[0][0])


async def _execute(sql: str, args: tuple = ()) -> list[tuple]:
    async with _pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(sql, args)
            return list(await cursor.fetchall())
