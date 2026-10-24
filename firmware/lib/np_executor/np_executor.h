#pragma once
// Command state transitions over the ledger (FR-06, FR-07, NFR-03, architecture.md §5 MQTT 4·5절).
// Lookup -> busy/validity -> persisted receipt -> recheck -> output -> persisted result.
#include <ArduinoJson.h>
#include <string>

#include "np_ledger.h"
#include "np_device.h"
#include "np_command.h"

namespace np {

// Board side of command handling. Calls must return promptly.
class CommandPort {
public:
    virtual ~CommandPort() = default;
    // Live checks of version, target connection, action, clock trust and validity window.
    virtual const char* validate(JsonDocument& command) = 0;
    virtual std::string newEventId() = 0;
    virtual std::string utcNow() = 0;
    virtual void publish(JsonDocument& record) = 0;
    virtual Device& device() = 0;
    virtual uint64_t monotonicMs() = 0;
    virtual void log(const std::string& line) = 0;
    // Unset by default: adapters must explicitly provide a verified clock for reclamation.
    virtual CommandContext retentionContext() { return {}; }
};

class Executor {
public:
    // maxRecordBytes bounds a stored record including a later error code, so the store's
    // slot count can be sized for the worst case.
    Executor(Ledger& ledger, CommandPort& port, size_t maxIdBytes, size_t maxRecordBytes)
        : ledger_(ledger), port_(port), maxIdBytes_(maxIdBytes), maxRecordBytes_(maxRecordBytes) {}

    // receivedBusy: the message arrived while another command was being handled.
    void handle(const std::string& payload, bool receivedBusy);

    // Poll once from loop(); no waiting or automatic restart of the actuator.
    void poll();

    // Restored receipts become unknown; a live execution is kept intact on reconnect.
    void replay();

    // replay() spread over loop iterations: one readable record per replayNext() call.
    // replayNext() returns false when nothing is left; beginReplay() starts again from the first slot.
    void beginReplay() { replayCursor_ = 0; }
    bool replayNext();

    bool busy() const { return busy_; }

private:
    void event(JsonDocument& record, const char* state, const char* error = nullptr);
    void reject(JsonDocument& record, const char* code, const char* note);
    void finish(const char* state, const char* error = nullptr);
    void replayRecord(int slot, JsonDocument& record);

    Ledger& ledger_;
    CommandPort& port_;
    size_t maxIdBytes_;
    size_t maxRecordBytes_;
    bool busy_ = false;
    int activeSlot_ = -1;
    JsonDocument activeRecord_;
    Actuation activeRequest_;
    Device* activeDevice_ = nullptr;
    CompletionPolicy activePolicy_;
    int replayCursor_ = -1;
};

}  // namespace np
