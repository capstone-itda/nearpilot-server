#include <unity.h>

#include <climits>
#include <cmath>

#include "np_clock.h"

namespace {

np::NodeConfig clockConfig() {
    np::NodeConfig config;
    config.timeServer = "10.0.0.2";
    config.clockMaxErrorSec = 0.25;
    config.timeTrustMs = 120000;
    config.timeResyncMs = 60000;
    return config;
}

void test_NFR12_clock_trust_window() {
    np::NodeConfig config = clockConfig();
    TEST_ASSERT_TRUE(np::clockTrusted(true, config, true, 1000, 1000 + 120000));
    TEST_ASSERT_FALSE(np::clockTrusted(true, config, true, 1000, 1000 + 120001));
    TEST_ASSERT_FALSE(np::clockTrusted(true, config, false, 1000, 1000));
    TEST_ASSERT_FALSE(np::clockTrusted(false, config, true, 1000, 1000));
    // A millis() wrap between sync and now still measures the true age.
    TEST_ASSERT_TRUE(np::clockTrusted(true, config, true, ULONG_MAX - 10, 5));
    config.clockMaxErrorSec = -1.0;
    TEST_ASSERT_FALSE(np::clockConfigured(true, config));
    TEST_ASSERT_FALSE(np::clockTrusted(true, config, true, 1000, 1000));
    config = clockConfig();
    config.timeServer.clear();
    TEST_ASSERT_FALSE(np::clockConfigured(true, config));
    config = clockConfig();
    config.timeTrustMs = 0;
    TEST_ASSERT_FALSE(np::clockConfigured(true, config));
}

void test_FR06_rejects_malformed_utc() {
    double seconds = 0.0;
    TEST_ASSERT_TRUE(np::parseUtc("2026-10-23T01:02:03Z", seconds));
    TEST_ASSERT_TRUE(std::fabs(seconds - 1792717323.0) < 1e-6);
    TEST_ASSERT_TRUE(np::parseUtc("2026-10-23T01:02:03.5+00:00", seconds));
    TEST_ASSERT_TRUE(std::fabs(seconds - 1792717323.5) < 1e-6);
    TEST_ASSERT_TRUE(np::parseUtc("2024-02-29T00:00:00.000001Z", seconds));
    TEST_ASSERT_TRUE(np::parseUtc("1970-01-01T00:00:00Z", seconds));
    TEST_ASSERT_TRUE(std::fabs(seconds) < 1e-9);
    const char* rejected[] = {
        "", "2026-10-23T01:02:03", "2026-10-23T01:02:03+09:00", "2026-10-23T01:02:03.Z",
        "2026-10-23 01:02:03Z", "26-10-23T01:02:03Z", "2026-1-23T01:02:03Z", "2026-13-01T00:00:00Z",
        "2026-10-00T00:00:00Z", "2026-02-29T00:00:00Z", "2026-04-31T00:00:00Z", "2026-10-23T24:00:00Z",
        "2026-10-23T23:60:00Z", "2026-10-23T23:59:60Z", "2026-10-23T01:02:03Zx", "2026-10-23T01:02:03z",
    };
    for (const char* raw : rejected) TEST_ASSERT_FALSE_MESSAGE(np::parseUtc(raw, seconds), raw);
}

}  // namespace

void runClockTests() {
    RUN_TEST(test_NFR12_clock_trust_window);
    RUN_TEST(test_FR06_rejects_malformed_utc);
}
