"""
src/llm/fake_llm.py

**가짜 모델.** Claude 대신 고정 규칙으로 답한다. 호출 0회, 같은 입력에 같은 출력.

쓰는 곳이 둘이다.
    서버 부하 테스트 — LLM_MODE=fake 이면 llm_plan.plan_all 이 Claude 대신 answer() 를 부른다.
                      **Claude 를 부르는 자리만** 대신하므로 응답 검사·후처리는 진짜와 똑같이 돈다.
    테스트·로컬 도구 — tools/fake_llm.py 가 plan_all() 로 llm_plan.plan_all 을 통째로 바꿔 끼운다.

2026-09-29 전에는 tools/ 에만 있었다 ("서버는 항상 실제 모델"). 클라우드팀 부하 테스트에
Claude 를 부르면 돈이 나가고 응답 시간도 매번 달라서, 기본은 진짜로 두고 스위치로만 켜게 했다.
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta

from src import dates
from src.models import Candidate, DayDraft, DayPlanDraft, PlanDraftItem

# 가짜가 답하기 전에 기다릴 초. 진짜 Claude 의 30~50초를 흉내 내는 용도
DELAY_VAR = "FAKE_LLM_DELAY_SEC"

# 고정 시간표
START_HOURS = [9, 11, 14, 16, 18]
DURATION_MINUTES = 90


def delay_seconds() -> float:
    """
    FAKE_LLM_DELAY_SEC 를 읽는다. 비우면 0초.

    틀린 값이면 멈춘다. 조용히 0초로 돌면 부하 테스트 결과가 통째로 틀린 채 믿게 된다.
    서버가 켜질 때 한 번 불러, 요청이 들어오기 전에 알 수 있게 한다 (main.lifespan).
    """
    raw = os.environ.get(DELAY_VAR, "").strip()
    if not raw:
        return 0.0

    try:
        seconds = float(raw)
    except ValueError:
        raise ValueError(f"{DELAY_VAR} 는 숫자(초)여야 합니다: {raw!r}") from None
    if seconds < 0:
        raise ValueError(f"{DELAY_VAR} 는 0 이상이어야 합니다: {raw!r}")
    return seconds


def answer(
    candidates: list[Candidate],
    *,
    start: date,
    day_count: int,
    per_day: int,
    conditions: dict,
) -> DayPlanDraft:
    """서버의 가짜 모드. 정해 둔 만큼 기다렸다가 plan_all 과 같은 답을 낸다."""
    time.sleep(delay_seconds())
    return plan_all(
        candidates,
        start=start,
        day_count=day_count,
        per_day=per_day,
        conditions=conditions,
    )


def plan_all(
    candidates: list[Candidate],
    *,
    start: date,
    day_count: int,
    per_day: int,
    conditions: dict,
    trip: object | None = None,
) -> DayPlanDraft:
    """
    배치 규칙을 흉내 낸 고정 답을 낸다. 골든 테스트가 이것에 기댄다.

    행사는 갈 수 있는 날이 적은 것부터 하루 1건씩, 남은 자리는 장소를 차례로 채운다 —
    프롬프트가 모델에게 시키는 규칙과 같다. 가짜가 검사(_validate_all)를 통과하는
    답을 내야 조립 이후 단계를 모델 없이 돌려 볼 수 있다.

    trip 은 받기만 한다. llm_plan.plan_all 자리에 그대로 끼우려면 모양이 같아야 한다.
    """
    buckets: list[list[Candidate]] = [[] for _ in range(day_count)]

    taken: set[int] = set()
    events = [c for c in candidates if c.pick == "event"]
    for candidate in _by_deadline(events):
        day = _day_for_event(candidate, start, day_count, taken)
        if day is None:
            continue
        buckets[day - 1].append(candidate)
        taken.add(day)

    total = min(day_count * per_day, len(candidates))
    targets = _day_targets(total, day_count)
    remaining = iter([c for c in candidates if c.pick != "event"])
    for bucket, target in zip(buckets, targets):
        while len(bucket) < target:
            place = next(remaining, None)
            if place is None:
                break
            bucket.append(place)

    region = conditions["region"]
    return DayPlanDraft(
        title=f"{region} 여행",
        intro=f"{region}에서 {day_count}일 동안 다니는 일정이에요.",
        days=[_fake_day(day, bucket) for day, bucket in enumerate(buckets, start=1)],
        items=_fake_items(buckets),
    )


def _by_deadline(events: list[Candidate]) -> list[Candidate]:
    """
    **끝나는 날이 빠른 행사부터** 세운다. 이 순서라야 아래 탐욕 배정이 최적이 된다.

    행사가 갈 수 있는 날은 연속 구간이다. 이런 배정에서 "가장 이른 빈 날에 넣기"는
    처리 순서에 따라 최적보다 적게 넣는다 — 아무 날이나 되는 연중 행사가 앞자리를
    차지하면, 그 날에만 열리는 행사가 통째로 빠진다.
    전수 조사에서 15,597건 중 750건(4.8%)이 그랬다.

    마감이 급한 것부터 놓으면 그 일이 안 생긴다. contentid 를 같이 넣는 것은
    끝나는 날이 같을 때 순서를 고정해 같은 입력에 같은 결과가 나오게 하려는 것이다.
    """
    return sorted(events, key=lambda e: (e.event_end_date or "", e.content_id))


def _day_for_event(
    event: Candidate, start: date, day_count: int, taken: set[int]
) -> int | None:
    """행사를 넣을 수 있는 가장 이른 날. 이미 행사가 찬 날과 기간 밖은 건너뛴다."""
    for day in range(1, day_count + 1):
        if day in taken:
            continue
        that_day = dates.to_tourapi(start + timedelta(days=day - 1))
        if (event.event_start_date or "") <= that_day <= (event.event_end_date or ""):
            return day
    return None


def _day_targets(total: int, day_count: int) -> list[int]:
    """
    날마다 몇 곳을 둘지. 후보가 모자라도 앞날부터 꽉 채우지 않고 고르게 나눈다 —
    5곳 · 3일이면 [3, 2, 0] 이 아니라 [2, 2, 1]. 빈 날은 계약 위반이다 (7장 date 규칙).
    후보가 넉넉하면 날마다 per_day 개가 되어 전과 같다.
    """
    base, extra = divmod(total, day_count)
    return [base + 1 if index < extra else base for index in range(day_count)]


def _fake_day(day: int, bucket: list[Candidate]) -> DayDraft:
    """그날 들르는 곳을 받은 순서대로 잇는다. 빈 날도 요약은 있어야 한다."""
    if not bucket:
        return DayDraft(day=day, summary=f"{day}일차는 쉬어 가는 날이에요.")
    route = " → ".join(candidate.name for candidate in bucket)
    return DayDraft(day=day, summary=f"{day}일차에는 {route} 순서로 둘러봐요.")


def _fake_items(buckets: list[list[Candidate]]) -> list[PlanDraftItem]:
    """받은 순서를 그대로 두고 순번에 고정 시간표를 붙인다."""
    items: list[PlanDraftItem] = []

    for day_index, bucket in enumerate(buckets):
        for slot, candidate in enumerate(bucket):
            hour = START_HOURS[min(slot, len(START_HOURS) - 1)]
            items.append(
                PlanDraftItem(
                    content_id=candidate.content_id,
                    day=day_index + 1,
                    start_time=f"{hour:02d}:00",
                    duration_minutes=DURATION_MINUTES,
                    reason=_fake_reason(candidate),
                )
            )

    return items


def _fake_reason(candidate: Candidate) -> str:
    if candidate.pick == "event":
        return "여행 기간에 열리는 행사예요"
    if candidate.pick == "related":
        return "고르신 관심사와 같은 갈래라 함께 담았어요"
    if candidate.pick == "filler":
        return "관심사에 맞는 곳이 부족해 함께 담았어요"
    return "선택하신 관심사에 맞는 곳이에요"
