#pragma once
// MQTT v1 return checks: durable receipt -> fresh sensor read -> durable result (FR-20).
#include "np_command.h"
#include "np_ledger.h"
#include "np_sensor.h"

namespace np {

class SafetyPort {
public:
    virtual ~SafetyPort() = default;
    virtual CommandContext context() = 0;
    virtual uint64_t monotonicMs() = 0;
    virtual std::string utcNow() = 0;
    virtual std::string newEventId() = 0;
    virtual Sensors& sensors() = 0;
    virtual void publish(JsonDocument& result) = 0;
    virtual void log(const std::string& line) = 0;
};

class SafetyChecks {
public:
    SafetyChecks(Ledger& ledger, SafetyPort& port, size_t maxIdBytes, size_t maxRecordBytes)
        : ledger_(ledger), port_(port), maxIdBytes_(maxIdBytes), maxRecordBytes_(maxRecordBytes) {}
    void handle(const std::string& payload);
    void replay();
    // replay() spread over loop iterations: one readable record per replayNext() call.
    void beginReplay() { replayCursor_ = 0; }
    bool replayNext();
private:
    int replayCursor_ = -1;
    bool recover(int slot, JsonDocument& record);
    void result(JsonDocument& record, const char* outcome);
    Ledger& ledger_;
    SafetyPort& port_;
    size_t maxIdBytes_, maxRecordBytes_;
};

}  // namespace np
