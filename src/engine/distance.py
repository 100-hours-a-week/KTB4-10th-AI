"""
src/engine/distance.py

두 방문지 사이 직선 거리. build 가 그날 구간 거리를 채울 때 쓴다.

2026-09-28 까지 이름이 same_spot.py 였다 — "같은 자리"로 후보를 합치던 코드가 있던 곳이다.
그 기능이 빠지고 거리 계산만 남아 이름을 바꿨다. "같은 자리"로 볼 기준(SAME_SPOT_KM)은
화면만 쓰므로 tools/guidebook_html.py 에 있다.
"""

from __future__ import annotations

import math

from src.models import PlannedPlace

EARTH_RADIUS_KM = 6371.0


def distance_km(a: PlannedPlace, b: PlannedPlace) -> float | None:
    """
    두 지점 사이 **직선** 거리(km). 어느 한쪽이라도 좌표가 없으면 None.

    실제 도보·차량 경로가 아니다 — TourAPI에 경로 데이터가 없다. 그래서 출력에도
    "직선"이라고 적는다. 분 단위로 바꿔 적으면 틀린 정보가 된다.
    """
    if a.coordinates is None or b.coordinates is None:
        return None

    lat_a, lng_a = a.coordinates.lat, a.coordinates.lng
    lat_b, lng_b = b.coordinates.lat, b.coordinates.lng

    # 하버사인 공식 — 지구를 구로 보고 두 점 사이 대원 거리를 구한다
    p1, p2 = math.radians(lat_a), math.radians(lat_b)
    d_lat = math.radians(lat_b - lat_a)
    d_lng = math.radians(lng_b - lng_a)
    h = (
        math.sin(d_lat / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(d_lng / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))
