#include <unity.h>

#include <cmath>
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
    doc["time_server"] = "10.0.0.2";
    doc["clock_max_error_sec"] = "0.25";
    doc["time_trust_ms"] = "120000";
    doc["time_resync_ms"] = "60000";
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
    TEST_ASSERT_TRUE(std::fabs(out.clockMaxErrorSec - 0.25) < 1e-9);
    TEST_ASSERT_EQUAL_UINT32(120000, out.timeTrustMs);
    TEST_ASSERT_EQUAL_UINT32(60000, out.timeResyncMs);
    TEST_ASSERT_EQUAL_UINT32(0, out.generation);
    // Unmeasured bound and disabled trust are valid settings; they only keep the clock untrusted.
    doc["clock_max_error_sec"] = "-1";
    doc["time_trust_ms"] = "0";
    doc["time_resync_ms"] = "0";
    doc["time_server"] = "";
    doc["generation"] = 7;
    TEST_ASSERT_NULL(np::decodeConfig(doc, out));
    TEST_ASSERT_TRUE(out.clockMaxErrorSec < 0.0);
    TEST_ASSERT_EQUAL_UINT32(7, out.generation);
    TEST_ASSERT_TRUE(np::isConfigKey("mqtt_password"));
    TEST_ASSERT_FALSE(np::isConfigKey("generation"));
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
        {"time_server", "pool ntp", "time_server"},
        {"mqtt_port", "0", "mqtt_port"},
        {"mqtt_port", "65536", "mqtt_port"},
        {"mqtt_port", "18a83", "mqtt_port"},
        {"clock_max_error_sec", "", "clock_max_error_sec"},
        {"clock_max_error_sec", "-0.5", "clock_max_error_sec"},
        {"clock_max_error_sec", "3600", "clock_max_error_sec"},
        {"clock_max_error_sec", "0.5s", "clock_max_error_sec"},
        {"time_trust_ms", "14999", "time_trust_ms"},
        {"time_trust_ms", "86400001", "time_trust_ms"},
        {"time_resync_ms", "130000", "time_resync_ms"},  // Resync must not exceed trust.
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
    RUN_TEST(test_NFR08_config_hex_rejects_nul);
}
