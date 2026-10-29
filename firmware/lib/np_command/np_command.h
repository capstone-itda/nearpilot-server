#pragma once
// MQTT v2 `cmd` checks that do not depend on the board. The ledger lookup runs before these.
#include <ArduinoJson.h>
#include <string>

#include "np_device.h"

namespace np {

// Every message, in both directions. Other versions are rejected, never reinterpreted.
constexpr int SCHEMA_VERSION = 2;

struct CommandContext {
    std::string nodeId, session, capability;
    bool connected = false;
    bool clockTrusted = false;
    double nowSec = 0.0;       // UTC seconds from the synchronized wall clock.
    double maxErrorSec = 0.0;  // Measured per-device bound from provisioning.
    int lightMaxLevel = 0;     // Firmware output limit for `parameters.brightness`; 0 accepts no parameters.
    bool lightColor = false;   // Whether `parameters.color` may be other than white.
    int speakerMaxVolume = 0;  // Firmware limit for `parameters.volume`; 0 accepts no speaker parameters.
};

// Returns nullptr when the command may run now, else the rejection code.
// Order: format, target connection, action, parameters, clock trust, validity window.
const char* validateCommand(JsonDocument& command, const CommandContext& context);

// Content fields exclude publication/session metadata; their original strings are preserved.
// `parameters` joins the content only when present, so records without it compare as before.
std::string canonicalContent(JsonDocument& command);

// The output request for a validated command; `parameters` becomes a light or speaker actuation.
Actuation commandActuation(const std::string& commandId, JsonDocument& command);

}  // namespace np
