FROM python:3.14-slim

WORKDIR /app

# uv 설치
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 의존성 파일 복사
COPY pyproject.toml uv.lock ./

# lock 기준 의존성 설치
RUN uv sync --frozen --no-dev

# 애플리케이션 코드 복사
COPY . .

EXPOSE 8000

# 요청마다 찍히는 uvicorn 평문 줄은 끈다. 로그는 JSON 한 줄로 모으고(CloudWatch),
# 요청 수·응답 코드는 /metrics 로 본다
CMD ["/app/.venv/bin/uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
