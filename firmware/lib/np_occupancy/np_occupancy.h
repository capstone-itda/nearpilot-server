#pragma once
// Retained MQTT v2 `occupancy` display (architecture.md §5 MQTT 3절).
// Display only: it never changes command acceptance, actuation or reports.
#include <ArduinoJson.h>
#include <cstdint>
#include <cstring>
#include <string>

#include "np_command.h"

namespace np {

enum class OccupancyDisplay { Invalid, Free, Occupied };

// Invalid for any message that is not this node's v2 display; the caller keeps its last color.
inline OccupancyDisplay decodeOccupancy(const char* payload, const std::string& nodeId) {
    JsonDocument doc;
    if (deserializeJson(doc, payload) || !doc.is<JsonObject>()) return OccupancyDisplay::Invalid;
    if (!doc["schema_version"].is<int>() || doc["schema_version"].as<int>() != SCHEMA_VERSION)
        return OccupancyDisplay::Invalid;
    if (!doc["node_id"].is<const char*>() || nodeId != doc["node_id"].as<const char*>())
        return OccupancyDisplay::Invalid;
    if (!doc["device_state"].is<const char*>()) return OccupancyDisplay::Invalid;
    const char* state = doc["device_state"].as<const char*>();
    if (std::strcmp(state, "AVAILABLE") == 0) return OccupancyDisplay::Free;
    for (const char* held : {"RESERVED", "ACTIVE", "CLOSING", "FAULT"})
        if (std::strcmp(state, held) == 0) return OccupancyDisplay::Occupied;
    return OccupancyDisplay::Invalid;
}

// 0xRRGGBB at the given level: free is blue, occupied is red, invalid is off.
inline uint32_t occupancyRgb(OccupancyDisplay display, uint8_t level) {
    if (display == OccupancyDisplay::Free) return level;
    if (display == OccupancyDisplay::Occupied) return uint32_t(level) << 16;
    return 0;
}

inline const char* occupancyName(OccupancyDisplay display) {
    return display == OccupancyDisplay::Free ? "free"
        : display == OccupancyDisplay::Occupied ? "occupied" : "off";
}

}  // namespace np
