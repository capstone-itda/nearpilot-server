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
    if (slot == Ledger::STORAGE_ERROR)
        return reject(record, "STORAGE_ERROR", "STORAGE_BLOCKED no_output_no_record_deletion");
    const char* rejection = (receivedBusy || busy_) ? "DEVICE_BUSY" : port_.validate(command);
    bool replace = false;
    if (empty < 0) {
        // Invalid/expired incoming requests never consume an old record just to store a rejection.
        const char* invalid = (receivedBusy || busy_) ? port_.validate(command) : rejection;
        if (invalid) return reject(record, invalid, "COMMAND_REJECTED no_record_replacement");
        CommandContext context = port_.retentionContext();
        empty = ledger_.replacement(context.clockTrusted, context.nowSec, context.maxErrorSec, 4);
        replace = true;
    }
    if (empty < 0) return reject(record, "STORAGE_ERROR", "STORAGE_BLOCKED no_replaceable_record");
    record["created_at"] = port_.utcNow();
    event(record, rejection ? "rejected" : "accepted", rejection);
    // Room for the longest error code a result may add. An oversized record is not stored:
    // the same content is always rejected the same way, so no record is needed to repeat it.
    if (measureJson(record) + (rejection ? 0 : ERROR_CODE_ROOM) > maxRecordBytes_)
        return reject(record, "INVALID_MESSAGE", "RECORD_TOO_LARGE not_recorded");
    if (!(replace ? ledger_.save(empty, record) : ledger_.create(empty, record)))
        return reject(record, "STORAGE_ERROR", "STORAGE_WRITE_FAILED no_output");
    if (rejection) {
        port_.publish(record);
        return;
    }
    busy_ = true;
    activeSlot_ = empty;
    activeRecord_.set(record);
    port_.publish(record);
    // Recheck after the flash write and receipt publish, immediately before output.
    rejection = port_.validate(command);
    if (rejection) return finish("failed", rejection);
    JsonDocument progress;
    progress.set(activeRecord_);
    event(progress, "running");
    port_.publish(progress);  // Not stored: a restart before the result restores the receipt as unknown.
    OutputResult output = port_.actuator().output(commandActuation(id, command));
    finish(output == OutputResult::Completed ? "succeeded" : output == OutputResult::Failed ? "failed" : "unknown");
}

void Executor::finish(const char* state, const char* error) {
    event(activeRecord_, state, error);
    // Success is published only after the result is stored and read back.
    if (ledger_.save(activeSlot_, activeRecord_)) port_.publish(activeRecord_);
    else port_.log("RESULT_STORE_FAILED accepted_record_kept_no_retry");
    busy_ = false;
    activeSlot_ = -1;
    activeRecord_.clear();
}

void Executor::replayRecord(int slot, JsonDocument& record) {
    // Reconnection during a live execution is not power-loss recovery.
    if (slot != activeSlot_ && (record["state"].as<std::string>() == "accepted" ||
                               record["state"].as<std::string>() == "running")) {
        event(record, "unknown");
        if (!ledger_.save(slot, record)) return;
    }
    port_.publish(record);  // Stable event ID and occurred_at; the port adds a new seq and sent_at.
}

void Executor::replay() {
    beginReplay();
    while (replayNext()) {
    }
}

bool Executor::replayNext() {
    if (replayCursor_ < 0) return false;
    JsonDocument record;
    int slot = ledger_.next(replayCursor_, record);
    if (slot < 0) {
        replayCursor_ = -1;
        return false;
    }
    replayCursor_ = slot + 1;
    replayRecord(slot, record);
    return true;
}

}  // namespace np
