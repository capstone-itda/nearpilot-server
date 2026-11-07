#pragma once
// MQTT `cmd` checks that do not depend on the board. The ledger lookup runs before these.
#include <ArduinoJson.h>
#include <string>

#include "np_descriptor.h"
#include "np_device.h"

namespace np {

// Messages other than `announce` and `cmd` (status, rssi, result, time_request, time, occupancy, restart).
// Other versions are rejected, never reinterpreted.
constexpr int SCHEMA_VERSION = 2;
// `announce` carries the declaration and `cmd` carries `arguments` (#245, device-protocol.md).
constexpr int DESCRIPTOR_SCHEMA_VERSION = 3;

struct CommandContext {
    CommandContext()
        : descriptor(nullptr), connected(false), clockTrusted(false), nowSec(0.0), maxErrorSec(0.0) {}
    std::string nodeId, session;
    const Descriptor* descriptor;  // The built-in declaration; null or unloaded accepts no action.
    bool connected;
    bool clockTrusted;
    double nowSec;       // UTC seconds from the synchronized wall clock.
    double maxErrorSec;  // Bound from the accepted MQTT time reply (#236).
};

// Returns nullptr when the command may run now, else the rejection code.
// Order: format, target connection, action, arguments, clock trust, validity window.
const char* validateCommand(JsonDocument& command, const CommandContext& context);

// MQTT `restart` (#235, architecture.md §5 MQTT 8절). nullptr when the node may restart now, else why not.
// Same node, current connection, trusted clock and validity window as a command, and a restart_id other
// than `lastRestartId`. `restartId` receives the id only on success.
const char* validateRestart(JsonDocument& restart, const CommandContext& context, const std::string& lastRestartId,
    std::string& restartId);

// Content fields exclude publication/session metadata; their original strings are preserved.
// `arguments` joins the content only when present, as [key, value] pairs sorted by key, so records
// without it compare as before and key order does not matter (spec decision 7).
std::string canonicalContent(JsonDocument& command);

// The output request for a validated command: `brightness`, `color` and `volume` arguments, each when given.
Actuation commandActuation(const std::string& commandId, JsonDocument& command);

}  // namespace np
