#pragma once
// Device-side connection conditions only. Execution permission is decided by the server.

namespace np {

struct ReadinessInputs {
    bool configured = false;
    bool storageReady = false;
    bool wifiUp = false;
    bool clockConfigured = false;
    bool trusted = false;       // One snapshot per loop: the SNTP callback runs on another task.
    bool everSynchronized = false;
    bool mqttConnected = false;
    bool announced = false;
};

// Contract order: config, storage, Wi-Fi, clock, MQTT, announce. nullptr means ready.
inline const char* notReadyReason(const ReadinessInputs& in) {
    if (!in.configured) return "config_missing";
    if (!in.storageReady) return "storage_unavailable";
    if (!in.wifiUp) return "wifi_disconnected";
    if (!in.clockConfigured) return "clock_not_configured";
    if (!in.trusted) return in.everSynchronized ? "clock_trust_expired" : "clock_unverified";
    if (!in.mqttConnected) return "mqtt_disconnected";
    if (!in.announced) return "announce_pending";
    return nullptr;
}

// Doubles the retry delay after each failure and stops at `maximum`.
inline unsigned long backoff(unsigned long current, unsigned long maximum) {
    return current >= maximum / 2 ? maximum : current * 2;
}

}  // namespace np
