#ifndef ADAPTIVE_ADMISSION_H
#define ADAPTIVE_ADMISSION_H

#include <algorithm>
#include <cstddef>
#include <cstdint>

namespace inference {

// AIMD policy for the gateway in-flight limit. The caller supplies the
// observed p95 and the number of successful samples in the current window.
// The function is pure so the policy can be tested without timing.
inline std::size_t nextAdmissionLimit(
    std::size_t current,
    std::int64_t observed_p95_ms,
    std::size_t samples,
    std::size_t min_limit,
    std::size_t max_limit,
    std::int64_t target_p95_ms
) {
    if (min_limit == 0 || max_limit < min_limit) {
        return current;
    }
    const std::size_t clamped = std::min(max_limit, std::max(min_limit, current));
    if (samples < 20 || observed_p95_ms <= 0 || target_p95_ms <= 0) {
        return clamped;
    }
    if (observed_p95_ms * 10 > target_p95_ms * 11) {
        const std::size_t step = std::max<std::size_t>(1, clamped / 5);
        return std::max(min_limit, clamped > step ? clamped - step : min_limit);
    }
    if (observed_p95_ms * 4 < target_p95_ms * 3) {
        return std::min(max_limit, clamped + 1);
    }
    return clamped;
}

}  // namespace inference

#endif
