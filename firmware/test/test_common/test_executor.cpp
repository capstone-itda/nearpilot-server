#include <unity.h>

#include <deque>
#include <functional>
#include <string>
#include <vector>

#include "fake_store.h"
#include "np_command.h"
#include "np_executor.h"
#include "np_clock.h"
#include "np_occupancy.h"

namespace {

class FakePort : public np::CommandPort, public np::Actuator {
public:
    std::deque<const char*> checks;  // Scripted validate() results; empty means valid.
    std::vector<std::string> published;  // "state" or "state:error_code" per result.
    std::vector<std::string> events;
    std::vector<std::string> logs;
    int actuations = 0;
    int counter = 0;
    np::OutputResult outputResult = np::OutputResult::Completed;
    std::function<void()> duringOutput;  // Runs inside output(), while the command is being handled.
    FakeStore* checkStore = nullptr;
    bool successWasStored = false;
    bool liveValidation = false;
    np::CommandContext live;
    np::CommandContext retentionContext() override { return live; }

    np::Actuator& actuator() override { return *this; }
    np::OutputResult output(const np::Actuation&) override {
        ++actuations;
        if (duringOutput) duringOutput();
        return outputResult;
    }

    const char* validate(JsonDocument& command) override {
        if (liveValidation) return np::validateCommand(command, live);
        if (checks.empty()) return nullptr;
        const char* code = checks.front();
        checks.pop_front();
        return code;
    }
    std::string newEventId() override { return "e" + std::to_string(++counter); }
    std::string utcNow() override { return liveValidation ? "2026-10-23T00:00:10Z" : "t" + std::to_string(counter); }
    void publish(JsonDocument& record) override {
        std::string state = record["state"].as<std::string>();
        if (record["error_code"].is<const char*>()) state += ":" + record["error_code"].as<std::string>();
        if (state == "succeeded" && checkStore) {
            for (const auto& slot : checkStore->slots) {
                JsonDocument saved;
                deserializeJson(saved, slot.second);
                if (saved["command_id"] == record["command_id"] && saved["state"] == "succeeded")
                    successWasStored = saved["event_id"] == record["event_id"];
            }
        }
        published.push_back(state);
        events.push_back(record["event_id"].as<std::string>() + "@" + record["occurred_at"].as<std::string>());
    }
    void log(const std::string& line) override { logs.push_back(line); }
};

// The receipt write succeeds; every result write fails, or reads back wrong when corrupt.
struct ResultFails : FakeStore {
    bool corrupt = false;
    bool write(int slot, const std::string& body) override {
        if (body.find("\"accepted\"") != std::string::npos) return FakeStore::write(slot, body);
        if (corrupt) slots[slot] = body.substr(1);
        return corrupt;
    }
};

std::string command(const char* id, const char* action = "on") {
    return std::string("{\"schema_version\":2,\"command_id\":\"") + id + "\",\"node_id\":\"light-01\",\"action\":\"" +
           action + "\",\"target_session_id\":\"s\",\"issued_at\":\"2026-10-23T00:00:00Z\","
           "\"expires_at\":\"2026-10-23T00:00:30Z\"}";
}

std::string stored(FakeStore& store, int slot) {
    JsonDocument doc;
    deserializeJson(doc, store.slots[slot]);
    return doc["state"].as<std::string>();
}

void assertPublished(const std::vector<std::string>& expected, const FakePort& port) {
    TEST_ASSERT_EQUAL_UINT32(expected.size(), port.published.size());
    for (size_t i = 0; i < expected.size(); ++i)
        TEST_ASSERT_EQUAL_STRING(expected[i].c_str(), port.published[i].c_str());
}

void test_FR06_accepted_running_then_succeeded() {
    FakeStore store;
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    assertPublished({"accepted", "running", "succeeded"}, port);
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
    TEST_ASSERT_EQUAL_STRING("succeeded", stored(store, 0).c_str());
    TEST_ASSERT_FALSE(executor.busy());
}

void test_FR06_invalid_message_no_actuation() {
    FakeStore store;
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 4, 512);
    executor.handle("not json", false);
    executor.handle("{\"command_id\":7}", false);
    executor.handle(command("too-long"), false);  // Longer than the 4-byte ID limit.
    TEST_ASSERT_EQUAL_UINT32(0, port.published.size());
    TEST_ASSERT_EQUAL_UINT32(0, store.slots.size());
    port.checks = {"INVALID_MESSAGE"};
    executor.handle(command("b"), false);
    assertPublished({"rejected:INVALID_MESSAGE"}, port);
    TEST_ASSERT_EQUAL_STRING("rejected", stored(store, 0).c_str());  // Kept for retransmissions.
    executor.handle(command("b"), false);
    assertPublished({"rejected:INVALID_MESSAGE", "rejected:INVALID_MESSAGE"}, port);
    TEST_ASSERT_EQUAL_INT(0, port.actuations);
    FakeStore small;
    np::Ledger smallLedger(small);
    np::Executor bounded(smallLedger, port, 128, 100);  // Too small for any record.
    bounded.handle(command("c"), false);
    TEST_ASSERT_EQUAL_STRING("rejected:INVALID_MESSAGE", port.published.back().c_str());
    TEST_ASSERT_EQUAL_STRING("RECORD_TOO_LARGE not_recorded", port.logs.back().c_str());
    TEST_ASSERT_EQUAL_UINT32(0, small.slots.size());
    TEST_ASSERT_EQUAL_INT(0, port.actuations);
}

void test_FR06_duplicate_after_restart() {
    FakeStore store;
    {
        FakePort port;
        np::Ledger ledger(store);
        np::Executor executor(ledger, port, 128, 512);
        executor.handle(command("a"), false);
    }
    FakePort port;
    np::Ledger ledger(store);  // New instances stand in for a reboot.
    np::Executor executor(ledger, port, 128, 512);
    port.checks = {"COMMAND_EXPIRED"};  // Lookup precedes expiry: this is never consulted.
    executor.handle(command("a"), false);
    assertPublished({"succeeded"}, port);
    TEST_ASSERT_EQUAL_INT(0, port.actuations);
    TEST_ASSERT_EQUAL_UINT32(1, port.checks.size());
}

void test_FR22_changed_capability_preserves_duplicate() {
    class ValidatingPort : public FakePort {
    public:
        np::CommandContext context;
        const char* validate(JsonDocument& doc) override { return np::validateCommand(doc, context); }
    } port;
    port.context.nodeId = "light-01";
    port.context.session = "s";
    port.context.capability = "light";
    port.context.connected = port.context.clockTrusted = true;
    port.context.nowSec = 1792713610.0;
    FakeStore store;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    const std::string original = store.slots[0];
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
    port.context.capability = "locker";
    TEST_ASSERT_NULL(ledger.ownerConflict("light-01"));
    executor.handle(command("a"), false);
    TEST_ASSERT_EQUAL_STRING("succeeded", port.published.back().c_str());
    TEST_ASSERT_EQUAL_STRING(original.c_str(), store.slots[0].c_str());
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
    executor.handle(command("b"), false);
    TEST_ASSERT_EQUAL_STRING("rejected:UNSUPPORTED_ACTION", port.published.back().c_str());
    executor.handle(command("c", "open"), false);
    TEST_ASSERT_EQUAL_STRING("succeeded", port.published.back().c_str());
    TEST_ASSERT_EQUAL_INT(2, port.actuations);
}

void test_FR06_command_id_conflict() {
    FakeStore store;
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    std::string original = store.slots[0];
    executor.handle(command("a", "off"), false);
    TEST_ASSERT_EQUAL_STRING("rejected:COMMAND_ID_CONFLICT", port.published.back().c_str());
    TEST_ASSERT_EQUAL_STRING(original.c_str(), store.slots[0].c_str());
    TEST_ASSERT_EQUAL_UINT32(1, store.slots.size());
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
}

void test_FR06_concurrent_commands_busy() {
    FakeStore store;
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    bool busyDuringOutput = false;
    port.duringOutput = [&]() {
        busyDuringOutput = executor.busy();
        port.duringOutput = nullptr;
        executor.handle(command("b"), false);  // A second command reaching the executor mid-output.
    };
    executor.handle(command("a"), false);
    TEST_ASSERT_TRUE(busyDuringOutput);
    assertPublished({"accepted", "running", "rejected:DEVICE_BUSY", "succeeded"}, port);
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
    TEST_ASSERT_EQUAL_STRING("rejected", stored(store, 1).c_str());
    TEST_ASSERT_FALSE(executor.busy());
    executor.handle(command("b"), false);  // The busy rejection survives completion.
    TEST_ASSERT_EQUAL_STRING("rejected:DEVICE_BUSY", port.published.back().c_str());
    executor.handle(command("c"), true);  // Arrived busy, processed after completion.
    TEST_ASSERT_EQUAL_STRING("rejected:DEVICE_BUSY", port.published.back().c_str());
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
}

void test_FR06_receipt_storage_failure() {
    FakeStore store;
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    store.failWrite = true;
    executor.handle(command("a"), false);
    store.failWrite = false;
    store.corruptWrite = true;  // Read-back mismatch counts as a failed write.
    executor.handle(command("b"), false);
    store.corruptWrite = false;
    store.slots.clear();
    store.slots[2] = "not json";  // Unreadable: the command might be a duplicate.
    executor.handle(command("c"), false);
    store.available = false;
    executor.handle(command("d"), false);
    assertPublished({"rejected:STORAGE_ERROR", "rejected:STORAGE_ERROR", "rejected:STORAGE_ERROR",
                     "rejected:STORAGE_ERROR"}, port);
    TEST_ASSERT_EQUAL_INT(0, port.actuations);
    TEST_ASSERT_EQUAL_STRING("not json", store.slots[2].c_str());
}

void test_FR06_session_and_expiry_rechecked() {
    FakeStore store;
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    port.checks = {nullptr, "SESSION_MISMATCH"};  // Fails after the receipt write.
    executor.handle(command("a"), false);
    port.checks = {nullptr, "COMMAND_EXPIRED"};  // Fails right before output.
    executor.handle(command("b"), false);
    assertPublished({"accepted", "failed:SESSION_MISMATCH", "accepted", "failed:COMMAND_EXPIRED"}, port);
    TEST_ASSERT_EQUAL_INT(0, port.actuations);
    TEST_ASSERT_EQUAL_STRING("failed", stored(store, 0).c_str());
    TEST_ASSERT_EQUAL_STRING("failed", stored(store, 1).c_str());
}

void test_FR07_power_loss_restores_unknown() {
    FakeStore store;
    store.slots[0] = "{\"command_id\":\"a\",\"content\":\"[]\",\"state\":\"accepted\",\"event_id\":\"old\","
                     "\"occurred_at\":\"t0\"}";
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.replay();
    assertPublished({"unknown"}, port);
    TEST_ASSERT_EQUAL_STRING("unknown", stored(store, 0).c_str());
    TEST_ASSERT_EQUAL_INT(0, port.actuations);
    store.failWrite = true;  // The receipt stays and is reported once storage works again.
    store.slots[1] = "{\"command_id\":\"b\",\"content\":\"[]\",\"state\":\"accepted\"}";
    executor.replay();
    assertPublished({"unknown", "unknown"}, port);
    TEST_ASSERT_EQUAL_STRING("accepted", stored(store, 1).c_str());
}

void test_FR07_output_completion_saved_before_success() {
    FakeStore store;
    FakePort port;
    port.checkStore = &store;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    assertPublished({"accepted", "running", "succeeded"}, port);
    TEST_ASSERT_TRUE(port.successWasStored);
    std::string resultEvent = port.events.back();
    executor.handle(command("a"), false);
    TEST_ASSERT_EQUAL_STRING(resultEvent.c_str(), port.events.back().c_str());
    TEST_ASSERT_EQUAL_INT(1, port.actuations);

    for (bool readBackFails : {false, true}) {
        // A missing write and a failed verification must both suppress success.
        ResultFails failing;
        failing.corrupt = readBackFails;
        FakePort next;
        np::Ledger nextLedger(failing);
        np::Executor second(nextLedger, next, 128, 512);
        second.handle(command("b"), false);
        assertPublished({"accepted", "running"}, next);
        TEST_ASSERT_FALSE(second.busy());
        TEST_ASSERT_EQUAL_STRING("RESULT_STORE_FAILED accepted_record_kept_no_retry", next.logs.back().c_str());
        TEST_ASSERT_EQUAL_INT(1, next.actuations);
        if (readBackFails) continue;
        TEST_ASSERT_EQUAL_STRING("accepted", stored(failing, 0).c_str());
        FakeStore recovered;  // Storage works again after a restart.
        recovered.slots = failing.slots;
        FakePort restarted;
        np::Ledger restartedLedger(recovered);
        np::Executor third(restartedLedger, restarted, 128, 512);
        third.replay();
        assertPublished({"unknown"}, restarted);
        TEST_ASSERT_EQUAL_INT(0, restarted.actuations);
    }
}

void test_FR07_result_event_identity() {
    FakeStore store;
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    std::string first = port.events.back();
    executor.replay();
    executor.handle(command("a"), false);
    TEST_ASSERT_EQUAL_STRING(first.c_str(), port.events[3].c_str());
    TEST_ASSERT_EQUAL_STRING(first.c_str(), port.events[4].c_str());
    TEST_ASSERT_EQUAL_STRING("succeeded", port.published[4].c_str());
}

void test_FR22_full_ledger_rejects_without_eviction() {
    FakeStore store(2);
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    executor.handle(command("b"), false);
    std::string first = store.slots[0];
    executor.handle(command("c"), false);  // No empty slot.
    TEST_ASSERT_EQUAL_STRING("rejected:STORAGE_ERROR", port.published.back().c_str());
    TEST_ASSERT_EQUAL_STRING(first.c_str(), store.slots[0].c_str());
    TEST_ASSERT_EQUAL_UINT32(2, store.slots.size());
}

void test_FR22_legacy_records_block_reexecution() {
    FakeStore primary(2), legacy(2);
    legacy.slots[1] = "{\"command_id\":\"old\",\"content\":"
                      "\"[\\\"light-01\\\",\\\"on\\\",\\\"s\\\",\\\"2026-10-23T00:00:00Z\\\",\\\"2026-10-23T00:00:30Z\\\"]\","
                      "\"state\":\"accepted\",\"event_id\":\"x\",\"occurred_at\":\"t0\"}";
    np::LayeredRecordStore layered(primary, legacy);
    FakePort port;
    np::Ledger ledger(layered);
    np::Executor executor(ledger, port, 128, 512);
    TEST_ASSERT_EQUAL_STRING("node_id_change_with_records", ledger.ownerConflict("light-02"));
    executor.replay();  // The interrupted legacy receipt is updated in place.
    TEST_ASSERT_EQUAL_STRING("unknown", stored(legacy, 1).c_str());
    executor.handle(command("old"), false);
    TEST_ASSERT_EQUAL_STRING("unknown", port.published.back().c_str());
    TEST_ASSERT_EQUAL_INT(0, port.actuations);
    executor.handle(command("n1"), false);
    executor.handle(command("n2"), false);
    executor.handle(command("n3"), false);  // Primary is full; legacy empty slots are never used.
    TEST_ASSERT_EQUAL_STRING("rejected:STORAGE_ERROR", port.published.back().c_str());
    TEST_ASSERT_EQUAL_UINT32(2, primary.slots.size());
    TEST_ASSERT_EQUAL_UINT32(1, legacy.slots.size());
    legacy.available = false;
    executor.handle(command("n4"), false);
    TEST_ASSERT_EQUAL_STRING("rejected:STORAGE_ERROR", port.published.back().c_str());
}


void test_FR07_verified_execution_failure() {
    FakeStore store;
    FakePort port;
    port.outputResult = np::OutputResult::Failed;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    assertPublished({"accepted", "running", "failed"}, port);
    TEST_ASSERT_EQUAL_STRING("failed", stored(store, 0).c_str());
    TEST_ASSERT_FALSE(executor.busy());
    executor.handle(command("a"), false);
    TEST_ASSERT_EQUAL_STRING("failed", port.published.back().c_str());
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
}

void test_FR07_unconfirmed_output_is_unknown() {
    FakeStore store;
    FakePort port;
    port.outputResult = np::OutputResult::Unknown;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    assertPublished({"accepted", "running", "unknown"}, port);
    TEST_ASSERT_EQUAL_STRING("unknown", stored(store, 0).c_str());
    TEST_ASSERT_FALSE(executor.busy());
    executor.handle(command("b"), false);
    TEST_ASSERT_EQUAL_INT(2, port.actuations);  // Busy was released.
}

void test_FR07_status_reports_command_state() {
    FakeStore store;
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    TEST_ASSERT_EQUAL_STRING("idle", executor.commandState());
    std::string during;
    port.duringOutput = [&]() { during = executor.commandState(); };
    executor.handle(command("a"), false);
    TEST_ASSERT_EQUAL_STRING("busy", during.c_str());
    TEST_ASSERT_EQUAL_STRING("idle", executor.commandState());
    store.available = false;  // The records cannot vouch for what was handled.
    TEST_ASSERT_EQUAL_STRING("unknown", executor.commandState());
}

void test_NFR03_at_most_once_actuation() {
    FakeStore store;
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    std::string duringReconnect;
    port.duringOutput = [&]() {
        port.duringOutput = nullptr;
        executor.replay();  // MQTT reconnect while this command is being handled.
        duringReconnect = stored(store, 0);
    };
    executor.handle(command("a"), false);
    TEST_ASSERT_EQUAL_STRING("accepted", duringReconnect.c_str());  // Not mistaken for a restored receipt.
    executor.handle(command("a"), false);
    executor.replay();
    TEST_ASSERT_EQUAL_STRING("succeeded", port.published.back().c_str());
    TEST_ASSERT_EQUAL_INT(1, port.actuations);

    ResultFails rebootStore;  // Power loss before the result: only the receipt is persisted.
    {
        FakePort oldPort;
        np::Ledger oldLedger(rebootStore);
        np::Executor oldExecutor(oldLedger, oldPort, 128, 512);
        oldExecutor.handle(command("a"), false);
        TEST_ASSERT_EQUAL_INT(1, oldPort.actuations);
    }
    FakeStore restoredStore;
    restoredStore.slots = rebootStore.slots;
    FakePort restored;
    np::Ledger restoredLedger(restoredStore);
    np::Executor restoredExecutor(restoredLedger, restored, 128, 512);
    restoredExecutor.replay();
    restoredExecutor.handle(command("a"), false);
    assertPublished({"unknown", "unknown"}, restored);
    TEST_ASSERT_EQUAL_INT(0, restored.actuations);
}

int signalCount = 0;
void countSignal(const np::Actuation&) { ++signalCount; }

void test_NFR10_signal_output_completes() {
    signalCount = 0;
    np::SignalActuator signal(countSignal);
    for (const char* action : {"open", "on", "off"})
        TEST_ASSERT_TRUE(signal.output({"a", action}) == np::OutputResult::Completed);
    TEST_ASSERT_EQUAL_INT(3, signalCount);
    np::SignalActuator missing(nullptr);
    TEST_ASSERT_TRUE(missing.output({"a", "on"}) == np::OutputResult::Unknown);
}

void test_NFR10_servo_pulse_for_angle() {
    TEST_ASSERT_EQUAL_UINT(500, np::servoPulseUs(0, 500, 2400));
    TEST_ASSERT_EQUAL_UINT(1450, np::servoPulseUs(90, 500, 2400));
    TEST_ASSERT_EQUAL_UINT(2400, np::servoPulseUs(180, 500, 2400));
    TEST_ASSERT_EQUAL_UINT(500, np::servoPulseUs(-10, 500, 2400));
    TEST_ASSERT_EQUAL_UINT(2400, np::servoPulseUs(270, 500, 2400));
    // 50 Hz, 16 bits: 1,450 us is 7.25% of the 20 ms period.
    TEST_ASSERT_EQUAL_UINT32(4751, np::servoDuty(1450, 50, 16));
    TEST_ASSERT_EQUAL_UINT32(65535, np::servoDuty(20000, 50, 16));
}

void test_FR06_servo_relocks_once_after_hold() {
    np::RelockTimer relock(3000);
    TEST_ASSERT_FALSE(relock.due(0));
    relock.opened(1000);
    TEST_ASSERT_TRUE(relock.pending());
    TEST_ASSERT_FALSE(relock.due(3999));
    TEST_ASSERT_TRUE(relock.due(4000));
    TEST_ASSERT_FALSE(relock.due(9000));
    // A new open restarts the hold.
    relock.opened(5000);
    TEST_ASSERT_FALSE(relock.due(7000));
    relock.opened(7000);
    TEST_ASSERT_FALSE(relock.due(9999));
    TEST_ASSERT_TRUE(relock.due(10000));
    // millis() wraps after about 49 days.
    relock.opened(0xFFFFFF00UL);
    TEST_ASSERT_FALSE(relock.due(0x00000100UL));
    TEST_ASSERT_TRUE(relock.due(0xFFFFFF00UL + 3000));
}

std::string occupancyBody(const char* state, const char* node = "locker-1", int version = 2) {
    return std::string("{\"schema_version\":") + std::to_string(version) + ",\"node_id\":\"" + node
        + "\",\"device_state\":\"" + state + "\",\"sent_at\":\"2026-10-27T07:00:00Z\"}";
}

void test_FR19_occupancy_display_colors() {
    using np::OccupancyDisplay;
    TEST_ASSERT_TRUE(np::decodeOccupancy(occupancyBody("AVAILABLE").c_str(), "locker-1") == OccupancyDisplay::Free);
    for (const char* held : {"RESERVED", "ACTIVE", "CLOSING", "FAULT"})
        TEST_ASSERT_TRUE(np::decodeOccupancy(occupancyBody(held).c_str(), "locker-1") == OccupancyDisplay::Occupied);
    TEST_ASSERT_EQUAL_HEX32(0x000040, np::occupancyRgb(OccupancyDisplay::Free, 64));
    TEST_ASSERT_EQUAL_HEX32(0x400000, np::occupancyRgb(OccupancyDisplay::Occupied, 64));
    TEST_ASSERT_EQUAL_HEX32(0, np::occupancyRgb(OccupancyDisplay::Invalid, 64));
}

void test_FR19_occupancy_display_ignores_invalid_messages() {
    using np::OccupancyDisplay;
    const std::string invalid[] = {occupancyBody("ACTIVE", "locker-2"), occupancyBody("ACTIVE", "locker-1", 1),
        occupancyBody("OPEN"), "{\"schema_version\":2,\"node_id\":\"locker-1\"}", "not json", "[]"};
    for (const std::string& body : invalid)
        TEST_ASSERT_TRUE(np::decodeOccupancy(body.c_str(), "locker-1") == OccupancyDisplay::Invalid);
}

void test_FR06_evicted_command_does_not_actuate() {
    FakeStore store(1);
    FakePort port;
    port.liveValidation = true;
    port.live.nodeId = "light-01"; port.live.session = "s"; port.live.capability = "light";
    port.live.clockTrusted = port.live.connected = true;
    np::parseUtc("2026-10-23T00:00:10Z", port.live.nowSec);
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
    port.live.nowSec += 60;
    JsonDocument next;
    deserializeJson(next, command("b"));
    next["issued_at"] = "2026-10-23T00:01:00Z";
    next["expires_at"] = "2026-10-23T00:01:30Z";
    std::string payload;
    serializeJson(next, payload);
    executor.handle(payload, false);
    TEST_ASSERT_EQUAL_INT(2, port.actuations);
    std::string saved = store.slots[0];
    executor.handle(command("a"), false);
    TEST_ASSERT_EQUAL_STRING("rejected:COMMAND_EXPIRED", port.published.back().c_str());
    TEST_ASSERT_EQUAL_STRING(saved.c_str(), store.slots[0].c_str());
    TEST_ASSERT_EQUAL_INT(2, port.actuations);
    // The replacement's own duplicate retains the final event without any output.
    std::string event = port.events[port.events.size() - 2];
    executor.handle(payload, false);
    TEST_ASSERT_EQUAL_STRING(event.c_str(), port.events.back().c_str());
    TEST_ASSERT_EQUAL_INT(2, port.actuations);
}

void test_FR06_busy_rejection_replaces_expired_record() {
    FakeStore store(1);
    FakePort port;
    port.liveValidation = true;
    port.live.nodeId = "light-01"; port.live.session = "s"; port.live.capability = "light";
    port.live.clockTrusted = port.live.connected = true;
    np::parseUtc("2026-10-23T00:00:10Z", port.live.nowSec);
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    port.live.nowSec += 60;
    JsonDocument next;
    deserializeJson(next, command("b"));
    next["issued_at"] = "2026-10-23T00:01:00Z";
    next["expires_at"] = "2026-10-23T00:01:30Z";
    std::string payload;
    serializeJson(next, payload);
    executor.handle(payload, true);
    TEST_ASSERT_EQUAL_STRING("rejected:DEVICE_BUSY", port.published.back().c_str());
    executor.handle(payload, false);  // No longer busy, but the rejection is durable.
    TEST_ASSERT_EQUAL_STRING("rejected:DEVICE_BUSY", port.published.back().c_str());
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
}

void test_FR07_replay_spreads_over_loop_iterations() {
    FakeStore store(5);
    store.slots[0] = "{\"command_id\":\"a\",\"content\":\"[]\",\"state\":\"unknown\",\"event_id\":\"x\",\"occurred_at\":\"t0\"}";
    store.slots[2] = "not json";  // Skipped and kept.
    store.slots[3] = "{\"command_id\":\"b\",\"content\":\"[]\",\"state\":\"accepted\",\"event_id\":\"y\",\"occurred_at\":\"t0\"}";
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    TEST_ASSERT_FALSE(executor.replayNext());  // Nothing until a replay starts.
    executor.beginReplay();
    TEST_ASSERT_TRUE(executor.replayNext());
    assertPublished({"unknown"}, port);
    TEST_ASSERT_TRUE(executor.replayNext());  // One record per call: the restored receipt.
    assertPublished({"unknown", "unknown"}, port);
    TEST_ASSERT_EQUAL_STRING("unknown", stored(store, 3).c_str());
    TEST_ASSERT_FALSE(executor.replayNext());
    TEST_ASSERT_FALSE(executor.replayNext());
    TEST_ASSERT_EQUAL_UINT32(2, port.published.size());
    TEST_ASSERT_EQUAL_STRING("not json", store.slots[2].c_str());
    executor.beginReplay();  // A new connection starts again from the first slot.
    TEST_ASSERT_TRUE(executor.replayNext());
    TEST_ASSERT_EQUAL_STRING("x@t0", port.events.back().c_str());
    TEST_ASSERT_EQUAL_INT(0, port.actuations);
}

}  // namespace

void runExecutorTests() {
    RUN_TEST(test_FR07_replay_spreads_over_loop_iterations);
    RUN_TEST(test_FR06_accepted_running_then_succeeded);
    RUN_TEST(test_FR06_invalid_message_no_actuation);
    RUN_TEST(test_FR06_duplicate_after_restart);
    RUN_TEST(test_FR22_changed_capability_preserves_duplicate);
    RUN_TEST(test_FR06_command_id_conflict);
    RUN_TEST(test_FR06_concurrent_commands_busy);
    RUN_TEST(test_FR06_receipt_storage_failure);
    RUN_TEST(test_FR06_session_and_expiry_rechecked);
    RUN_TEST(test_FR07_power_loss_restores_unknown);
    RUN_TEST(test_FR07_output_completion_saved_before_success);
    RUN_TEST(test_FR07_result_event_identity);
    RUN_TEST(test_FR22_full_ledger_rejects_without_eviction);
    RUN_TEST(test_FR22_legacy_records_block_reexecution);
    RUN_TEST(test_FR07_verified_execution_failure);
    RUN_TEST(test_FR07_unconfirmed_output_is_unknown);
    RUN_TEST(test_FR07_status_reports_command_state);
    RUN_TEST(test_NFR03_at_most_once_actuation);
    RUN_TEST(test_NFR10_signal_output_completes);
    RUN_TEST(test_NFR10_servo_pulse_for_angle);
    RUN_TEST(test_FR06_servo_relocks_once_after_hold);
    RUN_TEST(test_FR19_occupancy_display_colors);
    RUN_TEST(test_FR19_occupancy_display_ignores_invalid_messages);
    RUN_TEST(test_FR06_evicted_command_does_not_actuate);
    RUN_TEST(test_FR06_busy_rejection_replaces_expired_record);
}
