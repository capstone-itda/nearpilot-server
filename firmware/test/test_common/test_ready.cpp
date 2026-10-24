#include <unity.h>

#include "np_ready.h"

namespace {

void test_FR04_ready_requires_contract_order() {
    np::ReadinessInputs in;
    TEST_ASSERT_EQUAL_STRING("config_missing", np::notReadyReason(in));
    in.configured = true;
    TEST_ASSERT_EQUAL_STRING("storage_unavailable", np::notReadyReason(in));
    in.storageReady = true;
    TEST_ASSERT_EQUAL_STRING("wifi_disconnected", np::notReadyReason(in));
    in.wifiUp = true;
    TEST_ASSERT_EQUAL_STRING("clock_not_configured", np::notReadyReason(in));
    in.clockConfigured = true;
    TEST_ASSERT_EQUAL_STRING("clock_unverified", np::notReadyReason(in));
    in.everSynchronized = true;
    TEST_ASSERT_EQUAL_STRING("clock_trust_expired", np::notReadyReason(in));
    in.mqttConnected = in.announced = true;  // MQTT alone never makes an untrusted node ready.
    TEST_ASSERT_EQUAL_STRING("clock_trust_expired", np::notReadyReason(in));
    in.trusted = true;
    in.mqttConnected = in.announced = false;
    TEST_ASSERT_EQUAL_STRING("mqtt_disconnected", np::notReadyReason(in));
    in.mqttConnected = true;
    TEST_ASSERT_EQUAL_STRING("announce_pending", np::notReadyReason(in));
    in.announced = true;
    TEST_ASSERT_NULL(np::notReadyReason(in));
}

void test_FR04_retry_backoff_caps() {
    TEST_ASSERT_EQUAL_UINT32(4000, np::backoff(2000, 30000));
    TEST_ASSERT_EQUAL_UINT32(16000, np::backoff(8000, 30000));
    TEST_ASSERT_EQUAL_UINT32(30000, np::backoff(16000, 30000));
    TEST_ASSERT_EQUAL_UINT32(30000, np::backoff(30000, 30000));
}

}  // namespace

void runReadyTests() {
    RUN_TEST(test_FR04_ready_requires_contract_order);
    RUN_TEST(test_FR04_retry_backoff_caps);
}
