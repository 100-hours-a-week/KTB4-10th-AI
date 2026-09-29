"""
src/metrics.py

`/metrics` 로 나가는 숫자를 **정의만** 한다.
숫자를 올리는 일은 쓰는 쪽(main · jobs · llm_plan)이 한다.

src/ 바로 아래 둔 이유: 서버 쪽(src/server)과 LLM 쪽(src/llm)이 둘 다 쓴다.
한쪽 폴더에 두면 다른 쪽이 그 폴더를 끌어다 써야 한다 (paths.py · dates.py 와 같은 이유).

HTTP 숫자와 생성 작업 숫자를 **따로** 둔다. 생성 요청은 202 로 바로 끝나고 일정은
뒤에서 30~50초 걸려 만들어진다 — HTTP 시간만 재면 0.01초만 보인다 (2026-09-29 클라우드팀 합의).

CPU · 메모리는 여기 없다. prometheus_client 가 기본으로 process_* 숫자를 붙인다 (리눅스에서만).
"""

from __future__ import annotations

import time
from contextvars import ContextVar

from prometheus_client import Counter, Gauge, Histogram

# LLM 호출과 작업 전체는 수십 초 단위라 기본 구간(최대 10초)으로는 다 한 칸에 몰린다
SLOW_BUCKETS = (1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 180, 240, 300)

# --- HTTP ---
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP 요청 수",
    ["method", "path", "status"],
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 요청 하나에 걸린 시간",
    ["method", "path"],
)

# --- 생성 작업 ---
# 대기 중 · 처리 중은 긁어 갈 때마다 jobs 가 세어 준다 (jobs.start 에서 연결)
JOBS_QUEUED = Gauge("generation_jobs_queued", "대기열에 있는 작업 수")
JOBS_RUNNING = Gauge("generation_jobs_running", "지금 만들고 있는 작업 수")
JOB_DURATION = Histogram(
    "generation_job_duration_seconds",
    "작업 하나를 만드는 데 걸린 시간 (대기 시간 제외)",
    buckets=SLOW_BUCKETS,
)
JOBS_TOTAL = Counter(
    "generation_jobs_total",
    "작업 결과별 건수. rejected 는 대기열이 차서 접수조차 못 한 것",
    ["result"],  # success · failed · rejected
)
# 라벨 달린 숫자는 처음 올라갈 때 줄이 생긴다. 미리 0 으로 만들어 두지 않으면
# 실패·거절이 없던 동안 대시보드엔 0 이 아니라 "데이터 없음"으로 보인다.
# 부하 테스트에선 "거절 0건"도 확인할 값이다
for result in ("success", "failed", "rejected"):
    JOBS_TOTAL.labels(result=result)

GENERATION_ERRORS = Counter(
    "generation_errors_total",
    "작업이 실패한 원인별 건수",
    ["kind"],
)

# --- LLM ---
LLM_CALL_DURATION = Histogram(
    "llm_call_duration_seconds",
    "LLM 호출 한 번에 걸린 시간. 가짜 모드면 설정한 지연 시간",
    ["llm_mode"],
    buckets=SLOW_BUCKETS,
)
POST_PROCESSING_DURATION = Histogram(
    "post_processing_duration_seconds",
    "LLM 응답을 받은 직후부터 최종 결과(가이드북 HTML 포함)가 완성될 때까지",
)
LLM_MODE = Gauge(
    "llm_mode",
    "지금 서버의 LLM 모드. 해당 모드 라벨만 1",
    ["llm_mode"],
)


# --- 후처리 시간 재기 ---
# 시작점(LLM 응답 직후)은 일정 생성 흐름(다른 실) 안에서 생기고, 끝점(결과 완성)은
# 작업 관리(jobs) 쪽에서 생긴다. 둘을 잇는 통로가 필요하다.
#
# calllog._CURRENT_RUN 과 같은 이유로 **값이 아니라 상자(dict)**를 담는다.
# asyncio.to_thread 와 langgraph 는 문맥을 **복사**해서 돌린다. 안에서 새 값을 넣으면
# 복사본에만 들어가 밖에서 안 보인다. 상자는 복사돼도 같은 상자라 안에서 넣은 값이 밖에서 보인다.
_JOB_TIMING: ContextVar[dict | None] = ContextVar("job_timing", default=None)


def start_job_timing() -> None:
    """작업 하나를 돌리기 **전에** 부른다. 안 부르면 후처리 시간이 기록되지 않을 뿐이다."""
    _JOB_TIMING.set({"llm_done_at": None})


def mark_llm_done() -> None:
    """LLM 응답을 받은 직후. 응답 검사보다 먼저 불러야 검사 시간이 후처리에 들어간다."""
    timing = _JOB_TIMING.get()
    if timing is not None:
        timing["llm_done_at"] = time.monotonic()


def observe_post_processing() -> None:
    """최종 결과를 만든 직후. LLM 까지 가지 않은 작업이면 아무것도 안 한다."""
    timing = _JOB_TIMING.get()
    if timing is None or timing["llm_done_at"] is None:
        return
    elapsed = time.monotonic() - timing["llm_done_at"]
    POST_PROCESSING_DURATION.observe(elapsed)
