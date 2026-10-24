#include <unity.h>
#include <vector>
#include "fake_store.h"
#include "np_safety.h"
#include "np_clock.h"

namespace {

const char* STAMP = "2026-10-24T00:00:10.000000Z";

std::string request(const char* id = "a", const char* capability = "locker", int version = 1) {
    JsonDocument doc;
    doc["schema_version"] = 1;
    doc["node_id"] = "node";
    doc["check_id"] = id;
    doc["target_session_id"] = "s";
    doc["criteria_id"] = std::string(capability) + "-return";
    doc["criteria_version"] = version;
    doc["issued_at"] = "2026-10-24T00:00:00Z";
    doc["expires_at"] = "2026-10-24T00:00:30Z";
    std::string text;
    serializeJson(doc, text);
    return text;
}

class Port : public np::SafetyPort, public np::Sensors {
public:
    np::CommandContext ctx;
    uint64_t now = 100;
    int reads = 0, events = 0;
    bool failResult = false, corruptResult = false, expireDuringRead = false, loseClock = false;
    bool receiptPresent = false, finalPresent = false;
    FakeStore* store = nullptr;
    np::SensorSnapshot sample;
    std::vector<std::string> reports;
    Port() {
        ctx.nodeId = "node"; ctx.session = "s"; ctx.capability = "locker";
        ctx.connected = ctx.clockTrusted = true;
        np::parseUtc(STAMP, ctx.nowSec);
    }
    void values(bool door = true, bool locked = true) {
        sample.doorClosed = {true, door, now + 1, STAMP, "door-switch"};
        sample.locked = {true, locked, now + 1, STAMP, "lock-sensor"};
        sample.actuatorIdle = {true, true, now + 1, STAMP, "actuator-sensor"};
    }
    np::CommandContext context() override { return ctx; }
    uint64_t monotonicMs() override { return now; }
    std::string utcNow() override { return STAMP; }
    std::string newEventId() override { return "00000000-0000-4000-8000-" + std::string(11, '0') + std::to_string(++events); }
    np::Sensors& sensors() override { return *this; }
    bool sensesActuator() const override { return true; }
    bool detectsFaults() const override { return true; }
    np::SensorSnapshot read(const np::Measurement& measurement) override {
        ++reads;
        TEST_ASSERT_FALSE(measurement.checkId.empty());
        if (store) {
            for (const auto& slot : store->slots) {
                JsonDocument saved;
                deserializeJson(saved, slot.second);
                if (saved["check_id"].as<std::string>() == measurement.checkId && saved["state"] == "accepted")
                    receiptPresent = true;
            }
            store->failWrite = failResult;
            store->corruptWrite = corruptResult;
        }
        now += 2;
        if (expireDuringRead) ctx.nowSec += 30;
        if (loseClock) ctx.clockTrusted = false;
        return sample;
    }
    void publish(JsonDocument& record) override {
        if (store) for (const auto& slot : store->slots) {
            JsonDocument saved;
            deserializeJson(saved, slot.second);
            if (saved["check_id"] == record["check_id"] && saved["event_id"] == record["event_id"] && saved["state"] == "final")
                finalPresent = true;
        }
        std::string text;
        serializeJson(record, text);
        reports.push_back(text);
    }
    void log(const std::string&) override {}
};

void assertOutcome(const Port& port, const char* outcome, size_t evidenceCount) {
    TEST_ASSERT_FALSE(port.reports.empty());
    JsonDocument result;
    TEST_ASSERT_FALSE(deserializeJson(result, port.reports.back()));
    TEST_ASSERT_EQUAL_STRING(outcome, result["outcome"].as<const char*>());
    TEST_ASSERT_EQUAL_UINT32(evidenceCount, result["evidence"].size());
    TEST_ASSERT_FALSE(result["error_code"].is<const char*>());
}

void test_FR20_fresh_v1_evidence() {
    for (const char* capability : {"locker", "light", "speaker"}) {
        for (bool ready : {true, false}) {
            FakeStore store;
            np::Ledger ledger(store, "check_id");
            Port port;
            port.store = &store; port.ctx.capability = capability;
            port.values(ready, ready);
            port.sample.actuatorIdle.value = ready;
            np::SafetyChecks checks(ledger, port, 128, 1024);
            checks.handle(request("a", capability));
            assertOutcome(port, ready ? "passed" : "not_ready", std::string(capability) == "locker" ? 2 : 1);
            TEST_ASSERT_EQUAL_INT(1, port.reads);
            TEST_ASSERT_TRUE(port.receiptPresent);
            TEST_ASSERT_TRUE(port.finalPresent);
        }
    }
}

void test_FR20_duplicate_check_reuses_result() {
    FakeStore store;
    Port port; port.values();
    {
        np::Ledger ledger(store, "check_id");
        np::SafetyChecks checks(ledger, port, 128, 1024);
        checks.handle(request());
    }
    std::string original = port.reports.back();
    port.ctx.session = "new-session";
    port.ctx.nowSec += 60;  // Stored duplicate precedes expiry/session checks.
    np::Ledger rebooted(store, "check_id");
    np::SafetyChecks checks(rebooted, port, 128, 1024);
    checks.handle(request());
    checks.replay();
    TEST_ASSERT_EQUAL_STRING(original.c_str(), port.reports.back().c_str());
    TEST_ASSERT_EQUAL_INT(1, port.reads);
    TEST_ASSERT_EQUAL_UINT32(3, port.reports.size());
}

void test_FR20_check_id_conflict() {
    FakeStore store;
    Port port; port.values();
    np::Ledger ledger(store, "check_id");
    np::SafetyChecks checks(ledger, port, 128, 1024);
    checks.handle(request());
    std::string before = store.slots[0];
    checks.handle(request("a", "light"));
    checks.handle(request("a", "locker", 2));
    TEST_ASSERT_EQUAL_STRING(before.c_str(), store.slots[0].c_str());
    TEST_ASSERT_EQUAL_UINT32(1, port.reports.size());
    TEST_ASSERT_EQUAL_INT(1, port.reads);
}

void test_FR20_missing_evidence_unconfirmed() {
    for (int failure = 0; failure < 10; ++failure) {
        FakeStore store;
        Port port; port.values();
        if (failure == 0) port.sample.locked = {};  // Door is not lock evidence.
        if (failure == 1) port.sample.doorClosed.observedAtMs = 99;
        if (failure == 2) port.sample.locked.source.clear();
        if (failure == 3) port.sample.locked.observedAtMs = 200;
        if (failure == 4) port.sample.locked.observedAt = "2026-10-23T00:00:10Z";
        if (failure == 5) port.sample.locked.observedAt = "invalid";
        if (failure == 6) port.sample = {};
        if (failure == 7) port.sample.doorClosed.value = false, port.sample.locked = {};
        if (failure == 8) port.expireDuringRead = true;
        if (failure == 9) port.loseClock = true;
        np::Ledger ledger(store, "check_id");
        np::SafetyChecks checks(ledger, port, 128, 1024);
        checks.handle(request());
        assertOutcome(port, "unknown", failure >= 6 && failure != 7 ? 0 : 1);
    }
    for (const auto& payload : {request("a", "locker", 2), request("a", "light"), request("a", "unsupported")}) {
        FakeStore store;
        Port port; port.values();
        np::Ledger ledger(store, "check_id");
        np::SafetyChecks checks(ledger, port, 128, 1024);
        checks.handle(payload);
        assertOutcome(port, "unknown", 0);
        TEST_ASSERT_EQUAL_INT(0, port.reads);
    }
}

void test_FR07_verified_fault_report() {
    FakeStore store;
    Port port; port.values();
    port.sample.sensorFault = {true, true, 101, STAMP, "sensor-self-test"};
    np::Ledger ledger(store, "check_id");
    np::SafetyChecks checks(ledger, port, 128, 1024);
    checks.handle(request());
    assertOutcome(port, "fault", 2);
}

void test_FR07_check_storage_and_recovery() {
    for (int failure = 0; failure < 3; ++failure) {
        FakeStore store;
        Port port; port.values(); port.store = &store;
        store.failWrite = failure == 0;
        port.failResult = failure == 1;
        port.corruptResult = failure == 2;
        np::Ledger ledger(store, "check_id");
        np::SafetyChecks checks(ledger, port, 128, 1024);
        checks.handle(request());
        TEST_ASSERT_TRUE(port.reports.empty());
        TEST_ASSERT_EQUAL_INT(failure == 0 ? 0 : 1, port.reads);
        store.failWrite = store.corruptWrite = false;
        if (failure == 1) {
            np::Ledger rebooted(store, "check_id");
            Port next; next.store = &store; next.values();
            np::SafetyChecks recovered(rebooted, next, 128, 1024);
            recovered.replay();
            assertOutcome(next, "unknown", 0);
            recovered.handle(request());
            TEST_ASSERT_EQUAL_INT(0, next.reads);
            TEST_ASSERT_EQUAL_STRING(next.reports[0].c_str(), next.reports[1].c_str());
        }
    }
}

void test_FR20_invalid_check_no_measurement() {
    for (int invalid = 0; invalid < 12; ++invalid) {
        FakeStore store;
        Port port; port.values();
        JsonDocument doc;
        deserializeJson(doc, request());
        if (invalid == 0) doc["schema_version"] = 2;
        if (invalid == 1) doc["criteria_version"] = true;
        if (invalid == 2) doc["target_session_id"] = "old";
        if (invalid == 3) doc["node_id"] = "other";
        if (invalid == 4) doc["issued_at"] = "2026-02-30T00:00:00Z";
        if (invalid == 5) doc["expires_at"] = "2026-10-24T00:00:01Z";
        if (invalid == 6) doc["issued_at"] = "2026-10-24T00:00:29Z";
        if (invalid == 7) port.ctx.clockTrusted = false;
        if (invalid == 8) port.ctx.connected = false;
        if (invalid == 9) doc["check_id"] = "";
        if (invalid == 10) doc["criteria_id"] = std::string(2000, 'x');
        if (invalid == 11) doc["check_id"] = 12;
        std::string text;
        serializeJson(doc, text);
        np::Ledger ledger(store, "check_id");
        np::SafetyChecks checks(ledger, port, 128, 1024);
        checks.handle(text);
        TEST_ASSERT_EQUAL_INT(0, port.reads);
        TEST_ASSERT_TRUE(port.reports.empty());
        TEST_ASSERT_TRUE(store.slots.empty());
    }
}

void test_FR20_evicted_check_does_not_measure() {
    FakeStore store(1);
    Port port; port.values();
    np::Ledger ledger(store, "check_id");
    np::SafetyChecks checks(ledger, port, 128, 1024);
    checks.handle(request());
    port.ctx.nowSec += 60;
    JsonDocument fresh;
    deserializeJson(fresh, request("b"));
    fresh["issued_at"] = "2026-10-24T00:01:00Z";
    fresh["expires_at"] = "2026-10-24T00:01:30Z";
    std::string payload;
    serializeJson(fresh, payload);
    checks.handle(payload);
    TEST_ASSERT_EQUAL_INT(2, port.reads);
    std::string saved = store.slots[0];
    checks.handle(request());
    TEST_ASSERT_EQUAL_INT(2, port.reads);
    TEST_ASSERT_EQUAL_STRING(saved.c_str(), store.slots[0].c_str());
    TEST_ASSERT_EQUAL_UINT32(2, port.reports.size());
}

void test_FR07_status_uses_sensor_observations() {
    np::NoSensors none;
    JsonDocument doc;
    double utc;
    np::parseUtc(STAMP, utc);
    np::physicalState(doc, none.read({"", 100}), "locker", 100, 102, utc, utc);
    TEST_ASSERT_TRUE(doc["physical_state"]["door_closed"].isNull());
    TEST_ASSERT_TRUE(doc["physical_state"]["sensor_fault"].isNull());
    TEST_ASSERT_EQUAL_STRING("unknown", doc["physical_state"]["actuator_state"]);
    Port port; port.values();
    port.sample.lightOn = {true, false, 101, STAMP, "photodiode"};
    port.sample.sensorFault = {true, false, 101, STAMP, "self-test"};
    np::physicalState(doc, port.sample, "locker", 100, 102, utc, utc);
    TEST_ASSERT_TRUE(doc["physical_state"]["door_closed"].as<bool>());
    TEST_ASSERT_TRUE(doc["physical_state"]["light_on"].isNull());
    TEST_ASSERT_EQUAL_STRING("idle", doc["physical_state"]["actuator_state"]);
    TEST_ASSERT_FALSE(doc["physical_state"]["sensor_fault"].as<bool>());
    np::physicalState(doc, port.sample, "light", 100, 102, utc, utc);
    TEST_ASSERT_FALSE(doc["physical_state"]["light_on"].as<bool>());
    TEST_ASSERT_TRUE(doc["physical_state"]["door_closed"].isNull());
    np::physicalState(doc, port.sample, "light", 103, 104, utc, utc);
    TEST_ASSERT_TRUE(doc["physical_state"]["light_on"].isNull());
    TEST_ASSERT_EQUAL_STRING("unknown", doc["physical_state"]["actuator_state"]);
}

void test_FR07_status_without_sensors_uses_command_state() {
    np::NoSensors none;
    JsonDocument doc;
    double utc;
    np::parseUtc(STAMP, utc);
    np::StatusFallback board(none.sensesActuator(), none.detectsFaults(), false);
    np::physicalState(doc, none.read({"", 100}), "light", 100, 102, utc, utc, board);
    TEST_ASSERT_EQUAL_STRING("idle", doc["physical_state"]["actuator_state"]);
    TEST_ASSERT_FALSE(doc["physical_state"]["sensor_fault"].as<bool>());
    TEST_ASSERT_TRUE(doc["physical_state"]["sensor_fault"].is<bool>());
    TEST_ASSERT_TRUE(doc["physical_state"]["light_on"].isNull());  // Never filled from command state.
    board.commandActive = true;
    np::physicalState(doc, none.read({"", 100}), "light", 100, 102, utc, utc, board);
    TEST_ASSERT_EQUAL_STRING("busy", doc["physical_state"]["actuator_state"]);

    // Installed sensors win; a failed read stays unknown/null instead of the command state.
    Port port;
    np::StatusFallback sensed(port.sensesActuator(), port.detectsFaults(), false);
    np::physicalState(doc, np::SensorSnapshot(), "light", 100, 102, utc, utc, sensed);
    TEST_ASSERT_EQUAL_STRING("unknown", doc["physical_state"]["actuator_state"]);
    TEST_ASSERT_TRUE(doc["physical_state"]["sensor_fault"].isNull());
    port.values();
    port.sample.actuatorIdle = {true, false, 101, STAMP, "current-sensor"};
    port.sample.sensorFault = {true, true, 101, STAMP, "self-test"};
    np::physicalState(doc, port.sample, "light", 100, 102, utc, utc, board);
    TEST_ASSERT_EQUAL_STRING("busy", doc["physical_state"]["actuator_state"]);
    TEST_ASSERT_TRUE(doc["physical_state"]["sensor_fault"].as<bool>());
}

void test_FR20_full_check_store_preserves_unexpired_and_pending() {
    for (bool interrupted : {false, true}) {
        FakeStore store(1);
        Port port; port.values(); port.store = &store;
        port.failResult = interrupted;
        np::Ledger ledger(store, "check_id");
        np::SafetyChecks checks(ledger, port, 128, 1024);
        checks.handle(request());
        store.failWrite = false;
        port.failResult = false;
        if (interrupted) port.ctx.nowSec += 60;
        std::string before = store.slots[0];
        JsonDocument next;
        deserializeJson(next, request("b"));
        if (interrupted) {
            next["issued_at"] = "2026-10-24T00:01:00Z";
            next["expires_at"] = "2026-10-24T00:01:30Z";
        }
        std::string payload;
        serializeJson(next, payload);
        checks.handle(payload);
        TEST_ASSERT_EQUAL_STRING(before.c_str(), store.slots[0].c_str());
        TEST_ASSERT_EQUAL_INT(1, port.reads);
        TEST_ASSERT_EQUAL_UINT32(interrupted ? 0 : 1, port.reports.size());
    }
}

void test_FR22_check_owner_and_corruption_block_measurement() {
    FakeStore store;
    Port port; port.values();
    np::Ledger ledger(store, "check_id");
    np::SafetyChecks checks(ledger, port, 128, 1024);
    checks.handle(request());
    TEST_ASSERT_NULL(ledger.ownerConflict("node"));
    TEST_ASSERT_EQUAL_STRING("node_id_change_with_records", ledger.ownerConflict("other"));
    store.slots[1] = "broken";
    np::Ledger rebooted(store, "check_id");
    np::SafetyChecks next(rebooted, port, 128, 1024);
    next.handle(request("b"));
    TEST_ASSERT_EQUAL_INT(1, port.reads);
    TEST_ASSERT_EQUAL_UINT32(1, port.reports.size());
}

}  // namespace

void runSafetyTests() {
    RUN_TEST(test_FR20_fresh_v1_evidence);
    RUN_TEST(test_FR20_duplicate_check_reuses_result);
    RUN_TEST(test_FR20_check_id_conflict);
    RUN_TEST(test_FR20_missing_evidence_unconfirmed);
    RUN_TEST(test_FR07_verified_fault_report);
    RUN_TEST(test_FR07_check_storage_and_recovery);
    RUN_TEST(test_FR20_invalid_check_no_measurement);
    RUN_TEST(test_FR20_evicted_check_does_not_measure);
    RUN_TEST(test_FR07_status_uses_sensor_observations);
    RUN_TEST(test_FR07_status_without_sensors_uses_command_state);
    RUN_TEST(test_FR20_full_check_store_preserves_unexpired_and_pending);
    RUN_TEST(test_FR22_check_owner_and_corruption_block_measurement);
}
