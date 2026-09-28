"""
src/paths.py

저장소 뿌리가 어디인가. **이 계산은 여기서만 한다.**

폴더 깊이에 기대는 계산이라 파일이 옮겨지면 조용히 틀린다 — 2026-09-27 에 src/ 밑으로
폴더를 만들면서 engine/data.py · llm/calllog.py · llm/llm.py 세 곳이 동시에 깨졌다.
REPO_ROOT 가 src/ 를 가리켜 지역코드·덤프·.env 를 하나도 못 찾았고, 겉으로는
"파일이 없습니다"로만 보여서 원인을 찾는 데 시간이 걸렸다.

**이 파일은 src/ 바로 아래 있어야 한다.** 하위 폴더로 옮기면 parents[1] 이 틀린다.

뿌리에서 뻗어 나가는 경로(data/ · samples/ · _out/)는 여기 두지 않는다. 그건 쓰는
쪽에서 REPO_ROOT 에 붙이면 되고, 파일이 움직여도 안 깨진다 — 깨지는 것은 깊이 계산뿐이다.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 뿌리에는 pyproject.toml 이 있다. 없으면 이 파일이 옮겨졌다는 뜻이다 —
# 경로가 틀린 채로 계속 도는 것보다 여기서 멈추고 이유를 말하는 편이 낫다.
if not (REPO_ROOT / "pyproject.toml").exists():
    raise RuntimeError(
        f"저장소 뿌리를 잘못 잡았습니다: {REPO_ROOT}\n"
        "src/paths.py 가 src/ 바로 아래에 있는지 확인하세요."
    )
