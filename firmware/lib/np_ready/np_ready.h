#pragma once
// Device-side connection conditions only. Execution permission is decided by the server.

namespace np {

struct ReadinessInputs {
    bool configured = false;
    bool storageReady = false;
    bool wifiUp = false;
    bool mqttConnected = false;
    bool trusted = false;       // One snapshot per loop, the same value the loop acts on.
    bool everSynchronized = false;
    bool announced = false;
};

// Contract order: config, storage, Wi-Fi, MQTT, clock, announce. nullptr means ready.
// The clock is set over MQTT (#236), so it can only be checked once MQTT is up.
inline const char* notReadyReason(const ReadinessInputs& in) {
    if (!in.configured) return "config_missing";
    if (!in.storageReady) return "storage_unavailable";
    if (!in.wifiUp) return "wifi_disconnected";
    if (!in.mqttConnected) return "mqtt_disconnected";
    if (!in.trusted) return in.everSynchronized ? "clock_trust_expired" : "clock_unverified";
    if (!in.announced) return "announce_pending";
    return nullptr;
}

// Doubles the retry delay after each failure and stops at `maximum`.
inline unsigned long backoff(unsigned long current, unsigned long maximum) {
    return current >= maximum / 2 ? maximum : current * 2;
}

}  // namespace np
