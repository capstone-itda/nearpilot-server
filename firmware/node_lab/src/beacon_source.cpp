// Optional iBeacon source on an existing ESP32. No MQTT or client application.
#include <Arduino.h>
#include <NimBLEDevice.h>
#if __has_include("config.local.h")
#include "config.local.h"
#else
#include "config.example.h"
#endif

static_assert(NP_BEACON_MAJOR >= 0 && NP_BEACON_MAJOR <= 65535, "iBeacon major range");
static_assert(NP_BEACON_MINOR >= 0 && NP_BEACON_MINOR <= 65535, "iBeacon minor range");
static_assert(NP_BEACON_TX_POWER >= -128 && NP_BEACON_TX_POWER <= 127, "iBeacon measured-power range");

int hexDigit(char value) {
    if (value >= '0' && value <= '9') return value - '0';
    if (value >= 'a' && value <= 'f') return value - 'a' + 10;
    if (value >= 'A' && value <= 'F') return value - 'A' + 10;
    return -1;
}

bool beaconPayload(std::string& payload) {
    String uuid = NP_BEACON_UUID;
    if (uuid.length() != 36 || uuid[8] != '-' || uuid[13] != '-' || uuid[18] != '-' || uuid[23] != '-')
        return false;
    uint8_t bytes[25] = {0x4c, 0x00, 0x02, 0x15};
    size_t next = 4;
    for (size_t i = 0; i < uuid.length();) {
        if (i == 8 || i == 13 || i == 18 || i == 23) { ++i; continue; }
        int high = hexDigit(uuid[i++]);
        int low = hexDigit(uuid[i++]);
        if (high < 0 || low < 0 || next >= 20) return false;
        bytes[next++] = uint8_t(high * 16 + low);
    }
    bytes[20] = (NP_BEACON_MAJOR >> 8) & 0xff;
    bytes[21] = NP_BEACON_MAJOR & 0xff;
    bytes[22] = (NP_BEACON_MINOR >> 8) & 0xff;
    bytes[23] = NP_BEACON_MINOR & 0xff;
    bytes[24] = uint8_t(int8_t(NP_BEACON_TX_POWER));
    payload.assign(reinterpret_cast<const char*>(bytes), sizeof(bytes));
    return next == 20;
}

void setup() {
    Serial.begin(NP_SERIAL_BAUD);
    std::string payload;
    if (!beaconPayload(payload)) {
        Serial.println("BEACON_CONFIG_INVALID no_advertising");
        return;
    }
    NimBLEDevice::init("");
    NimBLEAdvertisementData data;
    data.setFlags(0x06);
    data.setManufacturerData(payload);
    NimBLEAdvertising* advertising = NimBLEDevice::getAdvertising();
    advertising->setAdvertisementData(data);
    advertising->setMinInterval(NP_BEACON_ADV_MIN_UNITS);
    advertising->setMaxInterval(NP_BEACON_ADV_MAX_UNITS);
    advertising->start();
    Serial.printf("BEACON_STARTED uuid=%s major=%u minor=%u\n",
        NP_BEACON_UUID, unsigned(NP_BEACON_MAJOR), unsigned(NP_BEACON_MINOR));
}

void loop() { delay(NP_STATUS_MS); }
