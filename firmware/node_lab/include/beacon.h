#pragma once
#include <cstddef>
#include <cstdint>
#include <cstdio>

// NimBLE manufacturer data includes Apple's little-endian company ID.
inline bool decodeIBeacon(const uint8_t* data, size_t size, char* uuid,
                          uint16_t& major, uint16_t& minor) {
    if (size != 25 || data[0] != 0x4c || data[1] != 0x00 ||
        data[2] != 0x02 || data[3] != 0x15) return false;
    std::snprintf(uuid, 37,
        "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
        data[4], data[5], data[6], data[7], data[8], data[9], data[10], data[11],
        data[12], data[13], data[14], data[15], data[16], data[17], data[18], data[19]);
    major = (uint16_t(data[20]) << 8) | data[21];
    minor = (uint16_t(data[22]) << 8) | data[23];
    return true;
}
