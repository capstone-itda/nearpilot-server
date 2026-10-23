// Runs only on the ESP32: fills the real np_records partition through a scratch namespace.
// Real command records live in "np-records" and are never written or erased here.
// Run it before real records exist; they would share the partition's pages.
#include <Arduino.h>
#include <nvs.h>
#include <unity.h>

#include <string>

#include "np_ledger.h"
#include "np_nvs_store.h"
#if __has_include("config.local.h")
#include "config.local.h"
#else
#include "config.example.h"
#endif

namespace {

// A record exactly NP_MAX_RECORD_BYTES long in its final (failed + error code) form.
void largest(JsonDocument& record, int index, bool final) {
    std::string id = std::to_string(index);
    id.insert(0, NP_MAX_COMMAND_ID_BYTES - id.size(), 'x');
    record["command_id"] = id;
    record["state"] = "failed";
    record["event_id"] = "00000000-0000-4000-8000-000000000000";
    record["occurred_at"] = "2026-10-23T00:00:00.000000Z";
    record["error_code"] = "COMMAND_ID_CONFLICT";
    record["content"] = "";
    record["content"] = std::string(NP_MAX_RECORD_BYTES - measureJson(record), 'n');
    if (!final) {
        record["state"] = "accepted";
        record.remove("error_code");
    }
}

void test_FR22_record_partition_capacity() {
    np::NvsRecordStore store("np_records", "np-test", NP_MAX_COMMAND_RECORDS);
    TEST_ASSERT_TRUE_MESSAGE(store.begin(), "np_records partition did not open; check partitions.csv");
    TEST_ASSERT_EQUAL(ESP_OK, nvs_erase_all(store.handle()));
    TEST_ASSERT_EQUAL(ESP_OK, nvs_commit(store.handle()));
    size_t before = store.freeEntries();
    np::Ledger ledger(store);
    TEST_ASSERT_TRUE(ledger.load());
    int stored = 0;
    for (int slot = 0; slot < NP_MAX_COMMAND_RECORDS; ++slot) {
        JsonDocument receipt, result;
        largest(receipt, slot, false);
        largest(result, slot, true);
        // Every receipt is later rewritten with its result, as a command would be.
        if (!ledger.create(slot, receipt) || !ledger.save(slot, result)) break;
        ++stored;
    }
    unsigned long started = millis();
    np::Ledger rebooted(store);
    bool loaded = rebooted.load();
    unsigned long loadMs = millis() - started;
    JsonDocument found;
    int empty;
    started = micros();
    int miss = rebooted.find("missing", found, empty);
    unsigned long missUs = micros() - started;
    JsonDocument probe;
    largest(probe, NP_MAX_COMMAND_RECORDS - 1, true);
    std::string last = probe["command_id"].as<std::string>();
    started = micros();
    int hit = rebooted.find(last, found, empty);
    unsigned long hitUs = micros() - started;
    char line[220];
    snprintf(line, sizeof(line), "record_bytes=%d entries_each=%u slots=%d stored=%d free_before=%u free_after=%u "
        "load_ms=%lu miss_us=%lu hit_us=%lu", NP_MAX_RECORD_BYTES,
        unsigned(np::NvsRecordStore::entriesFor(NP_MAX_RECORD_BYTES)), NP_MAX_COMMAND_RECORDS, stored,
        unsigned(before), unsigned(store.freeEntries()), loadMs, missUs, hitUs);
    TEST_MESSAGE(line);
    TEST_ASSERT_EQUAL(ESP_OK, nvs_erase_all(store.handle()));
    TEST_ASSERT_EQUAL(ESP_OK, nvs_commit(store.handle()));
    TEST_ASSERT_EQUAL_INT(NP_MAX_COMMAND_RECORDS, stored);
    TEST_ASSERT_TRUE(loaded);
    TEST_ASSERT_EQUAL_INT(np::Ledger::NOT_FOUND, miss);
    TEST_ASSERT_EQUAL_INT(NP_MAX_COMMAND_RECORDS - 1, hit);
}

}  // namespace

void setUp() {}
void tearDown() {}

void setup() {
    delay(2000);  // Let the test runner open the serial port after reset.
    UNITY_BEGIN();
    RUN_TEST(test_FR22_record_partition_capacity);
    UNITY_END();
}

void loop() {}
