"""
src/engine/data.py

v0가 읽는 바깥 데이터는 전부 여기를 거친다 — 지역코드표, 분류코드표, 취향 코드표.
후보(장소·행사)는 백엔드가 보내 준다. 테스트용 덤프 읽기는 tools/dump_candidates.py 에 있다.

TourAPI를 호출하지 않는다. 전부 로컬 파일 읽기다.
"""

from __future__ import annotations

import csv
import json
from functools import lru_cache
from pathlib import Path

from src.paths import REPO_ROOT

REGION_CODES_PATH = REPO_ROOT / "data" / "region_codes.json"
CATEGORY_CODES_PATH = REPO_ROOT / "data" / "category_codes.json"
PREFERENCE_ENUM_PATH = REPO_ROOT / "data" / "user_category_enum.csv"

# 일정에 넣을 장소의 대분류.
# EV(행사)를 빼는 게 중요하다 — 안 빼면 같은 항목이 장소로 한 번, 행사로 한 번 나온다.
PLACE_THEMES = frozenset({"NA", "EX", "HS", "LS", "VE"})

# user_category_enum.csv 의 DETAIL 코드 -> TourAPI lclsSystm2. 두 체계가 1:1로 붙는다.
# VE만 구멍이 있다: VE05·VE06·VE08~VE12가 어느 DETAIL에도 안 붙어
# 취향 일치로는 안 잡히고 채우기 경로로만 들어온다. VE03 은 아래 EXTRA_LCLS2 로 붙였다.
DETAIL_TO_LCLS2 = {
    "NATURE_MOUNTAIN": "NA01",
    "NATURE_RIVER_SEA": "NA02",
    "NATURE_ECOLOGY": "NA03",
    "NATURE_PARK": "NA04",
    "NATURE_ETC": "NA05",
    "HISTORY_HERITAGE_SITE": "HS01",
    "HISTORY_RELIC": "HS02",
    "HISTORY_RELIGIOUS_SITE": "HS03",
    "HISTORY_SECURITY_SITE": "HS04",
    "ATTRACTION_LANDMARK": "VE01",
    "ATTRACTION_THEME_PARK": "VE02",
    "ATTRACTION_URBAN_CULTURE": "VE04",
    "ATTRACTION_EXHIBITION": "VE07",
    "EXPERIENCE_TRADITION": "EX01",
    "EXPERIENCE_CRAFT": "EX02",
    "EXPERIENCE_RURAL": "EX03",
    "EXPERIENCE_TEMPLE_STAY": "EX04",
    "EXPERIENCE_HEALING": "EX05",
    "EXPERIENCE_INDUSTRY": "EX06",
    "EXPERIENCE_ETC": "EX07",
    "LEISURE_SPORTS_LAND": "LS01",
    "LEISURE_SPORTS_WATER": "LS02",
    "LEISURE_SPORTS_AIR": "LS03",
    "LEISURE_SPORTS_COMPLEX": "LS04",
    "EVENTS_FESTIVAL": "EV01",
    "EVENTS_CONCERT": "EV02",
    "EVENTS_FAIR": "EV03",
}

# 취향 하나가 TourAPI 중분류 둘에 걸치는 경우. DETAIL_TO_LCLS2 에 더해 관심사로만 잡는다.
# 넓히기(같은 대분류) 기준에는 넣지 않는다 — 넣으면 공원이 모자란 지역에서
# VE 전체(교육시설·청소년회관…)가 "비슷한 것"으로 끼어든다.
EXTRA_LCLS2 = {
    "NATURE_PARK": ["VE03"],  # 도시공원. 자연공원(NA04)만으로는 도시 지역 공원을 못 잡는다
}


class DataError(Exception):
    """데이터 파일이 없거나 찾는 값이 표에 없을 때. 메시지를 그대로 사용자에게 보여준다."""


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise DataError(f"{path} 가 없습니다.")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _provinces() -> dict:
    return _read_json(REGION_CODES_PATH)["provinces"]


def find_region_codes(province: str, city: str | None) -> tuple[str, str | None]:
    """
    지역명을 법정동코드(lDongRegnCd, lDongSignguCd)로 바꾼다.

    이름은 TourAPI 표기 그대로여야 한다 ('전라남도'가 아니라 '전남광주통합특별시').
    부분일치로 찾지 않는다 — '중구'는 5개 시/도에 있어서 엉뚱한 걸 집게 된다.
    city가 비면 시/도 전체다.
    """
    provinces = _provinces()

    entry = provinces.get(province)
    if entry is None:
        raise DataError(
            f"'{province}' 를 찾지 못했습니다. 후보: " + ", ".join(sorted(provinces))
        )

    if not city:
        return entry["code"], None

    sigungu_code = entry["cities"].get(city)
    if sigungu_code is None:
        raise DataError(
            f"'{province}' 에서 '{city}' 를 찾지 못했습니다. 후보: "
            + ", ".join(sorted(entry["cities"]))
        )

    return entry["code"], sigungu_code


@lru_cache(maxsize=1)
def _lcls_names() -> dict[str, str]:
    """lclsSystm 코드 -> 한글 이름. category_codes.json을 뒤집어 만든다."""
    names: dict[str, str] = {}
    for theme_name, theme in _read_json(CATEGORY_CODES_PATH)["categories"].items():
        names[theme["code"]] = theme_name
        for mid_name, mid in theme["mid"].items():
            names[mid["code"]] = mid_name
            for small_name, small_code in mid.get("small", {}).items():
                names[small_code] = small_name
    return names


def category_name(code: str) -> str:
    """코드의 한글 이름. 표에 없으면 코드를 그대로 돌려준다."""
    return _lcls_names().get(code, code)


def category_path(code2: str, code3: str = "") -> str:
    """
    '역사관광 › 역사유적지 › 고궁' 처럼 대→중→소 이름을 잇는다.

    대분류 코드는 중분류 코드의 앞 두 글자다 (HS01 → HS). 표를 따로 뒤지지 않는다.
    모르는 코드는 건너뛴다 — 이름이 없다고 줄 전체를 버리면 화면이 비어버린다.
    """
    parts = []
    for code in (code2[:2], code2, code3):
        if not code:
            continue
        name = _lcls_names().get(code)
        if name:
            parts.append(name)
    return " › ".join(parts)


# 동행 코드 -> "누구와 떠나는" 자리에 들어갈 말. 가이드북 표지와 모델 조건이 같이 쓴다
COMPANION_LABELS = {
    "alone": "혼자",
    "friend": "친구와",
    "couple": "연인과",
    "family": "가족과",
    "group": "단체로",
}


def companion_label(code: str) -> str:
    """'couple' -> '연인과'. 코드는 TripRequest 가 이미 다섯 가지로 막는다."""
    return COMPANION_LABELS[code]


@lru_cache(maxsize=1)
def _preference_labels() -> dict[str, str]:
    """취향 코드 -> 한글 라벨. user_category_enum.csv 를 그대로 읽는다."""
    labels: dict[str, str] = {}
    with PREFERENCE_ENUM_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            labels[row["code"]] = row["label"]
    return labels


def preference_label(code: str) -> str:
    """
    'HISTORY_HERITAGE_SITE' -> '역사 유적지'. 표에 없으면 코드를 그대로 돌려준다.

    category_name 과 같은 정책이다 — 이름을 못 찾았다고 빈 문자열을 내면
    화면에서 항목이 통째로 사라져, 값이 없는 것과 구별이 안 된다.
    """
    if not PREFERENCE_ENUM_PATH.exists():
        raise DataError(f"{PREFERENCE_ENUM_PATH} 가 없습니다.")
    return _preference_labels().get(code, code)


@lru_cache(maxsize=1)
def _preference_codes() -> dict[tuple[str, str], str]:
    """(종류, 한글 라벨) -> 취향 코드. preference_label 의 반대 방향이다."""
    codes: dict[tuple[str, str], str] = {}
    with PREFERENCE_ENUM_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            codes[(row["preference_type"], row["label"])] = row["code"]
    return codes


def preference_code(label: str, preference_type: str) -> str | None:
    """
    '공원', 'DETAIL' -> 'NATURE_PARK'. 표에 없으면 **None**.

    종류를 함께 받는 이유: 라벨만으로 찾으면 대분류 이름이 중분류 자리에 들어와도
    통과한다 ('자연'은 THEME 라벨이다). 백엔드는 자리를 나눠 보내므로 그대로 본다.

    못 찾았을 때 코드를 되돌려주지 않는다 (preference_label 과 다르다) — 이 값은
    화면에 쓰는 것이 아니라 취향 티어의 기준이라, 틀린 채로 흘러가면 조용히
    엉뚱한 일정이 나온다. 부르는 쪽이 거절해야 한다.
    """
    if not PREFERENCE_ENUM_PATH.exists():
        raise DataError(f"{PREFERENCE_ENUM_PATH} 가 없습니다.")
    return _preference_codes().get((preference_type, label))
