#include "np_ledger.h"

namespace np {

int Ledger::find(const std::string& id, JsonDocument& record, int& empty) {
    empty = -1;
    if (!store_.ready()) return STORAGE_ERROR;
    for (int slot = 0; slot < store_.capacity(); ++slot) {
        if (!store_.has(slot)) {
            if (empty < 0) empty = slot;
            continue;
        }
        JsonDocument stored;
        if (deserializeJson(stored, store_.read(slot)) || !stored["command_id"].is<const char*>())
            return STORAGE_ERROR;
        if (stored["command_id"].as<std::string>() == id) {
            record.set(stored);
            return slot;
        }
    }
    return NOT_FOUND;
}

bool Ledger::save(int slot, JsonDocument& record) {
    std::string body;
    serializeJson(record, body);
    return store_.ready() && store_.write(slot, body) && store_.read(slot) == body;
}

const char* Ledger::ownerConflict(const std::string& nodeId) {
    if (!store_.ready()) return "storage_unavailable";
    for (int slot = 0; slot < store_.capacity(); ++slot) {
        if (!store_.has(slot)) continue;
        JsonDocument stored, content;
        if (deserializeJson(stored, store_.read(slot)) ||
            deserializeJson(content, stored["content"].as<std::string>())) return "record_unreadable";
        if (content[0].as<std::string>() != nodeId) return "node_id_change_with_records";
    }
    return nullptr;
}

}  // namespace np
