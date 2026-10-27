#pragma once
// Build-time tuning shared by every device. Copy to config.local.h only to change it.
// Device ID, MQTT account, Wi-Fi, server/time-source address and clock trust are NOT here:
// they are injected over USB (`python -m tools.node_lab provision`) and kept in NVS.
// Optional plain GPIO output for any device. No build profile sets it.
// No extra board is required. Leave OUTPUT_PIN=-1 for serial-only verification.
#ifndef NP_OUTPUT_PIN
#define NP_OUTPUT_PIN -1
#endif
#ifndef NP_OUTPUT_ACTIVE_HIGH
#define NP_OUTPUT_ACTIVE_HIGH true
#endif
// Locker servo. The locker-1 build profile sets NP_SERVO_PIN=13; -1 leaves the servo off.
// Boot holds the locked angle; open turns to the open angle and stays until a close command.
#ifndef NP_SERVO_PIN
#define NP_SERVO_PIN -1
#endif
#ifndef NP_SERVO_LOCKED_DEG
#define NP_SERVO_LOCKED_DEG 0
#endif
#ifndef NP_SERVO_OPEN_DEG
#define NP_SERVO_OPEN_DEG 90
#endif
// SG90-class pulse range for 0..180 degrees at 50 Hz.
#define NP_SERVO_MIN_US 500
#define NP_SERVO_MAX_US 2400
#define NP_SERVO_FREQ_HZ 50
#define NP_SERVO_LEDC_CHANNEL 0
#define NP_SERVO_LEDC_BITS 16
// WS2812B occupancy stick. The locker-1 build profile sets NP_LED_PIN=19; -1 leaves it off.
// AVAILABLE shows blue, other stored states red; off until the first valid `occupancy`.
#ifndef NP_LED_PIN
#define NP_LED_PIN -1
#endif
#define NP_LED_COUNT 8
#define NP_LED_LEVEL 64
// WS2812B light ring. The light-1 build profile sets NP_LIGHT_RING_PIN=18; -1 leaves it off.
// on without parameters lights every LED white at the level, off clears it; off at boot.
// MAX_LEVEL is this firmware's own per-channel output limit, announced as controls.brightness_max.
// Parameters above it are rejected whatever the server allows (NFR-10). 64 is the level verified
// white on light-1 powered from the board 5V by a power bank; raise it only after measuring current.
#ifndef NP_LIGHT_RING_PIN
#define NP_LIGHT_RING_PIN -1
#endif
#define NP_LIGHT_RING_COUNT 16
#define NP_LIGHT_RING_LEVEL 64
#define NP_LIGHT_RING_MAX_LEVEL 64
// DFPlayer Mini speaker on UART2. The speaker-1 build profile sets TX=17, RX=16; -1 leaves it off.
// Boot waits for the module, then sets the volume and stops; on loops the SD folder until off.
#ifndef NP_SPEAKER_TX_PIN
#define NP_SPEAKER_TX_PIN -1
#endif
#ifndef NP_SPEAKER_RX_PIN
#define NP_SPEAKER_RX_PIN -1
#endif
#ifndef NP_SPEAKER_VOLUME
#define NP_SPEAKER_VOLUME 30
#endif
#ifndef NP_SPEAKER_FOLDER
#define NP_SPEAKER_FOLDER 1
#endif
#define NP_SPEAKER_BAUD 9600
// The module ignores commands while it reads the SD card after power-on.
#define NP_SPEAKER_BOOT_MS 2000
// Gap between consecutive frames so the module does not drop the second one.
#define NP_SPEAKER_FRAME_GAP_MS 100
#define NP_RSSI_FRAME_MS 1000UL
#define NP_STATUS_MS 1000UL
// Retry starts at these minimums, doubles after each failure and stops at NP_RETRY_MAX_MS.
#define NP_RECONNECT_MS 2000UL
#define NP_WIFI_RECONNECT_MS 10000UL
#define NP_RETRY_MAX_MS 30000UL
#define NP_MQTT_KEEPALIVE_SEC 30
// QoS1 PUBACK/CONNACK/SUBACK wait. A miss closes the link (MQTT -9). Measured worst 2,003 ms (spec 7단계).
#define NP_MQTT_IO_TIMEOUT_MS 5000
// Loop task watchdog. Fed after every network call, so it must exceed the longest single wait:
// TCP connect (3 s, WiFiClient default) plus one MQTT reply wait.
#define NP_WATCHDOG_TIMEOUT_SEC 15
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
// Wi-Fi and BLE share one radio. A 90 ms window starved MQTT; 30 ms removed the -9 disconnects (spec 7단계).
#define NP_BLE_SCAN_INTERVAL_MS 100
#define NP_BLE_SCAN_WINDOW_MS 30
