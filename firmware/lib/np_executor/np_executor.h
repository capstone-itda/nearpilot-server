#pragma once
// Command state transitions over the ledger (FR-06, FR-07, NFR-03, architecture.md §5 MQTT 4·5절).
// Lookup -> busy/validity -> persisted receipt -> recheck -> output -> persisted result.
#include <ArduinoJson.h>
#include <string>

#include "np_ledger.h"

namespace np {

// Board side of command handling. Every call is synchronous.
class CommandPort {
public:
    virtual ~CommandPort() = default;
    // Live checks of version, target connection, action, clock trust and validity window.
    virtual const char* validate(JsonDocument& command) = 0;
    virtual std::string newEventId() = 0;
    virtual std::string utcNow() = 0;
    virtual void publish(JsonDocument& record) = 0;
    // Sends the control signal only. It is never completion evidence.
    virtual void actuate(const std::string& commandId, const std::string& action) = 0;
    virtual void log(const std::string& line) = 0;
};

class Executor {
public:
    // maxRecordBytes bounds a stored record including a later error code, so the store's
    // slot count can be sized for the worst case.
    Executor(Ledger& ledger, CommandPort& port, size_t maxIdBytes, size_t maxRecordBytes)
        : ledger_(ledger), port_(port), maxIdBytes_(maxIdBytes), maxRecordBytes_(maxRecordBytes) {}

    // receivedBusy: the message arrived while another command was being handled.
    void handle(const std::string& payload, bool receivedBusy);

    // Re-reports every readable record. A restored receipt without a result becomes unknown.
    void replay();

    bool busy() const { return busy_; }

private:
    void event(JsonDocument& record, const char* state, const char* error = nullptr);
    void reject(JsonDocument& record, const char* code, const char* note);

    Ledger& ledger_;
    CommandPort& port_;
    size_t maxIdBytes_;
    size_t maxRecordBytes_;
    bool busy_ = false;
};

}  // namespace np
