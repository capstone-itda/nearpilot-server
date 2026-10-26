#pragma once
// Device adapters emit one control-signal output. Completion means the output finished,
// not an observed physical state (FR-07, architecture.md §5 MQTT 5절).
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

}  // namespace np
