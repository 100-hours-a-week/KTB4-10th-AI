"""
src/llm/vllm_client.py

**런팟 서버리스 vLLM(자체 모델)을 부르는 부품.** 글쓰기(llm_write)만 쓴다 — 배치는 Claude (#41).

vLLM 은 OpenAI 와 같은 모양의 API 를 열어 주므로 openai SDK 로 부른다. 모양을 llm_client 의
Claude 호출(call_and_record · claude_call)과 나란히 맞춰 두었다 — 하나를 읽으면 다른 하나도
읽히게. 실패 이름(kind)도 같은 것을 써서 로그·/metrics 를 그대로 읽는다.

vllm 이어도 글쓰기 직전에 워커가 켜져 있는지 묻고(worker_ready), 없으면 Claude 로 쓴다.
vLLM 은 정한 시간대에만 켜 두고(런팟 Active Workers) 나머지는 Claude 로 돌리려고 (2026-10-22).

설정은 환경변수가 먼저, 없으면 .env (llm_client.load_api_key 와 같은 방식):
    LLM_WRITE_BACKEND  글쓰기를 누가 하나. claude(비우면) · vllm(워커가 있을 때만)
    VLLM_BASE_URL      예: https://api.runpod.ai/v2/<endpoint-id>/openai/v1
    VLLM_API_KEY       런팟 API 키 (Pod 로 띄웠으면 vLLM 의 --api-key)
    VLLM_MODEL         vLLM 의 --served-model-name (/v1/models 에 나오는 이름)
    VLLM_PROMPT        vLLM 에 보낼 지시문. v7(비우면) · claude
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from src import logs
from src.llm import calllog, llm_client
from src.llm.llm_client import LLMError

WRITE_BACKEND_VAR = "LLM_WRITE_BACKEND"
WRITE_BACKENDS = ("claude", "vllm")
BASE_URL_VAR = "VLLM_BASE_URL"
API_KEY_VAR = "VLLM_API_KEY"
MODEL_VAR = "VLLM_MODEL"
PROMPT_VAR = "VLLM_PROMPT"
PROMPTS = ("v7", "claude")

# 답의 상한 (생각 없음). 학습 데이터(v7) 정답은 최대 2,659토큰(중앙 994)이라 넉넉히 잡았다.
# vLLM 은 「입력 + 이 값」이 --max-model-len 을 넘으면 답을 쓰기 전에 거절한다 — 가장 긴 입력
# (약 9,700) + 4096 이 서빙 길이 16384 안에 들어가게 둔다 (2026-10-17, 학습한 쪽 확인)
MAX_TOKENS = 4096

# 느린 건 GPU 속도 탓이라 시간 초과를 다시 불러도 또 넘는다. SDK 는 연결 오류와 시간 초과를
# 같은 횟수로 재시도해서, 연결 오류 재시도도 함께 포기한다
VLLM_MAX_RETRIES = 0

# /health 를 기다리는 상한. 이보다 늦으면 준비 안 된 것으로 보고 Claude 로 쓴다
HEALTH_TIMEOUT = 5
# /models 를 기다리는 상한. 켜진 워커는 1~2초에 답했고, 켜는 중에는 5초 안에 답이 없었다 (10-22)
MODELS_TIMEOUT = 5


def write_backend() -> str:
    """
    LLM_WRITE_BACKEND 를 읽는다. 비우면 claude.

    모르는 값이면 멈춘다 (llm_mode 와 같은 이유) — 오타를 claude 로 읽으면 자체 모델을
    시험하는 줄 알고 Sonnet 결과를 보게 된다. 서버가 켜질 때 한 번 불러 미리 막는다.
    """
    raw = os.environ.get(WRITE_BACKEND_VAR, "").strip().lower()
    if not raw:
        return "claude"
    if raw not in WRITE_BACKENDS:
        raise LLMError(
            f"{WRITE_BACKEND_VAR} 는 claude 또는 vllm 이어야 합니다: {raw!r}"
        )
    return raw


def prompt_choice() -> str:
    """
    VLLM_PROMPT 를 읽는다. 비우면 v7 — 모델이 학습한 지시문.

    claude 는 Claude 글쓰기 지시문(llm_write.SYSTEM_PROMPT, #39 3차)을 vLLM 에도 보낸다.
    학습 때와 다른 지시문이라 따른다는 보장이 없다 — v7 과 나란히 재 보려고 둔다 (#41).
    모르는 값이면 멈춘다 (write_backend 와 같은 이유).
    """
    raw = os.environ.get(PROMPT_VAR, "").strip().lower()
    if not raw:
        return "v7"
    if raw not in PROMPTS:
        raise LLMError(f"{PROMPT_VAR} 는 v7 또는 claude 여야 합니다: {raw!r}")
    return raw


def check_settings() -> None:
    """
    vllm 일 때 지시문 선택이 맞고 주소·키·모델 이름이 다 있나.
    없으면 LLMError — 서버를 켤 때 부른다.
    """
    prompt_choice()
    for var in (BASE_URL_VAR, API_KEY_VAR, MODEL_VAR):
        # 키가 아닌 값도 같은 방식(환경변수 → .env)으로 읽는다
        llm_client.load_api_key(var)


def worker_ready() -> bool:
    """
    런팟 서버리스에 지금 글쓰기를 받을 워커가 있나. 없거나 확인이 실패하면 False.

    두 단계로 묻는다.
        1. /health — 워커가 켜졌나 (running). 워커를 깨우지 않는다
        2. /models — 켜졌으면 실제로 답하나. 켜는 중(모델을 올리는 약 2분 30초)과
           켜짐은 /health 로는 똑같이 보인다
    /models 를 먼저 묻지 않는 이유: 꺼진 워커를 깨워 콜드 스타트와 과금이 생긴다.
    """
    base_url = llm_client.load_api_key(BASE_URL_VAR)
    api_key = llm_client.load_api_key(API_KEY_VAR)

    try:
        health = _get_json(health_url(base_url), api_key, HEALTH_TIMEOUT)
    # 연결 실패·시간 초과·HTTP 오류(OSError)와 깨진 JSON(ValueError)
    except (OSError, ValueError) as exc:
        logs.write("WARNING", "vllm_not_ready", reason="health_failed", error=str(exc))
        return False
    if not is_ready(health):
        logs.write("INFO", "vllm_not_ready", reason="no_worker", health=health)
        return False

    # 켜는 중이면 답이 없다. 우리가 끊어도 런팟 대기열에는 남아, 다음 요청의 확인에서
    # is_ready 의 「대기열이 쌓였는데 처리 중 0」으로 걸린다
    try:
        _get_json(f"{base_url.rstrip('/')}/models", api_key, MODELS_TIMEOUT)
    except (OSError, ValueError) as exc:
        logs.write("INFO", "vllm_not_ready", reason="worker_starting", error=str(exc))
        return False
    return True


def _get_json(url: str, api_key: str, timeout: int) -> dict:
    """GET 해서 JSON 으로 읽는다. /health 와 /models 가 같은 모양이라 함께 쓴다."""
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key}"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def health_url(base_url: str) -> str:
    """서버리스 주소 .../v2/<id>/openai/v1 → .../v2/<id>/health"""
    root = base_url.rstrip("/").removesuffix("/openai/v1")
    return f"{root}/health"


def is_ready(health: dict) -> bool:
    """
    /health 응답으로 판단한다 (2026-10-22 관찰).
        꺼짐    running 0 (idle·ready 는 꺼져 있어도 1 이라 보지 않는다)
        켜는 중  running 1 — 켜짐과 같다. 대기열이 쌓였는데 처리 중이 0 이면 켜는 중으로 본다.
                대기열이 비었으면 여기서는 못 가른다 → worker_ready 의 /models 가 가른다
        켜짐    running 1
    """
    workers = health.get("workers", {})
    jobs = health.get("jobs", {})
    running = workers.get("running", 0)
    queue_stuck = jobs.get("inQueue", 0) > 0 and jobs.get("inProgress", 0) == 0
    return running >= 1 and not queue_stuck


def call_and_record(
    system: str,
    payload: str,
    output_format,
    timeout: int,
    request: dict,
):
    """
    vLLM 을 1회 부르고 호출 기록을 남긴다. [런팟 호출 — GPU 시간 과금]

    request 는 기록에 남길 입력이다 (모델에게 보내는 것은 system · payload).
    비용 칸은 비운다 — 런팟은 토큰이 아니라 켜 둔 시간으로 과금된다.
    """
    # llm_client.call_and_record 의 import anthropic 과 같은 이유 (실제로 부를 때만 SDK 를 불러온다)
    import openai

    model = llm_client.load_api_key(MODEL_VAR)
    client = openai.OpenAI(
        base_url=llm_client.load_api_key(BASE_URL_VAR),
        api_key=llm_client.load_api_key(API_KEY_VAR),
        timeout=timeout,
        max_retries=VLLM_MAX_RETRIES,
    )

    started = time.monotonic()
    try:
        response = vllm_call(client, model, system, payload, output_format)
    except LLMError as exc:
        calllog.record(
            model,
            request,
            mode="llm",
            error=str(exc),
            elapsed=time.monotonic() - started,
        )
        raise

    parsed = response.choices[0].message.parsed
    usage = response.usage
    calllog.record(
        model,
        request,
        mode="llm",
        response_text=parsed.model_dump_json(),
        input_tokens=usage.prompt_tokens if usage else None,
        output_tokens=usage.completion_tokens if usage else None,
        elapsed=time.monotonic() - started,
    )
    return parsed


def vllm_call(client, model: str, system: str, payload: str, output_format):
    """
    SDK 를 부르고 실패를 전부 LLMError 로 바꾼다. 기록은 부르는 쪽에서 한다.

    response_format 에 응답 모양(WriterDraft)을 넘기면 vLLM 이 그 JSON 모양 밖의 글을
    못 쓰게 막는다 (guided decoding). 학습 데이터의 schema 와 같은 모양이다.
    """
    import openai
    import pydantic

    try:
        response = client.chat.completions.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": payload},
            ],
            response_format=output_format,
        )
    # 답을 쓰다 MAX_TOKENS 에 닿았다. SDK 가 해석하기 전에 알려 준다
    except openai.LengthFinishReasonError as exc:
        raise LLMError(
            f"출력이 {MAX_TOKENS} 토큰에서 잘렸습니다", kind="truncated"
        ) from exc
    except openai.RateLimitError as exc:
        raise LLMError(f"호출 한도 초과: {exc}") from exc
    # 시간 초과는 네트워크 실패의 한 종류라, 아래 APIConnectionError 보다 먼저 잡아야 갈린다
    except openai.APITimeoutError as exc:
        raise LLMError(f"시간 초과: {exc}", kind="timeout") from exc
    # 응답 글자를 output_format 으로 읽다 실패했다 (깨진 JSON · 칸 누락)
    except pydantic.ValidationError as exc:
        raise LLMError(f"응답 파싱 실패: {exc}", kind="parse") from exc
    except openai.APIConnectionError as exc:
        raise LLMError(f"네트워크 실패: {exc}") from exc
    except openai.APIStatusError as exc:
        raise LLMError(f"API 오류 {exc.status_code}: {exc.message}") from exc

    message = response.choices[0].message
    if message.parsed is None:
        raise LLMError(f"응답이 비었습니다: {message.content!r}", kind="parse")
    return response
