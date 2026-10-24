#pragma once
// Physical observations only. Command/executor state is never sensor evidence (FR-07, FR-20).
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
};

class NoSensors : public Sensors {
public:
    SensorSnapshot read(const Measurement&) override { return {}; }
};

bool freshObservation(const BoolObservation& value, uint64_t begin, uint64_t end,
                      double utcBegin, double utcEnd);
void physicalState(JsonDocument& report, const SensorSnapshot& sample, const std::string& capability,
                   uint64_t begin, uint64_t end, double utcBegin, double utcEnd);

}  // namespace np
