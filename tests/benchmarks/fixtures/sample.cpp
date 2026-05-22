// Synthetic A/B fixture — C++ task scheduler with thread pool.
// NOT for production use. Generated to exercise tree-sitter outline.

#include <atomic>
#include <condition_variable>
#include <functional>
#include <future>
#include <memory>
#include <mutex>
#include <optional>
#include <queue>
#include <stdexcept>
#include <string>
#include <thread>
#include <type_traits>
#include <vector>

namespace sched {

class TaskError : public std::runtime_error {
public:
    explicit TaskError(const std::string &msg) : std::runtime_error(msg) {}
};

class ShutdownError : public TaskError {
public:
    ShutdownError() : TaskError("scheduler is shut down") {}
};

struct WorkerStats {
    std::uint64_t completed{0};
    std::uint64_t failed{0};
    std::uint64_t stolen{0};
};

class Task {
public:
    using Fn = std::function<void()>;

    explicit Task(Fn fn, std::string name = "") noexcept
        : fn_(std::move(fn)), name_(std::move(name)) {}

    void run() { fn_(); }
    const std::string &name() const { return name_; }

private:
    Fn          fn_;
    std::string name_;
};

using TaskPtr = std::unique_ptr<Task>;

class LocalQueue {
public:
    explicit LocalQueue(std::size_t capacity = 256) : capacity_(capacity) {}

    bool push(TaskPtr task) {
        std::unique_lock<std::mutex> lk(mu_);
        if (queue_.size() >= capacity_) return false;
        queue_.push(std::move(task));
        lk.unlock();
        cv_.notify_one();
        return true;
    }

    TaskPtr pop(bool block = false) {
        std::unique_lock<std::mutex> lk(mu_);
        if (block) {
            cv_.wait(lk, [this] { return !queue_.empty() || shutdown_; });
        }
        if (queue_.empty()) return nullptr;
        auto t = std::move(queue_.front());
        queue_.pop();
        return t;
    }

    TaskPtr steal() {
        std::unique_lock<std::mutex> lk(mu_, std::try_to_lock);
        if (!lk || queue_.size() < 2) return nullptr;
        auto t = std::move(queue_.front());
        queue_.pop();
        return t;
    }

    void shutdown() {
        std::unique_lock<std::mutex> lk(mu_);
        shutdown_ = true;
        lk.unlock();
        cv_.notify_all();
    }

    std::size_t size() const {
        std::lock_guard<std::mutex> lk(mu_);
        return queue_.size();
    }

private:
    mutable std::mutex      mu_;
    std::condition_variable cv_;
    std::queue<TaskPtr>     queue_;
    std::size_t             capacity_;
    bool                    shutdown_{false};
};

class Worker {
public:
    Worker(std::size_t id, LocalQueue &own, std::vector<LocalQueue *> &peers)
        : id_(id), own_(own), peers_(peers) {}

    void start() {
        thread_ = std::thread([this] { run(); });
    }

    void join() {
        if (thread_.joinable()) thread_.join();
    }

    WorkerStats stats() const { return stats_; }

private:
    void run() {
        while (true) {
            if (auto t = own_.pop(/*block=*/true)) {
                execute(std::move(t));
            } else if (trySteal()) {
                // stolen task already executed inside trySteal
            } else {
                // queue shut down and nothing to steal
                break;
            }
        }
    }

    bool trySteal() {
        for (auto *peer : peers_) {
            if (peer == &own_) continue;
            if (auto t = peer->steal()) {
                ++stats_.stolen;
                execute(std::move(t));
                return true;
            }
        }
        return false;
    }

    void execute(TaskPtr task) {
        try {
            task->run();
            ++stats_.completed;
        } catch (...) {
            ++stats_.failed;
        }
    }

    std::size_t              id_;
    LocalQueue              &own_;
    std::vector<LocalQueue *> &peers_;
    std::thread              thread_;
    WorkerStats              stats_;
};

class Scheduler {
public:
    explicit Scheduler(std::size_t num_workers = 0) {
        std::size_t n = num_workers > 0 ? num_workers : std::thread::hardware_concurrency();
        queues_.reserve(n);
        workers_.reserve(n);
        std::vector<LocalQueue *> ptrs;
        for (std::size_t i = 0; i < n; i++) {
            queues_.emplace_back(std::make_unique<LocalQueue>());
            ptrs.push_back(queues_.back().get());
        }
        for (std::size_t i = 0; i < n; i++) {
            workers_.emplace_back(std::make_unique<Worker>(i, *queues_[i], ptrs));
            workers_.back()->start();
        }
    }

    ~Scheduler() { shutdown(); }

    void submit(Task::Fn fn, std::string name = "") {
        if (shutdown_) throw ShutdownError{};
        auto task = std::make_unique<Task>(std::move(fn), std::move(name));
        // Round-robin dispatch
        std::size_t idx = next_++ % queues_.size();
        if (!queues_[idx]->push(std::move(task))) {
            // If local queue is full, try others
            for (std::size_t i = 0; i < queues_.size(); i++) {
                std::size_t alt = (idx + i + 1) % queues_.size();
                if (queues_[alt]->push(std::move(task))) return;
            }
            throw TaskError{"all queues full"};
        }
    }

    template <typename F, typename... Args>
    auto async(F &&f, Args &&...args) -> std::future<std::invoke_result_t<F, Args...>> {
        using R = std::invoke_result_t<F, Args...>;
        auto promise = std::make_shared<std::promise<R>>();
        auto future = promise->get_future();
        submit([p = promise, fn = std::forward<F>(f),
                tup = std::make_tuple(std::forward<Args>(args)...)]() mutable {
            try {
                if constexpr (std::is_void_v<R>) {
                    std::apply(fn, std::move(tup));
                    p->set_value();
                } else {
                    p->set_value(std::apply(fn, std::move(tup)));
                }
            } catch (...) {
                p->set_exception(std::current_exception());
            }
        });
        return future;
    }

    void shutdown() {
        if (shutdown_.exchange(true)) return;
        for (auto &q : queues_) q->shutdown();
        for (auto &w : workers_) w->join();
    }

    std::vector<WorkerStats> worker_stats() const {
        std::vector<WorkerStats> out;
        out.reserve(workers_.size());
        for (auto &w : workers_) out.push_back(w->stats());
        return out;
    }

    std::size_t worker_count() const { return workers_.size(); }

private:
    std::vector<std::unique_ptr<LocalQueue>> queues_;
    std::vector<std::unique_ptr<Worker>>     workers_;
    std::atomic<std::size_t>                 next_{0};
    std::atomic<bool>                        shutdown_{false};
};

} // namespace sched
