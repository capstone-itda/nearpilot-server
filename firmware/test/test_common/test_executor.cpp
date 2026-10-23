#include <unity.h>

#include <deque>
#include <string>
#include <vector>

#include "fake_store.h"
#include "np_command.h"
#include "np_executor.h"

namespace {

class FakePort : public np::CommandPort, public np::Actuator, public np::CompletionObserver {
public:
    std::deque<const char*> checks;  // Scripted validate() results; empty means valid.
    std::vector<std::string> published;  // "state" or "state:error_code" per result.
    std::vector<std::string> events;
    std::vector<std::string> logs;
    int actuations = 0;
    int counter = 0;
    uint64_t now = 100;
    uint64_t observeDelayMs = 0;
    bool hasObservation = false;
    np::StartResult startResult = np::StartResult::Started;
    np::CompletionObservation observation;
    np::Device attached{*this, *this, {false, 0}};
    FakeStore* checkStore = nullptr;
    bool successWasStored = false;
    int observations = 0;

    np::Device& device() override { return attached; }
    uint64_t monotonicMs() override { return now; }
    bool available() const override { return hasObservation; }
    np::CompletionObservation observe(const np::Actuation&) override {
        ++observations;
        now += observeDelayMs;
        return observation;
    }
    np::StartResult start(const np::Actuation&) override {
        ++actuations;
        return startResult;
    }
    void enableObservation(bool successEnabled = true) {
        hasObservation = true;
        attached.policy = {successEnabled, 50};  // Synthetic test duration, not a device default.
        observation.state = np::ObservationState::Pending;
    }
    void evidence(np::ObservationState state, const char* id = "a") {
        now = 101;
        observation = {state, id, now, "fake_action_sensor"};
    }

    const char* validate(JsonDocument&) override {
        if (checks.empty()) return nullptr;
        const char* code = checks.front();
        checks.pop_front();
        return code;
    }
    std::string newEventId() override { return "e" + std::to_string(++counter); }
    std::string utcNow() override { return "t" + std::to_string(counter); }
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

std::string command(const char* id, const char* action = "on") {
    return std::string("{\"schema_version\":1,\"command_id\":\"") + id + "\",\"node_id\":\"light-01\",\"action\":\"" +
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

void test_FR06_accepted_then_unknown_without_evidence() {
    FakeStore store;
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    assertPublished({"accepted", "running", "unknown"}, port);
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
    TEST_ASSERT_EQUAL_STRING("unknown", stored(store, 0).c_str());
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
    assertPublished({"unknown"}, port);
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
    TEST_ASSERT_EQUAL_STRING("unknown", port.published.back().c_str());
    TEST_ASSERT_EQUAL_STRING(original.c_str(), store.slots[0].c_str());
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
    executor.handle(command("b"), false);
    TEST_ASSERT_EQUAL_STRING("rejected:UNSUPPORTED_ACTION", port.published.back().c_str());
    executor.handle(command("c", "open"), false);
    TEST_ASSERT_EQUAL_STRING("unknown", port.published.back().c_str());
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
    port.enableObservation();
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    TEST_ASSERT_TRUE(executor.busy());
    executor.poll();  // Pending: a second command cannot start in a later loop iteration.
    executor.handle(command("b"), false);
    assertPublished({"accepted", "running", "rejected:DEVICE_BUSY"}, port);
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
    TEST_ASSERT_EQUAL_STRING("rejected", stored(store, 1).c_str());
    port.evidence(np::ObservationState::Proven);
    executor.poll();
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

void test_FR07_no_completion_without_evidence() {
    FakeStore store;
    FakePort port;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    TEST_ASSERT_EQUAL_STRING("unknown", port.published.back().c_str());  // Output alone is not success.
    store.slots.clear();
    port.published.clear();
    // The receipt is written, then the result write fails.
    struct ResultFails : FakeStore {
        bool write(int slot, const std::string& body) override {
            return body.find("\"accepted\"") != std::string::npos && FakeStore::write(slot, body);
        }
    } failing;
    np::Ledger failingLedger(failing);
    np::Executor second(failingLedger, port, 128, 512);
    second.handle(command("b"), false);
    assertPublished({"accepted", "running"}, port);
    TEST_ASSERT_EQUAL_STRING("accepted", stored(failing, 0).c_str());
    TEST_ASSERT_EQUAL_STRING("RESULT_STORE_FAILED accepted_record_kept_no_retry", port.logs.back().c_str());
    TEST_ASSERT_FALSE(second.busy());
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
    TEST_ASSERT_EQUAL_STRING("unknown", port.published[4].c_str());
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


void test_FR07_proven_completion_saved_before_success() {
    FakeStore store;
    FakePort port;
    port.enableObservation();
    port.checkStore = &store;
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    TEST_ASSERT_EQUAL_STRING("accepted", stored(store, 0).c_str());
    port.evidence(np::ObservationState::Proven);
    executor.poll();
    assertPublished({"accepted", "running", "succeeded"}, port);
    TEST_ASSERT_TRUE(port.successWasStored);
    TEST_ASSERT_FALSE(executor.busy());
    std::string resultEvent = port.events.back();
    executor.handle(command("a"), false);
    TEST_ASSERT_EQUAL_STRING(resultEvent.c_str(), port.events.back().c_str());
    TEST_ASSERT_EQUAL_INT(1, port.actuations);

    for (bool readBackFails : {false, true}) {
        FakeStore failing;
        FakePort next;
        next.enableObservation();
        np::Ledger nextLedger(failing);
        np::Executor second(nextLedger, next, 128, 512);
        second.handle(command("a"), false);
        // A missing write and a failed verification must both suppress success.
        failing.failWrite = !readBackFails;
        failing.corruptWrite = readBackFails;
        next.evidence(np::ObservationState::Proven);
        second.poll();
        assertPublished({"accepted", "running"}, next);
        TEST_ASSERT_FALSE(second.busy());
        TEST_ASSERT_EQUAL_STRING("RESULT_STORE_FAILED accepted_record_kept_no_retry", next.logs.back().c_str());
        if (!readBackFails) {
            TEST_ASSERT_EQUAL_STRING("accepted", stored(failing, 0).c_str());
            failing.failWrite = false;
            second.replay();
            TEST_ASSERT_EQUAL_STRING("unknown", next.published.back().c_str());
        }
        TEST_ASSERT_EQUAL_INT(1, next.actuations);
    }
}

void test_FR07_verified_execution_failure() {
    for (bool startFails : {false, true}) {
        FakeStore store;
        FakePort port;
        port.enableObservation(false);  // Success gating never hides a verified failure.
        if (startFails) port.startResult = np::StartResult::Failed;
        np::Ledger ledger(store);
        np::Executor executor(ledger, port, 128, 512);
        executor.handle(command("a"), false);
        if (!startFails) {
            port.evidence(np::ObservationState::Failed);
            executor.poll();
        }
        if (startFails) assertPublished({"accepted", "failed"}, port);
        else assertPublished({"accepted", "running", "failed"}, port);
        TEST_ASSERT_EQUAL_STRING("failed", stored(store, 0).c_str());
        TEST_ASSERT_FALSE(executor.busy());
        executor.handle(command("a"), false);
        TEST_ASSERT_EQUAL_INT(1, port.actuations);
    }
}

void test_FR07_completion_requires_enabled_capability() {
    FakeStore store;
    FakePort port;
    port.enableObservation(false);
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    port.evidence(np::ObservationState::Proven);
    executor.poll();
    assertPublished({"accepted", "running", "unknown"}, port);
    TEST_ASSERT_FALSE(executor.busy());
}

void test_FR07_unavailable_observation_is_unknown() {
    for (int missing = 0; missing < 5; ++missing) {
        FakeStore store;
        FakePort port;
        port.enableObservation();
        if (missing == 0) port.hasObservation = false;
        if (missing == 1) port.attached.policy.timeoutMs = 0;
        if (missing == 2) port.startResult = np::StartResult::Unknown;
        np::Ledger ledger(store);
        np::Executor executor(ledger, port, 128, 512);
        executor.handle(command("a"), false);
        if (missing == 3) {
            port.observation.state = np::ObservationState::Unavailable;  // Read error.
            executor.poll();
        }
        if (missing == 4) {
            port.hasObservation = false;  // Observer disappears during the wait.
            executor.poll();
        }
        TEST_ASSERT_EQUAL_STRING("unknown", port.published.back().c_str());
        TEST_ASSERT_EQUAL_STRING("unknown", stored(store, 0).c_str());
        TEST_ASSERT_FALSE(executor.busy());
        TEST_ASSERT_EQUAL_INT(missing == 3 ? 1 : 0, port.observations);
    }
}

void test_FR07_completion_deadline() {
    for (bool delayedRead : {false, true}) {
        FakeStore store;
        FakePort port;
        port.enableObservation();
        np::Ledger ledger(store);
        np::Executor executor(ledger, port, 128, 512);
        executor.handle(command("a"), false);
        port.now = 149;
        executor.poll();
        TEST_ASSERT_TRUE(executor.busy());
        if (delayedRead) {
            port.evidence(np::ObservationState::Proven);
            port.now = 149;
            port.observeDelayMs = 1;  // Read finishes at the deadline: not success.
        } else port.now = 150;
        executor.poll();
        TEST_ASSERT_EQUAL_STRING("unknown", port.published.back().c_str());
        TEST_ASSERT_FALSE(executor.busy());
        port.hasObservation = false;
        executor.handle(command("b"), false);
        TEST_ASSERT_EQUAL_INT(2, port.actuations);  // Busy was released.
    }
}

void test_FR07_completion_evidence_freshness() {
    for (auto state : {np::ObservationState::Proven, np::ObservationState::Failed}) {
        for (int invalid = 0; invalid < 6; ++invalid) {
            FakeStore store;
            FakePort port;
            port.enableObservation();
            np::Ledger ledger(store);
            np::Executor executor(ledger, port, 128, 512);
            executor.handle(command("a"), false);
            port.evidence(state);
            if (invalid == 0) port.observation.observedAtMs = 99;  // Before start.
            if (invalid == 1) port.observation.observedAtMs = 100;  // Same tick: cannot prove order.
            if (invalid == 2) port.observation.commandId = "other";
            if (invalid == 3) port.observation.source.clear();
            if (invalid == 4) port.observation.observedAtMs = 102;  // Future.
            if (invalid == 5) port.now = 99;  // The clock contract was broken.
            executor.poll();
            TEST_ASSERT_EQUAL_STRING("unknown", port.published.back().c_str());
            TEST_ASSERT_FALSE(executor.busy());
        }
    }
}

void test_NFR03_at_most_once_actuation() {
    FakeStore store;
    FakePort port;
    port.enableObservation();
    np::Ledger ledger(store);
    np::Executor executor(ledger, port, 128, 512);
    executor.handle(command("a"), false);
    executor.handle(command("a"), false);
    executor.replay();  // MQTT reconnect during this boot.
    TEST_ASSERT_EQUAL_STRING("accepted", stored(store, 0).c_str());
    TEST_ASSERT_TRUE(executor.busy());
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
    port.evidence(np::ObservationState::Proven);
    executor.poll();
    executor.poll();
    executor.replay();
    TEST_ASSERT_EQUAL_STRING("succeeded", port.published.back().c_str());
    TEST_ASSERT_EQUAL_INT(1, port.actuations);
    TEST_ASSERT_EQUAL_INT(1, port.observations);

    FakeStore rebootStore;
    {
        FakePort oldPort;
        oldPort.enableObservation();
        np::Ledger oldLedger(rebootStore);
        np::Executor oldExecutor(oldLedger, oldPort, 128, 512);
        oldExecutor.handle(command("a"), false);
        TEST_ASSERT_TRUE(oldExecutor.busy());
    }  // Power loss while pending, with only the receipt persisted.
    FakePort restored;
    np::Ledger restoredLedger(rebootStore);
    np::Executor restoredExecutor(restoredLedger, restored, 128, 512);
    restoredExecutor.replay();
    restoredExecutor.handle(command("a"), false);
    assertPublished({"unknown", "unknown"}, restored);
    TEST_ASSERT_EQUAL_INT(0, restored.actuations);
}

int signalCount = 0;
void countSignal(const np::Actuation&) { ++signalCount; }

void test_NFR10_device_slots_have_no_completion() {
    signalCount = 0;
    np::SignalDevices devices(countSignal, 50, 50, 50);
    for (np::Device* device : {&devices.locker, &devices.light, &devices.speaker}) {
        TEST_ASSERT_FALSE(device->policy.successEnabled);
        TEST_ASSERT_FALSE(device->observer.available());
        auto observation = device->observer.observe({"a", "on", 100});
        TEST_ASSERT_TRUE(observation.state == np::ObservationState::Unavailable);
        TEST_ASSERT_TRUE(observation.source.empty());
        TEST_ASSERT_TRUE(device->actuator.start({"a", "on", 100}) == np::StartResult::Started);
    }
    TEST_ASSERT_EQUAL_INT(3, signalCount);
    np::SignalActuator missing(nullptr);
    TEST_ASSERT_TRUE(missing.start({"a", "on", 100}) == np::StartResult::Unknown);
}

}  // namespace

void runExecutorTests() {
    RUN_TEST(test_FR06_accepted_then_unknown_without_evidence);
    RUN_TEST(test_FR06_invalid_message_no_actuation);
    RUN_TEST(test_FR06_duplicate_after_restart);
    RUN_TEST(test_FR22_changed_capability_preserves_duplicate);
    RUN_TEST(test_FR06_command_id_conflict);
    RUN_TEST(test_FR06_concurrent_commands_busy);
    RUN_TEST(test_FR06_receipt_storage_failure);
    RUN_TEST(test_FR06_session_and_expiry_rechecked);
    RUN_TEST(test_FR07_power_loss_restores_unknown);
    RUN_TEST(test_FR07_no_completion_without_evidence);
    RUN_TEST(test_FR07_result_event_identity);
    RUN_TEST(test_FR22_full_ledger_rejects_without_eviction);
    RUN_TEST(test_FR22_legacy_records_block_reexecution);
    RUN_TEST(test_FR07_proven_completion_saved_before_success);
    RUN_TEST(test_FR07_verified_execution_failure);
    RUN_TEST(test_FR07_completion_requires_enabled_capability);
    RUN_TEST(test_FR07_unavailable_observation_is_unknown);
    RUN_TEST(test_FR07_completion_deadline);
    RUN_TEST(test_FR07_completion_evidence_freshness);
    RUN_TEST(test_NFR03_at_most_once_actuation);
    RUN_TEST(test_NFR10_device_slots_have_no_completion);
}
