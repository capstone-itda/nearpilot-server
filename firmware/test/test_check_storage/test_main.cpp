// ESP32 only: scratch namespace in the NEW check partition, never production records.
#include <Arduino.h>
#include <nvs.h>
#include <unity.h>
#include "np_ledger.h"
#include "np_nvs_store.h"
#if __has_include("config.local.h")
#include "config.local.h"
#else
#include "config.example.h"
#endif
#ifndef NP_MAX_CHECK_RECORDS
#define NP_MAX_CHECK_RECORDS 256
#endif
#ifndef NP_MAX_CHECK_RECORD_BYTES
#define NP_MAX_CHECK_RECORD_BYTES 1024
#endif

void test_FR22_check_partition_capacity() {
    np::NvsRecordStore store("np_checks", "np-check-test", NP_MAX_CHECK_RECORDS);
    TEST_ASSERT_TRUE(store.begin());
    TEST_ASSERT_EQUAL(ESP_OK, nvs_erase_all(store.handle()));
    TEST_ASSERT_EQUAL(ESP_OK, nvs_commit(store.handle()));
    np::Ledger ledger(store, "check_id");
    int written = 0, rewritten = 0;
    for (int slot = 0; slot < NP_MAX_CHECK_RECORDS; ++slot) {
        JsonDocument doc;
        doc["check_id"] = "check-" + std::to_string(slot);
        doc["state"] = "accepted";
        doc["padding"] = "";
        doc["padding"] = std::string(NP_MAX_CHECK_RECORD_BYTES - measureJson(doc), 'x');
        if (!ledger.create(slot, doc)) break;
        ++written;
    }
    for (int slot = 0; slot < written; ++slot) {
        JsonDocument doc;
        deserializeJson(doc, store.read(slot));
        doc["state"] = "final";
        std::string padding = doc["padding"].as<std::string>();
        padding.append(3, 'y');  // Keep exactly 1024 bytes when accepted -> final shrinks by 3.
        doc["padding"] = padding;
        if (measureJson(doc) != NP_MAX_CHECK_RECORD_BYTES || !ledger.save(slot, doc)) break;
        ++rewritten;
    }
    np::Ledger restored(store, "check_id");
    bool loaded = restored.load();
    int empty;
    JsonDocument last;
    int found = restored.find("check-" + std::to_string(NP_MAX_CHECK_RECORDS - 1), last, empty);
    char message[160];
    snprintf(message, sizeof(message), "check_bytes=%d slots=%d written=%d rewritten=%d free_entries=%u",
        NP_MAX_CHECK_RECORD_BYTES, NP_MAX_CHECK_RECORDS, written, rewritten, unsigned(store.freeEntries()));
    TEST_MESSAGE(message);
    // Only this scratch namespace. Never erase the partition or np-checks/np-records/np-config.
    TEST_ASSERT_EQUAL(ESP_OK, nvs_erase_all(store.handle()));
    TEST_ASSERT_EQUAL(ESP_OK, nvs_commit(store.handle()));
    TEST_ASSERT_EQUAL_INT(NP_MAX_CHECK_RECORDS, written);
    TEST_ASSERT_EQUAL_INT(NP_MAX_CHECK_RECORDS, rewritten);
    TEST_ASSERT_TRUE(loaded);
    TEST_ASSERT_EQUAL_INT(NP_MAX_CHECK_RECORDS - 1, found);
}
void setUp() {}
void tearDown() {}
void setup() {
    delay(2000);
    UNITY_BEGIN();
    RUN_TEST(test_FR22_check_partition_capacity);
    UNITY_END();
}
void loop() {}
