#include "np_command.h"

#include <algorithm>
#include <vector>

#include "np_clock.h"

namespace np {

namespace {

// `#RRGGBB` in either case; the declaration's pattern has already accepted it (spec decision 6).
bool parseColor(JsonVariantConst value, uint32_t& rgb) {
    if (!value.is<const char*>()) return false;
    const std::string text = value.as<std::string>();
    if (text.size() != 7 || text[0] != '#') return false;
    rgb = 0;
    for (size_t i = 1; i < text.size(); ++i) {
        const char ch = text[i];
        const int digit = ch >= '0' && ch <= '9' ? ch - '0' : ch >= 'A' && ch <= 'F' ? ch - 'A' + 10 :
                          ch >= 'a' && ch <= 'f' ? ch - 'a' + 10 : -1;
        if (digit < 0) return false;
        rgb = rgb << 4 | uint32_t(digit);
    }
    return true;
}

bool validWindow(JsonDocument& doc, const CommandContext& context, const char*& rejection) {
    double issued, expires;
    if (!parseUtc(doc["issued_at"].as<std::string>(), issued) ||
        !parseUtc(doc["expires_at"].as<std::string>(), expires) || issued >= expires) {
        rejection = "INVALID_MESSAGE";
        return false;
    }
    if (issued > context.nowSec + context.maxErrorSec) {
        rejection = "INVALID_MESSAGE";
        return false;
    }
    if (context.nowSec + context.maxErrorSec >= expires) {
        rejection = "COMMAND_EXPIRED";
        return false;
    }
    return true;
}

}  // namespace

const char* validateCommand(JsonDocument& doc, const CommandContext& context) {
    if (!doc["schema_version"].is<int>() || doc["schema_version"].as<int>() != DESCRIPTOR_SCHEMA_VERSION ||
        !doc["node_id"].is<const char*>() || doc["node_id"].as<std::string>() != context.nodeId ||
        !doc["action"].is<const char*>() || !doc["target_session_id"].is<const char*>() ||
        !doc["issued_at"].is<const char*>() || !doc["expires_at"].is<const char*>()) return "INVALID_MESSAGE";
    if (!context.connected || doc["target_session_id"].as<std::string>() != context.session)
        return "SESSION_MISMATCH";
    if (!context.descriptor) return "UNSUPPORTED_ACTION";
    JsonObjectConst action = context.descriptor->action(doc["action"].as<std::string>());
    if (action.isNull()) return "UNSUPPORTED_ACTION";
    if (const char* rejection = validateArguments(action, doc["arguments"])) return rejection;
    if (!context.clockTrusted) return "CLOCK_UNVERIFIED";
    const char* rejection = nullptr;
    return validWindow(doc, context, rejection) ? nullptr : rejection;
}

const char* validateRestart(JsonDocument& doc, const CommandContext& context, const std::string& lastRestartId,
    std::string& restartId) {
    if (!doc["schema_version"].is<int>() || doc["schema_version"].as<int>() != SCHEMA_VERSION ||
        !doc["node_id"].is<const char*>() || doc["node_id"].as<std::string>() != context.nodeId ||
        !doc["restart_id"].is<const char*>() || !doc["target_session_id"].is<const char*>() ||
        !doc["issued_at"].is<const char*>() || !doc["expires_at"].is<const char*>()) return "INVALID_MESSAGE";
    const std::string id = doc["restart_id"].as<std::string>();
    if (id.empty() || id.size() > 64) return "INVALID_MESSAGE";
    if (!context.connected || doc["target_session_id"].as<std::string>() != context.session)
        return "SESSION_MISMATCH";
    if (!context.clockTrusted) return "CLOCK_UNVERIFIED";
    const char* rejection = nullptr;
    if (!validWindow(doc, context, rejection)) return rejection;
    if (id == lastRestartId) return "ALREADY_HANDLED";
    restartId = id;
    return nullptr;
}

std::string canonicalContent(JsonDocument& command) {
    JsonDocument canonical;
    JsonArray fields = canonical.to<JsonArray>();
    for (const char* key : {"node_id", "action", "target_session_id", "issued_at", "expires_at"})
        fields.add(command[key]);
    JsonVariantConst arguments = command["arguments"];
    if (!arguments.isNull()) {
        JsonArray pairs = fields.add<JsonArray>();
        if (arguments.is<JsonObjectConst>()) {
            std::vector<std::string> keys;
            for (JsonPairConst pair : arguments.as<JsonObjectConst>()) keys.push_back(pair.key().c_str());
            std::sort(keys.begin(), keys.end());  // A retransmission compares equal whatever its key order.
            for (const std::string& key : keys) {
                JsonArray pair = pairs.add<JsonArray>();
                pair.add(key);
                pair.add(arguments[key]);
            }
        } else {
            pairs.add(arguments);  // Rejected anyway; kept so the record still differs from one without.
        }
    }
    std::string content;
    serializeJson(canonical, content);
    return content;
}

Actuation commandActuation(const std::string& commandId, JsonDocument& command) {
    Actuation request(commandId, command["action"].as<std::string>());
    JsonVariantConst arguments = command["arguments"];
    if (arguments["brightness"].is<int>()) {
        request.hasBrightness = true;
        request.brightness = uint8_t(arguments["brightness"].as<int>());
    }
    uint32_t rgb = 0;
    if (parseColor(arguments["color"], rgb)) {
        request.hasColor = true;
        request.color = rgb;
    }
    if (arguments["volume"].is<int>()) {
        request.hasVolume = true;
        request.volume = uint8_t(arguments["volume"].as<int>());
    }
    return request;
}

}  // namespace np
