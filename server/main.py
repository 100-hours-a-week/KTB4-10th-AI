"""
server/main.py

백엔드가 부르는 엔드포인트 넷. **아직 일정을 만들지 않는다** — 접수·조회·재시도의
자리만 잡아 둔 뼈대다 (계획 1단계, docs/claude/plan/server-wiring.md).

띄우기:
    uv run uvicorn server.main:app --reload
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MAX_ATTEMPTS = 3

app = FastAPI(title="가이드북 생성 서버")

# 작업 하나 = 딕셔너리 하나. 서버를 내리면 사라진다.
jobs: dict[str, dict] = {}

# request_id -> job_id. 응답이 네트워크에서 사라져 백엔드가 같은 요청을 다시 보내도
# 작업을 하나로 묶는다. 이 번호는 백엔드가 만들어 보낸다 (합의 사항).
request_map: dict[str, str] = {}

# 진행 단계 넷. 그래프 노드와 1:1이고 prepare 는 여기 없다 —
# 입력이 잘못된 것은 작업 실패가 아니라 접수 거절(400)이라서다.
STEPS = [
    ("finding_places", "취향에 맞는 장소 찾는 중"),
    ("checking_events", "기간 중 행사 확인하는 중"),
    ("building_itinerary", "일정 짜는 중"),
    ("polishing_itinerary", "일정 다듬는 중"),
]


class GenerationRequest(BaseModel):
    """
    접수 요청. `input` 을 아직 dict 로 두는 이유: 취향을 코드로 받을지 한글 라벨로
    받을지가 백엔드와 미정이라, 지금 스키마를 박으면 두 번 고치게 된다.
    """

    request_id: str
    input: dict


def _new_job(payload: GenerationRequest) -> dict:
    return {
        "job_id": str(uuid.uuid4()),
        "status": "queued",
        "attempt": 1,
        "steps": [
            {"key": key, "label": label, "state": "pending"} for key, label in STEPS
        ],
        "error": None,
        "input": payload.input,
    }


def _public(job: dict) -> dict:
    """백엔드에 돌려줄 모습. 입력을 그대로 되돌려주지 않는다."""
    return {key: value for key, value in job.items() if key != "input"}


def _find(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, detail="JOB_NOT_FOUND")
    return job


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "jobs": len(jobs)}


@app.post("/guidebooks-generations", status_code=202)
def create_generation(payload: GenerationRequest) -> dict:
    known = request_map.get(payload.request_id)
    if known is not None:
        return _public(jobs[known])

    job = _new_job(payload)
    jobs[job["job_id"]] = job
    request_map[payload.request_id] = job["job_id"]
    return _public(job)


@app.get("/guidebooks-generations/{job_id}")
def get_generation(job_id: str) -> dict:
    return _public(_find(job_id))


@app.post("/guidebooks-generations/{job_id}/retry", status_code=202)
def retry_generation(job_id: str) -> dict:
    job = _find(job_id)
    if job["status"] != "failed":
        raise HTTPException(409, detail="NOT_FAILED")
    if job["attempt"] >= MAX_ATTEMPTS:
        raise HTTPException(409, detail="MAX_ATTEMPTS_EXCEEDED")

    job.update(status="queued", attempt=job["attempt"] + 1, error=None)
    return _public(job)
