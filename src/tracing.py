"""
src/tracing.py

랭퓨즈로 일정 생성 과정을 기록한다 (#76). 키가 없으면 SDK 가 스스로 꺼져 아무것도 보내지 않는다.

노드 기록에는 그때까지 쌓인 State 가 통째로 실린다. 후보 수백 곳이 노드마다 되풀이해 나가지
않게 큰 칸은 줄여 보낸다. 모델이 받은 프롬프트는 모델 호출 기록에 통째로 남긴다 (11-04 결정).
"""

import logging
import os

from langfuse import Langfuse

from src.llm import llm_client
from src.llm.llm_client import LLMError

# 스테이징·운영·로컬은 LANGFUSE_TRACING_ENVIRONMENT(staging · production · local)로 가른다
LANGFUSE_VARS = [
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_TRACING_ENVIRONMENT",
]

NAMES_SHOWN = 5

CANDIDATE_KEYS = {"given_places", "given_events", "places", "events"}
# PlanItem 에는 이름이 없다 — 이름은 배치 호출의 응답 기록에서 본다
COUNT_KEYS = {"plan", "previous_plan", "reasons", "overview_summaries"}
# 글쓰기 그래프의 원재료. 같은 내용이 모델 호출 기록에 통째로 있다
PAYLOAD_KEYS = {"payload", "cover_payload", "day_payload"}


def init() -> bool:
    """서버가 켜질 때 한 번. 이후 get_client() 도 이 mask 를 쓴다. 켜졌는지를 돌려준다."""
    # SDK 는 환경변수만 읽는다. 로컬 서버는 .env 에만 키가 있으므로 옮겨 넣는다
    # (배포는 이미 환경변수라 load_api_key 가 그 값을 그대로 돌려준다)
    for var in LANGFUSE_VARS:
        try:
            os.environ[var] = llm_client.load_api_key(var)
        except LLMError:
            pass  # 없으면 기록을 끈 채로 둔다

    enabled = bool(os.environ.get("LANGFUSE_PUBLIC_KEY"))
    if not enabled:
        # 키가 없으면 SDK 가 get_client() 를 부를 때마다 경고를 한 줄씩 남긴다.
        # 꺼 둔 것이 의도이고, 평문 경고가 JSON 한 줄 로그를 깨므로 막는다
        logging.getLogger("langfuse").setLevel(logging.ERROR)
    Langfuse(mask=mask)
    return enabled


def mask(*, data, **kwargs):
    # dict 로 오는 것은 노드 입출력뿐이다. 모델 호출의 프롬프트(메시지 목록)와 응답은 그대로 둔다
    if not isinstance(data, dict):
        return data
    masked = {}
    for key, value in data.items():
        masked[key] = _shrink(key, value)
    return masked


# 노드 기록기(CallbackHandler)가 pydantic 객체를 dict 로 바꿔 넘긴다 — 그래서 ["name"] 으로 읽는다
def _shrink(key: str, value):
    if key in CANDIDATE_KEYS:
        names = ", ".join(candidate["name"] for candidate in value[:NAMES_SHOWN])
        return f"{len(value)}곳: {names} …"
    if key in COUNT_KEYS:
        return f"{len(value)}개"
    if key in PAYLOAD_KEYS:
        return "생략 — 모델 호출 기록에 있음"
    if key == "result":
        return _names_by_day(value)
    return value


def _names_by_day(itinerary: dict) -> dict[str, str]:
    days = {}
    for day in itinerary["itinerary"]:
        names = [place["name"] for place in day["places"]]
        days[f"{day['day']}일차"] = " → ".join(names)
    return days
