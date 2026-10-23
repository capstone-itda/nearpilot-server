#pragma once
// In-memory RecordStore whose instance outlives a Ledger, standing in for flash across reboots.
#include <map>
#include <string>

#include "np_record_store.h"

class FakeStore : public np::RecordStore {
public:
    explicit FakeStore(int slots = 3) : slots_(slots) {}

    bool available = true;
    bool failWrite = false;
    bool corruptWrite = false;
    std::map<int, std::string> slots;

    bool ready() const override { return available; }
    int capacity() const override { return slots_; }
    bool has(int slot) override { return slots.count(slot) > 0; }
    std::string read(int slot) override { return slots[slot]; }
    bool write(int slot, const std::string& body) override {
        if (failWrite) return false;
        slots[slot] = corruptWrite ? body.substr(1) : body;
        return true;
    }

private:
    int slots_;
};
