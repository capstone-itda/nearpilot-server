#pragma once
// Command records that block re-actuation across reconnects and restarts (FR-06, FR-22).
#include <ArduinoJson.h>
#include <string>

#include "np_record_store.h"

namespace np {

class Ledger {
public:
    static constexpr int NOT_FOUND = -1;
    static constexpr int STORAGE_ERROR = -2;

    explicit Ledger(RecordStore& store) : store_(store) {}

    bool ready() const { return store_.ready(); }

    // Scans fixed slots; no separately persisted index can lose an accepted record.
    // Returns the slot, NOT_FOUND (with the first empty slot or -1) or STORAGE_ERROR.
    int find(const std::string& id, JsonDocument& record, int& empty);

    bool save(int slot, JsonDocument& record);

    // Records keep the node ID inside their stored content. Never re-home them silently.
    const char* ownerConflict(const std::string& nodeId);

    // Visits each readable record in slot order; unreadable slots are skipped, never erased.
    template <class Visit>
    void forEach(Visit visit) {
        if (!store_.ready()) return;
        for (int slot = 0; slot < store_.capacity(); ++slot) {
            if (!store_.has(slot)) continue;
            JsonDocument record;
            if (deserializeJson(record, store_.read(slot))) continue;
            visit(slot, record);
        }
    }

private:
    RecordStore& store_;
};

}  // namespace np
