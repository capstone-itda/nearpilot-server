#include "np_sensor.h"
#include "np_clock.h"
#include <cmath>

namespace np {

bool freshObservation(const BoolObservation& value, uint64_t begin, uint64_t end,
                      double utcBegin, double utcEnd) {
    double observed;
    return value.known && !value.source.empty() && end >= begin &&
        value.observedAtMs >= begin && value.observedAtMs <= end &&
        std::isfinite(utcBegin) && std::isfinite(utcEnd) && utcEnd >= utcBegin &&
        parseUtc(value.observedAt, observed) && observed >= utcBegin && observed <= utcEnd;
}

void physicalState(JsonDocument& report, const SensorSnapshot& sample, const std::string& capability,
                   uint64_t begin, uint64_t end, double utcBegin, double utcEnd,
                   const StatusFallback& fallback) {
    JsonObject state = report["physical_state"].to<JsonObject>();
    state["door_closed"] = nullptr;
    state["light_on"] = nullptr;
    if (fallback.detectsFaults) state["sensor_fault"] = nullptr;
    else state["sensor_fault"] = false;
    if (fallback.sensesActuator) state["actuator_state"] = "unknown";
    else state["actuator_state"] = fallback.commandActive ? "busy" : "idle";
    auto fresh = [&](const BoolObservation& value) {
        return freshObservation(value, begin, end, utcBegin, utcEnd);
    };
    if (capability == "locker" && fresh(sample.doorClosed)) state["door_closed"] = sample.doorClosed.value;
    if (capability == "light" && fresh(sample.lightOn)) state["light_on"] = sample.lightOn.value;
    if (fresh(sample.sensorFault)) state["sensor_fault"] = sample.sensorFault.value;
    if (fresh(sample.actuatorIdle)) state["actuator_state"] = sample.actuatorIdle.value ? "idle" : "busy";
}

}  // namespace np
