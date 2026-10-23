#pragma once
// Command records that block re-actuation across reconnects and restarts (FR-06, FR-22).
#include <ArduinoJson.h>
#include <cstdint>
#include <string>
#include <vector>

#include "np_record_store.h"

namespace np {

class Ledger {
public:
    static constexpr int NOT_FOUND = -1;
    static constexpr int STORAGE_ERROR = -2;

    explicit Ledger(RecordStore& store) : store_(store) {}

    bool ready() const { return store_.ready(); }

    // Reads every slot once into an in-memory index of command ID hashes.
    // The slots stay the only persisted truth; the index is rebuilt after any failed write.
    bool load();

    // Returns the slot, NOT_FOUND (with the first creatable empty slot or -1) or STORAGE_ERROR.
    // Any unreadable record blocks lookups: the command might be the one it holds.
    int find(const std::string& id, JsonDocument& record, int& empty);

    // Places a new record in an empty slot and reads it back.
    bool create(int slot, JsonDocument& record);

    // Rewrites an existing record and reads it back.
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
    static uint32_t hash(const std::string& id);
    bool write(int slot, const std::string& body, const std::string& id);

    RecordStore& store_;
    bool loaded_ = false;
    bool unreadable_ = false;
    std::vector<bool> used_;
    std::vector<uint32_t> hashes_;
};

}  // namespace np
