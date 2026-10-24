#pragma once
// ESP32 NVS backend for RecordStore (ESP-IDF API, no Arduino headers). Not built for native tests.
// One string key per record (c0..cN): an NVS key write leaves either the old or the new value.
#include <nvs.h>

#include "np_record_store.h"

namespace np {

class NvsRecordStore : public RecordStore {
public:
    // partition: "nvs" is the default partition that Arduino already initialized.
    NvsRecordStore(const char* partition, const char* ns, int capacity)
        : partition_(partition), namespace_(ns), capacity_(capacity) {}

    // Never erases a partition it cannot open; the caller reports storage_unavailable.
    bool begin();
    bool ready() const override { return opened_; }
    int capacity() const override { return capacity_; }
    bool has(int slot) override;
    std::string read(int slot) override;
    bool write(int slot, const std::string& body) override;
    // Free entries of the partition after the page NVS keeps for compaction. Diagnostic only:
    // the tail of a full page counts as free but cannot hold a whole record.
    size_t freeEntries();
    nvs_handle_t handle() const { return handle_; }

    // Entries one string value of this many bytes occupies (header + 32-byte data entries).
    static size_t entriesFor(size_t bytes) { return 1 + (bytes + 1 + 31) / 32; }

private:
    const char* partition_;
    const char* namespace_;
    int capacity_;
    nvs_handle_t handle_ = 0;
    bool opened_ = false;
};

}  // namespace np
