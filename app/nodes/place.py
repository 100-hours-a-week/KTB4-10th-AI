"""
app/nodes/place.py

파이프라인 1단계: 장소 추천 노드 (steps key: finding_places).
지역·취향 조건으로 TourAPI를 조회해 장소 후보를 검색·필터링한다.

LLM 사용은 "조건부"다. 정상 경로(조회 결과 있음)에서는 LLM을 부르지 않고,
시/도로 범위를 넓혀도 원래 지역에 후보가 없을 때 대체 지역 선별에만 쓴다.

행사 추천 노드(app/nodes/event.py)와 병렬로 실행된다. State의 서로 다른 칸에
쓰기 때문에 동시에 실행해도 충돌하지 않는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.state import PipelineState


async def recommend_places(state: "PipelineState") -> dict:
    """
    장소 추천 노드.

    처리 흐름 (팀 문서 의사코드 3-2 기준):
      1. 조건 변환: 지역명 → TourAPI 지역코드, 취향 → 카테고리 코드
      2. TourAPIClient.search_places(시/군/구 포함)로 조회
      3. 후보가 있으면 조건으로 필터링해 State의 장소목록에 담고 종료
      4. 0건이면 sigungu_code를 빼고 시/도 범위로 재조회 (LLM 미사용)
      5. 확장 조회도 0건이면 State의 오류에 "장소를 찾을 수 없음" 기록 후 종료
      6. 확장 후보가 있으면 LLM이 그 후보 안에서만 취향에 맞는 대체 지역을 선별
         (모델이 지역명을 새로 만들지 않게 프롬프트로 제한), 장소목록은 비워둠

    0건은 실패가 아니라 정상 상황으로 본다 (지방 소도시·비수기에 흔함).

    Args:
        state: 파이프라인 공유 상태. 입력 조건(지역·취향)을 읽는다.

    Returns:
        LangGraph 규약에 따라 갱신할 State 필드만 담은 dict.
        장소목록(TourPlace 목록), 실패 시 오류.

    TODO: 취향 대분류/중분류 → TourAPI 카테고리 코드 매핑 테이블 없음 (임의로 만들지 않음)
    TODO: 지역명 → TourAPI 지역코드 매핑 테이블 없음
    TODO: 3번 "조건으로 필터링"의 구체 기준 미정 (이미지 없는 장소 제외 여부 등)
    TODO: 6번 대체 지역 결과를 State 어느 칸에 담을지 미정
          (문서 의사코드엔 "대체지역"이 있으나 요청하신 State 필드 목록엔 없음)
    TODO: TourAPIClient/LLMClient를 이 노드가 어떻게 받을지 미정
          (모듈 전역 인스턴스 / LangGraph config로 전달 / 팩토리 함수 중 결정 필요)
    TODO: 단계목록·현재단계 갱신을 노드 안에서 할지, 그래프 실행부에서 일괄 처리할지 미정
    """
    raise NotImplementedError
