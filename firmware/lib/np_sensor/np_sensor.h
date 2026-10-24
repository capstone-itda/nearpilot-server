#pragma once
// Physical observations. Command/executor state is never return-check evidence (FR-07, FR-20);
// status reports use it only for fields no installed sensor covers (spec 6절, 2026-10-24).
#include <ArduinoJson.h>
#include <cstdint>
#include <string>

namespace np {

struct BoolObservation {
    BoolObservation(bool observed = false, bool reading = false, uint64_t monotonic = 0,
                    const std::string& utc = "", const std::string& origin = "")
        : known(observed), value(reading), observedAtMs(monotonic), observedAt(utc), source(origin) {}
    bool known, value;
    uint64_t observedAtMs;
    std::string observedAt, source;
};

struct SensorSnapshot {
    BoolObservation doorClosed, locked, lightOn, actuatorIdle, sensorFault;
};

struct Measurement {
    Measurement(const std::string& id = "", uint64_t started = 0) : checkId(id), startedAtMs(started) {}
    std::string checkId;  // Empty for periodic status, otherwise a fresh return-check read.
    uint64_t startedAtMs;
};

class Sensors {
public:
    virtual ~Sensors() = default;
    // Read now; do not return a cached status as return-check evidence. Must return promptly.
    virtual SensorSnapshot read(const Measurement& request) = 0;
    // Installed hardware. A sensed field that fails to read stays unknown/null.
    virtual bool sensesActuator() const = 0;
    virtual bool detectsFaults() const = 0;
};

class NoSensors : public Sensors {
public:
    SensorSnapshot read(const Measurement&) override { return {}; }
    bool sensesActuator() const override { return false; }
    bool detectsFaults() const override { return false; }
};

// Board facts for status fields that no installed sensor covers.
struct StatusFallback {
    StatusFallback(bool actuatorSensed = true, bool faultSensed = true, bool active = false)
        : sensesActuator(actuatorSensed), detectsFaults(faultSensed), commandActive(active) {}
    bool sensesActuator;  // false: report whether this firmware is driving the output (idle/busy)
    bool detectsFaults;   // false: no fault-detecting sensor exists, so none can be faulty
    bool commandActive;
};

bool freshObservation(const BoolObservation& value, uint64_t begin, uint64_t end,
                      double utcBegin, double utcEnd);
void physicalState(JsonDocument& report, const SensorSnapshot& sample, const std::string& capability,
                   uint64_t begin, uint64_t end, double utcBegin, double utcEnd,
                   const StatusFallback& fallback = StatusFallback());

}  // namespace np
