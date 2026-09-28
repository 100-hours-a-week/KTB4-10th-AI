"""
src/engine/graph.py

노드 5개를 그래프로 잇는다. 노드 순서를 아는 곳은 여기뿐이다.

    prepare ─┬─> find_places ─┬─> plan ──> build
             └─> find_events ─┘

find_places와 find_events는 State의 다른 칸에 쓰므로 병렬로 돈다.
langgraph가 둘 다 끝나기를 기다렸다가 plan으로 넘긴다.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from src.engine import nodes
from src.models import Candidate, GraphState, Itinerary, TripRequest


def build_graph():
    """노드를 늘리거나 순서를 바꾸는 일은 전부 이 함수 안에서."""
    graph = StateGraph(GraphState)

    graph.add_node("prepare", nodes.prepare)
    graph.add_node("find_places", nodes.find_places)
    graph.add_node("find_events", nodes.find_events)
    graph.add_node("plan", nodes.plan)
    graph.add_node("build", nodes.build)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "find_places")  # 팬아웃 — 둘이 함께 출발한다
    graph.add_edge("prepare", "find_events")
    graph.add_edge("find_places", "plan")  # 합류 — 둘 다 끝나야 시작된다
    graph.add_edge("find_events", "plan")
    graph.add_edge("plan", "build")
    graph.add_edge("build", END)

    return graph.compile()


def run(
    request: TripRequest,
    *,
    places: list[Candidate],
    events: list[Candidate],
) -> Itinerary:
    """
    조건 하나를 받아 일정 하나를 돌려준다. v0의 유일한 진입점이다.

    `places`·`events` 는 **그 지역의 후보 전부**다. 서버에서는 백엔드 DB 가 보내 준 것을
    그대로 넣는다. 테스트와 로컬 도구는 tools/dump_candidates.py 로 덤프에서 만든다.

    고르는 일은 우리가 한다. 여기 들어온 것에서 취향 티어로 추리는 것은 find_places 이고,
    그 규칙은 후보 출처와 무관하다.
    """
    state = {
        "request": request,
        "given_places": places,
        "given_events": events,
    }
    return build_graph().invoke(state)["result"]
