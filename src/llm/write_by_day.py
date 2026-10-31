"""
src/llm/write_by_day.py

**Claude 글쓰기를 날짜별로 나눠 동시에 부른다.** (2026-10-31)

    START ─┬─ Send ─> write_cover (제목 · 소개)  ─┐
           ├─ Send ─> write_day (1일차)          ─┤
           └─ Send ─> write_day (N일차)          ─┴─> merge ─> END

왜: 한 번에 쓰면 출력이 장소 수만큼 늘어 7일 · 35곳이 137초로 글쓰기 상한(WRITE_TIMEOUT 120초)을
넘었다. 나누면 걸리는 시간이 「가장 늦은 하루」만큼이 된다 — 같은 일정이 42초.
대신 지시문을 호출마다 보내 비용이 늘었다 (종로 7일 $0.215 → $0.312, 청송 3일 $0.118 → $0.151).

Send 는 날짜 수처럼 실행 중에 정해지는 개수만큼 노드를 띄운다. 띄운 노드는 같은 단계에서 함께 돌고,
결과는 day_drafts 칸에 이어 붙는다(operator.add). merge 는 그 단계가 다 끝나야 시작된다.
동시에 도는 수는 정하지 않으면 파이썬 기본값(CPU 수 + 4)이다 — 글쓰기는 답을 기다리기만 해서
CPU 와 상관없으므로 MAX_CONCURRENCY 로 정해 둔다.

합친 답은 한 번에 쓴 답(WriterDraft)과 같은 모양이라 뒤의 검사 · build 는 그대로다.
하루라도 실패하면 작업 전체가 실패다 (한 번에 쓸 때와 같다).
vLLM 은 학습한 모양이 한 번에 쓰기라 나누지 않는다 (llm_write._vllm_write_all).
"""

from __future__ import annotations

import json
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from src.llm import llm_client, llm_write
from src.models import CoverDraft, DayDraft, DayWriterDraft, TripRequest, WriterDraft

# 7일 일정 = 표지 1 + 날짜 7. 한 번에 다 돌게
MAX_CONCURRENCY = 8


def split_payload(payload: dict) -> tuple[dict, list[dict]]:
    """
    한 번에 쓰는 입력(llm_write._plan_to_payload)을 표지용 하나와 날짜용 여럿으로 나눈다.
    표지용에는 장소 이름 · 분류만 — overview 까지 넣으면 나눈 의미가 줄어든다.
    """
    cover_days = []
    day_payloads = []
    for day in payload["days"]:
        names = []
        for place in day["places"]:
            names.append({"name": place["name"], "category": place["category"]})
        cover_days.append({"day": day["day"], "date": day["date"], "places": names})
        day_payloads.append({"trip": payload["trip"], "days": [day]})
    cover_payload = {"trip": payload["trip"], "days": cover_days}
    return cover_payload, day_payloads


def write_day(day_payload: dict, trip: TripRequest, model: str) -> DayWriterDraft:
    """하루치 글을 Claude 에게 1회 받는다. [비용 발생]"""
    system = llm_write.DAY_SYSTEM_PROMPT
    return _call("write_day", system, day_payload, DayWriterDraft, trip, model)


def write_cover(cover_payload: dict, trip: TripRequest, model: str) -> CoverDraft:
    """표지(제목 · 소개)를 Claude 에게 1회 받는다. [비용 발생]"""
    system = llm_write.COVER_SYSTEM_PROMPT
    return _call("write_cover", system, cover_payload, CoverDraft, trip, model)


def _call(stage: str, system: str, payload: dict, output_format, trip, model: str):
    request = {
        "stage": stage,  # 한 작업에 호출이 여럿이라 기록에서 가른다
        "trip": trip.model_dump(),
        "system": system,
        "plan": payload,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return llm_client.call_and_record(
        model,
        system,
        text,
        output_format,
        llm_write.WRITE_TIMEOUT,
        request,
    )


def merge_drafts(
    cover: CoverDraft, day_drafts: list[tuple[int, DayWriterDraft]]
) -> WriterDraft:
    """
    하루 답들을 한 번에 쓴 답과 같은 모양으로 합친다. 동시에 돌아 끝난 순서가 섞여 들어오므로
    일차로 다시 세운다.
    """
    ordered = sorted(day_drafts, key=lambda pair: pair[0])
    days = []
    reasons = []
    for day, draft in ordered:
        days.append(DayDraft(day=day, summary=draft.summary))
        reasons.extend(draft.reasons)
    return WriterDraft(title=cover.title, intro=cover.intro, days=days, reasons=reasons)


# ── 작은 그래프 ─────────────────────────────────────────────


class WriteState(TypedDict, total=False):
    payload: dict  # 한 번에 쓸 때의 입력
    trip: TripRequest  # 기록용
    model: str
    cover: CoverDraft
    # (일차, 그날 답). 날짜 노드 여럿이 함께 쓰는 칸이라 이어 붙이는 방식을 정해 둔다
    day_drafts: Annotated[list[tuple[int, DayWriterDraft]], operator.add]
    draft: WriterDraft


def fan_out(state: WriteState) -> list[Send]:
    """표지 하나와 날짜 수만큼의 하루 쓰기를 띄운다."""
    cover_payload, day_payloads = split_payload(state["payload"])
    common = {"trip": state["trip"], "model": state["model"]}
    sends = [Send("write_cover", {**common, "cover_payload": cover_payload})]
    for day_payload in day_payloads:
        day = day_payload["days"][0]["day"]
        task = {**common, "day": day, "day_payload": day_payload}
        sends.append(Send("write_day", task))
    return sends


def cover_node(task: dict) -> dict:
    cover = write_cover(task["cover_payload"], task["trip"], task["model"])
    return {"cover": cover}


def day_node(task: dict) -> dict:
    draft = write_day(task["day_payload"], task["trip"], task["model"])
    return {"day_drafts": [(task["day"], draft)]}


def merge_node(state: WriteState) -> dict:
    return {"draft": merge_drafts(state["cover"], state["day_drafts"])}


def build_write_graph():
    graph = StateGraph(WriteState)
    graph.add_node("write_cover", cover_node)
    graph.add_node("write_day", day_node)
    graph.add_node("merge", merge_node)

    graph.add_conditional_edges(START, fan_out, ["write_cover", "write_day"])
    graph.add_edge("write_cover", "merge")
    graph.add_edge("write_day", "merge")
    graph.add_edge("merge", END)
    return graph.compile()


def write_by_day(payload: dict, trip: TripRequest, model: str) -> WriterDraft:
    """날짜별로 나눠 동시에 쓰고, 한 번에 쓴 답과 같은 모양으로 돌려준다. [비용 발생]"""
    state = {"payload": payload, "trip": trip, "model": model}
    config = {"max_concurrency": MAX_CONCURRENCY}
    final = build_write_graph().invoke(state, config=config)
    return final["draft"]
