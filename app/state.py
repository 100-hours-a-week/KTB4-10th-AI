"""
app/state.py

'DB'가 아니라 요청 사이에 값을 잠깐 기억해두는 파이썬 딕셔너리입니다.
서버를 재시작하면 전부 사라지고, 스키마도 영속성도 없습니다.
목적은 단 하나: POST /guidebooks가 발급한 job_id가 GET /guidebooks/{job_id}
에서 정말로 통하는지, retry/regenerate가 실제로 상태를 갈아끼우는지를
검증하는 것입니다.

우리 아키텍처 문서의 '작업 저장소'/'결과 저장소' 두 개념을 각각
jobs / guidebooks 두 딕셔너리로 대응시켰습니다.
"""

from __future__ import annotations
import uuid
from typing import Any


# ============================================================
# 예외 클래스
# ============================================================
# 라우터(다음 단계)에서 이 예외를 잡아 그대로 상태 코드/메시지로
# 변환하기만 하면 되도록, 이름을 에러 코드 표준화 문서의 코드와
# 맞춰뒀습니다.

class JobNotFound(Exception):
    """404 guidebook_not_found"""


class InvalidState(Exception):
    """409 invalid_state - 대상이 기대한 상태(failed/completed)가 아님"""


class RetryLimitExceeded(Exception):
    """409 retry_limit_exceeded"""


class JobAlreadyRunning(Exception):
    """409 job_already_running"""
    def __init__(self, job_id: str):
        self.job_id = job_id


class QuotaExceeded(Exception):
    """422 quota_exceeded"""


# ============================================================
# 인메모리 저장소
# ============================================================

jobs: dict[str, dict[str, Any]] = {}
guidebooks: dict[str, dict[str, Any]] = {}

# 아주 단순한 quota 시뮬레이션. 실제로는 사용자별 계정 정보를
# 백엔드가 관리하므로, AI 서버는 계정 개념을 몰라야 합니다
# (이전 대화에서 확인한 아키텍처 원칙). 여기서는 테스트를 위해
# 전역 카운터 하나로만 흉내 냅니다.
_remaining_quota = 5

# "지금 동일 조건으로 진행 중인 작업이 있는가"를 판단하기 위한
# 아주 단순한 중복 방지 집합. 실제로는 조건 해시나 사용자+조건
# 조합으로 판단해야 하지만, 여기서는 개념 검증용으로 조건 딕셔너리를
# 그대로 문자열화해서 키로 씁니다.
_in_flight_signatures: set[str] = set()


def reset_state() -> None:
    """테스트 간 상태를 초기화할 때 사용."""
    global _remaining_quota
    jobs.clear()
    guidebooks.clear()
    _in_flight_signatures.clear()
    _remaining_quota = 5


def get_remaining_quota() -> int:
    return _remaining_quota


def _consume_quota() -> None:
    global _remaining_quota
    if _remaining_quota <= 0:
        raise QuotaExceeded()
    _remaining_quota -= 1


DEFAULT_STEPS = [
    {"key": "finding_places", "label": "취향에 맞는 장소 찾는 중", "state": "pending"},
    {"key": "checking_events", "label": "기간 중 행사 확인하는 중", "state": "pending"},
    {"key": "building_itinerary", "label": "일정 짜는 중", "state": "pending"},
    {"key": "writing_reasons", "label": "추천 이유 쓰는 중", "state": "pending"},
]


def create_job(conditions: dict) -> dict:
    """
    POST /guidebooks 처리.
    - quota 차감
    - 동일 조건으로 이미 실행 중인 작업이 있으면 JobAlreadyRunning
    - 새 job_id 발급, pending 상태로 기록
    """
    signature = str(sorted(conditions.items()))
    if signature in _in_flight_signatures:
        existing = next(
            j["job_id"] for j in jobs.values()
            if j.get("_signature") == signature and j["status"] in ("pending", "processing")
        )
        raise JobAlreadyRunning(existing)

    _consume_quota()

    job_id = f"job_{uuid.uuid4().hex[:8]}"
    jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "retry_count": 0,
        "steps": [dict(s) for s in DEFAULT_STEPS],
        "conditions": conditions,
        "guidebook_id": None,
        "error": None,
        "_signature": signature,
    }
    _in_flight_signatures.add(signature)
    return jobs[job_id]


def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise JobNotFound()
    return job


def get_guidebook(guidebook_id: str) -> dict:
    gb = guidebooks.get(guidebook_id)
    if not gb:
        raise JobNotFound()
    return gb


def advance_step(job_id: str, step_key: str, new_state: str) -> dict:
    """워커가 파이프라인 단계를 진행시킬 때 호출 (테스트 시나리오 재현용)."""
    job = get_job(job_id)
    job["status"] = "processing"
    for step in job["steps"]:
        if step["key"] == step_key:
            step["state"] = new_state
    return job


def fail_job(job_id: str, error_type: str, message: str) -> dict:
    job = get_job(job_id)
    job["status"] = "failed"
    job["error"] = {"type": error_type, "message": message}
    _in_flight_signatures.discard(job.get("_signature"))
    return job


def complete_job(job_id: str, guidebook_payload: dict) -> dict:
    """
    워커가 파이프라인을 끝까지 마쳤을 때 호출.
    이 시점에 비로소 guidebook_id가 발급됩니다 - job_id(진행 중 개념)와
    guidebook_id(완성된 리소스 개념)를 분리한 설계를 그대로 반영.
    """
    job = get_job(job_id)
    guidebook_id = f"gb_{uuid.uuid4().hex[:8]}"
    guidebooks[guidebook_id] = {**guidebook_payload, "guidebook_id": guidebook_id, "job_id": job_id}
    job["status"] = "completed"
    job["guidebook_id"] = guidebook_id
    _in_flight_signatures.discard(job.get("_signature"))
    return job


def retry_job(job_id: str) -> dict:
    """
    POST /guidebooks/{job_id}/retry 처리.
    - failed 상태가 아니면 InvalidState
    - retry_count가 이미 3이면 RetryLimitExceeded
    - quota는 차감하지 않음 (명세서 규칙)
    """
    job = get_job(job_id)
    if job["status"] != "failed":
        raise InvalidState()
    if job["retry_count"] >= 3:
        raise RetryLimitExceeded()

    job["retry_count"] += 1
    job["status"] = "pending"
    job["error"] = None
    job["steps"] = [dict(s) for s in DEFAULT_STEPS]  # resume 미구현 -> 처음부터
    return job


def regenerate_guidebook(guidebook_id: str, feedback: str, title: str | None) -> dict:
    """
    POST /guidebooks/{guidebook_id}/regenerate 처리.
    completed 상태(=guidebooks에 존재)가 아니면 InvalidState.
    quota 차감함 (retry와 달리 명세서상 차감 대상).
    """
    gb = get_guidebook(guidebook_id)
    _consume_quota()

    new_job_id = f"job_{uuid.uuid4().hex[:8]}"
    jobs[new_job_id] = {
        "job_id": new_job_id,
        "status": "pending",
        "retry_count": 0,
        "steps": [dict(s) for s in DEFAULT_STEPS],
        "conditions": {**gb.get("conditions", {}), "feedback": feedback, "title": title},
        "guidebook_id": None,
        "error": None,
        "_signature": None,
    }
    return jobs[new_job_id]
