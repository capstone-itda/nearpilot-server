#include <unity.h>

#include <string>

#include "descriptor_fixtures.h"
#include "np_command.h"

namespace {

const double NOW = 1792713610.0;  // 2026-10-23T00:00:10Z

void fill(JsonDocument& doc) {
    doc["schema_version"] = 3;
    doc["command_id"] = "cmd-1";
    doc["node_id"] = "light-01";
    doc["action"] = "on";
    doc["target_session_id"] = "session-1";
    doc["issued_at"] = "2026-10-23T00:00:00Z";
    doc["expires_at"] = "2026-10-23T00:00:30Z";
}

np::CommandContext context(const np::Descriptor* descriptor = &fixtures::light()) {
    np::CommandContext value;
    value.descriptor = descriptor;
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
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", check("schema_version", "3"));
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

// cmd is v3 only (spec decision 8): v1 and v2 are rejected, never reinterpreted.
void test_FR06_command_version_three_only() {
    for (int version : {1, 2, 4}) {
        JsonDocument doc;
        fill(doc);
        doc["schema_version"] = version;
        TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", np::validateCommand(doc, context()));
    }
}

// The declaration alone decides the actions; no kind name is compared (#245).
void test_FR06_descriptor_action_matrix() {
    struct Case {
        const np::Descriptor* descriptor;
        const char* allowed[2];
    };
    const Case cases[] = {{&fixtures::locker(), {"open", "close"}}, {&fixtures::light(), {"on", "off"}},
                          {&fixtures::lightRing(), {"on", "off"}}, {&fixtures::speaker(), {"on", "off"}},
                          {nullptr, {"", ""}}};
    for (const Case& item : cases) {
        for (const char* action : {"open", "on", "off", "", "close", "ON", "spin"}) {
            auto ctx = context(item.descriptor);
            const std::string act = action;
            const bool allowed = !act.empty() && (act == item.allowed[0] || act == item.allowed[1]);
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

void test_FR06_action_outside_descriptor_rejected() {
    np::Descriptor fan;
    TEST_ASSERT_NULL(fan.load(
        "{\"format\":\"nearpilot.device/1\",\"kind\":\"fan\",\"occupancy\":\"shared\","
        "\"vendor\":{\"name\":\"acme\",\"model\":\"f1\",\"firmware\":\"1\"},\"actions\":["
        "{\"name\":\"spin\",\"description\":\"d\",\"input_schema\":{\"type\":\"object\"},\"result\":{\"kind\":\"signal_output\"}},"
        "{\"name\":\"stop\",\"description\":\"d\",\"role\":\"deactivate\",\"input_schema\":{\"type\":\"object\","
        "\"properties\":{},\"additionalProperties\":false},\"result\":{\"kind\":\"signal_output\"}}]}"));
    TEST_ASSERT_NULL(check("action", "spin", context(&fan)));
    TEST_ASSERT_NULL(check("action", "stop", context(&fan)));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_ACTION", check("action", "on", context(&fan)));
    np::Descriptor unloaded;  // A declaration that failed at boot accepts nothing.
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_ACTION", check("action", "on", context(&unloaded)));
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

const char* withArguments(const char* arguments, const np::CommandContext& ctx = context(&fixtures::lightRing()),
                          const char* action = "on") {
    JsonDocument doc;
    fill(doc);
    doc["action"] = action;
    JsonDocument value;
    deserializeJson(value, arguments);
    doc["arguments"] = value;
    return np::validateCommand(doc, ctx);
}

np::CommandContext speakerContext() { return context(&fixtures::speaker()); }

void test_NFR10_light_arguments_boundaries() {
    TEST_ASSERT_NULL(withArguments("{\"brightness\":0,\"color\":\"#0000FF\"}"));
    TEST_ASSERT_NULL(withArguments("{\"brightness\":64,\"color\":\"#0000FF\"}"));
    TEST_ASSERT_NULL(withArguments("{\"color\":\"#FFFFFF\",\"brightness\":30}"));  // Key order is free.
    TEST_ASSERT_NULL(withArguments("{}"));
}

void test_NFR10_light_arguments_above_cap_rejected() {
    // The declared range holds even when a server would allow more.
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments("{\"brightness\":65,\"color\":\"#0000FF\"}"));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments("{\"brightness\":255}"));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments("{\"brightness\":-1}"));
}

void test_NFR10_arguments_outside_schema_rejected() {
    const char* bad[] = {
        "{\"brightness\":\"30\"}", "{\"brightness\":30.5}", "{\"brightness\":true}", "{\"brightness\":null}",
        "{\"color\":\"0000FF\"}", "{\"color\":\"#0000FG\"}", "{\"color\":\"#0000FF0\"}", "{\"color\":7}",
        "{\"brightness\":30,\"color\":\"#0000FF\",\"extra\":1}", "{\"volume\":3}", "[30,\"#0000FF\"]", "30",
        "\"on\""};
    for (const char* arguments : bad)
        TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments(arguments));
}

void test_NFR10_light_arguments_unsupported() {
    const char* blue = "{\"brightness\":30,\"color\":\"#0000FF\"}";
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments(blue, context()));  // No ring: no arguments.
    TEST_ASSERT_NULL(withArguments("{}", context()));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER",
                             withArguments(blue, context(&fixtures::lightRing()), "off"));  // deactivate takes none.
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments(blue, speakerContext()));
    // Arguments are checked before the clock: the order is part of the contract.
    np::CommandContext untrusted = context(&fixtures::lightRing());
    untrusted.clockTrusted = false;
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments("{\"brightness\":65}", untrusted));
    TEST_ASSERT_EQUAL_STRING("CLOCK_UNVERIFIED", withArguments(blue, untrusted));
}

void test_NFR10_speaker_arguments_boundaries() {
    TEST_ASSERT_NULL(withArguments("{\"volume\":0}", speakerContext()));
    TEST_ASSERT_NULL(withArguments("{\"volume\":20}", speakerContext()));
    // The limit is the declaration's own, not a fixed number.
    np::Descriptor lower;
    TEST_ASSERT_NULL(lower.load(
        "{\"format\":\"nearpilot.device/1\",\"kind\":\"speaker\",\"occupancy\":\"shared\","
        "\"vendor\":{\"name\":\"a\",\"model\":\"b\",\"firmware\":\"c\"},\"actions\":["
        "{\"name\":\"on\",\"description\":\"d\",\"input_schema\":{\"type\":\"object\",\"properties\":"
        "{\"volume\":{\"type\":\"integer\",\"minimum\":0,\"maximum\":10}},\"additionalProperties\":false},"
        "\"result\":{\"kind\":\"signal_output\"}},"
        "{\"name\":\"off\",\"description\":\"d\",\"role\":\"deactivate\",\"input_schema\":{\"type\":\"object\","
        "\"properties\":{},\"additionalProperties\":false},\"result\":{\"kind\":\"signal_output\"}}]}"));
    TEST_ASSERT_NULL(withArguments("{\"volume\":10}", context(&lower)));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments("{\"volume\":11}", context(&lower)));
}

void test_NFR10_speaker_arguments_above_cap_rejected() {
    // The declared limit holds even when a server would allow more; the DFPlayer itself takes up to 30.
    for (const char* arguments : {"{\"volume\":21}", "{\"volume\":30}", "{\"volume\":-1}"})
        TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments(arguments, speakerContext()));
}

void test_NFR10_speaker_arguments_bad_shape_rejected() {
    const char* bad[] = {
        "{\"volume\":\"12\"}", "{\"volume\":12.5}", "{\"volume\":true}", "{\"volume\":12,\"extra\":1}",
        "{\"brightness\":12}", "{\"brightness\":30,\"color\":\"#0000FF\"}", "[12]", "12"};
    for (const char* arguments : bad)
        TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments(arguments, speakerContext()));
    const char* twelve = "{\"volume\":12}";
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments(twelve, speakerContext(), "off"));
    // A light takes no volume, with or without the ring.
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments(twelve, context(&fixtures::lightRing())));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments(twelve, context()));
    // Arguments are checked before the clock here too: the order is part of the contract.
    np::CommandContext untrusted = speakerContext();
    untrusted.clockTrusted = false;
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", withArguments("{\"volume\":21}", untrusted));
    TEST_ASSERT_EQUAL_STRING("CLOCK_UNVERIFIED", withArguments(twelve, untrusted));
}

// [key, value] pairs sorted by key (spec decision 7).
void test_FR22_arguments_join_content() {
    JsonDocument doc;
    fill(doc);
    const std::string plain = np::canonicalContent(doc);  // Unchanged without arguments.
    TEST_ASSERT_EQUAL_STRING(
        "[\"light-01\",\"on\",\"session-1\",\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\"]", plain.c_str());
    doc["arguments"]["color"] = "#0000FF";
    doc["arguments"]["brightness"] = 30;
    const std::string blue = np::canonicalContent(doc);
    TEST_ASSERT_EQUAL_STRING(
        "[\"light-01\",\"on\",\"session-1\",\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\","
        "[[\"brightness\",30],[\"color\",\"#0000FF\"]]]", blue.c_str());
    doc["arguments"]["brightness"] = 31;
    TEST_ASSERT_FALSE(blue == np::canonicalContent(doc));
    JsonDocument swapped;
    fill(swapped);
    swapped["arguments"]["brightness"] = 30;
    swapped["arguments"]["color"] = "#0000FF";
    TEST_ASSERT_EQUAL_STRING(blue.c_str(), np::canonicalContent(swapped).c_str());
    JsonDocument empty;
    fill(empty);
    empty["arguments"].to<JsonObject>();  // Present but empty differs from absent.
    TEST_ASSERT_FALSE(plain == np::canonicalContent(empty));
    // Same values under other keys are another command.
    JsonDocument renamed;
    fill(renamed);
    renamed["arguments"]["level"] = 30;
    renamed["arguments"]["tint"] = "#0000FF";
    TEST_ASSERT_FALSE(blue == np::canonicalContent(renamed));
}

void test_FR22_speaker_arguments_join_content() {
    JsonDocument doc;
    fill(doc);
    doc["arguments"]["volume"] = 12;
    const std::string twelve = np::canonicalContent(doc);
    TEST_ASSERT_EQUAL_STRING(
        "[\"light-01\",\"on\",\"session-1\",\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\",[[\"volume\",12]]]",
        twelve.c_str());
    doc["arguments"]["volume"] = 13;
    TEST_ASSERT_FALSE(twelve == np::canonicalContent(doc));
    doc["arguments"]["volume"] = 0;  // Zero is a volume, not a missing argument.
    TEST_ASSERT_EQUAL_STRING(
        "[\"light-01\",\"on\",\"session-1\",\"2026-10-23T00:00:00Z\",\"2026-10-23T00:00:30Z\",[[\"volume\",0]]]",
        np::canonicalContent(doc).c_str());
}

void test_FR09_command_actuation() {
    JsonDocument doc;
    fill(doc);
    np::Actuation plain = np::commandActuation("cmd-1", doc);
    TEST_ASSERT_FALSE(plain.hasBrightness || plain.hasColor || plain.hasVolume);
    TEST_ASSERT_EQUAL_STRING("on", plain.action.c_str());
    doc["arguments"]["brightness"] = 30;
    doc["arguments"]["color"] = "#0000FF";
    np::Actuation blue = np::commandActuation("cmd-1", doc);
    TEST_ASSERT_TRUE(blue.hasBrightness && blue.hasColor);
    TEST_ASSERT_EQUAL_UINT8(30, blue.brightness);
    TEST_ASSERT_EQUAL_HEX32(0x0000FF, blue.color);
}

// `required` is empty, so one argument alone is valid; the board fills the other (spec decision 5).
void test_FR09_partial_arguments_actuation() {
    TEST_ASSERT_NULL(withArguments("{\"brightness\":30}"));
    TEST_ASSERT_NULL(withArguments("{\"color\":\"#00FF00\"}"));
    JsonDocument doc;
    fill(doc);
    doc["arguments"]["brightness"] = 30;
    np::Actuation dim = np::commandActuation("cmd-1", doc);
    TEST_ASSERT_TRUE(dim.hasBrightness);
    TEST_ASSERT_FALSE(dim.hasColor);
    JsonDocument green;
    fill(green);
    green["arguments"]["color"] = "#00FF00";
    np::Actuation tint = np::commandActuation("cmd-1", green);
    TEST_ASSERT_FALSE(tint.hasBrightness);
    TEST_ASSERT_TRUE(tint.hasColor);
    TEST_ASSERT_EQUAL_HEX32(0x00FF00, tint.color);
    // Without the ring's brightness the output equals the plain level: white scaled by it.
    TEST_ASSERT_EQUAL_HEX32(np::lightRingRgb(true, 64), np::lightRingScaled(0xFFFFFF, 64));
}

void test_FR09_lowercase_color_accepted() {
    TEST_ASSERT_NULL(withArguments("{\"color\":\"#00ff7f\"}"));
    JsonDocument doc;
    fill(doc);
    doc["arguments"]["color"] = "#00ff7f";
    TEST_ASSERT_EQUAL_HEX32(0x00FF7F, np::commandActuation("cmd-1", doc).color);
}

const char* restartCheck(const char* key, const char* value, const np::CommandContext& ctx = context(),
    const std::string& last = "") {
    JsonDocument doc;
    doc["schema_version"] = 2;
    doc["node_id"] = "light-01";
    doc["restart_id"] = "restart-1";
    doc["target_session_id"] = "session-1";
    doc["issued_at"] = "2026-10-23T00:00:00Z";
    doc["expires_at"] = "2026-10-23T00:00:30Z";
    if (key) {
        if (value) doc[key] = value;
        else doc.remove(key);
    }
    std::string id;
    const char* rejection = np::validateRestart(doc, ctx, last, id);
    TEST_ASSERT_EQUAL_STRING(rejection ? "" : "restart-1", id.c_str());  // The id is set only on success.
    return rejection;
}

void test_NFR04_restart_rejects_other_session() {
    TEST_ASSERT_NULL(restartCheck(nullptr, nullptr));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", restartCheck("node_id", "light-02"));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", restartCheck("restart_id", ""));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", restartCheck("restart_id", nullptr));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", restartCheck("schema_version", "1"));
    TEST_ASSERT_EQUAL_STRING("SESSION_MISMATCH", restartCheck("target_session_id", "session-0"));
    np::CommandContext offline = context();
    offline.connected = false;
    TEST_ASSERT_EQUAL_STRING("SESSION_MISMATCH", restartCheck(nullptr, nullptr, offline));
}

void test_NFR04_restart_requires_valid_window() {
    np::CommandContext untrusted = context();
    untrusted.clockTrusted = false;
    TEST_ASSERT_EQUAL_STRING("CLOCK_UNVERIFIED", restartCheck(nullptr, nullptr, untrusted));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", restartCheck("expires_at", "2026-10-23T00:00:00Z"));
    TEST_ASSERT_EQUAL_STRING("INVALID_MESSAGE", restartCheck("issued_at", "2026-10-23T00:00:11Z"));
    TEST_ASSERT_EQUAL_STRING("COMMAND_EXPIRED", restartCheck("expires_at", "2026-10-23T00:00:10.5Z"));
    TEST_ASSERT_NULL(restartCheck("expires_at", "2026-10-23T00:00:10.6Z"));
}

void test_NFR04_restart_ignores_handled_id() {
    TEST_ASSERT_EQUAL_STRING("ALREADY_HANDLED", restartCheck(nullptr, nullptr, context(), "restart-1"));
    TEST_ASSERT_NULL(restartCheck(nullptr, nullptr, context(), "restart-0"));
}

}  // namespace

void runCommandTests() {
    RUN_TEST(test_NFR04_restart_rejects_other_session);
    RUN_TEST(test_NFR04_restart_requires_valid_window);
    RUN_TEST(test_NFR04_restart_ignores_handled_id);
    RUN_TEST(test_FR06_validate_order);
    RUN_TEST(test_FR06_command_version_three_only);
    RUN_TEST(test_FR06_descriptor_action_matrix);
    RUN_TEST(test_FR06_action_outside_descriptor_rejected);
    RUN_TEST(test_FR06_canonical_content_fields);
    RUN_TEST(test_NFR10_light_arguments_boundaries);
    RUN_TEST(test_NFR10_light_arguments_above_cap_rejected);
    RUN_TEST(test_NFR10_arguments_outside_schema_rejected);
    RUN_TEST(test_NFR10_light_arguments_unsupported);
    RUN_TEST(test_NFR10_speaker_arguments_boundaries);
    RUN_TEST(test_NFR10_speaker_arguments_above_cap_rejected);
    RUN_TEST(test_NFR10_speaker_arguments_bad_shape_rejected);
    RUN_TEST(test_FR22_arguments_join_content);
    RUN_TEST(test_FR22_speaker_arguments_join_content);
    RUN_TEST(test_FR09_command_actuation);
    RUN_TEST(test_FR09_partial_arguments_actuation);
    RUN_TEST(test_FR09_lowercase_color_accepted);
}
