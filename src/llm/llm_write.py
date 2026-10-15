"""
src/llm/llm_write.py

**가이드북 문구를 쓰는 모델 호출 (write).** 배치(llm_arrange.arrange_all)가 끝난 일정을 받아
제목·소개·하루 요약·추천 이유를 한 번에 쓴다. 날짜·순서·시각은 바꾸지 않는다.

배치와 나눈 이유 (2026-10-15, #35): 글의 근거로 장소 소개글(overview)을 넣는데, 후보
전부에 붙이면 입력이 몇 배가 된다. 고른 곳에만 붙이려면 배치가 먼저 끝나야 한다.

입력·출력 모양은 자체 모델 학습 데이터(samples/open_model/label_inputs_v7.jsonl)를 따른다 —
나중에 이 호출을 자체 모델로 바꾼다. 지시문은 v7 이 아니라 #26 의 문구 규칙에 overview
규칙을 더한 것이다 (v7 로 갈지는 자체 모델로 바꿀 때 정한다).

Claude 를 부르고 기록하는 부품(call_and_record · LLMError)은 llm_client 의 것을 쓴다.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta

from src import metrics
from src.llm import fake_llm, llm_arrange, llm_client
from src.llm.llm_client import LLMError
from src.models import Candidate, DayDraft, PlanItem, Reason, TripRequest, WriterDraft

# 글쓰기 호출 한 건의 상한. 출력이 짧아 배치보다 훨씬 짧게 둔다.
# 시간 초과가 두 번 이어지면 60 × 2 + 재시도 대기(8초 이하) = 128초
# (llm_arrange.ARRANGE_TIMEOUT 참고)
WRITE_TIMEOUT = 60

# 자체 모델 학습 데이터가 overview 를 200자로 잘라 넣었다. 입력 모양을 그와 같게 둔다
OVERVIEW_MAX_CHARS = 200

SYSTEM_PROMPT = """당신은 여행 가이드북 문구를 쓰는 작가입니다.

여행 조건(trip)과 이미 짜인 일정(days)을 받습니다. 날짜·순서·시각은 정해져 있으니
바꾸지 말고, 문구만 씁니다.

사실 — 아래 문구 전체에 적용:
- **입력에 있는 사실(이름·분류·주소·overview·행사 기간)만 씁니다.**
  입력에 없는 것은 쓰지 마세요. 예: 역사적 사건, 문화재 지정·번호, 운영·공연 시각, 요금,
  해 지는 시각(노을·일몰·석양), 분위기·풍경(조용한·한적한·운치 있는·맑은 바다),
  평판(대표적인·유명한).
- **place 의 overview 는 그 장소의 소개글입니다.** 추천 이유와 하루 요약을 그 장소답게 쓰는
  재료로 쓰되, overview 에 있는 사실만 쓰고 문장을 그대로 베끼지 말고 풀어 씁니다.
  다른 장소의 overview 를 섞지 마세요.
- 행사 기간은 입력의 날짜와, 아침·점심·저녁 같은 시간대 말은 그 장소의 start_time 과 맞게 씁니다.
- 위치는 주소에 적힌 만큼만 씁니다. '같은 동네'·'같은 길'은 두 주소에 같은 읍·면·동이나
  도로 이름이 실제로 있을 때만 씁니다. 거리·해안·산간 같은 말과 '이동 없이' 같은 과장은 쓰지 마세요.

문구:
- 모든 문구(reason·intro·summary)는 '~합니다'체로 씁니다.
- reasons — places 의 **content_id 마다 하나씩.** reason 은 **두 문장, 100~150자.**
  첫 문장은 왜 이곳인지 — fit 이 "관심사"면 trip 의 관심사와, 아니면 동행·여행 스타일·인원과
  이어서 씁니다. 장소는 이름·분류·overview 에서 드러나는 만큼만,
  여행 스타일은 일정의 짜임(넉넉한 시간, 이어지는 순서)으로 잇습니다.
  둘째 문장은 왜 이 순서인지 — 앞뒤 장소와 같은 동네면 그 동선을,
  아니면 그날 몇 번째로 무엇 다음에 들르는지 씁니다.
  같은 문장을 돌려 쓰지 마세요.
- title — **15자 이내.** 지역과 여행의 성격이 드러나게 (예: "경주 역사 여행").
  날짜·일수는 넣지 마세요 — 표지에 따로 적힙니다.
- intro — 여행을 소개하는 한 문장.
- days — days 의 **모든 day 번호마다 하나씩.** summary 는 100~300자로,
  그날 도는 순서(장소 이름과 오전·오후)와 각 곳이 trip 의 관심사·동행과 어떻게 이어지는지,
  같은 동네로 묶인 곳이 있으면 그 묶음을 씁니다."""


def write_all(
    plan: list[PlanItem],
    candidates: list[Candidate],
    *,
    start: date,
    day_count: int,
    conditions: dict,
    trip: TripRequest,
) -> WriterDraft:
    """
    **글쓰기(write).** 배치가 끝난 일정(plan)을 받아 문구를 쓴다.

    candidates 는 plan 의 content_id 로 이름·분류·주소·overview 를 찾는 데만 쓴다.
    conditions·trip 은 llm_arrange.arrange_all 과 같다 (모델에게 보낼 조건 · 기록용).
    """
    mode = llm_client.llm_mode()
    started = time.monotonic()
    try:
        if mode == "fake":
            draft = fake_llm.write_all(
                plan,
                candidates,
                start=start,
                day_count=day_count,
                conditions=conditions,
            )
        else:
            payload = _plan_to_payload(plan, candidates, start, day_count, conditions)
            model = (
                os.environ.get(llm_client.MODEL_VAR, "").strip()
                or llm_client.DEFAULT_MODEL
            )
            draft = _claude_write_all(model, payload, trip)
    except LLMError as exc:
        exc.stage = "write"
        raise
    finally:
        elapsed = time.monotonic() - started
        metrics.LLM_CALL_DURATION.labels(llm_mode=mode).observe(elapsed)

    # 마지막 모델 응답이다. 검사부터가 후처리다 (클라우드팀 합의: 응답 검사 · 응답 조립 · HTML 생성)
    metrics.mark_llm_done()

    try:
        _validate_days(draft.days, day_count)
        _validate_reasons(draft.reasons, plan)
    except LLMError as exc:
        raise LLMError(str(exc), kind="validation", stage="write") from exc
    return draft


def _plan_to_payload(
    plan: list[PlanItem],
    candidates: list[Candidate],
    start: date,
    day_count: int,
    conditions: dict,
) -> str:
    """배치된 곳만 날짜별로 묶어 JSON 으로 만든다. 모양은 v7 학습 데이터의 입력과 같다."""
    by_id = {candidate.content_id: candidate for candidate in candidates}

    days = []
    for day in range(1, day_count + 1):
        # plan 은 nodes.number_by_time 이 일차·차례 순으로 세워 두었다
        places = [
            _place_entry(item, by_id[item.content_id])
            for item in plan
            if item.day == day
        ]
        days.append(
            {
                "day": day,
                "date": (start + timedelta(days=day - 1)).isoformat(),
                "places": places,
            }
        )

    payload = {"trip": conditions, "days": days}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _place_entry(item: PlanItem, candidate: Candidate) -> dict:
    """배치 때 보인 모양에 시각·머무는 시간과 글의 재료(overview)를 더한다."""
    entry = llm_arrange.candidate_entry(candidate)
    entry["start_time"] = item.start_time
    entry["duration_minutes"] = item.duration_minutes
    entry["overview"] = candidate.overview[:OVERVIEW_MAX_CHARS]
    return entry


def _validate_days(days: list[DayDraft], day_count: int) -> None:
    """
    하루 요약이 1~day_count 일차를 **한 번씩** 담았나.

    빠지거나 겹치면 가이드북 일차 지면이 비거나 엉뚱한 날에 붙는다. 제목 길이는 여기서
    보지 않는다 — 넘치면 서버가 자른다 (사실 오류가 아니라 작업을 멈출 일이 아니다).
    """
    returned = sorted(day.day for day in days)
    expected = list(range(1, day_count + 1))
    if returned != expected:
        raise LLMError(
            f"하루 요약의 일차가 {returned} 입니다 (1~{day_count} 일차가 한 번씩이어야 함)"
        )


def _validate_reasons(reasons: list[Reason], plan: list[PlanItem]) -> None:
    """
    추천 이유가 배치된 곳마다 **한 번씩** 있나.

    빠지면 그 장소의 이유 칸이 비고, 배치에 없는 곳의 이유는 붙일 자리가 없다.
    """
    returned = sorted(reason.content_id for reason in reasons)
    expected = sorted(item.content_id for item in plan)
    if returned != expected:
        raise LLMError(
            f"추천 이유의 장소가 배치와 다릅니다 (받음 {returned} · 배치 {expected})"
        )


def _claude_write_all(model: str, payload: str, trip: TripRequest) -> WriterDraft:
    """실제 Claude 를 1회 호출한다. [비용 발생]"""
    request = {
        "stage": "write",  # 한 작업에 호출이 둘이라 기록에서 가른다
        "trip": trip.model_dump(),  # 기록용
        "system": SYSTEM_PROMPT,
        "plan": json.loads(payload),
    }
    return llm_client.call_and_record(
        model, SYSTEM_PROMPT, payload, WriterDraft, WRITE_TIMEOUT, request
    )
