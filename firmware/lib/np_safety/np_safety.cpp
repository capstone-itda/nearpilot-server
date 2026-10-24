#include "np_safety.h"
#include "np_clock.h"
#include <cmath>

namespace np {
namespace {

std::string canonical(JsonDocument& request) {
    JsonDocument values;
    JsonArray fields = values.to<JsonArray>();
    for (const char* name : {"node_id", "criteria_id", "criteria_version", "target_session_id", "issued_at", "expires_at"})
        fields.add(request[name]);
    std::string text;
    serializeJson(values, text);
    return text;
}

bool valid(JsonDocument& doc, const CommandContext& context) {
    double issued, expires;
    return doc["schema_version"].is<int>() && doc["schema_version"].as<int>() == 1 &&
        doc["node_id"].is<const char*>() && doc["node_id"].as<std::string>() == context.nodeId &&
        doc["target_session_id"].is<const char*>() && !context.session.empty() && context.connected &&
        doc["target_session_id"].as<std::string>() == context.session &&
        doc["criteria_id"].is<const char*>() && !doc["criteria_id"].as<std::string>().empty() &&
        doc["criteria_version"].is<int>() && doc["criteria_version"].as<int>() > 0 &&
        doc["issued_at"].is<const char*>() && doc["expires_at"].is<const char*>() &&
        context.clockTrusted && std::isfinite(context.nowSec) && std::isfinite(context.maxErrorSec) && context.maxErrorSec >= 0 &&
        parseUtc(doc["issued_at"].as<std::string>(), issued) && parseUtc(doc["expires_at"].as<std::string>(), expires) &&
        issued < expires && issued <= context.nowSec + context.maxErrorSec && context.nowSec + context.maxErrorSec < expires;
}

bool supported(JsonDocument& doc, const std::string& capability) {
    return doc["criteria_version"].as<int>() == 1 &&
        (capability == "locker" || capability == "light" || capability == "speaker") &&
        doc["criteria_id"].as<std::string>() == capability + "-return";
}

}  // namespace

void SafetyChecks::result(JsonDocument& record, const char* outcome) {
    record["state"] = "final";
    record["outcome"] = outcome;
    record["event_id"] = port_.newEventId();
    record["checked_at"] = port_.utcNow();
}

bool SafetyChecks::recover(int slot, JsonDocument& record) {
    if (record["state"] == "accepted") {
        record["evidence"].to<JsonObject>();
        result(record, "unknown");
        if (measureJson(record) > maxRecordBytes_ || !ledger_.save(slot, record)) return false;
    }
    return record["state"] == "final";
}

void SafetyChecks::handle(const std::string& payload) {
    JsonDocument request;
    if (deserializeJson(request, payload) || !request["check_id"].is<const char*>()) {
        port_.log("SAFETY_CHECK_INVALID no_measurement");
        return;
    }
    std::string id = request["check_id"].as<std::string>();
    if (id.empty() || id.size() > maxIdBytes_) return;
    JsonDocument record;
    std::string content = canonical(request);
    int empty;
    int slot = ledger_.find(id, record, empty);
    // No measurement or conflicting response for an existing ID, even after reconnect.
    if (slot >= 0) {
        if (record["content"].as<std::string>() != content) {
            port_.log("SAFETY_CHECK_CONFLICT existing_record_kept");
            return;
        }
        if (port_.context().clockTrusted && recover(slot, record)) port_.publish(record);
        return;
    }
    if (slot == Ledger::STORAGE_ERROR || !valid(request, port_.context())) {
        port_.log("SAFETY_CHECK_BLOCKED no_measurement");
        return;
    }
    bool replace = empty < 0;
    if (replace) {
        CommandContext context = port_.context();
        empty = ledger_.replacement(context.clockTrusted, context.nowSec, context.maxErrorSec, 5);
    }
    if (empty < 0) {
        port_.log("SAFETY_CHECK_STORAGE_FULL no_measurement");
        return;
    }
    record["check_id"] = id;
    record["content"] = content;
    record["created_at"] = port_.utcNow();
    record["criteria_id"] = request["criteria_id"];
    record["criteria_version"] = request["criteria_version"];
    record["state"] = "accepted";
    // Preflight the largest v1 result before touching flash or reading sensors.
    JsonDocument largest;
    largest.set(record);
    largest["state"] = "final";
    largest["outcome"] = "not_ready";
    largest["event_id"] = std::string(36, 'x');
    largest["checked_at"] = "2026-10-24T00:00:00.000000Z";
    for (const char* key : {"door_closed", "locked"}) {
        largest["evidence"][key]["value"] = false;
        largest["evidence"][key]["observed_at"] = "2026-10-24T00:00:00.000000Z";
    }
    if (measureJson(largest) > maxRecordBytes_) {
        port_.log("SAFETY_CHECK_RECORD_TOO_LARGE no_measurement");
        return;
    }
    if (!(replace ? ledger_.save(empty, record) : ledger_.create(empty, record))) {
        port_.log("SAFETY_CHECK_RECEIPT_STORE_FAILED no_measurement");
        return;
    }
    JsonObject evidence = record["evidence"].to<JsonObject>();
    const char* outcome = "unknown";
    CommandContext before = port_.context();
    if (valid(request, before) && supported(request, before.capability)) {
        Measurement measurement(id, port_.monotonicMs());
        SensorSnapshot sample = port_.sensors().read(measurement);
        uint64_t end = port_.monotonicMs();
        CommandContext after = port_.context();
        // Clock/session expiry can change during measurement. In that case no safe evidence.
        if (valid(request, after) && before.session == after.session && before.capability == after.capability) {
            bool all = true, missing = false;
            auto add = [&](const char* key, const BoolObservation& observed) {
                if (!freshObservation(observed, measurement.startedAtMs, end, before.nowSec, after.nowSec)) {
                    missing = true;
                    return;
                }
                evidence[key]["value"] = observed.value;
                evidence[key]["observed_at"] = observed.observedAt;
                all = all && observed.value;
            };
            if (before.capability == "locker") {
                add("door_closed", sample.doorClosed);
                add("locked", sample.locked);
            } else add("actuator_idle", sample.actuatorIdle);
            outcome = missing ? "unknown" : all ? "passed" : "not_ready";
            // A measured fault with provenance is distinct from a missing/failed read.
            if (freshObservation(sample.sensorFault, measurement.startedAtMs, end, before.nowSec, after.nowSec) &&
                sample.sensorFault.value) outcome = "fault";
        }
    }
    result(record, outcome);
    if (measureJson(record) <= maxRecordBytes_ && ledger_.save(empty, record)) port_.publish(record);
    else port_.log("SAFETY_CHECK_RESULT_STORE_FAILED no_result");
}

void SafetyChecks::replay() {
    if (!port_.context().clockTrusted) return;
    ledger_.forEach([this](int slot, JsonDocument& record) {
        if (recover(slot, record)) port_.publish(record);
    });
}

}  // namespace np
