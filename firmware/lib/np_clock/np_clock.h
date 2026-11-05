#pragma once
// MQTT clock sync (#236) and server timestamp parsing. Callers pass millis()/micros() explicitly.
#include <cstddef>
#include <string>

namespace np {

// One `time` reply, already matched to this node and to the last request.
struct TimeOffer {
    double serverTime = 0.0;  // Unix seconds just before the server published.
    double maxErrorSec = 0.0;
    unsigned long trustMs = 0, resyncMs = 0;
};

// nullptr when `body` is a valid reply to `requestId` for `nodeId`, else why it was dropped.
const char* decodeTimeReply(const char* body, size_t length, const std::string& nodeId,
    const std::string& requestId, TimeOffer& out);

// Error of setting the clock to T + r/2: half the round trip plus drift over the whole trust window.
double offerErrorSec(const TimeOffer& offer, unsigned long roundTripUs, double driftPpm);

// Wall time to set: the server time plus half the round trip.
double offerTime(const TimeOffer& offer, unsigned long roundTripUs);

struct ClockState {
    bool synchronized = false;
    double maxErrorSec = -1.0;  // The accepted reply's bound; commands are checked against it.
    unsigned long syncedMs = 0, trustMs = 0, resyncMs = 0;
};

// Records an accepted offer. The caller sets the wall clock itself.
void recordSync(ClockState& state, const TimeOffer& offer, unsigned long nowMs);

// `nowMs - syncedMs` uses unsigned arithmetic, so a millis() wrap keeps the right age.
bool clockTrusted(const ClockState& state, unsigned long nowMs);

// Never synchronized, or at least the resync interval old.
bool resyncDue(const ClockState& state, unsigned long nowMs);

// Strict server wire timestamps: YYYY-MM-DDTHH:MM:SS, optional fraction, then Z or +00:00.
bool parseUtc(const std::string& raw, double& seconds);

}  // namespace np
