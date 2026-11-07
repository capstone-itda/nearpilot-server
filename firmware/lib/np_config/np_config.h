#pragma once
// Per-device settings injected over USB or BLE. Validation only: storage lives in the board glue.
// Connection and Wi-Fi only: the kind, occupancy and actions are in the built-in declaration (#245).
#include <ArduinoJson.h>
#include <cstddef>
#include <cstdint>
#include <string>

namespace np {

struct NodeConfig {
    std::string nodeId, mqttUser, mqttPassword, wifiSsid, wifiPassword, mqttHost;
    uint16_t mqttPort = 0;
    unsigned long generation = 0;
};

// No clock keys since #236: the clock comes from the server over MQTT.
// No capability or occupancy since #245: the device declares them itself.
constexpr size_t CONFIG_KEY_COUNT = 7;
extern const char* const CONFIG_KEYS[CONFIG_KEY_COUNT];

bool isConfigKey(const std::string& key);

// Returns nullptr when every value is present and valid, else the offending key.
// Other keys are ignored, so a stored configuration with pre-#236 clock keys or pre-#245 capability and
// occupancy still loads.
const char* decodeConfig(JsonDocument& doc, NodeConfig& out);

bool hexValid(const std::string& value);
// Serial values travel as hex; NUL bytes are rejected because settings are C strings.
bool fromHex(const std::string& hex, std::string& out);

}  // namespace np
