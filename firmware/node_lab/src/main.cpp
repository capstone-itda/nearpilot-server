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

WiFiClient network;
MQTTClient mqtt(NP_MAX_COMMAND_BYTES + 1024);
Preferences records;
bool storageReady = false;
bool handlingCommand = false;
String session;
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

bool clockTrusted() {
    return synchronizedClock && NP_CLOCK_MAX_ERROR_SEC >= 0.0 &&
        NP_TIME_TRUST_MS > 0 && NP_TIME_RESYNC_MS > 0 &&
        (millis() - lastSync <= NP_TIME_TRUST_MS);
}

void onSync(timeval*) {
    lastSync = millis();
    synchronizedClock = true;
    Serial.printf("CLOCK_SYNCED utc=%s\n", utcNow().c_str());
}

// Read-only USB diagnostic. It never changes clock trust or authorizes output.
void clockProbe() {
    static String line;
    static bool overflow = false;
    unsigned budget = 64;
    while (Serial.available() && budget--) {
        char ch = Serial.read();
        if (ch == '\r') continue;
        if (ch != '\n') {
            if (line.length() < 48) line += ch;
            else overflow = true;
            continue;
        }
        if (!overflow && line.startsWith("CLOCK? ") && line.length() == 39) {
            String nonce = line.substring(7);
            bool valid = true;
            for (unsigned i = 0; i < nonce.length(); ++i)
                if (!isxdigit(static_cast<unsigned char>(nonce[i]))) valid = false;
            if (valid) {
                timeval stamp;
                gettimeofday(&stamp, nullptr);
                Serial.printf("CLOCK_SAMPLE nonce=%s unix_us=%lld synced=%d age_ms=%lu uptime_ms=%lu\n",
                    nonce.c_str(), static_cast<long long>(stamp.tv_sec) * 1000000 + stamp.tv_usec,
                    synchronizedClock, millis() - lastSync, millis());
            }
        }
        line = "";
        overflow = false;
    }
}

void common(JsonDocument& doc, int channel) {
    doc["schema_version"] = 1;
    doc["node_id"] = NP_NODE_ID;
    doc["session_id"] = session;
    doc["seq"] = ++seq[channel];
    doc["sent_at"] = utcNow();
}

bool report(const char* kind, JsonDocument& doc, int qos, bool retained = false) {
    String body;
    serializeJson(doc, body);
    bool sent = mqtt.publish(String("nearpilot/node/") + NP_NODE_ID + "/" + kind, body, retained, qos);
    Serial.printf("MQTT_REPORT kind=%s seq=%lu published=%d\n", kind,
        doc["seq"].as<unsigned long>(), sent);
    return sent;  // MQTT confirmation alone is not physical success.
}

void announce() {
    JsonDocument doc;
    common(doc, 0);
    doc["capability"] = "light";
    doc["occupancy"] = "shared";
    report("announce", doc, 1, true);
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
    if (mqtt.connected() && clockTrusted()) report("rssi", doc, 0);
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
        !doc["node_id"].is<String>() || doc["node_id"].as<String>() != NP_NODE_ID ||
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
    if (issued > timestamp + NP_CLOCK_MAX_ERROR_SEC) return "INVALID_MESSAGE";
    if (timestamp + NP_CLOCK_MAX_ERROR_SEC >= expires) return "COMMAND_EXPIRED";
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

void setup() {
    Serial.begin(NP_SERIAL_BAUD);
    setenv("TZ", "UTC0", 1);
    tzset();
    storageReady = records.begin("np-node-lab", false);
    if (NP_OUTPUT_PIN >= 0) {
        // Avoid a transient active pulse when switching a pin into output mode.
        digitalWrite(NP_OUTPUT_PIN, NP_OUTPUT_ACTIVE_HIGH ? LOW : HIGH);
        pinMode(NP_OUTPUT_PIN, OUTPUT);
    }
    Serial.printf("NODE_START node=%s mode=%s storage=%d\n", NP_NODE_ID,
        NP_OUTPUT_PIN >= 0 ? "optional_gpio" : "serial_only", storageReady);
    WiFi.mode(WIFI_STA);
    WiFi.begin(NP_WIFI_SSID, NP_WIFI_PASSWORD);
    sntp_set_time_sync_notification_cb(onSync);
    if (strlen(NP_TIME_SERVER) && NP_TIME_RESYNC_MS > 0 && NP_TIME_TRUST_MS > 0) {
        sntp_set_sync_interval(NP_TIME_RESYNC_MS);
        configTime(0, 0, NP_TIME_SERVER);
    }
    mqtt.begin(NP_MQTT_HOST, NP_MQTT_PORT, network);
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
    clockProbe();
    static unsigned long lastConnect = 0, lastWifi = 0, lastStatus = 0, lastRssi = 0, lastClock = 0;
    static bool previouslyTrusted = false;
    unsigned long current = millis();
    if (WiFi.status() != WL_CONNECTED && current - lastWifi >= NP_WIFI_RECONNECT_MS) {
        lastWifi = current;
        WiFi.reconnect();
    }
    if (WiFi.status() == WL_CONNECTED && !mqtt.connected() && current - lastConnect >= NP_RECONNECT_MS) {
        lastConnect = current;
        session = newUuid();
        memset(seq, 0, sizeof(seq));
        if (mqtt.connect((String("np-") + NP_NODE_ID).c_str(), NP_MQTT_USER, NP_MQTT_PASSWORD)) {
            String base = String("nearpilot/node/") + NP_NODE_ID;
            if (!mqtt.subscribe(base + "/cmd", 1) || !mqtt.subscribe(base + "/safety_check", 1)) {
                mqtt.disconnect();
            } else {
                Serial.printf("MQTT_CONNECTED session_id=%s\n", session.c_str());
                announce();
                status();
                replayRecords();
            }
        } else Serial.println("MQTT_CONNECT_FAILED");
    }
    mqtt.loop();
    bool trusted = clockTrusted();
    if (mqtt.connected() && trusted && !previouslyTrusted) replayRecords();
    previouslyTrusted = trusted;
    if (!inbox.empty()) {
        Incoming received = inbox.front();
        inbox.erase(inbox.begin());
        if (received.topic.endsWith("/cmd")) handleCommand(received.payload, received.receivedBusy);
        else Serial.println("SAFETY_CHECK_UNSUPPORTED no_sensor_evidence_no_pass_result");
    }
    if (current - lastStatus >= NP_STATUS_MS) {
        lastStatus = current;
        if (mqtt.connected()) status();
    }
    if (current - lastRssi >= NP_RSSI_FRAME_MS) { lastRssi = current; rssiFrame(); }
    if (current - lastClock >= NP_STATUS_MS && !clockTrusted()) {
        lastClock = current;
        Serial.println("CLOCK_UNVERIFIED status_rssi_and_new_output_blocked");
    }
    delay(1);
}
