#pragma once
// Device adapters emit one control-signal output. Completion means the output finished,
// not an observed physical state (FR-07, architecture.md §5 MQTT 5절).
#include <cstdint>
#include <string>

namespace np {

struct Actuation {
    Actuation(const std::string& id = "", const std::string& requestedAction = "")
        : commandId(id), action(requestedAction) {}
    std::string commandId;
    std::string action;
};

enum class OutputResult { Completed, Failed, Unknown };

class Actuator {
public:
    virtual ~Actuator() = default;
    // Must return promptly. Completed: the whole control signal was emitted.
    // Failed: the output verifiably did not happen. Unknown: neither can be confirmed.
    virtual OutputResult output(const Actuation& request) = 0;
};

// Existing serial / optional GPIO output, shared by locker, light and speaker.
class SignalActuator : public Actuator {
public:
    using Output = void (*)(const Actuation&);
    explicit SignalActuator(Output output) : output_(output) {}
    OutputResult output(const Actuation& request) override {
        if (!output_) return OutputResult::Unknown;
        output_(request);
        return OutputResult::Completed;
    }
private:
    Output output_;
};

// Hobby servo pulse width for an angle, clamped to 0..180 degrees.
inline unsigned servoPulseUs(int degrees, unsigned minUs, unsigned maxUs) {
    if (degrees < 0) degrees = 0;
    if (degrees > 180) degrees = 180;
    return minUs + (maxUs - minUs) * unsigned(degrees) / 180;
}

// PWM duty for a pulse width at the given frequency and duty resolution.
inline uint32_t servoDuty(unsigned pulseUs, unsigned frequencyHz, unsigned bits) {
    uint64_t full = (uint64_t(1) << bits) - 1;
    return uint32_t(full * pulseUs * frequencyHz / 1000000ULL);
}

// Returns the locker servo to the locked angle once the open hold passes.
// A local timer, not a command: reboot and replay never reopen (FR-06).
class RelockTimer {
public:
    explicit RelockTimer(unsigned long holdMs) : holdMs_(holdMs) {}
    void opened(unsigned long nowMs) {
        openedAt_ = nowMs;
        pending_ = true;
    }
    // True once per open, when the hold has passed. millis() wrap-safe.
    bool due(unsigned long nowMs) {
        if (!pending_ || nowMs - openedAt_ < holdMs_) return false;
        pending_ = false;
        return true;
    }
    bool pending() const { return pending_; }
private:
    unsigned long holdMs_;
    unsigned long openedAt_ = 0;
    bool pending_ = false;
};

}  // namespace np
