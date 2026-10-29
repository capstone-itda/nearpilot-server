#include "np_command.h"

#include "np_clock.h"

namespace np {

namespace {

// `#RRGGBB` in upper case, the form the server always sends (FR-09).
bool parseColor(JsonVariantConst value, uint32_t& rgb) {
    if (!value.is<const char*>()) return false;
    const std::string text = value.as<std::string>();
    if (text.size() != 7 || text[0] != '#') return false;
    rgb = 0;
    for (size_t i = 1; i < text.size(); ++i) {
        const char ch = text[i];
        const int digit = ch >= '0' && ch <= '9' ? ch - '0' : ch >= 'A' && ch <= 'F' ? ch - 'A' + 10 : -1;
        if (digit < 0) return false;
        rgb = rgb << 4 | uint32_t(digit);
    }
    return true;
}

// Speaker parameters are exactly `{"volume": n}` with an integer n up to the firmware's limit.
const char* validateSpeakerParameters(JsonObjectConst object, const std::string& action,
                                      const CommandContext& context) {
    if (object.isNull() || object.size() != 1 || action != "on" || context.speakerMaxVolume <= 0 ||
        !object["volume"].is<int>() || object["volume"].is<bool>()) return "UNSUPPORTED_PARAMETER";
    const int volume = object["volume"].as<int>();
    if (volume < 0 || volume > context.speakerMaxVolume) return "UNSUPPORTED_PARAMETER";
    return nullptr;
}

// Light and speaker parameters are checked against this firmware's own limits, never the server's (NFR-10).
const char* validateParameters(JsonDocument& doc, const std::string& action, const CommandContext& context) {
    JsonVariantConst parameters = doc["parameters"];
    if (parameters.isNull()) return nullptr;
    JsonObjectConst object = parameters.as<JsonObjectConst>();
    if (context.capability == "speaker") return validateSpeakerParameters(object, action, context);
    uint32_t rgb = 0;
    if (object.isNull() || object.size() != 2 || action != "on" || context.capability != "light" ||
        context.lightMaxLevel <= 0 || !object["brightness"].is<int>() || object["brightness"].is<bool>() ||
        !parseColor(object["color"], rgb)) return "UNSUPPORTED_PARAMETER";
    const int brightness = object["brightness"].as<int>();
    if (brightness < 0 || brightness > context.lightMaxLevel) return "UNSUPPORTED_PARAMETER";
    if (!context.lightColor && rgb != 0xFFFFFF) return "UNSUPPORTED_PARAMETER";
    return nullptr;
}

}  // namespace

const char* validateCommand(JsonDocument& doc, const CommandContext& context) {
    if (!doc["schema_version"].is<int>() || doc["schema_version"].as<int>() != SCHEMA_VERSION ||
        !doc["node_id"].is<const char*>() || doc["node_id"].as<std::string>() != context.nodeId ||
        !doc["action"].is<const char*>() || !doc["target_session_id"].is<const char*>() ||
        !doc["issued_at"].is<const char*>() || !doc["expires_at"].is<const char*>()) return "INVALID_MESSAGE";
    if (!context.connected || doc["target_session_id"].as<std::string>() != context.session)
        return "SESSION_MISMATCH";
    const std::string action = doc["action"].as<std::string>();
    const bool supported = context.capability == "locker" ? action == "open" || action == "close" :
        (context.capability == "light" || context.capability == "speaker") && (action == "on" || action == "off");
    if (!supported) return "UNSUPPORTED_ACTION";
    if (const char* rejection = validateParameters(doc, action, context)) return rejection;
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
    JsonVariantConst parameters = command["parameters"];
    if (!parameters.isNull()) {
        // Fixed field order: a retransmission compares equal whatever its key order.
        JsonArray values = fields.add<JsonArray>();
        if (!parameters["volume"].isNull()) {
            values.add(parameters["volume"]);
        } else {
            values.add(parameters["brightness"]);
            values.add(parameters["color"]);
        }
    }
    std::string content;
    serializeJson(canonical, content);
    return content;
}

Actuation commandActuation(const std::string& commandId, JsonDocument& command) {
    const std::string action = command["action"].as<std::string>();
    JsonVariantConst parameters = command["parameters"];
    if (parameters["volume"].is<int>()) return Actuation(commandId, action, uint8_t(parameters["volume"].as<int>()));
    uint32_t rgb = 0;
    if (parameters.isNull() || !parseColor(parameters["color"], rgb)) return Actuation(commandId, action);
    return Actuation(commandId, action, uint8_t(parameters["brightness"].as<int>()), rgb);
}

}  // namespace np
