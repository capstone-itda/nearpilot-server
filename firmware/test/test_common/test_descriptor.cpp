#include <unity.h>

#include <string>

#include "descriptor_fixtures.h"

namespace {

// A valid shared declaration whose parts the tests replace one at a time.
std::string declaration(const std::string& format = "\"nearpilot.device/1\"", const std::string& kind = "\"fan\"",
                        const std::string& occupancy = "\"shared\"", const std::string& onSchema =
                            "{\"type\":\"object\",\"properties\":{\"speed\":{\"type\":\"integer\",\"minimum\":1,"
                            "\"maximum\":3}},\"required\":[],\"additionalProperties\":false}",
                        const std::string& offRole = "\"deactivate\"", const std::string& extra = "") {
    return "{\"format\":" + format + ",\"kind\":" + kind + ",\"occupancy\":" + occupancy +
           ",\"vendor\":{\"name\":\"acme\",\"model\":\"f1\",\"firmware\":\"1.0\"},\"actions\":["
           "{\"name\":\"on\",\"description\":\"켠다\",\"input_schema\":" + onSchema +
           ",\"result\":{\"kind\":\"signal_output\"}},"
           "{\"name\":\"off\",\"description\":\"끈다\",\"role\":" + offRole +
           ",\"input_schema\":{\"type\":\"object\",\"properties\":{},\"additionalProperties\":false},"
           "\"result\":{\"kind\":\"signal_output\"}}" + extra + "]}";
}

const char* load(const std::string& json) {
    np::Descriptor value;
    return value.load(json.c_str());
}

void test_FR04_built_in_descriptors_load() {
    const np::Descriptor* all[] = {&fixtures::locker(), &fixtures::light(), &fixtures::lightRing(), &fixtures::speaker()};
    for (const np::Descriptor* item : all) TEST_ASSERT_TRUE(item->loaded());
    TEST_ASSERT_EQUAL_STRING("locker", fixtures::locker().kind().c_str());
    TEST_ASSERT_EQUAL_STRING("light", fixtures::lightRing().kind().c_str());
    TEST_ASSERT_EQUAL_STRING("speaker", fixtures::speaker().kind().c_str());
    double limit = 0;
    TEST_ASSERT_TRUE(fixtures::lightRing().maximum("on", "brightness", limit));
    TEST_ASSERT_TRUE(limit == 64);
    TEST_ASSERT_TRUE(fixtures::speaker().maximum("on", "volume", limit));
    TEST_ASSERT_TRUE(limit == 20);
    TEST_ASSERT_FALSE(fixtures::light().maximum("on", "brightness", limit));
    TEST_ASSERT_EQUAL_STRING("reoperate", fixtures::locker().action("close")["role"].as<const char*>());
    TEST_ASSERT_TRUE(fixtures::locker().action("on").isNull());
    TEST_ASSERT_NULL(load(declaration()));
}

void test_FR04_descriptor_load_rejects_invalid() {
    TEST_ASSERT_EQUAL_STRING("json", load("{"));
    TEST_ASSERT_EQUAL_STRING("format", load(declaration("\"nearpilot.device/2\"")));
    TEST_ASSERT_EQUAL_STRING("kind", load(declaration("\"nearpilot.device/1\"", "\"Fan\"")));
    TEST_ASSERT_EQUAL_STRING("kind", load(declaration("\"nearpilot.device/1\"", "\"1fan\"")));
    TEST_ASSERT_EQUAL_STRING("kind", load(declaration("\"nearpilot.device/1\"", "\"fan:a\"")));
    TEST_ASSERT_EQUAL_STRING("occupancy", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"lease\"")));
    const std::string ok = "{\"type\":\"object\",\"properties\":{},\"additionalProperties\":false}";
    // Unknown schema keywords, nested arguments, a non-object top level.
    TEST_ASSERT_EQUAL_STRING("input_schema", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"",
        "{\"type\":\"object\",\"properties\":{\"x\":{\"type\":\"integer\",\"format\":\"int32\"}}}")));
    TEST_ASSERT_EQUAL_STRING("input_schema", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"",
        "{\"type\":\"object\",\"properties\":{\"x\":{\"type\":\"object\"}}}")));
    TEST_ASSERT_EQUAL_STRING("input_schema", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"",
        "{\"type\":\"array\"}")));
    TEST_ASSERT_EQUAL_STRING("input_schema", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"",
        "{\"type\":\"object\",\"properties\":{\"x\":{\"type\":\"string\",\"pattern\":\"(a|b)\"}}}")));
    TEST_ASSERT_EQUAL_STRING("input_schema", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"",
        "{\"type\":\"object\",\"properties\":{\"x\":{\"type\":\"integer\",\"pattern\":\"a\"}}}")));
    // Roles: unknown, shared without exactly one deactivate, reoperate outside rental, deactivate with arguments.
    TEST_ASSERT_EQUAL_STRING("role", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"", ok,
        "\"pause\"")));
    TEST_ASSERT_EQUAL_STRING("role", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"", ok,
        "\"reoperate\"")));
    TEST_ASSERT_EQUAL_STRING("role", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"rental\"")));
    TEST_ASSERT_EQUAL_STRING("role", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"", ok,
        "\"deactivate\"", ",{\"name\":\"halt\",\"description\":\"d\",\"role\":\"deactivate\",\"input_schema\":" + ok +
        ",\"result\":{\"kind\":\"signal_output\"}}")));
    // Duplicate action names and an unknown top-level key.
    TEST_ASSERT_EQUAL_STRING("action_name", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"", ok,
        "\"deactivate\"", ",{\"name\":\"on\",\"description\":\"d\",\"input_schema\":" + ok +
        ",\"result\":{\"kind\":\"signal_output\"}}")));
    std::string extraKey = declaration();
    extraKey.insert(1, "\"icon\":\"x\",");
    TEST_ASSERT_EQUAL_STRING("unknown_key", load(extraKey));
    // Size limits: description 120 bytes, 8 actions.
    std::string longText(121, 'a');
    std::string tooLong = declaration();
    tooLong.replace(tooLong.find("켠다"), std::string("켠다").size(), longText);
    TEST_ASSERT_EQUAL_STRING("description", load(tooLong));
    std::string many;
    for (int i = 0; i < 7; ++i)
        many += ",{\"name\":\"a" + std::to_string(i) + "\",\"description\":\"d\",\"input_schema\":" + ok +
                ",\"result\":{\"kind\":\"signal_output\"}}";
    TEST_ASSERT_EQUAL_STRING("actions", load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"", ok,
        "\"deactivate\"", many)));
}

np::Descriptor schemaUnderTest(const std::string& properties, const std::string& extra = ",\"additionalProperties\":false") {
    np::Descriptor value;
    TEST_ASSERT_NULL(value.load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"",
        "{\"type\":\"object\",\"properties\":" + properties + extra + "}").c_str()));
    return value;
}

const char* args(const np::Descriptor& descriptor, const char* json) {
    JsonDocument doc;
    deserializeJson(doc, json);
    return np::validateArguments(descriptor.action("on"), doc.as<JsonVariantConst>());
}

void test_NFR10_schema_keywords_checked() {
    np::Descriptor value = schemaUnderTest(
        "{\"mode\":{\"type\":\"string\",\"enum\":[\"eco\",\"turbo\"]},\"label\":{\"type\":\"string\",\"maxLength\":3},"
        "\"level\":{\"type\":\"number\",\"minimum\":0.5},\"quiet\":{\"type\":\"boolean\"}}",
        ",\"required\":[\"mode\"],\"additionalProperties\":false");
    TEST_ASSERT_NULL(args(value, "{\"mode\":\"eco\"}"));
    TEST_ASSERT_NULL(args(value, "{\"mode\":\"turbo\",\"label\":\"가나다\",\"level\":0.5,\"quiet\":true}"));
    TEST_ASSERT_NOT_NULL(args(value, "{}"));                                // required
    TEST_ASSERT_NOT_NULL(args(value, "{\"mode\":\"max\"}"));                // enum
    TEST_ASSERT_NOT_NULL(args(value, "{\"mode\":\"eco\",\"label\":\"가나다라\"}"));  // maxLength counts characters
    TEST_ASSERT_NOT_NULL(args(value, "{\"mode\":\"eco\",\"level\":0.4}"));  // minimum
    TEST_ASSERT_NOT_NULL(args(value, "{\"mode\":\"eco\",\"quiet\":1}"));    // boolean
    // Without additionalProperties=false, undeclared flat values pass and nested ones do not (JSON Schema).
    np::Descriptor open = schemaUnderTest("{}", "");
    TEST_ASSERT_NULL(args(open, "{\"anything\":1}"));
    TEST_ASSERT_NOT_NULL(args(open, "{\"anything\":{\"a\":1}}"));
    TEST_ASSERT_NULL(args(open, "null"));
    TEST_ASSERT_EQUAL_STRING("UNSUPPORTED_PARAMETER", np::validateArguments(JsonObjectConst(), JsonVariantConst()));
}

void test_NFR10_pattern_subset() {
    const char* color = "^#[0-9A-Fa-f]{6}$";
    TEST_ASSERT_TRUE(np::patternSupported(color));
    TEST_ASSERT_TRUE(np::patternMatches(color, "#00ff7F"));
    TEST_ASSERT_FALSE(np::patternMatches(color, "#00ff7"));
    TEST_ASSERT_FALSE(np::patternMatches(color, "#00ff7F0"));
    TEST_ASSERT_FALSE(np::patternMatches(color, " #00ff7F"));
    // Without anchors the pattern may match anywhere, as in JSON Schema.
    TEST_ASSERT_TRUE(np::patternMatches("b+c", "aabbbcd"));
    TEST_ASSERT_FALSE(np::patternMatches("^b+c", "aabbbcd"));
    TEST_ASSERT_TRUE(np::patternMatches("^a.c$", "a가c"));  // `.` is one character, not one byte.
    TEST_ASSERT_TRUE(np::patternMatches("^\\d{2,3}x?$", "123"));
    TEST_ASSERT_FALSE(np::patternMatches("^\\d{2,3}x?$", "1234"));
    TEST_ASSERT_TRUE(np::patternMatches("^[^0-9]*$", "abc"));
    TEST_ASSERT_FALSE(np::patternMatches("^[^0-9]*$", "ab1"));
    TEST_ASSERT_TRUE(np::patternMatches("^a\\.b{1,}$", "a.bbb"));
    for (const char* outside : {"(a)", "a|b", "a**", "a*?", "[a", "a{2", "a{3,1}", "\\w", "$a", "a$b", "{2}", "가"})
        TEST_ASSERT_FALSE(np::patternSupported(outside));
}

void test_FR04_announce_body_within_limit() {
    const std::string longest(64, 'n');  // node_id is at most 64 bytes (np_config).
    const np::Descriptor* all[] = {&fixtures::locker(), &fixtures::light(), &fixtures::lightRing(), &fixtures::speaker()};
    for (const np::Descriptor* item : all)
        TEST_ASSERT_TRUE(np::announceBytes(*item, longest) <= np::ANNOUNCE_MAX_BYTES);
    std::string many;
    const std::string ok = "{\"type\":\"object\",\"properties\":{},\"additionalProperties\":false}";
    for (int i = 0; i < 6; ++i)
        many += ",{\"name\":\"a" + std::to_string(i) + "\",\"description\":\"" + std::string(120, 'd') +
                "\",\"input_schema\":" + ok + ",\"result\":{\"kind\":\"signal_output\"}}";
    np::Descriptor large;
    TEST_ASSERT_NULL(large.load(declaration("\"nearpilot.device/1\"", "\"fan\"", "\"shared\"", ok, "\"deactivate\"",
                                            many).c_str()));
    TEST_ASSERT_TRUE(np::announceBytes(large, longest) > np::ANNOUNCE_MAX_BYTES);  // Valid, but too large to send.
}

}  // namespace

void runDescriptorTests() {
    RUN_TEST(test_FR04_built_in_descriptors_load);
    RUN_TEST(test_FR04_descriptor_load_rejects_invalid);
    RUN_TEST(test_NFR10_schema_keywords_checked);
    RUN_TEST(test_NFR10_pattern_subset);
    RUN_TEST(test_FR04_announce_body_within_limit);
}
