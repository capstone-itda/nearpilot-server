#pragma once
// Copy to config.local.h. That local file is ignored by Git.
// No extra board is required. Leave OUTPUT_PIN=-1 for serial-only verification.
#define NP_WIFI_SSID "REPLACE_WIFI_SSID"
#define NP_WIFI_PASSWORD "REPLACE_WIFI_PASSWORD"
#define NP_MQTT_HOST "192.168.0.10"
#define NP_MQTT_PORT 18883
#define NP_NODE_ID "light-1"
#define NP_MQTT_USER "light-1"
#define NP_MQTT_PASSWORD "REPLACE_FROM_LAB_ENV_FILE"
// Local AP time source; do not assume that an Internet time server is reachable.
#define NP_TIME_SERVER ""
// Remains unverified until a device measurement supplies this bound.
#define NP_CLOCK_MAX_ERROR_SEC -1.0
// Choose these only after checking the actual device/time source. Zero disables trust.
#define NP_TIME_TRUST_MS 0UL
#define NP_TIME_RESYNC_MS 0UL
#define NP_OUTPUT_PIN -1
#define NP_OUTPUT_ACTIVE_HIGH true
#define NP_RSSI_FRAME_MS 1000UL
#define NP_STATUS_MS 1000UL
#define NP_RECONNECT_MS 2000UL
#define NP_WIFI_RECONNECT_MS 10000UL
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
