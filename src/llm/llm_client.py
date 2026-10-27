"""
src/llm/llm_client.py

**Claude 를 부르는 공통 부품.** 배치(llm_arrange)와 글쓰기(llm_write)가 같이 쓴다 —
설정 · LLMError · 모드(real/fake) · 키 읽기 · SDK 호출 · 호출 기록.

배치 파일에 두면 글쓰기가 배치 파일에서 호출 부품을 가져다 쓰는 모양이 되어 따로 뗐다.
지시문·입력·검사는 단계마다 제 파일에 있다.
"""

from __future__ import annotations

import os
import time

from src.llm import calllog
from src.paths import REPO_ROOT

DEFAULT_MODEL = "claude-sonnet-5"
# 생각 포함. 최악 조합(종로구 관심 장소 177곳 · 7일 35곳)이 16000 중 15586 을 써서
# 올렸다 (2026-10-11). 한도는 상한일 뿐이라 쓰지 않으면 비용이 늘지 않는다
MAX_TOKENS = 20000
EFFORT = "medium"  # 기본값은 high. 일정 배치가 무거운 일이 아니라 낮춰서 시작한다

CLAUDE_MAX_RETRIES = 1

ENV_PATH = REPO_ROOT / ".env"
MODEL_VAR = "LLM_MODEL"
ANTHROPIC_KEY_VAR = "ANTHROPIC_API_KEY"

# real (비워 둬도 real) · fake. 인프라에 알려 준 이름이라 바꾸면 클라우드팀에도 알린다
MODE_VAR = "LLM_MODE"
MODES = ("real", "fake")


def model_name() -> str:
    """부를 모델. LLM_MODEL 이 비면 DEFAULT_MODEL."""
    return os.environ.get(MODEL_VAR, "").strip() or DEFAULT_MODEL


class LLMError(RuntimeError):
    """
    LLM 호출이나 응답 검증이 실패했다. 작업은 generation_failed 로 끝난다.

    kind 는 로그와 /metrics 에서 원인을 가르는 이름이다. 계약 응답의 코드와는 무관하다.
        call       — 호출 실패 (한도 · 네트워크 · API 오류 · 거부)
        timeout    — 호출이 시간 상한을 넘었다
        parse      — 응답이 DayPlanDraft · WriterDraft 모양이 아니다
        truncated  — 출력이 MAX_TOKENS 에서 잘렸다 (생각 중이든 답을 쓰던 중이든)
        validation — 모양은 맞는데 규칙을 어겼다 (_validate_all · llm_write 의 검사)
        fake       — 가짜 모드에서 가짜가 실패했다

    stage 는 어느 단계에서 났나 ("arrange" · "write"). 어느 단계가 자주 틀리는지 재려고
    실패 이름을 llm_arrange_validation 처럼 가른다 (#35).
    """

    def __init__(self, message: str, kind: str = "call", stage: str = ""):
        super().__init__(message)
        self.kind = kind
        self.stage = stage


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


def call_and_record(
    model: str,
    system: str,
    payload: str,
    output_format,
    timeout: int,
    request: dict,
):
    """
    Claude 를 1회 부르고 호출 기록을 남긴다. [비용 발생]

    배치와 글쓰기(llm_write)가 같이 쓴다 — 기록·비용 계산이 두 벌로 갈라지지 않게.
    request 는 기록에 남길 입력이다 (모델에게 보내는 것은 system · payload).
    """
    # 파일 맨 위가 아니라 여기서 불러온다 — 실제로 부를 때만 SDK 가 올라오고,
    # 가짜 모드 서버와 테스트는 아예 불러오지 않는다 (불러오기 0.3초쯤, 10-27 측정)
    import anthropic

    client = anthropic.Anthropic(
        api_key=load_api_key(ANTHROPIC_KEY_VAR),
        timeout=timeout,
        max_retries=CLAUDE_MAX_RETRIES,
    )

    started = time.monotonic()
    try:
        response = claude_call(client, model, system, payload, output_format)
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


def claude_call(client, model: str, system: str, payload: str, output_format):
    """
    SDK 를 부르고 실패를 전부 LLMError 로 바꾼다. 기록은 부르는 쪽에서 한다.

    배치(DayPlanDraft)와 글쓰기(WriterDraft)가 함께 쓴다 — 지시문과 응답 모양만 다르다.
    """
    import anthropic
    import pydantic

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            messages=[{"role": "user", "content": payload}],
            output_format=output_format,
        )
    except anthropic.RateLimitError as exc:
        raise LLMError(f"호출 한도 초과: {exc}") from exc
    # 시간 초과는 네트워크 실패의 한 종류라, 아래 APIConnectionError 보다 먼저 잡아야 갈린다
    except anthropic.APITimeoutError as exc:
        raise LLMError(f"시간 초과: {exc}", kind="timeout") from exc
    # SDK 가 응답 글자를 output_format 으로 읽다 실패했다 (깨진 JSON · 칸 누락 · 잘린 응답)
    except pydantic.ValidationError as exc:
        # 답을 쓰다 MAX_TOKENS 에 닿으면 JSON 이 중간에 끊겨 'EOF while parsing' 이 된다.
        # SDK 가 해석부터 하고 던져서 stop_reason 을 볼 수 없다
        if "EOF while parsing" in str(exc):
            raise LLMError(
                f"출력이 {MAX_TOKENS} 토큰에서 잘렸습니다", kind="truncated"
            ) from exc
        raise LLMError(f"응답 파싱 실패: {exc}", kind="parse") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"네트워크 실패: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"API 오류 {exc.status_code}: {exc.message}") from exc

    # 생각만 하다 MAX_TOKENS 에 닿으면 답 칸이 비어 SDK 가 오류 없이 돌려준다
    if response.stop_reason == "max_tokens":
        raise LLMError(f"출력이 {MAX_TOKENS} 토큰에서 잘렸습니다", kind="truncated")
    if response.stop_reason == "refusal":
        raise LLMError(f"모델이 거부했습니다: {response.stop_details}")

    return response
