# KTB4-10th-AI

## 개발 환경

uv로 의존성을 관리합니다 (`pyproject.toml` + `uv.lock`). Python 3.14 이상.

```bash
uv sync --frozen   # uv.lock에 박힌 버전 그대로 설치
```

## 실행

```bash
.venv/bin/python -m uvicorn app.main:app --reload   # http://127.0.0.1:8000
curl http://127.0.0.1:8000/health                   # {"message":"health_ok","data":null}
```
