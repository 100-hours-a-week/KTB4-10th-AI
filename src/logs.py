"""
src/logs.py

서버 로그 한 줄을 **JSON 한 줄**로 찍는다.

JSON 인 이유: 인프라가 CloudWatch Logs 로 모은다 (2026-09-29 클라우드팀 합의).
평문이면 "어느 칸이 무엇인가"를 사람이 읽어야 하지만, JSON 이면 칸 이름으로 걸러 볼 수 있다.

**여러 줄로 찍지 않는다.** CloudWatch 는 줄마다 따로 받아서, 오류 경로(traceback)를
그대로 찍으면 한 오류가 수십 조각으로 흩어진다. 여러 줄짜리 값은 칸 하나에 문자열로 넣는다.

출력은 stderr — 도커가 stdout·stderr 를 모두 모으고, 전부터 로그는 stderr 에 찍어 왔다.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def write(level: str, event: str, **fields: object) -> None:
    """
    level 은 INFO · WARNING · ERROR. event 는 무슨 일인지 짧은 영문 이름 (예: llm_call).

    나머지 칸은 부르는 쪽이 정한다. JSON 으로 못 바꾸는 값(날짜 등)은 문자열로 적는다.
    """
    line = {
        "time": datetime.now(KST).isoformat(timespec="milliseconds"),
        "level": level,
        "event": event,
    }
    line.update(fields)
    text = json.dumps(line, ensure_ascii=False, default=str)
    print(text, file=sys.stderr, flush=True)
