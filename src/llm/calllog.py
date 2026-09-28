"""
src/llm/calllog.py

모델을 부른 기록을 남긴다. 호출 자체는 여기 없다 (llm_plan.py 가 한다).

파일로 남기는 이유는 둘이다 — 무료 등급에도 분당·일일 한도가 있어 "오늘 몇 번
불렀나"가 스크롤에 밀리면 안 되고, "왜 이렇게 짰지"를 나중에 보려면 **그때 준 후보**와
**그때 받은 답**이 같이 있어야 한다.

갈라낸 이유: 날짜 배정 경로가 둘이던 때(llm.plan_days · llm_plan.plan_all, 2026-09-27 에
하나로 줄였다) 기록하는 일이 한쪽 파일 안에 있어서, 다른 쪽이 밑줄 함수를 가져다 썼다.
"""

from __future__ import annotations

import json
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from src.paths import REPO_ROOT

OUT_DIR = REPO_ROOT / "_out"  # 산출물은 전부 여기 아래. 커밋되지 않는다
CALL_LOG_PATH = OUT_DIR / "llm_calls.jsonl"  # 목차
CALL_DIR = OUT_DIR / "llm_calls"  # 호출마다 파일 하나

# 기록을 가리키는 문자열은 **저장소 뿌리 기준 상대경로**다. 터미널에 찍힌 값을 그대로
# scripts/replay_llm_call.py 에 붙일 수 있다.
CALL_DIR_REL = CALL_DIR.relative_to(REPO_ROOT)

# "0" 이면 기록 파일을 남기지 않고 화면 한 줄만 찍는다. 서버(도커)용 — 컨테이너 안에 쌓여 봐야
# 재배포하면 사라지고, 화면 한 줄은 docker logs 에 남는다. 로컬은 비워 두면 지금처럼 남긴다.
SAVE_VAR = "SAVE_LLM_CALLS"

# 마지막으로 남긴 호출 기록 파일. run.py 가 결과와 호출을 이어 붙일 때 읽는다 —
# 시각으로 짐작하지 않고 파일 이름으로 잇는다.
#
# **그냥 전역이 아니라 ContextVar 다.** 서버에서 작업 여럿을 나란히 돌리면 전역은
# 서로 덮어쓴다 — A 가 부른 직후 B 가 부르면 A 가 B 의 기록 파일을 자기 것으로 적는다.
# 에러도 안 나고 조용히 틀린다. ContextVar 는 **실행 흐름마다 따로** 갖는다:
# asyncio 작업마다, 그리고 asyncio.to_thread 로 넘긴 실마다 제 값을 본다.
#
# **값이 아니라 상자(dict)를 담는다.** langgraph 는 노드를 문맥의 **복사본** 위에서 돌린다.
# 노드 안에서 ContextVar 에 새 값을 넣으면 복사본에만 들어가 그래프 밖에서는 안 보였다
# (2026-09-28 실호출에서 "호출 기록: 없음" 으로 찍혔다). 상자는 복사돼도 **같은 상자**를
# 가리키므로, 밖에서 start_run() 으로 상자를 만들어 두면 노드가 넣은 값이 밖에서 보인다.
_CURRENT_RUN: ContextVar[dict | None] = ContextVar("v0_current_run", default=None)
KST = timezone(timedelta(hours=9))


# 모델별 백만 토큰당 (입력, 출력) 요금 USD. 2026-09-25 공식 문서 기준.
# LLM_MODEL 로 모델을 바꾸면 여기에 그 모델이 있어야 기록된 비용이 맞는다.
CLAUDE_PRICING = {
    "claude-opus-5": (5, 25),
    "claude-sonnet-5": (2, 10),
    "claude-haiku-4-5": (1, 5),
}


def claude_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """
    호출 한 건의 대략 비용. 요금표에 없는 모델이면 **None**.

    모르는 모델에 아무 요금이나 곱하지 않는 이유: 나중에 기록을 합산할 때
    틀린 숫자가 섞이면 비어 있는 것보다 나쁘다. 비어 있으면 눈에 띄지만
    2.5배 낮은 값은 그대로 믿게 된다.
    """
    price = CLAUDE_PRICING.get(model)
    if price is None:
        return None

    input_per_mtok, output_per_mtok = price
    return (
        input_tokens * input_per_mtok / 1_000_000
        + output_tokens * output_per_mtok / 1_000_000
    )


def _readable(response_text: str | None) -> object:
    """
    응답을 읽을 수 있는 모양으로 바꾼다.

    모델이 주는 것은 JSON 문자열이라 그대로 저장하면 한글이 \uc88b 로 남아 눈으로 못
    읽는다. 파싱해서 넣으면 들여쓰기된 채로 보인다 — 잃는 것은 공백뿐이다.
    파싱이 안 되면 문자열 그대로 둔다. **깨진 JSON 이야말로 원문이 필요한 상황이다.**
    """
    if response_text is None:
        return None
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        return response_text


def record(
    model: str,
    request: dict,
    *,
    mode: str,
    response_text: str | None = None,
    error: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost: float | None = None,
    elapsed: float | None = None,
) -> None:
    """
    나간 호출 한 건을 파일 하나로 남기고, 목차에 한 줄 더한다. 화면에도 찍는다.

    파일을 남기는 이유는 둘이다 — 무료 등급에도 분당·일일 한도가 있어 "오늘 몇 번
    불렀나"가 스크롤에 밀리면 안 되고, "왜 이렇게 짰지"를 나중에 보려면 **그때 준 후보**와
    **그때 받은 답**이 같이 있어야 하기 때문이다.

    **실패한 호출도 남긴다.** 실패도 한도를 깎고, 에러 본문(429·400)이 다음 판단의 근거다.
    API 키는 어떤 경우에도 저장하지 않는다 — request 에는 본문만 담고 헤더는 넣지 않는다.

    mode 는 **날짜 배정을 누가 했나**다 ("code" = 코드가 날짜를 묶고 모델은 하루 안만,
    "llm" = 모델이 날짜까지). **2026-09-25 에 추가했다 — 그 이전 기록에는 이 칸이 없다.**
    2026-09-27 에 code 경로를 지워 지금은 늘 "llm" 이다. 칸을 지우지 않는 이유는
    이전 기록과 같은 모양으로 이어 읽기 위해서다.

    elapsed 는 호출 한 건에 걸린 초다. 서버의 워커 수(jobs.MAX_CONCURRENCY)를 정할
    근거가 이것뿐인데, 예전에는 기록에 없어서 매번 손으로 재야 했다.
    **2026-09-27 에 추가했다 — 그 이전 기록에는 이 칸이 없다.**
    """
    if os.environ.get(SAVE_VAR) == "0":
        _print_call(model, None, error, input_tokens, output_tokens, cost, elapsed)
        return

    now = datetime.now(KST)
    provider = model.split("-")[0]
    stem = f"{now:%y%m%d-%H%M%S}-{provider}"

    # 같은 초에 두 번 나가면 이름이 겹쳐 앞의 기록이 덮인다 (검증 중 실제로 겪었다)
    CALL_DIR.mkdir(parents=True, exist_ok=True)
    path = CALL_DIR / f"{stem}.json"
    serial = 2
    while path.exists():
        path = CALL_DIR / f"{stem}-{serial}.json"
        serial += 1
    filename = path.name

    detail = {
        "at": now.isoformat(timespec="seconds"),
        "model": model,
        "mode": mode,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
        "elapsed_sec": round(elapsed, 1) if elapsed is not None else None,
        "request": request,
        "response": _readable(response_text),
        "error": error,
    }
    path.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")

    index_line = {
        "at": detail["at"],
        "model": model,
        "mode": mode,
        "ok": error is None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
        "elapsed_sec": round(elapsed, 1) if elapsed is not None else None,
        "file": filename,
    }
    with CALL_LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(json.dumps(index_line, ensure_ascii=False) + "\n")

    _remember(f"{CALL_DIR_REL}/{filename}")

    _print_call(model, filename, error, input_tokens, output_tokens, cost, elapsed)


def start_run() -> None:
    """
    이 실행 흐름의 기록 상자를 새로 만든다. 그래프를 돌리기 **전에** 부른다.

    안 부르면 last_call_file() 이 늘 None 이다 — 기록 파일은 그대로 남고, 결과와
    잇는 이름표만 빠진다. 서버는 이름표를 쓰지 않아서 부르지 않는다.
    """
    _CURRENT_RUN.set({"last_call_file": None})


def _remember(path: str) -> None:
    run = _CURRENT_RUN.get()
    if run is not None:
        run["last_call_file"] = path


def last_call_file() -> str | None:
    """이 실행 흐름에서 마지막으로 남긴 호출 기록 파일. 아직 안 불렀으면 None."""
    run = _CURRENT_RUN.get()
    if run is None:
        return None
    return run["last_call_file"]


def _print_call(
    model: str,
    filename: str | None,
    error: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost: float | None,
    elapsed: float | None = None,
) -> None:
    """
    화면 한 줄. 실패했으면 어디를 열어보면 되는지까지 알려준다.
    파일을 안 남겼으면(filename 이 None) 열어 볼 곳이 없으니 실패 이유를 그 자리에 적는다.
    """
    where = f"{CALL_DIR_REL}/{filename}" if filename else "기록 저장 안 함"
    if error is not None:
        reason = where if filename else error
        print(f"[LLM] {model} · 실패 · {reason}", file=sys.stderr)
        return

    cost_text = f" · 약 ${cost:.4f}" if cost is not None else ""
    time_text = f" · {elapsed:.1f}초" if elapsed is not None else ""
    print(
        f"[LLM] {model} · 입력 {input_tokens} · 출력 {output_tokens}{cost_text}{time_text}"
        f" · {where}",
        file=sys.stderr,
    )
