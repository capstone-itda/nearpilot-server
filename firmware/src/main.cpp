// NearPilot node: iBeacon -> MQTT v2, CORE command -> serial / optional GPIO.
// Board glue only: validation, clock trust, records and readiness live in lib/.
// Success means the control signal was emitted and stored, not an observed physical state.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <MQTT.h>
#include <NimBLEDevice.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_sntp.h>
#include <esp_system.h>
#include <esp_task_wdt.h>
#include <sys/time.h>
#include <time.h>
#include <string>
#include <vector>

#include "np_beacon.h"
#include "np_clock.h"
#include "np_command.h"
#include "np_config.h"
#include "np_executor.h"
#include "np_ledger.h"
#include "np_nvs_store.h"
#include "np_occupancy.h"
#include "np_ready.h"
#if __has_include("config.local.h")
#include "config.local.h"
#else
#include "config.example.h"
#endif

static_assert(NP_MAX_COMMAND_RECORDS > 0 && NP_MAX_COMMAND_RECORDS < 10000, "NVS slot limit");
static_assert(NP_LEGACY_COMMAND_RECORDS >= 0 && NP_LEGACY_COMMAND_RECORDS < 10000, "NVS slot limit");
static_assert(NP_RSSI_FRAME_MS > 0 && NP_STATUS_MS > 0, "report interval");
static_assert(NP_BLE_SCAN_WINDOW_MS <= NP_BLE_SCAN_INTERVAL_MS, "BLE scan window");
#ifndef NP_WATCHDOG_TIMEOUT_SEC
#error "config.local.h needs NP_WATCHDOG_TIMEOUT_SEC (see config.example.h)"
#endif
// WiFiClient's default connect timeout is 3 s; the watchdog is fed after each network call.
static_assert(NP_WATCHDOG_TIMEOUT_SEC * 1000UL > 3000UL + NP_MQTT_IO_TIMEOUT_MS, "watchdog shorter than one wait");
static_assert(NP_RECONNECT_MS > 0 && NP_WIFI_RECONNECT_MS > 0, "retry interval");
static_assert(NP_RECONNECT_MS <= NP_RETRY_MAX_MS && NP_WIFI_RECONNECT_MS <= NP_RETRY_MAX_MS, "retry limit");
#ifndef NP_SERVO_PIN
#error "config.local.h needs the NP_SERVO_* values (see config.example.h)"
#endif
static_assert(NP_SERVO_PIN < 0 || NP_SERVO_PIN != NP_OUTPUT_PIN, "servo and GPIO output share a pin");
static_assert(NP_SERVO_LOCKED_DEG >= 0 && NP_SERVO_LOCKED_DEG <= 180, "servo locked angle");
static_assert(NP_SERVO_OPEN_DEG >= 0 && NP_SERVO_OPEN_DEG <= 180, "servo open angle");
static_assert(NP_SERVO_MIN_US < NP_SERVO_MAX_US && NP_SERVO_OPEN_HOLD_MS > 0, "servo pulse range and hold");
#ifndef NP_LED_PIN
#error "config.local.h needs the NP_LED_* values (see config.example.h)"
#endif
static_assert(NP_LED_PIN < 0 || (NP_LED_PIN != NP_SERVO_PIN && NP_LED_PIN != NP_OUTPUT_PIN), "LED stick shares a pin");
static_assert(NP_LED_COUNT > 0 && NP_LED_LEVEL > 0 && NP_LED_LEVEL <= 255, "LED stick size and level");
#if NP_LED_PIN >= 0
#include <Adafruit_NeoPixel.h>
#endif

// Per-device values are injected over USB and kept in their own NVS namespace.
// The command record namespace is never opened for writing by provisioning.
np::NodeConfig config;
bool configured = false;
Preferences settings;
bool settingsReady = false;

WiFiClient network;
MQTTClient mqtt(NP_MAX_COMMAND_BYTES + 1024);
// New records go to the dedicated partition; node_lab's slots in the default nvs are still consulted.
np::NvsRecordStore recordStore("np_records", "np-records", NP_MAX_COMMAND_RECORDS);
np::NvsRecordStore legacyStore("nvs", "np-node-lab", NP_LEGACY_COMMAND_RECORDS);
np::LayeredRecordStore records(recordStore, legacyStore);
np::Ledger ledger(records);
String session;
bool announced = false;
bool watchdogArmed = false;

// Every network call is bounded (TCP connect or NP_MQTT_IO_TIMEOUT_MS); feed after each one.
void feedWatchdog() {
    if (watchdogArmed) esp_task_wdt_reset();
}
unsigned long seq[4] = {};  // announce, status, rssi, result
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

bool clockConfigured() { return np::clockConfigured(configured, config); }

bool clockTrusted() { return np::clockTrusted(configured, config, synchronizedClock, lastSync, millis()); }

np::CommandContext liveContext() {
    timeval current;
    gettimeofday(&current, nullptr);
    np::CommandContext context;
    context.nodeId = config.nodeId;
    context.capability = config.capability;
    context.session = session.c_str();
    context.connected = mqtt.connected();
    context.clockTrusted = clockTrusted();
    context.nowSec = current.tv_sec + current.tv_usec / 1e6;
    context.maxErrorSec = config.clockMaxErrorSec;
    return context;
}

void onSync(timeval*) {
    lastSync = millis();
    synchronizedClock = true;
    Serial.printf("CLOCK_SYNCED utc=%s\n", utcNow().c_str());
}

void common(JsonDocument& doc, int channel) {
    doc["schema_version"] = np::SCHEMA_VERSION;
    doc["node_id"] = config.nodeId;
    doc["session_id"] = session;
    doc["seq"] = ++seq[channel];
    doc["sent_at"] = utcNow();
}

bool report(const char* kind, JsonDocument& doc, int qos, bool retained = false) {
    String body;
    serializeJson(doc, body);
    unsigned long started = millis();
    bool sent = mqtt.publish(String("nearpilot/node/") + config.nodeId.c_str() + "/" + kind, body, retained, qos);
    feedWatchdog();
    Serial.printf("MQTT_REPORT kind=%s seq=%lu published=%d ms=%lu\n", kind,
        doc["seq"].as<unsigned long>(), sent, millis() - started);
    return sent;  // PUBACK alone is not command success.
}

bool announce() {
    JsonDocument doc;
    common(doc, 0);
    np::announceConfig(doc, config);
    return report("announce", doc, 1, true);
}

const char* commandState();

void status() {
    if (!clockTrusted()) return;
    JsonDocument doc;
    common(doc, 1);
    doc["observed_at"] = utcNow();
    doc["command_state"] = commandState();  // Command handling only; no door, light or sensor values.
    report("status", doc, 1);
}

class ScanCallbacks : public NimBLEScanCallbacks {
    void onResult(const NimBLEAdvertisedDevice* device) override {
        const auto manufacturer = device->getManufacturerData();
        char uuid[37];
        uint16_t major, minor;
        if (!np::decodeIBeacon(reinterpret_cast<const uint8_t*>(manufacturer.data()),
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

bool result(JsonDocument& record) {
    JsonDocument doc;
    common(doc, 3);
    for (const char* key : {"command_id", "event_id", "state", "occurred_at"}) doc[key] = record[key];
    if (record["error_code"].is<const char*>()) doc["error_code"] = record["error_code"];
    bool sent = report("result", doc, 1);
    Serial.printf("COMMAND_RESULT command_id=%s state=%s\n",
        record["command_id"].as<const char*>(), record["state"].as<const char*>());
    return sent;
}

// ---- Locker servo: open, then relock after NP_SERVO_OPEN_HOLD_MS ----

np::RelockTimer relock(NP_SERVO_OPEN_HOLD_MS);

void servoAngle(int degrees) {
    ledcWrite(NP_SERVO_LEDC_CHANNEL, np::servoDuty(np::servoPulseUs(degrees, NP_SERVO_MIN_US, NP_SERVO_MAX_US),
        NP_SERVO_FREQ_HZ, NP_SERVO_LEDC_BITS));
}

void relockStep() {
    if (NP_SERVO_PIN < 0 || !relock.due(millis())) return;
    servoAngle(NP_SERVO_LOCKED_DEG);
    Serial.printf("SERVO_RELOCKED pin=%d angle=%d\n", NP_SERVO_PIN, NP_SERVO_LOCKED_DEG);
}

// ---- Occupancy LED stick: shows the server's retained display, never decides it ----

#if NP_LED_PIN >= 0
Adafruit_NeoPixel ledStick(NP_LED_COUNT, NP_LED_PIN, NEO_GRB + NEO_KHZ800);
#endif
np::OccupancyDisplay shownOccupancy = np::OccupancyDisplay::Invalid;

void showOccupancy(np::OccupancyDisplay display) {
#if NP_LED_PIN >= 0
    uint32_t rgb = np::occupancyRgb(display, NP_LED_LEVEL);
    for (int i = 0; i < NP_LED_COUNT; ++i) ledStick.setPixelColor(i, rgb);
    ledStick.show();
#endif
    shownOccupancy = display;
}

void occupancyInput(const String& payload) {
    np::OccupancyDisplay display = np::decodeOccupancy(payload.c_str(), config.nodeId);
    if (display == np::OccupancyDisplay::Invalid) {
        Serial.println("OCCUPANCY_IGNORED reason=invalid_message");  // Keeps the last color.
        return;
    }
    if (display == shownOccupancy) return;
    showOccupancy(display);
    Serial.printf("OCCUPANCY_DISPLAY pin=%d state=%s\n", NP_LED_PIN, np::occupancyName(display));
}

const char* outputMode() {
    return NP_SERVO_PIN >= 0 ? "servo" : NP_OUTPUT_PIN >= 0 ? "optional_gpio" : "serial_only";
}

void signalOutput(const np::Actuation& request) {
    bool on = request.action == "on" || request.action == "open";
    if (NP_OUTPUT_PIN >= 0) digitalWrite(NP_OUTPUT_PIN, on == NP_OUTPUT_ACTIVE_HIGH ? HIGH : LOW);
    if (NP_SERVO_PIN >= 0 && request.action == "open") {
        // Only starts the pulse train; the angle reached is not observed (FR-07).
        servoAngle(NP_SERVO_OPEN_DEG);
        relock.opened(millis());
    }
    Serial.printf("COMMAND_HANDLED command_id=%s action=%s output=%s output_completed=1\n",
        request.commandId.c_str(), request.action.c_str(),
        NP_SERVO_PIN >= 0 ? "servo" : NP_OUTPUT_PIN >= 0 ? "gpio" : "serial_only");
}

// validateCommand rejects actions the configured capability does not support before output.
np::SignalActuator outputSignal(signalOutput);

class BoardCommandPort : public np::CommandPort {
public:
    const char* validate(JsonDocument& command) override {
        return np::validateCommand(command, liveContext());
    }
    np::CommandContext retentionContext() override { return liveContext(); }
    std::string newEventId() override { return newUuid().c_str(); }
    std::string utcNow() override { return ::utcNow().c_str(); }
    void publish(JsonDocument& record) override { result(record); }
    np::Actuator& actuator() override { return outputSignal; }
    void log(const std::string& line) override { Serial.println(line.c_str()); }
};

BoardCommandPort commandPort;
np::Executor executor(ledger, commandPort, NP_MAX_COMMAND_ID_BYTES, NP_MAX_RECORD_BYTES);

const char* commandState() { return executor.commandState(); }

// Re-reports are spread over loop iterations so hundreds of records never stall MQTT, BLE or the watchdog.
bool replaying = false;

void replayRecords() {
    executor.beginReplay();
    replaying = true;
}

void replayStep() {
    if (!replaying) return;
    if (!mqtt.connected() || !clockTrusted()) {
        replaying = false;  // The next announce or regained clock trust starts again from the first slot.
        return;
    }
    replaying = ledger.ready() && executor.replayNext();
}

// ---- USB provisioning (separate NVS namespace; command records are only read) ----

JsonDocument staged;
String stagingNonce;

const char* loadConfig() {
    if (!settingsReady) return "storage_unavailable";
    if (!settings.isKey("cfg")) return "missing";
    JsonDocument doc;
    np::NodeConfig loaded;
    if (deserializeJson(doc, settings.getString("cfg")) || np::decodeConfig(doc, loaded)) return "invalid";
    config = loaded;
    configured = true;
    return "loaded";
}

void configState() {
    Serial.printf("CONFIG_STATE configured=%d generation=%lu node=%s mqtt_user=%s mqtt_host=%s mqtt_port=%u "
        "time_server=%s clock_max_error_sec=%.6f time_trust_ms=%lu time_resync_ms=%lu wifi_ssid_bytes=%u "
        "wifi_password_set=%d mqtt_password_set=%d\n", configured, config.generation,
        config.nodeId.c_str(), config.mqttUser.c_str(), config.mqttHost.c_str(), config.mqttPort,
        config.timeServer.c_str(), config.clockMaxErrorSec, config.timeTrustMs, config.timeResyncMs,
        unsigned(config.wifiSsid.size()), !config.wifiPassword.empty(), !config.mqttPassword.empty());
}

bool hexValid(const String& value) { return np::hexValid(value.c_str()); }

void configReject(const char* reason) {
    Serial.printf("CONFIG_REJECTED reason=%s\n", reason);
}

void commitConfig(const String& nonce) {
    if (!stagingNonce.length() || nonce != stagingNonce) return configReject("no_session");
    stagingNonce = "";
    np::NodeConfig candidate;
    const char* invalid = np::decodeConfig(staged, candidate);
    if (invalid) {
        Serial.printf("CONFIG_REJECTED reason=invalid key=%s\n", invalid);
        staged.clear();
        return;
    }
    const char* conflict = settingsReady ? ledger.ownerConflict(candidate.nodeId) : "storage_unavailable";
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
        std::string value;
        if (!np::isConfigKey(key.c_str())) return configReject("unknown_key");
        if (!np::fromHex(split < 0 ? "" : rest.substring(split + 1).c_str(), value))
            return configReject("bad_value");
        staged[key] = value;
        Serial.printf("CONFIG_STAGED key=%s bytes=%u\n", key.c_str(), unsigned(value.size()));
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

unsigned long backoff(unsigned long current) { return np::backoff(current, NP_RETRY_MAX_MS); }

const char* notReadyReason(bool trusted) {
    np::ReadinessInputs in;
    in.configured = configured;
    in.storageReady = ledger.ready();
    in.wifiUp = WiFi.status() == WL_CONNECTED;
    in.clockConfigured = clockConfigured();
    in.trusted = trusted;
    in.everSynchronized = synchronizedClock;
    in.mqttConnected = mqtt.connected();
    in.announced = announced;
    return np::notReadyReason(in);
}

void setup() {
    Serial.begin(NP_SERIAL_BAUD);
    setenv("TZ", "UTC0", 1);
    tzset();
    bool storageReady = recordStore.begin() & legacyStore.begin();  // Open both even if one fails.
    if (storageReady) ledger.load();  // One full read now instead of on the first command.
    settingsReady = settings.begin("np-config", false);
    const char* loaded = loadConfig();
    if (NP_OUTPUT_PIN >= 0) {
        // Avoid a transient active pulse when switching a pin into output mode.
        digitalWrite(NP_OUTPUT_PIN, NP_OUTPUT_ACTIVE_HIGH ? LOW : HIGH);
        pinMode(NP_OUTPUT_PIN, OUTPUT);
        Serial.printf("GPIO_OUTPUT pin=%d on_level=%d off_level=%d initial_level=%d\n",
            NP_OUTPUT_PIN, NP_OUTPUT_ACTIVE_HIGH ? HIGH : LOW,
            NP_OUTPUT_ACTIVE_HIGH ? LOW : HIGH, NP_OUTPUT_ACTIVE_HIGH ? LOW : HIGH);
    }
    if (NP_SERVO_PIN >= 0) {
        // Boot always holds the locked angle; an open before a reset is never repeated.
        ledcSetup(NP_SERVO_LEDC_CHANNEL, NP_SERVO_FREQ_HZ, NP_SERVO_LEDC_BITS);
        servoAngle(NP_SERVO_LOCKED_DEG);
        ledcAttachPin(NP_SERVO_PIN, NP_SERVO_LEDC_CHANNEL);
        Serial.printf("SERVO_OUTPUT pin=%d locked_deg=%d open_deg=%d hold_ms=%lu pulse_us=%d-%d freq_hz=%d "
            "initial=locked\n", NP_SERVO_PIN, NP_SERVO_LOCKED_DEG, NP_SERVO_OPEN_DEG,
            static_cast<unsigned long>(NP_SERVO_OPEN_HOLD_MS), NP_SERVO_MIN_US, NP_SERVO_MAX_US, NP_SERVO_FREQ_HZ);
    }
#if NP_LED_PIN >= 0
    ledStick.begin();
    showOccupancy(np::OccupancyDisplay::Invalid);  // Off until the first valid display.
    Serial.printf("LED_OUTPUT pin=%d count=%d level=%d initial=off\n", NP_LED_PIN, NP_LED_COUNT, NP_LED_LEVEL);
#endif
    Serial.printf("NODE_START node=%s config=%s generation=%lu mode=%s storage=%d reset_reason=%s\n",
        configured ? config.nodeId.c_str() : "-", loaded, config.generation,
        outputMode(), storageReady, resetReason());
    Serial.printf("RECORD_STORE primary=%d legacy=%d slots=%d legacy_slots=%d free_entries=%u\n",
        recordStore.ready(), legacyStore.ready(), NP_MAX_COMMAND_RECORDS, NP_LEGACY_COMMAND_RECORDS,
        unsigned(recordStore.freeEntries()));
    // Armed after the record indexes are built; a reset keeps every receipt and never re-actuates.
    watchdogArmed = esp_task_wdt_init(NP_WATCHDOG_TIMEOUT_SEC, true) == ESP_OK;
    if (watchdogArmed) enableLoopWDT();
    Serial.printf("WATCHDOG timeout_sec=%d armed=%d\n", NP_WATCHDOG_TIMEOUT_SEC, watchdogArmed);
    if (!configured) {
        Serial.println("CONFIG_MISSING usb_provisioning_required network_not_started");
        return;
    }
    WiFi.onEvent(onWifiEvent);
    WiFi.persistent(false);  // Credentials live only in the provisioning namespace.
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(false);  // loop() owns retries so every attempt is logged.
    Serial.printf("WIFI_CONNECTING ssid_bytes=%u\n", unsigned(config.wifiSsid.size()));
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
            inbox.push_back({topic, payload, executor.busy()});
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
    relockStep();
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
        if (mqtt.connect((String("np-") + config.nodeId.c_str()).c_str(), config.mqttUser.c_str(),
                         config.mqttPassword.c_str())) {
            feedWatchdog();
            String base = String("nearpilot/node/") + config.nodeId.c_str();
            bool subscribed = mqtt.subscribe(base + "/cmd", 1);
            feedWatchdog();
            if (subscribed && NP_LED_PIN >= 0) {
                subscribed = mqtt.subscribe(base + "/occupancy", 1);
                feedWatchdog();
            }
            if (!subscribed) {
                mqttDelay = backoff(mqttDelay);
                Serial.printf("MQTT_SUBSCRIBE_FAILED error=%d next_retry_ms=%lu\n", mqtt.lastError(), mqttDelay);
                mqtt.disconnect();
            } else {
                mqttDelay = NP_RECONNECT_MS;
                wasConnected = true;
                Serial.printf("MQTT_CONNECTED session_id=%s subscribed=%s\n", session.c_str(),
                    NP_LED_PIN >= 0 ? "cmd,occupancy" : "cmd");
            }
        } else {
            mqttDelay = backoff(mqttDelay);
            Serial.printf("MQTT_CONNECT_FAILED host=%s port=%u error=%d return_code=%d next_retry_ms=%lu\n",
                config.mqttHost.c_str(), config.mqttPort, mqtt.lastError(), mqtt.returnCode(), mqttDelay);
        }
    }
    feedWatchdog();  // After a failed connect too.
    mqtt.loop();
    feedWatchdog();
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
        if (received.topic.endsWith("/cmd")) executor.handle(received.payload.c_str(), received.receivedBusy);
        else if (received.topic.endsWith("/occupancy")) occupancyInput(received.payload);
    }
    replayStep();
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
