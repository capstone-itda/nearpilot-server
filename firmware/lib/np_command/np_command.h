#pragma once
// MQTT v2 `cmd` checks that do not depend on the board. The ledger lookup runs before these.
#include <ArduinoJson.h>
#include <string>

namespace np {

// Every message, in both directions. Other versions are rejected, never reinterpreted.
constexpr int SCHEMA_VERSION = 2;

struct CommandContext {
    std::string nodeId, session, capability;
    bool connected = false;
    bool clockTrusted = false;
    double nowSec = 0.0;       // UTC seconds from the synchronized wall clock.
    double maxErrorSec = 0.0;  // Measured per-device bound from provisioning.
};

// Returns nullptr when the command may run now, else the rejection code.
// Order: format, target connection, action, clock trust, validity window.
const char* validateCommand(JsonDocument& command, const CommandContext& context);

// Content fields exclude publication/session metadata; their original strings are preserved.
std::string canonicalContent(JsonDocument& command);

}  // namespace np
