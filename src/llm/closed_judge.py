"""
src/llm/closed_judge.py

**방문할 수 없는 곳 판정.** (2026-11-03, #74)
후보의 소개글(overview)을 Haiku 가 읽고 장소마다 전체 불가 · 일부 제한 · 상관없음 중 하나로 답한다.
판정으로 후보를 고치는 일(전체 불가 빼기 · 일부 제한 안내 붙이기)은 nodes.filter_closed 가 한다.

50곳씩 묶어 동시에 부른다 — 한 번에 보내면 종로 350곳이 57초 걸렸고 제주시 441곳은
출력 상한에서 잘렸다 (docs/claude/plan/closed-place-judge-test.md).
실패한 묶음은 판정 없이 둔다 — 그 곳들은 빠지지 않을 뿐이고 일정 생성은 멈추지 않는다.
"""

from __future__ import annotations

import contextvars
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel

from src import logs
from src.llm import llm_client, llm_write
from src.llm.llm_client import LLMError
from src.models import Candidate

JUDGE_MODEL = "claude-haiku-5-5"
CHUNK_SIZE = 50
MAX_WORKERS = 10
# 작업 상한 600초(jobs.JOB_TIMEOUT)에서 배치·글쓰기의 최악(546초)을 빼면 약 50초가 남는다.
# 재시도하면 넘으므로 한 번만 부른다. 실험에서 50곳 묶음은 11~22초
JUDGE_TIMEOUT = 45

Status = Literal["전체 불가", "일부 제한", "상관없음"]

JUDGE_PROMPT = """여행 일정에 넣을 후보 장소들의 소개글(overview)을 읽고,
장소마다 **지금** 방문할 수 있는지 판정합니다.

장소마다 status 를 셋 중 하나로 답합니다.
  전체 불가  그 장소 전체를 지금 방문할 수 없다는 안내가 있다
             (예: "※ 산불로 건물이 전소되어 방문 전 참고", "현재는 출입이 불가하다",
                  "2021년 폐업하여 사라졌다")
             일반 여행자는 받을 수 없는 허가로만 들어갈 수 있는 곳도 전체 불가다
             (예: "관리 및 학술 목적으로 출입하려면 허가를 받아야 한다")
  일부 제한  방문은 할 수 있지만 지금 일부가 없거나 막혀 있다는 안내가 있다
             (예: "건물 일부가 소실되어 방문 전 참고", "현재 일부 구간은 출입이 제한된다")
             장소 안에는 못 들어가도 근처에서 보거나 즐길 수 있으면 일부 제한이다
             (예: "등대는 일반인에게 개방하지 않는다", "현재 상시 개방되고 있지 않다")
  상관없음   그런 안내가 없다. 아래도 모두 상관없음이다
             - 지나간 일: "임진왜란 때 전소되었다가 1660년 중건",
               "1871년 서원철폐령으로 폐쇄되었다가 복원",
               "오랫동안 출입이 금지되어 자연이 잘 보존된 곳이다"
             - 이용 수칙: "자전거 출입 금지", "숯불 바비큐 이용 불가"
             - 정해진 쉬는 날·철마다의 운영: "매주 월요일 휴관", "여름철에만 개장"
             - 일반 여행자도 사전 신청·예약하면 갈 수 있다: "사전 신청에 의한 견학이 가능하다"
             - 소개글이 비어 있다

evidence 에는 전체 불가 · 일부 제한일 때 판단의 근거가 된 소개글 문장을 그대로 옮깁니다.
상관없음이면 빈 문자열로 둡니다.
받은 장소마다 빠짐없이 하나씩 답하고, content_id 는 받은 값을 그대로 씁니다."""


class Verdict(BaseModel):
    content_id: str
    # 판정보다 근거를 먼저 쓰게 하려고 앞에 둔다
    evidence: str
    status: Status


class JudgeResult(BaseModel):
    verdicts: list[Verdict]


def judge_all(candidates: list[Candidate]) -> dict[str, Verdict]:
    """
    후보 전부를 판정한다. [비용 발생] 돌려주는 것은 content_id → 판정.
    판정이 없는 곳(실패한 묶음 · 모델이 빠뜨린 곳)은 결과에 없다 — 빼지 않는다는 뜻이다.
    가짜 모드는 부르지 않고 빈 결과를 돌려준다.
    """
    if llm_client.llm_mode() == "fake":
        return {}

    chunks = [
        candidates[start : start + CHUNK_SIZE]
        for start in range(0, len(candidates), CHUNK_SIZE)
    ]
    if not chunks:
        return {}

    workers = min(len(chunks), MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # 호출 기록을 이 작업에 묶는 ContextVar(calllog._CURRENT_RUN)는 직접 띄운 실에
        # 저절로 넘어가지 않는다 — 묶음마다 지금 문맥을 복사해 넘긴다
        futures = [
            pool.submit(contextvars.copy_context().run, _judge_chunk, chunk)
            for chunk in chunks
        ]
        results = [future.result() for future in futures]

    verdicts = {}
    for result in results:
        verdicts.update(result)
    return verdicts


def _judge_chunk(chunk: list[Candidate]) -> dict[str, Verdict]:
    """한 묶음을 1회 부른다. [비용 발생] 실패하면 빈 결과."""
    sent = [
        {
            "content_id": candidate.content_id,
            "name": candidate.name,
            "overview": llm_write._full_overview(candidate.overview),
        }
        for candidate in chunk
    ]
    payload = json.dumps(sent, ensure_ascii=False)
    request = {"stage": "judge", "system": JUDGE_PROMPT, "places": sent}

    try:
        result = llm_client.call_and_record(
            JUDGE_MODEL,
            JUDGE_PROMPT,
            payload,
            JudgeResult,
            JUDGE_TIMEOUT,
            request,
            max_retries=0,
        )
    except LLMError as exc:
        logs.write("WARNING", "judge_chunk_failed", places=len(chunk), error=str(exc))
        return {}

    # 보내지 않은 id 를 지어낼 수 있다 — 버린다
    sent_ids = {candidate.content_id for candidate in chunk}
    return {v.content_id: v for v in result.verdicts if v.content_id in sent_ids}
