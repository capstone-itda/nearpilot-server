#pragma once
// Per-device settings injected over USB. Validation only: storage lives in the board glue.
#include <ArduinoJson.h>
#include <cstddef>
#include <cstdint>
#include <string>

namespace np {

struct NodeConfig {
    std::string capability, occupancy;
    std::string nodeId, mqttUser, mqttPassword, wifiSsid, wifiPassword, mqttHost;
    uint16_t mqttPort = 0;
    unsigned long generation = 0;
};

// No clock keys since #236: the clock comes from the server over MQTT.
constexpr size_t CONFIG_KEY_COUNT = 9;
extern const char* const CONFIG_KEYS[CONFIG_KEY_COUNT];

bool isConfigKey(const std::string& key);

// Returns nullptr when every value is present and valid, else the offending key.
// Other keys are ignored, so a stored pre-#236 configuration with clock keys still loads.
const char* decodeConfig(JsonDocument& doc, NodeConfig& out);

// Adds configured discovery fields to the MQTT announcement.
void announceConfig(JsonDocument& doc, const NodeConfig& config);

bool hexValid(const std::string& value);
// Serial values travel as hex; NUL bytes are rejected because settings are C strings.
bool fromHex(const std::string& hex, std::string& out);

}  // namespace np
