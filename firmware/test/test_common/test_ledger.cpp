#include <unity.h>

#include <map>
#include <string>

#include "np_ledger.h"

namespace {

class FakeStore : public np::RecordStore {
public:
    bool available = true;
    bool failWrite = false;
    bool corruptWrite = false;
    std::map<int, std::string> slots;

    bool ready() const override { return available; }
    int capacity() const override { return 3; }
    bool has(int slot) override { return slots.count(slot) > 0; }
    std::string read(int slot) override { return slots[slot]; }
    bool write(int slot, const std::string& body) override {
        if (failWrite) return false;
        slots[slot] = corruptWrite ? body.substr(1) : body;
        return true;
    }
};

const char* CONTENT = "[\"light-01\",\"on\",\"s\",\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\"]";

void record(JsonDocument& doc, const char* id, const char* state) {
    doc["command_id"] = id;
    doc["content"] = CONTENT;
    doc["state"] = state;
}

void test_FR06_ledger_finds_record_after_reload() {
    FakeStore store;
    {
        np::Ledger writer(store);
        JsonDocument doc;
        record(doc, "a", "accepted");
        TEST_ASSERT_TRUE(writer.save(0, doc));
    }
    np::Ledger reader(store);  // A new instance stands in for a reboot.
    JsonDocument found;
    int empty = 99;
    TEST_ASSERT_EQUAL_INT(0, reader.find("a", found, empty));
    TEST_ASSERT_EQUAL_STRING("accepted", found["state"].as<const char*>());
    TEST_ASSERT_EQUAL_INT(np::Ledger::NOT_FOUND, reader.find("b", found, empty));
    TEST_ASSERT_EQUAL_INT(1, empty);
    JsonDocument second, third;
    record(second, "b", "rejected");
    record(third, "c", "unknown");
    TEST_ASSERT_TRUE(reader.save(1, second));
    TEST_ASSERT_TRUE(reader.save(2, third));
    TEST_ASSERT_EQUAL_INT(np::Ledger::NOT_FOUND, reader.find("d", found, empty));
    TEST_ASSERT_EQUAL_INT(-1, empty);  // Full: the caller rejects with STORAGE_ERROR, nothing is evicted.
    int visited = 0;
    reader.forEach([&](int, JsonDocument&) { ++visited; });
    TEST_ASSERT_EQUAL_INT(3, visited);
    store.failWrite = true;
    TEST_ASSERT_FALSE(reader.save(0, found));
    store.failWrite = false;
    store.corruptWrite = true;  // The read-back must match the written body.
    TEST_ASSERT_FALSE(reader.save(0, found));
}

void test_FR06_ledger_unreadable_blocks() {
    FakeStore store;
    np::Ledger ledger(store);
    JsonDocument found;
    int empty;
    store.slots[1] = "not json";
    TEST_ASSERT_EQUAL_INT(np::Ledger::STORAGE_ERROR, ledger.find("a", found, empty));
    store.slots[1] = "{\"state\":\"accepted\"}";
    TEST_ASSERT_EQUAL_INT(np::Ledger::STORAGE_ERROR, ledger.find("a", found, empty));
    store.slots.clear();
    store.available = false;
    TEST_ASSERT_EQUAL_INT(np::Ledger::STORAGE_ERROR, ledger.find("a", found, empty));
    JsonDocument doc;
    record(doc, "a", "accepted");
    TEST_ASSERT_FALSE(ledger.save(0, doc));
    store.available = true;
    store.slots[0] = "not json";
    TEST_ASSERT_TRUE(ledger.save(1, doc));
    int visited = 0;
    ledger.forEach([&](int slot, JsonDocument&) { visited += slot; });
    TEST_ASSERT_EQUAL_INT(1, visited);  // The unreadable slot is skipped and kept.
    TEST_ASSERT_EQUAL_STRING("not json", store.slots[0].c_str());
}

void test_FR22_ledger_owner_conflict() {
    FakeStore store;
    np::Ledger ledger(store);
    TEST_ASSERT_NULL(ledger.ownerConflict("light-02"));
    JsonDocument doc;
    record(doc, "a", "unknown");
    TEST_ASSERT_TRUE(ledger.save(0, doc));
    TEST_ASSERT_NULL(ledger.ownerConflict("light-01"));
    TEST_ASSERT_EQUAL_STRING("node_id_change_with_records", ledger.ownerConflict("light-02"));
    store.slots[1] = "{\"command_id\":\"b\",\"content\":\"broken\"}";
    TEST_ASSERT_EQUAL_STRING("record_unreadable", ledger.ownerConflict("light-01"));
    store.available = false;
    TEST_ASSERT_EQUAL_STRING("storage_unavailable", ledger.ownerConflict("light-01"));
}

}  // namespace

void runLedgerTests() {
    RUN_TEST(test_FR06_ledger_finds_record_after_reload);
    RUN_TEST(test_FR06_ledger_unreadable_blocks);
    RUN_TEST(test_FR22_ledger_owner_conflict);
}
