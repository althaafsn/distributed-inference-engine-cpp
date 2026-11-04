#include "adaptive_admission.h"

#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <iostream>

namespace {

void require_equal(std::size_t actual, std::size_t expected, const char* name) {
    if (actual != expected) {
        std::cerr << name << ": expected " << expected << ", got " << actual << "\n";
        std::exit(1);
    }
}

}  // namespace

int main() {
    using inference::nextAdmissionLimit;

    require_equal(nextAdmissionLimit(7, 500, 100, 1, 16, 400), 6, "decrease");
    require_equal(nextAdmissionLimit(10, 500, 100, 1, 16, 400), 8, "decrease by 20%");
    require_equal(nextAdmissionLimit(7, 250, 100, 1, 16, 400), 8, "increase");
    require_equal(nextAdmissionLimit(7, 390, 100, 1, 16, 400), 7, "hold inside band");
    require_equal(nextAdmissionLimit(7, 430, 100, 1, 16, 400), 7, "hold in upper deadband");
    require_equal(nextAdmissionLimit(7, 450, 100, 1, 16, 400), 6, "decrease above deadband");
    require_equal(nextAdmissionLimit(1, 500, 100, 1, 16, 400), 1, "floor");
    require_equal(nextAdmissionLimit(16, 250, 100, 1, 16, 400), 16, "ceiling");
    require_equal(nextAdmissionLimit(7, 500, 19, 1, 16, 400), 7, "too few samples");
    require_equal(nextAdmissionLimit(7, -1, 100, 1, 16, 400), 7, "no p95");
    require_equal(nextAdmissionLimit(7, 500, 100, 0, 16, 400), 7, "invalid floor");
    require_equal(nextAdmissionLimit(20, 250, 100, 1, 16, 400), 16, "clamp to ceiling");
    require_equal(nextAdmissionLimit(0, 200, 100, 1, 16, 400), 2, "clamp to floor then increase");

    std::cout << "adaptive admission policy tests passed\n";
    return 0;
}
