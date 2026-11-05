#include <unity.h>

#include <climits>
#include <cmath>
#include <string>

#include "np_clock.h"

namespace {

// The server's `time` reply (architecture.md §5 MQTT 6절) with the final demo values.
std::string reply(const char* field = nullptr, const char* value = nullptr) {
    std::string body = "{\"schema_version\":2,\"node_id\":\"light-1\",\"request_id\":\"req-7\","
        "\"server_time\":\"2026-10-23T01:02:03.250000Z\",\"max_error_sec\":0.5,\"trust_sec\":120,\"resync_sec\":60}";
    if (!field) return body;
    const std::string key = std::string("\"") + field + "\":";
    const size_t at = body.find(key) + key.size();
    const size_t end = body.find_first_of(",}", body[at] == '"' ? body.find('"', at + 1) : at);
    return body.substr(0, at) + value + body.substr(end);
}

const char* decode(const std::string& body, np::TimeOffer& offer, const char* requestId = "req-7") {
    return np::decodeTimeReply(body.c_str(), body.size(), "light-1", requestId, offer);
}

void test_FR07_time_reply_decodes() {
    np::TimeOffer offer;
    TEST_ASSERT_NULL(decode(reply(), offer));
    TEST_ASSERT_TRUE(std::fabs(offer.serverTime - 1792717323.25) < 1e-6);
    TEST_ASSERT_TRUE(std::fabs(offer.maxErrorSec - 0.5) < 1e-9);
    TEST_ASSERT_EQUAL_UINT32(120000, offer.trustMs);
    TEST_ASSERT_EQUAL_UINT32(60000, offer.resyncMs);
    TEST_ASSERT_NULL(decode(reply("max_error_sec", "1"), offer));  // An integer bound is a number too.
}

void test_FR07_reply_wrong_request() {
    np::TimeOffer offer;
    // A late reply to an earlier request, or none outstanding, never sets the clock.
    TEST_ASSERT_EQUAL_STRING("request_id", decode(reply(), offer, "req-8"));
    TEST_ASSERT_EQUAL_STRING("request_id", decode(reply(), offer, ""));
    TEST_ASSERT_EQUAL_STRING("node_id", decode(reply("node_id", "\"light-2\""), offer));
}

void test_FR07_time_reply_rejects_invalid() {
    struct Case { const char* field; const char* value; const char* expected; };
    const Case cases[] = {
        {"schema_version", "1", "schema_version"},
        {"request_id", "7", "request_id"},
        {"server_time", "\"2026-10-23T01:02:03\"", "server_time"},
        {"server_time", "1792717323", "server_time"},
        {"max_error_sec", "-0.1", "max_error_sec"},
        {"max_error_sec", "60.5", "max_error_sec"},
        {"max_error_sec", "\"0.5\"", "max_error_sec"},
        {"trust_sec", "14", "trust_sec"},
        {"trust_sec", "86401", "trust_sec"},
        {"trust_sec", "120.5", "trust_sec"},
        {"resync_sec", "4", "resync_sec"},
        {"resync_sec", "121", "resync_sec"},  // Resync must not exceed trust.
    };
    for (const Case& item : cases) {
        np::TimeOffer offer;
        const std::string body = reply(item.field, item.value);
        TEST_ASSERT_EQUAL_STRING_MESSAGE(item.expected, decode(body, offer), body.c_str());
    }
    np::TimeOffer offer;
    TEST_ASSERT_EQUAL_STRING("json", decode("{", offer));
    TEST_ASSERT_EQUAL_STRING("json", decode("[]", offer));
}

void test_FR07_offer_sets_half_round_trip() {
    np::TimeOffer offer;
    TEST_ASSERT_NULL(decode(reply(), offer));
    TEST_ASSERT_TRUE(std::fabs(np::offerTime(offer, 200000) - (1792717323.25 + 0.1)) < 1e-6);
    // r/2 = 0.1 s plus 100 ppm over 120 s = 0.012 s.
    TEST_ASSERT_TRUE(std::fabs(np::offerErrorSec(offer, 200000, 100) - 0.112) < 1e-9);
}

void test_FR07_offer_rejects_slow_round_trip() {
    np::TimeOffer offer;
    TEST_ASSERT_NULL(decode(reply(), offer));
    TEST_ASSERT_TRUE(np::offerErrorSec(offer, 970000, 100) <= offer.maxErrorSec);  // 0.485 + 0.012
    TEST_ASSERT_TRUE(np::offerErrorSec(offer, 990000, 100) > offer.maxErrorSec);  // 0.495 + 0.012
    offer.maxErrorSec = 0.0;  // Only a same-PC simulator has no error; a network reply never fits.
    TEST_ASSERT_TRUE(np::offerErrorSec(offer, 1, 0) > offer.maxErrorSec);
}

void test_FR07_clock_trust_expires() {
    np::TimeOffer offer;
    TEST_ASSERT_NULL(decode(reply(), offer));
    np::ClockState state;
    TEST_ASSERT_FALSE(np::clockTrusted(state, 0));
    TEST_ASSERT_TRUE(np::resyncDue(state, 0));
    np::recordSync(state, offer, 1000);
    TEST_ASSERT_TRUE(std::fabs(state.maxErrorSec - 0.5) < 1e-9);
    TEST_ASSERT_TRUE(np::clockTrusted(state, 1000 + 120000));
    TEST_ASSERT_FALSE(np::clockTrusted(state, 1000 + 120001));
    TEST_ASSERT_FALSE(np::resyncDue(state, 1000 + 59999));
    TEST_ASSERT_TRUE(np::resyncDue(state, 1000 + 60000));
    // A millis() wrap between sync and now still measures the true age.
    np::recordSync(state, offer, ULONG_MAX - 10);
    TEST_ASSERT_TRUE(np::clockTrusted(state, 5));
    TEST_ASSERT_FALSE(np::resyncDue(state, 5));
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
    RUN_TEST(test_FR07_time_reply_decodes);
    RUN_TEST(test_FR07_reply_wrong_request);
    RUN_TEST(test_FR07_time_reply_rejects_invalid);
    RUN_TEST(test_FR07_offer_sets_half_round_trip);
    RUN_TEST(test_FR07_offer_rejects_slow_round_trip);
    RUN_TEST(test_FR07_clock_trust_expires);
    RUN_TEST(test_FR06_rejects_malformed_utc);
}
