#include "np_command.h"

#include "np_clock.h"

namespace np {

const char* validateCommand(JsonDocument& doc, const CommandContext& context) {
    if (!doc["schema_version"].is<int>() || doc["schema_version"].as<int>() != 1 ||
        !doc["node_id"].is<const char*>() || doc["node_id"].as<std::string>() != context.nodeId ||
        !doc["action"].is<const char*>() || !doc["target_session_id"].is<const char*>() ||
        !doc["issued_at"].is<const char*>() || !doc["expires_at"].is<const char*>()) return "INVALID_MESSAGE";
    if (!context.connected || doc["target_session_id"].as<std::string>() != context.session)
        return "SESSION_MISMATCH";
    const std::string action = doc["action"].as<std::string>();
    const bool supported = context.capability == "locker" ? action == "open" :
        (context.capability == "light" || context.capability == "speaker") && (action == "on" || action == "off");
    if (!supported) return "UNSUPPORTED_ACTION";
    if (!context.clockTrusted) return "CLOCK_UNVERIFIED";
    double issued, expires;
    if (!parseUtc(doc["issued_at"].as<std::string>(), issued) ||
        !parseUtc(doc["expires_at"].as<std::string>(), expires) || issued >= expires) return "INVALID_MESSAGE";
    if (issued > context.nowSec + context.maxErrorSec) return "INVALID_MESSAGE";
    if (context.nowSec + context.maxErrorSec >= expires) return "COMMAND_EXPIRED";
    return nullptr;
}

std::string canonicalContent(JsonDocument& command) {
    JsonDocument canonical;
    JsonArray fields = canonical.to<JsonArray>();
    for (const char* key : {"node_id", "action", "target_session_id", "issued_at", "expires_at"})
        fields.add(command[key]);
    std::string content;
    serializeJson(canonical, content);
    return content;
}

}  // namespace np
