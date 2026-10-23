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
    if (rejection) {
        port_.publish(record);
        return;
    }
    busy_ = true;
    activeSlot_ = empty;
    activeRecord_.set(record);
    activeDevice_ = &port_.device();
    activePolicy_ = activeDevice_->policy;
    activeRequest_ = {id, command["action"].as<std::string>(), 0};
    port_.publish(record);
    // Recheck after the flash write and receipt publish, immediately before output.
    rejection = port_.validate(command);
    if (rejection) return finish("failed", rejection);
    activeRequest_.startedAtMs = port_.monotonicMs();
    StartResult started = activeDevice_->actuator.start(activeRequest_);
    if (started == StartResult::Failed) return finish("failed");
    if (started != StartResult::Started) return finish("unknown");
    JsonDocument progress;
    progress.set(activeRecord_);
    event(progress, "running");
    port_.publish(progress);
    if (!activePolicy_.timeoutMs || !activeDevice_->observer.available()) finish("unknown");
}

void Executor::finish(const char* state, const char* error) {
    event(activeRecord_, state, error);
    if (ledger_.save(activeSlot_, activeRecord_)) port_.publish(activeRecord_);
    else port_.log("RESULT_STORE_FAILED accepted_record_kept_no_retry");
    busy_ = false;
    activeSlot_ = -1;
    activeDevice_ = nullptr;
    activeRecord_.clear();
    activeRequest_ = {};
}

void Executor::poll() {
    if (!busy_) return;
    if (!activeDevice_->observer.available()) return finish("unknown");
    uint64_t now = port_.monotonicMs();
    if (now < activeRequest_.startedAtMs || now - activeRequest_.startedAtMs >= activePolicy_.timeoutMs)
        return finish("unknown");
    CompletionObservation observation = activeDevice_->observer.observe(activeRequest_);
    // An observer may have consumed time or become unavailable during this call.
    now = port_.monotonicMs();
    if (now < activeRequest_.startedAtMs || now - activeRequest_.startedAtMs >= activePolicy_.timeoutMs)
        return finish("unknown");
    if (observation.state == ObservationState::Pending) return;
    if (observation.state == ObservationState::Unavailable) return finish("unknown");
    if (observation.commandId != activeRequest_.commandId || observation.source.empty() ||
        observation.observedAtMs <= activeRequest_.startedAtMs || observation.observedAtMs > now)
        return finish("unknown");
    if (observation.state == ObservationState::Failed) return finish("failed");
    finish(observation.state == ObservationState::Proven && activePolicy_.successEnabled ? "succeeded" : "unknown");
}

void Executor::replay() {
    ledger_.forEach([this](int slot, JsonDocument& record) {
        // Reconnection during a live execution is not power-loss recovery.
        if (slot != activeSlot_ && (record["state"].as<std::string>() == "accepted" ||
                                   record["state"].as<std::string>() == "running")) {
            event(record, "unknown");
            if (!ledger_.save(slot, record)) return;
        }
        port_.publish(record);  // Stable event ID and occurred_at; the port adds a new seq and sent_at.
    });
}

}  // namespace np
