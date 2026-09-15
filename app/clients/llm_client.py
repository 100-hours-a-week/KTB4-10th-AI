"""
app/clients/llm_client.py

모든 LLM 호출이 거쳐가는 어댑터. 노드는 이 클래스만 알고, 실제 모델 제공자
(현재 v1: Claude Sonnet 5 / 향후 검토: 자체 서빙 vLLM)를 몰라야 한다.
모델을 바꿀 때 노드 코드는 건드리지 않고 이 파일 내부만 교체하는 것이 목적이다.

TODO(미정):
  - 벤더 교체를 이 클래스 내부 분기로 처리할지, 벤더별 구현 클래스를 나눌지
  - 프롬프트 캐싱을 어댑터에서 어떻게 다룰지 (프론티어 API와 자체 서빙이 방식이 다름)
  - 재시도 정책 (몇 회, 어떤 오류에서) — 실패 시 error.type(timeout/server_error 등)으로
    어떻게 올려보낼지와 연결됨
  - 호출별 토큰 수·응답 시간·TTFT 로깅 위치 (성능 측정 문서의 baseline 수집 항목)
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """LLM 호출 어댑터."""

    def __init__(self, *, api_key: str, model: str) -> None:
        """
        Args:
            api_key: 모델 제공자 API 키
            model: 사용할 모델 ID

        TODO: 두 값을 app/config.py(아직 없음, 신설 필요)에서 .env 값을 읽어와
        주입하는 방식을 가정. 자체 서빙 전환 시 api_key 대신 엔드포인트 URL이
        필요할 수 있어 인자 구성은 그때 다시 확정.
        """
        raise NotImplementedError

    async def generate_text(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
    ) -> str:
        """
        자유 텍스트 생성. 추천 이유 작성(장소별 2문장 이내)처럼 정해진 구조가
        없는 출력에 쓴다.

        Args:
            prompt: 완성된 프롬프트 문자열 (프롬프트 조립은 노드 책임)
            max_tokens: 최대 출력 토큰 수. None이면 어댑터 기본값 사용

        Returns:
            생성된 텍스트

        TODO: temperature 등 샘플링 파라미터를 노출할지 확정 필요
        """
        raise NotImplementedError

    async def generate_structured(
        self,
        prompt: str,
        *,
        response_model: type[T],
    ) -> T:
        """
        정해진 pydantic 스키마로만 응답하게 강제하는 생성.
        일정 작성(일자별 장소 배치), 메타정보(제목·테마키·표지장소ID)처럼
        출력 형식을 지정해야 하는 호출에 쓴다. 자유 텍스트로 받으면 파싱 코드를
        따로 만들어야 하고 형식이 매번 달라질 수 있기 때문.

        Args:
            prompt: 완성된 프롬프트 문자열
            response_model: 응답을 맞출 pydantic 모델 클래스

        Returns:
            response_model 인스턴스

        TODO: 구조를 강제하는 방식 확정 필요
              (벤더가 제공하는 구조화 출력 기능 사용 vs 프롬프트 + JSON 파싱·검증 실패 시 재시도)
        """
        raise NotImplementedError
