"""참조 펌웨어에 내장한 기기 선언과 서버 템플릿의 정합성 (FR-04, specs/iot-firmware-descriptor/spec.md 결정 1·2).

펌웨어의 선언 해시가 같은 기기의 v2 템플릿과 같으면, 기기를 v3 펌웨어로 올려도 소유자가 다시 신뢰하지 않아도 된다.
펌웨어 빌드 없이 헤더와 빌드 설정 파일을 읽어서 확인한다.
"""

from __future__ import annotations

import configparser
import json
import re
from pathlib import Path

import pytest

from nearpilot.shared.descriptor import ANNOUNCE_MAX_BYTES, legacy_descriptor, parse_descriptor
from nearpilot.shared.enums import Occupancy

FIRMWARE = Path(__file__).resolve().parents[3] / "firmware"
HEADER = FIRMWARE / "lib" / "np_descriptor" / "np_descriptors.h"
DECLARATION = re.compile(r'static const char (\w+)\[\] = R"json\((.*?)\)json";', re.DOTALL)

# 상수 이름 → 같은 기기가 v2로 발표하던 제어 정보의 템플릿
TEMPLATES = {
    "LOCKER_DESCRIPTOR": legacy_descriptor("locker", Occupancy.RENTAL),
    "LIGHT_DESCRIPTOR": legacy_descriptor("light", Occupancy.SHARED),
    "LIGHT_RING_DESCRIPTOR": legacy_descriptor("light", Occupancy.SHARED, brightness_max=64, color=True),
    "SPEAKER_DFPLAYER_DESCRIPTOR": legacy_descriptor("speaker", Occupancy.SHARED, volume_max=20),
}
# 빌드 환경 → 고른 선언. 기본 esp32dev는 main.cpp의 기본값(인자 없는 조명)이다
PROFILES = {
    "esp32dev": "LIGHT_DESCRIPTOR",
    "esp32dev-light-1": "LIGHT_RING_DESCRIPTOR",
    "esp32dev-locker-1": "LOCKER_DESCRIPTOR",
    "esp32dev-speaker-1": "SPEAKER_DFPLAYER_DESCRIPTOR",
}
# 펌웨어 PC 테스트(test_descriptor.cpp의 test_NFR10_pattern_subset)가 지원을 확인한 패턴
FIRMWARE_PATTERNS = {"^#[0-9A-Fa-f]{6}$"}


def declarations() -> dict[str, dict]:
    text = HEADER.read_text(encoding="utf-8")
    return {name: json.loads(body) for name, body in DECLARATION.findall(text)}


def test_FR04_firmware_declarations_match_templates():
    found = declarations()
    assert set(found) == set(TEMPLATES)
    for name, raw in found.items():
        declared = parse_descriptor(raw)  # 서버가 받는 선언이다
        template = TEMPLATES[name]
        assert declared.hash == template.hash, name  # 다시 신뢰하지 않아도 된다
        assert declared.to_json() | {"vendor": None} == template.to_json() | {"vendor": None}, name
        assert raw["vendor"] == {"name": "nearpilot", "model": declared.kind, "firmware": "v3"}, name


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_FR04_firmware_announce_within_limit(name):
    """가장 긴 기기 ID(64바이트)와 연결 ID·순번·시각을 넣어도 발표는 2048바이트 이하다."""
    body = {"schema_version": 3, "node_id": "n" * 64, "session_id": "0" * 36, "seq": 4294967295,
            "sent_at": "2026-01-01T00:00:00.000000Z", "descriptor": declarations()[name]}
    size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert size <= ANNOUNCE_MAX_BYTES


def test_NFR10_firmware_patterns_in_supported_subset():
    patterns = {
        prop["pattern"]
        for raw in declarations().values() for action in raw["actions"]
        for prop in action["input_schema"].get("properties", {}).values() if "pattern" in prop
    }
    assert patterns <= FIRMWARE_PATTERNS


def test_FR04_build_profiles_pick_one_declaration():
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(FIRMWARE / "platformio.ini", encoding="utf-8")
    for profile, expected in PROFILES.items():
        flags = ini[f"env:{profile}"].get("build_flags", "")
        chosen = re.findall(r"-D NP_DESCRIPTOR=np::(\w+)", flags)
        if profile == "esp32dev":
            assert chosen == []
            main = (FIRMWARE / "src" / "main.cpp").read_text(encoding="utf-8")
            assert f"#define NP_DESCRIPTOR np::{expected}" in main
        else:
            assert chosen == [expected], profile


def test_NFR10_declared_limits_within_output_limits():
    """조명·스피커 선언의 상한은 펌웨어 출력 상한과 같다. 넘으면 부팅 검사가 발표를 막는다 (결정 3)."""
    config = (FIRMWARE / "include" / "config.example.h").read_text(encoding="utf-8")

    def define(name: str) -> int:
        return int(re.search(rf"#define {name} (\d+)", config).group(1))

    ring = parse_descriptor(declarations()["LIGHT_RING_DESCRIPTOR"]).action("on").properties
    speaker = parse_descriptor(declarations()["SPEAKER_DFPLAYER_DESCRIPTOR"]).action("on").properties
    assert ring["brightness"]["maximum"] == define("NP_LIGHT_RING_MAX_LEVEL")
    assert speaker["volume"]["maximum"] == define("NP_SPEAKER_MAX_VOLUME")
