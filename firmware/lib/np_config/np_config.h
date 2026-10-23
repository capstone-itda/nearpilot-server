#pragma once
// Per-device settings injected over USB. Validation only: storage lives in the board glue.
#include <ArduinoJson.h>
#include <cstddef>
#include <cstdint>
#include <string>

namespace np {

struct NodeConfig {
    std::string capability, occupancy;
    std::string nodeId, mqttUser, mqttPassword, wifiSsid, wifiPassword, mqttHost, timeServer;
    uint16_t mqttPort = 0;
    double clockMaxErrorSec = -1.0;  // -1: not measured, never trusted.
    unsigned long timeTrustMs = 0, timeResyncMs = 0, generation = 0;
};

constexpr size_t CONFIG_KEY_COUNT = 13;
extern const char* const CONFIG_KEYS[CONFIG_KEY_COUNT];

bool isConfigKey(const std::string& key);

// Returns nullptr when every value is present and valid, else the offending key.
const char* decodeConfig(JsonDocument& doc, NodeConfig& out);

// Adds configured discovery fields to the MQTT announcement.
void announceConfig(JsonDocument& doc, const NodeConfig& config);

bool hexValid(const std::string& value);
// Serial values travel as hex; NUL bytes are rejected because settings are C strings.
bool fromHex(const std::string& hex, std::string& out);

}  // namespace np
