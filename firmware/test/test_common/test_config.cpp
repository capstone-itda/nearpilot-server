#include <unity.h>

#include <string>

#include "np_config.h"

namespace {

void fill(JsonDocument& doc) {
    doc["node_id"] = "light-01";
    doc["mqtt_user"] = "node-light-01";
    doc["mqtt_password"] = "test-only";
    doc["wifi_ssid"] = "lab ap";
    doc["wifi_password"] = "";
    doc["mqtt_host"] = "10.0.0.2";
    doc["mqtt_port"] = "18883";
}

const char* decodeWith(const char* key, const char* value) {
    JsonDocument doc;
    fill(doc);
    if (value) doc[key] = value;
    else doc.remove(key);
    np::NodeConfig out;
    return np::decodeConfig(doc, out);
}

void test_NFR08_config_accepts_valid_values() {
    JsonDocument doc;
    fill(doc);
    np::NodeConfig out;
    TEST_ASSERT_NULL(np::decodeConfig(doc, out));
    TEST_ASSERT_EQUAL_STRING("light-01", out.nodeId.c_str());
    TEST_ASSERT_EQUAL_STRING("lab ap", out.wifiSsid.c_str());
    TEST_ASSERT_EQUAL_UINT16(18883, out.mqttPort);
    TEST_ASSERT_EQUAL_UINT32(0, out.generation);
    doc["generation"] = 7;
    TEST_ASSERT_NULL(np::decodeConfig(doc, out));
    TEST_ASSERT_EQUAL_UINT32(7, out.generation);
    TEST_ASSERT_TRUE(np::isConfigKey("mqtt_password"));
    TEST_ASSERT_FALSE(np::isConfigKey("generation"));
}

void test_FR07_config_has_no_clock_keys() {
    TEST_ASSERT_EQUAL_UINT32(7, np::CONFIG_KEY_COUNT);
    for (const char* key : {"time_server", "clock_max_error_sec", "time_trust_ms", "time_resync_ms"})
        TEST_ASSERT_FALSE_MESSAGE(np::isConfigKey(key), key);  // CONFIG_SET answers unknown_key.
    // A configuration stored before #236 still loads; its clock keys are ignored.
    JsonDocument doc;
    fill(doc);
    doc["time_server"] = "10.0.0.2";
    doc["clock_max_error_sec"] = "-1";
    doc["time_trust_ms"] = "120000";
    doc["time_resync_ms"] = "60000";
    doc["generation"] = 3;
    np::NodeConfig out;
    TEST_ASSERT_NULL(np::decodeConfig(doc, out));
    TEST_ASSERT_EQUAL_STRING("light-01", out.nodeId.c_str());
    TEST_ASSERT_EQUAL_UINT32(3, out.generation);
}

void test_NFR08_config_rejects_invalid_values() {
    struct Case { const char* key; const char* value; const char* expected; };
    const std::string longId(65, 'a'), longSsid(33, 's');
    const Case cases[] = {
        {"node_id", nullptr, "node_id"},
        {"node_id", "light 01", "node_id"},
        {"node_id", longId.c_str(), "node_id"},
        {"mqtt_user", "", "mqtt_user"},
        {"mqtt_password", "", "mqtt_password"},
        {"mqtt_password", "tab\there", "mqtt_password"},
        {"wifi_ssid", longSsid.c_str(), "wifi_ssid"},
        {"wifi_password", "line\nbreak", "wifi_password"},
        {"mqtt_host", "host:1883", "mqtt_host"},
        {"mqtt_port", "0", "mqtt_port"},
        {"mqtt_port", "65536", "mqtt_port"},
        {"mqtt_port", "18a83", "mqtt_port"},
    };
    for (const Case& item : cases) {
        const char* rejected = decodeWith(item.key, item.value);
        TEST_ASSERT_NOT_NULL_MESSAGE(rejected, item.key);
        TEST_ASSERT_EQUAL_STRING_MESSAGE(item.expected, rejected, item.key);
    }
    JsonDocument doc;
    fill(doc);
    doc["mqtt_port"] = 18883;  // Values travel as strings; a number is a type error.
    np::NodeConfig out;
    TEST_ASSERT_EQUAL_STRING("mqtt_port", np::decodeConfig(doc, out));
}

// The kind and occupancy are in the built-in declaration, not in the settings (#245, spec decision 9).
void test_FR04_config_keys_exclude_kind() {
    const char* keys[] = {"node_id", "mqtt_user", "mqtt_password", "wifi_ssid", "wifi_password", "mqtt_host",
                          "mqtt_port"};
    for (const char* key : keys) TEST_ASSERT_TRUE_MESSAGE(np::isConfigKey(key), key);
    for (const char* key : {"capability", "occupancy", "kind", "descriptor"})
        TEST_ASSERT_FALSE_MESSAGE(np::isConfigKey(key), key);  // CONFIG_SET answers unknown_key.
}

// A configuration stored before #245 still loads; its capability and occupancy are ignored, whatever they say.
void test_FR04_stored_config_with_old_keys_loads() {
    for (const char* capability : {"locker", "light", "speaker", "unsupported"}) {
        JsonDocument doc;
        fill(doc);
        doc["capability"] = capability;
        doc["occupancy"] = "rental";
        np::NodeConfig out;
        TEST_ASSERT_NULL(np::decodeConfig(doc, out));
        TEST_ASSERT_EQUAL_STRING("light-01", out.nodeId.c_str());
    }
}

void test_NFR08_config_hex_rejects_nul() {
    std::string out;
    TEST_ASSERT_TRUE(np::fromHex("4142", out));
    TEST_ASSERT_EQUAL_STRING("AB", out.c_str());
    TEST_ASSERT_TRUE(np::fromHex("", out));
    TEST_ASSERT_EQUAL_STRING("", out.c_str());
    TEST_ASSERT_FALSE(np::fromHex("414", out));
    TEST_ASSERT_FALSE(np::fromHex("4g", out));
    TEST_ASSERT_FALSE(np::fromHex("4100", out));
    TEST_ASSERT_TRUE(np::hexValid("0123456789abcdefABCDEF"));
    TEST_ASSERT_FALSE(np::hexValid("12 4"));
}

}  // namespace

void runConfigTests() {
    RUN_TEST(test_NFR08_config_accepts_valid_values);
    RUN_TEST(test_NFR08_config_rejects_invalid_values);
    RUN_TEST(test_FR07_config_has_no_clock_keys);
    RUN_TEST(test_FR04_config_keys_exclude_kind);
    RUN_TEST(test_FR04_stored_config_with_old_keys_loads);
    RUN_TEST(test_NFR08_config_hex_rejects_nul);
}
