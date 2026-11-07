#pragma once
// Device declarations built into the team's firmware (#245, spec decision 1). Each build profile picks one
// with -D NP_DESCRIPTOR=<name> (platformio.ini). kind, occupancy and actions equal the server's v2 template
// for what the same build announced before, so the declaration hash is unchanged and the owner need not
// trust the device again; only vendor.firmware differs. server/tests/tools/test_firmware_descriptors.py
// parses this file and checks both, so keep every declaration a raw string literal with a literal number.

namespace np {

// Locker with a servo, or serial output only: open, and close as a re-operation of one's own rental.
static const char LOCKER_DESCRIPTOR[] = R"json({
  "format": "nearpilot.device/1",
  "kind": "locker",
  "occupancy": "rental",
  "vendor": {"name": "nearpilot", "model": "locker", "firmware": "v3"},
  "actions": [
    {"name": "open", "description": "사물함을 연다",
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": false},
     "result": {"kind": "signal_output"}},
    {"name": "close", "description": "사물함을 잠근다. 반납이 아니다", "role": "reoperate",
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": false},
     "result": {"kind": "signal_output"}}
  ]
})json";

// Light without a dimmable output (serial or plain GPIO only): on and off without arguments.
static const char LIGHT_DESCRIPTOR[] = R"json({
  "format": "nearpilot.device/1",
  "kind": "light",
  "occupancy": "shared",
  "vendor": {"name": "nearpilot", "model": "light", "firmware": "v3"},
  "actions": [
    {"name": "on", "description": "조명을 켠다",
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": false},
     "result": {"kind": "signal_output"}},
    {"name": "off", "description": "조명을 끈다", "role": "deactivate",
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": false},
     "result": {"kind": "signal_output"}}
  ]
})json";

// WS2812B ring: brightness up to NP_LIGHT_RING_MAX_LEVEL (64) and any color. The boot check refuses to
// announce when this maximum exceeds the build's output limit.
static const char LIGHT_RING_DESCRIPTOR[] = R"json({
  "format": "nearpilot.device/1",
  "kind": "light",
  "occupancy": "shared",
  "vendor": {"name": "nearpilot", "model": "light", "firmware": "v3"},
  "actions": [
    {"name": "on", "description": "조명을 켜고 밝기와 색을 정한다",
     "input_schema": {"type": "object",
                      "properties": {"brightness": {"type": "integer", "minimum": 0, "maximum": 64},
                                     "color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"}},
                      "required": [], "additionalProperties": false},
     "result": {"kind": "signal_output"}},
    {"name": "off", "description": "조명을 끈다", "role": "deactivate",
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": false},
     "result": {"kind": "signal_output"}}
  ]
})json";

// DFPlayer Mini speaker: volume up to NP_SPEAKER_MAX_VOLUME (20).
static const char SPEAKER_DFPLAYER_DESCRIPTOR[] = R"json({
  "format": "nearpilot.device/1",
  "kind": "speaker",
  "occupancy": "shared",
  "vendor": {"name": "nearpilot", "model": "speaker", "firmware": "v3"},
  "actions": [
    {"name": "on", "description": "스피커를 재생하고 음량을 정한다",
     "input_schema": {"type": "object",
                      "properties": {"volume": {"type": "integer", "minimum": 0, "maximum": 20}},
                      "required": [], "additionalProperties": false},
     "result": {"kind": "signal_output"}},
    {"name": "off", "description": "스피커를 정지한다", "role": "deactivate",
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": false},
     "result": {"kind": "signal_output"}}
  ]
})json";

}  // namespace np
