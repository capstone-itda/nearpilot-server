#pragma once
// The built-in declarations, loaded once, for the command and executor tests.
#include "np_descriptor.h"
#include "np_descriptors.h"

namespace fixtures {

inline const np::Descriptor& load(np::Descriptor& slot, const char* json) {
    if (!slot.loaded()) slot.load(json);
    return slot;
}

inline const np::Descriptor& locker() {
    static np::Descriptor value;
    return load(value, np::LOCKER_DESCRIPTOR);
}

inline const np::Descriptor& light() {
    static np::Descriptor value;
    return load(value, np::LIGHT_DESCRIPTOR);
}

inline const np::Descriptor& lightRing() {
    static np::Descriptor value;
    return load(value, np::LIGHT_RING_DESCRIPTOR);
}

inline const np::Descriptor& speaker() {
    static np::Descriptor value;
    return load(value, np::SPEAKER_DFPLAYER_DESCRIPTOR);
}

}  // namespace fixtures
