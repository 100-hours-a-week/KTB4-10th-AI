"""
app/errors.py

state.py의 예외들이 "상태 전이" 관련(이미 실행 중, 재시도 한도 등)이라면,
여기 정의된 건 "요청 자체의 비즈니스 규칙 위반"입니다.
둘 다 400/409/422로 매핑되지만 성격이 달라 파일을 분리했습니다.

- 필수 필드 누락/타입 오류: Pydantic이 자동으로 잡아냄 (main.py의
  RequestValidationError 핸들러가 400 missing_required_field로 변환)
- 기간 규칙, 취향 매핑 규칙: Pydantic만으로는 표현하기 까다로운
  '여러 필드를 같이 봐야 하는' 규칙이라 여기서 직접 검증
"""

from __future__ import annotations

from datetime import date, datetime


class InvalidDateRange(Exception):
    """400 invalid_date_range"""


class InvalidPreferenceMapping(Exception):
    """400 invalid_preference_mapping"""


def validate_date_range(start_date: str, end_date: str) -> None:
    """
    - 날짜 형식: YY.MM.DD (명세서 body 예시와 통일. 참고: 명세서 E열엔
      'YYYY-MM-DD'라고 적혀 있는데 실제 D열 예시는 '26.08.24'라 서로
      다릅니다 - 구현 중 발견한 불일치라, 명세서 쪽을 마저 정리해야 합니다)
    - 종료일이 시작일보다 빠르면 안 됨
    - 최대 7일 이내
    - 과거 날짜 불가 (오늘 = 실제 시스템 날짜 기준)
    """
    try:
        start = datetime.strptime(start_date, "%y.%m.%d").date()
        end = datetime.strptime(end_date, "%y.%m.%d").date()
    except ValueError:
        raise InvalidDateRange()

    if end < start:
        raise InvalidDateRange()
    if (end - start).days > 7:
        raise InvalidDateRange()
    if start < date.today():
        raise InvalidDateRange()


def validate_preference_mapping(preferences) -> None:
    """large_category의 모든 항목이 mid_category의 key로 존재하고,
    그 값(배열)이 비어있지 않은지 확인."""
    for category in preferences.large_category:
        sub_items = preferences.mid_category.get(category)
        if not sub_items:
            raise InvalidPreferenceMapping()
