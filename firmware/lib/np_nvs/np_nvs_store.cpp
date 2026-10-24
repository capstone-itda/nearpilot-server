#include "np_nvs_store.h"

#include <cstdio>
#include <cstring>
#include <nvs_flash.h>

namespace np {

namespace {

// NVS keeps one empty page for compaction; its entries are never usable for data.
constexpr size_t RESERVED_PAGE_ENTRIES = 126;

void key(int slot, char (&out)[16]) { snprintf(out, sizeof(out), "c%d", slot); }

}  // namespace

bool NvsRecordStore::begin() {
    if (strcmp(partition_, NVS_DEFAULT_PART_NAME) != 0 && nvs_flash_init_partition(partition_) != ESP_OK)
        return opened_ = false;
    return opened_ = nvs_open_from_partition(partition_, namespace_, NVS_READWRITE, &handle_) == ESP_OK;
}

bool NvsRecordStore::has(int slot) {
    char name[16];
    key(slot, name);
    size_t length = 0;
    return opened_ && nvs_get_str(handle_, name, nullptr, &length) == ESP_OK;
}

std::string NvsRecordStore::read(int slot) {
    char name[16];
    key(slot, name);
    size_t length = 0;
    if (!opened_ || nvs_get_str(handle_, name, nullptr, &length) != ESP_OK || length == 0) return {};
    std::string value(length, '\0');
    if (nvs_get_str(handle_, name, &value[0], &length) != ESP_OK) return {};
    value.resize(length - 1);  // Drop the terminator.
    return value;
}

bool NvsRecordStore::write(int slot, const std::string& body) {
    char name[16];
    key(slot, name);
    return opened_ && nvs_set_str(handle_, name, body.c_str()) == ESP_OK && nvs_commit(handle_) == ESP_OK;
}

size_t NvsRecordStore::freeEntries() {
    nvs_stats_t stats;
    if (!opened_ || nvs_get_stats(partition_, &stats) != ESP_OK) return 0;
    return stats.free_entries > RESERVED_PAGE_ENTRIES ? stats.free_entries - RESERVED_PAGE_ENTRIES : 0;
}

}  // namespace np
