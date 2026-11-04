#ifndef GATEWAY_METRICS_H
#define GATEWAY_METRICS_H

#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <deque>
#include <iomanip>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>

namespace inference {

struct WorkerMetrics {
    std::string node;
    int in_flight = 0;
    bool circuit_open = false;
};

// Bounded window of successful request latencies. The adaptive controller only
// needs a recent p95, so keeping the last 256 samples is enough and avoids the
// memory growth of an unbounded histogram.
class LatencyWindow {
public:
    explicit LatencyWindow(std::size_t capacity = 256) : capacity_(capacity) {}

    void record(std::int64_t latency_us) {
        std::lock_guard<std::mutex> lock(mutex_);
        values_.push_back(latency_us);
        while (values_.size() > capacity_) {
            values_.pop_front();
        }
    }

    void clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        values_.clear();
    }

    std::size_t size() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return values_.size();
    }

    std::int64_t p95() const {
        std::lock_guard<std::mutex> lock(mutex_);
        if (values_.empty()) {
            return -1;
        }
        std::vector<std::int64_t> sorted(values_.begin(), values_.end());
        std::sort(sorted.begin(), sorted.end());
        const std::size_t rank = (sorted.size() * 95 + 99) / 100;
        return sorted[std::min(rank - 1, sorted.size() - 1)];
    }

private:
    std::size_t capacity_;
    mutable std::mutex mutex_;
    std::deque<std::int64_t> values_;
};

class DurationHistogram {
public:
    static constexpr std::size_t kBucketCount = 12;
    static constexpr std::array<double, kBucketCount> kBuckets = {
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0
    };

    DurationHistogram() {
        for (auto& bucket : buckets_) {
            bucket.store(0, std::memory_order_relaxed);
        }
    }

    void observe(std::int64_t latency_us) {
        const double seconds = static_cast<double>(latency_us) / 1'000'000.0;
        for (std::size_t index = 0; index < kBuckets.size(); ++index) {
            if (seconds <= kBuckets[index]) {
                buckets_[index].fetch_add(1, std::memory_order_relaxed);
                break;
            }
        }
        count_.fetch_add(1, std::memory_order_relaxed);
        sum_us_.fetch_add(
            static_cast<std::uint64_t>(std::max<std::int64_t>(0, latency_us)),
            std::memory_order_relaxed
        );
    }

    std::uint64_t bucket(std::size_t index) const {
        return buckets_[index].load(std::memory_order_relaxed);
    }

    std::uint64_t count() const {
        return count_.load(std::memory_order_relaxed);
    }

    std::uint64_t sum_us() const {
        return sum_us_.load(std::memory_order_relaxed);
    }

private:
    std::array<std::atomic<std::uint64_t>, kBucketCount> buckets_;
    std::atomic<std::uint64_t> count_{0};
    std::atomic<std::uint64_t> sum_us_{0};
};

class MetricsRegistry {
public:
    enum class Endpoint : std::size_t { Infer = 0, Tensor = 1 };
    static constexpr std::size_t kEndpointCount = 2;
    static constexpr std::size_t kStatusCount = 5;

    MetricsRegistry() {
        for (auto& endpoint : requests_) {
            for (auto& status : endpoint) {
                status.store(0, std::memory_order_relaxed);
            }
        }
    }

    void recordRequest(Endpoint endpoint, int status, std::int64_t latency_us) {
        const auto index = static_cast<std::size_t>(endpoint);
        requests_[index][statusIndex(status)].fetch_add(1, std::memory_order_relaxed);
        histograms_[index].observe(latency_us);
        if (status == 200) {
            latency_window_.record(latency_us);
        }
    }

    std::string renderPrometheus(const std::vector<WorkerMetrics>& workers) const {
        std::ostringstream out;
        const std::array<const char*, kEndpointCount> endpoint_names = {
            "/infer", "/infer_tensor"
        };
        const std::array<const char*, kStatusCount> status_names = {
            "200", "400", "500", "503", "other"
        };
        const std::array<const char*, DurationHistogram::kBucketCount> bucket_names = {
            "0.005", "0.01", "0.025", "0.05", "0.1", "0.25",
            "0.5", "1", "2.5", "5", "10", "30"
        };

        out << "# HELP gateway_requests_total Inference requests by endpoint and status.\n";
        out << "# TYPE gateway_requests_total counter\n";
        for (std::size_t endpoint = 0; endpoint < kEndpointCount; ++endpoint) {
            for (std::size_t status = 0; status < kStatusCount; ++status) {
                out << "gateway_requests_total{endpoint=\"" << endpoint_names[endpoint]
                    << "\",status=\"" << status_names[status] << "\"} "
                    << requests_[endpoint][status].load(std::memory_order_relaxed)
                    << "\n";
            }
        }

        out << "# HELP gateway_request_duration_seconds End-to-end inference request duration.\n";
        out << "# TYPE gateway_request_duration_seconds histogram\n";
        for (std::size_t endpoint = 0; endpoint < kEndpointCount; ++endpoint) {
            const auto& histogram = histograms_[endpoint];
            std::uint64_t cumulative = 0;
            for (std::size_t bucket = 0; bucket < DurationHistogram::kBucketCount; ++bucket) {
                cumulative += histogram.bucket(bucket);
                out << "gateway_request_duration_seconds_bucket{endpoint=\""
                    << endpoint_names[endpoint] << "\",le=\"" << bucket_names[bucket]
                    << "\"} " << cumulative << "\n";
            }
            out << "gateway_request_duration_seconds_bucket{endpoint=\""
                << endpoint_names[endpoint] << "\",le=\"+Inf\"} " << histogram.count()
                << "\n";
            out << "gateway_request_duration_seconds_sum{endpoint=\""
                << endpoint_names[endpoint] << "\"} " << std::fixed << std::setprecision(6)
                << static_cast<double>(histogram.sum_us()) / 1'000'000.0 << "\n";
            out << "gateway_request_duration_seconds_count{endpoint=\""
                << endpoint_names[endpoint] << "\"} " << histogram.count() << "\n";
        }

        out << "# HELP gateway_admission_in_flight Current admitted inference requests.\n";
        out << "# TYPE gateway_admission_in_flight gauge\n";
        out << "gateway_admission_in_flight "
            << admission_in_flight.load(std::memory_order_relaxed) << "\n";
        out << "# HELP gateway_admission_limit Current admission limit.\n";
        out << "# TYPE gateway_admission_limit gauge\n";
        out << "gateway_admission_limit "
            << admission_limit.load(std::memory_order_relaxed) << "\n";
        out << "# HELP gateway_admission_configured_max Configured upper bound.\n";
        out << "# TYPE gateway_admission_configured_max gauge\n";
        out << "gateway_admission_configured_max "
            << admission_configured_max.load(std::memory_order_relaxed) << "\n";
        out << "# HELP gateway_admission_min Configured lower bound.\n";
        out << "# TYPE gateway_admission_min gauge\n";
        out << "gateway_admission_min "
            << admission_min.load(std::memory_order_relaxed) << "\n";
        out << "# HELP gateway_admission_adaptive Whether adaptive admission is enabled.\n";
        out << "# TYPE gateway_admission_adaptive gauge\n";
        out << "gateway_admission_adaptive "
            << admission_adaptive.load(std::memory_order_relaxed) << "\n";
        out << "# HELP gateway_admission_target_p95_ms Adaptive latency target.\n";
        out << "# TYPE gateway_admission_target_p95_ms gauge\n";
        out << "gateway_admission_target_p95_ms "
            << admission_target_p95_ms.load(std::memory_order_relaxed) << "\n";
        out << "# HELP gateway_admission_observed_p95_ms Latest observed p95.\n";
        out << "# TYPE gateway_admission_observed_p95_ms gauge\n";
        out << "gateway_admission_observed_p95_ms "
            << admission_observed_p95_ms.load(std::memory_order_relaxed) << "\n";
        out << "# HELP gateway_admission_observed_samples Samples used by the last adaptive decision.\n";
        out << "# TYPE gateway_admission_observed_samples gauge\n";
        out << "gateway_admission_observed_samples "
            << admission_observed_samples.load(std::memory_order_relaxed) << "\n";
        out << "# HELP gateway_admission_accepted_total Admitted requests.\n";
        out << "# TYPE gateway_admission_accepted_total counter\n";
        out << "gateway_admission_accepted_total "
            << admission_accepted.load(std::memory_order_relaxed) << "\n";
        out << "# HELP gateway_admission_rejected_total Rejected requests.\n";
        out << "# TYPE gateway_admission_rejected_total counter\n";
        out << "gateway_admission_rejected_total "
            << admission_rejected.load(std::memory_order_relaxed) << "\n";
        out << "# HELP gateway_admission_limit_changes_total Adaptive limit changes.\n";
        out << "# TYPE gateway_admission_limit_changes_total counter\n";
        out << "gateway_admission_limit_changes_total "
            << admission_limit_changes.load(std::memory_order_relaxed) << "\n";

        out << "# HELP gateway_worker_in_flight Current worker in-flight requests.\n";
        out << "# TYPE gateway_worker_in_flight gauge\n";
        for (const auto& worker : workers) {
            out << "gateway_worker_in_flight{worker=\"" << escapeLabel(worker.node)
                << "\"} " << worker.in_flight << "\n";
        }
        out << "# HELP gateway_worker_circuit_open Whether the worker circuit is open.\n";
        out << "# TYPE gateway_worker_circuit_open gauge\n";
        for (const auto& worker : workers) {
            out << "gateway_worker_circuit_open{worker=\"" << escapeLabel(worker.node)
                << "\"} " << (worker.circuit_open ? 1 : 0) << "\n";
        }
        return out.str();
    }

    LatencyWindow& latencyWindow() { return latency_window_; }
    const LatencyWindow& latencyWindow() const { return latency_window_; }

    std::atomic<std::int64_t> admission_in_flight{0};
    std::atomic<std::int64_t> admission_limit{0};
    std::atomic<std::int64_t> admission_configured_max{0};
    std::atomic<std::int64_t> admission_min{0};
    std::atomic<std::int64_t> admission_adaptive{0};
    std::atomic<std::int64_t> admission_target_p95_ms{0};
    std::atomic<std::int64_t> admission_observed_p95_ms{-1};
    std::atomic<std::int64_t> admission_observed_samples{0};
    std::atomic<std::uint64_t> admission_accepted{0};
    std::atomic<std::uint64_t> admission_rejected{0};
    std::atomic<std::uint64_t> admission_limit_changes{0};

private:
    static std::size_t statusIndex(int status) {
        if (status == 200) {
            return 0;
        }
        if (status == 400) {
            return 1;
        }
        if (status == 500) {
            return 2;
        }
        if (status == 503) {
            return 3;
        }
        return 4;
    }

    static std::string escapeLabel(const std::string& value) {
        std::string escaped;
        escaped.reserve(value.size());
        for (char character : value) {
            if (character == '\\' || character == '"') {
                escaped.push_back('\\');
            }
            if (character != '\n') {
                escaped.push_back(character);
            }
        }
        return escaped;
    }

    std::array<std::array<std::atomic<std::uint64_t>, kStatusCount>, kEndpointCount>
        requests_;
    std::array<DurationHistogram, kEndpointCount> histograms_;
    LatencyWindow latency_window_{256};
};

}  // namespace inference

#endif
