#include "np_descriptor.h"

#include <cstring>
#include <vector>

namespace np {

namespace {

// docs/device-protocol.md 4절. Fixed values, the same on every device and on the server.
const size_t MAX_ACTIONS = 8;
const size_t MAX_NAME_BYTES = 32;
const size_t MAX_DESCRIPTION_BYTES = 120;
const size_t MAX_ARGUMENTS = 8;
const size_t MAX_ENUM_ITEMS = 16;
const size_t MAX_PATTERN_BYTES = 64;
const char* const UNSUPPORTED = "UNSUPPORTED_PARAMETER";

bool hasKey(JsonObjectConst object, const char* key) {
    for (JsonPairConst pair : object)
        if (std::strcmp(pair.key().c_str(), key) == 0) return true;
    return false;
}

// Every key of `object` is one of `allowed` (null-terminated list).
bool keysWithin(JsonObjectConst object, const char* const* allowed) {
    for (JsonPairConst pair : object) {
        bool known = false;
        for (const char* const* name = allowed; *name && !known; ++name) known = std::strcmp(pair.key().c_str(), *name) == 0;
        if (!known) return false;
    }
    return true;
}

bool isName(JsonVariantConst value) {
    if (!value.is<const char*>()) return false;
    const std::string text = value.as<std::string>();
    if (text.empty() || text.size() > MAX_NAME_BYTES || text[0] < 'a' || text[0] > 'z') return false;
    for (char ch : text)
        if (!((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '_')) return false;
    return true;
}

bool isText(JsonVariantConst value) { return value.is<const char*>() && value.as<std::string>().size() > 0; }

bool isNumber(JsonVariantConst value) { return !value.is<bool>() && value.is<double>(); }

bool isInteger(JsonVariantConst value) { return !value.is<bool>() && value.is<long>(); }

bool isScalar(JsonVariantConst value) {
    return !value.isNull() && !value.is<JsonObjectConst>() && !value.is<JsonArrayConst>();
}

size_t codePoints(const std::string& text) {
    size_t count = 0;
    for (unsigned char ch : text) count += (ch & 0xC0) != 0x80;
    return count;
}

bool sameJson(JsonVariantConst a, JsonVariantConst b) {
    std::string left, right;
    serializeJson(a, left);
    serializeJson(b, right);
    return left == right;
}

const char* const ARGUMENT_KEYS[] = {"type", "minimum", "maximum", "enum", "pattern", "maxLength", nullptr};

bool argumentSchemaValid(JsonVariantConst value) {
    if (!value.is<JsonObjectConst>()) return false;
    JsonObjectConst schema = value.as<JsonObjectConst>();
    if (!keysWithin(schema, ARGUMENT_KEYS) || !schema["type"].is<const char*>()) return false;
    const std::string type = schema["type"].as<std::string>();
    const bool numeric = type == "integer" || type == "number";
    if (!numeric && type != "string" && type != "boolean") return false;  // Flat values only (depth 1).
    for (const char* bound : {"minimum", "maximum"})
        if (hasKey(schema, bound) && (!numeric || !isNumber(schema[bound]))) return false;
    if (hasKey(schema, "pattern")) {
        if (type != "string" || !schema["pattern"].is<const char*>()) return false;
        const std::string pattern = schema["pattern"].as<std::string>();
        if (pattern.size() > MAX_PATTERN_BYTES || !patternSupported(pattern)) return false;
    }
    if (hasKey(schema, "maxLength") && (type != "string" || !isInteger(schema["maxLength"]) ||
                                        schema["maxLength"].as<long>() < 0)) return false;
    if (hasKey(schema, "enum")) {
        if (!schema["enum"].is<JsonArrayConst>()) return false;
        JsonArrayConst items = schema["enum"].as<JsonArrayConst>();
        if (items.size() == 0 || items.size() > MAX_ENUM_ITEMS) return false;
        for (JsonVariantConst item : items)
            if (!isScalar(item)) return false;
    }
    return true;
}

const char* const OBJECT_KEYS[] = {"type", "properties", "required", "additionalProperties", nullptr};

bool inputSchemaValid(JsonVariantConst value) {
    if (!value.is<JsonObjectConst>()) return false;
    JsonObjectConst schema = value.as<JsonObjectConst>();
    if (!keysWithin(schema, OBJECT_KEYS) || schema["type"] != "object") return false;
    JsonVariantConst properties = schema["properties"];
    if (!properties.isNull()) {
        if (!properties.is<JsonObjectConst>() || properties.as<JsonObjectConst>().size() > MAX_ARGUMENTS) return false;
        for (JsonPairConst pair : properties.as<JsonObjectConst>())
            if (!argumentSchemaValid(pair.value())) return false;
    }
    JsonVariantConst required = schema["required"];
    if (!required.isNull()) {
        if (!required.is<JsonArrayConst>()) return false;
        for (JsonVariantConst name : required.as<JsonArrayConst>())
            if (!name.is<const char*>()) return false;
    }
    JsonVariantConst extra = schema["additionalProperties"];
    return extra.isNull() || extra.is<bool>();
}

// A role that takes no arguments: no declared properties and no undeclared ones.
bool takesNoArguments(JsonObjectConst schema) {
    JsonVariantConst properties = schema["properties"];
    return (properties.isNull() || properties.as<JsonObjectConst>().size() == 0) &&
           schema["additionalProperties"].is<bool>() && !schema["additionalProperties"].as<bool>();
}

const char* const DESCRIPTOR_KEYS[] = {"format", "kind", "occupancy", "vendor", "actions", nullptr};
const char* const VENDOR_KEYS[] = {"name", "model", "firmware", nullptr};
const char* const ACTION_KEYS[] = {"name", "description", "role", "input_schema", "result", nullptr};

// ---- regex subset (spec decision 4) ----

const unsigned UNBOUNDED = ~0u;

struct Atom {
    Atom() : any(false), literal(0), negate(false), min(1), max(1) {}
    bool any;
    char literal;           // 0 for `.` and classes.
    std::string ranges;     // Class as pairs: "azAZ09".
    bool negate;
    unsigned min, max;
};

struct Pattern {
    Pattern() : anchorStart(false), anchorEnd(false) {}
    std::vector<Atom> atoms;
    bool anchorStart, anchorEnd;
};

bool isMeta(char ch) { return std::strchr("^$.[]{}()*+?|\\/-", ch) != nullptr; }

bool readNumber(const std::string& text, size_t& i, unsigned& out) {
    size_t start = i;
    unsigned long value = 0;
    while (i < text.size() && text[i] >= '0' && text[i] <= '9') {
        value = value * 10 + unsigned(text[i++] - '0');
        if (value > 1000) return false;
    }
    out = unsigned(value);
    return i > start;
}

bool parseClass(const std::string& text, size_t& i, Atom& atom) {
    ++i;  // '['
    if (i < text.size() && text[i] == '^') {
        atom.negate = true;
        ++i;
    }
    bool first = true;
    while (i < text.size() && (text[i] != ']' || first)) {
        first = false;
        char low = text[i];
        if (low == '\\') {
            if (++i >= text.size() || !isMeta(text[i])) return false;
            low = text[i];
        } else if (low == '[' || low == ']') {
            return false;
        }
        ++i;
        char high = low;
        if (i + 1 < text.size() && text[i] == '-' && text[i + 1] != ']') {
            high = text[i + 1];
            if (high == '\\' || high == '[' || high > 0x7E || high < low) return false;
            i += 2;
        }
        if (low < 0x20 || low > 0x7E) return false;
        atom.ranges += low;
        atom.ranges += high;
    }
    if (i >= text.size() || atom.ranges.empty()) return false;
    ++i;  // ']'
    return true;
}

bool parsePattern(const std::string& text, Pattern& out) {
    size_t i = 0;
    if (i < text.size() && text[i] == '^') {
        out.anchorStart = true;
        ++i;
    }
    while (i < text.size()) {
        const char ch = text[i];
        if (ch == '$') {
            if (i + 1 != text.size()) return false;
            out.anchorEnd = true;
            break;
        }
        Atom atom;
        if (ch == '\\') {
            if (++i >= text.size()) return false;
            if (text[i] == 'd') atom.ranges = "09";
            else if (isMeta(text[i])) atom.literal = text[i];
            else return false;
            ++i;
        } else if (ch == '.') {
            atom.any = true;
            ++i;
        } else if (ch == '[') {
            if (!parseClass(text, i, atom)) return false;
        } else if (std::strchr("^]{}()*+?|", ch) || static_cast<unsigned char>(ch) < 0x20 ||
                   static_cast<unsigned char>(ch) > 0x7E) {
            return false;
        } else {
            atom.literal = ch;
            ++i;
        }
        if (i < text.size()) {
            const char q = text[i];
            if (q == '?' || q == '*' || q == '+') {
                atom.min = q == '+' ? 1 : 0;
                atom.max = q == '?' ? 1 : UNBOUNDED;
                ++i;
            } else if (q == '{') {
                ++i;
                if (!readNumber(text, i, atom.min)) return false;
                atom.max = atom.min;
                if (i < text.size() && text[i] == ',') {
                    ++i;
                    atom.max = UNBOUNDED;
                    if (i < text.size() && text[i] != '}' && (!readNumber(text, i, atom.max) || atom.max < atom.min))
                        return false;
                }
                if (i >= text.size() || text[i] != '}') return false;
                ++i;
            }
            // A second quantifier (`a**`, lazy `a*?`) is outside the subset.
            if (i < text.size() && std::strchr("?*+{", text[i])) return false;
        }
        out.atoms.push_back(atom);
    }
    return true;
}

// Bytes of the UTF-8 character at `pos` (1 for ASCII and stray bytes).
size_t charBytes(const std::string& text, size_t pos) {
    const unsigned char lead = text[pos];
    size_t bytes = lead >= 0xF0 ? 4 : lead >= 0xE0 ? 3 : lead >= 0xC0 ? 2 : 1;
    return pos + bytes > text.size() ? 1 : bytes;
}

bool matchOne(const Atom& atom, const std::string& text, size_t pos, size_t& next) {
    if (pos >= text.size()) return false;
    const size_t bytes = charBytes(text, pos);
    next = pos + bytes;
    if (atom.any) return text[pos] != '\n';
    if (atom.literal) return bytes == 1 && text[pos] == atom.literal;
    bool inClass = false;
    if (bytes == 1)
        for (size_t r = 0; r + 1 < atom.ranges.size() && !inClass; r += 2)
            inClass = text[pos] >= atom.ranges[r] && text[pos] <= atom.ranges[r + 1];
    return inClass != atom.negate;
}

bool matchFrom(const Pattern& pattern, size_t index, const std::string& text, size_t pos) {
    if (index == pattern.atoms.size()) return !pattern.anchorEnd || pos == text.size();
    const Atom& atom = pattern.atoms[index];
    std::vector<size_t> ends(1, pos);  // ends[k]: position after k repetitions, greedy first.
    size_t at = pos, next = 0;
    while (ends.size() - 1 < atom.max && matchOne(atom, text, at, next)) {
        at = next;
        ends.push_back(at);
    }
    for (size_t k = ends.size(); k-- > atom.min;)
        if (matchFrom(pattern, index + 1, text, ends[k])) return true;
    return false;
}

}  // namespace

bool patternSupported(const std::string& pattern) {
    Pattern parsed;
    return parsePattern(pattern, parsed);
}

bool patternMatches(const std::string& pattern, const std::string& text) {
    Pattern parsed;
    if (!parsePattern(pattern, parsed)) return false;
    if (parsed.anchorStart) return matchFrom(parsed, 0, text, 0);
    for (size_t start = 0; start <= text.size(); start += start < text.size() ? charBytes(text, start) : 1)
        if (matchFrom(parsed, 0, text, start)) return true;
    return false;
}

const char* Descriptor::load(const char* json) {
    loaded_ = false;
    kind_.clear();
    doc_.clear();
    if (deserializeJson(doc_, json) || !doc_.is<JsonObjectConst>()) return "json";
    JsonObjectConst root = doc_.as<JsonObjectConst>();
    if (!keysWithin(root, DESCRIPTOR_KEYS)) return "unknown_key";
    if (root["format"] != DESCRIPTOR_FORMAT) return "format";
    if (!isName(root["kind"])) return "kind";
    const std::string occupancy = root["occupancy"] | "";
    if (occupancy != "rental" && occupancy != "shared") return "occupancy";
    JsonVariantConst vendor = root["vendor"];
    if (!vendor.is<JsonObjectConst>() || !keysWithin(vendor.as<JsonObjectConst>(), VENDOR_KEYS) ||
        !isText(vendor["name"]) || !isText(vendor["model"]) || !isText(vendor["firmware"])) return "vendor";
    if (!root["actions"].is<JsonArrayConst>()) return "actions";
    JsonArrayConst actions = root["actions"].as<JsonArrayConst>();
    if (actions.size() == 0 || actions.size() > MAX_ACTIONS) return "actions";
    std::vector<std::string> names;
    int deactivate = 0;
    for (JsonVariantConst item : actions) {
        if (!item.is<JsonObjectConst>()) return "action";
        JsonObjectConst action = item.as<JsonObjectConst>();
        if (!keysWithin(action, ACTION_KEYS) || !isName(action["name"])) return "action_name";
        const std::string name = action["name"].as<std::string>();
        for (const std::string& seen : names)
            if (seen == name) return "action_name";
        names.push_back(name);
        if (!isText(action["description"]) || action["description"].as<std::string>().size() > MAX_DESCRIPTION_BYTES)
            return "description";
        if (!inputSchemaValid(action["input_schema"])) return "input_schema";
        JsonVariantConst result = action["result"];
        if (!result.is<JsonObjectConst>() || result.as<JsonObjectConst>().size() != 1 || result["kind"] != "signal_output")
            return "result";
        if (hasKey(action, "role")) {
            const std::string role = action["role"] | "";
            if (role == "reoperate") {
                if (occupancy != "rental") return "role";
            } else if (role == "deactivate") {
                if (occupancy != "shared" || !takesNoArguments(action["input_schema"].as<JsonObjectConst>())) return "role";
                ++deactivate;
            } else {
                return "role";
            }
        }
    }
    if (occupancy == "shared" && deactivate != 1) return "role";
    kind_ = root["kind"].as<std::string>();
    loaded_ = true;
    return nullptr;
}

JsonObjectConst Descriptor::action(const std::string& name) const {
    if (loaded_)
        for (JsonVariantConst item : doc_["actions"].as<JsonArrayConst>())
            if (item["name"] == name) return item.as<JsonObjectConst>();
    return JsonObjectConst();
}

size_t announceBytes(const Descriptor& descriptor, const std::string& nodeId) {
    JsonDocument doc;
    doc["schema_version"] = 3;
    doc["node_id"] = nodeId;
    doc["session_id"] = "00000000-0000-0000-0000-000000000000";
    doc["seq"] = 4294967295UL;
    doc["sent_at"] = "2026-01-01T00:00:00.000000Z";  // utcNow() in main.cpp: microseconds.
    doc["descriptor"] = descriptor.json();
    return measureJson(doc);
}

bool Descriptor::maximum(const std::string& name, const char* argument, double& out) const {
    JsonVariantConst bound = action(name)["input_schema"]["properties"][argument]["maximum"];
    if (!isNumber(bound)) return false;
    out = bound.as<double>();
    return true;
}

namespace {

bool valueFits(JsonObjectConst schema, JsonVariantConst value) {
    const std::string type = schema["type"].as<std::string>();
    if (type == "integer" ? !isInteger(value) : type == "number" ? !isNumber(value) :
        type == "string" ? !value.is<const char*>() : !value.is<bool>()) return false;
    if (hasKey(schema, "minimum") && value.as<double>() < schema["minimum"].as<double>()) return false;
    if (hasKey(schema, "maximum") && value.as<double>() > schema["maximum"].as<double>()) return false;
    if (hasKey(schema, "enum")) {
        bool listed = false;
        for (JsonVariantConst item : schema["enum"].as<JsonArrayConst>()) listed = listed || sameJson(item, value);
        if (!listed) return false;
    }
    if (type == "string") {
        const std::string text = value.as<std::string>();
        if (hasKey(schema, "maxLength") && codePoints(text) > schema["maxLength"].as<size_t>()) return false;
        if (hasKey(schema, "pattern") && !patternMatches(schema["pattern"].as<std::string>(), text)) return false;
    }
    return true;
}

}  // namespace

const char* validateArguments(JsonObjectConst action, JsonVariantConst arguments) {
    if (action.isNull()) return UNSUPPORTED;
    JsonObjectConst schema = action["input_schema"].as<JsonObjectConst>();
    JsonObjectConst properties = schema["properties"].as<JsonObjectConst>();
    JsonObjectConst given;
    if (!arguments.isNull()) {
        if (!arguments.is<JsonObjectConst>()) return UNSUPPORTED;
        given = arguments.as<JsonObjectConst>();
    }
    const bool closed = schema["additionalProperties"].is<bool>() && !schema["additionalProperties"].as<bool>();
    for (JsonPairConst pair : given) {
        JsonVariantConst declared = properties[pair.key().c_str()];
        if (declared.isNull()) {
            if (closed || !isScalar(pair.value())) return UNSUPPORTED;
            continue;
        }
        if (!valueFits(declared.as<JsonObjectConst>(), pair.value())) return UNSUPPORTED;
    }
    for (JsonVariantConst name : schema["required"].as<JsonArrayConst>())
        if (!hasKey(given, name.as<const char*>())) return UNSUPPORTED;
    return nullptr;
}

}  // namespace np
