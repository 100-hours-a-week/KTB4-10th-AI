"""
src/server/store.py

MQ 로 받은 작업을 메모리에 기억한다 (BE #331 문서). DB 는 쓰지 않는다 —
서버가 다시 켜지면 둘 다 비고, 그 뒤 같은 메시지가 다시 오면 한 번 더 만든다.
그 중복 완료 콜백은 BE 가 attempt·종료 상태로 걸러 무시한다 (BE 와 합의, 11/9).

- **중복 판별.** 받은 messageId. 같은 서버가 켜져 있는 동안 다시 오면 거른다
- **남긴 후보(A안).** 처음 생성 때 filter_closed 가 남긴 후보와 notice. 재생성이 이걸로
  후보를 좁힌다 — 메시지의 contents 는 거르기 전 목록이라, 그대로 쓰면 처음에 뺀 곳이 다시 들어온다.
  재시작 뒤에는 없으므로 재생성은 받은 contents 를 그대로 쓴다 (시안님 OK, 11/9)

둘 다 KEEP_FOR 가 지나면 지운다 (메모리가 끝없이 늘지 않게).
"""

from __future__ import annotations

from datetime import datetime, timedelta

# 재생성은 며칠 뒤에도 올 수 있어 넉넉히. 작업 하나에 후보 수십 개라 몇천 건이어도 수십 MB 안쪽
KEEP_FOR = timedelta(days=7)

# messageId → 받은 시각. dict 는 넣은 순서를 지키므로 앞에서부터 지우면 된다
_seen: dict[str, datetime] = {}
# BE jobId → (attempt, 남긴 후보 [{content_id, notice}], 적은 시각)
_kept: dict[int, tuple[int, list[dict], datetime]] = {}


def insert(message_id: str) -> bool:
    """처음 본 messageId 면 기억하고 True. 이미 받은 것이면 False (중복 — 실행하지 않는다)."""
    _prune()
    if message_id in _seen:
        return False
    _seen[message_id] = datetime.now()
    return True


def remove(message_id: str) -> None:
    """받은 기록을 지운다. ACK 전에 멈춰 메시지가 재배달될 때만 (consumer._handle)."""
    _seen.pop(message_id, None)


def keep(job_id: int, attempt: int, kept: list[dict]) -> None:
    """성공한 작업이 남긴 후보를 적는다. 사용자 재시도로 attempt 가 여럿이면 마지막 것만."""
    old = _kept.pop(job_id, None)
    if old is not None and old[0] > attempt:
        kept, attempt = old[1], old[0]
    # 지웠다 다시 넣어 맨 뒤로 보낸다 (_prune 이 앞에서부터 본다)
    _kept[job_id] = (attempt, kept, datetime.now())


def kept_candidates(job_id: int) -> list[dict] | None:
    """BE 작업 하나(jobId)가 남긴 후보. 재생성의 source_job_id 로 찾는다. 없으면 None."""
    entry = _kept.get(job_id)
    return None if entry is None else entry[1]


def _prune() -> None:
    limit = datetime.now() - KEEP_FOR
    while _seen and next(iter(_seen.values())) < limit:
        del _seen[next(iter(_seen))]
    while _kept and next(iter(_kept.values()))[2] < limit:
        del _kept[next(iter(_kept))]
