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
};

}  // namespace np
