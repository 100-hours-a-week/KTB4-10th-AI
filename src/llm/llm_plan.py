"""
src/llm/llm_plan.py

**일정을 짜는 모델 호출.** 후보를 전부 넘기고, 무엇을 고를지·어느 날에 둘지·몇 시에
갈지까지 모델이 정한다.

2026-09-27 까지는 코드가 날짜를 묶고 모델은 하루 안의 순서만 정하는 경로(llm.plan_days)와
나란히 있었다. 그 경로를 지우면서 같은 자리 합치기·붙이기도 함께 버렸다 — 이 경로에는
처음부터 없던 기능이다 (docs/archive/claude-260928/plan/llm-plan-only.md).

모델을 부르는 일은 전부 이 파일에 있다 — 설정·키 읽기·SDK 호출·프롬프트·페이로드·검사.
2026-09-28 에 부품 파일(py)을 합쳤다. Gemini 호출을 지우자 Claude 호출 부품만 남아
따로 둘 이유가 없어졌다.

**plan_all 은 기본으로 Claude 를 부른다.** 모델은 LLM_MODEL (비우면 claude-sonnet-5).
LLM_MODE=fake 일 때만 Claude 를 부르는 자리를 가짜(src/llm/fake_llm.py)가 대신한다 —
클라우드팀 부하 테스트용 (2026-09-29). 검사·후처리는 두 모드가 똑같이 돈다.

실패하면 **멈춘다** — 대충 채운 일정을 내보내지 않는다. 조용히 품질이 낮아지면 왜 그런지 알 수 없다.

코드가 날짜를 정하지 않으므로 행사가 기간 밖으로 가는 것을 **보장**하지 못하고 **검사**한다.
_validate_all() 이 확인한다 — 검사는 틀렸을 때 멈출 뿐이다.
"""

from __future__ import annotations

import collections
import json
import os
import re
import time
from datetime import date, timedelta

from src import dates, logs, metrics
from src.llm import calllog, fake_llm
from src.models import Candidate, DayDraft, DayPlanDraft, PlanDraftItem, TripRequest
from src.paths import REPO_ROOT

DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000
EFFORT = "medium"  # 기본값은 high. 일정 배치가 무거운 일이 아니라 낮춰서 시작한다

# Claude SDK 호출 한 건의 상한. SDK 는 **시간 초과도 재시도**하므로 시간 초과가 두 번
# 이어지면 140 × 2 + 재시도 대기(이때는 8초 이하) = 288초다. 이 값이 서버의 작업 타임아웃
# (jobs.JOB_TIMEOUT 300초)보다 짧아야 작업이 실패로 접힌 뒤 실이 혼자 계속 돌지 않는다.
# 다만 서버가 429·5xx 에 retry-after 를 붙이면 SDK 는 그만큼 상한 없이 기다려 300초를
# 넘을 수 있다 (드묾). 실측 호출은 8.5~57.5초라 정상 응답을 끊을 일은 거의 없다.
CLAUDE_TIMEOUT = 140
CLAUDE_MAX_RETRIES = 1

ENV_PATH = REPO_ROOT / ".env"
MODEL_VAR = "LLM_MODEL"
ANTHROPIC_KEY_VAR = "ANTHROPIC_API_KEY"

# real (비워 둬도 real) · fake. 인프라에 알려 준 이름이라 바꾸면 클라우드팀에도 알린다
MODE_VAR = "LLM_MODE"
MODES = ("real", "fake")


class LLMError(RuntimeError):
    """
    LLM 호출이나 응답 검증이 실패했다. 작업은 generation_failed 로 끝난다.

    kind 는 로그와 /metrics 에서 원인을 가르는 이름이다. 계약 응답의 코드와는 무관하다.
        call       — 호출 실패 (한도 · 네트워크 · API 오류 · 거부)
        timeout    — 호출이 CLAUDE_TIMEOUT 을 넘었다
        parse      — 응답이 DayPlanDraft 모양이 아니다
        validation — 모양은 맞는데 규칙을 어겼다 (_validate_all · _validate_days)
        fake       — 가짜 모드에서 가짜가 실패했다
    """

    def __init__(self, message: str, kind: str = "call"):
        super().__init__(message)
        self.kind = kind


def llm_mode() -> str:
    """
    LLM_MODE 를 읽는다. 비우면 real.

    모르는 값이면 멈춘다. 오타("fak")를 real 로 읽으면 부하 테스트가 조용히 과금되고,
    fake 로 읽으면 운영이 조용히 가짜 일정을 낸다. 서버가 켜질 때 한 번 불러 미리 막는다.
    """
    raw = os.environ.get(MODE_VAR, "").strip().lower()
    if not raw:
        return "real"
    if raw not in MODES:
        raise LLMError(f"{MODE_VAR} 는 real 또는 fake 여야 합니다: {raw!r}")
    return raw


# 하루에 둘 수 있는 행사 수. 프롬프트·페이로드·검사 세 군데가 같은 값을 봐야 한다.
MAX_EVENTS_PER_DAY = 1

# 시작 시각 00:00 ~ 23:59. 시는 한 자리도 받는다 — 모델이 '9:00' 으로 줄 때가 있고
# 그건 맞는 시각이다. '25:00'·'9:7'·'아침' 은 거절한다.
CLOCK_TIME = re.compile(r"([01]?[0-9]|2[0-3]):[0-5][0-9]")

SYSTEM_PROMPT = """당신은 여행 일정을 짜는 도우미입니다.

여행 조건(trip), 여행 날짜(days), 후보(candidates)를 받습니다. 후보 중에서 골라
날짜와 시각을 정하고, 추천 이유와 가이드북 문구를 씁니다.

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
- 주소가 가까운 곳끼리 같은 날에 묶고, 주소가 같은 곳은 이어서 둡니다.
- 이름이 시간대를 암시하면 맞춥니다 (예: '야간 개장'은 저녁).
  식사 시간대는 비워 두거나 음식 관련 장소를 둡니다.

사실 — 아래 문구 전체에 적용:
- **입력에 있는 사실(이름·분류·주소·행사 기간)만 씁니다.** 역사적 사건, 문화재 지정·번호,
  운영·공연 시각, 요금처럼 입력에 없는 사실은 쓰지 마세요.
- 행사 기간을 근거로 들 때는 입력의 날짜와 맞아야 합니다.

문구:
- reason — 한 문장. trip 의 조건(관심사·여행 스타일·동행·인원)과 동선으로 이유를 댑니다.
  같은 문장을 돌려 쓰지 마세요.
- title — **15자 이내.** 지역과 여행의 성격이 드러나게 (예: "경주 역사 여행").
  날짜·일수는 넣지 마세요 — 표지에 따로 적힙니다.
- intro — 여행을 소개하는 한 문장.
- days — days 의 **모든 day 번호마다 하나씩.** summary 는 그날 배치한 곳을 도는 순서와
  흐름을 100~250자로 씁니다."""


# ============================================================
# 진입점
# ============================================================


def plan_all(
    candidates: list[Candidate],
    *,
    start: date,
    day_count: int,
    per_day: int,
    conditions: dict,
    trip: TripRequest,
) -> DayPlanDraft:
    """
    후보 전부를 넘기고 날짜·순서·시각과 가이드북 문구(제목·소개·하루 요약)를 받는다.

    conditions 는 모델에게 보내는 여행 조건(지역·인원·취향 라벨)이고, trip(TripRequest)은
    모델에게 보내지 않는 **기록용**이다. 라벨로 바꾸는 일은 부르는 쪽(nodes.plan)이 한다 —
    코드표는 engine 에 있고, 여기서 engine 을 부르면 engine ↔ llm 폴더 순환이 된다.
    """
    mode = llm_mode()
    started = time.monotonic()
    try:
        if mode == "fake":
            draft = _fake_plan_all(candidates, start, day_count, per_day, conditions)
        else:
            payload = _candidates_to_payload(
                candidates, start, day_count, per_day, conditions
            )
            model = os.environ.get(MODEL_VAR, "").strip() or DEFAULT_MODEL
            draft = _claude_plan_all(model, payload, trip)
    finally:
        # 실패한 호출도 잰다. 시간 초과로 끝난 호출이 가장 알고 싶은 값이다
        elapsed = time.monotonic() - started
        metrics.LLM_CALL_DURATION.labels(llm_mode=mode).observe(elapsed)

    # 검사부터가 후처리다 (클라우드팀 합의: 응답 검사 · 응답 조립 · HTML 생성)
    metrics.mark_llm_done()

    try:
        _validate_all(draft.items, candidates, start, day_count, per_day)
        _validate_days(draft.days, day_count)
    except LLMError as exc:
        raise LLMError(str(exc), kind="validation") from exc
    return draft


# ============================================================
# 모델 호출
# ============================================================


def _iso(yyyymmdd: str | None) -> str | None:
    """20261014 -> 2026-10-14. 모델이 날짜를 비교할 때 형식이 섞이지 않게 맞춘다."""
    if not yyyymmdd or len(yyyymmdd) != 8:
        return None
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _candidates_to_payload(
    candidates: list[Candidate],
    start: date,
    day_count: int,
    per_day: int,
    conditions: dict,
) -> str:
    """
    모델에게 넘길 것을 JSON 으로 만든다.

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

    listed = []
    for candidate in candidates:
        entry = {
            "content_id": candidate.content_id,
            "name": candidate.name,
            "category_code": candidate.category_code,
            "address": candidate.address,
            "kind": "행사" if candidate.pick == "event" else "장소",
        }
        if candidate.pick == "event":
            entry["event_start_date"] = _iso(candidate.event_start_date)
            entry["event_end_date"] = _iso(candidate.event_end_date)
        listed.append(entry)

    # 합계를 코드가 넣어 준다. days 길이 × per_day 를 모델이 곱하게 두면 개수를 놓친다
    # (2026-09-25 전북에서 25개 중 15·12개만 왔다).
    payload = {
        "trip": conditions,
        "days": days,
        "per_day": per_day,
        "total_to_place": min(len(days) * per_day, len(candidates)),
        "max_events_per_day": MAX_EVENTS_PER_DAY,
        "max_events_total": MAX_EVENTS_PER_DAY * len(days),
        "candidates": listed,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _validate_all(
    items: list[PlanDraftItem],
    candidates: list[Candidate],
    start: date,
    day_count: int,
    per_day: int,
) -> None:
    """
    모델이 돌려준 것이 규칙을 지켰는지 본다. 하나라도 어긋나면 멈춘다.

    일곱 가지를 보는데 성격이 둘로 갈린다 — **행사 기간 검사만이 사실 검사**이고
    (어기면 사용자에게 거짓 정보가 나간다), 나머지 여섯은 우리가 정한 형식·규칙이다.

    order 관련 검사 둘은 없앴다. 모델이 order 를 안 돌려주므로 어긋날 수가 없다.
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

    # 후보가 자리보다 적은 시군구가 32곳 있다 (화성시 효행구는 장소가 2곳뿐).
    # 그런 데서는 다 넣어도 자리가 남으므로, 기대치는 둘 중 작은 쪽이다.
    expected = min(day_count * per_day, len(candidates))
    if len(items) != expected:
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
        # 적은 경우는 nodes.plan 이 모델을 부르기 전에 걸러 낸다
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


def _fake_plan_all(
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


def _claude_plan_all(model: str, payload: str, trip: TripRequest) -> DayPlanDraft:
    """실제 Claude 를 1회 호출한다. [비용 발생]"""
    import anthropic

    client = anthropic.Anthropic(
        api_key=load_api_key(ANTHROPIC_KEY_VAR),
        timeout=CLAUDE_TIMEOUT,
        max_retries=CLAUDE_MAX_RETRIES,
    )
    request = {
        "trip": trip.model_dump(),  # 기록용
        "system": SYSTEM_PROMPT,
        "candidates": json.loads(payload),
    }

    started = time.monotonic()
    try:
        response = claude_call(client, model, payload)
    except LLMError as exc:
        calllog.record(
            model,
            request,
            mode="llm",
            error=str(exc),
            elapsed=time.monotonic() - started,
        )
        raise

    usage = response.usage
    calllog.record(
        model,
        request,
        mode="llm",
        response_text=response.parsed_output.model_dump_json(),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost=calllog.claude_cost(model, usage.input_tokens, usage.output_tokens),
        elapsed=time.monotonic() - started,
    )

    return response.parsed_output


# ============================================================
# 호출 부품
# ============================================================


def load_api_key(key_var: str) -> str:
    """
    환경변수가 우선이고, 없으면 저장소 뿌리의 .env 파일을 직접 읽는다.

    환경변수를 먼저 보는 이유: 배포는 도커라 컨테이너 안에 .env 파일이 없다
    (.dockerignore 가 뺀다). 호스트의 .env 는 `docker run --env-file` 로 **환경변수가
    되어** 들어온다. 2026-09-28 에 .env 파일만 읽게 바꿨다가 배포에서 서버가 안 켜졌다.

    python-dotenv 를 쓰지 않는 이유는 의존성이 하나 더 늘어서다. 형식이 단순해 직접 읽는
    편이 낫다.
    """
    from_env = os.environ.get(key_var, "").strip()
    if from_env:
        return from_env

    if not ENV_PATH.exists():
        raise LLMError(f"환경변수 {key_var} 도 {ENV_PATH} 도 없습니다.")

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() != key_var:
            continue
        cleaned = value.strip().strip('"').strip("'")
        if cleaned:
            return cleaned

    raise LLMError(f"{ENV_PATH} 에 {key_var} 값이 비어 있습니다.")


def claude_call(client, model: str, payload: str):
    """SDK 를 부르고 실패를 전부 LLMError 로 바꾼다. 기록은 부르는 쪽에서 한다."""
    import anthropic
    import pydantic

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            messages=[{"role": "user", "content": payload}],
            output_format=DayPlanDraft,
        )
    except anthropic.RateLimitError as exc:
        raise LLMError(f"호출 한도 초과: {exc}") from exc
    # 시간 초과는 네트워크 실패의 한 종류라, 아래 APIConnectionError 보다 먼저 잡아야 갈린다
    except anthropic.APITimeoutError as exc:
        raise LLMError(
            f"시간 초과 ({CLAUDE_TIMEOUT}초): {exc}", kind="timeout"
        ) from exc
    # SDK 가 응답 글자를 DayPlanDraft 로 읽다 실패했다 (깨진 JSON · 칸 누락 · 잘린 응답)
    except pydantic.ValidationError as exc:
        raise LLMError(f"응답 파싱 실패: {exc}", kind="parse") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"네트워크 실패: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"API 오류 {exc.status_code}: {exc.message}") from exc

    if response.stop_reason == "refusal":
        raise LLMError(f"모델이 거부했습니다: {response.stop_details}")

    return response
