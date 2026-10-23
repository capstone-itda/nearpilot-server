#include "np_executor.h"

#include "np_command.h"

namespace np {

namespace {

constexpr size_t ERROR_CODE_ROOM = sizeof(",\"error_code\":\"COMMAND_ID_CONFLICT\"") - 1;

}  // namespace

void Executor::event(JsonDocument& record, const char* state, const char* error) {
    record["state"] = state;
    record["event_id"] = port_.newEventId();
    record["occurred_at"] = port_.utcNow();
    record.remove("error_code");
    if (error) record["error_code"] = error;
}

void Executor::reject(JsonDocument& record, const char* code, const char* note) {
    event(record, "rejected", code);
    port_.publish(record);
    port_.log(note);
}

void Executor::handle(const std::string& payload, bool receivedBusy) {
    JsonDocument command;
    if (deserializeJson(command, payload) || !command["command_id"].is<const char*>()) {
        port_.log("COMMAND_REJECTED invalid_json_or_id");
        return;
    }
    std::string id = command["command_id"].as<std::string>();
    if (id.empty() || id.size() > maxIdBytes_) {
        port_.log("COMMAND_REJECTED invalid_json_or_id");
        return;
    }
    std::string content = canonicalContent(command);
    JsonDocument record;
    int empty;
    int slot = ledger_.find(id, record, empty);
    port_.log("COMMAND_RECEIVED command_id=" + id);
    // Lookup precedes connection and expiry checks: old duplicates only get the stored result.
    if (slot >= 0) {
        if (record["content"].as<std::string>() != content) {
            JsonDocument conflict;
            conflict["command_id"] = id;
            conflict["content"] = content;
            reject(conflict, "COMMAND_ID_CONFLICT", "COMMAND_CONFLICT existing_record_kept");
        } else {
            port_.log("COMMAND_DUPLICATE no_new_output");
            port_.publish(record);
        }
        return;
    }
    record["command_id"] = id;
    record["content"] = content;
    if (slot == Ledger::STORAGE_ERROR || empty < 0)
        return reject(record, "STORAGE_ERROR", "STORAGE_BLOCKED no_output_no_record_deletion");
    const char* rejection = (receivedBusy || busy_) ? "DEVICE_BUSY" : port_.validate(command);
    event(record, rejection ? "rejected" : "accepted", rejection);
    // Room for the longest error code a result may add. An oversized record is not stored:
    // the same content is always rejected the same way, so no record is needed to repeat it.
    if (measureJson(record) + (rejection ? 0 : ERROR_CODE_ROOM) > maxRecordBytes_)
        return reject(record, "INVALID_MESSAGE", "RECORD_TOO_LARGE not_recorded");
    if (!ledger_.create(empty, record))
        return reject(record, "STORAGE_ERROR", "STORAGE_WRITE_FAILED no_output");
    port_.publish(record);
    if (rejection) return;
    busy_ = true;
    // Recheck after the flash write and publish, then once more right before output.
    rejection = port_.validate(command);
    if (!rejection) {
        JsonDocument progress;
        progress.set(record);
        event(progress, "running");
        port_.publish(progress);
        rejection = port_.validate(command);
        if (!rejection) port_.actuate(id, command["action"].as<std::string>());
    }
    // Not starting is a verified failure. Output without a completion sensor stays unknown.
    if (rejection) event(record, "failed", rejection);
    else event(record, "unknown");
    if (ledger_.save(empty, record)) port_.publish(record);
    else port_.log("RESULT_STORE_FAILED accepted_record_kept_no_retry");
    busy_ = false;
}

void Executor::replay() {
    ledger_.forEach([this](int slot, JsonDocument& record) {
        if (record["state"].as<std::string>() == "accepted") {
            event(record, "unknown");
            if (!ledger_.save(slot, record)) return;
        }
        port_.publish(record);  // Stable event ID and occurred_at; the port adds a new seq and sent_at.
    });
}

}  // namespace np
