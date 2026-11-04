#include "consistent_hash.h"
#include "circuit_breaker.h"
#include "request_validation.h"
#include "shutdown_signal.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <iostream>
#include <limits>
#include <memory>
#include <map>
#include <vector>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <optional>
#include <utility>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <httplib.h>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

// HTTP server worker threads for the gateway. Handlers are I/O-bound (they block
// on a synchronous Post to a worker), so the gateway needs many more threads than
// the CPU-count-based default to keep all workers busy concurrently.
static constexpr size_t kServerThreads = 256;

// A thread-safe pool of httplib::Client connections for a single worker.
// httplib::Client is not safe for concurrent use, so each in-flight request
// borrows its own client from the pool and returns it when done.
class ClientPool {
public:
    ClientPool(std::string host, int port, size_t size)
        : host_(std::move(host)), port_(port) {
        for (size_t i = 0; i < size; ++i) {
            available_.push_back(makeClient());
        }
    }

    // RAII handle: forwards to the borrowed client and returns it to the pool
    // on destruction.
    class Handle {
    public:
        Handle(ClientPool* pool, std::unique_ptr<httplib::Client> client)
            : pool_(pool), client_(std::move(client)) {}
        Handle(Handle&& other) noexcept
            : pool_(other.pool_), client_(std::move(other.client_)) {
            other.pool_ = nullptr;
        }
        Handle(const Handle&) = delete;
        Handle& operator=(const Handle&) = delete;
        ~Handle() {
            if (pool_ && client_) pool_->release(std::move(client_));
        }
        httplib::Client* operator->() { return client_.get(); }

    private:
        ClientPool* pool_;
        std::unique_ptr<httplib::Client> client_;
    };

    Handle acquire() {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [this] { return !available_.empty(); });
        auto client = std::move(available_.back());
        available_.pop_back();
        return Handle(this, std::move(client));
    }

private:
    std::unique_ptr<httplib::Client> makeClient() {
        auto client = std::make_unique<httplib::Client>(host_, port_);
        client->set_connection_timeout(5, 0);  // 5 seconds
        client->set_read_timeout(5, 0);
        client->set_keep_alive(true);
        // The gateway's worker requests are small and use keep-alive. Disable
        // Nagle so headers and body are not held behind delayed ACKs.
        client->set_tcp_nodelay(true);
        return client;
    }

    void release(std::unique_ptr<httplib::Client> client) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            available_.push_back(std::move(client));
        }
        cv_.notify_one();
    }

    std::string host_;
    int port_;
    std::mutex mutex_;
    std::condition_variable cv_;
    std::vector<std::unique_ptr<httplib::Client>> available_;
};

class Gateway {
public:
    struct TensorResponse {
        std::string body;
        std::string request_id;
        std::string node_id;
        bool cached;
        size_t batch_size;
        int64_t queue_wait_us;
        int64_t batch_execution_us;
        size_t output_elements;
    };

    explicit Gateway(const std::vector<std::string>& workers) {
        if (const char* value = std::getenv("ROUTING_POLICY")) {
            routing_policy_ = value;
        }
        if (routing_policy_ != "hash" && routing_policy_ != "least_loaded" &&
            routing_policy_ != "bounded_load") {
            throw std::invalid_argument(
                "ROUTING_POLICY must be hash, least_loaded, or bounded_load"
            );
        }
        if (const char* value = std::getenv("ADMISSION_MAX_INFLIGHT")) {
            try {
                size_t consumed = 0;
                const long long requested = std::stoll(value, &consumed);
                if (consumed != std::strlen(value)) {
                    std::cerr << "Ignoring ADMISSION_MAX_INFLIGHT with trailing characters: "
                              << value << std::endl;
                } else if (requested < 0) {
                    std::cerr << "Ignoring negative ADMISSION_MAX_INFLIGHT: "
                              << value << std::endl;
                } else if (requested >
                           static_cast<long long>(std::numeric_limits<int64_t>::max())) {
                    std::cerr << "Ignoring out-of-range ADMISSION_MAX_INFLIGHT: "
                              << value << std::endl;
                } else if (requested > 0) {
                    admission_max_inflight_ = static_cast<size_t>(requested);
                }
            } catch (const std::exception&) {
                std::cerr << "Ignoring invalid ADMISSION_MAX_INFLIGHT: "
                          << value << std::endl;
            }
        }
        if (const char* value = std::getenv("BOUNDED_LOAD_FACTOR")) {
            try {
                const double requested = std::stod(value);
                if (requested >= 1.0 && requested <= 4.0) {
                    load_factor_ = requested;
                }
            } catch (const std::exception&) {
                // Keep the default factor.
            }
        }
        // Initialize consistent hash
        for (const auto& worker : workers) {
            hash_ring_.addNode(worker);
            
            // Create circuit breaker for each worker
            circuit_breakers_[worker] = std::make_unique<CircuitBreaker>(
                5,   // failure_threshold
                2,   // success_threshold
                std::chrono::seconds(30)
            );
            
            // Create a pool of HTTP clients for each worker. httplib::Client
            // is not thread-safe, so concurrent gateway requests each borrow
            // their own connection from the pool.
            auto url_parts = parseUrl(worker);
            std::cout << "Parsed URL: " << worker << " -> host=" << url_parts.first
                      << " port=" << url_parts.second << std::endl;

            clients_[worker] = std::make_unique<ClientPool>(
                url_parts.first, url_parts.second, kClientPoolSize
            );
            in_flight_[worker] = std::make_unique<std::atomic<int>>(0);

            std::cout << "Connected to worker: " << worker << std::endl;
        }
        std::cout << "Routing policy: " << routing_policy_ << std::endl;
        std::cout << "Admission max in-flight: "
                  << (admission_max_inflight_ == 0 ? "disabled" : std::to_string(admission_max_inflight_))
                  << std::endl;
    }

    struct AdmissionGuard {
        std::atomic<int64_t>* counter;
        explicit AdmissionGuard(std::atomic<int64_t>* value) : counter(value) {}
        ~AdmissionGuard() {
            if (counter != nullptr) counter->fetch_sub(1);
        }
        AdmissionGuard(const AdmissionGuard&) = delete;
        AdmissionGuard& operator=(const AdmissionGuard&) = delete;
        AdmissionGuard(AdmissionGuard&& other) noexcept : counter(other.counter) {
            other.counter = nullptr;
        }
        AdmissionGuard& operator=(AdmissionGuard&&) = delete;
    };

    std::optional<AdmissionGuard> admitRequest() {
        int64_t current = admission_in_flight_.load(std::memory_order_relaxed);
        while (true) {
            if (admission_max_inflight_ > 0 &&
                current >= static_cast<int64_t>(admission_max_inflight_)) {
                admission_rejected_.fetch_add(1, std::memory_order_relaxed);
                return std::nullopt;
            }
            if (admission_in_flight_.compare_exchange_weak(
                    current, current + 1,
                    std::memory_order_acq_rel, std::memory_order_relaxed)) {
                admission_accepted_.fetch_add(1, std::memory_order_relaxed);
                return AdmissionGuard(&admission_in_flight_);
            }
        }
    }
    
    std::string routeRequest(const json& request) {
        // Hash routing preserves cache locality. Least-loaded and bounded-load
        // routing avoid the hot-worker bottleneck that a small hash ring can
        // create for a fixed image set.
        std::string routing_key = computeRoutingKey(request);
        std::string target_node = selectNode(routing_key);
        if (target_node.empty()) {
            throw std::runtime_error("No workers available");
        }
        // primary node with circuit breaker
        auto result = tryNode(target_node, request);
        if (result.has_value()) {
            return *result;
        }
        // primary failed
        auto all_nodes = hash_ring_.getAllNodes();
        for (const auto& node : all_nodes) {
            if (node != target_node) {
                auto retry_result = tryNode(node, request);
                if (retry_result.has_value()) {
                    return *retry_result;
                }
            }
        }
        throw std::runtime_error("All workers failed or circuit breakers open");
    }

    TensorResponse routeTensor(const std::string& body, const std::string& request_id) {
        const std::string routing_key = computeTensorKey(body);
        const std::string target_node = selectNode(routing_key);
        if (target_node.empty()) {
            throw std::runtime_error("No workers available");
        }
        auto result = tryNodeTensor(target_node, body, request_id);
        if (result.has_value()) {
            return *result;
        }
        for (const auto& node : hash_ring_.getAllNodes()) {
            if (node == target_node) {
                continue;
            }
            auto retry_result = tryNodeTensor(node, body, request_id);
            if (retry_result.has_value()) {
                return *retry_result;
            }
        }
        throw std::runtime_error("All workers failed or circuit breakers open");
    }

    json getStats() {
        json stats;
        auto nodes = hash_ring_.getAllNodes();
        stats["total_workers"] = nodes.size();
        stats["routing_policy"] = routing_policy_;
        stats["bounded_load_factor"] = load_factor_;
        json admission;
        admission["max_in_flight"] = admission_max_inflight_;
        admission["in_flight"] = admission_in_flight_.load();
        admission["accepted"] = admission_accepted_.load();
        admission["rejected"] = admission_rejected_.load();
        stats["admission"] = admission;
        json loads = json::array();
        for (const auto& node : nodes) {
            json item;
            item["node"] = node;
            item["in_flight"] = in_flight_.at(node)->load();
            loads.push_back(item);
        }
        stats["in_flight"] = loads;
        
        json circuit_states = json::array();
        for (const auto& [node, breaker] : circuit_breakers_) {
            json state;
            state["node"] = node;
            state["state"] = breaker->getStateString();
            state["failures"] = breaker->getFailureCount();
            state["successes"] = breaker->getSuccessCount();
            circuit_states.push_back(state);
        }
        stats["circuit_breakers"] = circuit_states;
        
        return stats;
    }
    
private:
    std::string selectNode(const std::string& key) {
        const auto nodes = hash_ring_.getAllNodes();
        if (nodes.empty()) {
            return "";
        }
        const std::string primary = hash_ring_.getNode(key);
        if (routing_policy_ == "hash") {
            return primary;
        }
        if (routing_policy_ == "bounded_load") {
            int total = 0;
            for (const auto& node : nodes) {
                total += in_flight_.at(node)->load();
            }
            const double average = static_cast<double>(total) / nodes.size();
            const int cap = std::max(1, static_cast<int>(std::ceil(average * load_factor_)));
            if (in_flight_.at(primary)->load() < cap) {
                return primary;
            }
        }
        return leastLoaded(nodes);
    }

    std::string leastLoaded(const std::vector<std::string>& nodes) {
        int best_load = std::numeric_limits<int>::max();
        std::vector<std::string> candidates;
        for (const auto& node : nodes) {
            const int load = in_flight_.at(node)->load();
            if (load < best_load) {
                best_load = load;
                candidates.clear();
            }
            if (load == best_load) {
                candidates.push_back(node);
            }
        }
        if (candidates.empty()) {
            return "";
        }
        // Rotate equal-load ties so a sequential client that never has two
        // requests in flight still spreads work across all workers.
        const size_t index = tie_break_counter_.fetch_add(1) % candidates.size();
        return candidates[index];
    }

    struct InFlightGuard {
        std::atomic<int>* counter;
        explicit InFlightGuard(std::atomic<int>* value) : counter(value) {
            counter->fetch_add(1);
        }
        ~InFlightGuard() { counter->fetch_sub(1); }
        InFlightGuard(const InFlightGuard&) = delete;
        InFlightGuard& operator=(const InFlightGuard&) = delete;
    };

    // Derive a stable ring key from the request's input data. Identical inputs
    // produce identical keys, so consistent hashing sends them to the same
    // worker. Falls back to request_id if input_data is absent.
    static std::string computeRoutingKey(const json& request) {
        auto it = request.find("input_data");
        if (it == request.end() || !it->is_array()) {
            return request.value("request_id", std::string{});
        }
        // FNV-1a over the raw float bits of each element. We narrow to float to
        // match how the worker stores its cache key (std::vector<float>), so the
        // routing and caching views of "the same input" stay consistent.
        uint64_t h = 1469598103934665603ull;  // FNV-1a 64-bit offset basis
        for (const auto& elem : *it) {
            float f = elem.get<float>();
            uint32_t bits;
            std::memcpy(&bits, &f, sizeof(bits));
            h ^= bits;
            h *= 1099511628211ull;  // FNV-1a 64-bit prime
        }
        return std::to_string(h);
    }

    static std::string computeTensorKey(const std::string& body) {
        uint64_t h = 1469598103934665603ull;
        for (unsigned char byte : body) {
            h ^= byte;
            h *= 1099511628211ull;
        }
        return std::to_string(h);
    }

    std::optional<std::string> tryNode(const std::string& node, const json& request) {
        auto breaker_it = circuit_breakers_.find(node);
        if (breaker_it == circuit_breakers_.end()) {
            return std::nullopt;
        }
        auto& breaker = breaker_it->second;
        if (!breaker->allowRequest()) {
            std::cout << "Circuit breaker OPEN for " << node << ", skipping" << std::endl;
            return std::nullopt;
        }
        auto client_it = clients_.find(node);
        if (client_it == clients_.end()) {
            breaker->recordFailure();
            return std::nullopt;
        }
        InFlightGuard in_flight(in_flight_.at(node).get());
        
        try {
            auto client = client_it->second->acquire();
            auto result = client->Post(
                "/infer",
                request.dump(),
                "application/json"
            );
            if (result && result->status == 200) {
                breaker->recordSuccess();
                // Forward the worker body unchanged. The gateway does not
                // transform successful responses, so parsing and re-dumping
                // them only adds CPU on the shared inference host.
                return result->body;
            } else {
                if (result) {
                    std::cerr << "Request to " << node << " failed with status: " 
                              << result->status << std::endl;
                    if (!result->body.empty()) {
                        std::cerr << "Response body: " << result->body << std::endl;
                    }
                } else {
                    std::cerr << "Request to " << node << " failed: no response" << std::endl;
                    auto err = result.error();
                    std::cerr << "Error: " << httplib::to_string(err) << std::endl;
                }
                breaker->recordFailure();
                return std::nullopt;
            }
        } catch (const std::exception& e) {
            std::cerr << "Request to " << node << " failed: " << e.what() << std::endl;
            breaker->recordFailure();
            return std::nullopt;
        }
    }

    std::optional<TensorResponse> tryNodeTensor(
        const std::string& node,
        const std::string& body,
        const std::string& request_id
    ) {
        auto breaker_it = circuit_breakers_.find(node);
        if (breaker_it == circuit_breakers_.end()) {
            return std::nullopt;
        }
        auto& breaker = breaker_it->second;
        if (!breaker->allowRequest()) {
            return std::nullopt;
        }
        auto client_it = clients_.find(node);
        if (client_it == clients_.end()) {
            breaker->recordFailure();
            return std::nullopt;
        }
        InFlightGuard in_flight(in_flight_.at(node).get());
        try {
            auto client = client_it->second->acquire();
            httplib::Headers headers{{"X-Request-Id", request_id}};
            auto result = client->Post(
                "/infer_tensor", headers, body, "application/octet-stream"
            );
            if (result && result->status == 200) {
                breaker->recordSuccess();
                TensorResponse response;
                response.body = result->body;
                response.request_id = result->get_header_value("X-Request-Id");
                response.node_id = result->get_header_value("X-Node-Id");
                response.cached = result->get_header_value("X-Cached") == "true";
                auto parse_size = [&result](const char* name) -> size_t {
                    try {
                        return static_cast<size_t>(std::stoull(result->get_header_value(name)));
                    } catch (const std::exception&) {
                        return 0;
                    }
                };
                auto parse_i64 = [&result](const char* name) -> int64_t {
                    try {
                        return std::stoll(result->get_header_value(name));
                    } catch (const std::exception&) {
                        return 0;
                    }
                };
                response.batch_size = parse_size("X-Batch-Size");
                response.queue_wait_us = parse_i64("X-Queue-Wait-Us");
                response.batch_execution_us = parse_i64("X-Batch-Execution-Us");
                response.output_elements = parse_size("X-Output-Elements");
                return response;
            }
            if (result) {
                std::cerr << "Tensor request to " << node
                          << " failed with status: " << result->status << std::endl;
            } else {
                std::cerr << "Tensor request to " << node << " failed: no response" << std::endl;
            }
            breaker->recordFailure();
            return std::nullopt;
        } catch (const std::exception& e) {
            std::cerr << "Tensor request to " << node << " failed: " << e.what() << std::endl;
            breaker->recordFailure();
            return std::nullopt;
        }
    }
    
    std::pair<std::string, int> parseUrl(const std::string& url) {
        std::string cleaned_url = url;
        size_t proto_pos = cleaned_url.find("://");
        if (proto_pos != std::string::npos) {
            cleaned_url = cleaned_url.substr(proto_pos + 3);
        }
        size_t colon_pos = cleaned_url.find_last_of(':');
        
        if (colon_pos == std::string::npos) {
            return {cleaned_url, 8080};
        }
        std::string host = cleaned_url.substr(0, colon_pos);
        std::string port_str = cleaned_url.substr(colon_pos + 1);
        size_t slash_pos = port_str.find('/');
        if (slash_pos != std::string::npos) {
            port_str = port_str.substr(0, slash_pos);
        }
        int port = 8080;  // default
        try {
            port = std::stoi(port_str);
        } catch (...) {
            std::cerr << "Warning: Invalid port '" << port_str << "', using 8080" << std::endl;
        }
        return {host, port};
    }
    
    // Number of concurrent connections kept open per worker.
    static constexpr size_t kClientPoolSize = 32;

    ConsistentHash hash_ring_;
    std::map<std::string, std::unique_ptr<CircuitBreaker>> circuit_breakers_;
    std::map<std::string, std::unique_ptr<ClientPool>> clients_;
    std::map<std::string, std::unique_ptr<std::atomic<int>>> in_flight_;
    std::string routing_policy_ = "hash";
    double load_factor_ = 1.25;
    std::atomic<size_t> tie_break_counter_{0};
    size_t admission_max_inflight_ = 0;
    std::atomic<int64_t> admission_in_flight_{0};
    std::atomic<int64_t> admission_accepted_{0};
    std::atomic<int64_t> admission_rejected_{0};
};

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <worker1:port> [worker2:port] ..." << std::endl;
        std::cerr << "Example: " << argv[0] << " localhost:8001 localhost:8002 localhost:8003" << std::endl;
        return 1;
    }
    
    std::vector<std::string> workers;
    for (int i = 1; i < argc; ++i) {
        workers.push_back(argv[i]);
    }
    
    int listen_port = 8000;
    if (const char* value = std::getenv("GATEWAY_PORT")) {
        try {
            const int requested = std::stoi(value);
            if (requested > 0 && requested <= 65535) {
                listen_port = requested;
            }
        } catch (const std::exception&) {
            // Keep the default port.
        }
    }

    try {
        block_shutdown_signals();
    } catch (const std::exception& e) {
        std::cerr << "Failed to install shutdown handler: " << e.what() << std::endl;
        return 1;
    }

    Gateway gateway(workers);
    httplib::Server server;
    // Keep response headers and body from waiting on delayed ACKs when the
    // client reuses a connection.
    server.set_tcp_nodelay(true);
    // Bound idle keep-alive connections so shutdown does not wait for the
    // library default of five seconds.
    server.set_keep_alive_timeout(2);
    // Each gateway handler blocks on a synchronous Post to a worker, so with the
    // default thread pool (hardware_concurrency-1, ~15) the gateway can only keep
    // ~15 requests in flight across the whole cluster -- starving the workers'
    // batchers and collapsing under load once inbound concurrency exceeds the
    // pool. Raise it well above per-worker connection pools so the gateway can
    // saturate all workers concurrently.
    server.new_task_queue = [] { return new httplib::ThreadPool(kServerThreads); };
    // inference endpoint
    server.Post("/infer", [&gateway](const httplib::Request& req, httplib::Response& res) {
        try {
            auto admission = gateway.admitRequest();
            if (!admission.has_value()) {
                res.status = 503;
                res.set_header("Retry-After", "1");
                res.set_content(
                    R"({"error":"server overloaded","retry_after_ms":1000})",
                    "application/json"
                );
                return;
            }
            auto request = json::parse(req.body);
            inference::validateInferRequest(request);
            auto response = gateway.routeRequest(request);
            
            res.set_content(response, "application/json");
        } catch (const nlohmann::json::parse_error& e) {
            json error;
            error["error"] = e.what();
            res.status = 400;
            res.set_content(error.dump(), "application/json");
        } catch (const inference::ValidationError& e) {
            json error;
            error["error"] = e.what();
            res.status = 400;
            res.set_content(error.dump(), "application/json");
        } catch (const std::exception& e) {
            json error;
            error["error"] = e.what();
            res.status = 500;
            res.set_content(error.dump(), "application/json");
        }
    });
    // raw float32 tensor endpoint
    server.Post("/infer_tensor", [&gateway](const httplib::Request& req, httplib::Response& res) {
        try {
            auto admission = gateway.admitRequest();
            if (!admission.has_value()) {
                res.status = 503;
                res.set_header("Retry-After", "1");
                res.set_content(
                    R"({"error":"server overloaded","retry_after_ms":1000})",
                    "application/json"
                );
                return;
            }
            const std::string request_id = req.get_header_value("X-Request-Id");
            if (request_id.empty() || request_id.size() > 256) {
                throw std::invalid_argument("X-Request-Id must be 1-256 characters");
            }
            constexpr size_t kMaxTensorBytes = 16'000'000;
            if (req.body.empty() || req.body.size() % 4 != 0 ||
                req.body.size() > kMaxTensorBytes) {
                throw std::invalid_argument(
                    "tensor body must be a non-empty multiple of 4 bytes, at most 16 MB"
                );
            }
            const auto response = gateway.routeTensor(req.body, request_id);
            res.set_header("X-Request-Id", response.request_id);
            res.set_header("X-Node-Id", response.node_id);
            res.set_header("X-Cached", response.cached ? "true" : "false");
            res.set_header("X-Output-Elements", std::to_string(response.output_elements));
            res.set_header("X-Batch-Size", std::to_string(response.batch_size));
            res.set_header("X-Queue-Wait-Us", std::to_string(response.queue_wait_us));
            res.set_header(
                "X-Batch-Execution-Us", std::to_string(response.batch_execution_us)
            );
            res.set_content(response.body, "application/octet-stream");
        } catch (const std::invalid_argument& e) {
            json error;
            error["error"] = e.what();
            res.status = 400;
            res.set_content(error.dump(), "application/json");
        } catch (const std::exception& e) {
            json error;
            error["error"] = e.what();
            res.status = 500;
            res.set_content(error.dump(), "application/json");
        }
    });
    // stats endpoint
    server.Get("/stats", [&gateway](const httplib::Request&, httplib::Response& res) {
        auto stats = gateway.getStats();
        res.set_content(stats.dump(), "application/json");
    });
    std::cout << "Gateway listening on port " << listen_port << std::endl;
    std::cout << "Workers: " << workers.size() << std::endl;
    std::cout << "Circuit breakers enabled" << std::endl;
    std::cout << "Ready!" << std::endl;
    std::thread server_thread([&server, listen_port] {
        server.listen("0.0.0.0", listen_port);
    });
    server.wait_until_ready();
    if (!server.is_running()) {
        std::cerr << "Gateway server failed to start on port " << listen_port << std::endl;
        server_thread.join();
        return 1;
    }
    const int signal_number = wait_for_shutdown_signal();
    std::cout << "Received signal " << signal_number << ", shutting down" << std::endl;
    server.stop();
    server_thread.join();
    return 0;
}