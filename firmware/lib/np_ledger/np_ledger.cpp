#include "np_ledger.h"
#include "np_clock.h"
#include <cmath>
#include <limits>

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
        if (deserializeJson(stored, store_.read(slot)) || !stored[idKey_].is<const char*>()) {
            unreadable_ = true;
            continue;
        }
        hashes_[slot] = hash(stored[idKey_].as<std::string>());
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
        if (deserializeJson(stored, store_.read(slot)) || !stored[idKey_].is<const char*>())
            return STORAGE_ERROR;
        if (stored[idKey_].as<std::string>() == id) {
            record.set(stored);
            return slot;
        }
    }
    return NOT_FOUND;
}

bool Ledger::create(int slot, JsonDocument& record) {
    std::string body;
    serializeJson(record, body);
    return store_.ready() && slot >= 0 && slot < store_.capacity() && !store_.has(slot) && store_.canCreate(slot) &&
           write(slot, body, record[idKey_].as<std::string>());
}

bool Ledger::save(int slot, JsonDocument& record) {
    std::string body;
    serializeJson(record, body);
    return store_.ready() && slot >= 0 && slot < store_.capacity() && write(slot, body, record[idKey_].as<std::string>());
}

int Ledger::replacement(bool trusted, double nowSec, double maxErrorSec, size_t expiryIndex) {
    if (!store_.ready() || (!loaded_ && !load()) || unreadable_) return STORAGE_ERROR;
    if (!trusted || !std::isfinite(nowSec) || !std::isfinite(maxErrorSec) || maxErrorSec < 0)
        return NOT_FOUND;
    int oldestSlot = NOT_FOUND;
    double oldest = std::numeric_limits<double>::infinity();
    for (int slot = 0; slot < store_.capacity(); ++slot) {
        if (!store_.canCreate(slot)) continue;  // Never grow/reuse node_lab's small legacy namespace.
        if (!store_.has(slot)) return slot;
        JsonDocument record, content;
        if (deserializeJson(record, store_.read(slot)) ||
            deserializeJson(content, record["content"].as<std::string>())) return STORAGE_ERROR;
        std::string state = record["state"].as<std::string>();
        std::string outcome = record["outcome"].as<std::string>();
        bool terminal = state == "succeeded" || state == "failed" || state == "rejected" || state == "unknown";
        // Checks store an explicit final marker, so a partially written receipt is never reclaimed.
        if (std::string(idKey_) == "check_id")
            terminal = state == "final" && (outcome == "passed" || outcome == "not_ready" ||
                                          outcome == "fault" || outcome == "unknown");
        if (!terminal) continue;
        double expires, created;
        std::string firstStored = record["created_at"].is<const char*>() ?
            record["created_at"].as<std::string>() : record["occurred_at"].as<std::string>();
        if (!content[expiryIndex].is<const char*>() ||
            !parseUtc(content[expiryIndex].as<std::string>(), expires) || !parseUtc(firstStored, created)) continue;
        if (nowSec - maxErrorSec < expires || created > nowSec - maxErrorSec) continue;
        if (created < oldest) { oldest = created; oldestSlot = slot; }
    }
    return oldestSlot;
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
