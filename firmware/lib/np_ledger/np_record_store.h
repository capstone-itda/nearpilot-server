#pragma once
// Device interface for persisted command records: fixed slots holding one JSON string each.
#include <string>

namespace np {

class RecordStore {
public:
    virtual ~RecordStore() = default;
    virtual bool ready() const = 0;
    virtual int capacity() const = 0;
    virtual bool has(int slot) = 0;
    virtual std::string read(int slot) = 0;
    // True only when the backend reports the whole value written; the ledger reads it back.
    virtual bool write(int slot, const std::string& body) = 0;
    // Whether a new record may be placed in this empty slot.
    virtual bool canCreate(int) const { return true; }
};

// Primary slots first, then legacy slots. Legacy records are read and updated in place,
// but new records only go to the primary store.
class LayeredRecordStore : public RecordStore {
public:
    LayeredRecordStore(RecordStore& primary, RecordStore& legacy) : primary_(primary), legacy_(legacy) {}

    bool ready() const override { return primary_.ready() && legacy_.ready(); }
    int capacity() const override { return primary_.capacity() + legacy_.capacity(); }
    bool has(int slot) override { return primary(slot) ? primary_.has(slot) : legacy_.has(legacySlot(slot)); }
    std::string read(int slot) override {
        return primary(slot) ? primary_.read(slot) : legacy_.read(legacySlot(slot));
    }
    bool write(int slot, const std::string& body) override {
        if (primary(slot)) return primary_.write(slot, body);
        return legacy_.has(legacySlot(slot)) && legacy_.write(legacySlot(slot), body);
    }
    bool canCreate(int slot) const override { return primary(slot) && primary_.canCreate(slot); }

private:
    bool primary(int slot) const { return slot < primary_.capacity(); }
    int legacySlot(int slot) const { return slot - primary_.capacity(); }

    RecordStore& primary_;
    RecordStore& legacy_;
};

}  // namespace np
