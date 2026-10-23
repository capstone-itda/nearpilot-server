// Experimental node: iBeacon -> MQTT, CORE command -> serial / optional GPIO.
// A serial line or output write is NOT physical completion evidence.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <MQTT.h>
#include <NimBLEDevice.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_sntp.h>
#include <esp_system.h>
#include <sys/time.h>
#include <time.h>
#include <vector>
#include "beacon.h"
#if __has_include("config.local.h")
#include "config.local.h"
#else
#include "config.example.h"
#endif

static_assert(NP_MAX_COMMAND_RECORDS > 0 && NP_MAX_COMMAND_RECORDS < 10000, "NVS slot limit");
static_assert(NP_RSSI_FRAME_MS > 0 && NP_STATUS_MS > 0, "report interval");
static_assert(NP_BLE_SCAN_WINDOW_MS <= NP_BLE_SCAN_INTERVAL_MS, "BLE scan window");
static_assert(NP_RECONNECT_MS > 0 && NP_WIFI_RECONNECT_MS > 0, "retry interval");
static_assert(NP_RECONNECT_MS <= NP_RETRY_MAX_MS && NP_WIFI_RECONNECT_MS <= NP_RETRY_MAX_MS, "retry limit");

// Per-device values are injected over USB and kept in their own NVS namespace.
// The command record namespace is never opened for writing by provisioning.
struct NodeConfig {
    String nodeId, mqttUser, mqttPassword, wifiSsid, wifiPassword, mqttHost, timeServer;
    uint16_t mqttPort = 0;
    double clockMaxErrorSec = -1.0;  // -1: not measured, never trusted.
    unsigned long timeTrustMs = 0, timeResyncMs = 0, generation = 0;
};
NodeConfig config;
bool configured = false;
Preferences settings;
bool settingsReady = false;

WiFiClient network;
MQTTClient mqtt(NP_MAX_COMMAND_BYTES + 1024);
Preferences records;
bool storageReady = false;
bool handlingCommand = false;
String session;
bool announced = false;
unsigned long seq[5] = {};
volatile bool synchronizedClock = false;
volatile unsigned long lastSync = 0;

struct Observation {
    char uuid[37] = {};
    uint16_t major = 0, minor = 0;
    int sum = 0;
    unsigned count = 0;
};
Observation observations[NP_MAX_BEACONS];
portMUX_TYPE observationLock = portMUX_INITIALIZER_UNLOCKED;
struct Incoming { String topic; String payload; bool receivedBusy; };
std::vector<Incoming> inbox;

String newUuid() {
    uint8_t data[16];
    esp_fill_random(data, sizeof(data));
    data[6] = (data[6] & 0x0f) | 0x40;
    data[8] = (data[8] & 0x3f) | 0x80;
    char value[37];
    snprintf(value, sizeof(value),
        "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
        data[0], data[1], data[2], data[3], data[4], data[5], data[6], data[7],
        data[8], data[9], data[10], data[11], data[12], data[13], data[14], data[15]);
    return String(value);
}

String utcNow() {
    timeval current;
    gettimeofday(&current, nullptr);
    tm utc;
    gmtime_r(&current.tv_sec, &utc);
    char stamp[40];
    strftime(stamp, sizeof(stamp), "%Y-%m-%dT%H:%M:%S", &utc);
    char tail[16];
    snprintf(tail, sizeof(tail), ".%06ldZ", long(current.tv_usec));
    return String(stamp) + tail;
}

bool clockConfigured() {
    return configured && config.timeServer.length() && config.clockMaxErrorSec >= 0.0 &&
        config.timeTrustMs > 0 && config.timeResyncMs > 0;
}

bool clockTrusted() {
    return synchronizedClock && clockConfigured() && (millis() - lastSync <= config.timeTrustMs);
}

void onSync(timeval*) {
    lastSync = millis();
    synchronizedClock = true;
    Serial.printf("CLOCK_SYNCED utc=%s\n", utcNow().c_str());
}

void common(JsonDocument& doc, int channel) {
    doc["schema_version"] = 1;
    doc["node_id"] = config.nodeId;
    doc["session_id"] = session;
    doc["seq"] = ++seq[channel];
    doc["sent_at"] = utcNow();
}

bool report(const char* kind, JsonDocument& doc, int qos, bool retained = false) {
    String body;
    serializeJson(doc, body);
    bool sent = mqtt.publish(String("nearpilot/node/") + config.nodeId + "/" + kind, body, retained, qos);
    Serial.printf("MQTT_REPORT kind=%s seq=%lu published=%d\n", kind,
        doc["seq"].as<unsigned long>(), sent);
    return sent;  // MQTT confirmation alone is not physical success.
}

bool announce() {
    JsonDocument doc;
    common(doc, 0);
    doc["capability"] = "light";
    doc["occupancy"] = "shared";
    return report("announce", doc, 1, true);
}

void status() {
    if (!clockTrusted()) return;
    JsonDocument doc;
    common(doc, 1);
    doc["observed_at"] = utcNow();
    JsonObject state = doc["physical_state"].to<JsonObject>();
    state["door_closed"] = nullptr;
    state["light_on"] = nullptr;  // No light sensor exists on the bare ESP32.
    state["actuator_state"] = handlingCommand ? "busy" : "idle";
    state["sensor_fault"] = false; // This diagnostic node has no external sensors.
    report("status", doc, 1);
}

class ScanCallbacks : public NimBLEScanCallbacks {
    void onResult(const NimBLEAdvertisedDevice* device) override {
        const auto manufacturer = device->getManufacturerData();
        char uuid[37];
        uint16_t major, minor;
        if (!decodeIBeacon(reinterpret_cast<const uint8_t*>(manufacturer.data()),
                           manufacturer.size(), uuid, major, minor)) return;
        portENTER_CRITICAL(&observationLock);
        for (auto& item : observations) {
            if (!item.count || (!strcmp(item.uuid, uuid) && item.major == major && item.minor == minor)) {
                strcpy(item.uuid, uuid);
                item.major = major;
                item.minor = minor;
                item.sum += device->getRSSI();
                ++item.count;
                break;
            }
        }
        portEXIT_CRITICAL(&observationLock);
    }
};
ScanCallbacks scanCallbacks;

void rssiFrame() {
    Observation copy[NP_MAX_BEACONS];
    portENTER_CRITICAL(&observationLock);
    memcpy(copy, observations, sizeof(copy));
    memset(observations, 0, sizeof(observations));
    portEXIT_CRITICAL(&observationLock);
    JsonDocument doc;
    common(doc, 2);
    doc["frame_ts"] = utcNow();
    JsonArray batch = doc["observations"].to<JsonArray>();
    for (const auto& item : copy) {
        if (!item.count) continue;
        float rssi = float(item.sum) / item.count;
        Serial.printf("BLE_BEACON uuid=%s major=%u minor=%u rssi=%.1f samples=%u\n",
            item.uuid, item.major, item.minor, rssi, item.count);
        JsonObject value = batch.add<JsonObject>();
        value["uuid"] = item.uuid;
        value["major"] = item.major;
        value["minor"] = item.minor;
        value["rssi"] = rssi;
    }
    if (mqtt.connected() && announced && clockTrusted()) report("rssi", doc, 0);
}

String slotName(int slot) { return String("c") + slot; }

bool saveRecord(int slot, JsonDocument& record) {
    String body;
    serializeJson(record, body);
    String key = slotName(slot);
    return storageReady && records.putString(key.c_str(), body) == body.length()
        && records.getString(key.c_str()) == body;
}

// Scan fixed slots; no separately persisted index can lose an accepted record.
int findRecord(const String& id, JsonDocument& record, int& empty) {
    empty = -1;
    if (!storageReady) return -2;
    for (int slot = 0; slot < NP_MAX_COMMAND_RECORDS; ++slot) {
        String key = slotName(slot);
        if (!records.isKey(key.c_str())) {
            if (empty < 0) empty = slot;
            continue;
        }
        String body = records.getString(key.c_str());
        JsonDocument stored;
        if (deserializeJson(stored, body) || !stored["command_id"].is<String>()) return -2;
        if (stored["command_id"].as<String>() == id) {
            record.set(stored);
            return slot;
        }
    }
    return -1;
}

void result(JsonDocument& record) {
    JsonDocument doc;
    common(doc, 3);
    for (const char* key : {"command_id", "event_id", "state", "occurred_at"}) doc[key] = record[key];
    if (record["error_code"].is<String>()) doc["error_code"] = record["error_code"];
    report("result", doc, 1);
    Serial.printf("COMMAND_RESULT command_id=%s state=%s\n",
        record["command_id"].as<const char*>(), record["state"].as<const char*>());
}

void event(JsonDocument& record, const char* state, const char* error = nullptr) {
    record["state"] = state;
    record["event_id"] = newUuid();
    record["occurred_at"] = utcNow();
    record.remove("error_code");
    if (error) record["error_code"] = error;
}

// Strict server wire timestamps: UTC Z or +00:00, optional fractional seconds.
bool parseUtc(const String& raw, double& seconds) {
    tm parsed = {};
    const char* tail = strptime(raw.c_str(), "%Y-%m-%dT%H:%M:%S", &parsed);
    if (!tail) return false;
    char normalized[20];
    tm original = parsed;
    time_t whole = mktime(&parsed);
    strftime(normalized, sizeof(normalized), "%Y-%m-%dT%H:%M:%S", &parsed);
    if (raw.substring(0, 19) != normalized || parsed.tm_mon != original.tm_mon ||
        parsed.tm_mday != original.tm_mday) return false;
    double fraction = 0.0, scale = 0.1;
    if (*tail == '.') {
        ++tail;
        if (*tail < '0' || *tail > '9') return false;
        while (*tail >= '0' && *tail <= '9') {
            fraction += (*tail++ - '0') * scale;
            scale *= 0.1;
        }
    }
    if (strcmp(tail, "Z") && strcmp(tail, "+00:00")) return false;
    seconds = double(whole) + fraction; // TZ is explicitly UTC in setup.
    return true;
}

const char* validate(JsonDocument& doc) {
    if (!doc["schema_version"].is<int>() || doc["schema_version"].as<int>() != 1 ||
        !doc["node_id"].is<String>() || doc["node_id"].as<String>() != config.nodeId ||
        !doc["action"].is<String>() || !doc["target_session_id"].is<String>() ||
        !doc["issued_at"].is<String>() || !doc["expires_at"].is<String>()) return "INVALID_MESSAGE";
    if (!mqtt.connected() || doc["target_session_id"].as<String>() != session) return "SESSION_MISMATCH";
    String action = doc["action"].as<String>();
    if (action != "on" && action != "off") return "UNSUPPORTED_ACTION";
    if (!clockTrusted()) return "CLOCK_UNVERIFIED";
    double issued, expires;
    if (!parseUtc(doc["issued_at"], issued) || !parseUtc(doc["expires_at"], expires) || issued >= expires)
        return "INVALID_MESSAGE";
    timeval current;
    gettimeofday(&current, nullptr);
    double timestamp = current.tv_sec + current.tv_usec / 1e6;
    if (issued > timestamp + config.clockMaxErrorSec) return "INVALID_MESSAGE";
    if (timestamp + config.clockMaxErrorSec >= expires) return "COMMAND_EXPIRED";
    return nullptr;
}

void handleCommand(const String& payload, bool receivedBusy) {
    JsonDocument command;
    if (deserializeJson(command, payload) || !command["command_id"].is<String>()) {
        Serial.println("COMMAND_REJECTED invalid_json_or_id");
        return;
    }
    String id = command["command_id"].as<String>();
    if (!id.length() || id.length() > NP_MAX_COMMAND_ID_BYTES) return;
    // Content fields exclude publication/session metadata; their original strings are preserved.
    JsonDocument canonical;
    JsonArray fields = canonical.to<JsonArray>();
    for (const char* key : {"node_id", "action", "target_session_id", "issued_at", "expires_at"})
        fields.add(command[key]);
    String content;
    serializeJson(canonical, content);
    JsonDocument record;
    int empty;
    int slot = findRecord(id, record, empty);
    Serial.printf("COMMAND_RECEIVED command_id=%s\n", id.c_str());
    if (slot >= 0) {
        if (record["content"].as<String>() != content) {
            JsonDocument conflict;
            conflict["command_id"] = id;
            event(conflict, "rejected", "COMMAND_ID_CONFLICT");
            result(conflict); // Keep original persisted record unchanged.
        } else {
            Serial.println("COMMAND_DUPLICATE no_new_output");
            result(record);
        }
        return;
    }
    record["command_id"] = id;
    record["content"] = content;
    if (slot == -2 || empty < 0) {
        event(record, "rejected", "STORAGE_ERROR");
        result(record);
        Serial.println("STORAGE_BLOCKED no_output_no_record_deletion");
        return;
    }
    const char* rejection = (receivedBusy || handlingCommand) ? "DEVICE_BUSY" : validate(command);
    event(record, rejection ? "rejected" : "accepted", rejection);
    if (!saveRecord(empty, record)) {
        event(record, "rejected", "STORAGE_ERROR");
        result(record);
        Serial.println("STORAGE_WRITE_FAILED no_output");
        return;
    }
    if (!rejection) handlingCommand = true;
    result(record);
    if (rejection) return;
    // Recheck connection, validity and time trust after the synchronous flash write / PUBACK.
    rejection = validate(command);
    if (!rejection) {
        JsonDocument progress;
        progress.set(record);
        event(progress, "running");
        result(progress);
        // Publishing can reconnect or consume validity time. Check again immediately before output.
        rejection = validate(command);
        if (!rejection) {
            bool on = command["action"].as<String>() == "on";
            if (NP_OUTPUT_PIN >= 0) digitalWrite(NP_OUTPUT_PIN, on == NP_OUTPUT_ACTIVE_HIGH ? HIGH : LOW);
            Serial.printf("COMMAND_HANDLED command_id=%s action=%s output=%s physical_completion=unverified\n",
                id.c_str(), on ? "on" : "off", NP_OUTPUT_PIN >= 0 ? "gpio" : "serial_only");
        }
    }
    // Accepted commands are never reported successful without a completion sensor.
    event(record, "unknown");
    if (saveRecord(empty, record)) result(record);
    else Serial.println("RESULT_STORE_FAILED accepted_record_kept_no_retry");
    handlingCommand = false;
}

void replayRecords() {
    if (!storageReady || !clockTrusted()) return;
    for (int slot = 0; slot < NP_MAX_COMMAND_RECORDS; ++slot) {
        String key = slotName(slot);
        if (!records.isKey(key.c_str())) continue;
        JsonDocument record;
        if (deserializeJson(record, records.getString(key.c_str()))) continue;
        if (record["state"].as<String>() == "accepted") {
            event(record, "unknown");
            if (!saveRecord(slot, record)) continue;
        }
        result(record); // Stable event ID / occurred_at; new publication seq and sent_at.
    }
}


// ---- USB provisioning (separate NVS namespace; command records are only read) ----

const char* const CONFIG_KEYS[] = {"node_id", "mqtt_user", "mqtt_password", "wifi_ssid", "wifi_password",
    "mqtt_host", "mqtt_port", "time_server", "clock_max_error_sec", "time_trust_ms", "time_resync_ms"};
JsonDocument staged;
String stagingNonce;

bool charsetValid(const String& value, unsigned minimum, unsigned maximum, const char* extra) {
    if (value.length() < minimum || value.length() > maximum) return false;
    for (unsigned i = 0; i < value.length(); ++i) {
        char ch = value[i];
        if (!isalnum(static_cast<unsigned char>(ch)) && !strchr(extra, ch)) return false;
    }
    return true;
}

bool textValid(const String& value, unsigned minimum, unsigned maximum) {
    if (value.length() < minimum || value.length() > maximum) return false;
    for (unsigned i = 0; i < value.length(); ++i) {
        unsigned char ch = value[i];
        if (ch < 0x20 || ch == 0x7f) return false;
    }
    return true;
}

bool parseUnsigned(const String& value, unsigned long maximum, unsigned long& out) {
    if (!value.length() || value.length() > 10) return false;
    unsigned long long parsed = 0;
    for (unsigned i = 0; i < value.length(); ++i) {
        if (!isdigit(static_cast<unsigned char>(value[i]))) return false;
        parsed = parsed * 10 + (value[i] - '0');
    }
    if (parsed > maximum) return false;
    out = parsed;
    return true;
}

// lwIP SNTP does not poll faster than 15 s; zero keeps clock trust disabled.
bool trustWindowValid(unsigned long value) { return value == 0 || (value >= 15000UL && value <= 86400000UL); }

// Returns nullptr when every value is present and valid, else the offending key.
const char* decodeConfig(JsonDocument& doc, NodeConfig& out) {
    for (const char* key : CONFIG_KEYS)
        if (!doc[key].is<const char*>()) return key;
    out.nodeId = doc["node_id"].as<String>();
    out.mqttUser = doc["mqtt_user"].as<String>();
    out.mqttPassword = doc["mqtt_password"].as<String>();
    out.wifiSsid = doc["wifi_ssid"].as<String>();
    out.wifiPassword = doc["wifi_password"].as<String>();
    out.mqttHost = doc["mqtt_host"].as<String>();
    out.timeServer = doc["time_server"].as<String>();
    if (!charsetValid(out.nodeId, 1, 64, "_-")) return "node_id";
    if (!charsetValid(out.mqttUser, 1, 64, "_-")) return "mqtt_user";
    if (!textValid(out.mqttPassword, 1, 64)) return "mqtt_password";
    if (!textValid(out.wifiSsid, 1, 32)) return "wifi_ssid";
    if (!textValid(out.wifiPassword, 0, 64)) return "wifi_password";
    if (!charsetValid(out.mqttHost, 1, 64, ".-")) return "mqtt_host";
    if (!charsetValid(out.timeServer, 0, 64, ".-")) return "time_server";
    unsigned long port;
    if (!parseUnsigned(doc["mqtt_port"].as<String>(), 65535, port) || !port) return "mqtt_port";
    out.mqttPort = port;
    String bound = doc["clock_max_error_sec"].as<String>();
    char* end = nullptr;
    double error = strtod(bound.c_str(), &end);
    if (!bound.length() || *end || !(bound == "-1" || (error >= 0.0 && error < 3600.0))) return "clock_max_error_sec";
    out.clockMaxErrorSec = error;
    if (!parseUnsigned(doc["time_trust_ms"].as<String>(), 86400000UL, out.timeTrustMs) ||
        !trustWindowValid(out.timeTrustMs)) return "time_trust_ms";
    if (!parseUnsigned(doc["time_resync_ms"].as<String>(), 86400000UL, out.timeResyncMs) ||
        !trustWindowValid(out.timeResyncMs)) return "time_resync_ms";
    if (out.timeTrustMs && out.timeResyncMs && out.timeResyncMs > out.timeTrustMs) return "time_resync_ms";
    out.generation = doc["generation"] | 0UL;
    return nullptr;
}

const char* loadConfig() {
    if (!settingsReady) return "storage_unavailable";
    if (!settings.isKey("cfg")) return "missing";
    JsonDocument doc;
    NodeConfig loaded;
    if (deserializeJson(doc, settings.getString("cfg")) || decodeConfig(doc, loaded)) return "invalid";
    config = loaded;
    configured = true;
    return "loaded";
}

// Records keep the node ID inside their stored content. Never re-home them silently.
const char* recordOwnerConflict(const String& nodeId) {
    if (!storageReady) return "storage_unavailable";
    for (int slot = 0; slot < NP_MAX_COMMAND_RECORDS; ++slot) {
        String key = slotName(slot);
        if (!records.isKey(key.c_str())) continue;
        JsonDocument stored, content;
        if (deserializeJson(stored, records.getString(key.c_str())) ||
            deserializeJson(content, stored["content"].as<String>())) return "record_unreadable";
        if (content[0].as<String>() != nodeId) return "node_id_change_with_records";
    }
    return nullptr;
}

void configState() {
    Serial.printf("CONFIG_STATE configured=%d generation=%lu node=%s mqtt_user=%s mqtt_host=%s mqtt_port=%u "
        "time_server=%s clock_max_error_sec=%.6f time_trust_ms=%lu time_resync_ms=%lu wifi_ssid_bytes=%u "
        "wifi_password_set=%d mqtt_password_set=%d\n", configured, config.generation,
        config.nodeId.c_str(), config.mqttUser.c_str(), config.mqttHost.c_str(), config.mqttPort,
        config.timeServer.c_str(), config.clockMaxErrorSec, config.timeTrustMs, config.timeResyncMs,
        config.wifiSsid.length(), config.wifiPassword.length() > 0, config.mqttPassword.length() > 0);
}

bool hexValid(const String& value) {
    for (unsigned i = 0; i < value.length(); ++i)
        if (!isxdigit(static_cast<unsigned char>(value[i]))) return false;
    return true;
}

bool fromHex(const String& hex, String& out) {
    if (hex.length() % 2 || !hexValid(hex)) return false;
    out = "";
    for (unsigned i = 0; i < hex.length(); i += 2) {
        char ch = strtol(hex.substring(i, i + 2).c_str(), nullptr, 16);
        if (!ch) return false;
        out += ch;
    }
    return true;
}

void configReject(const char* reason) {
    Serial.printf("CONFIG_REJECTED reason=%s\n", reason);
}

void commitConfig(const String& nonce) {
    if (!stagingNonce.length() || nonce != stagingNonce) return configReject("no_session");
    stagingNonce = "";
    NodeConfig candidate;
    const char* invalid = decodeConfig(staged, candidate);
    if (invalid) {
        Serial.printf("CONFIG_REJECTED reason=invalid key=%s\n", invalid);
        staged.clear();
        return;
    }
    const char* conflict = settingsReady ? recordOwnerConflict(candidate.nodeId) : "storage_unavailable";
    if (conflict) {
        staged.clear();
        return configReject(conflict);
    }
    staged["generation"] = config.generation + 1;
    String body;
    serializeJson(staged, body);
    staged.clear();
    // One key, one write: a power cut leaves either the old or the new configuration.
    if (settings.putString("cfg", body) != body.length() || settings.getString("cfg") != body)
        return configReject("write_failed");
    Serial.printf("CONFIG_SAVED generation=%lu node=%s restarting=1\n", config.generation + 1,
        candidate.nodeId.c_str());
    Serial.flush();
    delay(100);
    ESP.restart();  // Apply from a clean boot: a new clock source starts unverified.
}

void serialCommand(const String& line) {
    if (line.startsWith("CLOCK? ") && line.length() == 39 && hexValid(line.substring(7))) {
        // Read-only diagnostic. It never changes clock trust or authorizes output.
        timeval stamp;
        gettimeofday(&stamp, nullptr);
        Serial.printf("CLOCK_SAMPLE nonce=%s unix_us=%lld synced=%d age_ms=%lu uptime_ms=%lu\n",
            line.substring(7).c_str(), static_cast<long long>(stamp.tv_sec) * 1000000 + stamp.tv_usec,
            synchronizedClock, millis() - lastSync, millis());
    } else if (line == "CONFIG?") {
        configState();
    } else if (line.startsWith("CONFIG_BEGIN ") && line.length() == 45 && hexValid(line.substring(13))) {
        staged.clear();
        stagingNonce = line.substring(13);
        Serial.printf("CONFIG_READY nonce=%s\n", stagingNonce.c_str());
    } else if (line.startsWith("CONFIG_SET ")) {
        if (!stagingNonce.length()) return configReject("no_session");
        String rest = line.substring(11);
        int split = rest.indexOf(' ');
        String key = split < 0 ? rest : rest.substring(0, split);
        String value;
        bool known = false;
        for (const char* name : CONFIG_KEYS) known |= key == name;
        if (!known) return configReject("unknown_key");
        if (!fromHex(split < 0 ? String() : rest.substring(split + 1), value)) return configReject("bad_value");
        staged[key] = value;
        Serial.printf("CONFIG_STAGED key=%s bytes=%u\n", key.c_str(), value.length());
    } else if (line.startsWith("CONFIG_COMMIT ")) {
        commitConfig(line.substring(14));
    } else if (line == "CONFIG_ABORT") {
        staged.clear();
        stagingNonce = "";
        Serial.println("CONFIG_ABORTED");
    }
}

void serialInput() {
    static String line;
    static bool overflow = false;
    unsigned budget = 256;
    while (Serial.available() && budget--) {
        char ch = Serial.read();
        if (ch == '\r') continue;
        if (ch != '\n') {
            if (line.length() < 200) line += ch;
            else overflow = true;
            continue;
        }
        if (overflow) configReject("line_too_long");
        else serialCommand(line);
        line = "";
        overflow = false;
    }
}

// ---- Boot, reconnect and readiness ----

const char* resetReason() {
    switch (esp_reset_reason()) {
        case ESP_RST_POWERON: return "power_on";
        case ESP_RST_EXT: return "external_pin";
        case ESP_RST_SW: return "software";
        case ESP_RST_PANIC: return "panic";
        case ESP_RST_INT_WDT: case ESP_RST_TASK_WDT: case ESP_RST_WDT: return "watchdog";
        case ESP_RST_BROWNOUT: return "brownout";
        case ESP_RST_DEEPSLEEP: return "deep_sleep";
        default: return "other";
    }
}

void onWifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
    if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP)
        Serial.printf("WIFI_CONNECTED ip=%s rssi=%d\n",
            IPAddress(info.got_ip.ip_info.ip.addr).toString().c_str(), WiFi.RSSI());
    else if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED)
        Serial.printf("WIFI_DISCONNECTED reason=%u\n", info.wifi_sta_disconnected.reason);
}

unsigned long backoff(unsigned long current) {
    return current >= NP_RETRY_MAX_MS / 2 ? NP_RETRY_MAX_MS : current * 2;
}

// Device-side connection conditions only. Execution permission is decided by the server.
// `trusted` is the loop's snapshot: the SNTP callback runs on another task.
const char* notReadyReason(bool trusted) {
    if (!configured) return "config_missing";
    if (!storageReady) return "storage_unavailable";
    if (WiFi.status() != WL_CONNECTED) return "wifi_disconnected";
    if (!clockConfigured()) return "clock_not_configured";
    if (!trusted) return synchronizedClock ? "clock_trust_expired" : "clock_unverified";
    if (!mqtt.connected()) return "mqtt_disconnected";
    if (!announced) return "announce_pending";
    return nullptr;
}

void setup() {
    Serial.begin(NP_SERIAL_BAUD);
    setenv("TZ", "UTC0", 1);
    tzset();
    storageReady = records.begin("np-node-lab", false);
    settingsReady = settings.begin("np-config", false);
    const char* loaded = loadConfig();
    if (NP_OUTPUT_PIN >= 0) {
        // Avoid a transient active pulse when switching a pin into output mode.
        digitalWrite(NP_OUTPUT_PIN, NP_OUTPUT_ACTIVE_HIGH ? LOW : HIGH);
        pinMode(NP_OUTPUT_PIN, OUTPUT);
    }
    Serial.printf("NODE_START node=%s config=%s generation=%lu mode=%s storage=%d reset_reason=%s\n",
        configured ? config.nodeId.c_str() : "-", loaded, config.generation,
        NP_OUTPUT_PIN >= 0 ? "optional_gpio" : "serial_only", storageReady, resetReason());
    if (!configured) {
        Serial.println("CONFIG_MISSING usb_provisioning_required network_not_started");
        return;
    }
    WiFi.onEvent(onWifiEvent);
    WiFi.persistent(false);  // Credentials live only in the provisioning namespace.
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(false);  // loop() owns retries so every attempt is logged.
    Serial.printf("WIFI_CONNECTING ssid_bytes=%u\n", config.wifiSsid.length());
    WiFi.begin(config.wifiSsid.c_str(), config.wifiPassword.c_str());
    sntp_set_time_sync_notification_cb(onSync);
    if (clockConfigured()) {
        sntp_set_sync_interval(config.timeResyncMs);
        configTime(0, 0, config.timeServer.c_str());
        Serial.printf("CLOCK_SOURCE server=%s resync_ms=%lu trust_ms=%lu max_error_sec=%.3f\n",
            config.timeServer.c_str(), config.timeResyncMs, config.timeTrustMs, config.clockMaxErrorSec);
    } else Serial.println("CLOCK_SOURCE_UNSET clock_trust_disabled");
    mqtt.begin(config.mqttHost.c_str(), config.mqttPort, network);
    mqtt.setOptions(NP_MQTT_KEEPALIVE_SEC, true, NP_MQTT_IO_TIMEOUT_MS);
    mqtt.onMessage(+[](String& topic, String& payload) {
        // QoS1 publish can call this callback; never publish/actuate recursively here.
        if (payload.length() <= NP_MAX_COMMAND_BYTES && inbox.size() < 4)
            inbox.push_back({topic, payload, handlingCommand});
        else Serial.println("COMMAND_QUEUE_REJECTED capacity_or_size");
    });
    NimBLEDevice::init("");
    NimBLEScan* scan = NimBLEDevice::getScan();
    scan->setScanCallbacks(&scanCallbacks, true);
    scan->setActiveScan(false);
    scan->setInterval(NP_BLE_SCAN_INTERVAL_MS);
    scan->setWindow(NP_BLE_SCAN_WINDOW_MS);
    scan->setMaxResults(0);
    scan->start(0, false, true);
}

void loop() {
    serialInput();
    unsigned long current = millis();
    static unsigned long lastReadyLog = 0;
    static String lastReason = "boot";
    if (!configured) {
        if (current - lastReadyLog >= NP_RETRY_MAX_MS || lastReason != "config_missing") {
            lastReadyLog = current;
            lastReason = "config_missing";
            Serial.println("NODE_NOT_READY reason=config_missing");
        }
        delay(1);
        return;
    }
    static unsigned long lastConnect = 0, lastWifi = 0, lastStatus = 0, lastRssi = 0, lastAnnounce = 0;
    static unsigned long wifiDelay = NP_WIFI_RECONNECT_MS, mqttDelay = NP_RECONNECT_MS;
    static bool previouslyTrusted = false, wasConnected = false, deferredLogged = false;
    bool wifiUp = WiFi.status() == WL_CONNECTED;
    if (wifiUp) wifiDelay = NP_WIFI_RECONNECT_MS;
    else if (current - lastWifi >= wifiDelay) {
        lastWifi = current;
        wifiDelay = backoff(wifiDelay);
        Serial.printf("WIFI_RETRY status=%d next_retry_ms=%lu\n", WiFi.status(), wifiDelay);
        WiFi.reconnect();
    }
    // A publish inside the previous iteration can drop the link; log it before reconnecting.
    auto noteDisconnect = [&]() {
        if (!wasConnected || mqtt.connected()) return;
        wasConnected = announced = false;
        Serial.printf("MQTT_DISCONNECTED session_id=%s error=%d wifi=%d\n", session.c_str(), mqtt.lastError(),
            WiFi.status() == WL_CONNECTED);
    };
    noteDisconnect();
    if (wifiUp && !mqtt.connected() && current - lastConnect >= mqttDelay) {
        lastConnect = current;
        session = newUuid();
        memset(seq, 0, sizeof(seq));
        announced = deferredLogged = false;
        if (mqtt.connect((String("np-") + config.nodeId).c_str(), config.mqttUser.c_str(),
                         config.mqttPassword.c_str())) {
            String base = String("nearpilot/node/") + config.nodeId;
            if (!mqtt.subscribe(base + "/cmd", 1) || !mqtt.subscribe(base + "/safety_check", 1)) {
                mqttDelay = backoff(mqttDelay);
                Serial.printf("MQTT_SUBSCRIBE_FAILED error=%d next_retry_ms=%lu\n", mqtt.lastError(), mqttDelay);
                mqtt.disconnect();
            } else {
                mqttDelay = NP_RECONNECT_MS;
                wasConnected = true;
                Serial.printf("MQTT_CONNECTED session_id=%s subscribed=cmd,safety_check\n", session.c_str());
            }
        } else {
            mqttDelay = backoff(mqttDelay);
            Serial.printf("MQTT_CONNECT_FAILED host=%s port=%u error=%d return_code=%d next_retry_ms=%lu\n",
                config.mqttHost.c_str(), config.mqttPort, mqtt.lastError(), mqtt.returnCode(), mqttDelay);
        }
    }
    mqtt.loop();
    noteDisconnect();
    bool trusted = clockTrusted();
    static unsigned long lastClockRetry = 0;
    if (!trusted && wifiUp && clockConfigured() && current - lastClockRetry >= NP_RETRY_MAX_MS) {
        // lwIP backs off after failed polls; ask again now instead of waiting minutes.
        lastClockRetry = current;
        if (current >= NP_RETRY_MAX_MS) Serial.printf("CLOCK_RETRY server=%s restarted=%d\n",
            config.timeServer.c_str(), sntp_restart());
    }
    if (trusted && !previouslyTrusted) Serial.printf("CLOCK_TRUSTED age_ms=%lu\n", millis() - lastSync);
    if (!trusted && previouslyTrusted)
        Serial.printf("CLOCK_TRUST_EXPIRED age_ms=%lu trust_ms=%lu\n", millis() - lastSync, config.timeTrustMs);
    if (mqtt.connected() && !announced) {
        // Announce once per connection with a trusted sent_at; status and replay follow it.
        if (trusted && current - lastAnnounce >= NP_STATUS_MS) {
            lastAnnounce = current;
            announced = announce();
            if (announced) {
                Serial.printf("ANNOUNCED session_id=%s\n", session.c_str());
                lastStatus = current;
                status();
                replayRecords();
            }
        } else if (!trusted && !deferredLogged) {
            deferredLogged = true;
            Serial.printf("ANNOUNCE_DEFERRED session_id=%s reason=clock_unverified\n", session.c_str());
        }
    } else if (mqtt.connected() && trusted && !previouslyTrusted) replayRecords();
    previouslyTrusted = trusted;
    if (!inbox.empty()) {
        Incoming received = inbox.front();
        inbox.erase(inbox.begin());
        if (received.topic.endsWith("/cmd")) handleCommand(received.payload, received.receivedBusy);
        else Serial.println("SAFETY_CHECK_UNSUPPORTED no_sensor_evidence_no_pass_result");
    }
    if (current - lastStatus >= NP_STATUS_MS) {
        lastStatus = current;
        if (mqtt.connected() && announced) status();
    }
    if (current - lastRssi >= NP_RSSI_FRAME_MS) { lastRssi = current; rssiFrame(); }
    const char* reason = notReadyReason(trusted);
    String state = reason ? reason : "ready";
    if (state != lastReason || (reason && current - lastReadyLog >= NP_RETRY_MAX_MS)) {
        lastReadyLog = current;
        lastReason = state;
        if (reason) Serial.printf("NODE_NOT_READY reason=%s uptime_ms=%lu\n", reason, current);
        else Serial.printf("NODE_READY session_id=%s clock_age_ms=%lu uptime_ms=%lu\n",
            session.c_str(), millis() - lastSync, current);
    }
    delay(1);
}
