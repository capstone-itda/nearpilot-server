#pragma once
// The device declaration built into the firmware (nearpilot.device/1, docs/device-protocol.md 3·4절) and
// the checks the firmware runs with it: its own declaration at boot, every command's action and arguments
// before output (FR-06, architecture.md §5 MQTT 3절). The server checks the same rules; the device checks
// again because it is the last step before the output.
#include <ArduinoJson.h>
#include <string>

namespace np {

constexpr const char* DESCRIPTOR_FORMAT = "nearpilot.device/1";
// Whole `announce` body (device-protocol.md 4절).
constexpr size_t ANNOUNCE_MAX_BYTES = 2048;

class Descriptor {
public:
    // Parses `json` and checks the format version, names, occupancy, roles, schema keywords, argument types
    // and the `pattern` subset. Returns nullptr when usable, else a short reason for the serial log.
    const char* load(const char* json);
    bool loaded() const { return loaded_; }
    const std::string& kind() const { return kind_; }
    // The whole declaration, as announced.
    JsonObjectConst json() const { return doc_.as<JsonObjectConst>(); }
    // The action's declaration, or a null object when the declaration has no such action.
    JsonObjectConst action(const std::string& name) const;
    // `maximum` of one argument of one action; false when the action, the argument or the bound is absent.
    bool maximum(const std::string& action, const char* argument, double& out) const;

private:
    JsonDocument doc_;
    std::string kind_;
    bool loaded_ = false;
};

// Largest `announce` body this node can send with `descriptor`: the longest session id, sequence number and
// timestamp the firmware writes. Compared with ANNOUNCE_MAX_BYTES once at boot.
size_t announceBytes(const Descriptor& descriptor, const std::string& nodeId);

// nullptr when `arguments` (null = none) fits the action's `input_schema`, else "UNSUPPORTED_PARAMETER".
// JSON Schema meaning: undeclared keys are allowed unless `additionalProperties` is false.
const char* validateArguments(JsonObjectConst action, JsonVariantConst arguments);

// `pattern` in this firmware's regex subset (spec decision 4): `^`, `$` at the ends, ASCII literals,
// `\` escapes and `\d`, `.`, `[...]` with ranges and `^`, and the quantifiers `?`, `*`, `+`, `{n}`, `{n,}`,
// `{n,m}`. Without `^` the pattern may match anywhere, as in JSON Schema.
bool patternSupported(const std::string& pattern);
// false also when the pattern is outside the subset.
bool patternMatches(const std::string& pattern, const std::string& text);

}  // namespace np
