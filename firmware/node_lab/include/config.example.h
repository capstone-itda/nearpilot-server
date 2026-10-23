#pragma once
// Build-time tuning shared by every lab device. Copy to config.local.h only to change it.
// Device ID, MQTT account, Wi-Fi, server/time-source address and clock trust are NOT here:
// they are injected over USB (`python -m tools.node_lab provision`) and kept in NVS.
// No extra board is required. Leave OUTPUT_PIN=-1 for serial-only verification.
#define NP_OUTPUT_PIN -1
#define NP_OUTPUT_ACTIVE_HIGH true
#define NP_RSSI_FRAME_MS 1000UL
#define NP_STATUS_MS 1000UL
// Retry starts at these minimums, doubles after each failure and stops at NP_RETRY_MAX_MS.
#define NP_RECONNECT_MS 2000UL
#define NP_WIFI_RECONNECT_MS 10000UL
#define NP_RETRY_MAX_MS 30000UL
#define NP_MQTT_KEEPALIVE_SEC 30
#define NP_MQTT_IO_TIMEOUT_MS 2000
#define NP_MAX_COMMAND_RECORDS 64
#define NP_MAX_BEACONS 16
#define NP_MAX_COMMAND_BYTES 2048
#define NP_MAX_COMMAND_ID_BYTES 128
#define NP_SERIAL_BAUD 115200
#define NP_BLE_SCAN_INTERVAL_MS 100
#define NP_BLE_SCAN_WINDOW_MS 90
// Optional test transmitter: one of the available ESP32s can replace the phone beacon.
#define NP_BEACON_UUID "11111111-2222-3333-4444-555555555555"
#define NP_BEACON_MAJOR 1
#define NP_BEACON_MINOR 2
#define NP_BEACON_TX_POWER -59
// BLE interval units are 0.625 ms. These are test-source radio settings.
#define NP_BEACON_ADV_MIN_UNITS 160
#define NP_BEACON_ADV_MAX_UNITS 160
