"""
src/llm/llm_arrange.py

**일정을 배치하는 모델 호출 (arrange).** 후보를 전부 넘기고, 무엇을 고를지·어느 날에
둘지·몇 시에 갈지까지 모델이 정한다. 문구(제목·소개·하루 요약·추천 이유)는 쓰지 않는다 —
배치가 끝난 뒤 src/llm/llm_write.py 가 고른 곳의 overview 를 보고 쓴다 (2026-10-15, #35).

2026-09-27 까지는 코드가 날짜를 묶고 모델은 하루 안의 순서만 정하는 경로(llm.plan_days)와
나란히 있었다. 그 경로를 지우면서 같은 자리 합치기·붙이기도 함께 버렸다 — 이 경로에는
처음부터 없던 기능이다 (docs/archive/claude-260928/plan/llm-plan-only.md).

Claude 를 부르는 부품(설정 · LLMError · call_and_record)은 llm_client 에 있다.
2026-10-15 까지 이 파일 이름은 llm_plan.py 였다 (#35).

**arrange_all 은 기본으로 Claude 를 부른다.** 모델은 LLM_MODEL (비우면 claude-sonnet-5).
LLM_MODE=fake 일 때만 Claude 를 부르는 자리를 가짜(src/llm/fake_llm.py)가 대신한다 —
클라우드팀 부하 테스트용 (2026-09-29). 검사·후처리는 두 모드가 똑같이 돈다.

실패하면 **멈춘다** — 대충 채운 일정을 내보내지 않는다. 조용히 품질이 낮아지면 왜 그런지 알 수 없다.

코드가 날짜를 정하지 않으므로 행사가 기간 밖으로 가는 것을 **보장**하지 못하고 **검사**한다.
_validate_all() 이 확인한다 — 검사는 틀렸을 때 멈출 뿐이다.
"""

from __future__ import annotations

import collections
import json
import re
import time
from datetime import date, timedelta

from src import dates, logs, metrics
from src.engine import data
from src.llm import fake_llm, llm_client
from src.llm.llm_client import LLMError
from src.models import Candidate, DayPlanDraft, PlanDraftItem, TripRequest

# 배치 호출 한 건의 상한. SDK 는 **시간 초과도 재시도**하므로 시간 초과가 두 번
# 이어지면 140 × 2 + 재시도 대기(이때는 8초 이하) = 288초다. 여기에 글쓰기 호출의 최악
# (llm_write.WRITE_TIMEOUT)을 더한 값이 서버의 작업 타임아웃(jobs.JOB_TIMEOUT)보다 짧아야
# 작업이 실패로 접힌 뒤 실이 혼자 계속 돌지 않는다.
# 다만 서버가 429·5xx 에 retry-after 를 붙이면 SDK 는 그만큼 상한 없이 기다려 작업 상한을
# 넘을 수 있다 (드묾).
ARRANGE_TIMEOUT = 140

# 하루에 둘 수 있는 행사 수. 프롬프트·페이로드·검사 세 군데가 같은 값을 봐야 한다.
MAX_EVENTS_PER_DAY = 1

# 시작 시각 00:00 ~ 23:59. 시는 한 자리도 받는다 — 모델이 '9:00' 으로 줄 때가 있고
# 그건 맞는 시각이다. '25:00'·'9:7'·'아침' 은 거절한다.
CLOCK_TIME = re.compile(r"([01]?[0-9]|2[0-3]):[0-5][0-9]")

# 장소가 어느 단계에서 담겼는지 모델에게 알린다. 후보를 자리보다 많이 넘기므로
# 이게 없으면 모델이 관심 장소를 빼고 아무 장소나 고를 수 있다
FIT_LABELS = {"interest": "관심사", "related": "비슷한 분류", "filler": "그 밖"}

SYSTEM_PROMPT = """당신은 여행 일정을 짜는 도우미입니다.

여행 조건(trip), 여행 날짜(days), 후보(candidates)를 받습니다. 후보 중에서 골라
날짜와 시각을 정합니다. 추천 이유와 가이드북 문구는 다음 단계에서 따로 씁니다.

배치 규칙:
1. **정확히 total_to_place 개**를 배치합니다. 하루 최대 per_day 개이고,
   **모든 날에 한 곳 이상** 둡니다.
   total_to_place 가 days 수 × per_day 보다 적으면 날마다 개수 차이가 1을 넘지 않게 고르게 나눕니다.
   답하기 전에 개수를 세어 확인하세요.
2. 같은 곳을 두 번 넣지 않고, day 는 days 에 있는 번호만 씁니다.
3. 행사(kind="행사")는 event_start_date ~ event_end_date 안의 날에만 둡니다.
   하루 최대 max_events_per_day 건, 전체 최대 max_events_total 건이며,
   남는 행사는 빼고 그 자리는 장소로 채웁니다.
4. start_time 은 HH:MM(24시간), duration_minutes 는 분 단위 정수입니다.

배치 요령:
- **갈 수 있는 날이 적은 행사부터** 자리를 잡습니다. 아니면 하루만 열리는 행사가 빠집니다.
- fit 이 "관심사"인 장소를 우선 둡니다. 다만 주소로 보아 다른 곳들과 멀리 떨어져
  하루 동선이 무리한 곳(예: 배로 가야 하는 섬)은 빼고 다른 장소로 채워도 됩니다.
- 주소가 가까운 곳끼리 같은 날에 묶고, 주소가 같은 곳은 이어서 둡니다.
- 이름이 시간대를 암시하면 맞춥니다 (예: '야간 개장'은 저녁).
  식사 시간대는 비워 두거나 음식 관련 장소를 둡니다.
- 운영 시각은 입력에 없습니다. 이름으로 보아 실내 관람 시설(박물관·미술관·전시관·기념관 등)이나
  전시 행사는 늦게 가면 닫혀 있을 수 있으니 **17:00 전에 끝나게** 둡니다.
  17:00 이후는 야외 명소·산책로·야경처럼 시간 제약이 적은 곳에 씁니다.
"""


# ============================================================
# 진입점
# ============================================================


def arrange_all(
    candidates: list[Candidate],
    *,
    start: date,
    day_count: int,
    per_day: int,
    conditions: dict,
    trip: TripRequest,
) -> DayPlanDraft:
    """
    **배치(arrange).** 후보 전부를 넘기고 날짜·시각을 받는다. 문구는 받지 않는다.

    conditions 는 모델에게 보내는 여행 조건(지역·인원·취향 라벨)이고, trip(TripRequest)은
    모델에게 보내지 않는 **기록용**이다. 라벨로 바꾸는 일은 부르는 쪽(nodes.arrange)이 한다 —
    코드표는 engine 에 있고, 여기서 engine 을 부르면 engine ↔ llm 폴더 순환이 된다.
    """
    mode = llm_client.llm_mode()
    started = time.monotonic()
    try:
        if mode == "fake":
            draft = _fake_arrange_all(candidates, start, day_count, per_day, conditions)
        else:
            payload = _candidates_to_payload(
                candidates, start, day_count, per_day, conditions
            )
            draft = _claude_arrange_all(llm_client.model_name(), payload, trip)
    except LLMError as exc:
        exc.stage = "arrange"
        raise
    finally:
        # 실패한 호출도 잰다. 시간 초과로 끝난 호출이 가장 알고 싶은 값이다
        elapsed = time.monotonic() - started
        metrics.LLM_CALL_DURATION.labels(llm_mode=mode).observe(elapsed)

    try:
        _validate_all(draft.items, candidates, start, day_count, per_day)
    except LLMError as exc:
        raise LLMError(str(exc), kind="validation", stage="arrange") from exc
    return draft


# ============================================================
# 모델 호출
# ============================================================


def _iso(yyyymmdd: str | None) -> str | None:
    """20261014 -> 2026-10-14. 모델이 날짜를 비교할 때 형식이 섞이지 않게 맞춘다."""
    if not yyyymmdd or len(yyyymmdd) != 8:
        return None
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def candidate_entry(candidate: Candidate) -> dict:
    """후보 한 곳을 모델에게 보일 모양으로. 배치와 글쓰기(llm_write)가 같이 쓴다."""
    entry = {
        "content_id": candidate.content_id,
        "name": candidate.name,
        # 코드('HS01')는 모델이 읽지 못해 이름에서 짐작한다. 소분류까지 주면
        # 덕수궁과 숭례문처럼 같은 중분류('역사유적지')도 갈린다
        "category": data.category_path(
            candidate.category_code, candidate.subcategory_code
        ),
        "address": candidate.address,
        "kind": "행사" if candidate.pick == "event" else "장소",
    }
    if candidate.pick == "event":
        entry["event_start_date"] = _iso(candidate.event_start_date)
        entry["event_end_date"] = _iso(candidate.event_end_date)
    else:
        entry["fit"] = FIT_LABELS[candidate.pick]
    return entry


def _candidates_to_payload(
    candidates: list[Candidate],
    start: date,
    day_count: int,
    per_day: int,
    conditions: dict,
) -> str:
    """모델에게 넘길 것을 JSON 으로 만든다. 무엇이 들어가는지는 payload_fields."""
    fields = payload_fields(candidates, start, day_count, per_day, conditions)
    return json.dumps(fields, ensure_ascii=False, indent=2)


def payload_fields(
    candidates: list[Candidate],
    start: date,
    day_count: int,
    per_day: int,
    conditions: dict,
) -> dict:
    """
    모델에게 넘길 칸. 재생성(llm_regenerate)이 여기에 이전 배치·피드백을 더해 쓴다.

    **행사 기간과 여행 날짜**가 들어간다 — 그게 없으면 모델이 날짜를 정할 근거가 없다.
    **여행 조건(trip)**도 들어간다 — 없으면 추천 이유와 제목을 조건에 맞춰 쓸 수 없다
    (2026-09-27 까지 빠져 있었다. 프롬프트는 조건에 맞추라는데 조건을 안 보냈다).

    좌표는 넣지 않는다 — 동선을 보려면 필요하지만, 넣으면 모델이 좌표를 되돌려줄 여지가
    생긴다. 사실 데이터를 한 방향으로만 흐르게 두는 편이 낫다고 보고 일단 뺐다.
    """
    days = [
        {
            "day": index + 1,
            "date": (start + timedelta(days=index)).isoformat(),
        }
        for index in range(day_count)
    ]

    listed = [candidate_entry(candidate) for candidate in candidates]

    # 합계를 코드가 넣어 준다. days 길이 × per_day 를 모델이 곱하게 두면 개수를 놓친다
    # (2026-09-25 전북에서 25개 중 15·12개만 왔다).
    return {
        "trip": conditions,
        "days": days,
        "per_day": per_day,
        "total_to_place": min(len(days) * per_day, len(candidates)),
        "max_events_per_day": MAX_EVENTS_PER_DAY,
        "max_events_total": MAX_EVENTS_PER_DAY * len(days),
        "candidates": listed,
    }


def _validate_all(
    items: list[PlanDraftItem],
    candidates: list[Candidate],
    start: date,
    day_count: int,
    per_day: int,
    exact_count: bool = True,
) -> None:
    """
    모델이 돌려준 것이 규칙을 지켰는지 본다. 하나라도 어긋나면 멈춘다.

    exact_count 를 끄면 「정확히 N개」만 건너뛴다 — 재생성은 "여유롭게"·"빼 줘"로
    장소가 줄 수 있다 (#44). 하루 최대·하루 1곳 이상은 그대로 본다.

    일곱 가지를 보는데 성격이 둘로 갈린다 — **행사 기간 검사만이 사실 검사**이고
    (어기면 사용자에게 거짓 정보가 나간다), 나머지 여섯은 우리가 정한 형식·규칙이다.

    order 관련 검사 둘은 없앴다. 모델이 order 를 안 돌려주므로 어긋날 수가 없다.
    관심 장소 누락 검사도 없앴다 (#26) — 먼 섬처럼 동선이 무리한 관심 장소는 빼도
    되게 했는데, 무리한지는 코드가 판단할 수 없다.
    """
    known = {c.content_id: c for c in candidates}
    returned = [item.content_id for item in items]

    unknown = set(returned) - set(known)
    if unknown:
        raise LLMError(f"후보에 없는 content_id 를 돌려줬습니다: {sorted(unknown)}")

    counted = collections.Counter(returned)
    duplicated = sorted(cid for cid, n in counted.items() if n > 1)
    if duplicated:
        raise LLMError(f"같은 곳을 두 번 배치했습니다: {duplicated}")

    # 후보가 자리보다 적은 시군구가 있다 — 최대 35자리(7일 × 하루 5곳) 기준 230곳 중 44곳,
    # 가장 적은 부산 연제구는 장소 8곳 (2026-10-11 덤프, 사진 없는 곳을 뺀 수).
    # 그런 데서는 다 넣어도 자리가 남으므로, 기대치는 둘 중 작은 쪽이다.
    expected = min(day_count * per_day, len(candidates))
    if exact_count and len(items) != expected:
        raise LLMError(f"{expected}개를 배치해야 하는데 {len(items)}개가 왔습니다")

    out_of_range = sorted({i.day for i in items if not 1 <= i.day <= day_count})
    if out_of_range:
        raise LLMError(f"없는 날짜입니다 (1~{day_count}): {out_of_range}")

    # 시각은 뒤에서 nodes.number_by_time 이 순서를 매길 때 쓴다. 여기서 막아 두어야
    # 형식 오류도 다른 규칙 위반과 같은 validation 으로 집계된다
    bad_times = sorted(
        {i.start_time for i in items if not CLOCK_TIME.fullmatch(i.start_time)}
    )
    if bad_times:
        raise LLMError(f"시각이 00:00~23:59 의 시:분 형식이 아닙니다: {bad_times}")

    # 여기만 사실 검사다. 열리지 않는 날에 행사를 두면 그대로 거짓 정보가 된다
    misplaced = []
    for item in items:
        candidate = known[item.content_id]
        if candidate.pick != "event":
            continue
        that_day = dates.to_tourapi(start + timedelta(days=item.day - 1))
        begin = candidate.event_start_date or ""
        end = candidate.event_end_date or ""
        if not begin <= that_day <= end:
            misplaced.append(f"{candidate.name}({that_day} ∉ {begin}~{end})")
    if misplaced:
        raise LLMError(f"행사를 열리지 않는 날에 뒀습니다: {misplaced}")

    for day in range(1, day_count + 1):
        of_day = [item for item in items if item.day == day]

        # 빈 날이 있으면 응답에서 그 날짜가 빠진다 (계약 7장 위반). 후보가 일수보다
        # 적은 경우는 nodes.arrange 가 모델을 부르기 전에 걸러 낸다
        if not of_day:
            raise LLMError(f"{day}일차가 비었습니다 (모든 날에 한 곳 이상)")

        # 정원 '초과'만 막는다. 모자란 것은 위 전체 개수 검사가 잡는다 —
        # 후보가 넉넉하면 두 검사가 맞물려 하루 per_day 개가 강제된다.
        if len(of_day) > per_day:
            raise LLMError(f"{day}일차가 {len(of_day)}곳입니다 (하루 {per_day}곳까지)")

        event_count = sum(1 for i in of_day if known[i.content_id].pick == "event")
        if event_count > MAX_EVENTS_PER_DAY:
            raise LLMError(
                f"{day}일차에 행사가 {event_count}건입니다 (최대 {MAX_EVENTS_PER_DAY}건)"
            )


def _fake_arrange_all(
    candidates: list[Candidate],
    start: date,
    day_count: int,
    per_day: int,
    conditions: dict,
) -> DayPlanDraft:
    """
    가짜 모드의 호출 한 건. 비용 기록 파일(calllog)에는 남기지 않는다 — 비용 장부가
    가짜로 오염된다. 로그 한 줄은 진짜와 같은 이름(llm_call)으로 남긴다.
    """
    started = time.monotonic()
    try:
        draft = fake_llm.answer(
            candidates,
            start=start,
            day_count=day_count,
            per_day=per_day,
            conditions=conditions,
        )
    except Exception as exc:
        elapsed = round(time.monotonic() - started, 1)
        logs.write(
            "ERROR",
            "llm_call_failed",
            llm_mode="fake",
            error=str(exc),
            elapsed_sec=elapsed,
        )
        raise LLMError(f"가짜 모델 실패: {exc}", kind="fake") from exc

    elapsed = round(time.monotonic() - started, 1)
    logs.write("INFO", "llm_call", llm_mode="fake", elapsed_sec=elapsed)
    return draft


def _claude_arrange_all(model: str, payload: str, trip: TripRequest) -> DayPlanDraft:
    """실제 Claude 를 1회 호출한다. [비용 발생]"""
    request = {
        "stage": "arrange",  # 한 작업에 호출이 둘이라 기록에서 가른다
        "trip": trip.model_dump(),  # 기록용
        "system": SYSTEM_PROMPT,
        "candidates": json.loads(payload),
    }
    return llm_client.call_and_record(
        model, SYSTEM_PROMPT, payload, DayPlanDraft, ARRANGE_TIMEOUT, request
    )
