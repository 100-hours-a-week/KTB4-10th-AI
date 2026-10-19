"""
src/llm/llm_regenerate.py

**재생성 배치 (#44).** 완료된 일정과 사용자 피드백을 받아 배치를 고친다. 글쓰기는 생성과
똑같이 llm_write 가 한다 — 피드백은 배치에만 들어간다(문구만 고치는 재생성은 없다).

피드백 해석을 따로 부르지 않고 배치에 같이 넣는다 (2026-10-17 사용자 결정) — 호출이
늘면 작업 상한 600초 안에 넣기 어렵다. 그래서 받을 수 있는 피드백인지도 배치가 답한다.
결정과 버린 대안은 docs/claude/plan/split-writing-nodes.md 「재생성 결정 (10-17)」.

지시문 · 입력 · 검사는 llm_arrange 것을 바탕으로 재생성 몫만 더한다.
"""

from __future__ import annotations

import json
import time
from datetime import date

from src import metrics
from src.llm import fake_llm, llm_arrange, llm_client
from src.llm.llm_client import LLMError
from src.models import Candidate, PlanItem, RegenerateDraft, TripRequest

PROMPT = (
    llm_arrange.SYSTEM_PROMPT
    + """
재생성:
이전 일정(previous_plan)과 사용자 피드백(feedback)을 함께 받습니다. 피드백대로 이전 일정을 고칩니다.
받을 수 있는 피드백 — 날짜·순서·시각 바꾸기, 하루 장소를 줄여 여유롭게, 장소·행사 빼기,
후보에 있는 장소 넣기.
그 밖이면 applicable 을 false 로, reject_reason 에 이유를 한 문장으로 쓰고 items 는 비웁니다.
  (예: "취향과 안 맞아요"처럼 무엇을 바꿀지 알 수 없는 것 · 후보에 없는 곳 넣기 ·
  하루 장소를 per_day 보다 늘리기)
- 배치 규칙 1 대신: 모든 날에 한 곳 이상, 하루 최대 per_day 개입니다.
  개수를 맞추려고 장소를 더하지 않습니다.
- 피드백이 바꾸라고 하지 않은 곳은 날짜·시각을 그대로 둡니다.
- 행사를 빼라고 하면 다른 행사로 바꿔 넣지 않습니다.
"""
)


def rearrange_all(
    candidates: list[Candidate],
    previous_plan: list[PlanItem],
    feedback: str,
    *,
    start: date,
    day_count: int,
    per_day: int,
    conditions: dict,
    trip: TripRequest,
) -> RegenerateDraft:
    """
    **재생성 배치.** 이전 배치와 피드백을 넘기고 고친 배치를 받는다.
    받을 수 없는 피드백이면 applicable=false 인 답을 검사 없이 그대로 돌려준다 —
    멈출지는 부르는 쪽(nodes.arrange)이 정한다.
    """
    mode = llm_client.llm_mode()
    started = time.monotonic()
    try:
        if mode == "fake":
            draft = fake_llm.rearrange_all(candidates, previous_plan, feedback)
        else:
            payload = _payload(
                candidates,
                previous_plan,
                feedback,
                start,
                day_count,
                per_day,
                conditions,
            )
            draft = _claude_rearrange_all(llm_client.model_name(), payload, trip)
    except LLMError as exc:
        exc.stage = "arrange"
        raise
    finally:
        elapsed = time.monotonic() - started
        metrics.LLM_CALL_DURATION.labels(llm_mode=mode).observe(elapsed)

    if not draft.applicable:
        return draft

    # 생성 배치의 검사에서 「정확히 N개」만 끈다 — "여유롭게"·"빼 줘"로 장소가 준다
    try:
        llm_arrange._validate_all(
            draft.items, candidates, start, day_count, per_day, exact_count=False
        )
    except LLMError as exc:
        raise LLMError(str(exc), kind="validation", stage="arrange") from exc
    return draft


def _payload(
    candidates: list[Candidate],
    previous_plan: list[PlanItem],
    feedback: str,
    start: date,
    day_count: int,
    per_day: int,
    conditions: dict,
) -> str:
    """생성 배치의 입력에서 개수 강제를 빼고, 이전 배치와 피드백을 더한다."""
    fields = llm_arrange.payload_fields(
        candidates, start, day_count, per_day, conditions
    )
    # 재생성은 장소가 줄 수 있다 — 정확한 개수를 주면 「여유롭게」와 부딪힌다
    del fields["total_to_place"]
    fields["previous_plan"] = [
        {
            "day": item.day,
            "content_id": item.content_id,
            "start_time": item.start_time,
            "duration_minutes": item.duration_minutes,
        }
        for item in previous_plan
    ]
    fields["feedback"] = feedback
    return json.dumps(fields, ensure_ascii=False, indent=2)


def _claude_rearrange_all(
    model: str, payload: str, trip: TripRequest
) -> RegenerateDraft:
    """실제 Claude 를 1회 호출한다. [비용 발생]"""
    request = {
        "stage": "arrange",
        "regenerate": True,  # 기록에서 생성 배치와 가르려고
        "trip": trip.model_dump(),  # 기록용
        "system": PROMPT,
        "candidates": json.loads(payload),
    }
    return llm_client.call_and_record(
        model, PROMPT, payload, RegenerateDraft, llm_arrange.ARRANGE_TIMEOUT, request
    )
