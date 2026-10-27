#pragma once
// Device adapters emit one control-signal output. Completion means the output finished,
// not an observed physical state (FR-07, architecture.md §5 MQTT 5절).
#include <cstdint>
#include <string>

namespace np {

// light is set only for a light `on` that carried validated `parameters` (FR-09):
// brightness is the per-channel output limit and color is 0xRRGGBB before scaling.
struct Actuation {
    Actuation(const std::string& id = "", const std::string& requestedAction = "")
        : commandId(id), action(requestedAction), light(false), brightness(0), color(0) {}
    Actuation(const std::string& id, const std::string& requestedAction, uint8_t level, uint32_t rgb)
        : commandId(id), action(requestedAction), light(true), brightness(level), color(rgb) {}
    std::string commandId;
    std::string action;
    bool light;
    uint8_t brightness;
    uint32_t color;
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

// 0xRRGGBB for the light ring: on is white at the given level, off is dark.
inline uint32_t lightRingRgb(bool on, uint8_t level) {
    return on ? uint32_t(level) << 16 | uint32_t(level) << 8 | level : 0;
}

// Each channel is channel * brightness / 255, rounded down, so no channel exceeds brightness
// for any color (architecture.md §5 MQTT 3절). White at brightness equals lightRingRgb(true, brightness).
inline uint32_t lightRingScaled(uint32_t rgb, uint8_t brightness) {
    uint32_t out = 0;
    for (int shift = 16; shift >= 0; shift -= 8)
        out |= ((rgb >> shift & 0xFF) * brightness / 255) << shift;
    return out;
}

// PWM duty for a pulse width at the given frequency and duty resolution.
inline uint32_t servoDuty(unsigned pulseUs, unsigned frequencyHz, unsigned bits) {
    uint64_t full = (uint64_t(1) << bits) - 1;
    return uint32_t(full * pulseUs * frequencyHz / 1000000ULL);
}

// DFPlayer Mini serial commands used by the speaker. Sent without an acknowledgement request.
const uint8_t kDfplayerVolume = 0x06;
const uint8_t kDfplayerStop = 0x16;
const uint8_t kDfplayerLoopFolder = 0x17;
const unsigned kDfplayerFrameBytes = 10;

// One DFPlayer frame: start, version, length, command, no-ack, parameter, checksum, end.
inline void dfplayerFrame(uint8_t command, uint16_t parameter, uint8_t frame[kDfplayerFrameBytes]) {
    frame[0] = 0x7E;
    frame[1] = 0xFF;
    frame[2] = 0x06;
    frame[3] = command;
    frame[4] = 0x00;
    frame[5] = uint8_t(parameter >> 8);
    frame[6] = uint8_t(parameter);
    uint16_t sum = 0;
    for (unsigned i = 1; i <= 6; ++i) sum += frame[i];
    uint16_t checksum = uint16_t(0 - sum);
    frame[7] = uint8_t(checksum >> 8);
    frame[8] = uint8_t(checksum);
    frame[9] = 0xEF;
}

}  // namespace np
