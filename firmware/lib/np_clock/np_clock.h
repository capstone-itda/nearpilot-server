#pragma once
// Clock trust and server timestamp parsing. Callers pass millis() and wall time explicitly.
#include <string>

#include "np_config.h"

namespace np {

// Trust needs a source, a measured error bound and both windows; -1 or 0 keeps it disabled.
bool clockConfigured(bool configured, const NodeConfig& config);

// `nowMs - lastSyncMs` uses unsigned arithmetic, so a millis() wrap keeps the right age.
bool clockTrusted(bool configured, const NodeConfig& config, bool synchronized, unsigned long lastSyncMs,
    unsigned long nowMs);

// Strict server wire timestamps: YYYY-MM-DDTHH:MM:SS, optional fraction, then Z or +00:00.
bool parseUtc(const std::string& raw, double& seconds);

}  // namespace np
