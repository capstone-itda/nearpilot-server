#pragma once
// Device adapters supply command-specific evidence, never a guessed physical state (FR-07).
#include <cstdint>
#include <string>

namespace np {

struct Actuation {
    Actuation(const std::string& id = "", const std::string& requestedAction = "", uint64_t started = 0)
        : commandId(id), action(requestedAction), startedAtMs(started) {}
    std::string commandId;
    std::string action;
    uint64_t startedAtMs = 0;  // Monotonic time, in this boot only.
};

enum class StartResult { Started, Failed, Unknown };
enum class ObservationState { Pending, Proven, Failed, Unavailable };

struct CompletionObservation {
    CompletionObservation(ObservationState result = ObservationState::Unavailable,
                          const std::string& id = "", uint64_t observed = 0,
                          const std::string& evidenceSource = "")
        : state(result), commandId(id), observedAtMs(observed), source(evidenceSource) {}
    ObservationState state = ObservationState::Unavailable;
    std::string commandId;
    uint64_t observedAtMs = 0;  // Same monotonic clock as Actuation.
    std::string source;
};

class Actuator {
public:
    virtual ~Actuator() = default;
    // Must return promptly. Started confirms start, not physical completion.
    virtual StartResult start(const Actuation& request) = 0;
};

class CompletionObserver {
public:
    virtual ~CompletionObserver() = default;
    virtual bool available() const = 0;
    // Must return promptly. Proven/Failed must refer to this action, not just a target state.
    // Read failures return Unavailable; no observation yet returns Pending.
    virtual CompletionObservation observe(const Actuation& request) = 0;
};

struct CompletionPolicy {
    CompletionPolicy(bool enabled = false, uint64_t timeout = 0)
        : successEnabled(enabled), timeoutMs(timeout) {}
    bool successEnabled = false;  // Enable each feature only after #142 and hardware validation.
    uint64_t timeoutMs = 0;  // 0: unmeasured/unset. No completion wait or success is allowed.
};

struct Device {
    Actuator& actuator;
    CompletionObserver& observer;
    CompletionPolicy policy;
};

class NoCompletionObserver : public CompletionObserver {
public:
    bool available() const override { return false; }
    CompletionObservation observe(const Actuation&) override { return {}; }
};

// Existing serial / optional GPIO output, shared by the three unverified device slots.
class SignalActuator : public Actuator {
public:
    using Output = void (*)(const Actuation&);
    explicit SignalActuator(Output output) : output_(output) {}
    StartResult start(const Actuation& request) override {
        if (!output_) return StartResult::Unknown;
        output_(request);
        return StartResult::Started;
    }
private:
    Output output_;
};

class SignalDevices {
public:
    SignalDevices(SignalActuator::Output output, uint64_t lockerTimeoutMs,
                  uint64_t lightTimeoutMs, uint64_t speakerTimeoutMs)
        : actuator_(output), locker{actuator_, observer_, {false, lockerTimeoutMs}},
          light{actuator_, observer_, {false, lightTimeoutMs}},
          speaker{actuator_, observer_, {false, speakerTimeoutMs}} {}

private:
    SignalActuator actuator_;
    NoCompletionObserver observer_;
public:
    Device locker;
    Device light;  // LED and IR: neither has a completion observation yet.
    Device speaker;
};

}  // namespace np
