#include "np_config.h"

#include <cctype>
#include <cstdlib>
#include <cstring>

namespace np {

const char* const CONFIG_KEYS[CONFIG_KEY_COUNT] = {"node_id", "mqtt_user", "mqtt_password", "wifi_ssid",
    "wifi_password", "mqtt_host", "mqtt_port", "time_server", "clock_max_error_sec", "time_trust_ms",
    "time_resync_ms"};

namespace {

bool charsetValid(const std::string& value, size_t minimum, size_t maximum, const char* extra) {
    if (value.size() < minimum || value.size() > maximum) return false;
    for (char ch : value)
        if (!std::isalnum(static_cast<unsigned char>(ch)) && !std::strchr(extra, ch)) return false;
    return true;
}

bool textValid(const std::string& value, size_t minimum, size_t maximum) {
    if (value.size() < minimum || value.size() > maximum) return false;
    for (char raw : value) {
        unsigned char ch = raw;
        if (ch < 0x20 || ch == 0x7f) return false;
    }
    return true;
}

bool parseUnsigned(const std::string& value, unsigned long maximum, unsigned long& out) {
    if (value.empty() || value.size() > 10) return false;
    unsigned long long parsed = 0;
    for (char ch : value) {
        if (!std::isdigit(static_cast<unsigned char>(ch))) return false;
        parsed = parsed * 10 + (ch - '0');
    }
    if (parsed > maximum) return false;
    out = parsed;
    return true;
}

// lwIP SNTP does not poll faster than 15 s; zero keeps clock trust disabled.
bool trustWindowValid(unsigned long value) { return value == 0 || (value >= 15000UL && value <= 86400000UL); }

int nibble(char ch) {
    if (ch >= '0' && ch <= '9') return ch - '0';
    if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
    if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
    return -1;
}

}  // namespace

bool isConfigKey(const std::string& key) {
    for (const char* name : CONFIG_KEYS)
        if (key == name) return true;
    return false;
}

const char* decodeConfig(JsonDocument& doc, NodeConfig& out) {
    for (const char* key : CONFIG_KEYS)
        if (!doc[key].is<const char*>()) return key;
    out.nodeId = doc["node_id"].as<std::string>();
    out.mqttUser = doc["mqtt_user"].as<std::string>();
    out.mqttPassword = doc["mqtt_password"].as<std::string>();
    out.wifiSsid = doc["wifi_ssid"].as<std::string>();
    out.wifiPassword = doc["wifi_password"].as<std::string>();
    out.mqttHost = doc["mqtt_host"].as<std::string>();
    out.timeServer = doc["time_server"].as<std::string>();
    if (!charsetValid(out.nodeId, 1, 64, "_-")) return "node_id";
    if (!charsetValid(out.mqttUser, 1, 64, "_-")) return "mqtt_user";
    if (!textValid(out.mqttPassword, 1, 64)) return "mqtt_password";
    if (!textValid(out.wifiSsid, 1, 32)) return "wifi_ssid";
    if (!textValid(out.wifiPassword, 0, 64)) return "wifi_password";
    if (!charsetValid(out.mqttHost, 1, 64, ".-")) return "mqtt_host";
    if (!charsetValid(out.timeServer, 0, 64, ".-")) return "time_server";
    unsigned long port;
    if (!parseUnsigned(doc["mqtt_port"].as<std::string>(), 65535, port) || !port) return "mqtt_port";
    out.mqttPort = port;
    std::string bound = doc["clock_max_error_sec"].as<std::string>();
    char* end = nullptr;
    double error = std::strtod(bound.c_str(), &end);
    if (bound.empty() || *end || !(bound == "-1" || (error >= 0.0 && error < 3600.0))) return "clock_max_error_sec";
    out.clockMaxErrorSec = error;
    if (!parseUnsigned(doc["time_trust_ms"].as<std::string>(), 86400000UL, out.timeTrustMs) ||
        !trustWindowValid(out.timeTrustMs)) return "time_trust_ms";
    if (!parseUnsigned(doc["time_resync_ms"].as<std::string>(), 86400000UL, out.timeResyncMs) ||
        !trustWindowValid(out.timeResyncMs)) return "time_resync_ms";
    if (out.timeTrustMs && out.timeResyncMs && out.timeResyncMs > out.timeTrustMs) return "time_resync_ms";
    out.generation = doc["generation"] | 0UL;
    return nullptr;
}

bool hexValid(const std::string& value) {
    for (char ch : value)
        if (nibble(ch) < 0) return false;
    return true;
}

bool fromHex(const std::string& hex, std::string& out) {
    if (hex.size() % 2 || !hexValid(hex)) return false;
    out.clear();
    for (size_t i = 0; i < hex.size(); i += 2) {
        char ch = static_cast<char>(nibble(hex[i]) * 16 + nibble(hex[i + 1]));
        if (!ch) return false;
        out += ch;
    }
    return true;
}

}  // namespace np
