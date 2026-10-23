#include <unity.h>

#include "np_beacon.h"

namespace {

void test_FR05_ibeacon_decode() {
    uint8_t data[25] = {0x4c, 0x00, 0x02, 0x15,
        0x11, 0x11, 0x11, 0x11, 0x22, 0x22, 0x33, 0x33, 0x44, 0x44, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55,
        0x01, 0x02, 0x03, 0x04, 0xc5};
    char uuid[37];
    uint16_t major = 0, minor = 0;
    TEST_ASSERT_TRUE(np::decodeIBeacon(data, sizeof(data), uuid, major, minor));
    TEST_ASSERT_EQUAL_STRING("11111111-2222-3333-4444-555555555555", uuid);
    TEST_ASSERT_EQUAL_UINT16(0x0102, major);
    TEST_ASSERT_EQUAL_UINT16(0x0304, minor);
    TEST_ASSERT_FALSE(np::decodeIBeacon(data, 24, uuid, major, minor));
    data[0] = 0x4d;  // Another company ID.
    TEST_ASSERT_FALSE(np::decodeIBeacon(data, sizeof(data), uuid, major, minor));
    data[0] = 0x4c;
    data[2] = 0x03;  // Not the iBeacon type.
    TEST_ASSERT_FALSE(np::decodeIBeacon(data, sizeof(data), uuid, major, minor));
}

}  // namespace

void runBeaconTests() { RUN_TEST(test_FR05_ibeacon_decode); }
