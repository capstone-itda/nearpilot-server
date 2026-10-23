#include "np_ledger.h"

namespace np {

uint32_t Ledger::hash(const std::string& id) {
    uint32_t value = 2166136261u;  // FNV-1a; collisions only cost an extra read.
    for (unsigned char ch : id) value = (value ^ ch) * 16777619u;
    return value;
}

bool Ledger::load() {
    loaded_ = false;
    if (!store_.ready()) return false;
    int capacity = store_.capacity();
    used_.assign(capacity, false);
    hashes_.assign(capacity, 0);
    unreadable_ = false;
    for (int slot = 0; slot < capacity; ++slot) {
        if (!store_.has(slot)) continue;
        used_[slot] = true;
        JsonDocument stored;
        if (deserializeJson(stored, store_.read(slot)) || !stored["command_id"].is<const char*>()) {
            unreadable_ = true;
            continue;
        }
        hashes_[slot] = hash(stored["command_id"].as<std::string>());
    }
    return loaded_ = true;
}

int Ledger::find(const std::string& id, JsonDocument& record, int& empty) {
    empty = -1;
    if (!store_.ready() || (!loaded_ && !load()) || unreadable_) return STORAGE_ERROR;
    uint32_t wanted = hash(id);
    for (int slot = 0; slot < int(used_.size()); ++slot) {
        if (!used_[slot]) {
            if (empty < 0 && store_.canCreate(slot)) empty = slot;
            continue;
        }
        if (hashes_[slot] != wanted) continue;
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

bool Ledger::create(int slot, JsonDocument& record) {
    std::string body;
    serializeJson(record, body);
    return store_.ready() && slot >= 0 && !store_.has(slot) && store_.canCreate(slot) &&
           write(slot, body, record["command_id"].as<std::string>());
}

bool Ledger::save(int slot, JsonDocument& record) {
    std::string body;
    serializeJson(record, body);
    return store_.ready() && write(slot, body, record["command_id"].as<std::string>());
}

bool Ledger::write(int slot, const std::string& body, const std::string& id) {
    if (!store_.write(slot, body) || store_.read(slot) != body) {
        loaded_ = false;  // The slot may now hold anything; rescan before the next lookup.
        return false;
    }
    if (loaded_ && slot < int(used_.size())) {
        used_[slot] = true;
        hashes_[slot] = hash(id);
    }
    return true;
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
