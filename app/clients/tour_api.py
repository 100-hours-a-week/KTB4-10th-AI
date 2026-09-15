"""
app/clients/tour_api.py

한국관광공사 TourAPI(공공데이터포털) 호출을 감싸는 클라이언트.
노드는 이 클라이언트를 통해서만 TourAPI를 호출하고, 실제 엔드포인트·인증
방식·파라미터 이름 같은 세부사항을 몰라도 되게 한다.

TODO(스키마 조사 필요) — 아직 확정 안 된 것들:
  1. 엔드포인트
     - 장소 목록 조회: 정확한 오퍼레이션명/버전 미확정
       (예: areaBasedList 계열로 추정되나 버전(1/2)·명칭 확인 필요)
     - 행사 조회: 정확한 오퍼레이션명/버전 미확정
       (예: searchFestival 계열로 추정되나 확인 필요)
  2. 인증
     - 서비스키를 쿼리 파라미터로 보내는지, 헤더로 보내는지
     - 디코딩된 키를 쓰는지 인코딩된 키를 쓰는지 (공공데이터포털 특유의 이슈)
  3. 요청 파라미터명
     - 지역코드/시군구코드 파라미터명 (areaCode/sigunguCode 등으로 추정, 미확정)
     - 카테고리 코드 파라미터명·체계 (cat1/cat2/cat3 3단계인지 여부 등 미확정)
     - 행사 기간 파라미터명·날짜 포맷 (YYYYMMDD로 추정되나 미확정)
     - 응답 형식 지정 파라미터(JSON 강제 여부)
  4. 응답 구조
     - 공통 헤더(resultCode/resultMsg 등)와 실제 데이터가 담기는 경로
       (response.body.items.item 형태로 추정되나 미확정)
     - item이 1개일 때 배열이 아니라 객체로 오는지 여부 (공공데이터포털 흔한 이슈)
     - 각 필드의 실제 키 이름 (contentid/title/addr1/mapx·mapy/firstimage 등으로
       추정되나, TourPlace/TourEvent로 매핑하기 전에 반드시 실물 응답으로 확인)
  5. 페이지네이션
     - page/rows 방식인지, 커서 방식인지
     - 한 번에 몇 건까지 오는지, 전체 건수(totalCount)를 별도로 주는지
  6. 에러/장애
     - HTTP 상태코드로 오는 에러와 바디 안 resultCode 에러가 둘 다 있는지
     - 트래픽 제한(개발계정 하루 1,000건) 초과 시 응답 형태

위 항목이 확정되기 전까지는 실제 호출 로직을 채우지 않는다. 지금은
시그니처와 의도(무엇을 입력받고 무엇을 반환해야 하는지)만 고정해둔다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import TourEvent, TourPlace


class TourAPIClient:
    """
    TourAPI 호출 어댑터.

    - 이 클래스가 반환하는 TourPlace/TourEvent의 이름·주소·좌표·이미지 필드는
      TourAPI 응답 값을 그대로 담는다. LLM을 거치지 않고 그대로 최종 결과로
      흘러간다 (설계 원칙: 사실 정보는 LLM이 다시 쓰지 않는다).
    - 재시도·속도 제한·캐싱 책임도 이 클래스가 진다. 노드는 실패 시 무엇을
      할지만 결정하고, "어떻게 재시도했는지"는 몰라도 된다.
    - 조회 결과 0건은 예외가 아니라 정상적인 반환값(빈 리스트)이다.
      실패로 볼지, 범위를 넓혀 재조회할지는 호출하는 노드가 판단한다.
    """

    def __init__(self, *, api_key: str, base_url: str) -> None:
        """
        Args:
            api_key: 공공데이터포털에서 발급받은 서비스키.
                TODO: 인코딩/디코딩 여부 확정 필요 (위 파일 docstring 2번 참고)
            base_url: TourAPI 베이스 URL.
                TODO: 실제 도메인·버전 경로 확정 필요

        TODO: api_key/base_url을 app/config.py(아직 없음, 신설 필요)에서
        .env 값을 읽어와 주입하는 방식을 가정. 내부 HTTP 클라이언트
        (httpx.AsyncClient 등) 보관, 타임아웃·재시도 정책 설정은 실제
        구현 시 채운다.
        """
        raise NotImplementedError

    async def search_places(
        self,
        *,
        area_code: str,
        sigungu_code: str | None = None,
        category_codes: list[str],
        page: int = 1,
        num_of_rows: int = 50,
    ) -> list["TourPlace"]:
        """
        지역·카테고리 조건으로 TourAPI에서 장소 후보를 조회한다.

        Args:
            area_code: TourAPI 지역코드.
                TODO: 지역명(시/도, 시/군/구) → TourAPI 지역코드 매핑 테이블이
                아직 없음. TourAPI의 지역코드 조회(areaCode) 응답을 한 번
                받아서 정적 테이블로 만들어둘 예정.
            sigungu_code: 시/군/구 코드. None이면 시/도 전체 범위로 조회.
                장소 추천 노드의 "지역 내 조회 0건 시 시/도로 범위 확장" 폴백에서
                이 값을 생략하고 재조회하는 용도로 씀.
            category_codes: TourAPI 카테고리 코드 목록.
                TODO: 취향 대분류/중분류 → TourAPI 카테고리 코드 매핑 테이블이
                아직 없음. 팀에서 매핑 규칙을 정한 뒤 채움 (임의로 만들지 않음).
            page: 페이지 번호. TODO: TourAPI 페이지네이션 파라미터명 확정 필요
            num_of_rows: 페이지당 건수. TODO: 적정값(일정 채우기 충분한 후보 수
                vs 응답 크기) 확정 필요

        Returns:
            TourPlace 목록. 결과 없으면 빈 리스트.

        Raises:
            TODO: TourAPI 장애/트래픽 한도 초과 시 던질 예외 종류 확정 필요
            (재시도 후에도 실패하면 무엇을 던지는지 — 노드의 폴백 로직과 연결됨)
        """
        raise NotImplementedError

    async def search_events(
        self,
        *,
        area_code: str,
        start_date: str,
        end_date: str,
    ) -> list["TourEvent"]:
        """
        지역·여행 기간 조건으로 TourAPI에서 행사/축제 정보를 조회한다.
        LLM을 거치지 않는다 (행사 추천 노드는 항상 LLM 미사용).

        Args:
            area_code: TourAPI 지역코드. search_places와 같은 매핑 테이블 사용 예정.
            start_date: 여행 시작일. 백엔드가 우리에게 주는 형식은 YY.MM.DD인데,
                TODO: TourAPI가 요구하는 날짜 파라미터 형식(YYYYMMDD 등)이
                다를 수 있어 이 함수 내부(또는 호출 전)에서 변환이 필요할지 확정 필요
            end_date: 여행 종료일. start_date와 동일한 TODO 적용

        Returns:
            TourEvent 목록. 결과 없으면 빈 리스트 (행사 없는 기간은 정상 상황).
        """
        raise NotImplementedError
