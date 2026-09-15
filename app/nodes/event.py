"""
app/nodes/event.py

파이프라인 1단계: 행사 추천 노드 (steps key: checking_events).
여행 기간 내 열리는 행사·축제를 TourAPI로 조회한다. LLM을 사용하지 않는다.

장소 추천 노드(app/nodes/place.py)와 병렬로 실행된다. State의 서로 다른 칸에
쓰기 때문에 동시에 실행해도 충돌하지 않는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.state import PipelineState


async def recommend_events(state: "PipelineState") -> dict:
    """
    행사 추천 노드.

    처리 흐름 (팀 문서 의사코드 3-3 기준):
      1. 조건 변환: 지역명 → TourAPI 지역코드, 여행 기간 → TourAPI 날짜 파라미터
      2. TourAPIClient.search_events로 조회
      3. 결과를 State의 행사목록에 담는다

    0건이어도 실패가 아니다 (행사가 없는 기간은 정상 상황). 장소 추천과 달리
    범위 확장·LLM 폴백이 없다.

    Args:
        state: 파이프라인 공유 상태. 입력 조건(지역·기간)을 읽는다.

    Returns:
        LangGraph 규약에 따라 갱신할 State 필드만 담은 dict.
        행사목록(TourEvent 목록).

    TODO: 지역명 → TourAPI 지역코드 매핑 테이블 없음 (place.py와 공유 예정)
    TODO: 백엔드 날짜 형식(YY.MM.DD) → TourAPI 날짜 형식 변환 위치 미정
    TODO: TourAPI 조회 실패(재시도 후에도 장애) 시 파이프라인 전체를 실패시킬지,
          행사목록을 비운 채 진행할지 미정 — 행사는 부가 정보라 후자도 가능
    TODO: TourAPIClient를 이 노드가 어떻게 받을지 미정 (place.py와 동일)
    """
    raise NotImplementedError
