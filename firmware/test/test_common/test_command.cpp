#include <unity.h>

#include "np_command.h"

namespace {

const double NOW = 1792713610.0;  // 2026-10-23T00:00:10Z

void fill(JsonDocument& doc) {
    doc["schema_version"] = 2;
    doc["command_id"] = "cmd-1";
    doc["node_id"] = "light-01";
    doc["action"] = "on";
    doc["target_session_id"] = "session-1";
    doc["issued_at"] = "2026-10-23T00:00:00Z";
    doc["expires_at"] = "2026-10-23T00:00:30Z";
}

np::CommandContext context() {
    np::CommandContext value;
    value.capability = "light";
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
    doc["schema_version"] = 1;  // MQTT v1 is rejected, not reinterpreted.
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

void test_FR06_capability_action_matrix() {
    for (const char* capability : {"locker", "light", "speaker", "", "other"}) {
        for (const char* action : {"open", "on", "off", "", "close", "ON"}) {
            auto ctx = context();
            ctx.capability = capability;
            const std::string cap = capability, act = action;
            const bool allowed = cap == "locker" ? act == "open" || act == "close" :
                (cap == "light" || cap == "speaker") && (act == "on" || act == "off");
            const char* result = check("action", action, ctx);
            if (allowed) TEST_ASSERT_NULL(result);
            else TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_ACTION", result);
            ctx.clockTrusted = false;
            TEST_ASSERT_EQUAL_STRING(allowed ? "CLOCK_UNVERIFIED" : "UNSUPPORTED_ACTION",
                                     check("action", action, ctx));
            ctx.connected = false;
            TEST_ASSERT_EQUAL_STRING("SESSION_MISMATCH", check("action", action, ctx));
        }
    }
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

np::CommandContext ringContext() {
    np::CommandContext value = context();
    value.lightMaxLevel = 64;
    value.lightColor = true;
    return value;
}

np::CommandContext speakerContext() {
    np::CommandContext value = context();
    value.capability = "speaker";
    value.speakerMaxVolume = 20;
    return value;
}

const char* withParameters(const char* parameters, const np::CommandContext& ctx = ringContext(),
                           const char* action = "on") {
    JsonDocument doc;
    fill(doc);
    doc["action"] = action;
    JsonDocument value;
    deserializeJson(value, parameters);
    doc["parameters"] = value;
    return np::validateCommand(doc, ctx);
}

void test_NFR10_light_parameters_boundaries() {
    TEST_ASSERT_NULL(withParameters("{\"brightness\":0,\"color\":\"#0000FF\"}"));
    TEST_ASSERT_NULL(withParameters("{\"brightness\":64,\"color\":\"#0000FF\"}"));
    TEST_ASSERT_NULL(withParameters("{\"color\":\"#FFFFFF\",\"brightness\":30}"));  // Key order is free.
}

void test_NFR10_light_parameters_above_cap_rejected() {
    // The firmware limit holds even when a server would allow more.
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters("{\"brightness\":65,\"color\":\"#0000FF\"}"));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters("{\"brightness\":255,\"color\":\"#FFFFFF\"}"));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters("{\"brightness\":-1,\"color\":\"#FFFFFF\"}"));
}

void test_NFR10_light_parameters_format_rejected() {
    const char* bad[] = {
        "{\"brightness\":\"30\",\"color\":\"#0000FF\"}", "{\"brightness\":30.5,\"color\":\"#0000FF\"}",
        "{\"brightness\":true,\"color\":\"#0000FF\"}", "{\"brightness\":30,\"color\":\"#0000ff\"}",
        "{\"brightness\":30,\"color\":\"0000FF\"}", "{\"brightness\":30,\"color\":\"#0000FG\"}",
        "{\"brightness\":30,\"color\":\"#0000FF0\"}", "{\"brightness\":30}", "{\"color\":\"#0000FF\"}",
        "{\"brightness\":30,\"color\":\"#0000FF\",\"extra\":1}", "[30,\"#0000FF\"]", "30"};
    for (const char* parameters : bad)
        TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters(parameters));
}

void test_NFR10_light_parameters_unsupported() {
    const char* blue = "{\"brightness\":30,\"color\":\"#0000FF\"}";
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters(blue, context()));  // No ring.
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters(blue, ringContext(), "off"));
    np::CommandContext white = ringContext();
    white.lightColor = false;
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters(blue, white));
    TEST_ASSERT_NULL(withParameters("{\"brightness\":30,\"color\":\"#FFFFFF\"}", white));
    np::CommandContext speaker = ringContext();
    speaker.capability = "speaker";
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters(blue, speaker));
    // Parameters are checked before the clock: the order is part of the contract.
    np::CommandContext untrusted = ringContext();
    untrusted.clockTrusted = false;
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER",
                             withParameters("{\"brightness\":65,\"color\":\"#FFFFFF\"}", untrusted));
    TEST_ASSERT_EQUAL_STRING("CLOCK_UNVERIFIED", withParameters(blue, untrusted));
}

void test_NFR10_speaker_parameters_boundaries() {
    TEST_ASSERT_NULL(withParameters("{\"volume\":0}", speakerContext()));
    TEST_ASSERT_NULL(withParameters("{\"volume\":20}", speakerContext()));
    // The limit is the context's own, not a fixed number.
    np::CommandContext lower = speakerContext();
    lower.speakerMaxVolume = 10;
    TEST_ASSERT_NULL(withParameters("{\"volume\":10}", lower));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters("{\"volume\":11}", lower));
}

void test_NFR10_speaker_parameters_above_cap_rejected() {
    // The firmware limit holds even when a server would allow more; the DFPlayer itself takes up to 30.
    for (const char* parameters : {"{\"volume\":21}", "{\"volume\":30}", "{\"volume\":-1}"})
        TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters(parameters, speakerContext()));
}

void test_NFR10_speaker_parameters_bad_shape_rejected() {
    const char* bad[] = {
        "{\"volume\":\"12\"}", "{\"volume\":12.5}", "{\"volume\":true}", "{}", "{\"volume\":12,\"extra\":1}",
        "{\"brightness\":12}", "{\"brightness\":30,\"color\":\"#0000FF\"}", "[12]", "12"};
    for (const char* parameters : bad)
        TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters(parameters, speakerContext()));
    const char* twelve = "{\"volume\":12}";
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters(twelve, speakerContext(), "off"));
    np::CommandContext noLimit = speakerContext();
    noLimit.speakerMaxVolume = 0;  // A build without the DFPlayer announces no limit and takes no parameters.
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters("{\"volume\":0}", noLimit));
    // A light takes no volume, with or without the ring.
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters(twelve, ringContext()));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters(twelve, context()));
    // Parameters are checked before the clock here too: the order is part of the contract.
    np::CommandContext untrusted = speakerContext();
    untrusted.clockTrusted = false;
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withParameters("{\"volume\":21}", untrusted));
    TEST_ASSERT_EQUAL_STRING("CLOCK_UNVERIFIED", withParameters(twelve, untrusted));
}

void test_FR22_parameters_join_content() {
    JsonDocument doc;
    fill(doc);
    const std::string plain = np::canonicalContent(doc);  // Unchanged without parameters.
    TEST_ASSERT_EQUAL_STRING(
        "[\"light-01\",\"on\",\"session-1\",\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\"]", plain.c_str());
    doc["parameters"]["brightness"] = 30;
    doc["parameters"]["color"] = "#0000FF";
    const std::string blue = np::canonicalContent(doc);
    TEST_ASSERT_EQUAL_STRING(
        "[\"light-01\",\"on\",\"session-1\",\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\",[30,\"#0000FF\"]]",
        blue.c_str());
    doc["parameters"]["brightness"] = 31;
    TEST_ASSERT_FALSE(blue == np::canonicalContent(doc));
    JsonDocument swapped;
    fill(swapped);
    swapped["parameters"]["color"] = "#0000FF";
    swapped["parameters"]["brightness"] = 30;
    TEST_ASSERT_EQUAL_STRING(blue.c_str(), np::canonicalContent(swapped).c_str());
}

void test_FR22_speaker_parameters_join_content() {
    JsonDocument doc;
    fill(doc);
    const std::string plain = np::canonicalContent(doc);  // Unchanged without parameters.
    TEST_ASSERT_EQUAL_STRING(
        "[\"light-01\",\"on\",\"session-1\",\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\"]", plain.c_str());
    doc["parameters"]["volume"] = 12;
    const std::string twelve = np::canonicalContent(doc);
    TEST_ASSERT_EQUAL_STRING(
        "[\"light-01\",\"on\",\"session-1\",\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\",[12]]",
        twelve.c_str());
    doc["parameters"]["volume"] = 13;
    TEST_ASSERT_FALSE(twelve == np::canonicalContent(doc));
    doc["parameters"]["volume"] = 0;  // Zero is a volume, not a missing parameter.
    TEST_ASSERT_EQUAL_STRING(
        "[\"light-01\",\"on\",\"session-1\",\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\",[0]]",
        np::canonicalContent(doc).c_str());
}

void test_FR09_command_actuation() {
    JsonDocument doc;
    fill(doc);
    np::Actuation plain = np::commandActuation("cmd-1", doc);
    TEST_ASSERT_FALSE(plain.light);
    TEST_ASSERT_EQUAL_STRING("on", plain.action.c_str());
    doc["parameters"]["brightness"] = 30;
    doc["parameters"]["color"] = "#0000FF";
    np::Actuation blue = np::commandActuation("cmd-1", doc);
    TEST_ASSERT_TRUE(blue.light);
    TEST_ASSERT_EQUAL_UINT8(30, blue.brightness);
    TEST_ASSERT_EQUAL_HEX32(0x0000FF, blue.color);
}

}  // namespace

void runCommandTests() {
    RUN_TEST(test_FR06_validate_order);
    RUN_TEST(test_FR06_capability_action_matrix);
    RUN_TEST(test_FR06_canonical_content_fields);
    RUN_TEST(test_NFR10_light_parameters_boundaries);
    RUN_TEST(test_NFR10_light_parameters_above_cap_rejected);
    RUN_TEST(test_NFR10_light_parameters_format_rejected);
    RUN_TEST(test_NFR10_light_parameters_unsupported);
    RUN_TEST(test_NFR10_speaker_parameters_boundaries);
    RUN_TEST(test_NFR10_speaker_parameters_above_cap_rejected);
    RUN_TEST(test_NFR10_speaker_parameters_bad_shape_rejected);
    RUN_TEST(test_FR22_parameters_join_content);
    RUN_TEST(test_FR22_speaker_parameters_join_content);
    RUN_TEST(test_FR09_command_actuation);
}
