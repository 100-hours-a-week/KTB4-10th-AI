"""
src/engine/graph.py

노드 6개를 그래프로 잇는다. 노드 순서를 아는 곳은 여기뿐이다.

    prepare ─┬─> find_places ─┬─> arrange ──> write ──> build
             └─> find_events ─┘

find_places와 find_events는 State의 다른 칸에 쓰므로 병렬로 돈다.
langgraph가 둘 다 끝나기를 기다렸다가 arrange로 넘긴다.
arrange 가 배치를, write 가 문구를 모델에게 받는다 (2026-10-15 에 plan 하나를 나눴다, #35).
"""

from __future__ import annotations

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from src.engine import nodes
from src.models import Candidate, GraphState, Itinerary, PlanItem, TripRequest

# 이 노드가 끝나면 다음 단계가 시작된다. 서버가 백엔드에 진행 상황을 알리는 데 쓴다
# (src/server/callback.py). 나머지 노드는 순식간이라 단계로 나누지 않는다
STEP_AFTER_NODE = {"prepare": "arranging", "arrange": "writing"}


def build_graph():
    """노드를 늘리거나 순서를 바꾸는 일은 전부 이 함수 안에서."""
    graph = StateGraph(GraphState)

    graph.add_node("prepare", nodes.prepare)
    graph.add_node("find_places", nodes.find_places)
    graph.add_node("find_events", nodes.find_events)
    graph.add_node("arrange", nodes.arrange)
    graph.add_node("write", nodes.write)
    graph.add_node("build", nodes.build)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "find_places")  # 팬아웃 — 둘이 함께 출발한다
    graph.add_edge("prepare", "find_events")
    graph.add_edge("find_places", "arrange")  # 합류 — 둘 다 끝나야 시작된다
    graph.add_edge("find_events", "arrange")
    graph.add_edge("arrange", "write")
    graph.add_edge("write", "build")
    graph.add_edge("build", END)

    return graph.compile()


def run(
    request: TripRequest,
    *,
    places: list[Candidate],
    events: list[Candidate],
    on_step: Callable[[str], None] | None = None,
) -> Itinerary:
    """
    조건 하나를 받아 일정 하나를 돌려준다. 생성의 진입점이다. 재생성은 run_regeneration.

    `places`·`events` 는 **그 지역의 후보 전부**다. 서버에서는 백엔드 DB 가 보내 준 것을
    그대로 넣는다. 테스트와 로컬 도구는 tools/dump_candidates.py 로 덤프에서 만든다.

    고르는 일은 우리가 한다. 여기 들어온 것에서 취향 티어로 추리는 것은 find_places 이고,
    그 규칙은 후보 출처와 무관하다.

    `on_step` 은 단계가 바뀔 때 단계 이름으로 부를 함수다. 안 넘기면 알리지 않는다.
    """
    state = {
        "request": request,
        "given_places": places,
        "given_events": events,
    }
    return _run_graph(state, on_step)


def run_regeneration(
    request: TripRequest,
    *,
    places: list[Candidate],
    events: list[Candidate],
    previous_plan: list[PlanItem],
    feedback: str,
    on_step: Callable[[str], None] | None = None,
) -> Itinerary:
    """재생성 (#44). 그래프는 생성과 같다 — arrange 가 state 의 feedback 을 보고 갈라진다."""
    state = {
        "request": request,
        "given_places": places,
        "given_events": events,
        "previous_plan": previous_plan,
        "feedback": feedback,
    }
    return _run_graph(state, on_step)


def _run_graph(state: dict, on_step: Callable[[str], None] | None) -> Itinerary:
    """
    invoke 대신 stream 으로 돌려 노드가 끝날 때마다 받는다. 그래프가 도는 방식은 같다.
    노드에서 난 예외는 invoke 때처럼 그대로 올라간다.
    """
    result = None
    for update in build_graph().stream(state, stream_mode="updates"):
        # 함께 도는 노드(find_places · find_events)는 한 번에 같이 올 수 있다
        for node_name, output in update.items():
            step = STEP_AFTER_NODE.get(node_name)
            if step is not None and on_step is not None:
                on_step(step)
            if node_name == "build":
                result = output["result"]
    return result
