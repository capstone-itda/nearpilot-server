#pragma once
// Build-time tuning shared by every device. Copy to config.local.h only to change it.
// Device ID, MQTT account, Wi-Fi, server/time-source address and clock trust are NOT here:
// they are injected over USB (`python -m tools.node_lab provision`) and kept in NVS.
// No extra board is required. Leave OUTPUT_PIN=-1 for serial-only verification.
#define NP_OUTPUT_PIN -1
#define NP_OUTPUT_ACTIVE_HIGH true
// Milliseconds, 0 = unmeasured/unset; otherwise a positive integer.
// These are device completion limits, not the server's exec_timeout_sec.
// No observer is installed and success reporting is disabled for every feature (#142).
#define NP_LOCKER_COMPLETION_TIMEOUT_MS 0ULL
#define NP_LIGHT_COMPLETION_TIMEOUT_MS 0ULL
#define NP_SPEAKER_COMPLETION_TIMEOUT_MS 0ULL
#define NP_RSSI_FRAME_MS 1000UL
#define NP_STATUS_MS 1000UL
// Retry starts at these minimums, doubles after each failure and stops at NP_RETRY_MAX_MS.
#define NP_RECONNECT_MS 2000UL
#define NP_WIFI_RECONNECT_MS 10000UL
#define NP_RETRY_MAX_MS 30000UL
#define NP_MQTT_KEEPALIVE_SEC 30
#define NP_MQTT_IO_TIMEOUT_MS 2000
// Largest stored record. 512 bytes take 18 NVS entries, so 7 fit in a 126-entry page.
#define NP_MAX_RECORD_BYTES 512
// Slots in the 64-page np_records partition (partitions.csv): one page is NVS's compaction page and
// one is kept free for rewrites, so at most 7 x 62 = 434 records of the largest size fit.
// Verified by test_FR22_record_partition_capacity.
#define NP_MAX_COMMAND_RECORDS 400
// node_lab slots in the default nvs; at least node_lab's NP_MAX_COMMAND_RECORDS.
#define NP_LEGACY_COMMAND_RECORDS 64
#define NP_MAX_BEACONS 16
#define NP_MAX_COMMAND_BYTES 2048
#define NP_MAX_COMMAND_ID_BYTES 128
#define NP_SERIAL_BAUD 115200
#define NP_BLE_SCAN_INTERVAL_MS 100
#define NP_BLE_SCAN_WINDOW_MS 90
