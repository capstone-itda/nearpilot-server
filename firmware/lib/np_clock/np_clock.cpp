#include "np_clock.h"

namespace np {

namespace {

bool digits(const std::string& raw, size_t at, size_t count, int& out) {
    out = 0;
    for (size_t i = at; i < at + count; ++i) {
        if (raw[i] < '0' || raw[i] > '9') return false;
        out = out * 10 + (raw[i] - '0');
    }
    return true;
}

bool leap(int year) { return (year % 4 == 0 && year % 100 != 0) || year % 400 == 0; }

int daysInMonth(int year, int month) {
    static const int days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    return month == 2 && leap(year) ? 29 : days[month - 1];
}

// Days since 1970-01-01 in the proleptic Gregorian calendar (Howard Hinnant's algorithm).
long long daysFromCivil(int year, int month, int day) {
    year -= month <= 2;
    const long long era = (year >= 0 ? year : year - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(year - era * 400);
    const unsigned doy = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 + day - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + static_cast<long long>(doe) - 719468;
}

}  // namespace

bool clockConfigured(bool configured, const NodeConfig& config) {
    return configured && !config.timeServer.empty() && config.clockMaxErrorSec >= 0.0 &&
        config.timeTrustMs > 0 && config.timeResyncMs > 0;
}

bool clockTrusted(bool configured, const NodeConfig& config, bool synchronized, unsigned long lastSyncMs,
    unsigned long nowMs) {
    return synchronized && clockConfigured(configured, config) && nowMs - lastSyncMs <= config.timeTrustMs;
}

bool parseUtc(const std::string& raw, double& seconds) {
    if (raw.size() < 20 || raw[4] != '-' || raw[7] != '-' || raw[10] != 'T' || raw[13] != ':' || raw[16] != ':')
        return false;
    int year, month, day, hour, minute, second;
    if (!digits(raw, 0, 4, year) || !digits(raw, 5, 2, month) || !digits(raw, 8, 2, day) ||
        !digits(raw, 11, 2, hour) || !digits(raw, 14, 2, minute) || !digits(raw, 17, 2, second)) return false;
    if (month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month) || hour > 23 || minute > 59 ||
        second > 59) return false;
    size_t at = 19;
    double fraction = 0.0, scale = 0.1;
    if (raw[at] == '.') {
        ++at;
        if (at >= raw.size() || raw[at] < '0' || raw[at] > '9') return false;
        while (at < raw.size() && raw[at] >= '0' && raw[at] <= '9') {
            fraction += (raw[at++] - '0') * scale;
            scale *= 0.1;
        }
    }
    const std::string zone = raw.substr(at);
    if (zone != "Z" && zone != "+00:00") return false;
    seconds = double(daysFromCivil(year, month, day) * 86400LL + hour * 3600 + minute * 60 + second) + fraction;
    return true;
}

}  // namespace np
