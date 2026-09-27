"""
server/tests/test_endpoints.py

엔드포인트 넷이 실제로 붙어 있는지. **일정 내용은 아직 검사할 것이 없다** — 1단계는
접수한 작업이 조회에서 다시 나오는지, 없는 것은 404 인지까지다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.main import app, jobs, request_map

REQUEST = {"request_id": "req-1", "input": {"province": "서울특별시", "city": "중구"}}


@pytest.fixture(autouse=True)
def 비운다():
    jobs.clear()
    request_map.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "jobs": 0}


def test_접수하면_작업이_생긴다(client) -> None:
    response = client.post("/guidebooks-generations", json=REQUEST)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert [step["state"] for step in body["steps"]] == ["pending"] * 4


def test_입력은_돌려주지_않는다(client) -> None:
    body = client.post("/guidebooks-generations", json=REQUEST).json()

    assert "input" not in body


def test_같은_request_id_는_작업을_하나로_묶는다(client) -> None:
    first = client.post("/guidebooks-generations", json=REQUEST).json()
    second = client.post("/guidebooks-generations", json=REQUEST).json()

    assert first["job_id"] == second["job_id"]
    assert len(jobs) == 1


def test_접수한_작업을_조회할_수_있다(client) -> None:
    job_id = client.post("/guidebooks-generations", json=REQUEST).json()["job_id"]

    response = client.get(f"/guidebooks-generations/{job_id}")

    assert response.status_code == 200
    assert response.json()["job_id"] == job_id


def test_없는_작업은_404(client) -> None:
    response = client.get("/guidebooks-generations/없는것")

    assert response.status_code == 404


def test_request_id_가_없으면_거절한다(client) -> None:
    response = client.post("/guidebooks-generations", json={"input": {}})

    assert response.status_code == 422  # 접수 검증은 2단계에서 400 으로 바꾼다


def test_실패한_적_없는_작업은_재시도할_수_없다(client) -> None:
    job_id = client.post("/guidebooks-generations", json=REQUEST).json()["job_id"]

    response = client.post(f"/guidebooks-generations/{job_id}/retry")

    assert response.status_code == 409


def test_실패한_작업은_다시_큐로_간다(client) -> None:
    job_id = client.post("/guidebooks-generations", json=REQUEST).json()["job_id"]
    jobs[job_id].update(status="failed", error={"code": "LLM_TIMEOUT"})

    body = client.post(f"/guidebooks-generations/{job_id}/retry").json()

    assert body["status"] == "queued"
    assert body["attempt"] == 2
    assert body["error"] is None


def test_재시도_한도를_넘으면_거절한다(client) -> None:
    job_id = client.post("/guidebooks-generations", json=REQUEST).json()["job_id"]
    jobs[job_id].update(status="failed", attempt=3)

    response = client.post(f"/guidebooks-generations/{job_id}/retry")

    assert response.status_code == 409
