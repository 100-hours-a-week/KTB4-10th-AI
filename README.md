# KTB4-10th-AI

여행 가이드북 생성 AI 서버. 백엔드가 여행 조건과 후보 장소·행사를 보내면 Claude 로 일정을 짜고,
일정 JSON 과 가이드북 HTML 을 돌려줍니다. 계약서는 백엔드 팀과 공유한 `backend-AI-API.md` 입니다.

## 개발 환경

uv로 의존성을 관리합니다 (`pyproject.toml` + `uv.lock`). Python 3.14 이상.

```bash
uv sync --frozen   # uv.lock에 박힌 버전 그대로 설치
```

## 설정

Anthropic API 키가 필요합니다. **환경변수 `ANTHROPIC_API_KEY`** 가 있으면 그것을, 없으면 저장소 뿌리의 **`.env`** 를 읽습니다
(`.env.example` 참고). 둘 다 없으면 서버가 켜지지 않습니다.

- 모델은 `LLM_MODEL` 로 바꿉니다 (기본 `claude-sonnet-5`).
- `SAVE_LLM_CALLS=0` 이면 호출 기록 파일(`_out/llm_calls/`)을 남기지 않습니다. 배포 서버용이고, 로컬은 비워 둡니다.
- **서버는 항상 실제 모델을 부릅니다** — 생성 요청 1건마다 비용이 듭니다.

## 실행

```bash
uv run uvicorn src.main:app --reload     # http://127.0.0.1:8000
curl http://127.0.0.1:8000/health        # {"status":"ok","jobs":0,"queue_size":0,"running":0}
```

| 엔드포인트 | 하는 일 |
|---|---|
| `POST /guidebooks-generations` | 생성 접수 (202). 일정은 워커가 뒤에서 만든다 |
| `GET /guidebooks-generations/{job_id}` | 상태·결과 조회 |
| `GET /health` | 큐 길이·실행 중 작업 수 |

## 구조

```
src/
├── main.py      엔드포인트
├── server/      요청 검증·응답 변환(adapter) · 큐·워커(jobs)
├── engine/      일정 생성 그래프 (LangGraph, 노드 5개)
├── llm/         Claude 호출 · 호출 기록
└── output/      가이드북 HTML
data/            지역·분류·취향 코드표
```

## 배포

`main` 에 push 하면 GitHub Actions(`ai-cd.yml`)가 도커 이미지를 만들어 EC2 에 띄웁니다.
EC2 의 `.env` 는 `docker run --env-file` 로 **환경변수가 되어** 컨테이너에 들어갑니다.
서버용 `.env` 는 `.env.cloud.example` 을 복사해 채웁니다 (형식 주의사항이 그 안에 있습니다).
