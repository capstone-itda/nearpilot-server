#include <unity.h>

#include "np_command.h"

namespace {

const double NOW = 1792713610.0;  // 2026-10-23T00:00:10Z

void fill(JsonDocument& doc) {
    doc["schema_version"] = 1;
    doc["command_id"] = "cmd-1";
    doc["node_id"] = "light-01";
    doc["action"] = "on";
    doc["target_session_id"] = "session-1";
    doc["issued_at"] = "2026-10-23T00:00:00Z";
    doc["expires_at"] = "2026-10-23T00:00:30Z";
}

np::CommandContext context() {
    np::CommandContext value;
    value.nodeId = "light-01";
    value.session = "session-1";
    value.connected = true;
    value.clockTrusted = true;
    value.nowSec = NOW;
    value.maxErrorSec = 0.5;
    return value;
}

const char* check(const char* key, const char* value, const np::CommandContext& ctx = context()) {
    JsonDocument doc;
    fill(doc);
    if (key) {
        if (value) doc[key] = value;
        else doc.remove(key);
    }
    return np::validateCommand(doc, ctx);
}

void test_FR06_validate_order() {
    TEST_ASSERT_NULL(check(nullptr, nullptr));
    TEST_ASSERT_NULL(check("action", "off"));
    JsonDocument doc;
    fill(doc);
    doc["schema_version"] = 2;
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", np::validateCommand(doc, context()));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", check("schema_version", "1"));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", check("node_id", "light-02"));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", check("expires_at", nullptr));
    TEST_ASSERT_EQUAL_STRING("SESSION_MISMATCH", check("target_session_id", "session-0"));
    np::CommandContext offline = context();
    offline.connected = false;
    TEST_ASSERT_EQUAL_STRING("SESSION_MISMATCH", check(nullptr, nullptr, offline));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_ACTION", check("action", "open"));
    np::CommandContext untrusted = context();
    untrusted.clockTrusted = false;
    TEST_ASSERT_EQUAL_STRING("CLOCK_UNVERIFIED", check(nullptr, nullptr, untrusted));
    // Earlier checks win over the clock: the rejection order is part of the contract.
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_ACTION", check("action", "open", untrusted));
    TEST_ASSERT_EQUAL_STRING("SESSION_MISMATCH", check("target_session_id", "x", untrusted));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", check("issued_at", "2026-10-23T00:00:30Z"));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", check("issued_at", "2026-10-23T00:00:11Z"));
    TEST_ASSERT_NULL(check("issued_at", "2026-10-23T00:00:10.4Z"));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", check("issued_at", "2026-10-23T00:00:00+09:00"));
    TEST_ASSERT_EQUAL_STRING("COMMAND_EXPIRED", check("expires_at", "2026-10-23T00:00:10.5Z"));
    TEST_ASSERT_NULL(check("expires_at", "2026-10-23T00:00:10.6Z"));
}

void test_FR06_canonical_content_fields() {
    JsonDocument doc;
    fill(doc);
    const std::string content = np::canonicalContent(doc);
    TEST_ASSERT_EQUAL_STRING(
        "[\"light-01\",\"on\",\"session-1\",\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\"]", content.c_str());
    doc["command_id"] = "other";
    doc["seq"] = 9;
    doc["sent_at"] = "2026-10-23T00:00:01Z";
    TEST_ASSERT_EQUAL_STRING(content.c_str(), np::canonicalContent(doc).c_str());
    doc["action"] = "off";
    TEST_ASSERT_FALSE(content == np::canonicalContent(doc));
    doc.remove("target_session_id");
    TEST_ASSERT_EQUAL_STRING(
        "[\"light-01\",\"off\",null,\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\"]",
        np::canonicalContent(doc).c_str());
}

}  // namespace

void runCommandTests() {
    RUN_TEST(test_FR06_validate_order);
    RUN_TEST(test_FR06_canonical_content_fields);
}
