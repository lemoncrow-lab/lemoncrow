// --- rs_tokio_worker.rs ---
//! A scheduler is initialized with a fixed number of workers. Each worker is
//! driven by a thread. Each worker has a "core" which contains data such as the
//! run queue and other state. When `block_in_place` is called, the worker's
//! "core" is handed off to a new thread allowing the scheduler to continue to
//! make progress while the originating thread blocks.
//!
//! # Shutdown
//!
//! Shutting down the runtime involves the following steps:
//!
//!  1. The Shared::close method is called. This closes the inject queue and
//!     `OwnedTasks` instance and wakes up all worker threads.
//!
//!  2. Each worker thread observes the close signal next time it runs
//!     Core::maintenance by checking whether the inject queue is closed.
//!     The `Core::is_shutdown` flag is set to true.
//!
//!  3. The worker thread calls `pre_shutdown` in parallel. Here, the worker
//!     will keep removing tasks from `OwnedTasks` until it is empty. No new
//!     tasks can be pushed to the `OwnedTasks` during or after this step as it
//!     was closed in step 1.
//!
//!  5. The workers call Shared::shutdown to enter the single-threaded phase of
//!     shutdown. These calls will push their core to `Shared::shutdown_cores`,
//!     and the last thread to push its core will finish the shutdown procedure.
//!
//!  6. The local run queue of each core is emptied, then the inject queue is
//!     emptied.
//!
//! At this point, shutdown has completed. It is not possible for any of the
//! collections to contain any tasks at this point, as each collection was
//! closed first, then emptied afterwards.
//!
//! ## Spawns during shutdown
//!
//! When spawning tasks during shutdown, there are two cases:
//!
//!  * The spawner observes the `OwnedTasks` being open, and the inject queue is
//!    closed.
//!  * The spawner observes the `OwnedTasks` being closed and doesn't check the
//!    inject queue.
//!
//! The first case can only happen if the `OwnedTasks::bind` call happens before
//! or during step 1 of shutdown. In this case, the runtime will clean up the
//! task in step 3 of shutdown.
//!
//! In the latter case, the task was not spawned and the task is immediately
//! cancelled by the spawner.
//!
//! The correctness of shutdown requires both the inject queue and `OwnedTasks`
//! collection to have a closed bit. With a close bit on only the inject queue,
//! spawning could run in to a situation where a task is successfully bound long
//! after the runtime has shut down. With a close bit on only the `OwnedTasks`,
//! the first spawning situation could result in the notification being pushed
//! to the inject queue after step 6 of shutdown, which would leave a task in
//! the inject queue indefinitely. This would be a ref-count cycle and a memory
//! leak.

use crate::loom::sync::{Arc, Mutex};
use crate::runtime;
use crate::runtime::context;
use crate::runtime::scheduler::multi_thread::{
    idle, queue, Counters, Handle, Idle, Overflow, Parker, Stats, TraceStatus, Unparker,
};
use crate::runtime::scheduler::{inject, Defer, Lock};
use crate::runtime::task::OwnedTasks;
use crate::runtime::{
    blocking, coop, driver, scheduler, task, Config, SchedulerMetrics, WorkerMetrics,
};
use crate::util::atomic_cell::AtomicCell;
use crate::util::rand::{FastRand, RngSeedGenerator};

use std::cell::RefCell;
use std::task::Waker;
use std::time::Duration;

cfg_unstable_metrics! {
    mod metrics;
}

cfg_taskdump! {
    mod taskdump;
}

cfg_not_taskdump! {
    mod taskdump_mock;
}

/// A scheduler worker
pub(super) struct Worker {
    /// Reference to scheduler's handle
    handle: Arc<Handle>,

    /// Index holding this worker's remote state
    index: usize,

    /// Used to hand-off a worker's core to another thread.
    core: AtomicCell<Core>,
}

/// Core data
struct Core {
    /// Used to schedule bookkeeping tasks every so often.
    tick: u32,

    /// When a task is scheduled from a worker, it is stored in this slot. The
    /// worker will check this slot for a task **before** checking the run
    /// queue. This effectively results in the **last** scheduled task to be run
    /// next (LIFO). This is an optimization for improving locality which
    /// benefits message passing patterns and helps to reduce latency.
    lifo_slot: Option<Notified>,

    /// When `true`, locally scheduled tasks go to the LIFO slot. When `false`,
    /// they go to the back of the `run_queue`.
    lifo_enabled: bool,

    /// The worker-local run queue.
    run_queue: queue::Local<Arc<Handle>>,

    /// True if the worker is currently searching for more work. Searching
    /// involves attempting to steal from other workers.
    is_searching: bool,

    /// True if the scheduler is being shutdown
    is_shutdown: bool,

    /// True if the scheduler is being traced
    is_traced: bool,

    /// Parker
    ///
    /// Stored in an `Option` as the parker is added / removed to make the
    /// borrow checker happy.
    park: Option<Parker>,

    /// Per-worker runtime stats
    stats: Stats,

    /// How often to check the global queue
    global_queue_interval: u32,

    /// Fast random number generator.
    rand: FastRand,
}

/// State shared across all workers
pub(crate) struct Shared {
    /// Per-worker remote state. All other workers have access to this and is
    /// how they communicate between each other.
    remotes: Box<[Remote]>,

    /// Global task queue used for:
    ///  1. Submit work to the scheduler while **not** currently on a worker thread.
    ///  2. Submit work to the scheduler when a worker run queue is saturated
    pub(super) inject: inject::Shared<Arc<Handle>>,

    /// Coordinates idle workers
    idle: Idle,

    /// Collection of all active tasks spawned onto this executor.
    pub(crate) owned: OwnedTasks<Arc<Handle>>,

    /// Data synchronized by the scheduler mutex
    pub(super) synced: Mutex<Synced>,

    /// Cores that have observed the shutdown signal
    ///
    /// The core is **not** placed back in the worker to avoid it from being
    /// stolen by a thread that was spawned as part of `block_in_place`.
    #[allow(clippy::vec_box)] // we're moving an already-boxed value
    shutdown_cores: Mutex<Vec<Box<Core>>>,

    /// The number of cores that have observed the trace signal.
    pub(super) trace_status: TraceStatus,

    /// Scheduler configuration options
    config: Config,

    /// Collects metrics from the runtime.
    pub(super) scheduler_metrics: SchedulerMetrics,

    pub(super) worker_metrics: Box<[WorkerMetrics]>,

    /// Only held to trigger some code on drop. This is used to get internal
    /// runtime metrics that can be useful when doing performance
    /// investigations. This does nothing (empty struct, no drop impl) unless
    /// the `tokio_internal_mt_counters` `cfg` flag is set.
    _counters: Counters,
}

/// Data synchronized by the scheduler mutex
pub(crate) struct Synced {
    /// Synchronized state for `Idle`.
    pub(super) idle: idle::Synced,

    /// Synchronized state for `Inject`.
    pub(crate) inject: inject::Synced,
}

/// Used to communicate with a worker from other threads.
struct Remote {
    /// Steals tasks from this worker.
    pub(super) steal: queue::Steal<Arc<Handle>>,

    /// Unparks the associated worker thread
    unpark: Unparker,
}

/// Thread-local context
pub(crate) struct Context {
    /// Worker
    worker: Arc<Worker>,

    /// Core data
    core: RefCell<Option<Box<Core>>>,

    /// Tasks to wake after resource drivers are polled. This is mostly to
    /// handle yielded tasks.
    pub(crate) defer: Defer,
}

/// Starts the workers
pub(crate) struct Launch(Vec<Arc<Worker>>);

/// Running a task may consume the core. If the core is still available when
/// running the task completes, it is returned. Otherwise, the worker will need
/// to stop processing.
type RunResult = Result<Box<Core>, ()>;

/// A task handle
type Task = task::Task<Arc<Handle>>;

/// A notified task handle
type Notified = task::Notified<Arc<Handle>>;

/// Value picked out of thin-air. Running the LIFO slot a handful of times
/// seems sufficient to benefit from locality. More than 3 times probably is
/// overweighing. The value can be tuned in the future with data that shows
/// improvements.
const MAX_LIFO_POLLS_PER_TICK: usize = 3;

pub(super) fn create(
    size: usize,
    park: Parker,
    driver_handle: driver::Handle,
    blocking_spawner: blocking::Spawner,
    seed_generator: RngSeedGenerator,
    config: Config,
) -> (Arc<Handle>, Launch) {
    let mut cores = Vec::with_capacity(size);
    let mut remotes = Vec::with_capacity(size);
    let mut worker_metrics = Vec::with_capacity(size);

    // Create the local queues
    for _ in 0..size {
        let (steal, run_queue) = queue::local();

        let park = park.clone();
        let unpark = park.unpark();
        let metrics = WorkerMetrics::from_config(&config);
        let stats = Stats::new(&metrics);

        cores.push(Box::new(Core {
            tick: 0,
            lifo_slot: None,
            lifo_enabled: !config.disable_lifo_slot,
            run_queue,
            is_searching: false,
            is_shutdown: false,
            is_traced: false,
            park: Some(park),
            global_queue_interval: stats.tuned_global_queue_interval(&config),
            stats,
            rand: FastRand::from_seed(config.seed_generator.next_seed()),
        }));

        remotes.push(Remote { steal, unpark });
        worker_metrics.push(metrics);
    }

    let (idle, idle_synced) = Idle::new(size);
    let (inject, inject_synced) = inject::Shared::new();

    let remotes_len = remotes.len();
    let handle = Arc::new(Handle {
        shared: Shared {
            remotes: remotes.into_boxed_slice(),
            inject,
            idle,
            owned: OwnedTasks::new(size),
            synced: Mutex::new(Synced {
                idle: idle_synced,
                inject: inject_synced,
            }),
            shutdown_cores: Mutex::new(vec![]),
            trace_status: TraceStatus::new(remotes_len),
            config,
            scheduler_metrics: SchedulerMetrics::new(),
            worker_metrics: worker_metrics.into_boxed_slice(),
            _counters: Counters,
        },
        driver: driver_handle,
        blocking_spawner,
        seed_generator,
    });

    let mut launch = Launch(vec![]);

    for (index, core) in cores.drain(..).enumerate() {
        launch.0.push(Arc::new(Worker {
            handle: handle.clone(),
            index,
            core: AtomicCell::new(Some(core)),
        }));
    }

    (handle, launch)
}

#[track_caller]
pub(crate) fn block_in_place<F, R>(f: F) -> R
where
    F: FnOnce() -> R,
{
    // Try to steal the worker core back
    struct Reset {
        take_core: bool,
        budget: coop::Budget,
    }

    impl Drop for Reset {
        fn drop(&mut self) {
            with_current(|maybe_cx| {
                if let Some(cx) = maybe_cx {
                    if self.take_core {
                        let core = cx.worker.core.take();
                        let mut cx_core = cx.core.borrow_mut();
                        assert!(cx_core.is_none());
                        *cx_core = core;
                    }

                    // Reset the task budget as we are re-entering the
                    // runtime.
                    coop::set(self.budget);
                }
            });
        }
    }

    let mut had_entered = false;
    let mut take_core = false;

    let setup_result = with_current(|maybe_cx| {
        match (
            crate::runtime::context::current_enter_context(),
            maybe_cx.is_some(),
        ) {
            (context::EnterRuntime::Entered { .. }, true) => {
                // We are on a thread pool runtime thread, so we just need to
                // set up blocking.
                had_entered = true;
            }
            (
                context::EnterRuntime::Entered {
                    allow_block_in_place,
                },
                false,
            ) => {
                // We are on an executor, but _not_ on the thread pool.  That is
                // _only_ okay if we are in a thread pool runtime's block_on
                // method:
                if allow_block_in_place {
                    had_entered = true;
                    return Ok(());
                } else {
                    // This probably means we are on the current_thread runtime or in a
                    // LocalSet, where it is _not_ okay to block.
                    return Err(
                        "can call blocking only when running on the multi-threaded runtime",
                    );
                }
            }
            (context::EnterRuntime::NotEntered, true) => {
                // This is a nested call to block_in_place (we already exited).
                // All the necessary setup has already been done.
                return Ok(());
            }
            (context::EnterRuntime::NotEntered, false) => {
                // We are outside of the tokio runtime, so blocking is fine.
                // We can also skip all of the thread pool blocking setup steps.
                return Ok(());
            }
        }

        let cx = maybe_cx.expect("no .is_some() == false cases above should lead here");

        // Get the worker core. If none is set, then blocking is fine!
        let mut core = match cx.core.borrow_mut().take() {
            Some(core) => core,
            None => return Ok(()),
        };

        // If we heavily call `spawn_blocking`, there might be no available thread to
        // run this core. Except for the task in the lifo_slot, all tasks can be
        // stolen, so we move the task out of the lifo_slot to the run_queue.
        if let Some(task) = core.lifo_slot.take() {
            core.run_queue
                .push_back_or_overflow(task, &*cx.worker.handle, &mut core.stats);
        }

        // We are taking the core from the context and sending it to another
        // thread.
        take_core = true;

        // The parker should be set here
        assert!(core.park.is_some());

        // In order to block, the core must be sent to another thread for
        // execution.
        //
        // First, move the core back into the worker's shared core slot.
        cx.worker.core.set(core);

        // Next, clone the worker handle and send it to a new thread for
        // processing.
        //
        // Once the blocking task is done executing, we will attempt to
        // steal the core back.
        let worker = cx.worker.clone();
        runtime::spawn_blocking(move || run(worker));
        Ok(())
    });

    if let Err(panic_message) = setup_result {
        panic!("{}", panic_message);
    }

    if had_entered {
        // Unset the current task's budget. Blocking sections are not
        // constrained by task budgets.
        let _reset = Reset {
            take_core,
            budget: coop::stop(),
        };

        crate::runtime::context::exit_runtime(f)
    } else {
        f()
    }
}

impl Launch {
    pub(crate) fn launch(mut self) {
        for worker in self.0.drain(..) {
            runtime::spawn_blocking(move || run(worker));
        }
    }
}

fn run(worker: Arc<Worker>) {
    #[allow(dead_code)]
    struct AbortOnPanic;

    impl Drop for AbortOnPanic {
        fn drop(&mut self) {
            if std::thread::panicking() {
                eprintln!("worker thread panicking; aborting process");
                std::process::abort();
            }
        }
    }

    // Catching panics on worker threads in tests is quite tricky. Instead, when
    // debug assertions are enabled, we just abort the process.
    #[cfg(debug_assertions)]
    let _abort_on_panic = AbortOnPanic;

    // Acquire a core. If this fails, then another thread is running this
    // worker and there is nothing further to do.
    let core = match worker.core.take() {
        Some(core) => core,
        None => return,
    };

    let handle = scheduler::Handle::MultiThread(worker.handle.clone());

    crate::runtime::context::enter_runtime(&handle, true, |_| {
        // Set the worker context.
        let cx = scheduler::Context::MultiThread(Context {
            worker,
            core: RefCell::new(None),
            defer: Defer::new(),
        });

        context::set_scheduler(&cx, || {
            let cx = cx.expect_multi_thread();

            // This should always be an error. It only returns a `Result` to support
            // using `?` to short circuit.
            assert!(cx.run(core).is_err());

            // Check if there are any deferred tasks to notify. This can happen when
            // the worker core is lost due to `block_in_place()` being called from
            // within the task.
            cx.defer.wake();
        });
    });
}

impl Context {
    fn run(&self, mut core: Box<Core>) -> RunResult {
        // Reset `lifo_enabled` here in case the core was previously stolen from
        // a task that had the LIFO slot disabled.
        self.reset_lifo_enabled(&mut core);

        // Start as "processing" tasks as polling tasks from the local queue
        // will be one of the first things we do.
        core.stats.start_processing_scheduled_tasks();

        while !core.is_shutdown {
            self.assert_lifo_enabled_is_correct(&core);

            if core.is_traced {
                core = self.worker.handle.trace_core(core);
            }

            // Increment the tick
            core.tick();

            // Run maintenance, if needed
            core = self.maintenance(core);

            // First, check work available to the current worker.
            if let Some(task) = core.next_task(&self.worker) {
                core = self.run_task(task, core)?;
                continue;
            }

            // We consumed all work in the queues and will start searching for work.
            core.stats.end_processing_scheduled_tasks();

            // There is no more **local** work to process, try to steal work
            // from other workers.
            if let Some(task) = core.steal_work(&self.worker) {
                // Found work, switch back to processing
                core.stats.start_processing_scheduled_tasks();
                core = self.run_task(task, core)?;
            } else {
                // Wait for work
                core = if !self.defer.is_empty() {
                    self.park_timeout(core, Some(Duration::from_millis(0)))
                } else {
                    self.park(core)
                };
                core.stats.start_processing_scheduled_tasks();
            }
        }

        core.pre_shutdown(&self.worker);
        // Signal shutdown
        self.worker.handle.shutdown_core(core);
        Err(())
    }

    fn run_task(&self, task: Notified, mut core: Box<Core>) -> RunResult {
        let task = self.worker.handle.shared.owned.assert_owner(task);

        // Make sure the worker is not in the **searching** state. This enables
        // another idle worker to try to steal work.
        core.transition_from_searching(&self.worker);

        self.assert_lifo_enabled_is_correct(&core);

        // Measure the poll start time. Note that we may end up polling other
        // tasks under this measurement. In this case, the tasks came from the
        // LIFO slot and are considered part of the current task for scheduling
        // purposes. These tasks inherent the "parent"'s limits.
        core.stats.start_poll();

        // Make the core available to the runtime context
        *self.core.borrow_mut() = Some(core);

        // Run the task
        coop::budget(|| {
            task.run();
            let mut lifo_polls = 0;

            // As long as there is budget remaining and a task exists in the
            // `lifo_slot`, then keep running.
            loop {
                // Check if we still have the core. If not, the core was stolen
                // by another worker.
                let mut core = match self.core.borrow_mut().take() {
                    Some(core) => core,
                    None => {
                        // In this case, we cannot call `reset_lifo_enabled()`
                        // because the core was stolen. The stealer will handle
                        // that at the top of `Context::run`
                        return Err(());
                    }
                };

                // Check for a task in the LIFO slot
                let task = match core.lifo_slot.take() {
                    Some(task) => task,
                    None => {
                        self.reset_lifo_enabled(&mut core);
                        core.stats.end_poll();
                        return Ok(core);
                    }
                };

                if !coop::has_budget_remaining() {
                    core.stats.end_poll();

                    // Not enough budget left to run the LIFO task, push it to
                    // the back of the queue and return.
                    core.run_queue.push_back_or_overflow(
                        task,
                        &*self.worker.handle,
                        &mut core.stats,
                    );
                    // If we hit this point, the LIFO slot should be enabled.
                    // There is no need to reset it.
                    debug_assert!(core.lifo_enabled);
                    return Ok(core);
                }

                // Track that we are about to run a task from the LIFO slot.
                lifo_polls += 1;
                super::counters::inc_lifo_schedules();

                // Disable the LIFO slot if we reach our limit
                //
                // In ping-ping style workloads where task A notifies task B,
                // which notifies task A again, continuously prioritizing the
                // LIFO slot can cause starvation as these two tasks will
                // repeatedly schedule the other. To mitigate this, we limit the
                // number of times the LIFO slot is prioritized.
                if lifo_polls >= MAX_LIFO_POLLS_PER_TICK {
                    core.lifo_enabled = false;
                    super::counters::inc_lifo_capped();
                }

                // Run the LIFO task, then loop
                *self.core.borrow_mut() = Some(core);
                let task = self.worker.handle.shared.owned.assert_owner(task);
                task.run();
            }
        })
    }

    fn reset_lifo_enabled(&self, core: &mut Core) {
        core.lifo_enabled = !self.worker.handle.shared.config.disable_lifo_slot;
    }

    fn assert_lifo_enabled_is_correct(&self, core: &Core) {
        debug_assert_eq!(
            core.lifo_enabled,
            !self.worker.handle.shared.config.disable_lifo_slot
        );
    }

    fn maintenance(&self, mut core: Box<Core>) -> Box<Core> {
        if core.tick % self.worker.handle.shared.config.event_interval == 0 {
            super::counters::inc_num_maintenance();

            core.stats.end_processing_scheduled_tasks();

            // Call `park` with a 0 timeout. This enables the I/O driver, timer, ...
            // to run without actually putting the thread to sleep.
            core = self.park_timeout(core, Some(Duration::from_millis(0)));

            // Run regularly scheduled maintenance
            core.maintenance(&self.worker);

            core.stats.start_processing_scheduled_tasks();
        }

        core
    }

    /// Parks the worker thread while waiting for tasks to execute.
    ///
    /// This function checks if indeed there's no more work left to be done before parking.
    /// Also important to notice that, before parking, the worker thread will try to take
    /// ownership of the Driver (IO/Time) and dispatch any events that might have fired.
    /// Whenever a worker thread executes the Driver loop, all waken tasks are scheduled
    /// in its own local queue until the queue saturates (ntasks > `LOCAL_QUEUE_CAPACITY`).
    /// When the local queue is saturated, the overflow tasks are added to the injection queue
    /// from where other workers can pick them up.
    /// Also, we rely on the workstealing algorithm to spread the tasks amongst workers
    /// after all the IOs get dispatched
    fn park(&self, mut core: Box<Core>) -> Box<Core> {
        if let Some(f) = &self.worker.handle.shared.config.before_park {
            f();
        }

        if core.transition_to_parked(&self.worker) {
            while !core.is_shutdown && !core.is_traced {
                core.stats.about_to_park();
                core = self.park_timeout(core, None);

                // Run regularly scheduled maintenance
                core.maintenance(&self.worker);

                if core.transition_from_parked(&self.worker) {
                    break;
                }
            }
        }

        if let Some(f) = &self.worker.handle.shared.config.after_unpark {
            f();
        }
        core
    }

    fn park_timeout(&self, mut core: Box<Core>, duration: Option<Duration>) -> Box<Core> {
        self.assert_lifo_enabled_is_correct(&core);

        // Take the parker out of core
        let mut park = core.park.take().expect("park missing");

        // Store `core` in context
        *self.core.borrow_mut() = Some(core);

        // Park thread
        if let Some(timeout) = duration {
            park.park_timeout(&self.worker.handle.driver, timeout);
        } else {
            park.park(&self.worker.handle.driver);
        }

        self.defer.wake();

        // Remove `core` from context
        core = self.core.borrow_mut().take().expect("core missing");

        // Place `park` back in `core`
        core.park = Some(park);

        if core.should_notify_others() {
            self.worker.handle.notify_parked_local();
        }

        core
    }

    pub(crate) fn defer(&self, waker: &Waker) {
        self.defer.defer(waker);
    }

    #[allow(dead_code)]
    pub(crate) fn get_worker_index(&self) -> usize {
        self.worker.index
    }
}

impl Core {
    /// Increment the tick
    fn tick(&mut self) {
        self.tick = self.tick.wrapping_add(1);
    }

    /// Return the next notified task available to this worker.
    fn next_task(&mut self, worker: &Worker) -> Option<Notified> {
        if self.tick % self.global_queue_interval == 0 {
            // Update the global queue interval, if needed
            self.tune_global_queue_interval(worker);

            worker
                .handle
                .next_remote_task()
                .or_else(|| self.next_local_task())
        } else {
            let maybe_task = self.next_local_task();

            if maybe_task.is_some() {
                return maybe_task;
            }

            if worker.inject().is_empty() {
                return None;
            }

            // Other threads can only **remove** tasks from the current worker's
            // `run_queue`. So, we can be confident that by the time we call
            // `run_queue.push_back` below, there will be *at least* `cap`
            // available slots in the queue.
            let cap = usize::min(
                self.run_queue.remaining_slots(),
                self.run_queue.max_capacity() / 2,
            );

            // The worker is currently idle, pull a batch of work from the
            // injection queue. We don't want to pull *all* the work so other
            // workers can also get some.
            let n = usize::min(
                worker.inject().len() / worker.handle.shared.remotes.len() + 1,
                cap,
            );

            // Take at least one task since the first task is returned directly
            // and not pushed onto the local queue.
            let n = usize::max(1, n);

            let mut synced = worker.handle.shared.synced.lock();
            // safety: passing in the correct `inject::Synced`.
            let mut tasks = unsafe { worker.inject().pop_n(&mut synced.inject, n) };

            // Pop the first task to return immediately
            let ret = tasks.next();

            // Push the rest of the on the run queue
            self.run_queue.push_back(tasks);

            ret
        }
    }

    fn next_local_task(&mut self) -> Option<Notified> {
        self.lifo_slot.take().or_else(|| self.run_queue.pop())
    }

    /// Function responsible for stealing tasks from another worker
    ///
    /// Note: Only if less than half the workers are searching for tasks to steal
    /// a new worker will actually try to steal. The idea is to make sure not all
    /// workers will be trying to steal at the same time.
    fn steal_work(&mut self, worker: &Worker) -> Option<Notified> {
        if !self.transition_to_searching(worker) {
            return None;
        }

        let num = worker.handle.shared.remotes.len();
        // Start from a random worker
        let start = self.rand.fastrand_n(num as u32) as usize;

        for i in 0..num {
            let i = (start + i) % num;

            // Don't steal from ourself! We know we don't have work.
            if i == worker.index {
                continue;
            }

            let target = &worker.handle.shared.remotes[i];
            if let Some(task) = target
                .steal
                .steal_into(&mut self.run_queue, &mut self.stats)
            {
                return Some(task);
            }
        }

        // Fallback on checking the global queue
        worker.handle.next_remote_task()
    }

    fn transition_to_searching(&mut self, worker: &Worker) -> bool {
        if !self.is_searching {
            self.is_searching = worker.handle.shared.idle.transition_worker_to_searching();
        }

        self.is_searching
    }

    fn transition_from_searching(&mut self, worker: &Worker) {
        if !self.is_searching {
            return;
        }

        self.is_searching = false;
        worker.handle.transition_worker_from_searching();
    }

    fn has_tasks(&self) -> bool {
        self.lifo_slot.is_some() || self.run_queue.has_tasks()
    }

    fn should_notify_others(&self) -> bool {
        // If there are tasks available to steal, but this worker is not
        // looking for tasks to steal, notify another worker.
        if self.is_searching {
            return false;
        }
        self.lifo_slot.is_some() as usize + self.run_queue.len() > 1
    }

    /// Prepares the worker state for parking.
    ///
    /// Returns true if the transition happened, false if there is work to do first.
    fn transition_to_parked(&mut self, worker: &Worker) -> bool {
        // Workers should not park if they have work to do
        if self.has_tasks() || self.is_traced {
            return false;
        }

        // When the final worker transitions **out** of searching to parked, it
        // must check all the queues one last time in case work materialized
        // between the last work scan and transitioning out of searching.
        let is_last_searcher = worker.handle.shared.idle.transition_worker_to_parked(
            &worker.handle.shared,
            worker.index,
            self.is_searching,
        );

        // The worker is no longer searching. Setting this is the local cache
        // only.
        self.is_searching = false;

        if is_last_searcher {
            worker.handle.notify_if_work_pending();
        }

        true
    }

    /// Returns `true` if the transition happened.
    fn transition_from_parked(&mut self, worker: &Worker) -> bool {
        // If a task is in the lifo slot/run queue, then we must unpark regardless of
        // being notified
        if self.has_tasks() {
            // When a worker wakes, it should only transition to the "searching"
            // state when the wake originates from another worker *or* a new task
            // is pushed. We do *not* want the worker to transition to "searching"
            // when it wakes when the I/O driver receives new events.
            self.is_searching = !worker
                .handle
                .shared
                .idle
                .unpark_worker_by_id(&worker.handle.shared, worker.index);
            return true;
        }

        if worker
            .handle
            .shared
            .idle
            .is_parked(&worker.handle.shared, worker.index)
        {
            return false;
        }

        // When unparked, the worker is in the searching state.
        self.is_searching = true;
        true
    }

    /// Runs maintenance work such as checking the pool's state.
    fn maintenance(&mut self, worker: &Worker) {
        self.stats
            .submit(&worker.handle.shared.worker_metrics[worker.index]);

        if !self.is_shutdown {
            // Check if the scheduler has been shutdown
            let synced = worker.handle.shared.synced.lock();
            self.is_shutdown = worker.inject().is_closed(&synced.inject);
        }

        if !self.is_traced {
            // Check if the worker should be tracing.
            self.is_traced = worker.handle.shared.trace_status.trace_requested();
        }
    }

    /// Signals all tasks to shut down, and waits for them to complete. Must run
    /// before we enter the single-threaded phase of shutdown processing.
    fn pre_shutdown(&mut self, worker: &Worker) {
        // Start from a random inner list
        let start = self
            .rand
            .fastrand_n(worker.handle.shared.owned.get_shard_size() as u32);
        // Signal to all tasks to shut down.
        worker
            .handle
            .shared
            .owned
            .close_and_shutdown_all(start as usize);

        self.stats
            .submit(&worker.handle.shared.worker_metrics[worker.index]);
    }

    /// Shuts down the core.
    fn shutdown(&mut self, handle: &Handle) {
        // Take the core
        let mut park = self.park.take().expect("park missing");

        // Drain the queue
        while self.next_local_task().is_some() {}

        park.shutdown(&handle.driver);
    }

    fn tune_global_queue_interval(&mut self, worker: &Worker) {
        let next = self
            .stats
            .tuned_global_queue_interval(&worker.handle.shared.config);

        // Smooth out jitter
        if abs_diff(self.global_queue_interval, next) > 2 {
            self.global_queue_interval = next;
        }
    }
}

impl Worker {
    /// Returns a reference to the scheduler's injection queue.
    fn inject(&self) -> &inject::Shared<Arc<Handle>> {
        &self.handle.shared.inject
    }
}

// TODO: Move `Handle` impls into handle.rs
impl task::Schedule for Arc<Handle> {
    fn release(&self, task: &Task) -> Option<Task> {
        self.shared.owned.remove(task)
    }

    fn schedule(&self, task: Notified) {
        self.schedule_task(task, false);
    }

    fn yield_now(&self, task: Notified) {
        self.schedule_task(task, true);
    }
}

impl Handle {
    pub(super) fn schedule_task(&self, task: Notified, is_yield: bool) {
        with_current(|maybe_cx| {
            if let Some(cx) = maybe_cx {
                // Make sure the task is part of the **current** scheduler.
                if self.ptr_eq(&cx.worker.handle) {
                    // And the current thread still holds a core
                    if let Some(core) = cx.core.borrow_mut().as_mut() {
                        self.schedule_local(core, task, is_yield);
                        return;
                    }
                }
            }

            // Otherwise, use the inject queue.
            self.push_remote_task(task);
            self.notify_parked_remote();
        });
    }

    pub(super) fn schedule_option_task_without_yield(&self, task: Option<Notified>) {
        if let Some(task) = task {
            self.schedule_task(task, false);
        }
    }

    fn schedule_local(&self, core: &mut Core, task: Notified, is_yield: bool) {
        core.stats.inc_local_schedule_count();

        // Spawning from the worker thread. If scheduling a "yield" then the
        // task must always be pushed to the back of the queue, enabling other
        // tasks to be executed. If **not** a yield, then there is more
        // flexibility and the task may go to the front of the queue.
        let should_notify = if is_yield || !core.lifo_enabled {
            core.run_queue
                .push_back_or_overflow(task, self, &mut core.stats);
            true
        } else {
            // Push to the LIFO slot
            let prev = core.lifo_slot.take();
            let ret = prev.is_some();

            if let Some(prev) = prev {
                core.run_queue
                    .push_back_or_overflow(prev, self, &mut core.stats);
            }

            core.lifo_slot = Some(task);

            ret
        };

        // Only notify if not currently parked. If `park` is `None`, then the
        // scheduling is from a resource driver. As notifications often come in
        // batches, the notification is delayed until the park is complete.
        if should_notify && core.park.is_some() {
            self.notify_parked_local();
        }
    }

    fn next_remote_task(&self) -> Option<Notified> {
        if self.shared.inject.is_empty() {
            return None;
        }

        let mut synced = self.shared.synced.lock();
        // safety: passing in correct `idle::Synced`
        unsafe { self.shared.inject.pop(&mut synced.inject) }
    }

    fn push_remote_task(&self, task: Notified) {
        self.shared.scheduler_metrics.inc_remote_schedule_count();

        let mut synced = self.shared.synced.lock();
        // safety: passing in correct `idle::Synced`
        unsafe {
            self.shared.inject.push(&mut synced.inject, task);
        }
    }

    pub(super) fn close(&self) {
        if self
            .shared
            .inject
            .close(&mut self.shared.synced.lock().inject)
        {
            self.notify_all();
        }
    }

    fn notify_parked_local(&self) {
        super::counters::inc_num_inc_notify_local();

        if let Some(index) = self.shared.idle.worker_to_notify(&self.shared) {
            super::counters::inc_num_unparks_local();
            self.shared.remotes[index].unpark.unpark(&self.driver);
        }
    }

    fn notify_parked_remote(&self) {
        if let Some(index) = self.shared.idle.worker_to_notify(&self.shared) {
            self.shared.remotes[index].unpark.unpark(&self.driver);
        }
    }

    pub(super) fn notify_all(&self) {
        for remote in &self.shared.remotes[..] {
            remote.unpark.unpark(&self.driver);
        }
    }

    fn notify_if_work_pending(&self) {
        for remote in &self.shared.remotes[..] {
            if !remote.steal.is_empty() {
                self.notify_parked_local();
                return;
            }
        }

        if !self.shared.inject.is_empty() {
            self.notify_parked_local();
        }
    }

    fn transition_worker_from_searching(&self) {
        if self.shared.idle.transition_worker_from_searching() {
            // We are the final searching worker. Because work was found, we
            // need to notify another worker.
            self.notify_parked_local();
        }
    }

    /// Signals that a worker has observed the shutdown signal and has replaced
    /// its core back into its handle.
    ///
    /// If all workers have reached this point, the final cleanup is performed.
    fn shutdown_core(&self, core: Box<Core>) {
        let mut cores = self.shared.shutdown_cores.lock();
        cores.push(core);

        if cores.len() != self.shared.remotes.len() {
            return;
        }

        debug_assert!(self.shared.owned.is_empty());

        for mut core in cores.drain(..) {
            core.shutdown(self);
        }

        // Drain the injection queue
        //
        // We already shut down every task, so we can simply drop the tasks.
        while let Some(task) = self.next_remote_task() {
            drop(task);
        }
    }

    fn ptr_eq(&self, other: &Handle) -> bool {
        std::ptr::eq(self, other)
    }
}

impl Overflow<Arc<Handle>> for Handle {
    fn push(&self, task: task::Notified<Arc<Handle>>) {
        self.push_remote_task(task);
    }

    fn push_batch<I>(&self, iter: I)
    where
        I: Iterator<Item = task::Notified<Arc<Handle>>>,
    {
        unsafe {
            self.shared.inject.push_batch(self, iter);
        }
    }
}

pub(crate) struct InjectGuard<'a> {
    lock: crate::loom::sync::MutexGuard<'a, Synced>,
}

impl<'a> AsMut<inject::Synced> for InjectGuard<'a> {
    fn as_mut(&mut self) -> &mut inject::Synced {
        &mut self.lock.inject
    }
}

impl<'a> Lock<inject::Synced> for &'a Handle {
    type Handle = InjectGuard<'a>;

    fn lock(self) -> Self::Handle {
        InjectGuard {
            lock: self.shared.synced.lock(),
        }
    }
}

#[track_caller]
fn with_current<R>(f: impl FnOnce(Option<&Context>) -> R) -> R {
    use scheduler::Context::MultiThread;

    context::with_scheduler(|ctx| match ctx {
        Some(MultiThread(ctx)) => f(Some(ctx)),
        _ => f(None),
    })
}

// `u32::abs_diff` is not available on Tokio's MSRV.
fn abs_diff(a: u32, b: u32) -> u32 {
    if a > b {
        a - b
    } else {
        b - a
    }
}


// --- rs_rustc_ast.rs ---
//! The Rust abstract syntax tree module.
//!
//! This module contains common structures forming the language AST.
//! Two main entities in the module are [`Item`] (which represents an AST element with
//! additional metadata), and [`ItemKind`] (which represents a concrete type and contains
//! information specific to the type of the item).
//!
//! Other module items worth mentioning:
//! - [`Ty`] and [`TyKind`]: A parsed Rust type.
//! - [`Expr`] and [`ExprKind`]: A parsed Rust expression.
//! - [`Pat`] and [`PatKind`]: A parsed Rust pattern. Patterns are often dual to expressions.
//! - [`Stmt`] and [`StmtKind`]: An executable action that does not return a value.
//! - [`FnDecl`], [`FnHeader`] and [`Param`]: Metadata associated with a function declaration.
//! - [`Generics`], [`GenericParam`], [`WhereClause`]: Metadata associated with generic parameters.
//! - [`EnumDef`] and [`Variant`]: Enum declaration.
//! - [`MetaItemLit`] and [`LitKind`]: Literal expressions.
//! - [`MacroDef`], [`MacStmtStyle`], [`MacCall`]: Macro definition and invocation.
//! - [`Attribute`]: Metadata associated with item.
//! - [`UnOp`], [`BinOp`], and [`BinOpKind`]: Unary and binary operators.

pub use crate::format::*;
pub use crate::util::parser::ExprPrecedence;
pub use rustc_span::AttrId;
pub use GenericArgs::*;
pub use UnsafeSource::*;

use crate::ptr::P;
use crate::token::{self, CommentKind, Delimiter};
use crate::tokenstream::{DelimSpan, LazyAttrTokenStream, TokenStream};
pub use rustc_ast_ir::{Movability, Mutability};
use rustc_data_structures::packed::Pu128;
use rustc_data_structures::stable_hasher::{HashStable, StableHasher};
use rustc_data_structures::stack::ensure_sufficient_stack;
use rustc_data_structures::sync::Lrc;
use rustc_macros::HashStable_Generic;
use rustc_span::source_map::{respan, Spanned};
use rustc_span::symbol::{kw, sym, Ident, Symbol};
use rustc_span::{ErrorGuaranteed, Span, DUMMY_SP};
use std::fmt;
use std::mem;
use thin_vec::{thin_vec, ThinVec};

/// A "Label" is an identifier of some point in sources,
/// e.g. in the following code:
///
/// ```rust
/// 'outer: loop {
///     break 'outer;
/// }
/// ```
///
/// `'outer` is a label.
#[derive(Clone, Encodable, Decodable, Copy, HashStable_Generic, Eq, PartialEq)]
pub struct Label {
    pub ident: Ident,
}

impl fmt::Debug for Label {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "label({:?})", self.ident)
    }
}

/// A "Lifetime" is an annotation of the scope in which variable
/// can be used, e.g. `'a` in `&'a i32`.
#[derive(Clone, Encodable, Decodable, Copy, PartialEq, Eq)]
pub struct Lifetime {
    pub id: NodeId,
    pub ident: Ident,
}

impl fmt::Debug for Lifetime {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "lifetime({}: {})", self.id, self)
    }
}

impl fmt::Display for Lifetime {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.ident.name)
    }
}

/// A "Path" is essentially Rust's notion of a name.
///
/// It's represented as a sequence of identifiers,
/// along with a bunch of supporting information.
///
/// E.g., `std::cmp::PartialEq`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Path {
    pub span: Span,
    /// The segments in the path: the things separated by `::`.
    /// Global paths begin with `kw::PathRoot`.
    pub segments: ThinVec<PathSegment>,
    pub tokens: Option<LazyAttrTokenStream>,
}

impl PartialEq<Symbol> for Path {
    #[inline]
    fn eq(&self, symbol: &Symbol) -> bool {
        self.segments.len() == 1 && { self.segments[0].ident.name == *symbol }
    }
}

impl<CTX: rustc_span::HashStableContext> HashStable<CTX> for Path {
    fn hash_stable(&self, hcx: &mut CTX, hasher: &mut StableHasher) {
        self.segments.len().hash_stable(hcx, hasher);
        for segment in &self.segments {
            segment.ident.hash_stable(hcx, hasher);
        }
    }
}

impl Path {
    /// Convert a span and an identifier to the corresponding
    /// one-segment path.
    pub fn from_ident(ident: Ident) -> Path {
        Path { segments: thin_vec![PathSegment::from_ident(ident)], span: ident.span, tokens: None }
    }

    pub fn is_global(&self) -> bool {
        !self.segments.is_empty() && self.segments[0].ident.name == kw::PathRoot
    }

    /// If this path is a single identifier with no arguments, does not ensure
    /// that the path resolves to a const param, the caller should check this.
    pub fn is_potential_trivial_const_arg(&self) -> bool {
        self.segments.len() == 1 && self.segments[0].args.is_none()
    }
}

/// A segment of a path: an identifier, an optional lifetime, and a set of types.
///
/// E.g., `std`, `String` or `Box<T>`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct PathSegment {
    /// The identifier portion of this path segment.
    pub ident: Ident,

    pub id: NodeId,

    /// Type/lifetime parameters attached to this path. They come in
    /// two flavors: `Path<A,B,C>` and `Path(A,B) -> C`.
    /// `None` means that no parameter list is supplied (`Path`),
    /// `Some` means that parameter list is supplied (`Path<X, Y>`)
    /// but it can be empty (`Path<>`).
    /// `P` is used as a size optimization for the common case with no parameters.
    pub args: Option<P<GenericArgs>>,
}

impl PathSegment {
    pub fn from_ident(ident: Ident) -> Self {
        PathSegment { ident, id: DUMMY_NODE_ID, args: None }
    }

    pub fn path_root(span: Span) -> Self {
        PathSegment::from_ident(Ident::new(kw::PathRoot, span))
    }

    pub fn span(&self) -> Span {
        match &self.args {
            Some(args) => self.ident.span.to(args.span()),
            None => self.ident.span,
        }
    }
}

/// The arguments of a path segment.
///
/// E.g., `<A, B>` as in `Foo<A, B>` or `(A, B)` as in `Foo(A, B)`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum GenericArgs {
    /// The `<'a, A, B, C>` in `foo::bar::baz::<'a, A, B, C>`.
    AngleBracketed(AngleBracketedArgs),
    /// The `(A, B)` and `C` in `Foo(A, B) -> C`.
    Parenthesized(ParenthesizedArgs),
}

impl GenericArgs {
    pub fn is_angle_bracketed(&self) -> bool {
        matches!(self, AngleBracketed(..))
    }

    pub fn span(&self) -> Span {
        match self {
            AngleBracketed(data) => data.span,
            Parenthesized(data) => data.span,
        }
    }
}

/// Concrete argument in the sequence of generic args.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum GenericArg {
    /// `'a` in `Foo<'a>`
    Lifetime(Lifetime),
    /// `Bar` in `Foo<Bar>`
    Type(P<Ty>),
    /// `1` in `Foo<1>`
    Const(AnonConst),
}

impl GenericArg {
    pub fn span(&self) -> Span {
        match self {
            GenericArg::Lifetime(lt) => lt.ident.span,
            GenericArg::Type(ty) => ty.span,
            GenericArg::Const(ct) => ct.value.span,
        }
    }
}

/// A path like `Foo<'a, T>`.
#[derive(Clone, Encodable, Decodable, Debug, Default)]
pub struct AngleBracketedArgs {
    /// The overall span.
    pub span: Span,
    /// The comma separated parts in the `<...>`.
    pub args: ThinVec<AngleBracketedArg>,
}

/// Either an argument for a parameter e.g., `'a`, `Vec<u8>`, `0`,
/// or a constraint on an associated item, e.g., `Item = String` or `Item: Bound`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum AngleBracketedArg {
    /// Argument for a generic parameter.
    Arg(GenericArg),
    /// Constraint for an associated item.
    Constraint(AssocConstraint),
}

impl AngleBracketedArg {
    pub fn span(&self) -> Span {
        match self {
            AngleBracketedArg::Arg(arg) => arg.span(),
            AngleBracketedArg::Constraint(constraint) => constraint.span,
        }
    }
}

impl Into<P<GenericArgs>> for AngleBracketedArgs {
    fn into(self) -> P<GenericArgs> {
        P(GenericArgs::AngleBracketed(self))
    }
}

impl Into<P<GenericArgs>> for ParenthesizedArgs {
    fn into(self) -> P<GenericArgs> {
        P(GenericArgs::Parenthesized(self))
    }
}

/// A path like `Foo(A, B) -> C`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct ParenthesizedArgs {
    /// ```text
    /// Foo(A, B) -> C
    /// ^^^^^^^^^^^^^^
    /// ```
    pub span: Span,

    /// `(A, B)`
    pub inputs: ThinVec<P<Ty>>,

    /// ```text
    /// Foo(A, B) -> C
    ///    ^^^^^^
    /// ```
    pub inputs_span: Span,

    /// `C`
    pub output: FnRetTy,
}

impl ParenthesizedArgs {
    pub fn as_angle_bracketed_args(&self) -> AngleBracketedArgs {
        let args = self
            .inputs
            .iter()
            .cloned()
            .map(|input| AngleBracketedArg::Arg(GenericArg::Type(input)))
            .collect();
        AngleBracketedArgs { span: self.inputs_span, args }
    }
}

pub use crate::node_id::{NodeId, CRATE_NODE_ID, DUMMY_NODE_ID};

/// Modifiers on a trait bound like `~const`, `?` and `!`.
#[derive(Copy, Clone, PartialEq, Eq, Encodable, Decodable, Debug)]
pub struct TraitBoundModifiers {
    pub constness: BoundConstness,
    pub asyncness: BoundAsyncness,
    pub polarity: BoundPolarity,
}

impl TraitBoundModifiers {
    pub const NONE: Self = Self {
        constness: BoundConstness::Never,
        asyncness: BoundAsyncness::Normal,
        polarity: BoundPolarity::Positive,
    };
}

/// The AST represents all type param bounds as types.
/// `typeck::collect::compute_bounds` matches these against
/// the "special" built-in traits (see `middle::lang_items`) and
/// detects `Copy`, `Send` and `Sync`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum GenericBound {
    Trait(PolyTraitRef, TraitBoundModifiers),
    Outlives(Lifetime),
}

impl GenericBound {
    pub fn span(&self) -> Span {
        match self {
            GenericBound::Trait(t, ..) => t.span,
            GenericBound::Outlives(l) => l.ident.span,
        }
    }
}

pub type GenericBounds = Vec<GenericBound>;

/// Specifies the enforced ordering for generic parameters. In the future,
/// if we wanted to relax this order, we could override `PartialEq` and
/// `PartialOrd`, to allow the kinds to be unordered.
#[derive(Hash, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum ParamKindOrd {
    Lifetime,
    TypeOrConst,
}

impl fmt::Display for ParamKindOrd {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParamKindOrd::Lifetime => "lifetime".fmt(f),
            ParamKindOrd::TypeOrConst => "type and const".fmt(f),
        }
    }
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub enum GenericParamKind {
    /// A lifetime definition (e.g., `'a: 'b + 'c + 'd`).
    Lifetime,
    Type {
        default: Option<P<Ty>>,
    },
    Const {
        ty: P<Ty>,
        /// Span of the `const` keyword.
        kw_span: Span,
        /// Optional default value for the const generic param
        default: Option<AnonConst>,
    },
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct GenericParam {
    pub id: NodeId,
    pub ident: Ident,
    pub attrs: AttrVec,
    pub bounds: GenericBounds,
    pub is_placeholder: bool,
    pub kind: GenericParamKind,
    pub colon_span: Option<Span>,
}

impl GenericParam {
    pub fn span(&self) -> Span {
        match &self.kind {
            GenericParamKind::Lifetime | GenericParamKind::Type { default: None } => {
                self.ident.span
            }
            GenericParamKind::Type { default: Some(ty) } => self.ident.span.to(ty.span),
            GenericParamKind::Const { kw_span, default: Some(default), .. } => {
                kw_span.to(default.value.span)
            }
            GenericParamKind::Const { kw_span, default: None, ty } => kw_span.to(ty.span),
        }
    }
}

/// Represents lifetime, type and const parameters attached to a declaration of
/// a function, enum, trait, etc.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Generics {
    pub params: ThinVec<GenericParam>,
    pub where_clause: WhereClause,
    pub span: Span,
}

impl Default for Generics {
    /// Creates an instance of `Generics`.
    fn default() -> Generics {
        Generics { params: ThinVec::new(), where_clause: Default::default(), span: DUMMY_SP }
    }
}

/// A where-clause in a definition.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct WhereClause {
    /// `true` if we ate a `where` token.
    ///
    /// This can happen if we parsed no predicates, e.g., `struct Foo where {}`.
    /// This allows us to pretty-print accurately and provide correct suggestion diagnostics.
    pub has_where_token: bool,
    pub predicates: ThinVec<WherePredicate>,
    pub span: Span,
}

impl Default for WhereClause {
    fn default() -> WhereClause {
        WhereClause { has_where_token: false, predicates: ThinVec::new(), span: DUMMY_SP }
    }
}

/// A single predicate in a where-clause.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum WherePredicate {
    /// A type binding (e.g., `for<'c> Foo: Send + Clone + 'c`).
    BoundPredicate(WhereBoundPredicate),
    /// A lifetime predicate (e.g., `'a: 'b + 'c`).
    RegionPredicate(WhereRegionPredicate),
    /// An equality predicate (unsupported).
    EqPredicate(WhereEqPredicate),
}

impl WherePredicate {
    pub fn span(&self) -> Span {
        match self {
            WherePredicate::BoundPredicate(p) => p.span,
            WherePredicate::RegionPredicate(p) => p.span,
            WherePredicate::EqPredicate(p) => p.span,
        }
    }
}

/// A type bound.
///
/// E.g., `for<'c> Foo: Send + Clone + 'c`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct WhereBoundPredicate {
    pub span: Span,
    /// Any generics from a `for` binding.
    pub bound_generic_params: ThinVec<GenericParam>,
    /// The type being bounded.
    pub bounded_ty: P<Ty>,
    /// Trait and lifetime bounds (`Clone + Send + 'static`).
    pub bounds: GenericBounds,
}

/// A lifetime predicate.
///
/// E.g., `'a: 'b + 'c`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct WhereRegionPredicate {
    pub span: Span,
    pub lifetime: Lifetime,
    pub bounds: GenericBounds,
}

/// An equality predicate (unsupported).
///
/// E.g., `T = int`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct WhereEqPredicate {
    pub span: Span,
    pub lhs_ty: P<Ty>,
    pub rhs_ty: P<Ty>,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Crate {
    pub attrs: AttrVec,
    pub items: ThinVec<P<Item>>,
    pub spans: ModSpans,
    /// Must be equal to `CRATE_NODE_ID` after the crate root is expanded, but may hold
    /// expansion placeholders or an unassigned value (`DUMMY_NODE_ID`) before that.
    pub id: NodeId,
    pub is_placeholder: bool,
}

/// A semantic representation of a meta item. A meta item is a slightly
/// restricted form of an attribute -- it can only contain expressions in
/// certain leaf positions, rather than arbitrary token streams -- that is used
/// for most built-in attributes.
///
/// E.g., `#[test]`, `#[derive(..)]`, `#[rustfmt::skip]` or `#[feature = "foo"]`.
#[derive(Clone, Encodable, Decodable, Debug, HashStable_Generic)]
pub struct MetaItem {
    pub path: Path,
    pub kind: MetaItemKind,
    pub span: Span,
}

/// The meta item kind, containing the data after the initial path.
#[derive(Clone, Encodable, Decodable, Debug, HashStable_Generic)]
pub enum MetaItemKind {
    /// Word meta item.
    ///
    /// E.g., `#[test]`, which lacks any arguments after `test`.
    Word,

    /// List meta item.
    ///
    /// E.g., `#[derive(..)]`, where the field represents the `..`.
    List(ThinVec<NestedMetaItem>),

    /// Name value meta item.
    ///
    /// E.g., `#[feature = "foo"]`, where the field represents the `"foo"`.
    NameValue(MetaItemLit),
}

/// Values inside meta item lists.
///
/// E.g., each of `Clone`, `Copy` in `#[derive(Clone, Copy)]`.
#[derive(Clone, Encodable, Decodable, Debug, HashStable_Generic)]
pub enum NestedMetaItem {
    /// A full MetaItem, for recursive meta items.
    MetaItem(MetaItem),

    /// A literal.
    ///
    /// E.g., `"foo"`, `64`, `true`.
    Lit(MetaItemLit),
}

/// A block (`{ .. }`).
///
/// E.g., `{ .. }` as in `fn foo() { .. }`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Block {
    /// The statements in the block.
    pub stmts: ThinVec<Stmt>,
    pub id: NodeId,
    /// Distinguishes between `unsafe { ... }` and `{ ... }`.
    pub rules: BlockCheckMode,
    pub span: Span,
    pub tokens: Option<LazyAttrTokenStream>,
    /// The following *isn't* a parse error, but will cause multiple errors in following stages.
    /// ```compile_fail
    /// let x = {
    ///     foo: var
    /// };
    /// ```
    /// #34255
    pub could_be_bare_literal: bool,
}

/// A match pattern.
///
/// Patterns appear in match statements and some other contexts, such as `let` and `if let`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Pat {
    pub id: NodeId,
    pub kind: PatKind,
    pub span: Span,
    pub tokens: Option<LazyAttrTokenStream>,
}

impl Pat {
    /// Attempt reparsing the pattern as a type.
    /// This is intended for use by diagnostics.
    pub fn to_ty(&self) -> Option<P<Ty>> {
        let kind = match &self.kind {
            // In a type expression `_` is an inference variable.
            PatKind::Wild => TyKind::Infer,
            // An IDENT pattern with no binding mode would be valid as path to a type. E.g. `u32`.
            PatKind::Ident(BindingAnnotation::NONE, ident, None) => {
                TyKind::Path(None, Path::from_ident(*ident))
            }
            PatKind::Path(qself, path) => TyKind::Path(qself.clone(), path.clone()),
            PatKind::MacCall(mac) => TyKind::MacCall(mac.clone()),
            // `&mut? P` can be reinterpreted as `&mut? T` where `T` is `P` reparsed as a type.
            PatKind::Ref(pat, mutbl) => {
                pat.to_ty().map(|ty| TyKind::Ref(None, MutTy { ty, mutbl: *mutbl }))?
            }
            // A slice/array pattern `[P]` can be reparsed as `[T]`, an unsized array,
            // when `P` can be reparsed as a type `T`.
            PatKind::Slice(pats) if pats.len() == 1 => pats[0].to_ty().map(TyKind::Slice)?,
            // A tuple pattern `(P0, .., Pn)` can be reparsed as `(T0, .., Tn)`
            // assuming `T0` to `Tn` are all syntactically valid as types.
            PatKind::Tuple(pats) => {
                let mut tys = ThinVec::with_capacity(pats.len());
                // FIXME(#48994) - could just be collected into an Option<Vec>
                for pat in pats {
                    tys.push(pat.to_ty()?);
                }
                TyKind::Tup(tys)
            }
            _ => return None,
        };

        Some(P(Ty { kind, id: self.id, span: self.span, tokens: None }))
    }

    /// Walk top-down and call `it` in each place where a pattern occurs
    /// starting with the root pattern `walk` is called on. If `it` returns
    /// false then we will descend no further but siblings will be processed.
    pub fn walk(&self, it: &mut impl FnMut(&Pat) -> bool) {
        if !it(self) {
            return;
        }

        match &self.kind {
            // Walk into the pattern associated with `Ident` (if any).
            PatKind::Ident(_, _, Some(p)) => p.walk(it),

            // Walk into each field of struct.
            PatKind::Struct(_, _, fields, _) => fields.iter().for_each(|field| field.pat.walk(it)),

            // Sequence of patterns.
            PatKind::TupleStruct(_, _, s)
            | PatKind::Tuple(s)
            | PatKind::Slice(s)
            | PatKind::Or(s) => s.iter().for_each(|p| p.walk(it)),

            // Trivial wrappers over inner patterns.
            PatKind::Box(s) | PatKind::Ref(s, _) | PatKind::Paren(s) => s.walk(it),

            // These patterns do not contain subpatterns, skip.
            PatKind::Wild
            | PatKind::Rest
            | PatKind::Never
            | PatKind::Lit(_)
            | PatKind::Range(..)
            | PatKind::Ident(..)
            | PatKind::Path(..)
            | PatKind::MacCall(_)
            | PatKind::Err(_) => {}
        }
    }

    /// Is this a `..` pattern?
    pub fn is_rest(&self) -> bool {
        matches!(self.kind, PatKind::Rest)
    }

    /// Whether this could be a never pattern, taking into account that a macro invocation can
    /// return a never pattern. Used to inform errors during parsing.
    pub fn could_be_never_pattern(&self) -> bool {
        let mut could_be_never_pattern = false;
        self.walk(&mut |pat| match &pat.kind {
            PatKind::Never | PatKind::MacCall(_) => {
                could_be_never_pattern = true;
                false
            }
            PatKind::Or(s) => {
                could_be_never_pattern = s.iter().all(|p| p.could_be_never_pattern());
                false
            }
            _ => true,
        });
        could_be_never_pattern
    }

    /// Whether this contains a `!` pattern. This in particular means that a feature gate error will
    /// be raised if the feature is off. Used to avoid gating the feature twice.
    pub fn contains_never_pattern(&self) -> bool {
        let mut contains_never_pattern = false;
        self.walk(&mut |pat| {
            if matches!(pat.kind, PatKind::Never) {
                contains_never_pattern = true;
            }
            true
        });
        contains_never_pattern
    }

    /// Return a name suitable for diagnostics.
    pub fn descr(&self) -> Option<String> {
        match &self.kind {
            PatKind::Wild => Some("_".to_string()),
            PatKind::Ident(BindingAnnotation::NONE, ident, None) => Some(format!("{ident}")),
            PatKind::Ref(pat, mutbl) => pat.descr().map(|d| format!("&{}{d}", mutbl.prefix_str())),
            _ => None,
        }
    }
}

/// A single field in a struct pattern.
///
/// Patterns like the fields of `Foo { x, ref y, ref mut z }`
/// are treated the same as `x: x, y: ref y, z: ref mut z`,
/// except when `is_shorthand` is true.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct PatField {
    /// The identifier for the field.
    pub ident: Ident,
    /// The pattern the field is destructured to.
    pub pat: P<Pat>,
    pub is_shorthand: bool,
    pub attrs: AttrVec,
    pub id: NodeId,
    pub span: Span,
    pub is_placeholder: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[derive(Encodable, Decodable, HashStable_Generic)]
pub enum ByRef {
    Yes,
    No,
}

impl From<bool> for ByRef {
    fn from(b: bool) -> ByRef {
        match b {
            false => ByRef::No,
            true => ByRef::Yes,
        }
    }
}

/// Explicit binding annotations given in the HIR for a binding. Note
/// that this is not the final binding *mode* that we infer after type
/// inference.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[derive(Encodable, Decodable, HashStable_Generic)]
pub struct BindingAnnotation(pub ByRef, pub Mutability);

impl BindingAnnotation {
    pub const NONE: Self = Self(ByRef::No, Mutability::Not);
    pub const REF: Self = Self(ByRef::Yes, Mutability::Not);
    pub const MUT: Self = Self(ByRef::No, Mutability::Mut);
    pub const REF_MUT: Self = Self(ByRef::Yes, Mutability::Mut);

    pub fn prefix_str(self) -> &'static str {
        match self {
            Self::NONE => "",
            Self::REF => "ref ",
            Self::MUT => "mut ",
            Self::REF_MUT => "ref mut ",
        }
    }
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub enum RangeEnd {
    /// `..=` or `...`
    Included(RangeSyntax),
    /// `..`
    Excluded,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub enum RangeSyntax {
    /// `...`
    DotDotDot,
    /// `..=`
    DotDotEq,
}

/// All the different flavors of pattern that Rust recognizes.
//
// Adding a new variant? Please update `test_pat` in `tests/ui/macros/stringify.rs`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum PatKind {
    /// Represents a wildcard pattern (`_`).
    Wild,

    /// A `PatKind::Ident` may either be a new bound variable (`ref mut binding @ OPT_SUBPATTERN`),
    /// or a unit struct/variant pattern, or a const pattern (in the last two cases the third
    /// field must be `None`). Disambiguation cannot be done with parser alone, so it happens
    /// during name resolution.
    Ident(BindingAnnotation, Ident, Option<P<Pat>>),

    /// A struct or struct variant pattern (e.g., `Variant {x, y, ..}`).
    Struct(Option<P<QSelf>>, Path, ThinVec<PatField>, PatFieldsRest),

    /// A tuple struct/variant pattern (`Variant(x, y, .., z)`).
    TupleStruct(Option<P<QSelf>>, Path, ThinVec<P<Pat>>),

    /// An or-pattern `A | B | C`.
    /// Invariant: `pats.len() >= 2`.
    Or(ThinVec<P<Pat>>),

    /// A possibly qualified path pattern.
    /// Unqualified path patterns `A::B::C` can legally refer to variants, structs, constants
    /// or associated constants. Qualified path patterns `<A>::B::C`/`<A as Trait>::B::C` can
    /// only legally refer to associated constants.
    Path(Option<P<QSelf>>, Path),

    /// A tuple pattern (`(a, b)`).
    Tuple(ThinVec<P<Pat>>),

    /// A `box` pattern.
    Box(P<Pat>),

    /// A reference pattern (e.g., `&mut (a, b)`).
    Ref(P<Pat>, Mutability),

    /// A literal.
    Lit(P<Expr>),

    /// A range pattern (e.g., `1...2`, `1..2`, `1..`, `..2`, `1..=2`, `..=2`).
    Range(Option<P<Expr>>, Option<P<Expr>>, Spanned<RangeEnd>),

    /// A slice pattern `[a, b, c]`.
    Slice(ThinVec<P<Pat>>),

    /// A rest pattern `..`.
    ///
    /// Syntactically it is valid anywhere.
    ///
    /// Semantically however, it only has meaning immediately inside:
    /// - a slice pattern: `[a, .., b]`,
    /// - a binding pattern immediately inside a slice pattern: `[a, r @ ..]`,
    /// - a tuple pattern: `(a, .., b)`,
    /// - a tuple struct/variant pattern: `$path(a, .., b)`.
    ///
    /// In all of these cases, an additional restriction applies,
    /// only one rest pattern may occur in the pattern sequences.
    Rest,

    // A never pattern `!`
    Never,

    /// Parentheses in patterns used for grouping (i.e., `(PAT)`).
    Paren(P<Pat>),

    /// A macro pattern; pre-expansion.
    MacCall(P<MacCall>),

    /// Placeholder for a pattern that wasn't syntactically well formed in some way.
    Err(ErrorGuaranteed),
}

/// Whether the `..` is present in a struct fields pattern.
#[derive(Clone, Copy, Encodable, Decodable, Debug, PartialEq)]
pub enum PatFieldsRest {
    /// `module::StructName { field, ..}`
    Rest,
    /// `module::StructName { field }`
    None,
}

/// The kind of borrow in an `AddrOf` expression,
/// e.g., `&place` or `&raw const place`.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[derive(Encodable, Decodable, HashStable_Generic)]
pub enum BorrowKind {
    /// A normal borrow, `&$expr` or `&mut $expr`.
    /// The resulting type is either `&'a T` or `&'a mut T`
    /// where `T = typeof($expr)` and `'a` is some lifetime.
    Ref,
    /// A raw borrow, `&raw const $expr` or `&raw mut $expr`.
    /// The resulting type is either `*const T` or `*mut T`
    /// where `T = typeof($expr)`.
    Raw,
}

#[derive(Clone, Copy, Debug, PartialEq, Encodable, Decodable, HashStable_Generic)]
pub enum BinOpKind {
    /// The `+` operator (addition)
    Add,
    /// The `-` operator (subtraction)
    Sub,
    /// The `*` operator (multiplication)
    Mul,
    /// The `/` operator (division)
    Div,
    /// The `%` operator (modulus)
    Rem,
    /// The `&&` operator (logical and)
    And,
    /// The `||` operator (logical or)
    Or,
    /// The `^` operator (bitwise xor)
    BitXor,
    /// The `&` operator (bitwise and)
    BitAnd,
    /// The `|` operator (bitwise or)
    BitOr,
    /// The `<<` operator (shift left)
    Shl,
    /// The `>>` operator (shift right)
    Shr,
    /// The `==` operator (equality)
    Eq,
    /// The `<` operator (less than)
    Lt,
    /// The `<=` operator (less than or equal to)
    Le,
    /// The `!=` operator (not equal to)
    Ne,
    /// The `>=` operator (greater than or equal to)
    Ge,
    /// The `>` operator (greater than)
    Gt,
}

impl BinOpKind {
    pub fn as_str(&self) -> &'static str {
        use BinOpKind::*;
        match self {
            Add => "+",
            Sub => "-",
            Mul => "*",
            Div => "/",
            Rem => "%",
            And => "&&",
            Or => "||",
            BitXor => "^",
            BitAnd => "&",
            BitOr => "|",
            Shl => "<<",
            Shr => ">>",
            Eq => "==",
            Lt => "<",
            Le => "<=",
            Ne => "!=",
            Ge => ">=",
            Gt => ">",
        }
    }

    pub fn is_lazy(&self) -> bool {
        matches!(self, BinOpKind::And | BinOpKind::Or)
    }

    pub fn is_comparison(&self) -> bool {
        use BinOpKind::*;
        // Note for developers: please keep this match exhaustive;
        // we want compilation to fail if another variant is added.
        match *self {
            Eq | Lt | Le | Ne | Gt | Ge => true,
            And | Or | Add | Sub | Mul | Div | Rem | BitXor | BitAnd | BitOr | Shl | Shr => false,
        }
    }

    /// Returns `true` if the binary operator takes its arguments by value.
    pub fn is_by_value(self) -> bool {
        !self.is_comparison()
    }
}

pub type BinOp = Spanned<BinOpKind>;

/// Unary operator.
///
/// Note that `&data` is not an operator, it's an `AddrOf` expression.
#[derive(Clone, Copy, Debug, PartialEq, Encodable, Decodable, HashStable_Generic)]
pub enum UnOp {
    /// The `*` operator for dereferencing
    Deref,
    /// The `!` operator for logical inversion
    Not,
    /// The `-` operator for negation
    Neg,
}

impl UnOp {
    pub fn as_str(&self) -> &'static str {
        match self {
            UnOp::Deref => "*",
            UnOp::Not => "!",
            UnOp::Neg => "-",
        }
    }

    /// Returns `true` if the unary operator takes its argument by value.
    pub fn is_by_value(self) -> bool {
        matches!(self, Self::Neg | Self::Not)
    }
}

/// A statement
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Stmt {
    pub id: NodeId,
    pub kind: StmtKind,
    pub span: Span,
}

impl Stmt {
    pub fn has_trailing_semicolon(&self) -> bool {
        match &self.kind {
            StmtKind::Semi(_) => true,
            StmtKind::MacCall(mac) => matches!(mac.style, MacStmtStyle::Semicolon),
            _ => false,
        }
    }

    /// Converts a parsed `Stmt` to a `Stmt` with
    /// a trailing semicolon.
    ///
    /// This only modifies the parsed AST struct, not the attached
    /// `LazyAttrTokenStream`. The parser is responsible for calling
    /// `ToAttrTokenStream::add_trailing_semi` when there is actually
    /// a semicolon in the tokenstream.
    pub fn add_trailing_semicolon(mut self) -> Self {
        self.kind = match self.kind {
            StmtKind::Expr(expr) => StmtKind::Semi(expr),
            StmtKind::MacCall(mac) => {
                StmtKind::MacCall(mac.map(|MacCallStmt { mac, style: _, attrs, tokens }| {
                    MacCallStmt { mac, style: MacStmtStyle::Semicolon, attrs, tokens }
                }))
            }
            kind => kind,
        };

        self
    }

    pub fn is_item(&self) -> bool {
        matches!(self.kind, StmtKind::Item(_))
    }

    pub fn is_expr(&self) -> bool {
        matches!(self.kind, StmtKind::Expr(_))
    }
}

// Adding a new variant? Please update `test_stmt` in `tests/ui/macros/stringify.rs`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum StmtKind {
    /// A local (let) binding.
    Let(P<Local>),
    /// An item definition.
    Item(P<Item>),
    /// Expr without trailing semi-colon.
    Expr(P<Expr>),
    /// Expr with a trailing semi-colon.
    Semi(P<Expr>),
    /// Just a trailing semi-colon.
    Empty,
    /// Macro.
    MacCall(P<MacCallStmt>),
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct MacCallStmt {
    pub mac: P<MacCall>,
    pub style: MacStmtStyle,
    pub attrs: AttrVec,
    pub tokens: Option<LazyAttrTokenStream>,
}

#[derive(Clone, Copy, PartialEq, Encodable, Decodable, Debug)]
pub enum MacStmtStyle {
    /// The macro statement had a trailing semicolon (e.g., `foo! { ... };`
    /// `foo!(...);`, `foo![...];`).
    Semicolon,
    /// The macro statement had braces (e.g., `foo! { ... }`).
    Braces,
    /// The macro statement had parentheses or brackets and no semicolon (e.g.,
    /// `foo!(...)`). All of these will end up being converted into macro
    /// expressions.
    NoBraces,
}

/// Local represents a `let` statement, e.g., `let <pat>:<ty> = <expr>;`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Local {
    pub id: NodeId,
    pub pat: P<Pat>,
    pub ty: Option<P<Ty>>,
    pub kind: LocalKind,
    pub span: Span,
    pub colon_sp: Option<Span>,
    pub attrs: AttrVec,
    pub tokens: Option<LazyAttrTokenStream>,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub enum LocalKind {
    /// Local declaration.
    /// Example: `let x;`
    Decl,
    /// Local declaration with an initializer.
    /// Example: `let x = y;`
    Init(P<Expr>),
    /// Local declaration with an initializer and an `else` clause.
    /// Example: `let Some(x) = y else { return };`
    InitElse(P<Expr>, P<Block>),
}

impl LocalKind {
    pub fn init(&self) -> Option<&Expr> {
        match self {
            Self::Decl => None,
            Self::Init(i) | Self::InitElse(i, _) => Some(i),
        }
    }

    pub fn init_else_opt(&self) -> Option<(&Expr, Option<&Block>)> {
        match self {
            Self::Decl => None,
            Self::Init(init) => Some((init, None)),
            Self::InitElse(init, els) => Some((init, Some(els))),
        }
    }
}

/// An arm of a 'match'.
///
/// E.g., `0..=10 => { println!("match!") }` as in
///
/// ```
/// match 123 {
///     0..=10 => { println!("match!") },
///     _ => { println!("no match!") },
/// }
/// ```
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Arm {
    pub attrs: AttrVec,
    /// Match arm pattern, e.g. `10` in `match foo { 10 => {}, _ => {} }`
    pub pat: P<Pat>,
    /// Match arm guard, e.g. `n > 10` in `match foo { n if n > 10 => {}, _ => {} }`
    pub guard: Option<P<Expr>>,
    /// Match arm body. Omitted if the pattern is a never pattern.
    pub body: Option<P<Expr>>,
    pub span: Span,
    pub id: NodeId,
    pub is_placeholder: bool,
}

/// A single field in a struct expression, e.g. `x: value` and `y` in `Foo { x: value, y }`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct ExprField {
    pub attrs: AttrVec,
    pub id: NodeId,
    pub span: Span,
    pub ident: Ident,
    pub expr: P<Expr>,
    pub is_shorthand: bool,
    pub is_placeholder: bool,
}

#[derive(Clone, PartialEq, Encodable, Decodable, Debug, Copy)]
pub enum BlockCheckMode {
    Default,
    Unsafe(UnsafeSource),
}

#[derive(Clone, PartialEq, Encodable, Decodable, Debug, Copy)]
pub enum UnsafeSource {
    CompilerGenerated,
    UserProvided,
}

/// A constant (expression) that's not an item or associated item,
/// but needs its own `DefId` for type-checking, const-eval, etc.
/// These are usually found nested inside types (e.g., array lengths)
/// or expressions (e.g., repeat counts), and also used to define
/// explicit discriminant values for enum variants.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct AnonConst {
    pub id: NodeId,
    pub value: P<Expr>,
}

/// An expression.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Expr {
    pub id: NodeId,
    pub kind: ExprKind,
    pub span: Span,
    pub attrs: AttrVec,
    pub tokens: Option<LazyAttrTokenStream>,
}

impl Expr {
    /// Is this expr either `N`, or `{ N }`.
    ///
    /// If this is not the case, name resolution does not resolve `N` when using
    /// `min_const_generics` as more complex expressions are not supported.
    ///
    /// Does not ensure that the path resolves to a const param, the caller should check this.
    pub fn is_potential_trivial_const_arg(&self) -> bool {
        let this = if let ExprKind::Block(block, None) = &self.kind
            && block.stmts.len() == 1
            && let StmtKind::Expr(expr) = &block.stmts[0].kind
        {
            expr
        } else {
            self
        };

        if let ExprKind::Path(None, path) = &this.kind
            && path.is_potential_trivial_const_arg()
        {
            true
        } else {
            false
        }
    }

    pub fn to_bound(&self) -> Option<GenericBound> {
        match &self.kind {
            ExprKind::Path(None, path) => Some(GenericBound::Trait(
                PolyTraitRef::new(ThinVec::new(), path.clone(), self.span),
                TraitBoundModifiers::NONE,
            )),
            _ => None,
        }
    }

    pub fn peel_parens(&self) -> &Expr {
        let mut expr = self;
        while let ExprKind::Paren(inner) = &expr.kind {
            expr = inner;
        }
        expr
    }

    pub fn peel_parens_and_refs(&self) -> &Expr {
        let mut expr = self;
        while let ExprKind::Paren(inner) | ExprKind::AddrOf(BorrowKind::Ref, _, inner) = &expr.kind
        {
            expr = inner;
        }
        expr
    }

    /// Attempts to reparse as `Ty` (for diagnostic purposes).
    pub fn to_ty(&self) -> Option<P<Ty>> {
        let kind = match &self.kind {
            // Trivial conversions.
            ExprKind::Path(qself, path) => TyKind::Path(qself.clone(), path.clone()),
            ExprKind::MacCall(mac) => TyKind::MacCall(mac.clone()),

            ExprKind::Paren(expr) => expr.to_ty().map(TyKind::Paren)?,

            ExprKind::AddrOf(BorrowKind::Ref, mutbl, expr) => {
                expr.to_ty().map(|ty| TyKind::Ref(None, MutTy { ty, mutbl: *mutbl }))?
            }

            ExprKind::Repeat(expr, expr_len) => {
                expr.to_ty().map(|ty| TyKind::Array(ty, expr_len.clone()))?
            }

            ExprKind::Array(exprs) if exprs.len() == 1 => exprs[0].to_ty().map(TyKind::Slice)?,

            ExprKind::Tup(exprs) => {
                let tys = exprs.iter().map(|expr| expr.to_ty()).collect::<Option<ThinVec<_>>>()?;
                TyKind::Tup(tys)
            }

            // If binary operator is `Add` and both `lhs` and `rhs` are trait bounds,
            // then type of result is trait object.
            // Otherwise we don't assume the result type.
            ExprKind::Binary(binop, lhs, rhs) if binop.node == BinOpKind::Add => {
                if let (Some(lhs), Some(rhs)) = (lhs.to_bound(), rhs.to_bound()) {
                    TyKind::TraitObject(vec![lhs, rhs], TraitObjectSyntax::None)
                } else {
                    return None;
                }
            }

            ExprKind::Underscore => TyKind::Infer,

            // This expression doesn't look like a type syntactically.
            _ => return None,
        };

        Some(P(Ty { kind, id: self.id, span: self.span, tokens: None }))
    }

    pub fn precedence(&self) -> ExprPrecedence {
        match self.kind {
            ExprKind::Array(_) => ExprPrecedence::Array,
            ExprKind::ConstBlock(_) => ExprPrecedence::ConstBlock,
            ExprKind::Call(..) => ExprPrecedence::Call,
            ExprKind::MethodCall(..) => ExprPrecedence::MethodCall,
            ExprKind::Tup(_) => ExprPrecedence::Tup,
            ExprKind::Binary(op, ..) => ExprPrecedence::Binary(op.node),
            ExprKind::Unary(..) => ExprPrecedence::Unary,
            ExprKind::Lit(_) | ExprKind::IncludedBytes(..) => ExprPrecedence::Lit,
            ExprKind::Type(..) | ExprKind::Cast(..) => ExprPrecedence::Cast,
            ExprKind::Let(..) => ExprPrecedence::Let,
            ExprKind::If(..) => ExprPrecedence::If,
            ExprKind::While(..) => ExprPrecedence::While,
            ExprKind::ForLoop { .. } => ExprPrecedence::ForLoop,
            ExprKind::Loop(..) => ExprPrecedence::Loop,
            ExprKind::Match(..) => ExprPrecedence::Match,
            ExprKind::Closure(..) => ExprPrecedence::Closure,
            ExprKind::Block(..) => ExprPrecedence::Block,
            ExprKind::TryBlock(..) => ExprPrecedence::TryBlock,
            ExprKind::Gen(..) => ExprPrecedence::Gen,
            ExprKind::Await(..) => ExprPrecedence::Await,
            ExprKind::Assign(..) => ExprPrecedence::Assign,
            ExprKind::AssignOp(..) => ExprPrecedence::AssignOp,
            ExprKind::Field(..) => ExprPrecedence::Field,
            ExprKind::Index(..) => ExprPrecedence::Index,
            ExprKind::Range(..) => ExprPrecedence::Range,
            ExprKind::Underscore => ExprPrecedence::Path,
            ExprKind::Path(..) => ExprPrecedence::Path,
            ExprKind::AddrOf(..) => ExprPrecedence::AddrOf,
            ExprKind::Break(..) => ExprPrecedence::Break,
            ExprKind::Continue(..) => ExprPrecedence::Continue,
            ExprKind::Ret(..) => ExprPrecedence::Ret,
            ExprKind::InlineAsm(..) => ExprPrecedence::InlineAsm,
            ExprKind::OffsetOf(..) => ExprPrecedence::OffsetOf,
            ExprKind::MacCall(..) => ExprPrecedence::Mac,
            ExprKind::Struct(..) => ExprPrecedence::Struct,
            ExprKind::Repeat(..) => ExprPrecedence::Repeat,
            ExprKind::Paren(..) => ExprPrecedence::Paren,
            ExprKind::Try(..) => ExprPrecedence::Try,
            ExprKind::Yield(..) => ExprPrecedence::Yield,
            ExprKind::Yeet(..) => ExprPrecedence::Yeet,
            ExprKind::FormatArgs(..) => ExprPrecedence::FormatArgs,
            ExprKind::Become(..) => ExprPrecedence::Become,
            ExprKind::Err(_) | ExprKind::Dummy => ExprPrecedence::Err,
        }
    }

    /// To a first-order approximation, is this a pattern?
    pub fn is_approximately_pattern(&self) -> bool {
        matches!(
            &self.peel_parens().kind,
            ExprKind::Array(_)
                | ExprKind::Call(_, _)
                | ExprKind::Tup(_)
                | ExprKind::Lit(_)
                | ExprKind::Range(_, _, _)
                | ExprKind::Underscore
                | ExprKind::Path(_, _)
                | ExprKind::Struct(_)
        )
    }
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Closure {
    pub binder: ClosureBinder,
    pub capture_clause: CaptureBy,
    pub constness: Const,
    pub coroutine_kind: Option<CoroutineKind>,
    pub movability: Movability,
    pub fn_decl: P<FnDecl>,
    pub body: P<Expr>,
    /// The span of the declaration block: 'move |...| -> ...'
    pub fn_decl_span: Span,
    /// The span of the argument block `|...|`
    pub fn_arg_span: Span,
}

/// Limit types of a range (inclusive or exclusive)
#[derive(Copy, Clone, PartialEq, Encodable, Decodable, Debug)]
pub enum RangeLimits {
    /// Inclusive at the beginning, exclusive at the end
    HalfOpen,
    /// Inclusive at the beginning and end
    Closed,
}

/// A method call (e.g. `x.foo::<Bar, Baz>(a, b, c)`).
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct MethodCall {
    /// The method name and its generic arguments, e.g. `foo::<Bar, Baz>`.
    pub seg: PathSegment,
    /// The receiver, e.g. `x`.
    pub receiver: P<Expr>,
    /// The arguments, e.g. `a, b, c`.
    pub args: ThinVec<P<Expr>>,
    /// The span of the function, without the dot and receiver e.g. `foo::<Bar,
    /// Baz>(a, b, c)`.
    pub span: Span,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub enum StructRest {
    /// `..x`.
    Base(P<Expr>),
    /// `..`.
    Rest(Span),
    /// No trailing `..` or expression.
    None,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct StructExpr {
    pub qself: Option<P<QSelf>>,
    pub path: Path,
    pub fields: ThinVec<ExprField>,
    pub rest: StructRest,
}

// Adding a new variant? Please update `test_expr` in `tests/ui/macros/stringify.rs`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum ExprKind {
    /// An array (`[a, b, c, d]`)
    Array(ThinVec<P<Expr>>),
    /// Allow anonymous constants from an inline `const` block
    ConstBlock(AnonConst),
    /// A function call
    ///
    /// The first field resolves to the function itself,
    /// and the second field is the list of arguments.
    /// This also represents calling the constructor of
    /// tuple-like ADTs such as tuple structs and enum variants.
    Call(P<Expr>, ThinVec<P<Expr>>),
    /// A method call (e.g. `x.foo::<Bar, Baz>(a, b, c)`).
    MethodCall(Box<MethodCall>),
    /// A tuple (e.g., `(a, b, c, d)`).
    Tup(ThinVec<P<Expr>>),
    /// A binary operation (e.g., `a + b`, `a * b`).
    Binary(BinOp, P<Expr>, P<Expr>),
    /// A unary operation (e.g., `!x`, `*x`).
    Unary(UnOp, P<Expr>),
    /// A literal (e.g., `1`, `"foo"`).
    Lit(token::Lit),
    /// A cast (e.g., `foo as f64`).
    Cast(P<Expr>, P<Ty>),
    /// A type ascription (e.g., `42: usize`).
    Type(P<Expr>, P<Ty>),
    /// A `let pat = expr` expression that is only semantically allowed in the condition
    /// of `if` / `while` expressions. (e.g., `if let 0 = x { .. }`).
    ///
    /// `Span` represents the whole `let pat = expr` statement.
    Let(P<Pat>, P<Expr>, Span, Option<ErrorGuaranteed>),
    /// An `if` block, with an optional `else` block.
    ///
    /// `if expr { block } else { expr }`
    If(P<Expr>, P<Block>, Option<P<Expr>>),
    /// A while loop, with an optional label.
    ///
    /// `'label: while expr { block }`
    While(P<Expr>, P<Block>, Option<Label>),
    /// A `for` loop, with an optional label.
    ///
    /// `'label: for await? pat in iter { block }`
    ///
    /// This is desugared to a combination of `loop` and `match` expressions.
    ForLoop { pat: P<Pat>, iter: P<Expr>, body: P<Block>, label: Option<Label>, kind: ForLoopKind },
    /// Conditionless loop (can be exited with `break`, `continue`, or `return`).
    ///
    /// `'label: loop { block }`
    Loop(P<Block>, Option<Label>, Span),
    /// A `match` block.
    Match(P<Expr>, ThinVec<Arm>),
    /// A closure (e.g., `move |a, b, c| a + b + c`).
    Closure(Box<Closure>),
    /// A block (`'label: { ... }`).
    Block(P<Block>, Option<Label>),
    /// An `async` block (`async move { ... }`),
    /// or a `gen` block (`gen move { ... }`)
    Gen(CaptureBy, P<Block>, GenBlockKind),
    /// An await expression (`my_future.await`). Span is of await keyword.
    Await(P<Expr>, Span),

    /// A try block (`try { ... }`).
    TryBlock(P<Block>),

    /// An assignment (`a = foo()`).
    /// The `Span` argument is the span of the `=` token.
    Assign(P<Expr>, P<Expr>, Span),
    /// An assignment with an operator.
    ///
    /// E.g., `a += 1`.
    AssignOp(BinOp, P<Expr>, P<Expr>),
    /// Access of a named (e.g., `obj.foo`) or unnamed (e.g., `obj.0`) struct field.
    Field(P<Expr>, Ident),
    /// An indexing operation (e.g., `foo[2]`).
    /// The span represents the span of the `[2]`, including brackets.
    Index(P<Expr>, P<Expr>, Span),
    /// A range (e.g., `1..2`, `1..`, `..2`, `1..=2`, `..=2`; and `..` in destructuring assignment).
    Range(Option<P<Expr>>, Option<P<Expr>>, RangeLimits),
    /// An underscore, used in destructuring assignment to ignore a value.
    Underscore,

    /// Variable reference, possibly containing `::` and/or type
    /// parameters (e.g., `foo::bar::<baz>`).
    ///
    /// Optionally "qualified" (e.g., `<Vec<T> as SomeTrait>::SomeType`).
    Path(Option<P<QSelf>>, Path),

    /// A referencing operation (`&a`, `&mut a`, `&raw const a` or `&raw mut a`).
    AddrOf(BorrowKind, Mutability, P<Expr>),
    /// A `break`, with an optional label to break, and an optional expression.
    Break(Option<Label>, Option<P<Expr>>),
    /// A `continue`, with an optional label.
    Continue(Option<Label>),
    /// A `return`, with an optional value to be returned.
    Ret(Option<P<Expr>>),

    /// Output of the `asm!()` macro.
    InlineAsm(P<InlineAsm>),

    /// Output of the `offset_of!()` macro.
    OffsetOf(P<Ty>, P<[Ident]>),

    /// A macro invocation; pre-expansion.
    MacCall(P<MacCall>),

    /// A struct literal expression.
    ///
    /// E.g., `Foo {x: 1, y: 2}`, or `Foo {x: 1, .. rest}`.
    Struct(P<StructExpr>),

    /// An array literal constructed from one repeated element.
    ///
    /// E.g., `[1; 5]`. The expression is the element to be
    /// repeated; the constant is the number of times to repeat it.
    Repeat(P<Expr>, AnonConst),

    /// No-op: used solely so we can pretty-print faithfully.
    Paren(P<Expr>),

    /// A try expression (`expr?`).
    Try(P<Expr>),

    /// A `yield`, with an optional value to be yielded.
    Yield(Option<P<Expr>>),

    /// A `do yeet` (aka `throw`/`fail`/`bail`/`raise`/whatever),
    /// with an optional value to be returned.
    Yeet(Option<P<Expr>>),

    /// A tail call return, with the value to be returned.
    ///
    /// While `.0` must be a function call, we check this later, after parsing.
    Become(P<Expr>),

    /// Bytes included via `include_bytes!`
    /// Added for optimization purposes to avoid the need to escape
    /// large binary blobs - should always behave like [`ExprKind::Lit`]
    /// with a `ByteStr` literal.
    IncludedBytes(Lrc<[u8]>),

    /// A `format_args!()` expression.
    FormatArgs(P<FormatArgs>),

    /// Placeholder for an expression that wasn't syntactically well formed in some way.
    Err(ErrorGuaranteed),

    /// Acts as a null expression. Lowering it will always emit a bug.
    Dummy,
}

/// Used to differentiate between `for` loops and `for await` loops.
#[derive(Clone, Copy, Encodable, Decodable, Debug, PartialEq, Eq)]
pub enum ForLoopKind {
    For,
    ForAwait,
}

/// Used to differentiate between `async {}` blocks and `gen {}` blocks.
#[derive(Clone, Encodable, Decodable, Debug, PartialEq, Eq)]
pub enum GenBlockKind {
    Async,
    Gen,
    AsyncGen,
}

impl fmt::Display for GenBlockKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.modifier().fmt(f)
    }
}

impl GenBlockKind {
    pub fn modifier(&self) -> &'static str {
        match self {
            GenBlockKind::Async => "async",
            GenBlockKind::Gen => "gen",
            GenBlockKind::AsyncGen => "async gen",
        }
    }
}

/// The explicit `Self` type in a "qualified path". The actual
/// path, including the trait and the associated item, is stored
/// separately. `position` represents the index of the associated
/// item qualified with this `Self` type.
///
/// ```ignore (only-for-syntax-highlight)
/// <Vec<T> as a::b::Trait>::AssociatedItem
///  ^~~~~     ~~~~~~~~~~~~~~^
///  ty        position = 3
///
/// <Vec<T>>::AssociatedItem
///  ^~~~~    ^
///  ty       position = 0
/// ```
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct QSelf {
    pub ty: P<Ty>,

    /// The span of `a::b::Trait` in a path like `<Vec<T> as
    /// a::b::Trait>::AssociatedItem`; in the case where `position ==
    /// 0`, this is an empty span.
    pub path_span: Span,
    pub position: usize,
}

/// A capture clause used in closures and `async` blocks.
#[derive(Clone, Copy, PartialEq, Encodable, Decodable, Debug, HashStable_Generic)]
pub enum CaptureBy {
    /// `move |x| y + x`.
    Value {
        /// The span of the `move` keyword.
        move_kw: Span,
    },
    /// `move` keyword was not specified.
    Ref,
}

/// Closure lifetime binder, `for<'a, 'b>` in `for<'a, 'b> |_: &'a (), _: &'b ()|`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum ClosureBinder {
    /// The binder is not present, all closure lifetimes are inferred.
    NotPresent,
    /// The binder is present.
    For {
        /// Span of the whole `for<>` clause
        ///
        /// ```text
        /// for<'a, 'b> |_: &'a (), _: &'b ()| { ... }
        /// ^^^^^^^^^^^ -- this
        /// ```
        span: Span,

        /// Lifetimes in the `for<>` closure
        ///
        /// ```text
        /// for<'a, 'b> |_: &'a (), _: &'b ()| { ... }
        ///     ^^^^^^ -- this
        /// ```
        generic_params: ThinVec<GenericParam>,
    },
}

/// Represents a macro invocation. The `path` indicates which macro
/// is being invoked, and the `args` are arguments passed to it.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct MacCall {
    pub path: Path,
    pub args: P<DelimArgs>,
}

impl MacCall {
    pub fn span(&self) -> Span {
        self.path.span.to(self.args.dspan.entire())
    }
}

/// Arguments passed to an attribute macro.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum AttrArgs {
    /// No arguments: `#[attr]`.
    Empty,
    /// Delimited arguments: `#[attr()/[]/{}]`.
    Delimited(DelimArgs),
    /// Arguments of a key-value attribute: `#[attr = "value"]`.
    Eq(
        /// Span of the `=` token.
        Span,
        /// The "value".
        AttrArgsEq,
    ),
}

// The RHS of an `AttrArgs::Eq` starts out as an expression. Once macro
// expansion is completed, all cases end up either as a meta item literal,
// which is the form used after lowering to HIR, or as an error.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum AttrArgsEq {
    Ast(P<Expr>),
    Hir(MetaItemLit),
}

impl AttrArgs {
    pub fn span(&self) -> Option<Span> {
        match self {
            AttrArgs::Empty => None,
            AttrArgs::Delimited(args) => Some(args.dspan.entire()),
            AttrArgs::Eq(eq_span, AttrArgsEq::Ast(expr)) => Some(eq_span.to(expr.span)),
            AttrArgs::Eq(_, AttrArgsEq::Hir(lit)) => {
                unreachable!("in literal form when getting span: {:?}", lit);
            }
        }
    }

    /// Tokens inside the delimiters or after `=`.
    /// Proc macros see these tokens, for example.
    pub fn inner_tokens(&self) -> TokenStream {
        match self {
            AttrArgs::Empty => TokenStream::default(),
            AttrArgs::Delimited(args) => args.tokens.clone(),
            AttrArgs::Eq(_, AttrArgsEq::Ast(expr)) => TokenStream::from_ast(expr),
            AttrArgs::Eq(_, AttrArgsEq::Hir(lit)) => {
                unreachable!("in literal form when getting inner tokens: {:?}", lit)
            }
        }
    }
}

impl<CTX> HashStable<CTX> for AttrArgs
where
    CTX: crate::HashStableContext,
{
    fn hash_stable(&self, ctx: &mut CTX, hasher: &mut StableHasher) {
        mem::discriminant(self).hash_stable(ctx, hasher);
        match self {
            AttrArgs::Empty => {}
            AttrArgs::Delimited(args) => args.hash_stable(ctx, hasher),
            AttrArgs::Eq(_eq_span, AttrArgsEq::Ast(expr)) => {
                unreachable!("hash_stable {:?}", expr);
            }
            AttrArgs::Eq(eq_span, AttrArgsEq::Hir(lit)) => {
                eq_span.hash_stable(ctx, hasher);
                lit.hash_stable(ctx, hasher);
            }
        }
    }
}

/// Delimited arguments, as used in `#[attr()/[]/{}]` or `mac!()/[]/{}`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct DelimArgs {
    pub dspan: DelimSpan,
    pub delim: Delimiter, // Note: `Delimiter::Invisible` never occurs
    pub tokens: TokenStream,
}

impl DelimArgs {
    /// Whether a macro with these arguments needs a semicolon
    /// when used as a standalone item or statement.
    pub fn need_semicolon(&self) -> bool {
        !matches!(self, DelimArgs { delim: Delimiter::Brace, .. })
    }
}

impl<CTX> HashStable<CTX> for DelimArgs
where
    CTX: crate::HashStableContext,
{
    fn hash_stable(&self, ctx: &mut CTX, hasher: &mut StableHasher) {
        let DelimArgs { dspan, delim, tokens } = self;
        dspan.hash_stable(ctx, hasher);
        delim.hash_stable(ctx, hasher);
        tokens.hash_stable(ctx, hasher);
    }
}

/// Represents a macro definition.
#[derive(Clone, Encodable, Decodable, Debug, HashStable_Generic)]
pub struct MacroDef {
    pub body: P<DelimArgs>,
    /// `true` if macro was defined with `macro_rules`.
    pub macro_rules: bool,
}

#[derive(Clone, Encodable, Decodable, Debug, Copy, Hash, Eq, PartialEq)]
#[derive(HashStable_Generic)]
pub enum StrStyle {
    /// A regular string, like `"foo"`.
    Cooked,
    /// A raw string, like `r##"foo"##`.
    ///
    /// The value is the number of `#` symbols used.
    Raw(u8),
}

/// A literal in a meta item.
#[derive(Clone, Encodable, Decodable, Debug, HashStable_Generic)]
pub struct MetaItemLit {
    /// The original literal as written in the source code.
    pub symbol: Symbol,
    /// The original suffix as written in the source code.
    pub suffix: Option<Symbol>,
    /// The "semantic" representation of the literal lowered from the original tokens.
    /// Strings are unescaped, hexadecimal forms are eliminated, etc.
    pub kind: LitKind,
    pub span: Span,
}

/// Similar to `MetaItemLit`, but restricted to string literals.
#[derive(Clone, Copy, Encodable, Decodable, Debug)]
pub struct StrLit {
    /// The original literal as written in source code.
    pub symbol: Symbol,
    /// The original suffix as written in source code.
    pub suffix: Option<Symbol>,
    /// The semantic (unescaped) representation of the literal.
    pub symbol_unescaped: Symbol,
    pub style: StrStyle,
    pub span: Span,
}

impl StrLit {
    pub fn as_token_lit(&self) -> token::Lit {
        let token_kind = match self.style {
            StrStyle::Cooked => token::Str,
            StrStyle::Raw(n) => token::StrRaw(n),
        };
        token::Lit::new(token_kind, self.symbol, self.suffix)
    }
}

/// Type of the integer literal based on provided suffix.
#[derive(Clone, Copy, Encodable, Decodable, Debug, Hash, Eq, PartialEq)]
#[derive(HashStable_Generic)]
pub enum LitIntType {
    /// e.g. `42_i32`.
    Signed(IntTy),
    /// e.g. `42_u32`.
    Unsigned(UintTy),
    /// e.g. `42`.
    Unsuffixed,
}

/// Type of the float literal based on provided suffix.
#[derive(Clone, Copy, Encodable, Decodable, Debug, Hash, Eq, PartialEq)]
#[derive(HashStable_Generic)]
pub enum LitFloatType {
    /// A float literal with a suffix (`1f32` or `1E10f32`).
    Suffixed(FloatTy),
    /// A float literal without a suffix (`1.0 or 1.0E10`).
    Unsuffixed,
}

/// This type is used within both `ast::MetaItemLit` and `hir::Lit`.
///
/// Note that the entire literal (including the suffix) is considered when
/// deciding the `LitKind`. This means that float literals like `1f32` are
/// classified by this type as `Float`. This is different to `token::LitKind`
/// which does *not* consider the suffix.
#[derive(Clone, Encodable, Decodable, Debug, Hash, Eq, PartialEq, HashStable_Generic)]
pub enum LitKind {
    /// A string literal (`"foo"`). The symbol is unescaped, and so may differ
    /// from the original token's symbol.
    Str(Symbol, StrStyle),
    /// A byte string (`b"foo"`). Not stored as a symbol because it might be
    /// non-utf8, and symbols only allow utf8 strings.
    ByteStr(Lrc<[u8]>, StrStyle),
    /// A C String (`c"foo"`). Guaranteed to only have `\0` at the end.
    CStr(Lrc<[u8]>, StrStyle),
    /// A byte char (`b'f'`).
    Byte(u8),
    /// A character literal (`'a'`).
    Char(char),
    /// An integer literal (`1`).
    Int(Pu128, LitIntType),
    /// A float literal (`1.0`, `1f64` or `1E10f64`). The pre-suffix part is
    /// stored as a symbol rather than `f64` so that `LitKind` can impl `Eq`
    /// and `Hash`.
    Float(Symbol, LitFloatType),
    /// A boolean literal (`true`, `false`).
    Bool(bool),
    /// Placeholder for a literal that wasn't well-formed in some way.
    Err(ErrorGuaranteed),
}

impl LitKind {
    pub fn str(&self) -> Option<Symbol> {
        match *self {
            LitKind::Str(s, _) => Some(s),
            _ => None,
        }
    }

    /// Returns `true` if this literal is a string.
    pub fn is_str(&self) -> bool {
        matches!(self, LitKind::Str(..))
    }

    /// Returns `true` if this literal is byte literal string.
    pub fn is_bytestr(&self) -> bool {
        matches!(self, LitKind::ByteStr(..))
    }

    /// Returns `true` if this is a numeric literal.
    pub fn is_numeric(&self) -> bool {
        matches!(self, LitKind::Int(..) | LitKind::Float(..))
    }

    /// Returns `true` if this literal has no suffix.
    /// Note: this will return true for literals with prefixes such as raw strings and byte strings.
    pub fn is_unsuffixed(&self) -> bool {
        !self.is_suffixed()
    }

    /// Returns `true` if this literal has a suffix.
    pub fn is_suffixed(&self) -> bool {
        match *self {
            // suffixed variants
            LitKind::Int(_, LitIntType::Signed(..) | LitIntType::Unsigned(..))
            | LitKind::Float(_, LitFloatType::Suffixed(..)) => true,
            // unsuffixed variants
            LitKind::Str(..)
            | LitKind::ByteStr(..)
            | LitKind::CStr(..)
            | LitKind::Byte(..)
            | LitKind::Char(..)
            | LitKind::Int(_, LitIntType::Unsuffixed)
            | LitKind::Float(_, LitFloatType::Unsuffixed)
            | LitKind::Bool(..)
            | LitKind::Err(_) => false,
        }
    }
}

// N.B., If you change this, you'll probably want to change the corresponding
// type structure in `middle/ty.rs` as well.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct MutTy {
    pub ty: P<Ty>,
    pub mutbl: Mutability,
}

/// Represents a function's signature in a trait declaration,
/// trait implementation, or free function.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct FnSig {
    pub header: FnHeader,
    pub decl: P<FnDecl>,
    pub span: Span,
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Debug)]
#[derive(Encodable, Decodable, HashStable_Generic)]
pub enum FloatTy {
    F16,
    F32,
    F64,
    F128,
}

impl FloatTy {
    pub fn name_str(self) -> &'static str {
        match self {
            FloatTy::F16 => "f16",
            FloatTy::F32 => "f32",
            FloatTy::F64 => "f64",
            FloatTy::F128 => "f128",
        }
    }

    pub fn name(self) -> Symbol {
        match self {
            FloatTy::F16 => sym::f16,
            FloatTy::F32 => sym::f32,
            FloatTy::F64 => sym::f64,
            FloatTy::F128 => sym::f128,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Debug)]
#[derive(Encodable, Decodable, HashStable_Generic)]
pub enum IntTy {
    Isize,
    I8,
    I16,
    I32,
    I64,
    I128,
}

impl IntTy {
    pub fn name_str(&self) -> &'static str {
        match *self {
            IntTy::Isize => "isize",
            IntTy::I8 => "i8",
            IntTy::I16 => "i16",
            IntTy::I32 => "i32",
            IntTy::I64 => "i64",
            IntTy::I128 => "i128",
        }
    }

    pub fn name(&self) -> Symbol {
        match *self {
            IntTy::Isize => sym::isize,
            IntTy::I8 => sym::i8,
            IntTy::I16 => sym::i16,
            IntTy::I32 => sym::i32,
            IntTy::I64 => sym::i64,
            IntTy::I128 => sym::i128,
        }
    }
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Copy, Debug)]
#[derive(Encodable, Decodable, HashStable_Generic)]
pub enum UintTy {
    Usize,
    U8,
    U16,
    U32,
    U64,
    U128,
}

impl UintTy {
    pub fn name_str(&self) -> &'static str {
        match *self {
            UintTy::Usize => "usize",
            UintTy::U8 => "u8",
            UintTy::U16 => "u16",
            UintTy::U32 => "u32",
            UintTy::U64 => "u64",
            UintTy::U128 => "u128",
        }
    }

    pub fn name(&self) -> Symbol {
        match *self {
            UintTy::Usize => sym::usize,
            UintTy::U8 => sym::u8,
            UintTy::U16 => sym::u16,
            UintTy::U32 => sym::u32,
            UintTy::U64 => sym::u64,
            UintTy::U128 => sym::u128,
        }
    }
}

/// A constraint on an associated type (e.g., `A = Bar` in `Foo<A = Bar>` or
/// `A: TraitA + TraitB` in `Foo<A: TraitA + TraitB>`).
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct AssocConstraint {
    pub id: NodeId,
    pub ident: Ident,
    pub gen_args: Option<GenericArgs>,
    pub kind: AssocConstraintKind,
    pub span: Span,
}

/// The kinds of an `AssocConstraint`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum Term {
    Ty(P<Ty>),
    Const(AnonConst),
}

impl From<P<Ty>> for Term {
    fn from(v: P<Ty>) -> Self {
        Term::Ty(v)
    }
}

impl From<AnonConst> for Term {
    fn from(v: AnonConst) -> Self {
        Term::Const(v)
    }
}

/// The kinds of an `AssocConstraint`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum AssocConstraintKind {
    /// E.g., `A = Bar`, `A = 3` in `Foo<A = Bar>` where A is an associated type.
    Equality { term: Term },
    /// E.g. `A: TraitA + TraitB` in `Foo<A: TraitA + TraitB>`.
    Bound { bounds: GenericBounds },
}

#[derive(Encodable, Decodable, Debug)]
pub struct Ty {
    pub id: NodeId,
    pub kind: TyKind,
    pub span: Span,
    pub tokens: Option<LazyAttrTokenStream>,
}

impl Clone for Ty {
    fn clone(&self) -> Self {
        ensure_sufficient_stack(|| Self {
            id: self.id,
            kind: self.kind.clone(),
            span: self.span,
            tokens: self.tokens.clone(),
        })
    }
}

impl Ty {
    pub fn peel_refs(&self) -> &Self {
        let mut final_ty = self;
        while let TyKind::Ref(_, MutTy { ty, .. }) | TyKind::Ptr(MutTy { ty, .. }) = &final_ty.kind
        {
            final_ty = ty;
        }
        final_ty
    }
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct BareFnTy {
    pub unsafety: Unsafe,
    pub ext: Extern,
    pub generic_params: ThinVec<GenericParam>,
    pub decl: P<FnDecl>,
    /// Span of the `fn(...) -> ...` part.
    pub decl_span: Span,
}

/// The various kinds of type recognized by the compiler.
//
// Adding a new variant? Please update `test_ty` in `tests/ui/macros/stringify.rs`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum TyKind {
    /// A variable-length slice (`[T]`).
    Slice(P<Ty>),
    /// A fixed length array (`[T; n]`).
    Array(P<Ty>, AnonConst),
    /// A raw pointer (`*const T` or `*mut T`).
    Ptr(MutTy),
    /// A reference (`&'a T` or `&'a mut T`).
    Ref(Option<Lifetime>, MutTy),
    /// A bare function (e.g., `fn(usize) -> bool`).
    BareFn(P<BareFnTy>),
    /// The never type (`!`).
    Never,
    /// A tuple (`(A, B, C, D,...)`).
    Tup(ThinVec<P<Ty>>),
    /// An anonymous struct type i.e. `struct { foo: Type }`
    AnonStruct(NodeId, ThinVec<FieldDef>),
    /// An anonymous union type i.e. `union { bar: Type }`
    AnonUnion(NodeId, ThinVec<FieldDef>),
    /// A path (`module::module::...::Type`), optionally
    /// "qualified", e.g., `<Vec<T> as SomeTrait>::SomeType`.
    ///
    /// Type parameters are stored in the `Path` itself.
    Path(Option<P<QSelf>>, Path),
    /// A trait object type `Bound1 + Bound2 + Bound3`
    /// where `Bound` is a trait or a lifetime.
    TraitObject(GenericBounds, TraitObjectSyntax),
    /// An `impl Bound1 + Bound2 + Bound3` type
    /// where `Bound` is a trait or a lifetime.
    ///
    /// The `NodeId` exists to prevent lowering from having to
    /// generate `NodeId`s on the fly, which would complicate
    /// the generation of opaque `type Foo = impl Trait` items significantly.
    ImplTrait(NodeId, GenericBounds),
    /// No-op; kept solely so that we can pretty-print faithfully.
    Paren(P<Ty>),
    /// Unused for now.
    Typeof(AnonConst),
    /// This means the type should be inferred instead of it having been
    /// specified. This can appear anywhere in a type.
    Infer,
    /// Inferred type of a `self` or `&self` argument in a method.
    ImplicitSelf,
    /// A macro in the type position.
    MacCall(P<MacCall>),
    /// Placeholder for a `va_list`.
    CVarArgs,
    /// Sometimes we need a dummy value when no error has occurred.
    Dummy,
    /// Placeholder for a kind that has failed to be defined.
    Err(ErrorGuaranteed),
}

impl TyKind {
    pub fn is_implicit_self(&self) -> bool {
        matches!(self, TyKind::ImplicitSelf)
    }

    pub fn is_unit(&self) -> bool {
        matches!(self, TyKind::Tup(tys) if tys.is_empty())
    }

    pub fn is_simple_path(&self) -> Option<Symbol> {
        if let TyKind::Path(None, Path { segments, .. }) = &self
            && let [segment] = &segments[..]
            && segment.args.is_none()
        {
            Some(segment.ident.name)
        } else {
            None
        }
    }

    pub fn is_anon_adt(&self) -> bool {
        matches!(self, TyKind::AnonStruct(..) | TyKind::AnonUnion(..))
    }
}

/// Syntax used to declare a trait object.
#[derive(Clone, Copy, PartialEq, Encodable, Decodable, Debug, HashStable_Generic)]
pub enum TraitObjectSyntax {
    Dyn,
    DynStar,
    None,
}

/// Inline assembly operand explicit register or register class.
///
/// E.g., `"eax"` as in `asm!("mov eax, 2", out("eax") result)`.
#[derive(Clone, Copy, Encodable, Decodable, Debug)]
pub enum InlineAsmRegOrRegClass {
    Reg(Symbol),
    RegClass(Symbol),
}

#[derive(Clone, Copy, PartialEq, Eq, Hash, Encodable, Decodable, HashStable_Generic)]
pub struct InlineAsmOptions(u16);
bitflags::bitflags! {
    impl InlineAsmOptions: u16 {
        const PURE            = 1 << 0;
        const NOMEM           = 1 << 1;
        const READONLY        = 1 << 2;
        const PRESERVES_FLAGS = 1 << 3;
        const NORETURN        = 1 << 4;
        const NOSTACK         = 1 << 5;
        const ATT_SYNTAX      = 1 << 6;
        const RAW             = 1 << 7;
        const MAY_UNWIND      = 1 << 8;
    }
}

impl std::fmt::Debug for InlineAsmOptions {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        bitflags::parser::to_writer(self, f)
    }
}

#[derive(Clone, PartialEq, Encodable, Decodable, Debug, Hash, HashStable_Generic)]
pub enum InlineAsmTemplatePiece {
    String(String),
    Placeholder { operand_idx: usize, modifier: Option<char>, span: Span },
}

impl fmt::Display for InlineAsmTemplatePiece {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::String(s) => {
                for c in s.chars() {
                    match c {
                        '{' => f.write_str("{{")?,
                        '}' => f.write_str("}}")?,
                        _ => c.fmt(f)?,
                    }
                }
                Ok(())
            }
            Self::Placeholder { operand_idx, modifier: Some(modifier), .. } => {
                write!(f, "{{{operand_idx}:{modifier}}}")
            }
            Self::Placeholder { operand_idx, modifier: None, .. } => {
                write!(f, "{{{operand_idx}}}")
            }
        }
    }
}

impl InlineAsmTemplatePiece {
    /// Rebuilds the asm template string from its pieces.
    pub fn to_string(s: &[Self]) -> String {
        use fmt::Write;
        let mut out = String::new();
        for p in s.iter() {
            let _ = write!(out, "{p}");
        }
        out
    }
}

/// Inline assembly symbol operands get their own AST node that is somewhat
/// similar to `AnonConst`.
///
/// The main difference is that we specifically don't assign it `DefId` in
/// `DefCollector`. Instead this is deferred until AST lowering where we
/// lower it to an `AnonConst` (for functions) or a `Path` (for statics)
/// depending on what the path resolves to.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct InlineAsmSym {
    pub id: NodeId,
    pub qself: Option<P<QSelf>>,
    pub path: Path,
}

/// Inline assembly operand.
///
/// E.g., `out("eax") result` as in `asm!("mov eax, 2", out("eax") result)`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum InlineAsmOperand {
    In {
        reg: InlineAsmRegOrRegClass,
        expr: P<Expr>,
    },
    Out {
        reg: InlineAsmRegOrRegClass,
        late: bool,
        expr: Option<P<Expr>>,
    },
    InOut {
        reg: InlineAsmRegOrRegClass,
        late: bool,
        expr: P<Expr>,
    },
    SplitInOut {
        reg: InlineAsmRegOrRegClass,
        late: bool,
        in_expr: P<Expr>,
        out_expr: Option<P<Expr>>,
    },
    Const {
        anon_const: AnonConst,
    },
    Sym {
        sym: InlineAsmSym,
    },
    Label {
        block: P<Block>,
    },
}

impl InlineAsmOperand {
    pub fn reg(&self) -> Option<&InlineAsmRegOrRegClass> {
        match self {
            Self::In { reg, .. }
            | Self::Out { reg, .. }
            | Self::InOut { reg, .. }
            | Self::SplitInOut { reg, .. } => Some(reg),
            Self::Const { .. } | Self::Sym { .. } | Self::Label { .. } => None,
        }
    }
}

/// Inline assembly.
///
/// E.g., `asm!("NOP");`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct InlineAsm {
    pub template: Vec<InlineAsmTemplatePiece>,
    pub template_strs: Box<[(Symbol, Option<Symbol>, Span)]>,
    pub operands: Vec<(InlineAsmOperand, Span)>,
    pub clobber_abis: Vec<(Symbol, Span)>,
    pub options: InlineAsmOptions,
    pub line_spans: Vec<Span>,
}

/// A parameter in a function header.
///
/// E.g., `bar: usize` as in `fn foo(bar: usize)`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Param {
    pub attrs: AttrVec,
    pub ty: P<Ty>,
    pub pat: P<Pat>,
    pub id: NodeId,
    pub span: Span,
    pub is_placeholder: bool,
}

/// Alternative representation for `Arg`s describing `self` parameter of methods.
///
/// E.g., `&mut self` as in `fn foo(&mut self)`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum SelfKind {
    /// `self`, `mut self`
    Value(Mutability),
    /// `&'lt self`, `&'lt mut self`
    Region(Option<Lifetime>, Mutability),
    /// `self: TYPE`, `mut self: TYPE`
    Explicit(P<Ty>, Mutability),
}

pub type ExplicitSelf = Spanned<SelfKind>;

impl Param {
    /// Attempts to cast parameter to `ExplicitSelf`.
    pub fn to_self(&self) -> Option<ExplicitSelf> {
        if let PatKind::Ident(BindingAnnotation(ByRef::No, mutbl), ident, _) = self.pat.kind {
            if ident.name == kw::SelfLower {
                return match self.ty.kind {
                    TyKind::ImplicitSelf => Some(respan(self.pat.span, SelfKind::Value(mutbl))),
                    TyKind::Ref(lt, MutTy { ref ty, mutbl }) if ty.kind.is_implicit_self() => {
                        Some(respan(self.pat.span, SelfKind::Region(lt, mutbl)))
                    }
                    _ => Some(respan(
                        self.pat.span.to(self.ty.span),
                        SelfKind::Explicit(self.ty.clone(), mutbl),
                    )),
                };
            }
        }
        None
    }

    /// Returns `true` if parameter is `self`.
    pub fn is_self(&self) -> bool {
        if let PatKind::Ident(_, ident, _) = self.pat.kind {
            ident.name == kw::SelfLower
        } else {
            false
        }
    }

    /// Builds a `Param` object from `ExplicitSelf`.
    pub fn from_self(attrs: AttrVec, eself: ExplicitSelf, eself_ident: Ident) -> Param {
        let span = eself.span.to(eself_ident.span);
        let infer_ty = P(Ty {
            id: DUMMY_NODE_ID,
            kind: TyKind::ImplicitSelf,
            span: eself_ident.span,
            tokens: None,
        });
        let (mutbl, ty) = match eself.node {
            SelfKind::Explicit(ty, mutbl) => (mutbl, ty),
            SelfKind::Value(mutbl) => (mutbl, infer_ty),
            SelfKind::Region(lt, mutbl) => (
                Mutability::Not,
                P(Ty {
                    id: DUMMY_NODE_ID,
                    kind: TyKind::Ref(lt, MutTy { ty: infer_ty, mutbl }),
                    span,
                    tokens: None,
                }),
            ),
        };
        Param {
            attrs,
            pat: P(Pat {
                id: DUMMY_NODE_ID,
                kind: PatKind::Ident(BindingAnnotation(ByRef::No, mutbl), eself_ident, None),
                span,
                tokens: None,
            }),
            span,
            ty,
            id: DUMMY_NODE_ID,
            is_placeholder: false,
        }
    }
}

/// A signature (not the body) of a function declaration.
///
/// E.g., `fn foo(bar: baz)`.
///
/// Please note that it's different from `FnHeader` structure
/// which contains metadata about function safety, asyncness, constness and ABI.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct FnDecl {
    pub inputs: ThinVec<Param>,
    pub output: FnRetTy,
}

impl FnDecl {
    pub fn has_self(&self) -> bool {
        self.inputs.get(0).is_some_and(Param::is_self)
    }
    pub fn c_variadic(&self) -> bool {
        self.inputs.last().is_some_and(|arg| matches!(arg.ty.kind, TyKind::CVarArgs))
    }
}

/// Is the trait definition an auto trait?
#[derive(Copy, Clone, PartialEq, Encodable, Decodable, Debug, HashStable_Generic)]
pub enum IsAuto {
    Yes,
    No,
}

#[derive(Copy, Clone, PartialEq, Eq, Hash, Encodable, Decodable, Debug)]
#[derive(HashStable_Generic)]
pub enum Unsafe {
    Yes(Span),
    No,
}

/// Describes what kind of coroutine markers, if any, a function has.
///
/// Coroutine markers are things that cause the function to generate a coroutine, such as `async`,
/// which makes the function return `impl Future`, or `gen`, which makes the function return `impl
/// Iterator`.
#[derive(Copy, Clone, Encodable, Decodable, Debug)]
pub enum CoroutineKind {
    /// `async`, which returns an `impl Future`
    Async { span: Span, closure_id: NodeId, return_impl_trait_id: NodeId },
    /// `gen`, which returns an `impl Iterator`
    Gen { span: Span, closure_id: NodeId, return_impl_trait_id: NodeId },
    /// `async gen`, which returns an `impl AsyncIterator`
    AsyncGen { span: Span, closure_id: NodeId, return_impl_trait_id: NodeId },
}

impl CoroutineKind {
    pub fn is_async(self) -> bool {
        matches!(self, CoroutineKind::Async { .. })
    }

    pub fn is_gen(self) -> bool {
        matches!(self, CoroutineKind::Gen { .. })
    }

    pub fn closure_id(self) -> NodeId {
        match self {
            CoroutineKind::Async { closure_id, .. }
            | CoroutineKind::Gen { closure_id, .. }
            | CoroutineKind::AsyncGen { closure_id, .. } => closure_id,
        }
    }

    /// In this case this is an `async` or `gen` return, the `NodeId` for the generated `impl Trait`
    /// item.
    pub fn return_id(self) -> (NodeId, Span) {
        match self {
            CoroutineKind::Async { return_impl_trait_id, span, .. }
            | CoroutineKind::Gen { return_impl_trait_id, span, .. }
            | CoroutineKind::AsyncGen { return_impl_trait_id, span, .. } => {
                (return_impl_trait_id, span)
            }
        }
    }
}

#[derive(Copy, Clone, PartialEq, Eq, Hash, Encodable, Decodable, Debug)]
#[derive(HashStable_Generic)]
pub enum Const {
    Yes(Span),
    No,
}

/// Item defaultness.
/// For details see the [RFC #2532](https://github.com/rust-lang/rfcs/pull/2532).
#[derive(Copy, Clone, PartialEq, Encodable, Decodable, Debug, HashStable_Generic)]
pub enum Defaultness {
    Default(Span),
    Final,
}

#[derive(Copy, Clone, PartialEq, Encodable, Decodable, HashStable_Generic)]
pub enum ImplPolarity {
    /// `impl Trait for Type`
    Positive,
    /// `impl !Trait for Type`
    Negative(Span),
}

impl fmt::Debug for ImplPolarity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match *self {
            ImplPolarity::Positive => "positive".fmt(f),
            ImplPolarity::Negative(_) => "negative".fmt(f),
        }
    }
}

/// The polarity of a trait bound.
#[derive(Copy, Clone, PartialEq, Eq, Encodable, Decodable, Debug)]
#[derive(HashStable_Generic)]
pub enum BoundPolarity {
    /// `Type: Trait`
    Positive,
    /// `Type: !Trait`
    Negative(Span),
    /// `Type: ?Trait`
    Maybe(Span),
}

impl BoundPolarity {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Positive => "",
            Self::Negative(_) => "!",
            Self::Maybe(_) => "?",
        }
    }
}

/// The constness of a trait bound.
#[derive(Copy, Clone, PartialEq, Eq, Encodable, Decodable, Debug)]
#[derive(HashStable_Generic)]
pub enum BoundConstness {
    /// `Type: Trait`
    Never,
    /// `Type: const Trait`
    Always(Span),
    /// `Type: ~const Trait`
    Maybe(Span),
}

impl BoundConstness {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Never => "",
            Self::Always(_) => "const",
            Self::Maybe(_) => "~const",
        }
    }
}

/// The asyncness of a trait bound.
#[derive(Copy, Clone, PartialEq, Eq, Encodable, Decodable, Debug)]
#[derive(HashStable_Generic)]
pub enum BoundAsyncness {
    /// `Type: Trait`
    Normal,
    /// `Type: async Trait`
    Async(Span),
}

impl BoundAsyncness {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Normal => "",
            Self::Async(_) => "async",
        }
    }
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub enum FnRetTy {
    /// Returns type is not specified.
    ///
    /// Functions default to `()` and closures default to inference.
    /// Span points to where return type would be inserted.
    Default(Span),
    /// Everything else.
    Ty(P<Ty>),
}

impl FnRetTy {
    pub fn span(&self) -> Span {
        match self {
            &FnRetTy::Default(span) => span,
            FnRetTy::Ty(ty) => ty.span,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Encodable, Decodable, Debug)]
pub enum Inline {
    Yes,
    No,
}

/// Module item kind.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum ModKind {
    /// Module with inlined definition `mod foo { ... }`,
    /// or with definition outlined to a separate file `mod foo;` and already loaded from it.
    /// The inner span is from the first token past `{` to the last token until `}`,
    /// or from the first to the last token in the loaded file.
    Loaded(ThinVec<P<Item>>, Inline, ModSpans),
    /// Module with definition outlined to a separate file `mod foo;` but not yet loaded from it.
    Unloaded,
}

#[derive(Copy, Clone, Encodable, Decodable, Debug, Default)]
pub struct ModSpans {
    /// `inner_span` covers the body of the module; for a file module, its the whole file.
    /// For an inline module, its the span inside the `{ ... }`, not including the curly braces.
    pub inner_span: Span,
    pub inject_use_span: Span,
}

/// Foreign module declaration.
///
/// E.g., `extern { .. }` or `extern "C" { .. }`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct ForeignMod {
    /// `unsafe` keyword accepted syntactically for macro DSLs, but not
    /// semantically by Rust.
    pub unsafety: Unsafe,
    pub abi: Option<StrLit>,
    pub items: ThinVec<P<ForeignItem>>,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct EnumDef {
    pub variants: ThinVec<Variant>,
}
/// Enum variant.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Variant {
    /// Attributes of the variant.
    pub attrs: AttrVec,
    /// Id of the variant (not the constructor, see `VariantData::ctor_id()`).
    pub id: NodeId,
    /// Span
    pub span: Span,
    /// The visibility of the variant. Syntactically accepted but not semantically.
    pub vis: Visibility,
    /// Name of the variant.
    pub ident: Ident,

    /// Fields and constructor id of the variant.
    pub data: VariantData,
    /// Explicit discriminant, e.g., `Foo = 1`.
    pub disr_expr: Option<AnonConst>,
    /// Is a macro placeholder
    pub is_placeholder: bool,
}

/// Part of `use` item to the right of its prefix.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum UseTreeKind {
    /// `use prefix` or `use prefix as rename`
    Simple(Option<Ident>),
    /// `use prefix::{...}`
    Nested(ThinVec<(UseTree, NodeId)>),
    /// `use prefix::*`
    Glob,
}

/// A tree of paths sharing common prefixes.
/// Used in `use` items both at top-level and inside of braces in import groups.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct UseTree {
    pub prefix: Path,
    pub kind: UseTreeKind,
    pub span: Span,
}

impl UseTree {
    pub fn ident(&self) -> Ident {
        match self.kind {
            UseTreeKind::Simple(Some(rename)) => rename,
            UseTreeKind::Simple(None) => {
                self.prefix.segments.last().expect("empty prefix in a simple import").ident
            }
            _ => panic!("`UseTree::ident` can only be used on a simple import"),
        }
    }
}

/// Distinguishes between `Attribute`s that decorate items and Attributes that
/// are contained as statements within items. These two cases need to be
/// distinguished for pretty-printing.
#[derive(Clone, PartialEq, Encodable, Decodable, Debug, Copy, HashStable_Generic)]
pub enum AttrStyle {
    Outer,
    Inner,
}

/// A list of attributes.
pub type AttrVec = ThinVec<Attribute>;

/// A syntax-level representation of an attribute.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Attribute {
    pub kind: AttrKind,
    pub id: AttrId,
    /// Denotes if the attribute decorates the following construct (outer)
    /// or the construct this attribute is contained within (inner).
    pub style: AttrStyle,
    pub span: Span,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub enum AttrKind {
    /// A normal attribute.
    Normal(P<NormalAttr>),

    /// A doc comment (e.g. `/// ...`, `//! ...`, `/** ... */`, `/*! ... */`).
    /// Doc attributes (e.g. `#[doc="..."]`) are represented with the `Normal`
    /// variant (which is much less compact and thus more expensive).
    DocComment(CommentKind, Symbol),
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct NormalAttr {
    pub item: AttrItem,
    pub tokens: Option<LazyAttrTokenStream>,
}

impl NormalAttr {
    pub fn from_ident(ident: Ident) -> Self {
        Self {
            item: AttrItem { path: Path::from_ident(ident), args: AttrArgs::Empty, tokens: None },
            tokens: None,
        }
    }
}

#[derive(Clone, Encodable, Decodable, Debug, HashStable_Generic)]
pub struct AttrItem {
    pub path: Path,
    pub args: AttrArgs,
    pub tokens: Option<LazyAttrTokenStream>,
}

/// `TraitRef`s appear in impls.
///
/// Resolution maps each `TraitRef`'s `ref_id` to its defining trait; that's all
/// that the `ref_id` is for. The `impl_id` maps to the "self type" of this impl.
/// If this impl is an `ItemKind::Impl`, the `impl_id` is redundant (it could be the
/// same as the impl's `NodeId`).
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct TraitRef {
    pub path: Path,
    pub ref_id: NodeId,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct PolyTraitRef {
    /// The `'a` in `for<'a> Foo<&'a T>`.
    pub bound_generic_params: ThinVec<GenericParam>,

    /// The `Foo<&'a T>` in `<'a> Foo<&'a T>`.
    pub trait_ref: TraitRef,

    pub span: Span,
}

impl PolyTraitRef {
    pub fn new(generic_params: ThinVec<GenericParam>, path: Path, span: Span) -> Self {
        PolyTraitRef {
            bound_generic_params: generic_params,
            trait_ref: TraitRef { path, ref_id: DUMMY_NODE_ID },
            span,
        }
    }
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Visibility {
    pub kind: VisibilityKind,
    pub span: Span,
    pub tokens: Option<LazyAttrTokenStream>,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub enum VisibilityKind {
    Public,
    Restricted { path: P<Path>, id: NodeId, shorthand: bool },
    Inherited,
}

impl VisibilityKind {
    pub fn is_pub(&self) -> bool {
        matches!(self, VisibilityKind::Public)
    }
}

/// Field definition in a struct, variant or union.
///
/// E.g., `bar: usize` as in `struct Foo { bar: usize }`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct FieldDef {
    pub attrs: AttrVec,
    pub id: NodeId,
    pub span: Span,
    pub vis: Visibility,
    pub ident: Option<Ident>,

    pub ty: P<Ty>,
    pub is_placeholder: bool,
}

/// Fields and constructor ids of enum variants and structs.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum VariantData {
    /// Struct variant.
    ///
    /// E.g., `Bar { .. }` as in `enum Foo { Bar { .. } }`.
    Struct {
        fields: ThinVec<FieldDef>,
        // FIXME: investigate making this a `Option<ErrorGuaranteed>`
        recovered: bool,
    },
    /// Tuple variant.
    ///
    /// E.g., `Bar(..)` as in `enum Foo { Bar(..) }`.
    Tuple(ThinVec<FieldDef>, NodeId),
    /// Unit variant.
    ///
    /// E.g., `Bar = ..` as in `enum Foo { Bar = .. }`.
    Unit(NodeId),
}

impl VariantData {
    /// Return the fields of this variant.
    pub fn fields(&self) -> &[FieldDef] {
        match self {
            VariantData::Struct { fields, .. } | VariantData::Tuple(fields, _) => fields,
            _ => &[],
        }
    }

    /// Return the `NodeId` of this variant's constructor, if it has one.
    pub fn ctor_node_id(&self) -> Option<NodeId> {
        match *self {
            VariantData::Struct { .. } => None,
            VariantData::Tuple(_, id) | VariantData::Unit(id) => Some(id),
        }
    }
}

/// An item definition.
#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Item<K = ItemKind> {
    pub attrs: AttrVec,
    pub id: NodeId,
    pub span: Span,
    pub vis: Visibility,
    /// The name of the item.
    /// It might be a dummy name in case of anonymous items.
    pub ident: Ident,

    pub kind: K,

    /// Original tokens this item was parsed from. This isn't necessarily
    /// available for all items, although over time more and more items should
    /// have this be `Some`. Right now this is primarily used for procedural
    /// macros, notably custom attributes.
    ///
    /// Note that the tokens here do not include the outer attributes, but will
    /// include inner attributes.
    pub tokens: Option<LazyAttrTokenStream>,
}

impl Item {
    /// Return the span that encompasses the attributes.
    pub fn span_with_attributes(&self) -> Span {
        self.attrs.iter().fold(self.span, |acc, attr| acc.to(attr.span))
    }

    pub fn opt_generics(&self) -> Option<&Generics> {
        match &self.kind {
            ItemKind::ExternCrate(_)
            | ItemKind::Use(_)
            | ItemKind::Mod(_, _)
            | ItemKind::ForeignMod(_)
            | ItemKind::GlobalAsm(_)
            | ItemKind::MacCall(_)
            | ItemKind::Delegation(_)
            | ItemKind::MacroDef(_) => None,
            ItemKind::Static(_) => None,
            ItemKind::Const(i) => Some(&i.generics),
            ItemKind::Fn(i) => Some(&i.generics),
            ItemKind::TyAlias(i) => Some(&i.generics),
            ItemKind::TraitAlias(generics, _)
            | ItemKind::Enum(_, generics)
            | ItemKind::Struct(_, generics)
            | ItemKind::Union(_, generics) => Some(&generics),
            ItemKind::Trait(i) => Some(&i.generics),
            ItemKind::Impl(i) => Some(&i.generics),
        }
    }
}

/// `extern` qualifier on a function item or function type.
#[derive(Clone, Copy, Encodable, Decodable, Debug)]
pub enum Extern {
    /// No explicit extern keyword was used
    ///
    /// E.g. `fn foo() {}`
    None,
    /// An explicit extern keyword was used, but with implicit ABI
    ///
    /// E.g. `extern fn foo() {}`
    ///
    /// This is just `extern "C"` (see `rustc_target::spec::abi::Abi::FALLBACK`)
    Implicit(Span),
    /// An explicit extern keyword was used with an explicit ABI
    ///
    /// E.g. `extern "C" fn foo() {}`
    Explicit(StrLit, Span),
}

impl Extern {
    pub fn from_abi(abi: Option<StrLit>, span: Span) -> Extern {
        match abi {
            Some(name) => Extern::Explicit(name, span),
            None => Extern::Implicit(span),
        }
    }
}

/// A function header.
///
/// All the information between the visibility and the name of the function is
/// included in this struct (e.g., `async unsafe fn` or `const extern "C" fn`).
#[derive(Clone, Copy, Encodable, Decodable, Debug)]
pub struct FnHeader {
    /// The `unsafe` keyword, if any
    pub unsafety: Unsafe,
    /// Whether this is `async`, `gen`, or nothing.
    pub coroutine_kind: Option<CoroutineKind>,
    /// The `const` keyword, if any
    pub constness: Const,
    /// The `extern` keyword and corresponding ABI string, if any
    pub ext: Extern,
}

impl FnHeader {
    /// Does this function header have any qualifiers or is it empty?
    pub fn has_qualifiers(&self) -> bool {
        let Self { unsafety, coroutine_kind, constness, ext } = self;
        matches!(unsafety, Unsafe::Yes(_))
            || coroutine_kind.is_some()
            || matches!(constness, Const::Yes(_))
            || !matches!(ext, Extern::None)
    }
}

impl Default for FnHeader {
    fn default() -> FnHeader {
        FnHeader {
            unsafety: Unsafe::No,
            coroutine_kind: None,
            constness: Const::No,
            ext: Extern::None,
        }
    }
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Trait {
    pub unsafety: Unsafe,
    pub is_auto: IsAuto,
    pub generics: Generics,
    pub bounds: GenericBounds,
    pub items: ThinVec<P<AssocItem>>,
}

/// The location of a where clause on a `TyAlias` (`Span`) and whether there was
/// a `where` keyword (`bool`). This is split out from `WhereClause`, since there
/// are two locations for where clause on type aliases, but their predicates
/// are concatenated together.
///
/// Take this example:
/// ```ignore (only-for-syntax-highlight)
/// trait Foo {
///   type Assoc<'a, 'b> where Self: 'a, Self: 'b;
/// }
/// impl Foo for () {
///   type Assoc<'a, 'b> where Self: 'a = () where Self: 'b;
///   //                 ^^^^^^^^^^^^^^ first where clause
///   //                                     ^^^^^^^^^^^^^^ second where clause
/// }
/// ```
///
/// If there is no where clause, then this is `false` with `DUMMY_SP`.
#[derive(Copy, Clone, Encodable, Decodable, Debug, Default)]
pub struct TyAliasWhereClause {
    pub has_where_token: bool,
    pub span: Span,
}

/// The span information for the two where clauses on a `TyAlias`.
#[derive(Copy, Clone, Encodable, Decodable, Debug, Default)]
pub struct TyAliasWhereClauses {
    /// Before the equals sign.
    pub before: TyAliasWhereClause,
    /// After the equals sign.
    pub after: TyAliasWhereClause,
    /// The index in `TyAlias.generics.where_clause.predicates` that would split
    /// into predicates from the where clause before the equals sign and the ones
    /// from the where clause after the equals sign.
    pub split: usize,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct TyAlias {
    pub defaultness: Defaultness,
    pub generics: Generics,
    pub where_clauses: TyAliasWhereClauses,
    pub bounds: GenericBounds,
    pub ty: Option<P<Ty>>,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Impl {
    pub defaultness: Defaultness,
    pub unsafety: Unsafe,
    pub generics: Generics,
    pub constness: Const,
    pub polarity: ImplPolarity,
    /// The trait being implemented, if any.
    pub of_trait: Option<TraitRef>,
    pub self_ty: P<Ty>,
    pub items: ThinVec<P<AssocItem>>,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Fn {
    pub defaultness: Defaultness,
    pub generics: Generics,
    pub sig: FnSig,
    pub body: Option<P<Block>>,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct Delegation {
    /// Path resolution id.
    pub id: NodeId,
    pub qself: Option<P<QSelf>>,
    pub path: Path,
    pub body: Option<P<Block>>,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct StaticItem {
    pub ty: P<Ty>,
    pub mutability: Mutability,
    pub expr: Option<P<Expr>>,
}

#[derive(Clone, Encodable, Decodable, Debug)]
pub struct ConstItem {
    pub defaultness: Defaultness,
    pub generics: Generics,
    pub ty: P<Ty>,
    pub expr: Option<P<Expr>>,
}

// Adding a new variant? Please update `test_item` in `tests/ui/macros/stringify.rs`.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum ItemKind {
    /// An `extern crate` item, with the optional *original* crate name if the crate was renamed.
    ///
    /// E.g., `extern crate foo` or `extern crate foo_bar as foo`.
    ExternCrate(Option<Symbol>),
    /// A use declaration item (`use`).
    ///
    /// E.g., `use foo;`, `use foo::bar;` or `use foo::bar as FooBar;`.
    Use(UseTree),
    /// A static item (`static`).
    ///
    /// E.g., `static FOO: i32 = 42;` or `static FOO: &'static str = "bar";`.
    Static(Box<StaticItem>),
    /// A constant item (`const`).
    ///
    /// E.g., `const FOO: i32 = 42;`.
    Const(Box<ConstItem>),
    /// A function declaration (`fn`).
    ///
    /// E.g., `fn foo(bar: usize) -> usize { .. }`.
    Fn(Box<Fn>),
    /// A module declaration (`mod`).
    ///
    /// E.g., `mod foo;` or `mod foo { .. }`.
    /// `unsafe` keyword on modules is accepted syntactically for macro DSLs, but not
    /// semantically by Rust.
    Mod(Unsafe, ModKind),
    /// An external module (`extern`).
    ///
    /// E.g., `extern {}` or `extern "C" {}`.
    ForeignMod(ForeignMod),
    /// Module-level inline assembly (from `global_asm!()`).
    GlobalAsm(Box<InlineAsm>),
    /// A type alias (`type`).
    ///
    /// E.g., `type Foo = Bar<u8>;`.
    TyAlias(Box<TyAlias>),
    /// An enum definition (`enum`).
    ///
    /// E.g., `enum Foo<A, B> { C<A>, D<B> }`.
    Enum(EnumDef, Generics),
    /// A struct definition (`struct`).
    ///
    /// E.g., `struct Foo<A> { x: A }`.
    Struct(VariantData, Generics),
    /// A union definition (`union`).
    ///
    /// E.g., `union Foo<A, B> { x: A, y: B }`.
    Union(VariantData, Generics),
    /// A trait declaration (`trait`).
    ///
    /// E.g., `trait Foo { .. }`, `trait Foo<T> { .. }` or `auto trait Foo {}`.
    Trait(Box<Trait>),
    /// Trait alias
    ///
    /// E.g., `trait Foo = Bar + Quux;`.
    TraitAlias(Generics, GenericBounds),
    /// An implementation.
    ///
    /// E.g., `impl<A> Foo<A> { .. }` or `impl<A> Trait for Foo<A> { .. }`.
    Impl(Box<Impl>),
    /// A macro invocation.
    ///
    /// E.g., `foo!(..)`.
    MacCall(P<MacCall>),

    /// A macro definition.
    MacroDef(MacroDef),

    /// A delegation item (`reuse`).
    ///
    /// E.g. `reuse <Type as Trait>::name { target_expr_template }`.
    Delegation(Box<Delegation>),
}

impl ItemKind {
    pub fn article(&self) -> &'static str {
        use ItemKind::*;
        match self {
            Use(..) | Static(..) | Const(..) | Fn(..) | Mod(..) | GlobalAsm(..) | TyAlias(..)
            | Struct(..) | Union(..) | Trait(..) | TraitAlias(..) | MacroDef(..)
            | Delegation(..) => "a",
            ExternCrate(..) | ForeignMod(..) | MacCall(..) | Enum(..) | Impl { .. } => "an",
        }
    }

    pub fn descr(&self) -> &'static str {
        match self {
            ItemKind::ExternCrate(..) => "extern crate",
            ItemKind::Use(..) => "`use` import",
            ItemKind::Static(..) => "static item",
            ItemKind::Const(..) => "constant item",
            ItemKind::Fn(..) => "function",
            ItemKind::Mod(..) => "module",
            ItemKind::ForeignMod(..) => "extern block",
            ItemKind::GlobalAsm(..) => "global asm item",
            ItemKind::TyAlias(..) => "type alias",
            ItemKind::Enum(..) => "enum",
            ItemKind::Struct(..) => "struct",
            ItemKind::Union(..) => "union",
            ItemKind::Trait(..) => "trait",
            ItemKind::TraitAlias(..) => "trait alias",
            ItemKind::MacCall(..) => "item macro invocation",
            ItemKind::MacroDef(..) => "macro definition",
            ItemKind::Impl { .. } => "implementation",
            ItemKind::Delegation(..) => "delegated function",
        }
    }

    pub fn generics(&self) -> Option<&Generics> {
        match self {
            Self::Fn(box Fn { generics, .. })
            | Self::TyAlias(box TyAlias { generics, .. })
            | Self::Const(box ConstItem { generics, .. })
            | Self::Enum(_, generics)
            | Self::Struct(_, generics)
            | Self::Union(_, generics)
            | Self::Trait(box Trait { generics, .. })
            | Self::TraitAlias(generics, _)
            | Self::Impl(box Impl { generics, .. }) => Some(generics),
            _ => None,
        }
    }
}

/// Represents associated items.
/// These include items in `impl` and `trait` definitions.
pub type AssocItem = Item<AssocItemKind>;

/// Represents associated item kinds.
///
/// The term "provided" in the variants below refers to the item having a default
/// definition / body. Meanwhile, a "required" item lacks a definition / body.
/// In an implementation, all items must be provided.
/// The `Option`s below denote the bodies, where `Some(_)`
/// means "provided" and conversely `None` means "required".
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum AssocItemKind {
    /// An associated constant, `const $ident: $ty $def?;` where `def ::= "=" $expr? ;`.
    /// If `def` is parsed, then the constant is provided, and otherwise required.
    Const(Box<ConstItem>),
    /// An associated function.
    Fn(Box<Fn>),
    /// An associated type.
    Type(Box<TyAlias>),
    /// A macro expanding to associated items.
    MacCall(P<MacCall>),
    /// An associated delegation item.
    Delegation(Box<Delegation>),
}

impl AssocItemKind {
    pub fn defaultness(&self) -> Defaultness {
        match *self {
            Self::Const(box ConstItem { defaultness, .. })
            | Self::Fn(box Fn { defaultness, .. })
            | Self::Type(box TyAlias { defaultness, .. }) => defaultness,
            Self::MacCall(..) | Self::Delegation(..) => Defaultness::Final,
        }
    }
}

impl From<AssocItemKind> for ItemKind {
    fn from(assoc_item_kind: AssocItemKind) -> ItemKind {
        match assoc_item_kind {
            AssocItemKind::Const(item) => ItemKind::Const(item),
            AssocItemKind::Fn(fn_kind) => ItemKind::Fn(fn_kind),
            AssocItemKind::Type(ty_alias_kind) => ItemKind::TyAlias(ty_alias_kind),
            AssocItemKind::MacCall(a) => ItemKind::MacCall(a),
            AssocItemKind::Delegation(delegation) => ItemKind::Delegation(delegation),
        }
    }
}

impl TryFrom<ItemKind> for AssocItemKind {
    type Error = ItemKind;

    fn try_from(item_kind: ItemKind) -> Result<AssocItemKind, ItemKind> {
        Ok(match item_kind {
            ItemKind::Const(item) => AssocItemKind::Const(item),
            ItemKind::Fn(fn_kind) => AssocItemKind::Fn(fn_kind),
            ItemKind::TyAlias(ty_kind) => AssocItemKind::Type(ty_kind),
            ItemKind::MacCall(a) => AssocItemKind::MacCall(a),
            ItemKind::Delegation(d) => AssocItemKind::Delegation(d),
            _ => return Err(item_kind),
        })
    }
}

/// An item in `extern` block.
#[derive(Clone, Encodable, Decodable, Debug)]
pub enum ForeignItemKind {
    /// A foreign static item (`static FOO: u8`).
    Static(P<Ty>, Mutability, Option<P<Expr>>),
    /// An foreign function.
    Fn(Box<Fn>),
    /// An foreign type.
    TyAlias(Box<TyAlias>),
    /// A macro expanding to foreign items.
    MacCall(P<MacCall>),
}

impl From<ForeignItemKind> for ItemKind {
    fn from(foreign_item_kind: ForeignItemKind) -> ItemKind {
        match foreign_item_kind {
            ForeignItemKind::Static(a, b, c) => {
                ItemKind::Static(StaticItem { ty: a, mutability: b, expr: c }.into())
            }
            ForeignItemKind::Fn(fn_kind) => ItemKind::Fn(fn_kind),
            ForeignItemKind::TyAlias(ty_alias_kind) => ItemKind::TyAlias(ty_alias_kind),
            ForeignItemKind::MacCall(a) => ItemKind::MacCall(a),
        }
    }
}

impl TryFrom<ItemKind> for ForeignItemKind {
    type Error = ItemKind;

    fn try_from(item_kind: ItemKind) -> Result<ForeignItemKind, ItemKind> {
        Ok(match item_kind {
            ItemKind::Static(box StaticItem { ty: a, mutability: b, expr: c }) => {
                ForeignItemKind::Static(a, b, c)
            }
            ItemKind::Fn(fn_kind) => ForeignItemKind::Fn(fn_kind),
            ItemKind::TyAlias(ty_alias_kind) => ForeignItemKind::TyAlias(ty_alias_kind),
            ItemKind::MacCall(a) => ForeignItemKind::MacCall(a),
            _ => return Err(item_kind),
        })
    }
}

pub type ForeignItem = Item<ForeignItemKind>;

// Some nodes are used a lot. Make sure they don't unintentionally get bigger.
#[cfg(all(target_arch = "x86_64", target_pointer_width = "64"))]
mod size_asserts {
    use super::*;
    use rustc_data_structures::static_assert_size;
    // tidy-alphabetical-start
    static_assert_size!(AssocItem, 88);
    static_assert_size!(AssocItemKind, 16);
    static_assert_size!(Attribute, 32);
    static_assert_size!(Block, 32);
    static_assert_size!(Expr, 72);
    static_assert_size!(ExprKind, 40);
    static_assert_size!(Fn, 160);
    static_assert_size!(ForeignItem, 96);
    static_assert_size!(ForeignItemKind, 24);
    static_assert_size!(GenericArg, 24);
    static_assert_size!(GenericBound, 88);
    static_assert_size!(Generics, 40);
    static_assert_size!(Impl, 136);
    static_assert_size!(Item, 136);
    static_assert_size!(ItemKind, 64);
    static_assert_size!(LitKind, 24);
    static_assert_size!(Local, 80);
    static_assert_size!(MetaItemLit, 40);
    static_assert_size!(Param, 40);
    static_assert_size!(Pat, 72);
    static_assert_size!(Path, 24);
    static_assert_size!(PathSegment, 24);
    static_assert_size!(PatKind, 48);
    static_assert_size!(Stmt, 32);
    static_assert_size!(StmtKind, 16);
    static_assert_size!(Ty, 64);
    static_assert_size!(TyKind, 40);
    // tidy-alphabetical-end
}


// --- rs_rustc_hir.rs ---
use crate::def::{CtorKind, DefKind, Res};
use crate::def_id::{DefId, LocalDefIdMap};
pub(crate) use crate::hir_id::{HirId, ItemLocalId, ItemLocalMap, OwnerId};
use crate::intravisit::FnKind;
use crate::LangItem;

use rustc_ast as ast;
use rustc_ast::util::parser::ExprPrecedence;
use rustc_ast::{Attribute, FloatTy, IntTy, Label, LitKind, TraitObjectSyntax, UintTy};
pub use rustc_ast::{BinOp, BinOpKind, BindingAnnotation, BorrowKind, ByRef, CaptureBy};
pub use rustc_ast::{ImplPolarity, IsAuto, Movability, Mutability, UnOp};
use rustc_ast::{InlineAsmOptions, InlineAsmTemplatePiece};
use rustc_data_structures::fingerprint::Fingerprint;
use rustc_data_structures::sorted_map::SortedMap;
use rustc_index::IndexVec;
use rustc_macros::HashStable_Generic;
use rustc_span::hygiene::MacroKind;
use rustc_span::source_map::Spanned;
use rustc_span::symbol::{kw, sym, Ident, Symbol};
use rustc_span::ErrorGuaranteed;
use rustc_span::{def_id::LocalDefId, BytePos, Span, DUMMY_SP};
use rustc_target::asm::InlineAsmRegOrRegClass;
use rustc_target::spec::abi::Abi;

use smallvec::SmallVec;
use std::fmt;

#[derive(Debug, Copy, Clone, HashStable_Generic)]
pub struct Lifetime {
    pub hir_id: HirId,

    /// Either "`'a`", referring to a named lifetime definition,
    /// `'_` referring to an anonymous lifetime (either explicitly `'_` or `&type`),
    /// or "``" (i.e., `kw::Empty`) when appearing in path.
    ///
    /// See `Lifetime::suggestion_position` for practical use.
    pub ident: Ident,

    /// Semantics of this lifetime.
    pub res: LifetimeName,
}

#[derive(Debug, Copy, Clone, HashStable_Generic)]
pub enum ParamName {
    /// Some user-given name like `T` or `'x`.
    Plain(Ident),

    /// Synthetic name generated when user elided a lifetime in an impl header.
    ///
    /// E.g., the lifetimes in cases like these:
    /// ```ignore (fragment)
    /// impl Foo for &u32
    /// impl Foo<'_> for u32
    /// ```
    /// in that case, we rewrite to
    /// ```ignore (fragment)
    /// impl<'f> Foo for &'f u32
    /// impl<'f> Foo<'f> for u32
    /// ```
    /// where `'f` is something like `Fresh(0)`. The indices are
    /// unique per impl, but not necessarily continuous.
    Fresh,

    /// Indicates an illegal name was given and an error has been
    /// reported (so we should squelch other derived errors). Occurs
    /// when, e.g., `'_` is used in the wrong place.
    Error,
}

impl ParamName {
    pub fn ident(&self) -> Ident {
        match *self {
            ParamName::Plain(ident) => ident,
            ParamName::Fresh | ParamName::Error => Ident::with_dummy_span(kw::UnderscoreLifetime),
        }
    }
}

#[derive(Debug, Copy, Clone, PartialEq, Eq, HashStable_Generic)]
pub enum LifetimeName {
    /// User-given names or fresh (synthetic) names.
    Param(LocalDefId),

    /// Implicit lifetime in a context like `dyn Foo`. This is
    /// distinguished from implicit lifetimes elsewhere because the
    /// lifetime that they default to must appear elsewhere within the
    /// enclosing type. This means that, in an `impl Trait` context, we
    /// don't have to create a parameter for them. That is, `impl
    /// Trait<Item = &u32>` expands to an opaque type like `type
    /// Foo<'a> = impl Trait<Item = &'a u32>`, but `impl Trait<item =
    /// dyn Bar>` expands to `type Foo = impl Trait<Item = dyn Bar +
    /// 'static>`. The latter uses `ImplicitObjectLifetimeDefault` so
    /// that surrounding code knows not to create a lifetime
    /// parameter.
    ImplicitObjectLifetimeDefault,

    /// Indicates an error during lowering (usually `'_` in wrong place)
    /// that was already reported.
    Error,

    /// User wrote an anonymous lifetime, either `'_` or nothing.
    /// The semantics of this lifetime should be inferred by typechecking code.
    Infer,

    /// User wrote `'static`.
    Static,
}

impl LifetimeName {
    fn is_elided(&self) -> bool {
        match self {
            LifetimeName::ImplicitObjectLifetimeDefault | LifetimeName::Infer => true,

            // It might seem surprising that `Fresh` counts as not *elided*
            // -- but this is because, as far as the code in the compiler is
            // concerned -- `Fresh` variants act equivalently to "some fresh name".
            // They correspond to early-bound regions on an impl, in other words.
            LifetimeName::Error | LifetimeName::Param(..) | LifetimeName::Static => false,
        }
    }
}

impl fmt::Display for Lifetime {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.ident.name != kw::Empty { self.ident.name.fmt(f) } else { "'_".fmt(f) }
    }
}

pub enum LifetimeSuggestionPosition {
    /// The user wrote `'a` or `'_`.
    Normal,
    /// The user wrote `&type` or `&mut type`.
    Ampersand,
    /// The user wrote `Path` and omitted the `<'_>`.
    ElidedPath,
    /// The user wrote `Path<T>`, and omitted the `'_,`.
    ElidedPathArgument,
    /// The user wrote `dyn Trait` and omitted the `+ '_`.
    ObjectDefault,
}

impl Lifetime {
    pub fn is_elided(&self) -> bool {
        self.res.is_elided()
    }

    pub fn is_anonymous(&self) -> bool {
        self.ident.name == kw::Empty || self.ident.name == kw::UnderscoreLifetime
    }

    pub fn suggestion_position(&self) -> (LifetimeSuggestionPosition, Span) {
        if self.ident.name == kw::Empty {
            if self.ident.span.is_empty() {
                (LifetimeSuggestionPosition::ElidedPathArgument, self.ident.span)
            } else {
                (LifetimeSuggestionPosition::ElidedPath, self.ident.span.shrink_to_hi())
            }
        } else if self.res == LifetimeName::ImplicitObjectLifetimeDefault {
            (LifetimeSuggestionPosition::ObjectDefault, self.ident.span)
        } else if self.ident.span.is_empty() {
            (LifetimeSuggestionPosition::Ampersand, self.ident.span)
        } else {
            (LifetimeSuggestionPosition::Normal, self.ident.span)
        }
    }
}

/// A `Path` is essentially Rust's notion of a name; for instance,
/// `std::cmp::PartialEq`. It's represented as a sequence of identifiers,
/// along with a bunch of supporting information.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Path<'hir, R = Res> {
    pub span: Span,
    /// The resolution for the path.
    pub res: R,
    /// The segments in the path: the things separated by `::`.
    pub segments: &'hir [PathSegment<'hir>],
}

/// Up to three resolutions for type, value and macro namespaces.
pub type UsePath<'hir> = Path<'hir, SmallVec<[Res; 3]>>;

impl Path<'_> {
    pub fn is_global(&self) -> bool {
        !self.segments.is_empty() && self.segments[0].ident.name == kw::PathRoot
    }
}

/// A segment of a path: an identifier, an optional lifetime, and a set of
/// types.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct PathSegment<'hir> {
    /// The identifier portion of this path segment.
    pub ident: Ident,
    pub hir_id: HirId,
    pub res: Res,

    /// Type/lifetime parameters attached to this path. They come in
    /// two flavors: `Path<A,B,C>` and `Path(A,B) -> C`. Note that
    /// this is more than just simple syntactic sugar; the use of
    /// parens affects the region binding rules, so we preserve the
    /// distinction.
    pub args: Option<&'hir GenericArgs<'hir>>,

    /// Whether to infer remaining type parameters, if any.
    /// This only applies to expression and pattern paths, and
    /// out of those only the segments with no type parameters
    /// to begin with, e.g., `Vec::new` is `<Vec<..>>::new::<..>`.
    pub infer_args: bool,
}

impl<'hir> PathSegment<'hir> {
    /// Converts an identifier to the corresponding segment.
    pub fn new(ident: Ident, hir_id: HirId, res: Res) -> PathSegment<'hir> {
        PathSegment { ident, hir_id, res, infer_args: true, args: None }
    }

    pub fn invalid() -> Self {
        Self::new(Ident::empty(), HirId::INVALID, Res::Err)
    }

    pub fn args(&self) -> &GenericArgs<'hir> {
        if let Some(ref args) = self.args {
            args
        } else {
            const DUMMY: &GenericArgs<'_> = &GenericArgs::none();
            DUMMY
        }
    }
}

#[derive(Clone, Copy, Debug, HashStable_Generic)]
pub struct ConstArg {
    pub value: AnonConst,
    pub span: Span,
    /// Indicates whether this comes from a `~const` desugaring.
    pub is_desugared_from_effects: bool,
}

#[derive(Clone, Copy, Debug, HashStable_Generic)]
pub struct InferArg {
    pub hir_id: HirId,
    pub span: Span,
}

impl InferArg {
    pub fn to_ty(&self) -> Ty<'static> {
        Ty { kind: TyKind::Infer, span: self.span, hir_id: self.hir_id }
    }
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum GenericArg<'hir> {
    Lifetime(&'hir Lifetime),
    Type(&'hir Ty<'hir>),
    Const(ConstArg),
    Infer(InferArg),
}

impl GenericArg<'_> {
    pub fn span(&self) -> Span {
        match self {
            GenericArg::Lifetime(l) => l.ident.span,
            GenericArg::Type(t) => t.span,
            GenericArg::Const(c) => c.span,
            GenericArg::Infer(i) => i.span,
        }
    }

    pub fn hir_id(&self) -> HirId {
        match self {
            GenericArg::Lifetime(l) => l.hir_id,
            GenericArg::Type(t) => t.hir_id,
            GenericArg::Const(c) => c.value.hir_id,
            GenericArg::Infer(i) => i.hir_id,
        }
    }

    pub fn descr(&self) -> &'static str {
        match self {
            GenericArg::Lifetime(_) => "lifetime",
            GenericArg::Type(_) => "type",
            GenericArg::Const(_) => "constant",
            GenericArg::Infer(_) => "inferred",
        }
    }

    pub fn to_ord(&self) -> ast::ParamKindOrd {
        match self {
            GenericArg::Lifetime(_) => ast::ParamKindOrd::Lifetime,
            GenericArg::Type(_) | GenericArg::Const(_) | GenericArg::Infer(_) => {
                ast::ParamKindOrd::TypeOrConst
            }
        }
    }

    pub fn is_ty_or_const(&self) -> bool {
        match self {
            GenericArg::Lifetime(_) => false,
            GenericArg::Type(_) | GenericArg::Const(_) | GenericArg::Infer(_) => true,
        }
    }
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct GenericArgs<'hir> {
    /// The generic arguments for this path segment.
    pub args: &'hir [GenericArg<'hir>],
    /// Bindings (equality constraints) on associated types, if present.
    /// E.g., `Foo<A = Bar>`.
    pub bindings: &'hir [TypeBinding<'hir>],
    /// Were arguments written in parenthesized form `Fn(T) -> U`?
    /// This is required mostly for pretty-printing and diagnostics,
    /// but also for changing lifetime elision rules to be "function-like".
    pub parenthesized: GenericArgsParentheses,
    /// The span encompassing arguments and the surrounding brackets `<>` or `()`
    ///       Foo<A, B, AssocTy = D>           Fn(T, U, V) -> W
    ///          ^^^^^^^^^^^^^^^^^^^             ^^^^^^^^^
    /// Note that this may be:
    /// - empty, if there are no generic brackets (but there may be hidden lifetimes)
    /// - dummy, if this was generated while desugaring
    pub span_ext: Span,
}

impl<'hir> GenericArgs<'hir> {
    pub const fn none() -> Self {
        Self {
            args: &[],
            bindings: &[],
            parenthesized: GenericArgsParentheses::No,
            span_ext: DUMMY_SP,
        }
    }

    pub fn inputs(&self) -> &[Ty<'hir>] {
        if self.parenthesized == GenericArgsParentheses::ParenSugar {
            for arg in self.args {
                match arg {
                    GenericArg::Lifetime(_) => {}
                    GenericArg::Type(ref ty) => {
                        if let TyKind::Tup(ref tys) = ty.kind {
                            return tys;
                        }
                        break;
                    }
                    GenericArg::Const(_) => {}
                    GenericArg::Infer(_) => {}
                }
            }
        }
        panic!("GenericArgs::inputs: not a `Fn(T) -> U`");
    }

    pub fn has_err(&self) -> bool {
        self.args.iter().any(|arg| match arg {
            GenericArg::Type(ty) => matches!(ty.kind, TyKind::Err(_)),
            _ => false,
        }) || self.bindings.iter().any(|arg| match arg.kind {
            TypeBindingKind::Equality { term: Term::Ty(ty) } => matches!(ty.kind, TyKind::Err(_)),
            _ => false,
        })
    }

    #[inline]
    pub fn num_lifetime_params(&self) -> usize {
        self.args.iter().filter(|arg| matches!(arg, GenericArg::Lifetime(_))).count()
    }

    #[inline]
    pub fn has_lifetime_params(&self) -> bool {
        self.args.iter().any(|arg| matches!(arg, GenericArg::Lifetime(_)))
    }

    #[inline]
    /// This function returns the number of type and const generic params.
    /// It should only be used for diagnostics.
    pub fn num_generic_params(&self) -> usize {
        self.args
            .iter()
            .filter(|arg| match arg {
                GenericArg::Lifetime(_)
                | GenericArg::Const(ConstArg { is_desugared_from_effects: true, .. }) => false,
                _ => true,
            })
            .count()
    }

    /// The span encompassing the text inside the surrounding brackets.
    /// It will also include bindings if they aren't in the form `-> Ret`
    /// Returns `None` if the span is empty (e.g. no brackets) or dummy
    pub fn span(&self) -> Option<Span> {
        let span_ext = self.span_ext()?;
        Some(span_ext.with_lo(span_ext.lo() + BytePos(1)).with_hi(span_ext.hi() - BytePos(1)))
    }

    /// Returns span encompassing arguments and their surrounding `<>` or `()`
    pub fn span_ext(&self) -> Option<Span> {
        Some(self.span_ext).filter(|span| !span.is_empty())
    }

    pub fn is_empty(&self) -> bool {
        self.args.is_empty()
    }
}

#[derive(Copy, Clone, PartialEq, Eq, Debug, HashStable_Generic)]
pub enum GenericArgsParentheses {
    No,
    /// Bounds for `feature(return_type_notation)`, like `T: Trait<method(..): Send>`,
    /// where the args are explicitly elided with `..`
    ReturnTypeNotation,
    /// parenthesized function-family traits, like `T: Fn(u32) -> i32`
    ParenSugar,
}

/// A modifier on a trait bound.
#[derive(Copy, Clone, PartialEq, Eq, Hash, Debug, HashStable_Generic)]
pub enum TraitBoundModifier {
    /// `Type: Trait`
    None,
    /// `Type: !Trait`
    Negative,
    /// `Type: ?Trait`
    Maybe,
    /// `Type: const Trait`
    Const,
    /// `Type: ~const Trait`
    MaybeConst,
}

/// The AST represents all type param bounds as types.
/// `typeck::collect::compute_bounds` matches these against
/// the "special" built-in traits (see `middle::lang_items`) and
/// detects `Copy`, `Send` and `Sync`.
#[derive(Clone, Copy, Debug, HashStable_Generic)]
pub enum GenericBound<'hir> {
    Trait(PolyTraitRef<'hir>, TraitBoundModifier),
    Outlives(&'hir Lifetime),
}

impl GenericBound<'_> {
    pub fn trait_ref(&self) -> Option<&TraitRef<'_>> {
        match self {
            GenericBound::Trait(data, _) => Some(&data.trait_ref),
            _ => None,
        }
    }

    pub fn span(&self) -> Span {
        match self {
            GenericBound::Trait(t, ..) => t.span,
            GenericBound::Outlives(l) => l.ident.span,
        }
    }
}

pub type GenericBounds<'hir> = &'hir [GenericBound<'hir>];

#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub enum LifetimeParamKind {
    // Indicates that the lifetime definition was explicitly declared (e.g., in
    // `fn foo<'a>(x: &'a u8) -> &'a u8 { x }`).
    Explicit,

    // Indication that the lifetime was elided (e.g., in both cases in
    // `fn foo(x: &u8) -> &'_ u8 { x }`).
    Elided,

    // Indication that the lifetime name was somehow in error.
    Error,
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum GenericParamKind<'hir> {
    /// A lifetime definition (e.g., `'a: 'b + 'c + 'd`).
    Lifetime {
        kind: LifetimeParamKind,
    },
    Type {
        default: Option<&'hir Ty<'hir>>,
        synthetic: bool,
    },
    Const {
        ty: &'hir Ty<'hir>,
        /// Optional default value for the const generic param
        default: Option<AnonConst>,
        is_host_effect: bool,
    },
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct GenericParam<'hir> {
    pub hir_id: HirId,
    pub def_id: LocalDefId,
    pub name: ParamName,
    pub span: Span,
    pub pure_wrt_drop: bool,
    pub kind: GenericParamKind<'hir>,
    pub colon_span: Option<Span>,
    pub source: GenericParamSource,
}

impl<'hir> GenericParam<'hir> {
    /// Synthetic type-parameters are inserted after normal ones.
    /// In order for normal parameters to be able to refer to synthetic ones,
    /// scans them first.
    pub fn is_impl_trait(&self) -> bool {
        matches!(self.kind, GenericParamKind::Type { synthetic: true, .. })
    }

    /// This can happen for `async fn`, e.g. `async fn f<'_>(&'_ self)`.
    ///
    /// See `lifetime_to_generic_param` in `rustc_ast_lowering` for more information.
    pub fn is_elided_lifetime(&self) -> bool {
        matches!(self.kind, GenericParamKind::Lifetime { kind: LifetimeParamKind::Elided })
    }
}

/// Records where the generic parameter originated from.
///
/// This can either be from an item's generics, in which case it's typically
/// early-bound (but can be a late-bound lifetime in functions, for example),
/// or from a `for<...>` binder, in which case it's late-bound (and notably,
/// does not show up in the parent item's generics).
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum GenericParamSource {
    // Early or late-bound parameters defined on an item
    Generics,
    // Late-bound parameters defined via a `for<...>`
    Binder,
}

#[derive(Default)]
pub struct GenericParamCount {
    pub lifetimes: usize,
    pub types: usize,
    pub consts: usize,
    pub infer: usize,
}

/// Represents lifetimes and type parameters attached to a declaration
/// of a function, enum, trait, etc.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Generics<'hir> {
    pub params: &'hir [GenericParam<'hir>],
    pub predicates: &'hir [WherePredicate<'hir>],
    pub has_where_clause_predicates: bool,
    pub where_clause_span: Span,
    pub span: Span,
}

impl<'hir> Generics<'hir> {
    pub const fn empty() -> &'hir Generics<'hir> {
        const NOPE: Generics<'_> = Generics {
            params: &[],
            predicates: &[],
            has_where_clause_predicates: false,
            where_clause_span: DUMMY_SP,
            span: DUMMY_SP,
        };
        &NOPE
    }

    pub fn get_named(&self, name: Symbol) -> Option<&GenericParam<'hir>> {
        self.params.iter().find(|&param| name == param.name.ident().name)
    }

    /// If there are generic parameters, return where to introduce a new one.
    pub fn span_for_lifetime_suggestion(&self) -> Option<Span> {
        if let Some(first) = self.params.first()
            && self.span.contains(first.span)
        {
            // `fn foo<A>(t: impl Trait)`
            //         ^ suggest `'a, ` here
            Some(first.span.shrink_to_lo())
        } else {
            None
        }
    }

    /// If there are generic parameters, return where to introduce a new one.
    pub fn span_for_param_suggestion(&self) -> Option<Span> {
        self.params.iter().any(|p| self.span.contains(p.span)).then(|| {
            // `fn foo<A>(t: impl Trait)`
            //          ^ suggest `, T: Trait` here
            self.span.with_lo(self.span.hi() - BytePos(1)).shrink_to_lo()
        })
    }

    /// `Span` where further predicates would be suggested, accounting for trailing commas, like
    ///  in `fn foo<T>(t: T) where T: Foo,` so we don't suggest two trailing commas.
    pub fn tail_span_for_predicate_suggestion(&self) -> Span {
        let end = self.where_clause_span.shrink_to_hi();
        if self.has_where_clause_predicates {
            self.predicates
                .iter()
                .rfind(|&p| p.in_where_clause())
                .map_or(end, |p| p.span())
                .shrink_to_hi()
                .to(end)
        } else {
            end
        }
    }

    pub fn add_where_or_trailing_comma(&self) -> &'static str {
        if self.has_where_clause_predicates {
            ","
        } else if self.where_clause_span.is_empty() {
            " where"
        } else {
            // No where clause predicates, but we have `where` token
            ""
        }
    }

    pub fn bounds_for_param(
        &self,
        param_def_id: LocalDefId,
    ) -> impl Iterator<Item = &WhereBoundPredicate<'hir>> {
        self.predicates.iter().filter_map(move |pred| match pred {
            WherePredicate::BoundPredicate(bp) if bp.is_param_bound(param_def_id.to_def_id()) => {
                Some(bp)
            }
            _ => None,
        })
    }

    pub fn outlives_for_param(
        &self,
        param_def_id: LocalDefId,
    ) -> impl Iterator<Item = &WhereRegionPredicate<'_>> {
        self.predicates.iter().filter_map(move |pred| match pred {
            WherePredicate::RegionPredicate(rp) if rp.is_param_bound(param_def_id) => Some(rp),
            _ => None,
        })
    }

    pub fn bounds_span_for_suggestions(&self, param_def_id: LocalDefId) -> Option<Span> {
        self.bounds_for_param(param_def_id).flat_map(|bp| bp.bounds.iter().rev()).find_map(
            |bound| {
                // We include bounds that come from a `#[derive(_)]` but point at the user's code,
                // as we use this method to get a span appropriate for suggestions.
                let bs = bound.span();
                bs.can_be_used_for_suggestions().then(|| bs.shrink_to_hi())
            },
        )
    }

    fn span_for_predicate_removal(&self, pos: usize) -> Span {
        let predicate = &self.predicates[pos];
        let span = predicate.span();

        if !predicate.in_where_clause() {
            // <T: ?Sized, U>
            //   ^^^^^^^^
            return span;
        }

        // We need to find out which comma to remove.
        if pos < self.predicates.len() - 1 {
            let next_pred = &self.predicates[pos + 1];
            if next_pred.in_where_clause() {
                // where T: ?Sized, Foo: Bar,
                //       ^^^^^^^^^^^
                return span.until(next_pred.span());
            }
        }

        if pos > 0 {
            let prev_pred = &self.predicates[pos - 1];
            if prev_pred.in_where_clause() {
                // where Foo: Bar, T: ?Sized,
                //               ^^^^^^^^^^^
                return prev_pred.span().shrink_to_hi().to(span);
            }
        }

        // This is the only predicate in the where clause.
        // where T: ?Sized
        // ^^^^^^^^^^^^^^^
        self.where_clause_span
    }

    pub fn span_for_bound_removal(&self, predicate_pos: usize, bound_pos: usize) -> Span {
        let predicate = &self.predicates[predicate_pos];
        let bounds = predicate.bounds();

        if bounds.len() == 1 {
            return self.span_for_predicate_removal(predicate_pos);
        }

        let span = bounds[bound_pos].span();
        if bound_pos == 0 {
            // where T: ?Sized + Bar, Foo: Bar,
            //          ^^^^^^^^^
            span.to(bounds[1].span().shrink_to_lo())
        } else {
            // where T: Bar + ?Sized, Foo: Bar,
            //             ^^^^^^^^^
            bounds[bound_pos - 1].span().shrink_to_hi().to(span)
        }
    }
}

/// A single predicate in a where-clause.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum WherePredicate<'hir> {
    /// A type binding (e.g., `for<'c> Foo: Send + Clone + 'c`).
    BoundPredicate(WhereBoundPredicate<'hir>),
    /// A lifetime predicate (e.g., `'a: 'b + 'c`).
    RegionPredicate(WhereRegionPredicate<'hir>),
    /// An equality predicate (unsupported).
    EqPredicate(WhereEqPredicate<'hir>),
}

impl<'hir> WherePredicate<'hir> {
    pub fn span(&self) -> Span {
        match self {
            WherePredicate::BoundPredicate(p) => p.span,
            WherePredicate::RegionPredicate(p) => p.span,
            WherePredicate::EqPredicate(p) => p.span,
        }
    }

    pub fn in_where_clause(&self) -> bool {
        match self {
            WherePredicate::BoundPredicate(p) => p.origin == PredicateOrigin::WhereClause,
            WherePredicate::RegionPredicate(p) => p.in_where_clause,
            WherePredicate::EqPredicate(_) => false,
        }
    }

    pub fn bounds(&self) -> GenericBounds<'hir> {
        match self {
            WherePredicate::BoundPredicate(p) => p.bounds,
            WherePredicate::RegionPredicate(p) => p.bounds,
            WherePredicate::EqPredicate(_) => &[],
        }
    }
}

#[derive(Copy, Clone, Debug, HashStable_Generic, PartialEq, Eq)]
pub enum PredicateOrigin {
    WhereClause,
    GenericParam,
    ImplTrait,
}

/// A type bound (e.g., `for<'c> Foo: Send + Clone + 'c`).
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct WhereBoundPredicate<'hir> {
    pub hir_id: HirId,
    pub span: Span,
    /// Origin of the predicate.
    pub origin: PredicateOrigin,
    /// Any generics from a `for` binding.
    pub bound_generic_params: &'hir [GenericParam<'hir>],
    /// The type being bounded.
    pub bounded_ty: &'hir Ty<'hir>,
    /// Trait and lifetime bounds (e.g., `Clone + Send + 'static`).
    pub bounds: GenericBounds<'hir>,
}

impl<'hir> WhereBoundPredicate<'hir> {
    /// Returns `true` if `param_def_id` matches the `bounded_ty` of this predicate.
    pub fn is_param_bound(&self, param_def_id: DefId) -> bool {
        self.bounded_ty.as_generic_param().is_some_and(|(def_id, _)| def_id == param_def_id)
    }
}

/// A lifetime predicate (e.g., `'a: 'b + 'c`).
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct WhereRegionPredicate<'hir> {
    pub span: Span,
    pub in_where_clause: bool,
    pub lifetime: &'hir Lifetime,
    pub bounds: GenericBounds<'hir>,
}

impl<'hir> WhereRegionPredicate<'hir> {
    /// Returns `true` if `param_def_id` matches the `lifetime` of this predicate.
    fn is_param_bound(&self, param_def_id: LocalDefId) -> bool {
        self.lifetime.res == LifetimeName::Param(param_def_id)
    }
}

/// An equality predicate (e.g., `T = int`); currently unsupported.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct WhereEqPredicate<'hir> {
    pub span: Span,
    pub lhs_ty: &'hir Ty<'hir>,
    pub rhs_ty: &'hir Ty<'hir>,
}

/// HIR node coupled with its parent's id in the same HIR owner.
///
/// The parent is trash when the node is a HIR owner.
#[derive(Clone, Copy, Debug)]
pub struct ParentedNode<'tcx> {
    pub parent: ItemLocalId,
    pub node: Node<'tcx>,
}

/// Attributes owned by a HIR owner.
#[derive(Debug)]
pub struct AttributeMap<'tcx> {
    pub map: SortedMap<ItemLocalId, &'tcx [Attribute]>,
    // Only present when the crate hash is needed.
    pub opt_hash: Option<Fingerprint>,
}

impl<'tcx> AttributeMap<'tcx> {
    pub const EMPTY: &'static AttributeMap<'static> =
        &AttributeMap { map: SortedMap::new(), opt_hash: Some(Fingerprint::ZERO) };

    #[inline]
    pub fn get(&self, id: ItemLocalId) -> &'tcx [Attribute] {
        self.map.get(&id).copied().unwrap_or(&[])
    }
}

/// Map of all HIR nodes inside the current owner.
/// These nodes are mapped by `ItemLocalId` alongside the index of their parent node.
/// The HIR tree, including bodies, is pre-hashed.
pub struct OwnerNodes<'tcx> {
    /// Pre-computed hash of the full HIR. Used in the crate hash. Only present
    /// when incr. comp. is enabled.
    pub opt_hash_including_bodies: Option<Fingerprint>,
    /// Full HIR for the current owner.
    // The zeroth node's parent should never be accessed: the owner's parent is computed by the
    // hir_owner_parent query. It is set to `ItemLocalId::INVALID` to force an ICE if accidentally
    // used.
    pub nodes: IndexVec<ItemLocalId, ParentedNode<'tcx>>,
    /// Content of local bodies.
    pub bodies: SortedMap<ItemLocalId, &'tcx Body<'tcx>>,
}

impl<'tcx> OwnerNodes<'tcx> {
    pub fn node(&self) -> OwnerNode<'tcx> {
        use rustc_index::Idx;
        // Indexing must ensure it is an OwnerNode.
        self.nodes[ItemLocalId::new(0)].node.as_owner().unwrap()
    }
}

impl fmt::Debug for OwnerNodes<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("OwnerNodes")
            // Do not print all the pointers to all the nodes, as it would be unreadable.
            .field("node", &self.nodes[ItemLocalId::from_u32(0)])
            .field(
                "parents",
                &self
                    .nodes
                    .iter_enumerated()
                    .map(|(id, parented_node)| {
                        debug_fn(move |f| write!(f, "({id:?}, {:?})", parented_node.parent))
                    })
                    .collect::<Vec<_>>(),
            )
            .field("bodies", &self.bodies)
            .field("opt_hash_including_bodies", &self.opt_hash_including_bodies)
            .finish()
    }
}

/// Full information resulting from lowering an AST node.
#[derive(Debug, HashStable_Generic)]
pub struct OwnerInfo<'hir> {
    /// Contents of the HIR.
    pub nodes: OwnerNodes<'hir>,
    /// Map from each nested owner to its parent's local id.
    pub parenting: LocalDefIdMap<ItemLocalId>,
    /// Collected attributes of the HIR nodes.
    pub attrs: AttributeMap<'hir>,
    /// Map indicating what traits are in scope for places where this
    /// is relevant; generated by resolve.
    pub trait_map: ItemLocalMap<Box<[TraitCandidate]>>,
}

impl<'tcx> OwnerInfo<'tcx> {
    #[inline]
    pub fn node(&self) -> OwnerNode<'tcx> {
        self.nodes.node()
    }
}

#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub enum MaybeOwner<'tcx> {
    Owner(&'tcx OwnerInfo<'tcx>),
    NonOwner(HirId),
    /// Used as a placeholder for unused LocalDefId.
    Phantom,
}

impl<'tcx> MaybeOwner<'tcx> {
    pub fn as_owner(self) -> Option<&'tcx OwnerInfo<'tcx>> {
        match self {
            MaybeOwner::Owner(i) => Some(i),
            MaybeOwner::NonOwner(_) | MaybeOwner::Phantom => None,
        }
    }

    pub fn unwrap(self) -> &'tcx OwnerInfo<'tcx> {
        self.as_owner().unwrap_or_else(|| panic!("Not a HIR owner"))
    }
}

/// The top-level data structure that stores the entire contents of
/// the crate currently being compiled.
///
/// For more details, see the [rustc dev guide].
///
/// [rustc dev guide]: https://rustc-dev-guide.rust-lang.org/hir.html
#[derive(Debug)]
pub struct Crate<'hir> {
    pub owners: IndexVec<LocalDefId, MaybeOwner<'hir>>,
    // Only present when incr. comp. is enabled.
    pub opt_hir_hash: Option<Fingerprint>,
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Closure<'hir> {
    pub def_id: LocalDefId,
    pub binder: ClosureBinder,
    pub constness: Constness,
    pub capture_clause: CaptureBy,
    pub bound_generic_params: &'hir [GenericParam<'hir>],
    pub fn_decl: &'hir FnDecl<'hir>,
    pub body: BodyId,
    /// The span of the declaration block: 'move |...| -> ...'
    pub fn_decl_span: Span,
    /// The span of the argument block `|...|`
    pub fn_arg_span: Option<Span>,
    pub kind: ClosureKind,
}

#[derive(Clone, PartialEq, Eq, Debug, Copy, Hash, HashStable_Generic, Encodable, Decodable)]
pub enum ClosureKind {
    /// This is a plain closure expression.
    Closure,
    /// This is a coroutine expression -- i.e. a closure expression in which
    /// we've found a `yield`. These can arise either from "plain" coroutine
    ///  usage (e.g. `let x = || { yield (); }`) or from a desugared expression
    /// (e.g. `async` and `gen` blocks).
    Coroutine(CoroutineKind),
    /// This is a coroutine-closure, which is a special sugared closure that
    /// returns one of the sugared coroutine (`async`/`gen`/`async gen`). It
    /// additionally allows capturing the coroutine's upvars by ref, and therefore
    /// needs to be specially treated during analysis and borrowck.
    CoroutineClosure(CoroutineDesugaring),
}

/// A block of statements `{ .. }`, which may have a label (in this case the
/// `targeted_by_break` field will be `true`) and may be `unsafe` by means of
/// the `rules` being anything but `DefaultBlock`.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Block<'hir> {
    /// Statements in a block.
    pub stmts: &'hir [Stmt<'hir>],
    /// An expression at the end of the block
    /// without a semicolon, if any.
    pub expr: Option<&'hir Expr<'hir>>,
    #[stable_hasher(ignore)]
    pub hir_id: HirId,
    /// Distinguishes between `unsafe { ... }` and `{ ... }`.
    pub rules: BlockCheckMode,
    pub span: Span,
    /// If true, then there may exist `break 'a` values that aim to
    /// break out of this block early.
    /// Used by `'label: {}` blocks and by `try {}` blocks.
    pub targeted_by_break: bool,
}

impl<'hir> Block<'hir> {
    pub fn innermost_block(&self) -> &Block<'hir> {
        let mut block = self;
        while let Some(Expr { kind: ExprKind::Block(inner_block, _), .. }) = block.expr {
            block = inner_block;
        }
        block
    }
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Pat<'hir> {
    #[stable_hasher(ignore)]
    pub hir_id: HirId,
    pub kind: PatKind<'hir>,
    pub span: Span,
    /// Whether to use default binding modes.
    /// At present, this is false only for destructuring assignment.
    pub default_binding_modes: bool,
}

impl<'hir> Pat<'hir> {
    fn walk_short_(&self, it: &mut impl FnMut(&Pat<'hir>) -> bool) -> bool {
        if !it(self) {
            return false;
        }

        use PatKind::*;
        match self.kind {
            Wild | Never | Lit(_) | Range(..) | Binding(.., None) | Path(_) | Err(_) => true,
            Box(s) | Ref(s, _) | Binding(.., Some(s)) => s.walk_short_(it),
            Struct(_, fields, _) => fields.iter().all(|field| field.pat.walk_short_(it)),
            TupleStruct(_, s, _) | Tuple(s, _) | Or(s) => s.iter().all(|p| p.walk_short_(it)),
            Slice(before, slice, after) => {
                before.iter().chain(slice).chain(after.iter()).all(|p| p.walk_short_(it))
            }
        }
    }

    /// Walk the pattern in left-to-right order,
    /// short circuiting (with `.all(..)`) if `false` is returned.
    ///
    /// Note that when visiting e.g. `Tuple(ps)`,
    /// if visiting `ps[0]` returns `false`,
    /// then `ps[1]` will not be visited.
    pub fn walk_short(&self, mut it: impl FnMut(&Pat<'hir>) -> bool) -> bool {
        self.walk_short_(&mut it)
    }

    fn walk_(&self, it: &mut impl FnMut(&Pat<'hir>) -> bool) {
        if !it(self) {
            return;
        }

        use PatKind::*;
        match self.kind {
            Wild | Never | Lit(_) | Range(..) | Binding(.., None) | Path(_) | Err(_) => {}
            Box(s) | Ref(s, _) | Binding(.., Some(s)) => s.walk_(it),
            Struct(_, fields, _) => fields.iter().for_each(|field| field.pat.walk_(it)),
            TupleStruct(_, s, _) | Tuple(s, _) | Or(s) => s.iter().for_each(|p| p.walk_(it)),
            Slice(before, slice, after) => {
                before.iter().chain(slice).chain(after.iter()).for_each(|p| p.walk_(it))
            }
        }
    }

    /// Walk the pattern in left-to-right order.
    ///
    /// If `it(pat)` returns `false`, the children are not visited.
    pub fn walk(&self, mut it: impl FnMut(&Pat<'hir>) -> bool) {
        self.walk_(&mut it)
    }

    /// Walk the pattern in left-to-right order.
    ///
    /// If you always want to recurse, prefer this method over `walk`.
    pub fn walk_always(&self, mut it: impl FnMut(&Pat<'_>)) {
        self.walk(|p| {
            it(p);
            true
        })
    }

    /// Whether this a never pattern.
    pub fn is_never_pattern(&self) -> bool {
        let mut is_never_pattern = false;
        self.walk(|pat| match &pat.kind {
            PatKind::Never => {
                is_never_pattern = true;
                false
            }
            PatKind::Or(s) => {
                is_never_pattern = s.iter().all(|p| p.is_never_pattern());
                false
            }
            _ => true,
        });
        is_never_pattern
    }
}

/// A single field in a struct pattern.
///
/// Patterns like the fields of Foo `{ x, ref y, ref mut z }`
/// are treated the same as` x: x, y: ref y, z: ref mut z`,
/// except `is_shorthand` is true.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct PatField<'hir> {
    #[stable_hasher(ignore)]
    pub hir_id: HirId,
    /// The identifier for the field.
    pub ident: Ident,
    /// The pattern the field is destructured to.
    pub pat: &'hir Pat<'hir>,
    pub is_shorthand: bool,
    pub span: Span,
}

#[derive(Copy, Clone, PartialEq, Debug, HashStable_Generic)]
pub enum RangeEnd {
    Included,
    Excluded,
}

impl fmt::Display for RangeEnd {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            RangeEnd::Included => "..=",
            RangeEnd::Excluded => "..",
        })
    }
}

// Equivalent to `Option<usize>`. That type takes up 16 bytes on 64-bit, but
// this type only takes up 4 bytes, at the cost of being restricted to a
// maximum value of `u32::MAX - 1`. In practice, this is more than enough.
#[derive(Clone, Copy, PartialEq, Eq, Hash, HashStable_Generic)]
pub struct DotDotPos(u32);

impl DotDotPos {
    /// Panics if n >= u32::MAX.
    pub fn new(n: Option<usize>) -> Self {
        match n {
            Some(n) => {
                assert!(n < u32::MAX as usize);
                Self(n as u32)
            }
            None => Self(u32::MAX),
        }
    }

    pub fn as_opt_usize(&self) -> Option<usize> {
        if self.0 == u32::MAX { None } else { Some(self.0 as usize) }
    }
}

impl fmt::Debug for DotDotPos {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.as_opt_usize().fmt(f)
    }
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum PatKind<'hir> {
    /// Represents a wildcard pattern (i.e., `_`).
    Wild,

    /// A fresh binding `ref mut binding @ OPT_SUBPATTERN`.
    /// The `HirId` is the canonical ID for the variable being bound,
    /// (e.g., in `Ok(x) | Err(x)`, both `x` use the same canonical ID),
    /// which is the pattern ID of the first `x`.
    Binding(BindingAnnotation, HirId, Ident, Option<&'hir Pat<'hir>>),

    /// A struct or struct variant pattern (e.g., `Variant {x, y, ..}`).
    /// The `bool` is `true` in the presence of a `..`.
    Struct(QPath<'hir>, &'hir [PatField<'hir>], bool),

    /// A tuple struct/variant pattern `Variant(x, y, .., z)`.
    /// If the `..` pattern fragment is present, then `DotDotPos` denotes its position.
    /// `0 <= position <= subpats.len()`
    TupleStruct(QPath<'hir>, &'hir [Pat<'hir>], DotDotPos),

    /// An or-pattern `A | B | C`.
    /// Invariant: `pats.len() >= 2`.
    Or(&'hir [Pat<'hir>]),

    /// A never pattern `!`.
    Never,

    /// A path pattern for a unit struct/variant or a (maybe-associated) constant.
    Path(QPath<'hir>),

    /// A tuple pattern (e.g., `(a, b)`).
    /// If the `..` pattern fragment is present, then `Option<usize>` denotes its position.
    /// `0 <= position <= subpats.len()`
    Tuple(&'hir [Pat<'hir>], DotDotPos),

    /// A `box` pattern.
    Box(&'hir Pat<'hir>),

    /// A reference pattern (e.g., `&mut (a, b)`).
    Ref(&'hir Pat<'hir>, Mutability),

    /// A literal.
    Lit(&'hir Expr<'hir>),

    /// A range pattern (e.g., `1..=2` or `1..2`).
    Range(Option<&'hir Expr<'hir>>, Option<&'hir Expr<'hir>>, RangeEnd),

    /// A slice pattern, `[before_0, ..., before_n, (slice, after_0, ..., after_n)?]`.
    ///
    /// Here, `slice` is lowered from the syntax `($binding_mode $ident @)? ..`.
    /// If `slice` exists, then `after` can be non-empty.
    ///
    /// The representation for e.g., `[a, b, .., c, d]` is:
    /// ```ignore (illustrative)
    /// PatKind::Slice([Binding(a), Binding(b)], Some(Wild), [Binding(c), Binding(d)])
    /// ```
    Slice(&'hir [Pat<'hir>], Option<&'hir Pat<'hir>>, &'hir [Pat<'hir>]),

    /// A placeholder for a pattern that wasn't well formed in some way.
    Err(ErrorGuaranteed),
}

/// A statement.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Stmt<'hir> {
    pub hir_id: HirId,
    pub kind: StmtKind<'hir>,
    pub span: Span,
}

/// The contents of a statement.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum StmtKind<'hir> {
    /// A local (`let`) binding.
    Let(&'hir Local<'hir>),

    /// An item binding.
    Item(ItemId),

    /// An expression without a trailing semi-colon (must have unit type).
    Expr(&'hir Expr<'hir>),

    /// An expression with a trailing semi-colon (may have any type).
    Semi(&'hir Expr<'hir>),
}

/// Represents a `let` statement (i.e., `let <pat>:<ty> = <init>;`).
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Local<'hir> {
    pub pat: &'hir Pat<'hir>,
    /// Type annotation, if any (otherwise the type will be inferred).
    pub ty: Option<&'hir Ty<'hir>>,
    /// Initializer expression to set the value, if any.
    pub init: Option<&'hir Expr<'hir>>,
    /// Else block for a `let...else` binding.
    pub els: Option<&'hir Block<'hir>>,
    pub hir_id: HirId,
    pub span: Span,
    /// Can be `ForLoopDesugar` if the `let` statement is part of a `for` loop
    /// desugaring. Otherwise will be `Normal`.
    pub source: LocalSource,
}

/// Represents a single arm of a `match` expression, e.g.
/// `<pat> (if <guard>) => <body>`.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Arm<'hir> {
    #[stable_hasher(ignore)]
    pub hir_id: HirId,
    pub span: Span,
    /// If this pattern and the optional guard matches, then `body` is evaluated.
    pub pat: &'hir Pat<'hir>,
    /// Optional guard clause.
    pub guard: Option<&'hir Expr<'hir>>,
    /// The expression the arm evaluates to if this arm matches.
    pub body: &'hir Expr<'hir>,
}

/// Represents a `let <pat>[: <ty>] = <expr>` expression (not a [`Local`]), occurring in an `if-let`
/// or `let-else`, evaluating to a boolean. Typically the pattern is refutable.
///
/// In an `if let`, imagine it as `if (let <pat> = <expr>) { ... }`; in a let-else, it is part of
/// the desugaring to if-let. Only let-else supports the type annotation at present.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Let<'hir> {
    pub span: Span,
    pub pat: &'hir Pat<'hir>,
    pub ty: Option<&'hir Ty<'hir>>,
    pub init: &'hir Expr<'hir>,
    /// `Some` when this let expressions is not in a syntanctically valid location.
    /// Used to prevent building MIR in such situations.
    pub is_recovered: Option<ErrorGuaranteed>,
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct ExprField<'hir> {
    #[stable_hasher(ignore)]
    pub hir_id: HirId,
    pub ident: Ident,
    pub expr: &'hir Expr<'hir>,
    pub span: Span,
    pub is_shorthand: bool,
}

#[derive(Copy, Clone, PartialEq, Debug, HashStable_Generic)]
pub enum BlockCheckMode {
    DefaultBlock,
    UnsafeBlock(UnsafeSource),
}

#[derive(Copy, Clone, PartialEq, Debug, HashStable_Generic)]
pub enum UnsafeSource {
    CompilerGenerated,
    UserProvided,
}

#[derive(Copy, Clone, PartialEq, Eq, Hash, Debug, HashStable_Generic)]
pub struct BodyId {
    pub hir_id: HirId,
}

/// The body of a function, closure, or constant value. In the case of
/// a function, the body contains not only the function body itself
/// (which is an expression), but also the argument patterns, since
/// those are something that the caller doesn't really care about.
///
/// # Examples
///
/// ```
/// fn foo((x, y): (u32, u32)) -> u32 {
///     x + y
/// }
/// ```
///
/// Here, the `Body` associated with `foo()` would contain:
///
/// - an `params` array containing the `(x, y)` pattern
/// - a `value` containing the `x + y` expression (maybe wrapped in a block)
/// - `coroutine_kind` would be `None`
///
/// All bodies have an **owner**, which can be accessed via the HIR
/// map using `body_owner_def_id()`.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Body<'hir> {
    pub params: &'hir [Param<'hir>],
    pub value: &'hir Expr<'hir>,
}

impl<'hir> Body<'hir> {
    pub fn id(&self) -> BodyId {
        BodyId { hir_id: self.value.hir_id }
    }
}

/// The type of source expression that caused this coroutine to be created.
#[derive(Clone, PartialEq, Eq, Debug, Copy, Hash, HashStable_Generic, Encodable, Decodable)]
pub enum CoroutineKind {
    /// A coroutine that comes from a desugaring.
    Desugared(CoroutineDesugaring, CoroutineSource),

    /// A coroutine literal created via a `yield` inside a closure.
    Coroutine(Movability),
}

impl CoroutineKind {
    pub fn movability(self) -> Movability {
        match self {
            CoroutineKind::Desugared(CoroutineDesugaring::Async, _)
            | CoroutineKind::Desugared(CoroutineDesugaring::AsyncGen, _) => Movability::Static,
            CoroutineKind::Desugared(CoroutineDesugaring::Gen, _) => Movability::Movable,
            CoroutineKind::Coroutine(mov) => mov,
        }
    }
}

impl CoroutineKind {
    pub fn is_fn_like(self) -> bool {
        matches!(self, CoroutineKind::Desugared(_, CoroutineSource::Fn))
    }
}

impl fmt::Display for CoroutineKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CoroutineKind::Desugared(d, k) => {
                d.fmt(f)?;
                k.fmt(f)
            }
            CoroutineKind::Coroutine(_) => f.write_str("coroutine"),
        }
    }
}

/// In the case of a coroutine created as part of an async/gen construct,
/// which kind of async/gen construct caused it to be created?
///
/// This helps error messages but is also used to drive coercions in
/// type-checking (see #60424).
#[derive(Clone, PartialEq, Eq, Hash, Debug, Copy, HashStable_Generic, Encodable, Decodable)]
pub enum CoroutineSource {
    /// An explicit `async`/`gen` block written by the user.
    Block,

    /// An explicit `async`/`gen` closure written by the user.
    Closure,

    /// The `async`/`gen` block generated as the body of an async/gen function.
    Fn,
}

impl fmt::Display for CoroutineSource {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CoroutineSource::Block => "block",
            CoroutineSource::Closure => "closure body",
            CoroutineSource::Fn => "fn body",
        }
        .fmt(f)
    }
}

#[derive(Clone, PartialEq, Eq, Debug, Copy, Hash, HashStable_Generic, Encodable, Decodable)]
pub enum CoroutineDesugaring {
    /// An explicit `async` block or the body of an `async` function.
    Async,

    /// An explicit `gen` block or the body of a `gen` function.
    Gen,

    /// An explicit `async gen` block or the body of an `async gen` function,
    /// which is able to both `yield` and `.await`.
    AsyncGen,
}

impl fmt::Display for CoroutineDesugaring {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CoroutineDesugaring::Async => {
                if f.alternate() {
                    f.write_str("`async` ")?;
                } else {
                    f.write_str("async ")?
                }
            }
            CoroutineDesugaring::Gen => {
                if f.alternate() {
                    f.write_str("`gen` ")?;
                } else {
                    f.write_str("gen ")?
                }
            }
            CoroutineDesugaring::AsyncGen => {
                if f.alternate() {
                    f.write_str("`async gen` ")?;
                } else {
                    f.write_str("async gen ")?
                }
            }
        }

        Ok(())
    }
}

#[derive(Copy, Clone, Debug)]
pub enum BodyOwnerKind {
    /// Functions and methods.
    Fn,

    /// Closures
    Closure,

    /// Constants and associated constants, also including inline constants.
    Const { inline: bool },

    /// Initializer of a `static` item.
    Static(Mutability),
}

impl BodyOwnerKind {
    pub fn is_fn_or_closure(self) -> bool {
        match self {
            BodyOwnerKind::Fn | BodyOwnerKind::Closure => true,
            BodyOwnerKind::Const { .. } | BodyOwnerKind::Static(_) => false,
        }
    }
}

/// The kind of an item that requires const-checking.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConstContext {
    /// A `const fn`.
    ConstFn,

    /// A `static` or `static mut`.
    Static(Mutability),

    /// A `const`, associated `const`, or other const context.
    ///
    /// Other contexts include:
    /// - Array length expressions
    /// - Enum discriminants
    /// - Const generics
    ///
    /// For the most part, other contexts are treated just like a regular `const`, so they are
    /// lumped into the same category.
    Const { inline: bool },
}

impl ConstContext {
    /// A description of this const context that can appear between backticks in an error message.
    ///
    /// E.g. `const` or `static mut`.
    pub fn keyword_name(self) -> &'static str {
        match self {
            Self::Const { .. } => "const",
            Self::Static(Mutability::Not) => "static",
            Self::Static(Mutability::Mut) => "static mut",
            Self::ConstFn => "const fn",
        }
    }
}

/// A colloquial, trivially pluralizable description of this const context for use in error
/// messages.
impl fmt::Display for ConstContext {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match *self {
            Self::Const { .. } => write!(f, "constant"),
            Self::Static(_) => write!(f, "static"),
            Self::ConstFn => write!(f, "constant function"),
        }
    }
}

// NOTE: `IntoDiagArg` impl for `ConstContext` lives in `rustc_errors`
// due to a cyclical dependency between hir that crate.

/// A literal.
pub type Lit = Spanned<LitKind>;

#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub enum ArrayLen {
    Infer(InferArg),
    Body(AnonConst),
}

impl ArrayLen {
    pub fn hir_id(&self) -> HirId {
        match self {
            ArrayLen::Infer(InferArg { hir_id, .. }) | ArrayLen::Body(AnonConst { hir_id, .. }) => {
                *hir_id
            }
        }
    }
}

/// A constant (expression) that's not an item or associated item,
/// but needs its own `DefId` for type-checking, const-eval, etc.
/// These are usually found nested inside types (e.g., array lengths)
/// or expressions (e.g., repeat counts), and also used to define
/// explicit discriminant values for enum variants.
///
/// You can check if this anon const is a default in a const param
/// `const N: usize = { ... }` with `tcx.hir().opt_const_param_default_param_def_id(..)`
#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub struct AnonConst {
    pub hir_id: HirId,
    pub def_id: LocalDefId,
    pub body: BodyId,
}

/// An inline constant expression `const { something }`.
#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub struct ConstBlock {
    pub hir_id: HirId,
    pub def_id: LocalDefId,
    pub body: BodyId,
}

/// An expression.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Expr<'hir> {
    pub hir_id: HirId,
    pub kind: ExprKind<'hir>,
    pub span: Span,
}

impl Expr<'_> {
    pub fn precedence(&self) -> ExprPrecedence {
        match self.kind {
            ExprKind::ConstBlock(_) => ExprPrecedence::ConstBlock,
            ExprKind::Array(_) => ExprPrecedence::Array,
            ExprKind::Call(..) => ExprPrecedence::Call,
            ExprKind::MethodCall(..) => ExprPrecedence::MethodCall,
            ExprKind::Tup(_) => ExprPrecedence::Tup,
            ExprKind::Binary(op, ..) => ExprPrecedence::Binary(op.node),
            ExprKind::Unary(..) => ExprPrecedence::Unary,
            ExprKind::Lit(_) => ExprPrecedence::Lit,
            ExprKind::Type(..) | ExprKind::Cast(..) => ExprPrecedence::Cast,
            ExprKind::DropTemps(ref expr, ..) => expr.precedence(),
            ExprKind::If(..) => ExprPrecedence::If,
            ExprKind::Let(..) => ExprPrecedence::Let,
            ExprKind::Loop(..) => ExprPrecedence::Loop,
            ExprKind::Match(..) => ExprPrecedence::Match,
            ExprKind::Closure { .. } => ExprPrecedence::Closure,
            ExprKind::Block(..) => ExprPrecedence::Block,
            ExprKind::Assign(..) => ExprPrecedence::Assign,
            ExprKind::AssignOp(..) => ExprPrecedence::AssignOp,
            ExprKind::Field(..) => ExprPrecedence::Field,
            ExprKind::Index(..) => ExprPrecedence::Index,
            ExprKind::Path(..) => ExprPrecedence::Path,
            ExprKind::AddrOf(..) => ExprPrecedence::AddrOf,
            ExprKind::Break(..) => ExprPrecedence::Break,
            ExprKind::Continue(..) => ExprPrecedence::Continue,
            ExprKind::Ret(..) => ExprPrecedence::Ret,
            ExprKind::Become(..) => ExprPrecedence::Become,
            ExprKind::InlineAsm(..) => ExprPrecedence::InlineAsm,
            ExprKind::OffsetOf(..) => ExprPrecedence::OffsetOf,
            ExprKind::Struct(..) => ExprPrecedence::Struct,
            ExprKind::Repeat(..) => ExprPrecedence::Repeat,
            ExprKind::Yield(..) => ExprPrecedence::Yield,
            ExprKind::Err(_) => ExprPrecedence::Err,
        }
    }

    /// Whether this looks like a place expr, without checking for deref
    /// adjustments.
    /// This will return `true` in some potentially surprising cases such as
    /// `CONSTANT.field`.
    pub fn is_syntactic_place_expr(&self) -> bool {
        self.is_place_expr(|_| true)
    }

    /// Whether this is a place expression.
    ///
    /// `allow_projections_from` should return `true` if indexing a field or index expression based
    /// on the given expression should be considered a place expression.
    pub fn is_place_expr(&self, mut allow_projections_from: impl FnMut(&Self) -> bool) -> bool {
        match self.kind {
            ExprKind::Path(QPath::Resolved(_, ref path)) => {
                matches!(path.res, Res::Local(..) | Res::Def(DefKind::Static { .. }, _) | Res::Err)
            }

            // Type ascription inherits its place expression kind from its
            // operand. See:
            // https://github.com/rust-lang/rfcs/blob/master/text/0803-type-ascription.md#type-ascription-and-temporaries
            ExprKind::Type(ref e, _) => e.is_place_expr(allow_projections_from),

            ExprKind::Unary(UnOp::Deref, _) => true,

            ExprKind::Field(ref base, _) | ExprKind::Index(ref base, _, _) => {
                allow_projections_from(base) || base.is_place_expr(allow_projections_from)
            }

            // Lang item paths cannot currently be local variables or statics.
            ExprKind::Path(QPath::LangItem(..)) => false,

            // Partially qualified paths in expressions can only legally
            // refer to associated items which are always rvalues.
            ExprKind::Path(QPath::TypeRelative(..))
            | ExprKind::Call(..)
            | ExprKind::MethodCall(..)
            | ExprKind::Struct(..)
            | ExprKind::Tup(..)
            | ExprKind::If(..)
            | ExprKind::Match(..)
            | ExprKind::Closure { .. }
            | ExprKind::Block(..)
            | ExprKind::Repeat(..)
            | ExprKind::Array(..)
            | ExprKind::Break(..)
            | ExprKind::Continue(..)
            | ExprKind::Ret(..)
            | ExprKind::Become(..)
            | ExprKind::Let(..)
            | ExprKind::Loop(..)
            | ExprKind::Assign(..)
            | ExprKind::InlineAsm(..)
            | ExprKind::OffsetOf(..)
            | ExprKind::AssignOp(..)
            | ExprKind::Lit(_)
            | ExprKind::ConstBlock(..)
            | ExprKind::Unary(..)
            | ExprKind::AddrOf(..)
            | ExprKind::Binary(..)
            | ExprKind::Yield(..)
            | ExprKind::Cast(..)
            | ExprKind::DropTemps(..)
            | ExprKind::Err(_) => false,
        }
    }

    /// If `Self.kind` is `ExprKind::DropTemps(expr)`, drill down until we get a non-`DropTemps`
    /// `Expr`. This is used in suggestions to ignore this `ExprKind` as it is semantically
    /// silent, only signaling the ownership system. By doing this, suggestions that check the
    /// `ExprKind` of any given `Expr` for presentation don't have to care about `DropTemps`
    /// beyond remembering to call this function before doing analysis on it.
    pub fn peel_drop_temps(&self) -> &Self {
        let mut expr = self;
        while let ExprKind::DropTemps(inner) = &expr.kind {
            expr = inner;
        }
        expr
    }

    pub fn peel_blocks(&self) -> &Self {
        let mut expr = self;
        while let ExprKind::Block(Block { expr: Some(inner), .. }, _) = &expr.kind {
            expr = inner;
        }
        expr
    }

    pub fn peel_borrows(&self) -> &Self {
        let mut expr = self;
        while let ExprKind::AddrOf(.., inner) = &expr.kind {
            expr = inner;
        }
        expr
    }

    pub fn can_have_side_effects(&self) -> bool {
        match self.peel_drop_temps().kind {
            ExprKind::Path(_) | ExprKind::Lit(_) | ExprKind::OffsetOf(..) => false,
            ExprKind::Type(base, _)
            | ExprKind::Unary(_, base)
            | ExprKind::Field(base, _)
            | ExprKind::Index(base, _, _)
            | ExprKind::AddrOf(.., base)
            | ExprKind::Cast(base, _) => {
                // This isn't exactly true for `Index` and all `Unary`, but we are using this
                // method exclusively for diagnostics and there's a *cultural* pressure against
                // them being used only for its side-effects.
                base.can_have_side_effects()
            }
            ExprKind::Struct(_, fields, init) => {
                fields.iter().map(|field| field.expr).chain(init).any(|e| e.can_have_side_effects())
            }

            ExprKind::Array(args)
            | ExprKind::Tup(args)
            | ExprKind::Call(
                Expr {
                    kind:
                        ExprKind::Path(QPath::Resolved(
                            None,
                            Path { res: Res::Def(DefKind::Ctor(_, CtorKind::Fn), _), .. },
                        )),
                    ..
                },
                args,
            ) => args.iter().any(|arg| arg.can_have_side_effects()),
            ExprKind::If(..)
            | ExprKind::Match(..)
            | ExprKind::MethodCall(..)
            | ExprKind::Call(..)
            | ExprKind::Closure { .. }
            | ExprKind::Block(..)
            | ExprKind::Repeat(..)
            | ExprKind::Break(..)
            | ExprKind::Continue(..)
            | ExprKind::Ret(..)
            | ExprKind::Become(..)
            | ExprKind::Let(..)
            | ExprKind::Loop(..)
            | ExprKind::Assign(..)
            | ExprKind::InlineAsm(..)
            | ExprKind::AssignOp(..)
            | ExprKind::ConstBlock(..)
            | ExprKind::Binary(..)
            | ExprKind::Yield(..)
            | ExprKind::DropTemps(..)
            | ExprKind::Err(_) => true,
        }
    }

    /// To a first-order approximation, is this a pattern?
    pub fn is_approximately_pattern(&self) -> bool {
        match &self.kind {
            ExprKind::Array(_)
            | ExprKind::Call(..)
            | ExprKind::Tup(_)
            | ExprKind::Lit(_)
            | ExprKind::Path(_)
            | ExprKind::Struct(..) => true,
            _ => false,
        }
    }

    pub fn method_ident(&self) -> Option<Ident> {
        match self.kind {
            ExprKind::MethodCall(receiver_method, ..) => Some(receiver_method.ident),
            ExprKind::Unary(_, expr) | ExprKind::AddrOf(.., expr) => expr.method_ident(),
            _ => None,
        }
    }
}

/// Checks if the specified expression is a built-in range literal.
/// (See: `LoweringContext::lower_expr()`).
pub fn is_range_literal(expr: &Expr<'_>) -> bool {
    match expr.kind {
        // All built-in range literals but `..=` and `..` desugar to `Struct`s.
        ExprKind::Struct(ref qpath, _, _) => matches!(
            **qpath,
            QPath::LangItem(
                LangItem::Range
                    | LangItem::RangeTo
                    | LangItem::RangeFrom
                    | LangItem::RangeFull
                    | LangItem::RangeToInclusive,
                ..
            )
        ),

        // `..=` desugars into `::std::ops::RangeInclusive::new(...)`.
        ExprKind::Call(ref func, _) => {
            matches!(func.kind, ExprKind::Path(QPath::LangItem(LangItem::RangeInclusiveNew, ..)))
        }

        _ => false,
    }
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum ExprKind<'hir> {
    /// Allow anonymous constants from an inline `const` block
    ConstBlock(ConstBlock),
    /// An array (e.g., `[a, b, c, d]`).
    Array(&'hir [Expr<'hir>]),
    /// A function call.
    ///
    /// The first field resolves to the function itself (usually an `ExprKind::Path`),
    /// and the second field is the list of arguments.
    /// This also represents calling the constructor of
    /// tuple-like ADTs such as tuple structs and enum variants.
    Call(&'hir Expr<'hir>, &'hir [Expr<'hir>]),
    /// A method call (e.g., `x.foo::<'static, Bar, Baz>(a, b, c, d)`).
    ///
    /// The `PathSegment` represents the method name and its generic arguments
    /// (within the angle brackets).
    /// The `&Expr` is the expression that evaluates
    /// to the object on which the method is being called on (the receiver),
    /// and the `&[Expr]` is the rest of the arguments.
    /// Thus, `x.foo::<Bar, Baz>(a, b, c, d)` is represented as
    /// `ExprKind::MethodCall(PathSegment { foo, [Bar, Baz] }, x, [a, b, c, d], span)`.
    /// The final `Span` represents the span of the function and arguments
    /// (e.g. `foo::<Bar, Baz>(a, b, c, d)` in `x.foo::<Bar, Baz>(a, b, c, d)`
    ///
    /// To resolve the called method to a `DefId`, call [`type_dependent_def_id`] with
    /// the `hir_id` of the `MethodCall` node itself.
    ///
    /// [`type_dependent_def_id`]: ../../rustc_middle/ty/struct.TypeckResults.html#method.type_dependent_def_id
    MethodCall(&'hir PathSegment<'hir>, &'hir Expr<'hir>, &'hir [Expr<'hir>], Span),
    /// A tuple (e.g., `(a, b, c, d)`).
    Tup(&'hir [Expr<'hir>]),
    /// A binary operation (e.g., `a + b`, `a * b`).
    Binary(BinOp, &'hir Expr<'hir>, &'hir Expr<'hir>),
    /// A unary operation (e.g., `!x`, `*x`).
    Unary(UnOp, &'hir Expr<'hir>),
    /// A literal (e.g., `1`, `"foo"`).
    Lit(&'hir Lit),
    /// A cast (e.g., `foo as f64`).
    Cast(&'hir Expr<'hir>, &'hir Ty<'hir>),
    /// A type ascription (e.g., `x: Foo`). See RFC 3307.
    Type(&'hir Expr<'hir>, &'hir Ty<'hir>),
    /// Wraps the expression in a terminating scope.
    /// This makes it semantically equivalent to `{ let _t = expr; _t }`.
    ///
    /// This construct only exists to tweak the drop order in HIR lowering.
    /// An example of that is the desugaring of `for` loops.
    DropTemps(&'hir Expr<'hir>),
    /// A `let $pat = $expr` expression.
    ///
    /// These are not `Local` and only occur as expressions.
    /// The `let Some(x) = foo()` in `if let Some(x) = foo()` is an example of `Let(..)`.
    Let(&'hir Let<'hir>),
    /// An `if` block, with an optional else block.
    ///
    /// I.e., `if <expr> { <expr> } else { <expr> }`.
    If(&'hir Expr<'hir>, &'hir Expr<'hir>, Option<&'hir Expr<'hir>>),
    /// A conditionless loop (can be exited with `break`, `continue`, or `return`).
    ///
    /// I.e., `'label: loop { <block> }`.
    ///
    /// The `Span` is the loop header (`for x in y`/`while let pat = expr`).
    Loop(&'hir Block<'hir>, Option<Label>, LoopSource, Span),
    /// A `match` block, with a source that indicates whether or not it is
    /// the result of a desugaring, and if so, which kind.
    Match(&'hir Expr<'hir>, &'hir [Arm<'hir>], MatchSource),
    /// A closure (e.g., `move |a, b, c| {a + b + c}`).
    ///
    /// The `Span` is the argument block `|...|`.
    ///
    /// This may also be a coroutine literal or an `async block` as indicated by the
    /// `Option<Movability>`.
    Closure(&'hir Closure<'hir>),
    /// A block (e.g., `'label: { ... }`).
    Block(&'hir Block<'hir>, Option<Label>),

    /// An assignment (e.g., `a = foo()`).
    Assign(&'hir Expr<'hir>, &'hir Expr<'hir>, Span),
    /// An assignment with an operator.
    ///
    /// E.g., `a += 1`.
    AssignOp(BinOp, &'hir Expr<'hir>, &'hir Expr<'hir>),
    /// Access of a named (e.g., `obj.foo`) or unnamed (e.g., `obj.0`) struct or tuple field.
    Field(&'hir Expr<'hir>, Ident),
    /// An indexing operation (`foo[2]`).
    /// Similar to [`ExprKind::MethodCall`], the final `Span` represents the span of the brackets
    /// and index.
    Index(&'hir Expr<'hir>, &'hir Expr<'hir>, Span),

    /// Path to a definition, possibly containing lifetime or type parameters.
    Path(QPath<'hir>),

    /// A referencing operation (i.e., `&a` or `&mut a`).
    AddrOf(BorrowKind, Mutability, &'hir Expr<'hir>),
    /// A `break`, with an optional label to break.
    Break(Destination, Option<&'hir Expr<'hir>>),
    /// A `continue`, with an optional label.
    Continue(Destination),
    /// A `return`, with an optional value to be returned.
    Ret(Option<&'hir Expr<'hir>>),
    /// A `become`, with the value to be returned.
    Become(&'hir Expr<'hir>),

    /// Inline assembly (from `asm!`), with its outputs and inputs.
    InlineAsm(&'hir InlineAsm<'hir>),

    /// Field offset (`offset_of!`)
    OffsetOf(&'hir Ty<'hir>, &'hir [Ident]),

    /// A struct or struct-like variant literal expression.
    ///
    /// E.g., `Foo {x: 1, y: 2}`, or `Foo {x: 1, .. base}`,
    /// where `base` is the `Option<Expr>`.
    Struct(&'hir QPath<'hir>, &'hir [ExprField<'hir>], Option<&'hir Expr<'hir>>),

    /// An array literal constructed from one repeated element.
    ///
    /// E.g., `[1; 5]`. The first expression is the element
    /// to be repeated; the second is the number of times to repeat it.
    Repeat(&'hir Expr<'hir>, ArrayLen),

    /// A suspension point for coroutines (i.e., `yield <expr>`).
    Yield(&'hir Expr<'hir>, YieldSource),

    /// A placeholder for an expression that wasn't syntactically well formed in some way.
    Err(rustc_span::ErrorGuaranteed),
}

/// Represents an optionally `Self`-qualified value/type path or associated extension.
///
/// To resolve the path to a `DefId`, call [`qpath_res`].
///
/// [`qpath_res`]: ../../rustc_middle/ty/struct.TypeckResults.html#method.qpath_res
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum QPath<'hir> {
    /// Path to a definition, optionally "fully-qualified" with a `Self`
    /// type, if the path points to an associated item in a trait.
    ///
    /// E.g., an unqualified path like `Clone::clone` has `None` for `Self`,
    /// while `<Vec<T> as Clone>::clone` has `Some(Vec<T>)` for `Self`,
    /// even though they both have the same two-segment `Clone::clone` `Path`.
    Resolved(Option<&'hir Ty<'hir>>, &'hir Path<'hir>),

    /// Type-related paths (e.g., `<T>::default` or `<T>::Output`).
    /// Will be resolved by type-checking to an associated item.
    ///
    /// UFCS source paths can desugar into this, with `Vec::new` turning into
    /// `<Vec>::new`, and `T::X::Y::method` into `<<<T>::X>::Y>::method`,
    /// the `X` and `Y` nodes each being a `TyKind::Path(QPath::TypeRelative(..))`.
    TypeRelative(&'hir Ty<'hir>, &'hir PathSegment<'hir>),

    /// Reference to a `#[lang = "foo"]` item.
    LangItem(LangItem, Span),
}

impl<'hir> QPath<'hir> {
    /// Returns the span of this `QPath`.
    pub fn span(&self) -> Span {
        match *self {
            QPath::Resolved(_, path) => path.span,
            QPath::TypeRelative(qself, ps) => qself.span.to(ps.ident.span),
            QPath::LangItem(_, span) => span,
        }
    }

    /// Returns the span of the qself of this `QPath`. For example, `()` in
    /// `<() as Trait>::method`.
    pub fn qself_span(&self) -> Span {
        match *self {
            QPath::Resolved(_, path) => path.span,
            QPath::TypeRelative(qself, _) => qself.span,
            QPath::LangItem(_, span) => span,
        }
    }
}

/// Hints at the original code for a let statement.
#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub enum LocalSource {
    /// A `match _ { .. }`.
    Normal,
    /// When lowering async functions, we create locals within the `async move` so that
    /// all parameters are dropped after the future is polled.
    ///
    /// ```ignore (pseudo-Rust)
    /// async fn foo(<pattern> @ x: Type) {
    ///     async move {
    ///         let <pattern> = x;
    ///     }
    /// }
    /// ```
    AsyncFn,
    /// A desugared `<expr>.await`.
    AwaitDesugar,
    /// A desugared `expr = expr`, where the LHS is a tuple, struct, array or underscore expression.
    /// The span is that of the `=` sign.
    AssignDesugar(Span),
}

/// Hints at the original code for a `match _ { .. }`.
#[derive(Copy, Clone, PartialEq, Eq, Hash, Debug, HashStable_Generic, Encodable, Decodable)]
pub enum MatchSource {
    /// A `match _ { .. }`.
    Normal,
    /// A desugared `for _ in _ { .. }` loop.
    ForLoopDesugar,
    /// A desugared `?` operator.
    TryDesugar(HirId),
    /// A desugared `<expr>.await`.
    AwaitDesugar,
    /// A desugared `format_args!()`.
    FormatArgs,
}

impl MatchSource {
    #[inline]
    pub const fn name(self) -> &'static str {
        use MatchSource::*;
        match self {
            Normal => "match",
            ForLoopDesugar => "for",
            TryDesugar(_) => "?",
            AwaitDesugar => ".await",
            FormatArgs => "format_args!()",
        }
    }
}

/// The loop type that yielded an `ExprKind::Loop`.
#[derive(Copy, Clone, PartialEq, Debug, HashStable_Generic)]
pub enum LoopSource {
    /// A `loop { .. }` loop.
    Loop,
    /// A `while _ { .. }` loop.
    While,
    /// A `for _ in _ { .. }` loop.
    ForLoop,
}

impl LoopSource {
    pub fn name(self) -> &'static str {
        match self {
            LoopSource::Loop => "loop",
            LoopSource::While => "while",
            LoopSource::ForLoop => "for",
        }
    }
}

#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub enum LoopIdError {
    OutsideLoopScope,
    UnlabeledCfInWhileCondition,
    UnresolvedLabel,
}

impl fmt::Display for LoopIdError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            LoopIdError::OutsideLoopScope => "not inside loop scope",
            LoopIdError::UnlabeledCfInWhileCondition => {
                "unlabeled control flow (break or continue) in while condition"
            }
            LoopIdError::UnresolvedLabel => "label not found",
        })
    }
}

#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub struct Destination {
    /// This is `Some(_)` iff there is an explicit user-specified 'label
    pub label: Option<Label>,

    /// These errors are caught and then reported during the diagnostics pass in
    /// `librustc_passes/loops.rs`
    pub target_id: Result<HirId, LoopIdError>,
}

/// The yield kind that caused an `ExprKind::Yield`.
#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub enum YieldSource {
    /// An `<expr>.await`.
    Await { expr: Option<HirId> },
    /// A plain `yield`.
    Yield,
}

impl fmt::Display for YieldSource {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            YieldSource::Await { .. } => "`await`",
            YieldSource::Yield => "`yield`",
        })
    }
}

// N.B., if you change this, you'll probably want to change the corresponding
// type structure in middle/ty.rs as well.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct MutTy<'hir> {
    pub ty: &'hir Ty<'hir>,
    pub mutbl: Mutability,
}

/// Represents a function's signature in a trait declaration,
/// trait implementation, or a free function.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct FnSig<'hir> {
    pub header: FnHeader,
    pub decl: &'hir FnDecl<'hir>,
    pub span: Span,
}

// The bodies for items are stored "out of line", in a separate
// hashmap in the `Crate`. Here we just record the hir-id of the item
// so it can fetched later.
#[derive(Copy, Clone, PartialEq, Eq, Encodable, Decodable, Debug, HashStable_Generic)]
pub struct TraitItemId {
    pub owner_id: OwnerId,
}

impl TraitItemId {
    #[inline]
    pub fn hir_id(&self) -> HirId {
        // Items are always HIR owners.
        HirId::make_owner(self.owner_id.def_id)
    }
}

/// Represents an item declaration within a trait declaration,
/// possibly including a default implementation. A trait item is
/// either required (meaning it doesn't have an implementation, just a
/// signature) or provided (meaning it has a default implementation).
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct TraitItem<'hir> {
    pub ident: Ident,
    pub owner_id: OwnerId,
    pub generics: &'hir Generics<'hir>,
    pub kind: TraitItemKind<'hir>,
    pub span: Span,
    pub defaultness: Defaultness,
}

macro_rules! expect_methods_self_kind {
    ( $( $name:ident, $ret_ty:ty, $pat:pat, $ret_val:expr; )* ) => {
        $(
            #[track_caller]
            pub fn $name(&self) -> $ret_ty {
                let $pat = &self.kind else { expect_failed(stringify!($ident), self) };
                $ret_val
            }
        )*
    }
}

macro_rules! expect_methods_self {
    ( $( $name:ident, $ret_ty:ty, $pat:pat, $ret_val:expr; )* ) => {
        $(
            #[track_caller]
            pub fn $name(&self) -> $ret_ty {
                let $pat = self else { expect_failed(stringify!($ident), self) };
                $ret_val
            }
        )*
    }
}

#[track_caller]
fn expect_failed<T: fmt::Debug>(ident: &'static str, found: T) -> ! {
    panic!("{ident}: found {found:?}")
}

impl<'hir> TraitItem<'hir> {
    #[inline]
    pub fn hir_id(&self) -> HirId {
        // Items are always HIR owners.
        HirId::make_owner(self.owner_id.def_id)
    }

    pub fn trait_item_id(&self) -> TraitItemId {
        TraitItemId { owner_id: self.owner_id }
    }

    expect_methods_self_kind! {
        expect_const, (&'hir Ty<'hir>, Option<BodyId>),
            TraitItemKind::Const(ty, body), (ty, *body);

        expect_fn, (&FnSig<'hir>, &TraitFn<'hir>),
            TraitItemKind::Fn(ty, trfn), (ty, trfn);

        expect_type, (GenericBounds<'hir>, Option<&'hir Ty<'hir>>),
            TraitItemKind::Type(bounds, ty), (bounds, *ty);
    }
}

/// Represents a trait method's body (or just argument names).
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum TraitFn<'hir> {
    /// No default body in the trait, just a signature.
    Required(&'hir [Ident]),

    /// Both signature and body are provided in the trait.
    Provided(BodyId),
}

/// Represents a trait method or associated constant or type
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum TraitItemKind<'hir> {
    /// An associated constant with an optional value (otherwise `impl`s must contain a value).
    Const(&'hir Ty<'hir>, Option<BodyId>),
    /// An associated function with an optional body.
    Fn(FnSig<'hir>, TraitFn<'hir>),
    /// An associated type with (possibly empty) bounds and optional concrete
    /// type.
    Type(GenericBounds<'hir>, Option<&'hir Ty<'hir>>),
}

// The bodies for items are stored "out of line", in a separate
// hashmap in the `Crate`. Here we just record the hir-id of the item
// so it can fetched later.
#[derive(Copy, Clone, PartialEq, Eq, Encodable, Decodable, Debug, HashStable_Generic)]
pub struct ImplItemId {
    pub owner_id: OwnerId,
}

impl ImplItemId {
    #[inline]
    pub fn hir_id(&self) -> HirId {
        // Items are always HIR owners.
        HirId::make_owner(self.owner_id.def_id)
    }
}

/// Represents anything within an `impl` block.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct ImplItem<'hir> {
    pub ident: Ident,
    pub owner_id: OwnerId,
    pub generics: &'hir Generics<'hir>,
    pub kind: ImplItemKind<'hir>,
    pub defaultness: Defaultness,
    pub span: Span,
    pub vis_span: Span,
}

impl<'hir> ImplItem<'hir> {
    #[inline]
    pub fn hir_id(&self) -> HirId {
        // Items are always HIR owners.
        HirId::make_owner(self.owner_id.def_id)
    }

    pub fn impl_item_id(&self) -> ImplItemId {
        ImplItemId { owner_id: self.owner_id }
    }

    expect_methods_self_kind! {
        expect_const, (&'hir Ty<'hir>, BodyId), ImplItemKind::Const(ty, body), (ty, *body);
        expect_fn,    (&FnSig<'hir>, BodyId),   ImplItemKind::Fn(ty, body),    (ty, *body);
        expect_type,  &'hir Ty<'hir>,           ImplItemKind::Type(ty),        ty;
    }
}

/// Represents various kinds of content within an `impl`.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum ImplItemKind<'hir> {
    /// An associated constant of the given type, set to the constant result
    /// of the expression.
    Const(&'hir Ty<'hir>, BodyId),
    /// An associated function implementation with the given signature and body.
    Fn(FnSig<'hir>, BodyId),
    /// An associated type.
    Type(&'hir Ty<'hir>),
}

/// Bind a type to an associated type (i.e., `A = Foo`).
///
/// Bindings like `A: Debug` are represented as a special type `A =
/// $::Debug` that is understood by the astconv code.
///
/// FIXME(alexreg): why have a separate type for the binding case,
/// wouldn't it be better to make the `ty` field an enum like the
/// following?
///
/// ```ignore (pseudo-rust)
/// enum TypeBindingKind {
///    Equals(...),
///    Binding(...),
/// }
/// ```
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct TypeBinding<'hir> {
    pub hir_id: HirId,
    pub ident: Ident,
    pub gen_args: &'hir GenericArgs<'hir>,
    pub kind: TypeBindingKind<'hir>,
    pub span: Span,
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum Term<'hir> {
    Ty(&'hir Ty<'hir>),
    Const(AnonConst),
}

impl<'hir> From<&'hir Ty<'hir>> for Term<'hir> {
    fn from(ty: &'hir Ty<'hir>) -> Self {
        Term::Ty(ty)
    }
}

impl<'hir> From<AnonConst> for Term<'hir> {
    fn from(c: AnonConst) -> Self {
        Term::Const(c)
    }
}

// Represents the two kinds of type bindings.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum TypeBindingKind<'hir> {
    /// E.g., `Foo<Bar: Send>`.
    Constraint { bounds: &'hir [GenericBound<'hir>] },
    /// E.g., `Foo<Bar = ()>`, `Foo<Bar = ()>`
    Equality { term: Term<'hir> },
}

impl TypeBinding<'_> {
    pub fn ty(&self) -> &Ty<'_> {
        match self.kind {
            TypeBindingKind::Equality { term: Term::Ty(ref ty) } => ty,
            _ => panic!("expected equality type binding for parenthesized generic args"),
        }
    }
    pub fn opt_const(&self) -> Option<&'_ AnonConst> {
        match self.kind {
            TypeBindingKind::Equality { term: Term::Const(ref c) } => Some(c),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Ty<'hir> {
    pub hir_id: HirId,
    pub kind: TyKind<'hir>,
    pub span: Span,
}

impl<'hir> Ty<'hir> {
    /// Returns `true` if `param_def_id` matches the `bounded_ty` of this predicate.
    pub fn as_generic_param(&self) -> Option<(DefId, Ident)> {
        let TyKind::Path(QPath::Resolved(None, path)) = self.kind else {
            return None;
        };
        let [segment] = &path.segments else {
            return None;
        };
        match path.res {
            Res::Def(DefKind::TyParam, def_id) | Res::SelfTyParam { trait_: def_id } => {
                Some((def_id, segment.ident))
            }
            _ => None,
        }
    }

    pub fn peel_refs(&self) -> &Self {
        let mut final_ty = self;
        while let TyKind::Ref(_, MutTy { ty, .. }) = &final_ty.kind {
            final_ty = ty;
        }
        final_ty
    }

    pub fn find_self_aliases(&self) -> Vec<Span> {
        use crate::intravisit::Visitor;
        struct MyVisitor(Vec<Span>);
        impl<'v> Visitor<'v> for MyVisitor {
            fn visit_ty(&mut self, t: &'v Ty<'v>) {
                if matches!(
                    &t.kind,
                    TyKind::Path(QPath::Resolved(
                        _,
                        Path { res: crate::def::Res::SelfTyAlias { .. }, .. },
                    ))
                ) {
                    self.0.push(t.span);
                    return;
                }
                crate::intravisit::walk_ty(self, t);
            }
        }

        let mut my_visitor = MyVisitor(vec![]);
        my_visitor.visit_ty(self);
        my_visitor.0
    }

    /// Whether `ty` is a type with `_` placeholders that can be inferred. Used in diagnostics only to
    /// use inference to provide suggestions for the appropriate type if possible.
    pub fn is_suggestable_infer_ty(&self) -> bool {
        fn are_suggestable_generic_args(generic_args: &[GenericArg<'_>]) -> bool {
            generic_args.iter().any(|arg| match arg {
                GenericArg::Type(ty) => ty.is_suggestable_infer_ty(),
                GenericArg::Infer(_) => true,
                _ => false,
            })
        }
        debug!(?self);
        match &self.kind {
            TyKind::Infer => true,
            TyKind::Slice(ty) => ty.is_suggestable_infer_ty(),
            TyKind::Array(ty, length) => {
                ty.is_suggestable_infer_ty() || matches!(length, ArrayLen::Infer(..))
            }
            TyKind::Tup(tys) => tys.iter().any(Self::is_suggestable_infer_ty),
            TyKind::Ptr(mut_ty) | TyKind::Ref(_, mut_ty) => mut_ty.ty.is_suggestable_infer_ty(),
            TyKind::OpaqueDef(_, generic_args, _) => are_suggestable_generic_args(generic_args),
            TyKind::Path(QPath::TypeRelative(ty, segment)) => {
                ty.is_suggestable_infer_ty() || are_suggestable_generic_args(segment.args().args)
            }
            TyKind::Path(QPath::Resolved(ty_opt, Path { segments, .. })) => {
                ty_opt.is_some_and(Self::is_suggestable_infer_ty)
                    || segments
                        .iter()
                        .any(|segment| are_suggestable_generic_args(segment.args().args))
            }
            _ => false,
        }
    }
}

/// Not represented directly in the AST; referred to by name through a `ty_path`.
#[derive(Copy, Clone, PartialEq, Eq, Encodable, Decodable, Hash, Debug, HashStable_Generic)]
pub enum PrimTy {
    Int(IntTy),
    Uint(UintTy),
    Float(FloatTy),
    Str,
    Bool,
    Char,
}

impl PrimTy {
    /// All of the primitive types
    pub const ALL: [Self; 19] = [
        // any changes here should also be reflected in `PrimTy::from_name`
        Self::Int(IntTy::I8),
        Self::Int(IntTy::I16),
        Self::Int(IntTy::I32),
        Self::Int(IntTy::I64),
        Self::Int(IntTy::I128),
        Self::Int(IntTy::Isize),
        Self::Uint(UintTy::U8),
        Self::Uint(UintTy::U16),
        Self::Uint(UintTy::U32),
        Self::Uint(UintTy::U64),
        Self::Uint(UintTy::U128),
        Self::Uint(UintTy::Usize),
        Self::Float(FloatTy::F16),
        Self::Float(FloatTy::F32),
        Self::Float(FloatTy::F64),
        Self::Float(FloatTy::F128),
        Self::Bool,
        Self::Char,
        Self::Str,
    ];

    /// Like [`PrimTy::name`], but returns a &str instead of a symbol.
    ///
    /// Used by clippy.
    pub fn name_str(self) -> &'static str {
        match self {
            PrimTy::Int(i) => i.name_str(),
            PrimTy::Uint(u) => u.name_str(),
            PrimTy::Float(f) => f.name_str(),
            PrimTy::Str => "str",
            PrimTy::Bool => "bool",
            PrimTy::Char => "char",
        }
    }

    pub fn name(self) -> Symbol {
        match self {
            PrimTy::Int(i) => i.name(),
            PrimTy::Uint(u) => u.name(),
            PrimTy::Float(f) => f.name(),
            PrimTy::Str => sym::str,
            PrimTy::Bool => sym::bool,
            PrimTy::Char => sym::char,
        }
    }

    /// Returns the matching `PrimTy` for a `Symbol` such as "str" or "i32".
    /// Returns `None` if no matching type is found.
    pub fn from_name(name: Symbol) -> Option<Self> {
        let ty = match name {
            // any changes here should also be reflected in `PrimTy::ALL`
            sym::i8 => Self::Int(IntTy::I8),
            sym::i16 => Self::Int(IntTy::I16),
            sym::i32 => Self::Int(IntTy::I32),
            sym::i64 => Self::Int(IntTy::I64),
            sym::i128 => Self::Int(IntTy::I128),
            sym::isize => Self::Int(IntTy::Isize),
            sym::u8 => Self::Uint(UintTy::U8),
            sym::u16 => Self::Uint(UintTy::U16),
            sym::u32 => Self::Uint(UintTy::U32),
            sym::u64 => Self::Uint(UintTy::U64),
            sym::u128 => Self::Uint(UintTy::U128),
            sym::usize => Self::Uint(UintTy::Usize),
            sym::f16 => Self::Float(FloatTy::F16),
            sym::f32 => Self::Float(FloatTy::F32),
            sym::f64 => Self::Float(FloatTy::F64),
            sym::f128 => Self::Float(FloatTy::F128),
            sym::bool => Self::Bool,
            sym::char => Self::Char,
            sym::str => Self::Str,
            _ => return None,
        };
        Some(ty)
    }
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct BareFnTy<'hir> {
    pub unsafety: Unsafety,
    pub abi: Abi,
    pub generic_params: &'hir [GenericParam<'hir>],
    pub decl: &'hir FnDecl<'hir>,
    pub param_names: &'hir [Ident],
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct OpaqueTy<'hir> {
    pub generics: &'hir Generics<'hir>,
    pub bounds: GenericBounds<'hir>,
    pub origin: OpaqueTyOrigin,
    /// Return-position impl traits (and async futures) must "reify" any late-bound
    /// lifetimes that are captured from the function signature they originate from.
    ///
    /// This is done by generating a new early-bound lifetime parameter local to the
    /// opaque which is instantiated in the function signature with the late-bound
    /// lifetime.
    ///
    /// This mapping associated a captured lifetime (first parameter) with the new
    /// early-bound lifetime that was generated for the opaque.
    pub lifetime_mapping: &'hir [(&'hir Lifetime, LocalDefId)],
    /// Whether the opaque is a return-position impl trait (or async future)
    /// originating from a trait method. This makes it so that the opaque is
    /// lowered as an associated type.
    pub in_trait: bool,
}

#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub struct AssocOpaqueTy {
    // Add some data if necessary
}

/// From whence the opaque type came.
#[derive(Copy, Clone, PartialEq, Eq, Debug, HashStable_Generic)]
pub enum OpaqueTyOrigin {
    /// `-> impl Trait`
    FnReturn(LocalDefId),
    /// `async fn`
    AsyncFn(LocalDefId),
    /// type aliases: `type Foo = impl Trait;`
    TyAlias {
        /// The type alias or associated type parent of the TAIT/ATPIT
        parent: LocalDefId,
        /// associated types in impl blocks for traits.
        in_assoc_ty: bool,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, HashStable_Generic)]
pub enum InferDelegationKind {
    Input(usize),
    Output,
}

/// The various kinds of types recognized by the compiler.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum TyKind<'hir> {
    /// Actual type should be inherited from `DefId` signature
    InferDelegation(DefId, InferDelegationKind),
    /// A variable length slice (i.e., `[T]`).
    Slice(&'hir Ty<'hir>),
    /// A fixed length array (i.e., `[T; n]`).
    Array(&'hir Ty<'hir>, ArrayLen),
    /// A raw pointer (i.e., `*const T` or `*mut T`).
    Ptr(MutTy<'hir>),
    /// A reference (i.e., `&'a T` or `&'a mut T`).
    Ref(&'hir Lifetime, MutTy<'hir>),
    /// A bare function (e.g., `fn(usize) -> bool`).
    BareFn(&'hir BareFnTy<'hir>),
    /// The never type (`!`).
    Never,
    /// A tuple (`(A, B, C, D, ...)`).
    Tup(&'hir [Ty<'hir>]),
    /// An anonymous struct or union type i.e. `struct { foo: Type }` or `union { foo: Type }`
    AnonAdt(ItemId),
    /// A path to a type definition (`module::module::...::Type`), or an
    /// associated type (e.g., `<Vec<T> as Trait>::Type` or `<T>::Target`).
    ///
    /// Type parameters may be stored in each `PathSegment`.
    Path(QPath<'hir>),
    /// An opaque type definition itself. This is only used for `impl Trait`.
    ///
    /// The generic argument list contains the lifetimes (and in the future
    /// possibly parameters) that are actually bound on the `impl Trait`.
    ///
    /// The last parameter specifies whether this opaque appears in a trait definition.
    OpaqueDef(ItemId, &'hir [GenericArg<'hir>], bool),
    /// A trait object type `Bound1 + Bound2 + Bound3`
    /// where `Bound` is a trait or a lifetime.
    TraitObject(&'hir [PolyTraitRef<'hir>], &'hir Lifetime, TraitObjectSyntax),
    /// Unused for now.
    Typeof(AnonConst),
    /// `TyKind::Infer` means the type should be inferred instead of it having been
    /// specified. This can appear anywhere in a type.
    Infer,
    /// Placeholder for a type that has failed to be defined.
    Err(rustc_span::ErrorGuaranteed),
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum InlineAsmOperand<'hir> {
    In {
        reg: InlineAsmRegOrRegClass,
        expr: &'hir Expr<'hir>,
    },
    Out {
        reg: InlineAsmRegOrRegClass,
        late: bool,
        expr: Option<&'hir Expr<'hir>>,
    },
    InOut {
        reg: InlineAsmRegOrRegClass,
        late: bool,
        expr: &'hir Expr<'hir>,
    },
    SplitInOut {
        reg: InlineAsmRegOrRegClass,
        late: bool,
        in_expr: &'hir Expr<'hir>,
        out_expr: Option<&'hir Expr<'hir>>,
    },
    Const {
        anon_const: AnonConst,
    },
    SymFn {
        anon_const: AnonConst,
    },
    SymStatic {
        path: QPath<'hir>,
        def_id: DefId,
    },
    Label {
        block: &'hir Block<'hir>,
    },
}

impl<'hir> InlineAsmOperand<'hir> {
    pub fn reg(&self) -> Option<InlineAsmRegOrRegClass> {
        match *self {
            Self::In { reg, .. }
            | Self::Out { reg, .. }
            | Self::InOut { reg, .. }
            | Self::SplitInOut { reg, .. } => Some(reg),
            Self::Const { .. }
            | Self::SymFn { .. }
            | Self::SymStatic { .. }
            | Self::Label { .. } => None,
        }
    }

    pub fn is_clobber(&self) -> bool {
        matches!(
            self,
            InlineAsmOperand::Out { reg: InlineAsmRegOrRegClass::Reg(_), late: _, expr: None }
        )
    }
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct InlineAsm<'hir> {
    pub template: &'hir [InlineAsmTemplatePiece],
    pub template_strs: &'hir [(Symbol, Option<Symbol>, Span)],
    pub operands: &'hir [(InlineAsmOperand<'hir>, Span)],
    pub options: InlineAsmOptions,
    pub line_spans: &'hir [Span],
}

impl InlineAsm<'_> {
    pub fn contains_label(&self) -> bool {
        self.operands.iter().any(|x| matches!(x.0, InlineAsmOperand::Label { .. }))
    }
}

/// Represents a parameter in a function header.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Param<'hir> {
    pub hir_id: HirId,
    pub pat: &'hir Pat<'hir>,
    pub ty_span: Span,
    pub span: Span,
}

/// Represents the header (not the body) of a function declaration.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct FnDecl<'hir> {
    /// The types of the function's parameters.
    ///
    /// Additional argument data is stored in the function's [body](Body::params).
    pub inputs: &'hir [Ty<'hir>],
    pub output: FnRetTy<'hir>,
    pub c_variadic: bool,
    /// Does the function have an implicit self?
    pub implicit_self: ImplicitSelfKind,
    /// Is lifetime elision allowed.
    pub lifetime_elision_allowed: bool,
}

/// Represents what type of implicit self a function has, if any.
#[derive(Copy, Clone, PartialEq, Eq, Encodable, Decodable, Debug, HashStable_Generic)]
pub enum ImplicitSelfKind {
    /// Represents a `fn x(self);`.
    Imm,
    /// Represents a `fn x(mut self);`.
    Mut,
    /// Represents a `fn x(&self);`.
    ImmRef,
    /// Represents a `fn x(&mut self);`.
    MutRef,
    /// Represents when a function does not have a self argument or
    /// when a function has a `self: X` argument.
    None,
}

impl ImplicitSelfKind {
    /// Does this represent an implicit self?
    pub fn has_implicit_self(&self) -> bool {
        !matches!(*self, ImplicitSelfKind::None)
    }
}

#[derive(Copy, Clone, PartialEq, Eq, Encodable, Decodable, Debug, HashStable_Generic)]
pub enum IsAsync {
    Async(Span),
    NotAsync,
}

impl IsAsync {
    pub fn is_async(self) -> bool {
        matches!(self, IsAsync::Async(_))
    }
}

#[derive(Copy, Clone, PartialEq, Eq, Debug, Encodable, Decodable, HashStable_Generic)]
pub enum Defaultness {
    Default { has_value: bool },
    Final,
}

impl Defaultness {
    pub fn has_value(&self) -> bool {
        match *self {
            Defaultness::Default { has_value } => has_value,
            Defaultness::Final => true,
        }
    }

    pub fn is_final(&self) -> bool {
        *self == Defaultness::Final
    }

    pub fn is_default(&self) -> bool {
        matches!(*self, Defaultness::Default { .. })
    }
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum FnRetTy<'hir> {
    /// Return type is not specified.
    ///
    /// Functions default to `()` and
    /// closures default to inference. Span points to where return
    /// type would be inserted.
    DefaultReturn(Span),
    /// Everything else.
    Return(&'hir Ty<'hir>),
}

impl<'hir> FnRetTy<'hir> {
    #[inline]
    pub fn span(&self) -> Span {
        match *self {
            Self::DefaultReturn(span) => span,
            Self::Return(ref ty) => ty.span,
        }
    }

    pub fn get_infer_ret_ty(&self) -> Option<&'hir Ty<'hir>> {
        if let Self::Return(ty) = self {
            if ty.is_suggestable_infer_ty() {
                return Some(*ty);
            }
        }
        None
    }
}

/// Represents `for<...>` binder before a closure
#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub enum ClosureBinder {
    /// Binder is not specified.
    Default,
    /// Binder is specified.
    ///
    /// Span points to the whole `for<...>`.
    For { span: Span },
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Mod<'hir> {
    pub spans: ModSpans,
    pub item_ids: &'hir [ItemId],
}

#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub struct ModSpans {
    /// A span from the first token past `{` to the last token until `}`.
    /// For `mod foo;`, the inner span ranges from the first token
    /// to the last token in the external file.
    pub inner_span: Span,
    pub inject_use_span: Span,
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct EnumDef<'hir> {
    pub variants: &'hir [Variant<'hir>],
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Variant<'hir> {
    /// Name of the variant.
    pub ident: Ident,
    /// Id of the variant (not the constructor, see `VariantData::ctor_hir_id()`).
    pub hir_id: HirId,
    pub def_id: LocalDefId,
    /// Fields and constructor id of the variant.
    pub data: VariantData<'hir>,
    /// Explicit discriminant (e.g., `Foo = 1`).
    pub disr_expr: Option<AnonConst>,
    /// Span
    pub span: Span,
}

#[derive(Copy, Clone, PartialEq, Debug, HashStable_Generic)]
pub enum UseKind {
    /// One import, e.g., `use foo::bar` or `use foo::bar as baz`.
    /// Also produced for each element of a list `use`, e.g.
    /// `use foo::{a, b}` lowers to `use foo::a; use foo::b;`.
    Single,

    /// Glob import, e.g., `use foo::*`.
    Glob,

    /// Degenerate list import, e.g., `use foo::{a, b}` produces
    /// an additional `use foo::{}` for performing checks such as
    /// unstable feature gating. May be removed in the future.
    ListStem,
}

/// References to traits in impls.
///
/// `resolve` maps each `TraitRef`'s `ref_id` to its defining trait; that's all
/// that the `ref_id` is for. Note that `ref_id`'s value is not the `HirId` of the
/// trait being referred to but just a unique `HirId` that serves as a key
/// within the resolution map.
#[derive(Clone, Debug, Copy, HashStable_Generic)]
pub struct TraitRef<'hir> {
    pub path: &'hir Path<'hir>,
    // Don't hash the `ref_id`. It is tracked via the thing it is used to access.
    #[stable_hasher(ignore)]
    pub hir_ref_id: HirId,
}

impl TraitRef<'_> {
    /// Gets the `DefId` of the referenced trait. It _must_ actually be a trait or trait alias.
    pub fn trait_def_id(&self) -> Option<DefId> {
        match self.path.res {
            Res::Def(DefKind::Trait | DefKind::TraitAlias, did) => Some(did),
            Res::Err => None,
            res => panic!("{res:?} did not resolve to a trait or trait alias"),
        }
    }
}

#[derive(Clone, Debug, Copy, HashStable_Generic)]
pub struct PolyTraitRef<'hir> {
    /// The `'a` in `for<'a> Foo<&'a T>`.
    pub bound_generic_params: &'hir [GenericParam<'hir>],

    /// The `Foo<&'a T>` in `for<'a> Foo<&'a T>`.
    pub trait_ref: TraitRef<'hir>,

    pub span: Span,
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct FieldDef<'hir> {
    pub span: Span,
    pub vis_span: Span,
    pub ident: Ident,
    pub hir_id: HirId,
    pub def_id: LocalDefId,
    pub ty: &'hir Ty<'hir>,
}

impl FieldDef<'_> {
    // Still necessary in couple of places
    pub fn is_positional(&self) -> bool {
        self.ident.as_str().as_bytes()[0].is_ascii_digit()
    }
}

/// Fields and constructor IDs of enum variants and structs.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum VariantData<'hir> {
    /// A struct variant.
    ///
    /// E.g., `Bar { .. }` as in `enum Foo { Bar { .. } }`.
    Struct {
        fields: &'hir [FieldDef<'hir>],
        // FIXME: investigate making this a `Option<ErrorGuaranteed>`
        recovered: bool,
    },
    /// A tuple variant.
    ///
    /// E.g., `Bar(..)` as in `enum Foo { Bar(..) }`.
    Tuple(&'hir [FieldDef<'hir>], HirId, LocalDefId),
    /// A unit variant.
    ///
    /// E.g., `Bar = ..` as in `enum Foo { Bar = .. }`.
    Unit(HirId, LocalDefId),
}

impl<'hir> VariantData<'hir> {
    /// Return the fields of this variant.
    pub fn fields(&self) -> &'hir [FieldDef<'hir>] {
        match *self {
            VariantData::Struct { fields, .. } | VariantData::Tuple(fields, ..) => fields,
            _ => &[],
        }
    }

    pub fn ctor(&self) -> Option<(CtorKind, HirId, LocalDefId)> {
        match *self {
            VariantData::Tuple(_, hir_id, def_id) => Some((CtorKind::Fn, hir_id, def_id)),
            VariantData::Unit(hir_id, def_id) => Some((CtorKind::Const, hir_id, def_id)),
            VariantData::Struct { .. } => None,
        }
    }

    #[inline]
    pub fn ctor_kind(&self) -> Option<CtorKind> {
        self.ctor().map(|(kind, ..)| kind)
    }

    /// Return the `HirId` of this variant's constructor, if it has one.
    #[inline]
    pub fn ctor_hir_id(&self) -> Option<HirId> {
        self.ctor().map(|(_, hir_id, _)| hir_id)
    }

    /// Return the `LocalDefId` of this variant's constructor, if it has one.
    #[inline]
    pub fn ctor_def_id(&self) -> Option<LocalDefId> {
        self.ctor().map(|(.., def_id)| def_id)
    }
}

// The bodies for items are stored "out of line", in a separate
// hashmap in the `Crate`. Here we just record the hir-id of the item
// so it can fetched later.
#[derive(Copy, Clone, PartialEq, Eq, Encodable, Decodable, Debug, Hash, HashStable_Generic)]
pub struct ItemId {
    pub owner_id: OwnerId,
}

impl ItemId {
    #[inline]
    pub fn hir_id(&self) -> HirId {
        // Items are always HIR owners.
        HirId::make_owner(self.owner_id.def_id)
    }
}

/// An item
///
/// The name might be a dummy name in case of anonymous items
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Item<'hir> {
    pub ident: Ident,
    pub owner_id: OwnerId,
    pub kind: ItemKind<'hir>,
    pub span: Span,
    pub vis_span: Span,
}

impl<'hir> Item<'hir> {
    #[inline]
    pub fn hir_id(&self) -> HirId {
        // Items are always HIR owners.
        HirId::make_owner(self.owner_id.def_id)
    }

    pub fn item_id(&self) -> ItemId {
        ItemId { owner_id: self.owner_id }
    }

    /// Check if this is an [`ItemKind::Enum`], [`ItemKind::Struct`] or
    /// [`ItemKind::Union`].
    pub fn is_adt(&self) -> bool {
        matches!(self.kind, ItemKind::Enum(..) | ItemKind::Struct(..) | ItemKind::Union(..))
    }

    /// Check if this is an [`ItemKind::Struct`] or [`ItemKind::Union`].
    pub fn is_struct_or_union(&self) -> bool {
        matches!(self.kind, ItemKind::Struct(..) | ItemKind::Union(..))
    }

    expect_methods_self_kind! {
        expect_extern_crate, Option<Symbol>, ItemKind::ExternCrate(s), *s;

        expect_use, (&'hir UsePath<'hir>, UseKind), ItemKind::Use(p, uk), (p, *uk);

        expect_static, (&'hir Ty<'hir>, Mutability, BodyId),
            ItemKind::Static(ty, mutbl, body), (ty, *mutbl, *body);

        expect_const, (&'hir Ty<'hir>, &'hir Generics<'hir>, BodyId),
            ItemKind::Const(ty, gen, body), (ty, gen, *body);

        expect_fn, (&FnSig<'hir>, &'hir Generics<'hir>, BodyId),
            ItemKind::Fn(sig, gen, body), (sig, gen, *body);

        expect_macro, (&ast::MacroDef, MacroKind), ItemKind::Macro(def, mk), (def, *mk);

        expect_mod, &'hir Mod<'hir>, ItemKind::Mod(m), m;

        expect_foreign_mod, (Abi, &'hir [ForeignItemRef]),
            ItemKind::ForeignMod { abi, items }, (*abi, items);

        expect_global_asm, &'hir InlineAsm<'hir>, ItemKind::GlobalAsm(asm), asm;

        expect_ty_alias, (&'hir Ty<'hir>, &'hir Generics<'hir>),
            ItemKind::TyAlias(ty, gen), (ty, gen);

        expect_opaque_ty, &OpaqueTy<'hir>, ItemKind::OpaqueTy(ty), ty;

        expect_enum, (&EnumDef<'hir>, &'hir Generics<'hir>), ItemKind::Enum(def, gen), (def, gen);

        expect_struct, (&VariantData<'hir>, &'hir Generics<'hir>),
            ItemKind::Struct(data, gen), (data, gen);

        expect_union, (&VariantData<'hir>, &'hir Generics<'hir>),
            ItemKind::Union(data, gen), (data, gen);

        expect_trait,
            (IsAuto, Unsafety, &'hir Generics<'hir>, GenericBounds<'hir>, &'hir [TraitItemRef]),
            ItemKind::Trait(is_auto, unsafety, gen, bounds, items),
            (*is_auto, *unsafety, gen, bounds, items);

        expect_trait_alias, (&'hir Generics<'hir>, GenericBounds<'hir>),
            ItemKind::TraitAlias(gen, bounds), (gen, bounds);

        expect_impl, &'hir Impl<'hir>, ItemKind::Impl(imp), imp;
    }
}

#[derive(Copy, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Debug)]
#[derive(Encodable, Decodable, HashStable_Generic)]
pub enum Unsafety {
    Unsafe,
    Normal,
}

impl Unsafety {
    pub fn prefix_str(&self) -> &'static str {
        match self {
            Self::Unsafe => "unsafe ",
            Self::Normal => "",
        }
    }
}

impl fmt::Display for Unsafety {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match *self {
            Self::Unsafe => "unsafe",
            Self::Normal => "normal",
        })
    }
}

#[derive(Copy, Clone, PartialEq, Eq, Debug, Encodable, Decodable, HashStable_Generic)]
pub enum Constness {
    Const,
    NotConst,
}

impl fmt::Display for Constness {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match *self {
            Self::Const => "const",
            Self::NotConst => "non-const",
        })
    }
}

#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub struct FnHeader {
    pub unsafety: Unsafety,
    pub constness: Constness,
    pub asyncness: IsAsync,
    pub abi: Abi,
}

impl FnHeader {
    pub fn is_async(&self) -> bool {
        matches!(&self.asyncness, IsAsync::Async(_))
    }

    pub fn is_const(&self) -> bool {
        matches!(&self.constness, Constness::Const)
    }

    pub fn is_unsafe(&self) -> bool {
        matches!(&self.unsafety, Unsafety::Unsafe)
    }
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum ItemKind<'hir> {
    /// An `extern crate` item, with optional *original* crate name if the crate was renamed.
    ///
    /// E.g., `extern crate foo` or `extern crate foo_bar as foo`.
    ExternCrate(Option<Symbol>),

    /// `use foo::bar::*;` or `use foo::bar::baz as quux;`
    ///
    /// or just
    ///
    /// `use foo::bar::baz;` (with `as baz` implicitly on the right).
    Use(&'hir UsePath<'hir>, UseKind),

    /// A `static` item.
    Static(&'hir Ty<'hir>, Mutability, BodyId),
    /// A `const` item.
    Const(&'hir Ty<'hir>, &'hir Generics<'hir>, BodyId),
    /// A function declaration.
    Fn(FnSig<'hir>, &'hir Generics<'hir>, BodyId),
    /// A MBE macro definition (`macro_rules!` or `macro`).
    Macro(&'hir ast::MacroDef, MacroKind),
    /// A module.
    Mod(&'hir Mod<'hir>),
    /// An external module, e.g. `extern { .. }`.
    ForeignMod { abi: Abi, items: &'hir [ForeignItemRef] },
    /// Module-level inline assembly (from `global_asm!`).
    GlobalAsm(&'hir InlineAsm<'hir>),
    /// A type alias, e.g., `type Foo = Bar<u8>`.
    TyAlias(&'hir Ty<'hir>, &'hir Generics<'hir>),
    /// An opaque `impl Trait` type alias, e.g., `type Foo = impl Bar;`.
    OpaqueTy(&'hir OpaqueTy<'hir>),
    /// An enum definition, e.g., `enum Foo<A, B> {C<A>, D<B>}`.
    Enum(EnumDef<'hir>, &'hir Generics<'hir>),
    /// A struct definition, e.g., `struct Foo<A> {x: A}`.
    Struct(VariantData<'hir>, &'hir Generics<'hir>),
    /// A union definition, e.g., `union Foo<A, B> {x: A, y: B}`.
    Union(VariantData<'hir>, &'hir Generics<'hir>),
    /// A trait definition.
    Trait(IsAuto, Unsafety, &'hir Generics<'hir>, GenericBounds<'hir>, &'hir [TraitItemRef]),
    /// A trait alias.
    TraitAlias(&'hir Generics<'hir>, GenericBounds<'hir>),

    /// An implementation, e.g., `impl<A> Trait for Foo { .. }`.
    Impl(&'hir Impl<'hir>),
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct Impl<'hir> {
    pub unsafety: Unsafety,
    pub polarity: ImplPolarity,
    pub defaultness: Defaultness,
    // We do not put a `Span` in `Defaultness` because it breaks foreign crate metadata
    // decoding as `Span`s cannot be decoded when a `Session` is not available.
    pub defaultness_span: Option<Span>,
    pub generics: &'hir Generics<'hir>,

    /// The trait being implemented, if any.
    pub of_trait: Option<TraitRef<'hir>>,

    pub self_ty: &'hir Ty<'hir>,
    pub items: &'hir [ImplItemRef],
}

impl ItemKind<'_> {
    pub fn generics(&self) -> Option<&Generics<'_>> {
        Some(match *self {
            ItemKind::Fn(_, ref generics, _)
            | ItemKind::TyAlias(_, ref generics)
            | ItemKind::Const(_, ref generics, _)
            | ItemKind::OpaqueTy(OpaqueTy { ref generics, .. })
            | ItemKind::Enum(_, ref generics)
            | ItemKind::Struct(_, ref generics)
            | ItemKind::Union(_, ref generics)
            | ItemKind::Trait(_, _, ref generics, _, _)
            | ItemKind::TraitAlias(ref generics, _)
            | ItemKind::Impl(Impl { ref generics, .. }) => generics,
            _ => return None,
        })
    }

    pub fn descr(&self) -> &'static str {
        match self {
            ItemKind::ExternCrate(..) => "extern crate",
            ItemKind::Use(..) => "`use` import",
            ItemKind::Static(..) => "static item",
            ItemKind::Const(..) => "constant item",
            ItemKind::Fn(..) => "function",
            ItemKind::Macro(..) => "macro",
            ItemKind::Mod(..) => "module",
            ItemKind::ForeignMod { .. } => "extern block",
            ItemKind::GlobalAsm(..) => "global asm item",
            ItemKind::TyAlias(..) => "type alias",
            ItemKind::OpaqueTy(..) => "opaque type",
            ItemKind::Enum(..) => "enum",
            ItemKind::Struct(..) => "struct",
            ItemKind::Union(..) => "union",
            ItemKind::Trait(..) => "trait",
            ItemKind::TraitAlias(..) => "trait alias",
            ItemKind::Impl(..) => "implementation",
        }
    }
}

/// A reference from an trait to one of its associated items. This
/// contains the item's id, naturally, but also the item's name and
/// some other high-level details (like whether it is an associated
/// type or method, and whether it is public). This allows other
/// passes to find the impl they want without loading the ID (which
/// means fewer edges in the incremental compilation graph).
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct TraitItemRef {
    pub id: TraitItemId,
    pub ident: Ident,
    pub kind: AssocItemKind,
    pub span: Span,
}

/// A reference from an impl to one of its associated items. This
/// contains the item's ID, naturally, but also the item's name and
/// some other high-level details (like whether it is an associated
/// type or method, and whether it is public). This allows other
/// passes to find the impl they want without loading the ID (which
/// means fewer edges in the incremental compilation graph).
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct ImplItemRef {
    pub id: ImplItemId,
    pub ident: Ident,
    pub kind: AssocItemKind,
    pub span: Span,
    /// When we are in a trait impl, link to the trait-item's id.
    pub trait_item_def_id: Option<DefId>,
}

#[derive(Copy, Clone, PartialEq, Debug, HashStable_Generic)]
pub enum AssocItemKind {
    Const,
    Fn { has_self: bool },
    Type,
}

// The bodies for items are stored "out of line", in a separate
// hashmap in the `Crate`. Here we just record the hir-id of the item
// so it can fetched later.
#[derive(Copy, Clone, PartialEq, Eq, Encodable, Decodable, Debug, HashStable_Generic)]
pub struct ForeignItemId {
    pub owner_id: OwnerId,
}

impl ForeignItemId {
    #[inline]
    pub fn hir_id(&self) -> HirId {
        // Items are always HIR owners.
        HirId::make_owner(self.owner_id.def_id)
    }
}

/// A reference from a foreign block to one of its items. This
/// contains the item's ID, naturally, but also the item's name and
/// some other high-level details (like whether it is an associated
/// type or method, and whether it is public). This allows other
/// passes to find the impl they want without loading the ID (which
/// means fewer edges in the incremental compilation graph).
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct ForeignItemRef {
    pub id: ForeignItemId,
    pub ident: Ident,
    pub span: Span,
}

#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub struct ForeignItem<'hir> {
    pub ident: Ident,
    pub kind: ForeignItemKind<'hir>,
    pub owner_id: OwnerId,
    pub span: Span,
    pub vis_span: Span,
}

impl ForeignItem<'_> {
    #[inline]
    pub fn hir_id(&self) -> HirId {
        // Items are always HIR owners.
        HirId::make_owner(self.owner_id.def_id)
    }

    pub fn foreign_item_id(&self) -> ForeignItemId {
        ForeignItemId { owner_id: self.owner_id }
    }
}

/// An item within an `extern` block.
#[derive(Debug, Clone, Copy, HashStable_Generic)]
pub enum ForeignItemKind<'hir> {
    /// A foreign function.
    Fn(&'hir FnDecl<'hir>, &'hir [Ident], &'hir Generics<'hir>),
    /// A foreign static item (`static ext: u8`).
    Static(&'hir Ty<'hir>, Mutability),
    /// A foreign type.
    Type,
}

/// A variable captured by a closure.
#[derive(Debug, Copy, Clone, HashStable_Generic)]
pub struct Upvar {
    /// First span where it is accessed (there can be multiple).
    pub span: Span,
}

// The TraitCandidate's import_ids is empty if the trait is defined in the same module, and
// has length > 0 if the trait is found through an chain of imports, starting with the
// import/use statement in the scope where the trait is used.
#[derive(Debug, Clone, HashStable_Generic)]
pub struct TraitCandidate {
    pub def_id: DefId,
    pub import_ids: SmallVec<[LocalDefId; 1]>,
}

#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub enum OwnerNode<'hir> {
    Item(&'hir Item<'hir>),
    ForeignItem(&'hir ForeignItem<'hir>),
    TraitItem(&'hir TraitItem<'hir>),
    ImplItem(&'hir ImplItem<'hir>),
    Crate(&'hir Mod<'hir>),
    AssocOpaqueTy(&'hir AssocOpaqueTy),
}

impl<'hir> OwnerNode<'hir> {
    pub fn ident(&self) -> Option<Ident> {
        match self {
            OwnerNode::Item(Item { ident, .. })
            | OwnerNode::ForeignItem(ForeignItem { ident, .. })
            | OwnerNode::ImplItem(ImplItem { ident, .. })
            | OwnerNode::TraitItem(TraitItem { ident, .. }) => Some(*ident),
            OwnerNode::Crate(..) | OwnerNode::AssocOpaqueTy(..) => None,
        }
    }

    // Span by reference to pass to `Node::Err`.
    #[allow(rustc::pass_by_value)]
    pub fn span(&self) -> &'hir Span {
        match self {
            OwnerNode::Item(Item { span, .. })
            | OwnerNode::ForeignItem(ForeignItem { span, .. })
            | OwnerNode::ImplItem(ImplItem { span, .. })
            | OwnerNode::TraitItem(TraitItem { span, .. }) => span,
            OwnerNode::Crate(Mod { spans: ModSpans { inner_span, .. }, .. }) => inner_span,
            OwnerNode::AssocOpaqueTy(..) => unreachable!(),
        }
    }

    pub fn fn_sig(self) -> Option<&'hir FnSig<'hir>> {
        match self {
            OwnerNode::TraitItem(TraitItem { kind: TraitItemKind::Fn(fn_sig, _), .. })
            | OwnerNode::ImplItem(ImplItem { kind: ImplItemKind::Fn(fn_sig, _), .. })
            | OwnerNode::Item(Item { kind: ItemKind::Fn(fn_sig, _, _), .. }) => Some(fn_sig),
            _ => None,
        }
    }

    pub fn fn_decl(self) -> Option<&'hir FnDecl<'hir>> {
        match self {
            OwnerNode::TraitItem(TraitItem { kind: TraitItemKind::Fn(fn_sig, _), .. })
            | OwnerNode::ImplItem(ImplItem { kind: ImplItemKind::Fn(fn_sig, _), .. })
            | OwnerNode::Item(Item { kind: ItemKind::Fn(fn_sig, _, _), .. }) => Some(fn_sig.decl),
            OwnerNode::ForeignItem(ForeignItem {
                kind: ForeignItemKind::Fn(fn_decl, _, _),
                ..
            }) => Some(fn_decl),
            _ => None,
        }
    }

    pub fn body_id(&self) -> Option<BodyId> {
        match self {
            OwnerNode::Item(Item {
                kind:
                    ItemKind::Static(_, _, body)
                    | ItemKind::Const(_, _, body)
                    | ItemKind::Fn(_, _, body),
                ..
            })
            | OwnerNode::TraitItem(TraitItem {
                kind:
                    TraitItemKind::Fn(_, TraitFn::Provided(body)) | TraitItemKind::Const(_, Some(body)),
                ..
            })
            | OwnerNode::ImplItem(ImplItem {
                kind: ImplItemKind::Fn(_, body) | ImplItemKind::Const(_, body),
                ..
            }) => Some(*body),
            _ => None,
        }
    }

    pub fn generics(self) -> Option<&'hir Generics<'hir>> {
        Node::generics(self.into())
    }

    pub fn def_id(self) -> OwnerId {
        match self {
            OwnerNode::Item(Item { owner_id, .. })
            | OwnerNode::TraitItem(TraitItem { owner_id, .. })
            | OwnerNode::ImplItem(ImplItem { owner_id, .. })
            | OwnerNode::ForeignItem(ForeignItem { owner_id, .. }) => *owner_id,
            OwnerNode::Crate(..) => crate::CRATE_HIR_ID.owner,
            OwnerNode::AssocOpaqueTy(..) => unreachable!(),
        }
    }

    expect_methods_self! {
        expect_item,         &'hir Item<'hir>,        OwnerNode::Item(n),        n;
        expect_foreign_item, &'hir ForeignItem<'hir>, OwnerNode::ForeignItem(n), n;
        expect_impl_item,    &'hir ImplItem<'hir>,    OwnerNode::ImplItem(n),    n;
        expect_trait_item,   &'hir TraitItem<'hir>,   OwnerNode::TraitItem(n),   n;
    }
}

impl<'hir> Into<OwnerNode<'hir>> for &'hir Item<'hir> {
    fn into(self) -> OwnerNode<'hir> {
        OwnerNode::Item(self)
    }
}

impl<'hir> Into<OwnerNode<'hir>> for &'hir ForeignItem<'hir> {
    fn into(self) -> OwnerNode<'hir> {
        OwnerNode::ForeignItem(self)
    }
}

impl<'hir> Into<OwnerNode<'hir>> for &'hir ImplItem<'hir> {
    fn into(self) -> OwnerNode<'hir> {
        OwnerNode::ImplItem(self)
    }
}

impl<'hir> Into<OwnerNode<'hir>> for &'hir TraitItem<'hir> {
    fn into(self) -> OwnerNode<'hir> {
        OwnerNode::TraitItem(self)
    }
}

impl<'hir> Into<Node<'hir>> for OwnerNode<'hir> {
    fn into(self) -> Node<'hir> {
        match self {
            OwnerNode::Item(n) => Node::Item(n),
            OwnerNode::ForeignItem(n) => Node::ForeignItem(n),
            OwnerNode::ImplItem(n) => Node::ImplItem(n),
            OwnerNode::TraitItem(n) => Node::TraitItem(n),
            OwnerNode::Crate(n) => Node::Crate(n),
            OwnerNode::AssocOpaqueTy(n) => Node::AssocOpaqueTy(n),
        }
    }
}

#[derive(Copy, Clone, Debug, HashStable_Generic)]
pub enum Node<'hir> {
    Param(&'hir Param<'hir>),
    Item(&'hir Item<'hir>),
    ForeignItem(&'hir ForeignItem<'hir>),
    TraitItem(&'hir TraitItem<'hir>),
    ImplItem(&'hir ImplItem<'hir>),
    Variant(&'hir Variant<'hir>),
    Field(&'hir FieldDef<'hir>),
    AnonConst(&'hir AnonConst),
    ConstBlock(&'hir ConstBlock),
    Expr(&'hir Expr<'hir>),
    ExprField(&'hir ExprField<'hir>),
    Stmt(&'hir Stmt<'hir>),
    PathSegment(&'hir PathSegment<'hir>),
    Ty(&'hir Ty<'hir>),
    TypeBinding(&'hir TypeBinding<'hir>),
    TraitRef(&'hir TraitRef<'hir>),
    Pat(&'hir Pat<'hir>),
    PatField(&'hir PatField<'hir>),
    Arm(&'hir Arm<'hir>),
    Block(&'hir Block<'hir>),
    Local(&'hir Local<'hir>),
    /// `Ctor` refers to the constructor of an enum variant or struct. Only tuple or unit variants
    /// with synthesized constructors.
    Ctor(&'hir VariantData<'hir>),
    Lifetime(&'hir Lifetime),
    GenericParam(&'hir GenericParam<'hir>),
    Crate(&'hir Mod<'hir>),
    Infer(&'hir InferArg),
    WhereBoundPredicate(&'hir WhereBoundPredicate<'hir>),
    // FIXME: Merge into `Node::Infer`.
    ArrayLenInfer(&'hir InferArg),
    AssocOpaqueTy(&'hir AssocOpaqueTy),
    // Span by reference to minimize `Node`'s size
    #[allow(rustc::pass_by_value)]
    Err(&'hir Span),
}

impl<'hir> Node<'hir> {
    /// Get the identifier of this `Node`, if applicable.
    ///
    /// # Edge cases
    ///
    /// Calling `.ident()` on a [`Node::Ctor`] will return `None`
    /// because `Ctor`s do not have identifiers themselves.
    /// Instead, call `.ident()` on the parent struct/variant, like so:
    ///
    /// ```ignore (illustrative)
    /// ctor
    ///     .ctor_hir_id()
    ///     .map(|ctor_id| tcx.parent_hir_node(ctor_id))
    ///     .and_then(|parent| parent.ident())
    /// ```
    pub fn ident(&self) -> Option<Ident> {
        match self {
            Node::TraitItem(TraitItem { ident, .. })
            | Node::ImplItem(ImplItem { ident, .. })
            | Node::ForeignItem(ForeignItem { ident, .. })
            | Node::Field(FieldDef { ident, .. })
            | Node::Variant(Variant { ident, .. })
            | Node::Item(Item { ident, .. })
            | Node::PathSegment(PathSegment { ident, .. }) => Some(*ident),
            Node::Lifetime(lt) => Some(lt.ident),
            Node::GenericParam(p) => Some(p.name.ident()),
            Node::TypeBinding(b) => Some(b.ident),
            Node::PatField(f) => Some(f.ident),
            Node::ExprField(f) => Some(f.ident),
            Node::Param(..)
            | Node::AnonConst(..)
            | Node::ConstBlock(..)
            | Node::Expr(..)
            | Node::Stmt(..)
            | Node::Block(..)
            | Node::Ctor(..)
            | Node::Pat(..)
            | Node::Arm(..)
            | Node::Local(..)
            | Node::Crate(..)
            | Node::Ty(..)
            | Node::TraitRef(..)
            | Node::Infer(..)
            | Node::WhereBoundPredicate(..)
            | Node::ArrayLenInfer(..)
            | Node::AssocOpaqueTy(..)
            | Node::Err(..) => None,
        }
    }

    pub fn fn_decl(self) -> Option<&'hir FnDecl<'hir>> {
        match self {
            Node::TraitItem(TraitItem { kind: TraitItemKind::Fn(fn_sig, _), .. })
            | Node::ImplItem(ImplItem { kind: ImplItemKind::Fn(fn_sig, _), .. })
            | Node::Item(Item { kind: ItemKind::Fn(fn_sig, _, _), .. }) => Some(fn_sig.decl),
            Node::Expr(Expr { kind: ExprKind::Closure(Closure { fn_decl, .. }), .. })
            | Node::ForeignItem(ForeignItem { kind: ForeignItemKind::Fn(fn_decl, _, _), .. }) => {
                Some(fn_decl)
            }
            _ => None,
        }
    }

    pub fn fn_sig(self) -> Option<&'hir FnSig<'hir>> {
        match self {
            Node::TraitItem(TraitItem { kind: TraitItemKind::Fn(fn_sig, _), .. })
            | Node::ImplItem(ImplItem { kind: ImplItemKind::Fn(fn_sig, _), .. })
            | Node::Item(Item { kind: ItemKind::Fn(fn_sig, _, _), .. }) => Some(fn_sig),
            _ => None,
        }
    }

    /// Get the type for constants, assoc types, type aliases and statics.
    pub fn ty(self) -> Option<&'hir Ty<'hir>> {
        match self {
            Node::Item(it) => match it.kind {
                ItemKind::TyAlias(ty, _)
                | ItemKind::Static(ty, _, _)
                | ItemKind::Const(ty, _, _) => Some(ty),
                ItemKind::Impl(impl_item) => Some(&impl_item.self_ty),
                _ => None,
            },
            Node::TraitItem(it) => match it.kind {
                TraitItemKind::Const(ty, _) => Some(ty),
                TraitItemKind::Type(_, ty) => ty,
                _ => None,
            },
            Node::ImplItem(it) => match it.kind {
                ImplItemKind::Const(ty, _) => Some(ty),
                ImplItemKind::Type(ty) => Some(ty),
                _ => None,
            },
            _ => None,
        }
    }

    pub fn alias_ty(self) -> Option<&'hir Ty<'hir>> {
        match self {
            Node::Item(Item { kind: ItemKind::TyAlias(ty, ..), .. }) => Some(ty),
            _ => None,
        }
    }

    pub fn body_id(&self) -> Option<BodyId> {
        match self {
            Node::Item(Item {
                kind:
                    ItemKind::Static(_, _, body)
                    | ItemKind::Const(_, _, body)
                    | ItemKind::Fn(_, _, body),
                ..
            })
            | Node::TraitItem(TraitItem {
                kind:
                    TraitItemKind::Fn(_, TraitFn::Provided(body)) | TraitItemKind::Const(_, Some(body)),
                ..
            })
            | Node::ImplItem(ImplItem {
                kind: ImplItemKind::Fn(_, body) | ImplItemKind::Const(_, body),
                ..
            })
            | Node::Expr(Expr {
                kind:
                    ExprKind::ConstBlock(ConstBlock { body, .. })
                    | ExprKind::Closure(Closure { body, .. })
                    | ExprKind::Repeat(_, ArrayLen::Body(AnonConst { body, .. })),
                ..
            }) => Some(*body),
            _ => None,
        }
    }

    pub fn generics(self) -> Option<&'hir Generics<'hir>> {
        match self {
            Node::ForeignItem(ForeignItem {
                kind: ForeignItemKind::Fn(_, _, generics), ..
            })
            | Node::TraitItem(TraitItem { generics, .. })
            | Node::ImplItem(ImplItem { generics, .. }) => Some(generics),
            Node::Item(item) => item.kind.generics(),
            _ => None,
        }
    }

    pub fn as_owner(self) -> Option<OwnerNode<'hir>> {
        match self {
            Node::Item(i) => Some(OwnerNode::Item(i)),
            Node::ForeignItem(i) => Some(OwnerNode::ForeignItem(i)),
            Node::TraitItem(i) => Some(OwnerNode::TraitItem(i)),
            Node::ImplItem(i) => Some(OwnerNode::ImplItem(i)),
            Node::Crate(i) => Some(OwnerNode::Crate(i)),
            Node::AssocOpaqueTy(i) => Some(OwnerNode::AssocOpaqueTy(i)),
            _ => None,
        }
    }

    pub fn fn_kind(self) -> Option<FnKind<'hir>> {
        match self {
            Node::Item(i) => match i.kind {
                ItemKind::Fn(ref sig, ref generics, _) => {
                    Some(FnKind::ItemFn(i.ident, generics, sig.header))
                }
                _ => None,
            },
            Node::TraitItem(ti) => match ti.kind {
                TraitItemKind::Fn(ref sig, TraitFn::Provided(_)) => {
                    Some(FnKind::Method(ti.ident, sig))
                }
                _ => None,
            },
            Node::ImplItem(ii) => match ii.kind {
                ImplItemKind::Fn(ref sig, _) => Some(FnKind::Method(ii.ident, sig)),
                _ => None,
            },
            Node::Expr(e) => match e.kind {
                ExprKind::Closure { .. } => Some(FnKind::Closure),
                _ => None,
            },
            _ => None,
        }
    }

    expect_methods_self! {
        expect_param,         &'hir Param<'hir>,        Node::Param(n),        n;
        expect_item,          &'hir Item<'hir>,         Node::Item(n),         n;
        expect_foreign_item,  &'hir ForeignItem<'hir>,  Node::ForeignItem(n),  n;
        expect_trait_item,    &'hir TraitItem<'hir>,    Node::TraitItem(n),    n;
        expect_impl_item,     &'hir ImplItem<'hir>,     Node::ImplItem(n),     n;
        expect_variant,       &'hir Variant<'hir>,      Node::Variant(n),      n;
        expect_field,         &'hir FieldDef<'hir>,     Node::Field(n),        n;
        expect_anon_const,    &'hir AnonConst,          Node::AnonConst(n),    n;
        expect_inline_const,  &'hir ConstBlock,         Node::ConstBlock(n),   n;
        expect_expr,          &'hir Expr<'hir>,         Node::Expr(n),         n;
        expect_expr_field,    &'hir ExprField<'hir>,    Node::ExprField(n),    n;
        expect_stmt,          &'hir Stmt<'hir>,         Node::Stmt(n),         n;
        expect_path_segment,  &'hir PathSegment<'hir>,  Node::PathSegment(n),  n;
        expect_ty,            &'hir Ty<'hir>,           Node::Ty(n),           n;
        expect_type_binding,  &'hir TypeBinding<'hir>,  Node::TypeBinding(n),  n;
        expect_trait_ref,     &'hir TraitRef<'hir>,     Node::TraitRef(n),     n;
        expect_pat,           &'hir Pat<'hir>,          Node::Pat(n),          n;
        expect_pat_field,     &'hir PatField<'hir>,     Node::PatField(n),     n;
        expect_arm,           &'hir Arm<'hir>,          Node::Arm(n),          n;
        expect_block,         &'hir Block<'hir>,        Node::Block(n),        n;
        expect_local,         &'hir Local<'hir>,        Node::Local(n),        n;
        expect_ctor,          &'hir VariantData<'hir>,  Node::Ctor(n),         n;
        expect_lifetime,      &'hir Lifetime,           Node::Lifetime(n),     n;
        expect_generic_param, &'hir GenericParam<'hir>, Node::GenericParam(n), n;
        expect_crate,         &'hir Mod<'hir>,          Node::Crate(n),        n;
        expect_infer,         &'hir InferArg,           Node::Infer(n),        n;
        expect_closure,       &'hir Closure<'hir>, Node::Expr(Expr { kind: ExprKind::Closure(n), .. }), n;
    }
}

// Some nodes are used a lot. Make sure they don't unintentionally get bigger.
#[cfg(all(target_arch = "x86_64", target_pointer_width = "64"))]
mod size_asserts {
    use super::*;
    // tidy-alphabetical-start
    static_assert_size!(Block<'_>, 48);
    static_assert_size!(Body<'_>, 24);
    static_assert_size!(Expr<'_>, 64);
    static_assert_size!(ExprKind<'_>, 48);
    static_assert_size!(FnDecl<'_>, 40);
    static_assert_size!(ForeignItem<'_>, 72);
    static_assert_size!(ForeignItemKind<'_>, 40);
    static_assert_size!(GenericArg<'_>, 32);
    static_assert_size!(GenericBound<'_>, 48);
    static_assert_size!(Generics<'_>, 56);
    static_assert_size!(Impl<'_>, 80);
    static_assert_size!(ImplItem<'_>, 88);
    static_assert_size!(ImplItemKind<'_>, 40);
    static_assert_size!(Item<'_>, 88);
    static_assert_size!(ItemKind<'_>, 56);
    static_assert_size!(Local<'_>, 64);
    static_assert_size!(Param<'_>, 32);
    static_assert_size!(Pat<'_>, 72);
    static_assert_size!(Path<'_>, 40);
    static_assert_size!(PathSegment<'_>, 48);
    static_assert_size!(PatKind<'_>, 48);
    static_assert_size!(QPath<'_>, 24);
    static_assert_size!(Res, 12);
    static_assert_size!(Stmt<'_>, 32);
    static_assert_size!(StmtKind<'_>, 16);
    static_assert_size!(TraitItem<'_>, 88);
    static_assert_size!(TraitItemKind<'_>, 48);
    static_assert_size!(Ty<'_>, 48);
    static_assert_size!(TyKind<'_>, 32);
    // tidy-alphabetical-end
}

fn debug_fn(f: impl Fn(&mut fmt::Formatter<'_>) -> fmt::Result) -> impl fmt::Debug {
    struct DebugFn<F>(F);
    impl<F: Fn(&mut fmt::Formatter<'_>) -> fmt::Result> fmt::Debug for DebugFn<F> {
        fn fmt(&self, fmt: &mut fmt::Formatter<'_>) -> fmt::Result {
            (self.0)(fmt)
        }
    }
    DebugFn(f)
}


// --- rs_serde_json_de.rs ---
//! Deserialize JSON data to a Rust data structure.

use crate::error::{Error, ErrorCode, Result};
#[cfg(feature = "float_roundtrip")]
use crate::lexical;
use crate::number::Number;
use crate::read::{self, Fused, Reference};
use alloc::string::String;
use alloc::vec::Vec;
#[cfg(feature = "float_roundtrip")]
use core::iter;
use core::iter::FusedIterator;
use core::marker::PhantomData;
use core::result;
use core::str::FromStr;
use serde::de::{self, Expected, Unexpected};
use serde::forward_to_deserialize_any;

#[cfg(feature = "arbitrary_precision")]
use crate::number::NumberDeserializer;

pub use crate::read::{Read, SliceRead, StrRead};

#[cfg(feature = "std")]
#[cfg_attr(docsrs, doc(cfg(feature = "std")))]
pub use crate::read::IoRead;

//////////////////////////////////////////////////////////////////////////////

/// A structure that deserializes JSON into Rust values.
pub struct Deserializer<R> {
    read: R,
    scratch: Vec<u8>,
    remaining_depth: u8,
    #[cfg(feature = "float_roundtrip")]
    single_precision: bool,
    #[cfg(feature = "unbounded_depth")]
    disable_recursion_limit: bool,
}

impl<'de, R> Deserializer<R>
where
    R: read::Read<'de>,
{
    /// Create a JSON deserializer from one of the possible serde_json input
    /// sources.
    ///
    /// Typically it is more convenient to use one of these methods instead:
    ///
    ///   - Deserializer::from_str
    ///   - Deserializer::from_slice
    ///   - Deserializer::from_reader
    pub fn new(read: R) -> Self {
        Deserializer {
            read,
            scratch: Vec::new(),
            remaining_depth: 128,
            #[cfg(feature = "float_roundtrip")]
            single_precision: false,
            #[cfg(feature = "unbounded_depth")]
            disable_recursion_limit: false,
        }
    }
}

#[cfg(feature = "std")]
impl<R> Deserializer<read::IoRead<R>>
where
    R: crate::io::Read,
{
    /// Creates a JSON deserializer from an `io::Read`.
    ///
    /// Reader-based deserializers do not support deserializing borrowed types
    /// like `&str`, since the `std::io::Read` trait has no non-copying methods
    /// -- everything it does involves copying bytes out of the data source.
    pub fn from_reader(reader: R) -> Self {
        Deserializer::new(read::IoRead::new(reader))
    }
}

impl<'a> Deserializer<read::SliceRead<'a>> {
    /// Creates a JSON deserializer from a `&[u8]`.
    pub fn from_slice(bytes: &'a [u8]) -> Self {
        Deserializer::new(read::SliceRead::new(bytes))
    }
}

impl<'a> Deserializer<read::StrRead<'a>> {
    /// Creates a JSON deserializer from a `&str`.
    pub fn from_str(s: &'a str) -> Self {
        Deserializer::new(read::StrRead::new(s))
    }
}

macro_rules! overflow {
    ($a:ident * 10 + $b:ident, $c:expr) => {
        match $c {
            c => $a >= c / 10 && ($a > c / 10 || $b > c % 10),
        }
    };
}

pub(crate) enum ParserNumber {
    F64(f64),
    U64(u64),
    I64(i64),
    #[cfg(feature = "arbitrary_precision")]
    String(String),
}

impl ParserNumber {
    fn visit<'de, V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        match self {
            ParserNumber::F64(x) => visitor.visit_f64(x),
            ParserNumber::U64(x) => visitor.visit_u64(x),
            ParserNumber::I64(x) => visitor.visit_i64(x),
            #[cfg(feature = "arbitrary_precision")]
            ParserNumber::String(x) => visitor.visit_map(NumberDeserializer { number: x.into() }),
        }
    }

    fn invalid_type(self, exp: &dyn Expected) -> Error {
        match self {
            ParserNumber::F64(x) => de::Error::invalid_type(Unexpected::Float(x), exp),
            ParserNumber::U64(x) => de::Error::invalid_type(Unexpected::Unsigned(x), exp),
            ParserNumber::I64(x) => de::Error::invalid_type(Unexpected::Signed(x), exp),
            #[cfg(feature = "arbitrary_precision")]
            ParserNumber::String(_) => de::Error::invalid_type(Unexpected::Other("number"), exp),
        }
    }
}

impl<'de, R: Read<'de>> Deserializer<R> {
    /// The `Deserializer::end` method should be called after a value has been fully deserialized.
    /// This allows the `Deserializer` to validate that the input stream is at the end or that it
    /// only has trailing whitespace.
    pub fn end(&mut self) -> Result<()> {
        match tri!(self.parse_whitespace()) {
            Some(_) => Err(self.peek_error(ErrorCode::TrailingCharacters)),
            None => Ok(()),
        }
    }

    /// Turn a JSON deserializer into an iterator over values of type T.
    pub fn into_iter<T>(self) -> StreamDeserializer<'de, R, T>
    where
        T: de::Deserialize<'de>,
    {
        // This cannot be an implementation of std::iter::IntoIterator because
        // we need the caller to choose what T is.
        let offset = self.read.byte_offset();
        StreamDeserializer {
            de: self,
            offset,
            failed: false,
            output: PhantomData,
            lifetime: PhantomData,
        }
    }

    /// Parse arbitrarily deep JSON structures without any consideration for
    /// overflowing the stack.
    ///
    /// You will want to provide some other way to protect against stack
    /// overflows, such as by wrapping your Deserializer in the dynamically
    /// growing stack adapter provided by the serde_stacker crate. Additionally
    /// you will need to be careful around other recursive operations on the
    /// parsed result which may overflow the stack after deserialization has
    /// completed, including, but not limited to, Display and Debug and Drop
    /// impls.
    ///
    /// *This method is only available if serde_json is built with the
    /// `"unbounded_depth"` feature.*
    ///
    /// # Examples
    ///
    /// ```
    /// use serde::Deserialize;
    /// use serde_json::Value;
    ///
    /// fn main() {
    ///     let mut json = String::new();
    ///     for _ in 0..10000 {
    ///         json = format!("[{}]", json);
    ///     }
    ///
    ///     let mut deserializer = serde_json::Deserializer::from_str(&json);
    ///     deserializer.disable_recursion_limit();
    ///     let deserializer = serde_stacker::Deserializer::new(&mut deserializer);
    ///     let value = Value::deserialize(deserializer).unwrap();
    ///
    ///     carefully_drop_nested_arrays(value);
    /// }
    ///
    /// fn carefully_drop_nested_arrays(value: Value) {
    ///     let mut stack = vec![value];
    ///     while let Some(value) = stack.pop() {
    ///         if let Value::Array(array) = value {
    ///             stack.extend(array);
    ///         }
    ///     }
    /// }
    /// ```
    #[cfg(feature = "unbounded_depth")]
    #[cfg_attr(docsrs, doc(cfg(feature = "unbounded_depth")))]
    pub fn disable_recursion_limit(&mut self) {
        self.disable_recursion_limit = true;
    }

    pub(crate) fn peek(&mut self) -> Result<Option<u8>> {
        self.read.peek()
    }

    fn peek_or_null(&mut self) -> Result<u8> {
        Ok(tri!(self.peek()).unwrap_or(b'\x00'))
    }

    fn eat_char(&mut self) {
        self.read.discard();
    }

    fn next_char(&mut self) -> Result<Option<u8>> {
        self.read.next()
    }

    fn next_char_or_null(&mut self) -> Result<u8> {
        Ok(tri!(self.next_char()).unwrap_or(b'\x00'))
    }

    /// Error caused by a byte from next_char().
    #[cold]
    fn error(&self, reason: ErrorCode) -> Error {
        let position = self.read.position();
        Error::syntax(reason, position.line, position.column)
    }

    /// Error caused by a byte from peek().
    #[cold]
    fn peek_error(&self, reason: ErrorCode) -> Error {
        let position = self.read.peek_position();
        Error::syntax(reason, position.line, position.column)
    }

    /// Returns the first non-whitespace byte without consuming it, or `None` if
    /// EOF is encountered.
    fn parse_whitespace(&mut self) -> Result<Option<u8>> {
        loop {
            match tri!(self.peek()) {
                Some(b' ' | b'\n' | b'\t' | b'\r') => {
                    self.eat_char();
                }
                other => {
                    return Ok(other);
                }
            }
        }
    }

    #[cold]
    fn peek_invalid_type(&mut self, exp: &dyn Expected) -> Error {
        let err = match self.peek_or_null().unwrap_or(b'\x00') {
            b'n' => {
                self.eat_char();
                if let Err(err) = self.parse_ident(b"ull") {
                    return err;
                }
                de::Error::invalid_type(Unexpected::Unit, exp)
            }
            b't' => {
                self.eat_char();
                if let Err(err) = self.parse_ident(b"rue") {
                    return err;
                }
                de::Error::invalid_type(Unexpected::Bool(true), exp)
            }
            b'f' => {
                self.eat_char();
                if let Err(err) = self.parse_ident(b"alse") {
                    return err;
                }
                de::Error::invalid_type(Unexpected::Bool(false), exp)
            }
            b'-' => {
                self.eat_char();
                match self.parse_any_number(false) {
                    Ok(n) => n.invalid_type(exp),
                    Err(err) => return err,
                }
            }
            b'0'..=b'9' => match self.parse_any_number(true) {
                Ok(n) => n.invalid_type(exp),
                Err(err) => return err,
            },
            b'"' => {
                self.eat_char();
                self.scratch.clear();
                match self.read.parse_str(&mut self.scratch) {
                    Ok(s) => de::Error::invalid_type(Unexpected::Str(&s), exp),
                    Err(err) => return err,
                }
            }
            b'[' => de::Error::invalid_type(Unexpected::Seq, exp),
            b'{' => de::Error::invalid_type(Unexpected::Map, exp),
            _ => self.peek_error(ErrorCode::ExpectedSomeValue),
        };

        self.fix_position(err)
    }

    pub(crate) fn deserialize_number<'any, V>(&mut self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'any>,
    {
        let peek = match tri!(self.parse_whitespace()) {
            Some(b) => b,
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        let value = match peek {
            b'-' => {
                self.eat_char();
                tri!(self.parse_integer(false)).visit(visitor)
            }
            b'0'..=b'9' => tri!(self.parse_integer(true)).visit(visitor),
            _ => Err(self.peek_invalid_type(&visitor)),
        };

        match value {
            Ok(value) => Ok(value),
            Err(err) => Err(self.fix_position(err)),
        }
    }

    #[cfg(feature = "float_roundtrip")]
    pub(crate) fn do_deserialize_f32<'any, V>(&mut self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'any>,
    {
        self.single_precision = true;
        let val = self.deserialize_number(visitor);
        self.single_precision = false;
        val
    }

    pub(crate) fn do_deserialize_i128<'any, V>(&mut self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'any>,
    {
        let mut buf = String::new();

        match tri!(self.parse_whitespace()) {
            Some(b'-') => {
                self.eat_char();
                buf.push('-');
            }
            Some(_) => {}
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        tri!(self.scan_integer128(&mut buf));

        let value = match buf.parse() {
            Ok(int) => visitor.visit_i128(int),
            Err(_) => {
                return Err(self.error(ErrorCode::NumberOutOfRange));
            }
        };

        match value {
            Ok(value) => Ok(value),
            Err(err) => Err(self.fix_position(err)),
        }
    }

    pub(crate) fn do_deserialize_u128<'any, V>(&mut self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'any>,
    {
        match tri!(self.parse_whitespace()) {
            Some(b'-') => {
                return Err(self.peek_error(ErrorCode::NumberOutOfRange));
            }
            Some(_) => {}
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        }

        let mut buf = String::new();
        tri!(self.scan_integer128(&mut buf));

        let value = match buf.parse() {
            Ok(int) => visitor.visit_u128(int),
            Err(_) => {
                return Err(self.error(ErrorCode::NumberOutOfRange));
            }
        };

        match value {
            Ok(value) => Ok(value),
            Err(err) => Err(self.fix_position(err)),
        }
    }

    fn scan_integer128(&mut self, buf: &mut String) -> Result<()> {
        match tri!(self.next_char_or_null()) {
            b'0' => {
                buf.push('0');
                // There can be only one leading '0'.
                match tri!(self.peek_or_null()) {
                    b'0'..=b'9' => Err(self.peek_error(ErrorCode::InvalidNumber)),
                    _ => Ok(()),
                }
            }
            c @ b'1'..=b'9' => {
                buf.push(c as char);
                while let c @ b'0'..=b'9' = tri!(self.peek_or_null()) {
                    self.eat_char();
                    buf.push(c as char);
                }
                Ok(())
            }
            _ => Err(self.error(ErrorCode::InvalidNumber)),
        }
    }

    #[cold]
    fn fix_position(&self, err: Error) -> Error {
        err.fix_position(move |code| self.error(code))
    }

    fn parse_ident(&mut self, ident: &[u8]) -> Result<()> {
        for expected in ident {
            match tri!(self.next_char()) {
                None => {
                    return Err(self.error(ErrorCode::EofWhileParsingValue));
                }
                Some(next) => {
                    if next != *expected {
                        return Err(self.error(ErrorCode::ExpectedSomeIdent));
                    }
                }
            }
        }

        Ok(())
    }

    fn parse_integer(&mut self, positive: bool) -> Result<ParserNumber> {
        let next = match tri!(self.next_char()) {
            Some(b) => b,
            None => {
                return Err(self.error(ErrorCode::EofWhileParsingValue));
            }
        };

        match next {
            b'0' => {
                // There can be only one leading '0'.
                match tri!(self.peek_or_null()) {
                    b'0'..=b'9' => Err(self.peek_error(ErrorCode::InvalidNumber)),
                    _ => self.parse_number(positive, 0),
                }
            }
            c @ b'1'..=b'9' => {
                let mut significand = (c - b'0') as u64;

                loop {
                    match tri!(self.peek_or_null()) {
                        c @ b'0'..=b'9' => {
                            let digit = (c - b'0') as u64;

                            // We need to be careful with overflow. If we can,
                            // try to keep the number as a `u64` until we grow
                            // too large. At that point, switch to parsing the
                            // value as a `f64`.
                            if overflow!(significand * 10 + digit, u64::MAX) {
                                return Ok(ParserNumber::F64(tri!(
                                    self.parse_long_integer(positive, significand),
                                )));
                            }

                            self.eat_char();
                            significand = significand * 10 + digit;
                        }
                        _ => {
                            return self.parse_number(positive, significand);
                        }
                    }
                }
            }
            _ => Err(self.error(ErrorCode::InvalidNumber)),
        }
    }

    fn parse_number(&mut self, positive: bool, significand: u64) -> Result<ParserNumber> {
        Ok(match tri!(self.peek_or_null()) {
            b'.' => ParserNumber::F64(tri!(self.parse_decimal(positive, significand, 0))),
            b'e' | b'E' => ParserNumber::F64(tri!(self.parse_exponent(positive, significand, 0))),
            _ => {
                if positive {
                    ParserNumber::U64(significand)
                } else {
                    let neg = (significand as i64).wrapping_neg();

                    // Convert into a float if we underflow, or on `-0`.
                    if neg >= 0 {
                        ParserNumber::F64(-(significand as f64))
                    } else {
                        ParserNumber::I64(neg)
                    }
                }
            }
        })
    }

    fn parse_decimal(
        &mut self,
        positive: bool,
        mut significand: u64,
        exponent_before_decimal_point: i32,
    ) -> Result<f64> {
        self.eat_char();

        let mut exponent_after_decimal_point = 0;
        while let c @ b'0'..=b'9' = tri!(self.peek_or_null()) {
            let digit = (c - b'0') as u64;

            if overflow!(significand * 10 + digit, u64::MAX) {
                let exponent = exponent_before_decimal_point + exponent_after_decimal_point;
                return self.parse_decimal_overflow(positive, significand, exponent);
            }

            self.eat_char();
            significand = significand * 10 + digit;
            exponent_after_decimal_point -= 1;
        }

        // Error if there is not at least one digit after the decimal point.
        if exponent_after_decimal_point == 0 {
            match tri!(self.peek()) {
                Some(_) => return Err(self.peek_error(ErrorCode::InvalidNumber)),
                None => return Err(self.peek_error(ErrorCode::EofWhileParsingValue)),
            }
        }

        let exponent = exponent_before_decimal_point + exponent_after_decimal_point;
        match tri!(self.peek_or_null()) {
            b'e' | b'E' => self.parse_exponent(positive, significand, exponent),
            _ => self.f64_from_parts(positive, significand, exponent),
        }
    }

    fn parse_exponent(
        &mut self,
        positive: bool,
        significand: u64,
        starting_exp: i32,
    ) -> Result<f64> {
        self.eat_char();

        let positive_exp = match tri!(self.peek_or_null()) {
            b'+' => {
                self.eat_char();
                true
            }
            b'-' => {
                self.eat_char();
                false
            }
            _ => true,
        };

        let next = match tri!(self.next_char()) {
            Some(b) => b,
            None => {
                return Err(self.error(ErrorCode::EofWhileParsingValue));
            }
        };

        // Make sure a digit follows the exponent place.
        let mut exp = match next {
            c @ b'0'..=b'9' => (c - b'0') as i32,
            _ => {
                return Err(self.error(ErrorCode::InvalidNumber));
            }
        };

        while let c @ b'0'..=b'9' = tri!(self.peek_or_null()) {
            self.eat_char();
            let digit = (c - b'0') as i32;

            if overflow!(exp * 10 + digit, i32::MAX) {
                let zero_significand = significand == 0;
                return self.parse_exponent_overflow(positive, zero_significand, positive_exp);
            }

            exp = exp * 10 + digit;
        }

        let final_exp = if positive_exp {
            starting_exp.saturating_add(exp)
        } else {
            starting_exp.saturating_sub(exp)
        };

        self.f64_from_parts(positive, significand, final_exp)
    }

    #[cfg(feature = "float_roundtrip")]
    fn f64_from_parts(&mut self, positive: bool, significand: u64, exponent: i32) -> Result<f64> {
        let f = if self.single_precision {
            lexical::parse_concise_float::<f32>(significand, exponent) as f64
        } else {
            lexical::parse_concise_float::<f64>(significand, exponent)
        };

        if f.is_infinite() {
            Err(self.error(ErrorCode::NumberOutOfRange))
        } else {
            Ok(if positive { f } else { -f })
        }
    }

    #[cfg(not(feature = "float_roundtrip"))]
    fn f64_from_parts(
        &mut self,
        positive: bool,
        significand: u64,
        mut exponent: i32,
    ) -> Result<f64> {
        let mut f = significand as f64;
        loop {
            match POW10.get(exponent.wrapping_abs() as usize) {
                Some(&pow) => {
                    if exponent >= 0 {
                        f *= pow;
                        if f.is_infinite() {
                            return Err(self.error(ErrorCode::NumberOutOfRange));
                        }
                    } else {
                        f /= pow;
                    }
                    break;
                }
                None => {
                    if f == 0.0 {
                        break;
                    }
                    if exponent >= 0 {
                        return Err(self.error(ErrorCode::NumberOutOfRange));
                    }
                    f /= 1e308;
                    exponent += 308;
                }
            }
        }
        Ok(if positive { f } else { -f })
    }

    #[cfg(feature = "float_roundtrip")]
    #[cold]
    #[inline(never)]
    fn parse_long_integer(&mut self, positive: bool, partial_significand: u64) -> Result<f64> {
        // To deserialize floats we'll first push the integer and fraction
        // parts, both as byte strings, into the scratch buffer and then feed
        // both slices to lexical's parser. For example if the input is
        // `12.34e5` we'll push b"1234" into scratch and then pass b"12" and
        // b"34" to lexical. `integer_end` will be used to track where to split
        // the scratch buffer.
        //
        // Note that lexical expects the integer part to contain *no* leading
        // zeroes and the fraction part to contain *no* trailing zeroes. The
        // first requirement is already handled by the integer parsing logic.
        // The second requirement will be enforced just before passing the
        // slices to lexical in f64_long_from_parts.
        self.scratch.clear();
        self.scratch
            .extend_from_slice(itoa::Buffer::new().format(partial_significand).as_bytes());

        loop {
            match tri!(self.peek_or_null()) {
                c @ b'0'..=b'9' => {
                    self.scratch.push(c);
                    self.eat_char();
                }
                b'.' => {
                    self.eat_char();
                    return self.parse_long_decimal(positive, self.scratch.len());
                }
                b'e' | b'E' => {
                    return self.parse_long_exponent(positive, self.scratch.len());
                }
                _ => {
                    return self.f64_long_from_parts(positive, self.scratch.len(), 0);
                }
            }
        }
    }

    #[cfg(not(feature = "float_roundtrip"))]
    #[cold]
    #[inline(never)]
    fn parse_long_integer(&mut self, positive: bool, significand: u64) -> Result<f64> {
        let mut exponent = 0;
        loop {
            match tri!(self.peek_or_null()) {
                b'0'..=b'9' => {
                    self.eat_char();
                    // This could overflow... if your integer is gigabytes long.
                    // Ignore that possibility.
                    exponent += 1;
                }
                b'.' => {
                    return self.parse_decimal(positive, significand, exponent);
                }
                b'e' | b'E' => {
                    return self.parse_exponent(positive, significand, exponent);
                }
                _ => {
                    return self.f64_from_parts(positive, significand, exponent);
                }
            }
        }
    }

    #[cfg(feature = "float_roundtrip")]
    #[cold]
    fn parse_long_decimal(&mut self, positive: bool, integer_end: usize) -> Result<f64> {
        let mut at_least_one_digit = integer_end < self.scratch.len();
        while let c @ b'0'..=b'9' = tri!(self.peek_or_null()) {
            self.scratch.push(c);
            self.eat_char();
            at_least_one_digit = true;
        }

        if !at_least_one_digit {
            match tri!(self.peek()) {
                Some(_) => return Err(self.peek_error(ErrorCode::InvalidNumber)),
                None => return Err(self.peek_error(ErrorCode::EofWhileParsingValue)),
            }
        }

        match tri!(self.peek_or_null()) {
            b'e' | b'E' => self.parse_long_exponent(positive, integer_end),
            _ => self.f64_long_from_parts(positive, integer_end, 0),
        }
    }

    #[cfg(feature = "float_roundtrip")]
    fn parse_long_exponent(&mut self, positive: bool, integer_end: usize) -> Result<f64> {
        self.eat_char();

        let positive_exp = match tri!(self.peek_or_null()) {
            b'+' => {
                self.eat_char();
                true
            }
            b'-' => {
                self.eat_char();
                false
            }
            _ => true,
        };

        let next = match tri!(self.next_char()) {
            Some(b) => b,
            None => {
                return Err(self.error(ErrorCode::EofWhileParsingValue));
            }
        };

        // Make sure a digit follows the exponent place.
        let mut exp = match next {
            c @ b'0'..=b'9' => (c - b'0') as i32,
            _ => {
                return Err(self.error(ErrorCode::InvalidNumber));
            }
        };

        while let c @ b'0'..=b'9' = tri!(self.peek_or_null()) {
            self.eat_char();
            let digit = (c - b'0') as i32;

            if overflow!(exp * 10 + digit, i32::MAX) {
                let zero_significand = self.scratch.iter().all(|&digit| digit == b'0');
                return self.parse_exponent_overflow(positive, zero_significand, positive_exp);
            }

            exp = exp * 10 + digit;
        }

        let final_exp = if positive_exp { exp } else { -exp };

        self.f64_long_from_parts(positive, integer_end, final_exp)
    }

    // This cold code should not be inlined into the middle of the hot
    // decimal-parsing loop above.
    #[cfg(feature = "float_roundtrip")]
    #[cold]
    #[inline(never)]
    fn parse_decimal_overflow(
        &mut self,
        positive: bool,
        significand: u64,
        exponent: i32,
    ) -> Result<f64> {
        let mut buffer = itoa::Buffer::new();
        let significand = buffer.format(significand);
        let fraction_digits = -exponent as usize;
        self.scratch.clear();
        if let Some(zeros) = fraction_digits.checked_sub(significand.len() + 1) {
            self.scratch.extend(iter::repeat(b'0').take(zeros + 1));
        }
        self.scratch.extend_from_slice(significand.as_bytes());
        let integer_end = self.scratch.len() - fraction_digits;
        self.parse_long_decimal(positive, integer_end)
    }

    #[cfg(not(feature = "float_roundtrip"))]
    #[cold]
    #[inline(never)]
    fn parse_decimal_overflow(
        &mut self,
        positive: bool,
        significand: u64,
        exponent: i32,
    ) -> Result<f64> {
        // The next multiply/add would overflow, so just ignore all further
        // digits.
        while let b'0'..=b'9' = tri!(self.peek_or_null()) {
            self.eat_char();
        }

        match tri!(self.peek_or_null()) {
            b'e' | b'E' => self.parse_exponent(positive, significand, exponent),
            _ => self.f64_from_parts(positive, significand, exponent),
        }
    }

    // This cold code should not be inlined into the middle of the hot
    // exponent-parsing loop above.
    #[cold]
    #[inline(never)]
    fn parse_exponent_overflow(
        &mut self,
        positive: bool,
        zero_significand: bool,
        positive_exp: bool,
    ) -> Result<f64> {
        // Error instead of +/- infinity.
        if !zero_significand && positive_exp {
            return Err(self.error(ErrorCode::NumberOutOfRange));
        }

        while let b'0'..=b'9' = tri!(self.peek_or_null()) {
            self.eat_char();
        }
        Ok(if positive { 0.0 } else { -0.0 })
    }

    #[cfg(feature = "float_roundtrip")]
    fn f64_long_from_parts(
        &mut self,
        positive: bool,
        integer_end: usize,
        exponent: i32,
    ) -> Result<f64> {
        let integer = &self.scratch[..integer_end];
        let fraction = &self.scratch[integer_end..];

        let f = if self.single_precision {
            lexical::parse_truncated_float::<f32>(integer, fraction, exponent) as f64
        } else {
            lexical::parse_truncated_float::<f64>(integer, fraction, exponent)
        };

        if f.is_infinite() {
            Err(self.error(ErrorCode::NumberOutOfRange))
        } else {
            Ok(if positive { f } else { -f })
        }
    }

    fn parse_any_signed_number(&mut self) -> Result<ParserNumber> {
        let peek = match tri!(self.peek()) {
            Some(b) => b,
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        let value = match peek {
            b'-' => {
                self.eat_char();
                self.parse_any_number(false)
            }
            b'0'..=b'9' => self.parse_any_number(true),
            _ => Err(self.peek_error(ErrorCode::InvalidNumber)),
        };

        let value = match tri!(self.peek()) {
            Some(_) => Err(self.peek_error(ErrorCode::InvalidNumber)),
            None => value,
        };

        match value {
            Ok(value) => Ok(value),
            // The de::Error impl creates errors with unknown line and column.
            // Fill in the position here by looking at the current index in the
            // input. There is no way to tell whether this should call `error`
            // or `peek_error` so pick the one that seems correct more often.
            // Worst case, the position is off by one character.
            Err(err) => Err(self.fix_position(err)),
        }
    }

    #[cfg(not(feature = "arbitrary_precision"))]
    fn parse_any_number(&mut self, positive: bool) -> Result<ParserNumber> {
        self.parse_integer(positive)
    }

    #[cfg(feature = "arbitrary_precision")]
    fn parse_any_number(&mut self, positive: bool) -> Result<ParserNumber> {
        let mut buf = String::with_capacity(16);
        if !positive {
            buf.push('-');
        }
        tri!(self.scan_integer(&mut buf));
        if positive {
            if let Ok(unsigned) = buf.parse() {
                return Ok(ParserNumber::U64(unsigned));
            }
        } else {
            if let Ok(signed) = buf.parse() {
                return Ok(ParserNumber::I64(signed));
            }
        }
        Ok(ParserNumber::String(buf))
    }

    #[cfg(feature = "arbitrary_precision")]
    fn scan_or_eof(&mut self, buf: &mut String) -> Result<u8> {
        match tri!(self.next_char()) {
            Some(b) => {
                buf.push(b as char);
                Ok(b)
            }
            None => Err(self.error(ErrorCode::EofWhileParsingValue)),
        }
    }

    #[cfg(feature = "arbitrary_precision")]
    fn scan_integer(&mut self, buf: &mut String) -> Result<()> {
        match tri!(self.scan_or_eof(buf)) {
            b'0' => {
                // There can be only one leading '0'.
                match tri!(self.peek_or_null()) {
                    b'0'..=b'9' => Err(self.peek_error(ErrorCode::InvalidNumber)),
                    _ => self.scan_number(buf),
                }
            }
            b'1'..=b'9' => loop {
                match tri!(self.peek_or_null()) {
                    c @ b'0'..=b'9' => {
                        self.eat_char();
                        buf.push(c as char);
                    }
                    _ => {
                        return self.scan_number(buf);
                    }
                }
            },
            _ => Err(self.error(ErrorCode::InvalidNumber)),
        }
    }

    #[cfg(feature = "arbitrary_precision")]
    fn scan_number(&mut self, buf: &mut String) -> Result<()> {
        match tri!(self.peek_or_null()) {
            b'.' => self.scan_decimal(buf),
            e @ (b'e' | b'E') => self.scan_exponent(e as char, buf),
            _ => Ok(()),
        }
    }

    #[cfg(feature = "arbitrary_precision")]
    fn scan_decimal(&mut self, buf: &mut String) -> Result<()> {
        self.eat_char();
        buf.push('.');

        let mut at_least_one_digit = false;
        while let c @ b'0'..=b'9' = tri!(self.peek_or_null()) {
            self.eat_char();
            buf.push(c as char);
            at_least_one_digit = true;
        }

        if !at_least_one_digit {
            match tri!(self.peek()) {
                Some(_) => return Err(self.peek_error(ErrorCode::InvalidNumber)),
                None => return Err(self.peek_error(ErrorCode::EofWhileParsingValue)),
            }
        }

        match tri!(self.peek_or_null()) {
            e @ (b'e' | b'E') => self.scan_exponent(e as char, buf),
            _ => Ok(()),
        }
    }

    #[cfg(feature = "arbitrary_precision")]
    fn scan_exponent(&mut self, e: char, buf: &mut String) -> Result<()> {
        self.eat_char();
        buf.push(e);

        match tri!(self.peek_or_null()) {
            b'+' => {
                self.eat_char();
                buf.push('+');
            }
            b'-' => {
                self.eat_char();
                buf.push('-');
            }
            _ => {}
        }

        // Make sure a digit follows the exponent place.
        match tri!(self.scan_or_eof(buf)) {
            b'0'..=b'9' => {}
            _ => {
                return Err(self.error(ErrorCode::InvalidNumber));
            }
        }

        while let c @ b'0'..=b'9' = tri!(self.peek_or_null()) {
            self.eat_char();
            buf.push(c as char);
        }

        Ok(())
    }

    fn parse_object_colon(&mut self) -> Result<()> {
        match tri!(self.parse_whitespace()) {
            Some(b':') => {
                self.eat_char();
                Ok(())
            }
            Some(_) => Err(self.peek_error(ErrorCode::ExpectedColon)),
            None => Err(self.peek_error(ErrorCode::EofWhileParsingObject)),
        }
    }

    fn end_seq(&mut self) -> Result<()> {
        match tri!(self.parse_whitespace()) {
            Some(b']') => {
                self.eat_char();
                Ok(())
            }
            Some(b',') => {
                self.eat_char();
                match self.parse_whitespace() {
                    Ok(Some(b']')) => Err(self.peek_error(ErrorCode::TrailingComma)),
                    _ => Err(self.peek_error(ErrorCode::TrailingCharacters)),
                }
            }
            Some(_) => Err(self.peek_error(ErrorCode::TrailingCharacters)),
            None => Err(self.peek_error(ErrorCode::EofWhileParsingList)),
        }
    }

    fn end_map(&mut self) -> Result<()> {
        match tri!(self.parse_whitespace()) {
            Some(b'}') => {
                self.eat_char();
                Ok(())
            }
            Some(b',') => Err(self.peek_error(ErrorCode::TrailingComma)),
            Some(_) => Err(self.peek_error(ErrorCode::TrailingCharacters)),
            None => Err(self.peek_error(ErrorCode::EofWhileParsingObject)),
        }
    }

    fn ignore_value(&mut self) -> Result<()> {
        self.scratch.clear();
        let mut enclosing = None;

        loop {
            let peek = match tri!(self.parse_whitespace()) {
                Some(b) => b,
                None => {
                    return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
                }
            };

            let frame = match peek {
                b'n' => {
                    self.eat_char();
                    tri!(self.parse_ident(b"ull"));
                    None
                }
                b't' => {
                    self.eat_char();
                    tri!(self.parse_ident(b"rue"));
                    None
                }
                b'f' => {
                    self.eat_char();
                    tri!(self.parse_ident(b"alse"));
                    None
                }
                b'-' => {
                    self.eat_char();
                    tri!(self.ignore_integer());
                    None
                }
                b'0'..=b'9' => {
                    tri!(self.ignore_integer());
                    None
                }
                b'"' => {
                    self.eat_char();
                    tri!(self.read.ignore_str());
                    None
                }
                frame @ (b'[' | b'{') => {
                    self.scratch.extend(enclosing.take());
                    self.eat_char();
                    Some(frame)
                }
                _ => return Err(self.peek_error(ErrorCode::ExpectedSomeValue)),
            };

            let (mut accept_comma, mut frame) = match frame {
                Some(frame) => (false, frame),
                None => match enclosing.take() {
                    Some(frame) => (true, frame),
                    None => match self.scratch.pop() {
                        Some(frame) => (true, frame),
                        None => return Ok(()),
                    },
                },
            };

            loop {
                match tri!(self.parse_whitespace()) {
                    Some(b',') if accept_comma => {
                        self.eat_char();
                        break;
                    }
                    Some(b']') if frame == b'[' => {}
                    Some(b'}') if frame == b'{' => {}
                    Some(_) => {
                        if accept_comma {
                            return Err(self.peek_error(match frame {
                                b'[' => ErrorCode::ExpectedListCommaOrEnd,
                                b'{' => ErrorCode::ExpectedObjectCommaOrEnd,
                                _ => unreachable!(),
                            }));
                        } else {
                            break;
                        }
                    }
                    None => {
                        return Err(self.peek_error(match frame {
                            b'[' => ErrorCode::EofWhileParsingList,
                            b'{' => ErrorCode::EofWhileParsingObject,
                            _ => unreachable!(),
                        }));
                    }
                }

                self.eat_char();
                frame = match self.scratch.pop() {
                    Some(frame) => frame,
                    None => return Ok(()),
                };
                accept_comma = true;
            }

            if frame == b'{' {
                match tri!(self.parse_whitespace()) {
                    Some(b'"') => self.eat_char(),
                    Some(_) => return Err(self.peek_error(ErrorCode::KeyMustBeAString)),
                    None => return Err(self.peek_error(ErrorCode::EofWhileParsingObject)),
                }
                tri!(self.read.ignore_str());
                match tri!(self.parse_whitespace()) {
                    Some(b':') => self.eat_char(),
                    Some(_) => return Err(self.peek_error(ErrorCode::ExpectedColon)),
                    None => return Err(self.peek_error(ErrorCode::EofWhileParsingObject)),
                }
            }

            enclosing = Some(frame);
        }
    }

    fn ignore_integer(&mut self) -> Result<()> {
        match tri!(self.next_char_or_null()) {
            b'0' => {
                // There can be only one leading '0'.
                if let b'0'..=b'9' = tri!(self.peek_or_null()) {
                    return Err(self.peek_error(ErrorCode::InvalidNumber));
                }
            }
            b'1'..=b'9' => {
                while let b'0'..=b'9' = tri!(self.peek_or_null()) {
                    self.eat_char();
                }
            }
            _ => {
                return Err(self.error(ErrorCode::InvalidNumber));
            }
        }

        match tri!(self.peek_or_null()) {
            b'.' => self.ignore_decimal(),
            b'e' | b'E' => self.ignore_exponent(),
            _ => Ok(()),
        }
    }

    fn ignore_decimal(&mut self) -> Result<()> {
        self.eat_char();

        let mut at_least_one_digit = false;
        while let b'0'..=b'9' = tri!(self.peek_or_null()) {
            self.eat_char();
            at_least_one_digit = true;
        }

        if !at_least_one_digit {
            return Err(self.peek_error(ErrorCode::InvalidNumber));
        }

        match tri!(self.peek_or_null()) {
            b'e' | b'E' => self.ignore_exponent(),
            _ => Ok(()),
        }
    }

    fn ignore_exponent(&mut self) -> Result<()> {
        self.eat_char();

        match tri!(self.peek_or_null()) {
            b'+' | b'-' => self.eat_char(),
            _ => {}
        }

        // Make sure a digit follows the exponent place.
        match tri!(self.next_char_or_null()) {
            b'0'..=b'9' => {}
            _ => {
                return Err(self.error(ErrorCode::InvalidNumber));
            }
        }

        while let b'0'..=b'9' = tri!(self.peek_or_null()) {
            self.eat_char();
        }

        Ok(())
    }

    #[cfg(feature = "raw_value")]
    fn deserialize_raw_value<V>(&mut self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        tri!(self.parse_whitespace());
        self.read.begin_raw_buffering();
        tri!(self.ignore_value());
        self.read.end_raw_buffering(visitor)
    }
}

impl FromStr for Number {
    type Err = Error;

    fn from_str(s: &str) -> result::Result<Self, Self::Err> {
        Deserializer::from_str(s)
            .parse_any_signed_number()
            .map(Into::into)
    }
}

#[cfg(not(feature = "float_roundtrip"))]
static POW10: [f64; 309] = [
    1e000, 1e001, 1e002, 1e003, 1e004, 1e005, 1e006, 1e007, 1e008, 1e009, //
    1e010, 1e011, 1e012, 1e013, 1e014, 1e015, 1e016, 1e017, 1e018, 1e019, //
    1e020, 1e021, 1e022, 1e023, 1e024, 1e025, 1e026, 1e027, 1e028, 1e029, //
    1e030, 1e031, 1e032, 1e033, 1e034, 1e035, 1e036, 1e037, 1e038, 1e039, //
    1e040, 1e041, 1e042, 1e043, 1e044, 1e045, 1e046, 1e047, 1e048, 1e049, //
    1e050, 1e051, 1e052, 1e053, 1e054, 1e055, 1e056, 1e057, 1e058, 1e059, //
    1e060, 1e061, 1e062, 1e063, 1e064, 1e065, 1e066, 1e067, 1e068, 1e069, //
    1e070, 1e071, 1e072, 1e073, 1e074, 1e075, 1e076, 1e077, 1e078, 1e079, //
    1e080, 1e081, 1e082, 1e083, 1e084, 1e085, 1e086, 1e087, 1e088, 1e089, //
    1e090, 1e091, 1e092, 1e093, 1e094, 1e095, 1e096, 1e097, 1e098, 1e099, //
    1e100, 1e101, 1e102, 1e103, 1e104, 1e105, 1e106, 1e107, 1e108, 1e109, //
    1e110, 1e111, 1e112, 1e113, 1e114, 1e115, 1e116, 1e117, 1e118, 1e119, //
    1e120, 1e121, 1e122, 1e123, 1e124, 1e125, 1e126, 1e127, 1e128, 1e129, //
    1e130, 1e131, 1e132, 1e133, 1e134, 1e135, 1e136, 1e137, 1e138, 1e139, //
    1e140, 1e141, 1e142, 1e143, 1e144, 1e145, 1e146, 1e147, 1e148, 1e149, //
    1e150, 1e151, 1e152, 1e153, 1e154, 1e155, 1e156, 1e157, 1e158, 1e159, //
    1e160, 1e161, 1e162, 1e163, 1e164, 1e165, 1e166, 1e167, 1e168, 1e169, //
    1e170, 1e171, 1e172, 1e173, 1e174, 1e175, 1e176, 1e177, 1e178, 1e179, //
    1e180, 1e181, 1e182, 1e183, 1e184, 1e185, 1e186, 1e187, 1e188, 1e189, //
    1e190, 1e191, 1e192, 1e193, 1e194, 1e195, 1e196, 1e197, 1e198, 1e199, //
    1e200, 1e201, 1e202, 1e203, 1e204, 1e205, 1e206, 1e207, 1e208, 1e209, //
    1e210, 1e211, 1e212, 1e213, 1e214, 1e215, 1e216, 1e217, 1e218, 1e219, //
    1e220, 1e221, 1e222, 1e223, 1e224, 1e225, 1e226, 1e227, 1e228, 1e229, //
    1e230, 1e231, 1e232, 1e233, 1e234, 1e235, 1e236, 1e237, 1e238, 1e239, //
    1e240, 1e241, 1e242, 1e243, 1e244, 1e245, 1e246, 1e247, 1e248, 1e249, //
    1e250, 1e251, 1e252, 1e253, 1e254, 1e255, 1e256, 1e257, 1e258, 1e259, //
    1e260, 1e261, 1e262, 1e263, 1e264, 1e265, 1e266, 1e267, 1e268, 1e269, //
    1e270, 1e271, 1e272, 1e273, 1e274, 1e275, 1e276, 1e277, 1e278, 1e279, //
    1e280, 1e281, 1e282, 1e283, 1e284, 1e285, 1e286, 1e287, 1e288, 1e289, //
    1e290, 1e291, 1e292, 1e293, 1e294, 1e295, 1e296, 1e297, 1e298, 1e299, //
    1e300, 1e301, 1e302, 1e303, 1e304, 1e305, 1e306, 1e307, 1e308,
];

macro_rules! deserialize_number {
    ($method:ident) => {
        deserialize_number!($method, deserialize_number);
    };

    ($method:ident, $using:ident) => {
        fn $method<V>(self, visitor: V) -> Result<V::Value>
        where
            V: de::Visitor<'de>,
        {
            self.$using(visitor)
        }
    };
}

#[cfg(not(feature = "unbounded_depth"))]
macro_rules! if_checking_recursion_limit {
    ($($body:tt)*) => {
        $($body)*
    };
}

#[cfg(feature = "unbounded_depth")]
macro_rules! if_checking_recursion_limit {
    ($this:ident $($body:tt)*) => {
        if !$this.disable_recursion_limit {
            $this $($body)*
        }
    };
}

macro_rules! check_recursion {
    ($this:ident $($body:tt)*) => {
        if_checking_recursion_limit! {
            $this.remaining_depth -= 1;
            if $this.remaining_depth == 0 {
                return Err($this.peek_error(ErrorCode::RecursionLimitExceeded));
            }
        }

        $this $($body)*

        if_checking_recursion_limit! {
            $this.remaining_depth += 1;
        }
    };
}

impl<'de, 'a, R: Read<'de>> de::Deserializer<'de> for &'a mut Deserializer<R> {
    type Error = Error;

    #[inline]
    fn deserialize_any<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        let peek = match tri!(self.parse_whitespace()) {
            Some(b) => b,
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        let value = match peek {
            b'n' => {
                self.eat_char();
                tri!(self.parse_ident(b"ull"));
                visitor.visit_unit()
            }
            b't' => {
                self.eat_char();
                tri!(self.parse_ident(b"rue"));
                visitor.visit_bool(true)
            }
            b'f' => {
                self.eat_char();
                tri!(self.parse_ident(b"alse"));
                visitor.visit_bool(false)
            }
            b'-' => {
                self.eat_char();
                tri!(self.parse_any_number(false)).visit(visitor)
            }
            b'0'..=b'9' => tri!(self.parse_any_number(true)).visit(visitor),
            b'"' => {
                self.eat_char();
                self.scratch.clear();
                match tri!(self.read.parse_str(&mut self.scratch)) {
                    Reference::Borrowed(s) => visitor.visit_borrowed_str(s),
                    Reference::Copied(s) => visitor.visit_str(s),
                }
            }
            b'[' => {
                check_recursion! {
                    self.eat_char();
                    let ret = visitor.visit_seq(SeqAccess::new(self));
                }

                match (ret, self.end_seq()) {
                    (Ok(ret), Ok(())) => Ok(ret),
                    (Err(err), _) | (_, Err(err)) => Err(err),
                }
            }
            b'{' => {
                check_recursion! {
                    self.eat_char();
                    let ret = visitor.visit_map(MapAccess::new(self));
                }

                match (ret, self.end_map()) {
                    (Ok(ret), Ok(())) => Ok(ret),
                    (Err(err), _) | (_, Err(err)) => Err(err),
                }
            }
            _ => Err(self.peek_error(ErrorCode::ExpectedSomeValue)),
        };

        match value {
            Ok(value) => Ok(value),
            // The de::Error impl creates errors with unknown line and column.
            // Fill in the position here by looking at the current index in the
            // input. There is no way to tell whether this should call `error`
            // or `peek_error` so pick the one that seems correct more often.
            // Worst case, the position is off by one character.
            Err(err) => Err(self.fix_position(err)),
        }
    }

    fn deserialize_bool<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        let peek = match tri!(self.parse_whitespace()) {
            Some(b) => b,
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        let value = match peek {
            b't' => {
                self.eat_char();
                tri!(self.parse_ident(b"rue"));
                visitor.visit_bool(true)
            }
            b'f' => {
                self.eat_char();
                tri!(self.parse_ident(b"alse"));
                visitor.visit_bool(false)
            }
            _ => Err(self.peek_invalid_type(&visitor)),
        };

        match value {
            Ok(value) => Ok(value),
            Err(err) => Err(self.fix_position(err)),
        }
    }

    deserialize_number!(deserialize_i8);
    deserialize_number!(deserialize_i16);
    deserialize_number!(deserialize_i32);
    deserialize_number!(deserialize_i64);
    deserialize_number!(deserialize_u8);
    deserialize_number!(deserialize_u16);
    deserialize_number!(deserialize_u32);
    deserialize_number!(deserialize_u64);
    #[cfg(not(feature = "float_roundtrip"))]
    deserialize_number!(deserialize_f32);
    deserialize_number!(deserialize_f64);

    #[cfg(feature = "float_roundtrip")]
    deserialize_number!(deserialize_f32, do_deserialize_f32);
    deserialize_number!(deserialize_i128, do_deserialize_i128);
    deserialize_number!(deserialize_u128, do_deserialize_u128);

    fn deserialize_char<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.deserialize_str(visitor)
    }

    fn deserialize_str<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        let peek = match tri!(self.parse_whitespace()) {
            Some(b) => b,
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        let value = match peek {
            b'"' => {
                self.eat_char();
                self.scratch.clear();
                match tri!(self.read.parse_str(&mut self.scratch)) {
                    Reference::Borrowed(s) => visitor.visit_borrowed_str(s),
                    Reference::Copied(s) => visitor.visit_str(s),
                }
            }
            _ => Err(self.peek_invalid_type(&visitor)),
        };

        match value {
            Ok(value) => Ok(value),
            Err(err) => Err(self.fix_position(err)),
        }
    }

    fn deserialize_string<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.deserialize_str(visitor)
    }

    /// Parses a JSON string as bytes. Note that this function does not check
    /// whether the bytes represent a valid UTF-8 string.
    ///
    /// The relevant part of the JSON specification is Section 8.2 of [RFC
    /// 7159]:
    ///
    /// > When all the strings represented in a JSON text are composed entirely
    /// > of Unicode characters (however escaped), then that JSON text is
    /// > interoperable in the sense that all software implementations that
    /// > parse it will agree on the contents of names and of string values in
    /// > objects and arrays.
    /// >
    /// > However, the ABNF in this specification allows member names and string
    /// > values to contain bit sequences that cannot encode Unicode characters;
    /// > for example, "\uDEAD" (a single unpaired UTF-16 surrogate). Instances
    /// > of this have been observed, for example, when a library truncates a
    /// > UTF-16 string without checking whether the truncation split a
    /// > surrogate pair.  The behavior of software that receives JSON texts
    /// > containing such values is unpredictable; for example, implementations
    /// > might return different values for the length of a string value or even
    /// > suffer fatal runtime exceptions.
    ///
    /// [RFC 7159]: https://tools.ietf.org/html/rfc7159
    ///
    /// The behavior of serde_json is specified to fail on non-UTF-8 strings
    /// when deserializing into Rust UTF-8 string types such as String, and
    /// succeed with non-UTF-8 bytes when deserializing using this method.
    ///
    /// Escape sequences are processed as usual, and for `\uXXXX` escapes it is
    /// still checked if the hex number represents a valid Unicode code point.
    ///
    /// # Examples
    ///
    /// You can use this to parse JSON strings containing invalid UTF-8 bytes,
    /// or unpaired surrogates.
    ///
    /// ```
    /// use serde_bytes::ByteBuf;
    ///
    /// fn look_at_bytes() -> Result<(), serde_json::Error> {
    ///     let json_data = b"\"some bytes: \xe5\x00\xe5\"";
    ///     let bytes: ByteBuf = serde_json::from_slice(json_data)?;
    ///
    ///     assert_eq!(b'\xe5', bytes[12]);
    ///     assert_eq!(b'\0', bytes[13]);
    ///     assert_eq!(b'\xe5', bytes[14]);
    ///
    ///     Ok(())
    /// }
    /// #
    /// # look_at_bytes().unwrap();
    /// ```
    ///
    /// Backslash escape sequences like `\n` are still interpreted and required
    /// to be valid. `\u` escape sequences are required to represent a valid
    /// Unicode code point or lone surrogate.
    ///
    /// ```
    /// use serde_bytes::ByteBuf;
    ///
    /// fn look_at_bytes() -> Result<(), serde_json::Error> {
    ///     let json_data = b"\"lone surrogate: \\uD801\"";
    ///     let bytes: ByteBuf = serde_json::from_slice(json_data)?;
    ///     let expected = b"lone surrogate: \xED\xA0\x81";
    ///     assert_eq!(expected, bytes.as_slice());
    ///     Ok(())
    /// }
    /// #
    /// # look_at_bytes();
    /// ```
    fn deserialize_bytes<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        let peek = match tri!(self.parse_whitespace()) {
            Some(b) => b,
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        let value = match peek {
            b'"' => {
                self.eat_char();
                self.scratch.clear();
                match tri!(self.read.parse_str_raw(&mut self.scratch)) {
                    Reference::Borrowed(b) => visitor.visit_borrowed_bytes(b),
                    Reference::Copied(b) => visitor.visit_bytes(b),
                }
            }
            b'[' => self.deserialize_seq(visitor),
            _ => Err(self.peek_invalid_type(&visitor)),
        };

        match value {
            Ok(value) => Ok(value),
            Err(err) => Err(self.fix_position(err)),
        }
    }

    #[inline]
    fn deserialize_byte_buf<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.deserialize_bytes(visitor)
    }

    /// Parses a `null` as a None, and any other values as a `Some(...)`.
    #[inline]
    fn deserialize_option<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        match tri!(self.parse_whitespace()) {
            Some(b'n') => {
                self.eat_char();
                tri!(self.parse_ident(b"ull"));
                visitor.visit_none()
            }
            _ => visitor.visit_some(self),
        }
    }

    fn deserialize_unit<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        let peek = match tri!(self.parse_whitespace()) {
            Some(b) => b,
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        let value = match peek {
            b'n' => {
                self.eat_char();
                tri!(self.parse_ident(b"ull"));
                visitor.visit_unit()
            }
            _ => Err(self.peek_invalid_type(&visitor)),
        };

        match value {
            Ok(value) => Ok(value),
            Err(err) => Err(self.fix_position(err)),
        }
    }

    fn deserialize_unit_struct<V>(self, _name: &'static str, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.deserialize_unit(visitor)
    }

    /// Parses a newtype struct as the underlying value.
    #[inline]
    fn deserialize_newtype_struct<V>(self, name: &str, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        #[cfg(feature = "raw_value")]
        {
            if name == crate::raw::TOKEN {
                return self.deserialize_raw_value(visitor);
            }
        }

        let _ = name;
        visitor.visit_newtype_struct(self)
    }

    fn deserialize_seq<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        let peek = match tri!(self.parse_whitespace()) {
            Some(b) => b,
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        let value = match peek {
            b'[' => {
                check_recursion! {
                    self.eat_char();
                    let ret = visitor.visit_seq(SeqAccess::new(self));
                }

                match (ret, self.end_seq()) {
                    (Ok(ret), Ok(())) => Ok(ret),
                    (Err(err), _) | (_, Err(err)) => Err(err),
                }
            }
            _ => Err(self.peek_invalid_type(&visitor)),
        };

        match value {
            Ok(value) => Ok(value),
            Err(err) => Err(self.fix_position(err)),
        }
    }

    fn deserialize_tuple<V>(self, _len: usize, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.deserialize_seq(visitor)
    }

    fn deserialize_tuple_struct<V>(
        self,
        _name: &'static str,
        _len: usize,
        visitor: V,
    ) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.deserialize_seq(visitor)
    }

    fn deserialize_map<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        let peek = match tri!(self.parse_whitespace()) {
            Some(b) => b,
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        let value = match peek {
            b'{' => {
                check_recursion! {
                    self.eat_char();
                    let ret = visitor.visit_map(MapAccess::new(self));
                }

                match (ret, self.end_map()) {
                    (Ok(ret), Ok(())) => Ok(ret),
                    (Err(err), _) | (_, Err(err)) => Err(err),
                }
            }
            _ => Err(self.peek_invalid_type(&visitor)),
        };

        match value {
            Ok(value) => Ok(value),
            Err(err) => Err(self.fix_position(err)),
        }
    }

    fn deserialize_struct<V>(
        self,
        _name: &'static str,
        _fields: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        let peek = match tri!(self.parse_whitespace()) {
            Some(b) => b,
            None => {
                return Err(self.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        let value = match peek {
            b'[' => {
                check_recursion! {
                    self.eat_char();
                    let ret = visitor.visit_seq(SeqAccess::new(self));
                }

                match (ret, self.end_seq()) {
                    (Ok(ret), Ok(())) => Ok(ret),
                    (Err(err), _) | (_, Err(err)) => Err(err),
                }
            }
            b'{' => {
                check_recursion! {
                    self.eat_char();
                    let ret = visitor.visit_map(MapAccess::new(self));
                }

                match (ret, self.end_map()) {
                    (Ok(ret), Ok(())) => Ok(ret),
                    (Err(err), _) | (_, Err(err)) => Err(err),
                }
            }
            _ => Err(self.peek_invalid_type(&visitor)),
        };

        match value {
            Ok(value) => Ok(value),
            Err(err) => Err(self.fix_position(err)),
        }
    }

    /// Parses an enum as an object like `{"$KEY":$VALUE}`, where $VALUE is either a straight
    /// value, a `[..]`, or a `{..}`.
    #[inline]
    fn deserialize_enum<V>(
        self,
        _name: &str,
        _variants: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        match tri!(self.parse_whitespace()) {
            Some(b'{') => {
                check_recursion! {
                    self.eat_char();
                    let value = tri!(visitor.visit_enum(VariantAccess::new(self)));
                }

                match tri!(self.parse_whitespace()) {
                    Some(b'}') => {
                        self.eat_char();
                        Ok(value)
                    }
                    Some(_) => Err(self.error(ErrorCode::ExpectedSomeValue)),
                    None => Err(self.error(ErrorCode::EofWhileParsingObject)),
                }
            }
            Some(b'"') => visitor.visit_enum(UnitVariantAccess::new(self)),
            Some(_) => Err(self.peek_error(ErrorCode::ExpectedSomeValue)),
            None => Err(self.peek_error(ErrorCode::EofWhileParsingValue)),
        }
    }

    fn deserialize_identifier<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.deserialize_str(visitor)
    }

    fn deserialize_ignored_any<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        tri!(self.ignore_value());
        visitor.visit_unit()
    }
}

struct SeqAccess<'a, R: 'a> {
    de: &'a mut Deserializer<R>,
    first: bool,
}

impl<'a, R: 'a> SeqAccess<'a, R> {
    fn new(de: &'a mut Deserializer<R>) -> Self {
        SeqAccess { de, first: true }
    }
}

impl<'de, 'a, R: Read<'de> + 'a> de::SeqAccess<'de> for SeqAccess<'a, R> {
    type Error = Error;

    fn next_element_seed<T>(&mut self, seed: T) -> Result<Option<T::Value>>
    where
        T: de::DeserializeSeed<'de>,
    {
        let peek = match tri!(self.de.parse_whitespace()) {
            Some(b']') => {
                return Ok(None);
            }
            Some(b',') if !self.first => {
                self.de.eat_char();
                tri!(self.de.parse_whitespace())
            }
            Some(b) => {
                if self.first {
                    self.first = false;
                    Some(b)
                } else {
                    return Err(self.de.peek_error(ErrorCode::ExpectedListCommaOrEnd));
                }
            }
            None => {
                return Err(self.de.peek_error(ErrorCode::EofWhileParsingList));
            }
        };

        match peek {
            Some(b']') => Err(self.de.peek_error(ErrorCode::TrailingComma)),
            Some(_) => Ok(Some(tri!(seed.deserialize(&mut *self.de)))),
            None => Err(self.de.peek_error(ErrorCode::EofWhileParsingValue)),
        }
    }
}

struct MapAccess<'a, R: 'a> {
    de: &'a mut Deserializer<R>,
    first: bool,
}

impl<'a, R: 'a> MapAccess<'a, R> {
    fn new(de: &'a mut Deserializer<R>) -> Self {
        MapAccess { de, first: true }
    }
}

impl<'de, 'a, R: Read<'de> + 'a> de::MapAccess<'de> for MapAccess<'a, R> {
    type Error = Error;

    fn next_key_seed<K>(&mut self, seed: K) -> Result<Option<K::Value>>
    where
        K: de::DeserializeSeed<'de>,
    {
        let peek = match tri!(self.de.parse_whitespace()) {
            Some(b'}') => {
                return Ok(None);
            }
            Some(b',') if !self.first => {
                self.de.eat_char();
                tri!(self.de.parse_whitespace())
            }
            Some(b) => {
                if self.first {
                    self.first = false;
                    Some(b)
                } else {
                    return Err(self.de.peek_error(ErrorCode::ExpectedObjectCommaOrEnd));
                }
            }
            None => {
                return Err(self.de.peek_error(ErrorCode::EofWhileParsingObject));
            }
        };

        match peek {
            Some(b'"') => seed.deserialize(MapKey { de: &mut *self.de }).map(Some),
            Some(b'}') => Err(self.de.peek_error(ErrorCode::TrailingComma)),
            Some(_) => Err(self.de.peek_error(ErrorCode::KeyMustBeAString)),
            None => Err(self.de.peek_error(ErrorCode::EofWhileParsingValue)),
        }
    }

    fn next_value_seed<V>(&mut self, seed: V) -> Result<V::Value>
    where
        V: de::DeserializeSeed<'de>,
    {
        tri!(self.de.parse_object_colon());

        seed.deserialize(&mut *self.de)
    }
}

struct VariantAccess<'a, R: 'a> {
    de: &'a mut Deserializer<R>,
}

impl<'a, R: 'a> VariantAccess<'a, R> {
    fn new(de: &'a mut Deserializer<R>) -> Self {
        VariantAccess { de }
    }
}

impl<'de, 'a, R: Read<'de> + 'a> de::EnumAccess<'de> for VariantAccess<'a, R> {
    type Error = Error;
    type Variant = Self;

    fn variant_seed<V>(self, seed: V) -> Result<(V::Value, Self)>
    where
        V: de::DeserializeSeed<'de>,
    {
        let val = tri!(seed.deserialize(&mut *self.de));
        tri!(self.de.parse_object_colon());
        Ok((val, self))
    }
}

impl<'de, 'a, R: Read<'de> + 'a> de::VariantAccess<'de> for VariantAccess<'a, R> {
    type Error = Error;

    fn unit_variant(self) -> Result<()> {
        de::Deserialize::deserialize(self.de)
    }

    fn newtype_variant_seed<T>(self, seed: T) -> Result<T::Value>
    where
        T: de::DeserializeSeed<'de>,
    {
        seed.deserialize(self.de)
    }

    fn tuple_variant<V>(self, _len: usize, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        de::Deserializer::deserialize_seq(self.de, visitor)
    }

    fn struct_variant<V>(self, fields: &'static [&'static str], visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        de::Deserializer::deserialize_struct(self.de, "", fields, visitor)
    }
}

struct UnitVariantAccess<'a, R: 'a> {
    de: &'a mut Deserializer<R>,
}

impl<'a, R: 'a> UnitVariantAccess<'a, R> {
    fn new(de: &'a mut Deserializer<R>) -> Self {
        UnitVariantAccess { de }
    }
}

impl<'de, 'a, R: Read<'de> + 'a> de::EnumAccess<'de> for UnitVariantAccess<'a, R> {
    type Error = Error;
    type Variant = Self;

    fn variant_seed<V>(self, seed: V) -> Result<(V::Value, Self)>
    where
        V: de::DeserializeSeed<'de>,
    {
        let variant = tri!(seed.deserialize(&mut *self.de));
        Ok((variant, self))
    }
}

impl<'de, 'a, R: Read<'de> + 'a> de::VariantAccess<'de> for UnitVariantAccess<'a, R> {
    type Error = Error;

    fn unit_variant(self) -> Result<()> {
        Ok(())
    }

    fn newtype_variant_seed<T>(self, _seed: T) -> Result<T::Value>
    where
        T: de::DeserializeSeed<'de>,
    {
        Err(de::Error::invalid_type(
            Unexpected::UnitVariant,
            &"newtype variant",
        ))
    }

    fn tuple_variant<V>(self, _len: usize, _visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        Err(de::Error::invalid_type(
            Unexpected::UnitVariant,
            &"tuple variant",
        ))
    }

    fn struct_variant<V>(self, _fields: &'static [&'static str], _visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        Err(de::Error::invalid_type(
            Unexpected::UnitVariant,
            &"struct variant",
        ))
    }
}

/// Only deserialize from this after peeking a '"' byte! Otherwise it may
/// deserialize invalid JSON successfully.
struct MapKey<'a, R: 'a> {
    de: &'a mut Deserializer<R>,
}

macro_rules! deserialize_numeric_key {
    ($method:ident) => {
        fn $method<V>(self, visitor: V) -> Result<V::Value>
        where
            V: de::Visitor<'de>,
        {
            self.deserialize_number(visitor)
        }
    };

    ($method:ident, $delegate:ident) => {
        fn $method<V>(self, visitor: V) -> Result<V::Value>
        where
            V: de::Visitor<'de>,
        {
            self.de.eat_char();

            match tri!(self.de.peek()) {
                Some(b'0'..=b'9' | b'-') => {}
                _ => return Err(self.de.error(ErrorCode::ExpectedNumericKey)),
            }

            let value = tri!(self.de.$delegate(visitor));

            match tri!(self.de.peek()) {
                Some(b'"') => self.de.eat_char(),
                _ => return Err(self.de.peek_error(ErrorCode::ExpectedDoubleQuote)),
            }

            Ok(value)
        }
    };
}

impl<'de, 'a, R> MapKey<'a, R>
where
    R: Read<'de>,
{
    deserialize_numeric_key!(deserialize_number, deserialize_number);
}

impl<'de, 'a, R> de::Deserializer<'de> for MapKey<'a, R>
where
    R: Read<'de>,
{
    type Error = Error;

    #[inline]
    fn deserialize_any<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.de.eat_char();
        self.de.scratch.clear();
        match tri!(self.de.read.parse_str(&mut self.de.scratch)) {
            Reference::Borrowed(s) => visitor.visit_borrowed_str(s),
            Reference::Copied(s) => visitor.visit_str(s),
        }
    }

    deserialize_numeric_key!(deserialize_i8);
    deserialize_numeric_key!(deserialize_i16);
    deserialize_numeric_key!(deserialize_i32);
    deserialize_numeric_key!(deserialize_i64);
    deserialize_numeric_key!(deserialize_i128, deserialize_i128);
    deserialize_numeric_key!(deserialize_u8);
    deserialize_numeric_key!(deserialize_u16);
    deserialize_numeric_key!(deserialize_u32);
    deserialize_numeric_key!(deserialize_u64);
    deserialize_numeric_key!(deserialize_u128, deserialize_u128);
    #[cfg(not(feature = "float_roundtrip"))]
    deserialize_numeric_key!(deserialize_f32);
    #[cfg(feature = "float_roundtrip")]
    deserialize_numeric_key!(deserialize_f32, deserialize_f32);
    deserialize_numeric_key!(deserialize_f64);

    fn deserialize_bool<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.de.eat_char();

        let peek = match tri!(self.de.next_char()) {
            Some(b) => b,
            None => {
                return Err(self.de.peek_error(ErrorCode::EofWhileParsingValue));
            }
        };

        let value = match peek {
            b't' => {
                tri!(self.de.parse_ident(b"rue\""));
                visitor.visit_bool(true)
            }
            b'f' => {
                tri!(self.de.parse_ident(b"alse\""));
                visitor.visit_bool(false)
            }
            _ => {
                self.de.scratch.clear();
                let s = tri!(self.de.read.parse_str(&mut self.de.scratch));
                Err(de::Error::invalid_type(Unexpected::Str(&s), &visitor))
            }
        };

        match value {
            Ok(value) => Ok(value),
            Err(err) => Err(self.de.fix_position(err)),
        }
    }

    #[inline]
    fn deserialize_option<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        // Map keys cannot be null.
        visitor.visit_some(self)
    }

    #[inline]
    fn deserialize_newtype_struct<V>(self, name: &'static str, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        #[cfg(feature = "raw_value")]
        {
            if name == crate::raw::TOKEN {
                return self.de.deserialize_raw_value(visitor);
            }
        }

        let _ = name;
        visitor.visit_newtype_struct(self)
    }

    #[inline]
    fn deserialize_enum<V>(
        self,
        name: &'static str,
        variants: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.de.deserialize_enum(name, variants, visitor)
    }

    #[inline]
    fn deserialize_bytes<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.de.deserialize_bytes(visitor)
    }

    #[inline]
    fn deserialize_byte_buf<V>(self, visitor: V) -> Result<V::Value>
    where
        V: de::Visitor<'de>,
    {
        self.de.deserialize_bytes(visitor)
    }

    forward_to_deserialize_any! {
        char str string unit unit_struct seq tuple tuple_struct map struct
        identifier ignored_any
    }
}

//////////////////////////////////////////////////////////////////////////////

/// Iterator that deserializes a stream into multiple JSON values.
///
/// A stream deserializer can be created from any JSON deserializer using the
/// `Deserializer::into_iter` method.
///
/// The data can consist of any JSON value. Values need to be a self-delineating value e.g.
/// arrays, objects, or strings, or be followed by whitespace or a self-delineating value.
///
/// ```
/// use serde_json::{Deserializer, Value};
///
/// fn main() {
///     let data = "{\"k\": 3}1\"cool\"\"stuff\" 3{}  [0, 1, 2]";
///
///     let stream = Deserializer::from_str(data).into_iter::<Value>();
///
///     for value in stream {
///         println!("{}", value.unwrap());
///     }
/// }
/// ```
pub struct StreamDeserializer<'de, R, T> {
    de: Deserializer<R>,
    offset: usize,
    failed: bool,
    output: PhantomData<T>,
    lifetime: PhantomData<&'de ()>,
}

impl<'de, R, T> StreamDeserializer<'de, R, T>
where
    R: read::Read<'de>,
    T: de::Deserialize<'de>,
{
    /// Create a JSON stream deserializer from one of the possible serde_json
    /// input sources.
    ///
    /// Typically it is more convenient to use one of these methods instead:
    ///
    ///   - Deserializer::from_str(...).into_iter()
    ///   - Deserializer::from_slice(...).into_iter()
    ///   - Deserializer::from_reader(...).into_iter()
    pub fn new(read: R) -> Self {
        let offset = read.byte_offset();
        StreamDeserializer {
            de: Deserializer::new(read),
            offset,
            failed: false,
            output: PhantomData,
            lifetime: PhantomData,
        }
    }

    /// Returns the number of bytes so far deserialized into a successful `T`.
    ///
    /// If a stream deserializer returns an EOF error, new data can be joined to
    /// `old_data[stream.byte_offset()..]` to try again.
    ///
    /// ```
    /// let data = b"[0] [1] [";
    ///
    /// let de = serde_json::Deserializer::from_slice(data);
    /// let mut stream = de.into_iter::<Vec<i32>>();
    /// assert_eq!(0, stream.byte_offset());
    ///
    /// println!("{:?}", stream.next()); // [0]
    /// assert_eq!(3, stream.byte_offset());
    ///
    /// println!("{:?}", stream.next()); // [1]
    /// assert_eq!(7, stream.byte_offset());
    ///
    /// println!("{:?}", stream.next()); // error
    /// assert_eq!(8, stream.byte_offset());
    ///
    /// // If err.is_eof(), can join the remaining data to new data and continue.
    /// let remaining = &data[stream.byte_offset()..];
    /// ```
    ///
    /// *Note:* In the future this method may be changed to return the number of
    /// bytes so far deserialized into a successful T *or* syntactically valid
    /// JSON skipped over due to a type error. See [serde-rs/json#70] for an
    /// example illustrating this.
    ///
    /// [serde-rs/json#70]: https://github.com/serde-rs/json/issues/70
    pub fn byte_offset(&self) -> usize {
        self.offset
    }

    fn peek_end_of_value(&mut self) -> Result<()> {
        match tri!(self.de.peek()) {
            Some(b' ' | b'\n' | b'\t' | b'\r' | b'"' | b'[' | b']' | b'{' | b'}' | b',' | b':')
            | None => Ok(()),
            Some(_) => {
                let position = self.de.read.peek_position();
                Err(Error::syntax(
                    ErrorCode::TrailingCharacters,
                    position.line,
                    position.column,
                ))
            }
        }
    }
}

impl<'de, R, T> Iterator for StreamDeserializer<'de, R, T>
where
    R: Read<'de>,
    T: de::Deserialize<'de>,
{
    type Item = Result<T>;

    fn next(&mut self) -> Option<Result<T>> {
        if R::should_early_return_if_failed && self.failed {
            return None;
        }

        // skip whitespaces, if any
        // this helps with trailing whitespaces, since whitespaces between
        // values are handled for us.
        match self.de.parse_whitespace() {
            Ok(None) => {
                self.offset = self.de.read.byte_offset();
                None
            }
            Ok(Some(b)) => {
                // If the value does not have a clear way to show the end of the value
                // (like numbers, null, true etc.) we have to look for whitespace or
                // the beginning of a self-delineated value.
                let self_delineated_value = match b {
                    b'[' | b'"' | b'{' => true,
                    _ => false,
                };
                self.offset = self.de.read.byte_offset();
                let result = de::Deserialize::deserialize(&mut self.de);

                Some(match result {
                    Ok(value) => {
                        self.offset = self.de.read.byte_offset();
                        if self_delineated_value {
                            Ok(value)
                        } else {
                            self.peek_end_of_value().map(|()| value)
                        }
                    }
                    Err(e) => {
                        self.de.read.set_failed(&mut self.failed);
                        Err(e)
                    }
                })
            }
            Err(e) => {
                self.de.read.set_failed(&mut self.failed);
                Some(Err(e))
            }
        }
    }
}

impl<'de, R, T> FusedIterator for StreamDeserializer<'de, R, T>
where
    R: Read<'de> + Fused,
    T: de::Deserialize<'de>,
{
}

//////////////////////////////////////////////////////////////////////////////

fn from_trait<'de, R, T>(read: R) -> Result<T>
where
    R: Read<'de>,
    T: de::Deserialize<'de>,
{
    let mut de = Deserializer::new(read);
    let value = tri!(de::Deserialize::deserialize(&mut de));

    // Make sure the whole stream has been consumed.
    tri!(de.end());
    Ok(value)
}

/// Deserialize an instance of type `T` from an I/O stream of JSON.
///
/// The content of the I/O stream is deserialized directly from the stream
/// without being buffered in memory by serde_json.
///
/// When reading from a source against which short reads are not efficient, such
/// as a [`File`], you will want to apply your own buffering because serde_json
/// will not buffer the input. See [`std::io::BufReader`].
///
/// It is expected that the input stream ends after the deserialized object.
/// If the stream does not end, such as in the case of a persistent socket connection,
/// this function will not return. It is possible instead to deserialize from a prefix of an input
/// stream without looking for EOF by managing your own [`Deserializer`].
///
/// Note that counter to intuition, this function is usually slower than
/// reading a file completely into memory and then applying [`from_str`]
/// or [`from_slice`] on it. See [issue #160].
///
/// [`File`]: https://doc.rust-lang.org/std/fs/struct.File.html
/// [`std::io::BufReader`]: https://doc.rust-lang.org/std/io/struct.BufReader.html
/// [`from_str`]: ./fn.from_str.html
/// [`from_slice`]: ./fn.from_slice.html
/// [issue #160]: https://github.com/serde-rs/json/issues/160
///
/// # Example
///
/// Reading the contents of a file.
///
/// ```
/// use serde::Deserialize;
///
/// use std::error::Error;
/// use std::fs::File;
/// use std::io::BufReader;
/// use std::path::Path;
///
/// #[derive(Deserialize, Debug)]
/// struct User {
///     fingerprint: String,
///     location: String,
/// }
///
/// fn read_user_from_file<P: AsRef<Path>>(path: P) -> Result<User, Box<dyn Error>> {
///     // Open the file in read-only mode with buffer.
///     let file = File::open(path)?;
///     let reader = BufReader::new(file);
///
///     // Read the JSON contents of the file as an instance of `User`.
///     let u = serde_json::from_reader(reader)?;
///
///     // Return the `User`.
///     Ok(u)
/// }
///
/// fn main() {
/// # }
/// # fn fake_main() {
///     let u = read_user_from_file("test.json").unwrap();
///     println!("{:#?}", u);
/// }
/// ```
///
/// Reading from a persistent socket connection.
///
/// ```
/// use serde::Deserialize;
///
/// use std::error::Error;
/// use std::net::{TcpListener, TcpStream};
///
/// #[derive(Deserialize, Debug)]
/// struct User {
///     fingerprint: String,
///     location: String,
/// }
///
/// fn read_user_from_stream(tcp_stream: TcpStream) -> Result<User, Box<dyn Error>> {
///     let mut de = serde_json::Deserializer::from_reader(tcp_stream);
///     let u = User::deserialize(&mut de)?;
///
///     Ok(u)
/// }
///
/// fn main() {
/// # }
/// # fn fake_main() {
///     let listener = TcpListener::bind("127.0.0.1:4000").unwrap();
///
///     for stream in listener.incoming() {
///         println!("{:#?}", read_user_from_stream(stream.unwrap()));
///     }
/// }
/// ```
///
/// # Errors
///
/// This conversion can fail if the structure of the input does not match the
/// structure expected by `T`, for example if `T` is a struct type but the input
/// contains something other than a JSON map. It can also fail if the structure
/// is correct but `T`'s implementation of `Deserialize` decides that something
/// is wrong with the data, for example required struct fields are missing from
/// the JSON map or some number is too big to fit in the expected primitive
/// type.
#[cfg(feature = "std")]
#[cfg_attr(docsrs, doc(cfg(feature = "std")))]
pub fn from_reader<R, T>(rdr: R) -> Result<T>
where
    R: crate::io::Read,
    T: de::DeserializeOwned,
{
    from_trait(read::IoRead::new(rdr))
}

/// Deserialize an instance of type `T` from bytes of JSON text.
///
/// # Example
///
/// ```
/// use serde::Deserialize;
///
/// #[derive(Deserialize, Debug)]
/// struct User {
///     fingerprint: String,
///     location: String,
/// }
///
/// fn main() {
///     // The type of `j` is `&[u8]`
///     let j = b"
///         {
///             \"fingerprint\": \"0xF9BA143B95FF6D82\",
///             \"location\": \"Menlo Park, CA\"
///         }";
///
///     let u: User = serde_json::from_slice(j).unwrap();
///     println!("{:#?}", u);
/// }
/// ```
///
/// # Errors
///
/// This conversion can fail if the structure of the input does not match the
/// structure expected by `T`, for example if `T` is a struct type but the input
/// contains something other than a JSON map. It can also fail if the structure
/// is correct but `T`'s implementation of `Deserialize` decides that something
/// is wrong with the data, for example required struct fields are missing from
/// the JSON map or some number is too big to fit in the expected primitive
/// type.
pub fn from_slice<'a, T>(v: &'a [u8]) -> Result<T>
where
    T: de::Deserialize<'a>,
{
    from_trait(read::SliceRead::new(v))
}

/// Deserialize an instance of type `T` from a string of JSON text.
///
/// # Example
///
/// ```
/// use serde::Deserialize;
///
/// #[derive(Deserialize, Debug)]
/// struct User {
///     fingerprint: String,
///     location: String,
/// }
///
/// fn main() {
///     // The type of `j` is `&str`
///     let j = "
///         {
///             \"fingerprint\": \"0xF9BA143B95FF6D82\",
///             \"location\": \"Menlo Park, CA\"
///         }";
///
///     let u: User = serde_json::from_str(j).unwrap();
///     println!("{:#?}", u);
/// }
/// ```
///
/// # Errors
///
/// This conversion can fail if the structure of the input does not match the
/// structure expected by `T`, for example if `T` is a struct type but the input
/// contains something other than a JSON map. It can also fail if the structure
/// is correct but `T`'s implementation of `Deserialize` decides that something
/// is wrong with the data, for example required struct fields are missing from
/// the JSON map or some number is too big to fit in the expected primitive
/// type.
pub fn from_str<'a, T>(s: &'a str) -> Result<T>
where
    T: de::Deserialize<'a>,
{
    from_trait(read::StrRead::new(s))
}


// --- rs_serde_json_ser.rs ---
//! Serialize a Rust data structure into JSON data.

use crate::error::{Error, ErrorCode, Result};
use crate::io;
use alloc::string::{String, ToString};
use alloc::vec::Vec;
use core::fmt::{self, Display};
use core::num::FpCategory;
use serde::ser::{self, Impossible, Serialize};

/// A structure for serializing Rust values into JSON.
#[cfg_attr(docsrs, doc(cfg(feature = "std")))]
pub struct Serializer<W, F = CompactFormatter> {
    writer: W,
    formatter: F,
}

impl<W> Serializer<W>
where
    W: io::Write,
{
    /// Creates a new JSON serializer.
    #[inline]
    pub fn new(writer: W) -> Self {
        Serializer::with_formatter(writer, CompactFormatter)
    }
}

impl<'a, W> Serializer<W, PrettyFormatter<'a>>
where
    W: io::Write,
{
    /// Creates a new JSON pretty print serializer.
    #[inline]
    pub fn pretty(writer: W) -> Self {
        Serializer::with_formatter(writer, PrettyFormatter::new())
    }
}

impl<W, F> Serializer<W, F>
where
    W: io::Write,
    F: Formatter,
{
    /// Creates a new JSON visitor whose output will be written to the writer
    /// specified.
    #[inline]
    pub fn with_formatter(writer: W, formatter: F) -> Self {
        Serializer { writer, formatter }
    }

    /// Unwrap the `Writer` from the `Serializer`.
    #[inline]
    pub fn into_inner(self) -> W {
        self.writer
    }
}

impl<'a, W, F> ser::Serializer for &'a mut Serializer<W, F>
where
    W: io::Write,
    F: Formatter,
{
    type Ok = ();
    type Error = Error;

    type SerializeSeq = Compound<'a, W, F>;
    type SerializeTuple = Compound<'a, W, F>;
    type SerializeTupleStruct = Compound<'a, W, F>;
    type SerializeTupleVariant = Compound<'a, W, F>;
    type SerializeMap = Compound<'a, W, F>;
    type SerializeStruct = Compound<'a, W, F>;
    type SerializeStructVariant = Compound<'a, W, F>;

    #[inline]
    fn serialize_bool(self, value: bool) -> Result<()> {
        self.formatter
            .write_bool(&mut self.writer, value)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_i8(self, value: i8) -> Result<()> {
        self.formatter
            .write_i8(&mut self.writer, value)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_i16(self, value: i16) -> Result<()> {
        self.formatter
            .write_i16(&mut self.writer, value)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_i32(self, value: i32) -> Result<()> {
        self.formatter
            .write_i32(&mut self.writer, value)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_i64(self, value: i64) -> Result<()> {
        self.formatter
            .write_i64(&mut self.writer, value)
            .map_err(Error::io)
    }

    fn serialize_i128(self, value: i128) -> Result<()> {
        self.formatter
            .write_i128(&mut self.writer, value)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_u8(self, value: u8) -> Result<()> {
        self.formatter
            .write_u8(&mut self.writer, value)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_u16(self, value: u16) -> Result<()> {
        self.formatter
            .write_u16(&mut self.writer, value)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_u32(self, value: u32) -> Result<()> {
        self.formatter
            .write_u32(&mut self.writer, value)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_u64(self, value: u64) -> Result<()> {
        self.formatter
            .write_u64(&mut self.writer, value)
            .map_err(Error::io)
    }

    fn serialize_u128(self, value: u128) -> Result<()> {
        self.formatter
            .write_u128(&mut self.writer, value)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_f32(self, value: f32) -> Result<()> {
        match value.classify() {
            FpCategory::Nan | FpCategory::Infinite => self
                .formatter
                .write_null(&mut self.writer)
                .map_err(Error::io),
            _ => self
                .formatter
                .write_f32(&mut self.writer, value)
                .map_err(Error::io),
        }
    }

    #[inline]
    fn serialize_f64(self, value: f64) -> Result<()> {
        match value.classify() {
            FpCategory::Nan | FpCategory::Infinite => self
                .formatter
                .write_null(&mut self.writer)
                .map_err(Error::io),
            _ => self
                .formatter
                .write_f64(&mut self.writer, value)
                .map_err(Error::io),
        }
    }

    #[inline]
    fn serialize_char(self, value: char) -> Result<()> {
        // A char encoded as UTF-8 takes 4 bytes at most.
        let mut buf = [0; 4];
        self.serialize_str(value.encode_utf8(&mut buf))
    }

    #[inline]
    fn serialize_str(self, value: &str) -> Result<()> {
        format_escaped_str(&mut self.writer, &mut self.formatter, value).map_err(Error::io)
    }

    #[inline]
    fn serialize_bytes(self, value: &[u8]) -> Result<()> {
        self.formatter
            .write_byte_array(&mut self.writer, value)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_unit(self) -> Result<()> {
        self.formatter
            .write_null(&mut self.writer)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_unit_struct(self, _name: &'static str) -> Result<()> {
        self.serialize_unit()
    }

    #[inline]
    fn serialize_unit_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        variant: &'static str,
    ) -> Result<()> {
        self.serialize_str(variant)
    }

    /// Serialize newtypes without an object wrapper.
    #[inline]
    fn serialize_newtype_struct<T>(self, _name: &'static str, value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        value.serialize(self)
    }

    #[inline]
    fn serialize_newtype_variant<T>(
        self,
        _name: &'static str,
        _variant_index: u32,
        variant: &'static str,
        value: &T,
    ) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        tri!(self
            .formatter
            .begin_object(&mut self.writer)
            .map_err(Error::io));
        tri!(self
            .formatter
            .begin_object_key(&mut self.writer, true)
            .map_err(Error::io));
        tri!(self.serialize_str(variant));
        tri!(self
            .formatter
            .end_object_key(&mut self.writer)
            .map_err(Error::io));
        tri!(self
            .formatter
            .begin_object_value(&mut self.writer)
            .map_err(Error::io));
        tri!(value.serialize(&mut *self));
        tri!(self
            .formatter
            .end_object_value(&mut self.writer)
            .map_err(Error::io));
        self.formatter
            .end_object(&mut self.writer)
            .map_err(Error::io)
    }

    #[inline]
    fn serialize_none(self) -> Result<()> {
        self.serialize_unit()
    }

    #[inline]
    fn serialize_some<T>(self, value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        value.serialize(self)
    }

    #[inline]
    fn serialize_seq(self, len: Option<usize>) -> Result<Self::SerializeSeq> {
        tri!(self
            .formatter
            .begin_array(&mut self.writer)
            .map_err(Error::io));
        if len == Some(0) {
            tri!(self
                .formatter
                .end_array(&mut self.writer)
                .map_err(Error::io));
            Ok(Compound::Map {
                ser: self,
                state: State::Empty,
            })
        } else {
            Ok(Compound::Map {
                ser: self,
                state: State::First,
            })
        }
    }

    #[inline]
    fn serialize_tuple(self, len: usize) -> Result<Self::SerializeTuple> {
        self.serialize_seq(Some(len))
    }

    #[inline]
    fn serialize_tuple_struct(
        self,
        _name: &'static str,
        len: usize,
    ) -> Result<Self::SerializeTupleStruct> {
        self.serialize_seq(Some(len))
    }

    #[inline]
    fn serialize_tuple_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        variant: &'static str,
        len: usize,
    ) -> Result<Self::SerializeTupleVariant> {
        tri!(self
            .formatter
            .begin_object(&mut self.writer)
            .map_err(Error::io));
        tri!(self
            .formatter
            .begin_object_key(&mut self.writer, true)
            .map_err(Error::io));
        tri!(self.serialize_str(variant));
        tri!(self
            .formatter
            .end_object_key(&mut self.writer)
            .map_err(Error::io));
        tri!(self
            .formatter
            .begin_object_value(&mut self.writer)
            .map_err(Error::io));
        self.serialize_seq(Some(len))
    }

    #[inline]
    fn serialize_map(self, len: Option<usize>) -> Result<Self::SerializeMap> {
        tri!(self
            .formatter
            .begin_object(&mut self.writer)
            .map_err(Error::io));
        if len == Some(0) {
            tri!(self
                .formatter
                .end_object(&mut self.writer)
                .map_err(Error::io));
            Ok(Compound::Map {
                ser: self,
                state: State::Empty,
            })
        } else {
            Ok(Compound::Map {
                ser: self,
                state: State::First,
            })
        }
    }

    #[inline]
    fn serialize_struct(self, name: &'static str, len: usize) -> Result<Self::SerializeStruct> {
        match name {
            #[cfg(feature = "arbitrary_precision")]
            crate::number::TOKEN => Ok(Compound::Number { ser: self }),
            #[cfg(feature = "raw_value")]
            crate::raw::TOKEN => Ok(Compound::RawValue { ser: self }),
            _ => self.serialize_map(Some(len)),
        }
    }

    #[inline]
    fn serialize_struct_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        variant: &'static str,
        len: usize,
    ) -> Result<Self::SerializeStructVariant> {
        tri!(self
            .formatter
            .begin_object(&mut self.writer)
            .map_err(Error::io));
        tri!(self
            .formatter
            .begin_object_key(&mut self.writer, true)
            .map_err(Error::io));
        tri!(self.serialize_str(variant));
        tri!(self
            .formatter
            .end_object_key(&mut self.writer)
            .map_err(Error::io));
        tri!(self
            .formatter
            .begin_object_value(&mut self.writer)
            .map_err(Error::io));
        self.serialize_map(Some(len))
    }

    fn collect_str<T>(self, value: &T) -> Result<()>
    where
        T: ?Sized + Display,
    {
        use self::fmt::Write;

        struct Adapter<'ser, W: 'ser, F: 'ser> {
            writer: &'ser mut W,
            formatter: &'ser mut F,
            error: Option<io::Error>,
        }

        impl<'ser, W, F> Write for Adapter<'ser, W, F>
        where
            W: io::Write,
            F: Formatter,
        {
            fn write_str(&mut self, s: &str) -> fmt::Result {
                debug_assert!(self.error.is_none());
                match format_escaped_str_contents(self.writer, self.formatter, s) {
                    Ok(()) => Ok(()),
                    Err(err) => {
                        self.error = Some(err);
                        Err(fmt::Error)
                    }
                }
            }
        }

        tri!(self
            .formatter
            .begin_string(&mut self.writer)
            .map_err(Error::io));
        let mut adapter = Adapter {
            writer: &mut self.writer,
            formatter: &mut self.formatter,
            error: None,
        };
        match write!(adapter, "{}", value) {
            Ok(()) => debug_assert!(adapter.error.is_none()),
            Err(fmt::Error) => {
                return Err(Error::io(adapter.error.expect("there should be an error")));
            }
        }
        self.formatter
            .end_string(&mut self.writer)
            .map_err(Error::io)
    }
}

// Not public API. Should be pub(crate).
#[doc(hidden)]
#[derive(Eq, PartialEq)]
pub enum State {
    Empty,
    First,
    Rest,
}

// Not public API. Should be pub(crate).
#[doc(hidden)]
pub enum Compound<'a, W: 'a, F: 'a> {
    Map {
        ser: &'a mut Serializer<W, F>,
        state: State,
    },
    #[cfg(feature = "arbitrary_precision")]
    Number { ser: &'a mut Serializer<W, F> },
    #[cfg(feature = "raw_value")]
    RawValue { ser: &'a mut Serializer<W, F> },
}

impl<'a, W, F> ser::SerializeSeq for Compound<'a, W, F>
where
    W: io::Write,
    F: Formatter,
{
    type Ok = ();
    type Error = Error;

    #[inline]
    fn serialize_element<T>(&mut self, value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        match self {
            Compound::Map { ser, state } => {
                tri!(ser
                    .formatter
                    .begin_array_value(&mut ser.writer, *state == State::First)
                    .map_err(Error::io));
                *state = State::Rest;
                tri!(value.serialize(&mut **ser));
                ser.formatter
                    .end_array_value(&mut ser.writer)
                    .map_err(Error::io)
            }
            #[cfg(feature = "arbitrary_precision")]
            Compound::Number { .. } => unreachable!(),
            #[cfg(feature = "raw_value")]
            Compound::RawValue { .. } => unreachable!(),
        }
    }

    #[inline]
    fn end(self) -> Result<()> {
        match self {
            Compound::Map { ser, state } => match state {
                State::Empty => Ok(()),
                _ => ser.formatter.end_array(&mut ser.writer).map_err(Error::io),
            },
            #[cfg(feature = "arbitrary_precision")]
            Compound::Number { .. } => unreachable!(),
            #[cfg(feature = "raw_value")]
            Compound::RawValue { .. } => unreachable!(),
        }
    }
}

impl<'a, W, F> ser::SerializeTuple for Compound<'a, W, F>
where
    W: io::Write,
    F: Formatter,
{
    type Ok = ();
    type Error = Error;

    #[inline]
    fn serialize_element<T>(&mut self, value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        ser::SerializeSeq::serialize_element(self, value)
    }

    #[inline]
    fn end(self) -> Result<()> {
        ser::SerializeSeq::end(self)
    }
}

impl<'a, W, F> ser::SerializeTupleStruct for Compound<'a, W, F>
where
    W: io::Write,
    F: Formatter,
{
    type Ok = ();
    type Error = Error;

    #[inline]
    fn serialize_field<T>(&mut self, value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        ser::SerializeSeq::serialize_element(self, value)
    }

    #[inline]
    fn end(self) -> Result<()> {
        ser::SerializeSeq::end(self)
    }
}

impl<'a, W, F> ser::SerializeTupleVariant for Compound<'a, W, F>
where
    W: io::Write,
    F: Formatter,
{
    type Ok = ();
    type Error = Error;

    #[inline]
    fn serialize_field<T>(&mut self, value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        ser::SerializeSeq::serialize_element(self, value)
    }

    #[inline]
    fn end(self) -> Result<()> {
        match self {
            Compound::Map { ser, state } => {
                match state {
                    State::Empty => {}
                    _ => tri!(ser.formatter.end_array(&mut ser.writer).map_err(Error::io)),
                }
                tri!(ser
                    .formatter
                    .end_object_value(&mut ser.writer)
                    .map_err(Error::io));
                ser.formatter.end_object(&mut ser.writer).map_err(Error::io)
            }
            #[cfg(feature = "arbitrary_precision")]
            Compound::Number { .. } => unreachable!(),
            #[cfg(feature = "raw_value")]
            Compound::RawValue { .. } => unreachable!(),
        }
    }
}

impl<'a, W, F> ser::SerializeMap for Compound<'a, W, F>
where
    W: io::Write,
    F: Formatter,
{
    type Ok = ();
    type Error = Error;

    #[inline]
    fn serialize_key<T>(&mut self, key: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        match self {
            Compound::Map { ser, state } => {
                tri!(ser
                    .formatter
                    .begin_object_key(&mut ser.writer, *state == State::First)
                    .map_err(Error::io));
                *state = State::Rest;

                tri!(key.serialize(MapKeySerializer { ser: *ser }));

                ser.formatter
                    .end_object_key(&mut ser.writer)
                    .map_err(Error::io)
            }
            #[cfg(feature = "arbitrary_precision")]
            Compound::Number { .. } => unreachable!(),
            #[cfg(feature = "raw_value")]
            Compound::RawValue { .. } => unreachable!(),
        }
    }

    #[inline]
    fn serialize_value<T>(&mut self, value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        match self {
            Compound::Map { ser, .. } => {
                tri!(ser
                    .formatter
                    .begin_object_value(&mut ser.writer)
                    .map_err(Error::io));
                tri!(value.serialize(&mut **ser));
                ser.formatter
                    .end_object_value(&mut ser.writer)
                    .map_err(Error::io)
            }
            #[cfg(feature = "arbitrary_precision")]
            Compound::Number { .. } => unreachable!(),
            #[cfg(feature = "raw_value")]
            Compound::RawValue { .. } => unreachable!(),
        }
    }

    #[inline]
    fn end(self) -> Result<()> {
        match self {
            Compound::Map { ser, state } => match state {
                State::Empty => Ok(()),
                _ => ser.formatter.end_object(&mut ser.writer).map_err(Error::io),
            },
            #[cfg(feature = "arbitrary_precision")]
            Compound::Number { .. } => unreachable!(),
            #[cfg(feature = "raw_value")]
            Compound::RawValue { .. } => unreachable!(),
        }
    }
}

impl<'a, W, F> ser::SerializeStruct for Compound<'a, W, F>
where
    W: io::Write,
    F: Formatter,
{
    type Ok = ();
    type Error = Error;

    #[inline]
    fn serialize_field<T>(&mut self, key: &'static str, value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        match self {
            Compound::Map { .. } => ser::SerializeMap::serialize_entry(self, key, value),
            #[cfg(feature = "arbitrary_precision")]
            Compound::Number { ser, .. } => {
                if key == crate::number::TOKEN {
                    value.serialize(NumberStrEmitter(ser))
                } else {
                    Err(invalid_number())
                }
            }
            #[cfg(feature = "raw_value")]
            Compound::RawValue { ser, .. } => {
                if key == crate::raw::TOKEN {
                    value.serialize(RawValueStrEmitter(ser))
                } else {
                    Err(invalid_raw_value())
                }
            }
        }
    }

    #[inline]
    fn end(self) -> Result<()> {
        match self {
            Compound::Map { .. } => ser::SerializeMap::end(self),
            #[cfg(feature = "arbitrary_precision")]
            Compound::Number { .. } => Ok(()),
            #[cfg(feature = "raw_value")]
            Compound::RawValue { .. } => Ok(()),
        }
    }
}

impl<'a, W, F> ser::SerializeStructVariant for Compound<'a, W, F>
where
    W: io::Write,
    F: Formatter,
{
    type Ok = ();
    type Error = Error;

    #[inline]
    fn serialize_field<T>(&mut self, key: &'static str, value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        match *self {
            Compound::Map { .. } => ser::SerializeStruct::serialize_field(self, key, value),
            #[cfg(feature = "arbitrary_precision")]
            Compound::Number { .. } => unreachable!(),
            #[cfg(feature = "raw_value")]
            Compound::RawValue { .. } => unreachable!(),
        }
    }

    #[inline]
    fn end(self) -> Result<()> {
        match self {
            Compound::Map { ser, state } => {
                match state {
                    State::Empty => {}
                    _ => tri!(ser.formatter.end_object(&mut ser.writer).map_err(Error::io)),
                }
                tri!(ser
                    .formatter
                    .end_object_value(&mut ser.writer)
                    .map_err(Error::io));
                ser.formatter.end_object(&mut ser.writer).map_err(Error::io)
            }
            #[cfg(feature = "arbitrary_precision")]
            Compound::Number { .. } => unreachable!(),
            #[cfg(feature = "raw_value")]
            Compound::RawValue { .. } => unreachable!(),
        }
    }
}

struct MapKeySerializer<'a, W: 'a, F: 'a> {
    ser: &'a mut Serializer<W, F>,
}

#[cfg(feature = "arbitrary_precision")]
fn invalid_number() -> Error {
    Error::syntax(ErrorCode::InvalidNumber, 0, 0)
}

#[cfg(feature = "raw_value")]
fn invalid_raw_value() -> Error {
    Error::syntax(ErrorCode::ExpectedSomeValue, 0, 0)
}

fn key_must_be_a_string() -> Error {
    Error::syntax(ErrorCode::KeyMustBeAString, 0, 0)
}

fn float_key_must_be_finite() -> Error {
    Error::syntax(ErrorCode::FloatKeyMustBeFinite, 0, 0)
}

impl<'a, W, F> ser::Serializer for MapKeySerializer<'a, W, F>
where
    W: io::Write,
    F: Formatter,
{
    type Ok = ();
    type Error = Error;

    #[inline]
    fn serialize_str(self, value: &str) -> Result<()> {
        self.ser.serialize_str(value)
    }

    #[inline]
    fn serialize_unit_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        variant: &'static str,
    ) -> Result<()> {
        self.ser.serialize_str(variant)
    }

    #[inline]
    fn serialize_newtype_struct<T>(self, _name: &'static str, value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        value.serialize(self)
    }

    type SerializeSeq = Impossible<(), Error>;
    type SerializeTuple = Impossible<(), Error>;
    type SerializeTupleStruct = Impossible<(), Error>;
    type SerializeTupleVariant = Impossible<(), Error>;
    type SerializeMap = Impossible<(), Error>;
    type SerializeStruct = Impossible<(), Error>;
    type SerializeStructVariant = Impossible<(), Error>;

    fn serialize_bool(self, value: bool) -> Result<()> {
        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_bool(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_i8(self, value: i8) -> Result<()> {
        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_i8(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_i16(self, value: i16) -> Result<()> {
        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_i16(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_i32(self, value: i32) -> Result<()> {
        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_i32(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_i64(self, value: i64) -> Result<()> {
        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_i64(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_i128(self, value: i128) -> Result<()> {
        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_i128(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_u8(self, value: u8) -> Result<()> {
        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_u8(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_u16(self, value: u16) -> Result<()> {
        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_u16(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_u32(self, value: u32) -> Result<()> {
        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_u32(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_u64(self, value: u64) -> Result<()> {
        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_u64(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_u128(self, value: u128) -> Result<()> {
        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_u128(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_f32(self, value: f32) -> Result<()> {
        if !value.is_finite() {
            return Err(float_key_must_be_finite());
        }

        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_f32(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_f64(self, value: f64) -> Result<()> {
        if !value.is_finite() {
            return Err(float_key_must_be_finite());
        }

        tri!(self
            .ser
            .formatter
            .begin_string(&mut self.ser.writer)
            .map_err(Error::io));
        tri!(self
            .ser
            .formatter
            .write_f64(&mut self.ser.writer, value)
            .map_err(Error::io));
        self.ser
            .formatter
            .end_string(&mut self.ser.writer)
            .map_err(Error::io)
    }

    fn serialize_char(self, value: char) -> Result<()> {
        self.ser.serialize_str(&value.to_string())
    }

    fn serialize_bytes(self, _value: &[u8]) -> Result<()> {
        Err(key_must_be_a_string())
    }

    fn serialize_unit(self) -> Result<()> {
        Err(key_must_be_a_string())
    }

    fn serialize_unit_struct(self, _name: &'static str) -> Result<()> {
        Err(key_must_be_a_string())
    }

    fn serialize_newtype_variant<T>(
        self,
        _name: &'static str,
        _variant_index: u32,
        _variant: &'static str,
        _value: &T,
    ) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        Err(key_must_be_a_string())
    }

    fn serialize_none(self) -> Result<()> {
        Err(key_must_be_a_string())
    }

    fn serialize_some<T>(self, value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        value.serialize(self)
    }

    fn serialize_seq(self, _len: Option<usize>) -> Result<Self::SerializeSeq> {
        Err(key_must_be_a_string())
    }

    fn serialize_tuple(self, _len: usize) -> Result<Self::SerializeTuple> {
        Err(key_must_be_a_string())
    }

    fn serialize_tuple_struct(
        self,
        _name: &'static str,
        _len: usize,
    ) -> Result<Self::SerializeTupleStruct> {
        Err(key_must_be_a_string())
    }

    fn serialize_tuple_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        _variant: &'static str,
        _len: usize,
    ) -> Result<Self::SerializeTupleVariant> {
        Err(key_must_be_a_string())
    }

    fn serialize_map(self, _len: Option<usize>) -> Result<Self::SerializeMap> {
        Err(key_must_be_a_string())
    }

    fn serialize_struct(self, _name: &'static str, _len: usize) -> Result<Self::SerializeStruct> {
        Err(key_must_be_a_string())
    }

    fn serialize_struct_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        _variant: &'static str,
        _len: usize,
    ) -> Result<Self::SerializeStructVariant> {
        Err(key_must_be_a_string())
    }

    fn collect_str<T>(self, value: &T) -> Result<()>
    where
        T: ?Sized + Display,
    {
        self.ser.collect_str(value)
    }
}

#[cfg(feature = "arbitrary_precision")]
struct NumberStrEmitter<'a, W: 'a + io::Write, F: 'a + Formatter>(&'a mut Serializer<W, F>);

#[cfg(feature = "arbitrary_precision")]
impl<'a, W: io::Write, F: Formatter> ser::Serializer for NumberStrEmitter<'a, W, F> {
    type Ok = ();
    type Error = Error;

    type SerializeSeq = Impossible<(), Error>;
    type SerializeTuple = Impossible<(), Error>;
    type SerializeTupleStruct = Impossible<(), Error>;
    type SerializeTupleVariant = Impossible<(), Error>;
    type SerializeMap = Impossible<(), Error>;
    type SerializeStruct = Impossible<(), Error>;
    type SerializeStructVariant = Impossible<(), Error>;

    fn serialize_bool(self, _v: bool) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_i8(self, _v: i8) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_i16(self, _v: i16) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_i32(self, _v: i32) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_i64(self, _v: i64) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_i128(self, _v: i128) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_u8(self, _v: u8) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_u16(self, _v: u16) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_u32(self, _v: u32) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_u64(self, _v: u64) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_u128(self, _v: u128) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_f32(self, _v: f32) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_f64(self, _v: f64) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_char(self, _v: char) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_str(self, value: &str) -> Result<()> {
        let NumberStrEmitter(serializer) = self;
        serializer
            .formatter
            .write_number_str(&mut serializer.writer, value)
            .map_err(Error::io)
    }

    fn serialize_bytes(self, _value: &[u8]) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_none(self) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_some<T>(self, _value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        Err(invalid_number())
    }

    fn serialize_unit(self) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_unit_struct(self, _name: &'static str) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_unit_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        _variant: &'static str,
    ) -> Result<()> {
        Err(invalid_number())
    }

    fn serialize_newtype_struct<T>(self, _name: &'static str, _value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        Err(invalid_number())
    }

    fn serialize_newtype_variant<T>(
        self,
        _name: &'static str,
        _variant_index: u32,
        _variant: &'static str,
        _value: &T,
    ) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        Err(invalid_number())
    }

    fn serialize_seq(self, _len: Option<usize>) -> Result<Self::SerializeSeq> {
        Err(invalid_number())
    }

    fn serialize_tuple(self, _len: usize) -> Result<Self::SerializeTuple> {
        Err(invalid_number())
    }

    fn serialize_tuple_struct(
        self,
        _name: &'static str,
        _len: usize,
    ) -> Result<Self::SerializeTupleStruct> {
        Err(invalid_number())
    }

    fn serialize_tuple_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        _variant: &'static str,
        _len: usize,
    ) -> Result<Self::SerializeTupleVariant> {
        Err(invalid_number())
    }

    fn serialize_map(self, _len: Option<usize>) -> Result<Self::SerializeMap> {
        Err(invalid_number())
    }

    fn serialize_struct(self, _name: &'static str, _len: usize) -> Result<Self::SerializeStruct> {
        Err(invalid_number())
    }

    fn serialize_struct_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        _variant: &'static str,
        _len: usize,
    ) -> Result<Self::SerializeStructVariant> {
        Err(invalid_number())
    }
}

#[cfg(feature = "raw_value")]
struct RawValueStrEmitter<'a, W: 'a + io::Write, F: 'a + Formatter>(&'a mut Serializer<W, F>);

#[cfg(feature = "raw_value")]
impl<'a, W: io::Write, F: Formatter> ser::Serializer for RawValueStrEmitter<'a, W, F> {
    type Ok = ();
    type Error = Error;

    type SerializeSeq = Impossible<(), Error>;
    type SerializeTuple = Impossible<(), Error>;
    type SerializeTupleStruct = Impossible<(), Error>;
    type SerializeTupleVariant = Impossible<(), Error>;
    type SerializeMap = Impossible<(), Error>;
    type SerializeStruct = Impossible<(), Error>;
    type SerializeStructVariant = Impossible<(), Error>;

    fn serialize_bool(self, _v: bool) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_i8(self, _v: i8) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_i16(self, _v: i16) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_i32(self, _v: i32) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_i64(self, _v: i64) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_i128(self, _v: i128) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_u8(self, _v: u8) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_u16(self, _v: u16) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_u32(self, _v: u32) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_u64(self, _v: u64) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_u128(self, _v: u128) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_f32(self, _v: f32) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_f64(self, _v: f64) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_char(self, _v: char) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_str(self, value: &str) -> Result<()> {
        let RawValueStrEmitter(serializer) = self;
        serializer
            .formatter
            .write_raw_fragment(&mut serializer.writer, value)
            .map_err(Error::io)
    }

    fn serialize_bytes(self, _value: &[u8]) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_none(self) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_some<T>(self, _value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_unit(self) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_unit_struct(self, _name: &'static str) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_unit_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        _variant: &'static str,
    ) -> Result<()> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_newtype_struct<T>(self, _name: &'static str, _value: &T) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_newtype_variant<T>(
        self,
        _name: &'static str,
        _variant_index: u32,
        _variant: &'static str,
        _value: &T,
    ) -> Result<()>
    where
        T: ?Sized + Serialize,
    {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_seq(self, _len: Option<usize>) -> Result<Self::SerializeSeq> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_tuple(self, _len: usize) -> Result<Self::SerializeTuple> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_tuple_struct(
        self,
        _name: &'static str,
        _len: usize,
    ) -> Result<Self::SerializeTupleStruct> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_tuple_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        _variant: &'static str,
        _len: usize,
    ) -> Result<Self::SerializeTupleVariant> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_map(self, _len: Option<usize>) -> Result<Self::SerializeMap> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_struct(self, _name: &'static str, _len: usize) -> Result<Self::SerializeStruct> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn serialize_struct_variant(
        self,
        _name: &'static str,
        _variant_index: u32,
        _variant: &'static str,
        _len: usize,
    ) -> Result<Self::SerializeStructVariant> {
        Err(ser::Error::custom("expected RawValue"))
    }

    fn collect_str<T>(self, value: &T) -> Result<Self::Ok>
    where
        T: ?Sized + Display,
    {
        self.serialize_str(&value.to_string())
    }
}

/// Represents a character escape code in a type-safe manner.
pub enum CharEscape {
    /// An escaped quote `"`
    Quote,
    /// An escaped reverse solidus `\`
    ReverseSolidus,
    /// An escaped solidus `/`
    Solidus,
    /// An escaped backspace character (usually escaped as `\b`)
    Backspace,
    /// An escaped form feed character (usually escaped as `\f`)
    FormFeed,
    /// An escaped line feed character (usually escaped as `\n`)
    LineFeed,
    /// An escaped carriage return character (usually escaped as `\r`)
    CarriageReturn,
    /// An escaped tab character (usually escaped as `\t`)
    Tab,
    /// An escaped ASCII plane control character (usually escaped as
    /// `\u00XX` where `XX` are two hex characters)
    AsciiControl(u8),
}

impl CharEscape {
    #[inline]
    fn from_escape_table(escape: u8, byte: u8) -> CharEscape {
        match escape {
            self::BB => CharEscape::Backspace,
            self::TT => CharEscape::Tab,
            self::NN => CharEscape::LineFeed,
            self::FF => CharEscape::FormFeed,
            self::RR => CharEscape::CarriageReturn,
            self::QU => CharEscape::Quote,
            self::BS => CharEscape::ReverseSolidus,
            self::UU => CharEscape::AsciiControl(byte),
            _ => unreachable!(),
        }
    }
}

/// This trait abstracts away serializing the JSON control characters, which allows the user to
/// optionally pretty print the JSON output.
pub trait Formatter {
    /// Writes a `null` value to the specified writer.
    #[inline]
    fn write_null<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(b"null")
    }

    /// Writes a `true` or `false` value to the specified writer.
    #[inline]
    fn write_bool<W>(&mut self, writer: &mut W, value: bool) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let s = if value {
            b"true" as &[u8]
        } else {
            b"false" as &[u8]
        };
        writer.write_all(s)
    }

    /// Writes an integer value like `-123` to the specified writer.
    #[inline]
    fn write_i8<W>(&mut self, writer: &mut W, value: i8) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = itoa::Buffer::new();
        let s = buffer.format(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes an integer value like `-123` to the specified writer.
    #[inline]
    fn write_i16<W>(&mut self, writer: &mut W, value: i16) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = itoa::Buffer::new();
        let s = buffer.format(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes an integer value like `-123` to the specified writer.
    #[inline]
    fn write_i32<W>(&mut self, writer: &mut W, value: i32) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = itoa::Buffer::new();
        let s = buffer.format(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes an integer value like `-123` to the specified writer.
    #[inline]
    fn write_i64<W>(&mut self, writer: &mut W, value: i64) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = itoa::Buffer::new();
        let s = buffer.format(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes an integer value like `-123` to the specified writer.
    #[inline]
    fn write_i128<W>(&mut self, writer: &mut W, value: i128) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = itoa::Buffer::new();
        let s = buffer.format(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes an integer value like `123` to the specified writer.
    #[inline]
    fn write_u8<W>(&mut self, writer: &mut W, value: u8) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = itoa::Buffer::new();
        let s = buffer.format(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes an integer value like `123` to the specified writer.
    #[inline]
    fn write_u16<W>(&mut self, writer: &mut W, value: u16) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = itoa::Buffer::new();
        let s = buffer.format(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes an integer value like `123` to the specified writer.
    #[inline]
    fn write_u32<W>(&mut self, writer: &mut W, value: u32) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = itoa::Buffer::new();
        let s = buffer.format(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes an integer value like `123` to the specified writer.
    #[inline]
    fn write_u64<W>(&mut self, writer: &mut W, value: u64) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = itoa::Buffer::new();
        let s = buffer.format(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes an integer value like `123` to the specified writer.
    #[inline]
    fn write_u128<W>(&mut self, writer: &mut W, value: u128) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = itoa::Buffer::new();
        let s = buffer.format(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes a floating point value like `-31.26e+12` to the specified writer.
    #[inline]
    fn write_f32<W>(&mut self, writer: &mut W, value: f32) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = ryu::Buffer::new();
        let s = buffer.format_finite(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes a floating point value like `-31.26e+12` to the specified writer.
    #[inline]
    fn write_f64<W>(&mut self, writer: &mut W, value: f64) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        let mut buffer = ryu::Buffer::new();
        let s = buffer.format_finite(value);
        writer.write_all(s.as_bytes())
    }

    /// Writes a number that has already been rendered to a string.
    #[inline]
    fn write_number_str<W>(&mut self, writer: &mut W, value: &str) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(value.as_bytes())
    }

    /// Called before each series of `write_string_fragment` and
    /// `write_char_escape`.  Writes a `"` to the specified writer.
    #[inline]
    fn begin_string<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(b"\"")
    }

    /// Called after each series of `write_string_fragment` and
    /// `write_char_escape`.  Writes a `"` to the specified writer.
    #[inline]
    fn end_string<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(b"\"")
    }

    /// Writes a string fragment that doesn't need any escaping to the
    /// specified writer.
    #[inline]
    fn write_string_fragment<W>(&mut self, writer: &mut W, fragment: &str) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(fragment.as_bytes())
    }

    /// Writes a character escape code to the specified writer.
    #[inline]
    fn write_char_escape<W>(&mut self, writer: &mut W, char_escape: CharEscape) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        use self::CharEscape::*;

        let s = match char_escape {
            Quote => b"\\\"",
            ReverseSolidus => b"\\\\",
            Solidus => b"\\/",
            Backspace => b"\\b",
            FormFeed => b"\\f",
            LineFeed => b"\\n",
            CarriageReturn => b"\\r",
            Tab => b"\\t",
            AsciiControl(byte) => {
                static HEX_DIGITS: [u8; 16] = *b"0123456789abcdef";
                let bytes = &[
                    b'\\',
                    b'u',
                    b'0',
                    b'0',
                    HEX_DIGITS[(byte >> 4) as usize],
                    HEX_DIGITS[(byte & 0xF) as usize],
                ];
                return writer.write_all(bytes);
            }
        };

        writer.write_all(s)
    }

    /// Writes the representation of a byte array. Formatters can choose whether
    /// to represent bytes as a JSON array of integers (the default), or some
    /// JSON string encoding like hex or base64.
    fn write_byte_array<W>(&mut self, writer: &mut W, value: &[u8]) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        tri!(self.begin_array(writer));
        let mut first = true;
        for byte in value {
            tri!(self.begin_array_value(writer, first));
            tri!(self.write_u8(writer, *byte));
            tri!(self.end_array_value(writer));
            first = false;
        }
        self.end_array(writer)
    }

    /// Called before every array.  Writes a `[` to the specified
    /// writer.
    #[inline]
    fn begin_array<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(b"[")
    }

    /// Called after every array.  Writes a `]` to the specified
    /// writer.
    #[inline]
    fn end_array<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(b"]")
    }

    /// Called before every array value.  Writes a `,` if needed to
    /// the specified writer.
    #[inline]
    fn begin_array_value<W>(&mut self, writer: &mut W, first: bool) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        if first {
            Ok(())
        } else {
            writer.write_all(b",")
        }
    }

    /// Called after every array value.
    #[inline]
    fn end_array_value<W>(&mut self, _writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        Ok(())
    }

    /// Called before every object.  Writes a `{` to the specified
    /// writer.
    #[inline]
    fn begin_object<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(b"{")
    }

    /// Called after every object.  Writes a `}` to the specified
    /// writer.
    #[inline]
    fn end_object<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(b"}")
    }

    /// Called before every object key.
    #[inline]
    fn begin_object_key<W>(&mut self, writer: &mut W, first: bool) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        if first {
            Ok(())
        } else {
            writer.write_all(b",")
        }
    }

    /// Called after every object key.  A `:` should be written to the
    /// specified writer by either this method or
    /// `begin_object_value`.
    #[inline]
    fn end_object_key<W>(&mut self, _writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        Ok(())
    }

    /// Called before every object value.  A `:` should be written to
    /// the specified writer by either this method or
    /// `end_object_key`.
    #[inline]
    fn begin_object_value<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(b":")
    }

    /// Called after every object value.
    #[inline]
    fn end_object_value<W>(&mut self, _writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        Ok(())
    }

    /// Writes a raw JSON fragment that doesn't need any escaping to the
    /// specified writer.
    #[inline]
    fn write_raw_fragment<W>(&mut self, writer: &mut W, fragment: &str) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(fragment.as_bytes())
    }
}

/// This structure compacts a JSON value with no extra whitespace.
#[derive(Clone, Debug)]
pub struct CompactFormatter;

impl Formatter for CompactFormatter {}

/// This structure pretty prints a JSON value to make it human readable.
#[derive(Clone, Debug)]
pub struct PrettyFormatter<'a> {
    current_indent: usize,
    has_value: bool,
    indent: &'a [u8],
}

impl<'a> PrettyFormatter<'a> {
    /// Construct a pretty printer formatter that defaults to using two spaces for indentation.
    pub fn new() -> Self {
        PrettyFormatter::with_indent(b"  ")
    }

    /// Construct a pretty printer formatter that uses the `indent` string for indentation.
    pub fn with_indent(indent: &'a [u8]) -> Self {
        PrettyFormatter {
            current_indent: 0,
            has_value: false,
            indent,
        }
    }
}

impl<'a> Default for PrettyFormatter<'a> {
    fn default() -> Self {
        PrettyFormatter::new()
    }
}

impl<'a> Formatter for PrettyFormatter<'a> {
    #[inline]
    fn begin_array<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        self.current_indent += 1;
        self.has_value = false;
        writer.write_all(b"[")
    }

    #[inline]
    fn end_array<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        self.current_indent -= 1;

        if self.has_value {
            tri!(writer.write_all(b"\n"));
            tri!(indent(writer, self.current_indent, self.indent));
        }

        writer.write_all(b"]")
    }

    #[inline]
    fn begin_array_value<W>(&mut self, writer: &mut W, first: bool) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        tri!(writer.write_all(if first { b"\n" } else { b",\n" }));
        indent(writer, self.current_indent, self.indent)
    }

    #[inline]
    fn end_array_value<W>(&mut self, _writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        self.has_value = true;
        Ok(())
    }

    #[inline]
    fn begin_object<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        self.current_indent += 1;
        self.has_value = false;
        writer.write_all(b"{")
    }

    #[inline]
    fn end_object<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        self.current_indent -= 1;

        if self.has_value {
            tri!(writer.write_all(b"\n"));
            tri!(indent(writer, self.current_indent, self.indent));
        }

        writer.write_all(b"}")
    }

    #[inline]
    fn begin_object_key<W>(&mut self, writer: &mut W, first: bool) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        tri!(writer.write_all(if first { b"\n" } else { b",\n" }));
        indent(writer, self.current_indent, self.indent)
    }

    #[inline]
    fn begin_object_value<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(b": ")
    }

    #[inline]
    fn end_object_value<W>(&mut self, _writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        self.has_value = true;
        Ok(())
    }
}

fn format_escaped_str<W, F>(writer: &mut W, formatter: &mut F, value: &str) -> io::Result<()>
where
    W: ?Sized + io::Write,
    F: ?Sized + Formatter,
{
    tri!(formatter.begin_string(writer));
    tri!(format_escaped_str_contents(writer, formatter, value));
    formatter.end_string(writer)
}

fn format_escaped_str_contents<W, F>(
    writer: &mut W,
    formatter: &mut F,
    value: &str,
) -> io::Result<()>
where
    W: ?Sized + io::Write,
    F: ?Sized + Formatter,
{
    let bytes = value.as_bytes();

    let mut start = 0;

    for (i, &byte) in bytes.iter().enumerate() {
        let escape = ESCAPE[byte as usize];
        if escape == 0 {
            continue;
        }

        if start < i {
            tri!(formatter.write_string_fragment(writer, &value[start..i]));
        }

        let char_escape = CharEscape::from_escape_table(escape, byte);
        tri!(formatter.write_char_escape(writer, char_escape));

        start = i + 1;
    }

    if start == bytes.len() {
        return Ok(());
    }

    formatter.write_string_fragment(writer, &value[start..])
}

const BB: u8 = b'b'; // \x08
const TT: u8 = b't'; // \x09
const NN: u8 = b'n'; // \x0A
const FF: u8 = b'f'; // \x0C
const RR: u8 = b'r'; // \x0D
const QU: u8 = b'"'; // \x22
const BS: u8 = b'\\'; // \x5C
const UU: u8 = b'u'; // \x00...\x1F except the ones above
const __: u8 = 0;

// Lookup table of escape sequences. A value of b'x' at index i means that byte
// i is escaped as "\x" in JSON. A value of 0 means that byte i is not escaped.
static ESCAPE: [u8; 256] = [
    //   1   2   3   4   5   6   7   8   9   A   B   C   D   E   F
    UU, UU, UU, UU, UU, UU, UU, UU, BB, TT, NN, UU, FF, RR, UU, UU, // 0
    UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, // 1
    __, __, QU, __, __, __, __, __, __, __, __, __, __, __, __, __, // 2
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 3
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 4
    __, __, __, __, __, __, __, __, __, __, __, __, BS, __, __, __, // 5
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 6
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 7
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 8
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 9
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // A
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // B
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // C
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // D
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // E
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // F
];

/// Serialize the given data structure as JSON into the I/O stream.
///
/// Serialization guarantees it only feeds valid UTF-8 sequences to the writer.
///
/// # Errors
///
/// Serialization can fail if `T`'s implementation of `Serialize` decides to
/// fail, or if `T` contains a map with non-string keys.
#[inline]
#[cfg_attr(docsrs, doc(cfg(feature = "std")))]
pub fn to_writer<W, T>(writer: W, value: &T) -> Result<()>
where
    W: io::Write,
    T: ?Sized + Serialize,
{
    let mut ser = Serializer::new(writer);
    value.serialize(&mut ser)
}

/// Serialize the given data structure as pretty-printed JSON into the I/O
/// stream.
///
/// Serialization guarantees it only feeds valid UTF-8 sequences to the writer.
///
/// # Errors
///
/// Serialization can fail if `T`'s implementation of `Serialize` decides to
/// fail, or if `T` contains a map with non-string keys.
#[inline]
#[cfg_attr(docsrs, doc(cfg(feature = "std")))]
pub fn to_writer_pretty<W, T>(writer: W, value: &T) -> Result<()>
where
    W: io::Write,
    T: ?Sized + Serialize,
{
    let mut ser = Serializer::pretty(writer);
    value.serialize(&mut ser)
}

/// Serialize the given data structure as a JSON byte vector.
///
/// # Errors
///
/// Serialization can fail if `T`'s implementation of `Serialize` decides to
/// fail, or if `T` contains a map with non-string keys.
#[inline]
pub fn to_vec<T>(value: &T) -> Result<Vec<u8>>
where
    T: ?Sized + Serialize,
{
    let mut writer = Vec::with_capacity(128);
    tri!(to_writer(&mut writer, value));
    Ok(writer)
}

/// Serialize the given data structure as a pretty-printed JSON byte vector.
///
/// # Errors
///
/// Serialization can fail if `T`'s implementation of `Serialize` decides to
/// fail, or if `T` contains a map with non-string keys.
#[inline]
pub fn to_vec_pretty<T>(value: &T) -> Result<Vec<u8>>
where
    T: ?Sized + Serialize,
{
    let mut writer = Vec::with_capacity(128);
    tri!(to_writer_pretty(&mut writer, value));
    Ok(writer)
}

/// Serialize the given data structure as a String of JSON.
///
/// # Errors
///
/// Serialization can fail if `T`'s implementation of `Serialize` decides to
/// fail, or if `T` contains a map with non-string keys.
#[inline]
pub fn to_string<T>(value: &T) -> Result<String>
where
    T: ?Sized + Serialize,
{
    let vec = tri!(to_vec(value));
    let string = unsafe {
        // We do not emit invalid UTF-8.
        String::from_utf8_unchecked(vec)
    };
    Ok(string)
}

/// Serialize the given data structure as a pretty-printed String of JSON.
///
/// # Errors
///
/// Serialization can fail if `T`'s implementation of `Serialize` decides to
/// fail, or if `T` contains a map with non-string keys.
#[inline]
pub fn to_string_pretty<T>(value: &T) -> Result<String>
where
    T: ?Sized + Serialize,
{
    let vec = tri!(to_vec_pretty(value));
    let string = unsafe {
        // We do not emit invalid UTF-8.
        String::from_utf8_unchecked(vec)
    };
    Ok(string)
}

fn indent<W>(wr: &mut W, n: usize, s: &[u8]) -> io::Result<()>
where
    W: ?Sized + io::Write,
{
    for _ in 0..n {
        tri!(wr.write_all(s));
    }

    Ok(())
}


// --- rs_serde_json_value.rs ---
//! The Value enum, a loosely typed way of representing any valid JSON value.
//!
//! # Constructing JSON
//!
//! Serde JSON provides a [`json!` macro][macro] to build `serde_json::Value`
//! objects with very natural JSON syntax.
//!
//! ```
//! use serde_json::json;
//!
//! fn main() {
//!     // The type of `john` is `serde_json::Value`
//!     let john = json!({
//!         "name": "John Doe",
//!         "age": 43,
//!         "phones": [
//!             "+44 1234567",
//!             "+44 2345678"
//!         ]
//!     });
//!
//!     println!("first phone number: {}", john["phones"][0]);
//!
//!     // Convert to a string of JSON and print it out
//!     println!("{}", john.to_string());
//! }
//! ```
//!
//! The `Value::to_string()` function converts a `serde_json::Value` into a
//! `String` of JSON text.
//!
//! One neat thing about the `json!` macro is that variables and expressions can
//! be interpolated directly into the JSON value as you are building it. Serde
//! will check at compile time that the value you are interpolating is able to
//! be represented as JSON.
//!
//! ```
//! # use serde_json::json;
//! #
//! # fn random_phone() -> u16 { 0 }
//! #
//! let full_name = "John Doe";
//! let age_last_year = 42;
//!
//! // The type of `john` is `serde_json::Value`
//! let john = json!({
//!     "name": full_name,
//!     "age": age_last_year + 1,
//!     "phones": [
//!         format!("+44 {}", random_phone())
//!     ]
//! });
//! ```
//!
//! A string of JSON data can be parsed into a `serde_json::Value` by the
//! [`serde_json::from_str`][from_str] function. There is also
//! [`from_slice`][from_slice] for parsing from a byte slice `&[u8]` and
//! [`from_reader`][from_reader] for parsing from any `io::Read` like a File or
//! a TCP stream.
//!
//! ```
//! use serde_json::{json, Value, Error};
//!
//! fn untyped_example() -> Result<(), Error> {
//!     // Some JSON input data as a &str. Maybe this comes from the user.
//!     let data = r#"
//!         {
//!             "name": "John Doe",
//!             "age": 43,
//!             "phones": [
//!                 "+44 1234567",
//!                 "+44 2345678"
//!             ]
//!         }"#;
//!
//!     // Parse the string of data into serde_json::Value.
//!     let v: Value = serde_json::from_str(data)?;
//!
//!     // Access parts of the data by indexing with square brackets.
//!     println!("Please call {} at the number {}", v["name"], v["phones"][0]);
//!
//!     Ok(())
//! }
//! #
//! # untyped_example().unwrap();
//! ```
//!
//! [macro]: crate::json
//! [from_str]: crate::de::from_str
//! [from_slice]: crate::de::from_slice
//! [from_reader]: crate::de::from_reader

use crate::error::Error;
use crate::io;
use alloc::string::String;
use alloc::vec::Vec;
use core::fmt::{self, Debug, Display};
use core::mem;
use core::str;
use serde::de::DeserializeOwned;
use serde::ser::Serialize;

pub use self::index::Index;
pub use self::ser::Serializer;
pub use crate::map::Map;
pub use crate::number::Number;

#[cfg(feature = "raw_value")]
#[cfg_attr(docsrs, doc(cfg(feature = "raw_value")))]
pub use crate::raw::{to_raw_value, RawValue};

/// Represents any valid JSON value.
///
/// See the [`serde_json::value` module documentation](self) for usage examples.
#[derive(Clone, Eq, PartialEq)]
pub enum Value {
    /// Represents a JSON null value.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!(null);
    /// ```
    Null,

    /// Represents a JSON boolean.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!(true);
    /// ```
    Bool(bool),

    /// Represents a JSON number, whether integer or floating point.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!(12.5);
    /// ```
    Number(Number),

    /// Represents a JSON string.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!("a string");
    /// ```
    String(String),

    /// Represents a JSON array.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!(["an", "array"]);
    /// ```
    Array(Vec<Value>),

    /// Represents a JSON object.
    ///
    /// By default the map is backed by a BTreeMap. Enable the `preserve_order`
    /// feature of serde_json to use IndexMap instead, which preserves
    /// entries in the order they are inserted into the map. In particular, this
    /// allows JSON data to be deserialized into a Value and serialized to a
    /// string while retaining the order of map keys in the input.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "an": "object" });
    /// ```
    Object(Map<String, Value>),
}

impl Debug for Value {
    fn fmt(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        match self {
            Value::Null => formatter.write_str("Null"),
            Value::Bool(boolean) => write!(formatter, "Bool({})", boolean),
            Value::Number(number) => Debug::fmt(number, formatter),
            Value::String(string) => write!(formatter, "String({:?})", string),
            Value::Array(vec) => {
                tri!(formatter.write_str("Array "));
                Debug::fmt(vec, formatter)
            }
            Value::Object(map) => {
                tri!(formatter.write_str("Object "));
                Debug::fmt(map, formatter)
            }
        }
    }
}

impl Display for Value {
    /// Display a JSON value as a string.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let json = json!({ "city": "London", "street": "10 Downing Street" });
    ///
    /// // Compact format:
    /// //
    /// // {"city":"London","street":"10 Downing Street"}
    /// let compact = format!("{}", json);
    /// assert_eq!(compact,
    ///     "{\"city\":\"London\",\"street\":\"10 Downing Street\"}");
    ///
    /// // Pretty format:
    /// //
    /// // {
    /// //   "city": "London",
    /// //   "street": "10 Downing Street"
    /// // }
    /// let pretty = format!("{:#}", json);
    /// assert_eq!(pretty,
    ///     "{\n  \"city\": \"London\",\n  \"street\": \"10 Downing Street\"\n}");
    /// ```
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        struct WriterFormatter<'a, 'b: 'a> {
            inner: &'a mut fmt::Formatter<'b>,
        }

        impl<'a, 'b> io::Write for WriterFormatter<'a, 'b> {
            fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
                // Safety: the serializer below only emits valid utf8 when using
                // the default formatter.
                let s = unsafe { str::from_utf8_unchecked(buf) };
                tri!(self.inner.write_str(s).map_err(io_error));
                Ok(buf.len())
            }

            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }

        fn io_error(_: fmt::Error) -> io::Error {
            // Error value does not matter because Display impl just maps it
            // back to fmt::Error.
            io::Error::new(io::ErrorKind::Other, "fmt error")
        }

        let alternate = f.alternate();
        let mut wr = WriterFormatter { inner: f };
        if alternate {
            // {:#}
            super::ser::to_writer_pretty(&mut wr, self).map_err(|_| fmt::Error)
        } else {
            // {}
            super::ser::to_writer(&mut wr, self).map_err(|_| fmt::Error)
        }
    }
}

fn parse_index(s: &str) -> Option<usize> {
    if s.starts_with('+') || (s.starts_with('0') && s.len() != 1) {
        return None;
    }
    s.parse().ok()
}

impl Value {
    /// Index into a JSON array or map. A string index can be used to access a
    /// value in a map, and a usize index can be used to access an element of an
    /// array.
    ///
    /// Returns `None` if the type of `self` does not match the type of the
    /// index, for example if the index is a string and `self` is an array or a
    /// number. Also returns `None` if the given key does not exist in the map
    /// or the given index is not within the bounds of the array.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let object = json!({ "A": 65, "B": 66, "C": 67 });
    /// assert_eq!(*object.get("A").unwrap(), json!(65));
    ///
    /// let array = json!([ "A", "B", "C" ]);
    /// assert_eq!(*array.get(2).unwrap(), json!("C"));
    ///
    /// assert_eq!(array.get("A"), None);
    /// ```
    ///
    /// Square brackets can also be used to index into a value in a more concise
    /// way. This returns `Value::Null` in cases where `get` would have returned
    /// `None`.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let object = json!({
    ///     "A": ["a", "á", "à"],
    ///     "B": ["b", "b́"],
    ///     "C": ["c", "ć", "ć̣", "ḉ"],
    /// });
    /// assert_eq!(object["B"][0], json!("b"));
    ///
    /// assert_eq!(object["D"], json!(null));
    /// assert_eq!(object[0]["x"]["y"]["z"], json!(null));
    /// ```
    pub fn get<I: Index>(&self, index: I) -> Option<&Value> {
        index.index_into(self)
    }

    /// Mutably index into a JSON array or map. A string index can be used to
    /// access a value in a map, and a usize index can be used to access an
    /// element of an array.
    ///
    /// Returns `None` if the type of `self` does not match the type of the
    /// index, for example if the index is a string and `self` is an array or a
    /// number. Also returns `None` if the given key does not exist in the map
    /// or the given index is not within the bounds of the array.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let mut object = json!({ "A": 65, "B": 66, "C": 67 });
    /// *object.get_mut("A").unwrap() = json!(69);
    ///
    /// let mut array = json!([ "A", "B", "C" ]);
    /// *array.get_mut(2).unwrap() = json!("D");
    /// ```
    pub fn get_mut<I: Index>(&mut self, index: I) -> Option<&mut Value> {
        index.index_into_mut(self)
    }

    /// Returns true if the `Value` is an Object. Returns false otherwise.
    ///
    /// For any Value on which `is_object` returns true, `as_object` and
    /// `as_object_mut` are guaranteed to return the map representation of the
    /// object.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let obj = json!({ "a": { "nested": true }, "b": ["an", "array"] });
    ///
    /// assert!(obj.is_object());
    /// assert!(obj["a"].is_object());
    ///
    /// // array, not an object
    /// assert!(!obj["b"].is_object());
    /// ```
    pub fn is_object(&self) -> bool {
        self.as_object().is_some()
    }

    /// If the `Value` is an Object, returns the associated Map. Returns None
    /// otherwise.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": { "nested": true }, "b": ["an", "array"] });
    ///
    /// // The length of `{"nested": true}` is 1 entry.
    /// assert_eq!(v["a"].as_object().unwrap().len(), 1);
    ///
    /// // The array `["an", "array"]` is not an object.
    /// assert_eq!(v["b"].as_object(), None);
    /// ```
    pub fn as_object(&self) -> Option<&Map<String, Value>> {
        match self {
            Value::Object(map) => Some(map),
            _ => None,
        }
    }

    /// If the `Value` is an Object, returns the associated mutable Map.
    /// Returns None otherwise.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let mut v = json!({ "a": { "nested": true } });
    ///
    /// v["a"].as_object_mut().unwrap().clear();
    /// assert_eq!(v, json!({ "a": {} }));
    /// ```
    pub fn as_object_mut(&mut self) -> Option<&mut Map<String, Value>> {
        match self {
            Value::Object(map) => Some(map),
            _ => None,
        }
    }

    /// Returns true if the `Value` is an Array. Returns false otherwise.
    ///
    /// For any Value on which `is_array` returns true, `as_array` and
    /// `as_array_mut` are guaranteed to return the vector representing the
    /// array.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let obj = json!({ "a": ["an", "array"], "b": { "an": "object" } });
    ///
    /// assert!(obj["a"].is_array());
    ///
    /// // an object, not an array
    /// assert!(!obj["b"].is_array());
    /// ```
    pub fn is_array(&self) -> bool {
        self.as_array().is_some()
    }

    /// If the `Value` is an Array, returns the associated vector. Returns None
    /// otherwise.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": ["an", "array"], "b": { "an": "object" } });
    ///
    /// // The length of `["an", "array"]` is 2 elements.
    /// assert_eq!(v["a"].as_array().unwrap().len(), 2);
    ///
    /// // The object `{"an": "object"}` is not an array.
    /// assert_eq!(v["b"].as_array(), None);
    /// ```
    pub fn as_array(&self) -> Option<&Vec<Value>> {
        match self {
            Value::Array(array) => Some(array),
            _ => None,
        }
    }

    /// If the `Value` is an Array, returns the associated mutable vector.
    /// Returns None otherwise.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let mut v = json!({ "a": ["an", "array"] });
    ///
    /// v["a"].as_array_mut().unwrap().clear();
    /// assert_eq!(v, json!({ "a": [] }));
    /// ```
    pub fn as_array_mut(&mut self) -> Option<&mut Vec<Value>> {
        match self {
            Value::Array(list) => Some(list),
            _ => None,
        }
    }

    /// Returns true if the `Value` is a String. Returns false otherwise.
    ///
    /// For any Value on which `is_string` returns true, `as_str` is guaranteed
    /// to return the string slice.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": "some string", "b": false });
    ///
    /// assert!(v["a"].is_string());
    ///
    /// // The boolean `false` is not a string.
    /// assert!(!v["b"].is_string());
    /// ```
    pub fn is_string(&self) -> bool {
        self.as_str().is_some()
    }

    /// If the `Value` is a String, returns the associated str. Returns None
    /// otherwise.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": "some string", "b": false });
    ///
    /// assert_eq!(v["a"].as_str(), Some("some string"));
    ///
    /// // The boolean `false` is not a string.
    /// assert_eq!(v["b"].as_str(), None);
    ///
    /// // JSON values are printed in JSON representation, so strings are in quotes.
    /// //
    /// //    The value is: "some string"
    /// println!("The value is: {}", v["a"]);
    ///
    /// // Rust strings are printed without quotes.
    /// //
    /// //    The value is: some string
    /// println!("The value is: {}", v["a"].as_str().unwrap());
    /// ```
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::String(s) => Some(s),
            _ => None,
        }
    }

    /// Returns true if the `Value` is a Number. Returns false otherwise.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": 1, "b": "2" });
    ///
    /// assert!(v["a"].is_number());
    ///
    /// // The string `"2"` is a string, not a number.
    /// assert!(!v["b"].is_number());
    /// ```
    pub fn is_number(&self) -> bool {
        match *self {
            Value::Number(_) => true,
            _ => false,
        }
    }

    /// If the `Value` is a Number, returns the associated [`Number`]. Returns
    /// None otherwise.
    ///
    /// ```
    /// # use serde_json::{json, Number};
    /// #
    /// let v = json!({ "a": 1, "b": 2.2, "c": -3, "d": "4" });
    ///
    /// assert_eq!(v["a"].as_number(), Some(&Number::from(1u64)));
    /// assert_eq!(v["b"].as_number(), Some(&Number::from_f64(2.2).unwrap()));
    /// assert_eq!(v["c"].as_number(), Some(&Number::from(-3i64)));
    ///
    /// // The string `"4"` is not a number.
    /// assert_eq!(v["d"].as_number(), None);
    /// ```
    pub fn as_number(&self) -> Option<&Number> {
        match self {
            Value::Number(number) => Some(number),
            _ => None,
        }
    }

    /// Returns true if the `Value` is an integer between `i64::MIN` and
    /// `i64::MAX`.
    ///
    /// For any Value on which `is_i64` returns true, `as_i64` is guaranteed to
    /// return the integer value.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let big = i64::max_value() as u64 + 10;
    /// let v = json!({ "a": 64, "b": big, "c": 256.0 });
    ///
    /// assert!(v["a"].is_i64());
    ///
    /// // Greater than i64::MAX.
    /// assert!(!v["b"].is_i64());
    ///
    /// // Numbers with a decimal point are not considered integers.
    /// assert!(!v["c"].is_i64());
    /// ```
    pub fn is_i64(&self) -> bool {
        match self {
            Value::Number(n) => n.is_i64(),
            _ => false,
        }
    }

    /// Returns true if the `Value` is an integer between zero and `u64::MAX`.
    ///
    /// For any Value on which `is_u64` returns true, `as_u64` is guaranteed to
    /// return the integer value.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": 64, "b": -64, "c": 256.0 });
    ///
    /// assert!(v["a"].is_u64());
    ///
    /// // Negative integer.
    /// assert!(!v["b"].is_u64());
    ///
    /// // Numbers with a decimal point are not considered integers.
    /// assert!(!v["c"].is_u64());
    /// ```
    pub fn is_u64(&self) -> bool {
        match self {
            Value::Number(n) => n.is_u64(),
            _ => false,
        }
    }

    /// Returns true if the `Value` is a number that can be represented by f64.
    ///
    /// For any Value on which `is_f64` returns true, `as_f64` is guaranteed to
    /// return the floating point value.
    ///
    /// Currently this function returns true if and only if both `is_i64` and
    /// `is_u64` return false but this is not a guarantee in the future.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": 256.0, "b": 64, "c": -64 });
    ///
    /// assert!(v["a"].is_f64());
    ///
    /// // Integers.
    /// assert!(!v["b"].is_f64());
    /// assert!(!v["c"].is_f64());
    /// ```
    pub fn is_f64(&self) -> bool {
        match self {
            Value::Number(n) => n.is_f64(),
            _ => false,
        }
    }

    /// If the `Value` is an integer, represent it as i64 if possible. Returns
    /// None otherwise.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let big = i64::max_value() as u64 + 10;
    /// let v = json!({ "a": 64, "b": big, "c": 256.0 });
    ///
    /// assert_eq!(v["a"].as_i64(), Some(64));
    /// assert_eq!(v["b"].as_i64(), None);
    /// assert_eq!(v["c"].as_i64(), None);
    /// ```
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Value::Number(n) => n.as_i64(),
            _ => None,
        }
    }

    /// If the `Value` is an integer, represent it as u64 if possible. Returns
    /// None otherwise.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": 64, "b": -64, "c": 256.0 });
    ///
    /// assert_eq!(v["a"].as_u64(), Some(64));
    /// assert_eq!(v["b"].as_u64(), None);
    /// assert_eq!(v["c"].as_u64(), None);
    /// ```
    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Value::Number(n) => n.as_u64(),
            _ => None,
        }
    }

    /// If the `Value` is a number, represent it as f64 if possible. Returns
    /// None otherwise.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": 256.0, "b": 64, "c": -64 });
    ///
    /// assert_eq!(v["a"].as_f64(), Some(256.0));
    /// assert_eq!(v["b"].as_f64(), Some(64.0));
    /// assert_eq!(v["c"].as_f64(), Some(-64.0));
    /// ```
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Value::Number(n) => n.as_f64(),
            _ => None,
        }
    }

    /// Returns true if the `Value` is a Boolean. Returns false otherwise.
    ///
    /// For any Value on which `is_boolean` returns true, `as_bool` is
    /// guaranteed to return the boolean value.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": false, "b": "false" });
    ///
    /// assert!(v["a"].is_boolean());
    ///
    /// // The string `"false"` is a string, not a boolean.
    /// assert!(!v["b"].is_boolean());
    /// ```
    pub fn is_boolean(&self) -> bool {
        self.as_bool().is_some()
    }

    /// If the `Value` is a Boolean, returns the associated bool. Returns None
    /// otherwise.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": false, "b": "false" });
    ///
    /// assert_eq!(v["a"].as_bool(), Some(false));
    ///
    /// // The string `"false"` is a string, not a boolean.
    /// assert_eq!(v["b"].as_bool(), None);
    /// ```
    pub fn as_bool(&self) -> Option<bool> {
        match *self {
            Value::Bool(b) => Some(b),
            _ => None,
        }
    }

    /// Returns true if the `Value` is a Null. Returns false otherwise.
    ///
    /// For any Value on which `is_null` returns true, `as_null` is guaranteed
    /// to return `Some(())`.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": null, "b": false });
    ///
    /// assert!(v["a"].is_null());
    ///
    /// // The boolean `false` is not null.
    /// assert!(!v["b"].is_null());
    /// ```
    pub fn is_null(&self) -> bool {
        self.as_null().is_some()
    }

    /// If the `Value` is a Null, returns (). Returns None otherwise.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let v = json!({ "a": null, "b": false });
    ///
    /// assert_eq!(v["a"].as_null(), Some(()));
    ///
    /// // The boolean `false` is not null.
    /// assert_eq!(v["b"].as_null(), None);
    /// ```
    pub fn as_null(&self) -> Option<()> {
        match *self {
            Value::Null => Some(()),
            _ => None,
        }
    }

    /// Looks up a value by a JSON Pointer.
    ///
    /// JSON Pointer defines a string syntax for identifying a specific value
    /// within a JavaScript Object Notation (JSON) document.
    ///
    /// A Pointer is a Unicode string with the reference tokens separated by `/`.
    /// Inside tokens `/` is replaced by `~1` and `~` is replaced by `~0`. The
    /// addressed value is returned and if there is no such value `None` is
    /// returned.
    ///
    /// For more information read [RFC6901](https://tools.ietf.org/html/rfc6901).
    ///
    /// # Examples
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let data = json!({
    ///     "x": {
    ///         "y": ["z", "zz"]
    ///     }
    /// });
    ///
    /// assert_eq!(data.pointer("/x/y/1").unwrap(), &json!("zz"));
    /// assert_eq!(data.pointer("/a/b/c"), None);
    /// ```
    pub fn pointer(&self, pointer: &str) -> Option<&Value> {
        if pointer.is_empty() {
            return Some(self);
        }
        if !pointer.starts_with('/') {
            return None;
        }
        pointer
            .split('/')
            .skip(1)
            .map(|x| x.replace("~1", "/").replace("~0", "~"))
            .try_fold(self, |target, token| match target {
                Value::Object(map) => map.get(&token),
                Value::Array(list) => parse_index(&token).and_then(|x| list.get(x)),
                _ => None,
            })
    }

    /// Looks up a value by a JSON Pointer and returns a mutable reference to
    /// that value.
    ///
    /// JSON Pointer defines a string syntax for identifying a specific value
    /// within a JavaScript Object Notation (JSON) document.
    ///
    /// A Pointer is a Unicode string with the reference tokens separated by `/`.
    /// Inside tokens `/` is replaced by `~1` and `~` is replaced by `~0`. The
    /// addressed value is returned and if there is no such value `None` is
    /// returned.
    ///
    /// For more information read [RFC6901](https://tools.ietf.org/html/rfc6901).
    ///
    /// # Example of Use
    ///
    /// ```
    /// use serde_json::Value;
    ///
    /// fn main() {
    ///     let s = r#"{"x": 1.0, "y": 2.0}"#;
    ///     let mut value: Value = serde_json::from_str(s).unwrap();
    ///
    ///     // Check value using read-only pointer
    ///     assert_eq!(value.pointer("/x"), Some(&1.0.into()));
    ///     // Change value with direct assignment
    ///     *value.pointer_mut("/x").unwrap() = 1.5.into();
    ///     // Check that new value was written
    ///     assert_eq!(value.pointer("/x"), Some(&1.5.into()));
    ///     // Or change the value only if it exists
    ///     value.pointer_mut("/x").map(|v| *v = 1.5.into());
    ///
    ///     // "Steal" ownership of a value. Can replace with any valid Value.
    ///     let old_x = value.pointer_mut("/x").map(Value::take).unwrap();
    ///     assert_eq!(old_x, 1.5);
    ///     assert_eq!(value.pointer("/x").unwrap(), &Value::Null);
    /// }
    /// ```
    pub fn pointer_mut(&mut self, pointer: &str) -> Option<&mut Value> {
        if pointer.is_empty() {
            return Some(self);
        }
        if !pointer.starts_with('/') {
            return None;
        }
        pointer
            .split('/')
            .skip(1)
            .map(|x| x.replace("~1", "/").replace("~0", "~"))
            .try_fold(self, |target, token| match target {
                Value::Object(map) => map.get_mut(&token),
                Value::Array(list) => parse_index(&token).and_then(move |x| list.get_mut(x)),
                _ => None,
            })
    }

    /// Takes the value out of the `Value`, leaving a `Null` in its place.
    ///
    /// ```
    /// # use serde_json::json;
    /// #
    /// let mut v = json!({ "x": "y" });
    /// assert_eq!(v["x"].take(), json!("y"));
    /// assert_eq!(v, json!({ "x": null }));
    /// ```
    pub fn take(&mut self) -> Value {
        mem::replace(self, Value::Null)
    }
}

/// The default value is `Value::Null`.
///
/// This is useful for handling omitted `Value` fields when deserializing.
///
/// # Examples
///
/// ```
/// # use serde::Deserialize;
/// use serde_json::Value;
///
/// #[derive(Deserialize)]
/// struct Settings {
///     level: i32,
///     #[serde(default)]
///     extras: Value,
/// }
///
/// # fn try_main() -> Result<(), serde_json::Error> {
/// let data = r#" { "level": 42 } "#;
/// let s: Settings = serde_json::from_str(data)?;
///
/// assert_eq!(s.level, 42);
/// assert_eq!(s.extras, Value::Null);
/// #
/// #     Ok(())
/// # }
/// #
/// # try_main().unwrap()
/// ```
impl Default for Value {
    fn default() -> Value {
        Value::Null
    }
}

mod de;
mod from;
mod index;
mod partial_eq;
mod ser;

/// Convert a `T` into `serde_json::Value` which is an enum that can represent
/// any valid JSON data.
///
/// # Example
///
/// ```
/// use serde::Serialize;
/// use serde_json::json;
/// use std::error::Error;
///
/// #[derive(Serialize)]
/// struct User {
///     fingerprint: String,
///     location: String,
/// }
///
/// fn compare_json_values() -> Result<(), Box<dyn Error>> {
///     let u = User {
///         fingerprint: "0xF9BA143B95FF6D82".to_owned(),
///         location: "Menlo Park, CA".to_owned(),
///     };
///
///     // The type of `expected` is `serde_json::Value`
///     let expected = json!({
///         "fingerprint": "0xF9BA143B95FF6D82",
///         "location": "Menlo Park, CA",
///     });
///
///     let v = serde_json::to_value(u).unwrap();
///     assert_eq!(v, expected);
///
///     Ok(())
/// }
/// #
/// # compare_json_values().unwrap();
/// ```
///
/// # Errors
///
/// This conversion can fail if `T`'s implementation of `Serialize` decides to
/// fail, or if `T` contains a map with non-string keys.
///
/// ```
/// use std::collections::BTreeMap;
///
/// fn main() {
///     // The keys in this map are vectors, not strings.
///     let mut map = BTreeMap::new();
///     map.insert(vec![32, 64], "x86");
///
///     println!("{}", serde_json::to_value(map).unwrap_err());
/// }
/// ```
// Taking by value is more friendly to iterator adapters, option and result
// consumers, etc. See https://github.com/serde-rs/json/pull/149.
pub fn to_value<T>(value: T) -> Result<Value, Error>
where
    T: Serialize,
{
    value.serialize(Serializer)
}

/// Interpret a `serde_json::Value` as an instance of type `T`.
///
/// # Example
///
/// ```
/// use serde::Deserialize;
/// use serde_json::json;
///
/// #[derive(Deserialize, Debug)]
/// struct User {
///     fingerprint: String,
///     location: String,
/// }
///
/// fn main() {
///     // The type of `j` is `serde_json::Value`
///     let j = json!({
///         "fingerprint": "0xF9BA143B95FF6D82",
///         "location": "Menlo Park, CA"
///     });
///
///     let u: User = serde_json::from_value(j).unwrap();
///     println!("{:#?}", u);
/// }
/// ```
///
/// # Errors
///
/// This conversion can fail if the structure of the Value does not match the
/// structure expected by `T`, for example if `T` is a struct type but the Value
/// contains something other than a JSON map. It can also fail if the structure
/// is correct but `T`'s implementation of `Deserialize` decides that something
/// is wrong with the data, for example required struct fields are missing from
/// the JSON map or some number is too big to fit in the expected primitive
/// type.
pub fn from_value<T>(value: Value) -> Result<T, Error>
where
    T: DeserializeOwned,
{
    T::deserialize(value)
}


// --- rs_tokio_runtime.rs ---
use crate::runtime::blocking::BlockingPool;
use crate::runtime::scheduler::CurrentThread;
use crate::runtime::{context, EnterGuard, Handle};
use crate::task::JoinHandle;

use std::future::Future;
use std::time::Duration;

cfg_rt_multi_thread! {
    use crate::runtime::Builder;
    use crate::runtime::scheduler::MultiThread;

    cfg_unstable! {
        use crate::runtime::scheduler::MultiThreadAlt;
    }
}

/// The Tokio runtime.
///
/// The runtime provides an I/O driver, task scheduler, [timer], and
/// blocking pool, necessary for running asynchronous tasks.
///
/// Instances of `Runtime` can be created using [`new`], or [`Builder`].
/// However, most users will use the [`#[tokio::main]`][main] annotation on
/// their entry point instead.
///
/// See [module level][mod] documentation for more details.
///
/// # Shutdown
///
/// Shutting down the runtime is done by dropping the value, or calling
/// [`shutdown_background`] or [`shutdown_timeout`].
///
/// Tasks spawned through [`Runtime::spawn`] keep running until they yield.
/// Then they are dropped. They are not *guaranteed* to run to completion, but
/// *might* do so if they do not yield until completion.
///
/// Blocking functions spawned through [`Runtime::spawn_blocking`] keep running
/// until they return.
///
/// The thread initiating the shutdown blocks until all spawned work has been
/// stopped. This can take an indefinite amount of time. The `Drop`
/// implementation waits forever for this.
///
/// The [`shutdown_background`] and [`shutdown_timeout`] methods can be used if
/// waiting forever is undesired. When the timeout is reached, spawned work that
/// did not stop in time and threads running it are leaked. The work continues
/// to run until one of the stopping conditions is fulfilled, but the thread
/// initiating the shutdown is unblocked.
///
/// Once the runtime has been dropped, any outstanding I/O resources bound to
/// it will no longer function. Calling any method on them will result in an
/// error.
///
/// # Sharing
///
/// There are several ways to establish shared access to a Tokio runtime:
///
///  * Using an <code>[Arc]\<Runtime></code>.
///  * Using a [`Handle`].
///  * Entering the runtime context.
///
/// Using an <code>[Arc]\<Runtime></code> or [`Handle`] allows you to do various
/// things with the runtime such as spawning new tasks or entering the runtime
/// context. Both types can be cloned to create a new handle that allows access
/// to the same runtime. By passing clones into different tasks or threads, you
/// will be able to access the runtime from those tasks or threads.
///
/// The difference between <code>[Arc]\<Runtime></code> and [`Handle`] is that
/// an <code>[Arc]\<Runtime></code> will prevent the runtime from shutting down,
/// whereas a [`Handle`] does not prevent that. This is because shutdown of the
/// runtime happens when the destructor of the `Runtime` object runs.
///
/// Calls to [`shutdown_background`] and [`shutdown_timeout`] require exclusive
/// ownership of the `Runtime` type. When using an <code>[Arc]\<Runtime></code>,
/// this can be achieved via [`Arc::try_unwrap`] when only one strong count
/// reference is left over.
///
/// The runtime context is entered using the [`Runtime::enter`] or
/// [`Handle::enter`] methods, which use a thread-local variable to store the
/// current runtime. Whenever you are inside the runtime context, methods such
/// as [`tokio::spawn`] will use the runtime whose context you are inside.
///
/// [timer]: crate::time
/// [mod]: index.html
/// [`new`]: method@Self::new
/// [`Builder`]: struct@Builder
/// [`Handle`]: struct@Handle
/// [main]: macro@crate::main
/// [`tokio::spawn`]: crate::spawn
/// [`Arc::try_unwrap`]: std::sync::Arc::try_unwrap
/// [Arc]: std::sync::Arc
/// [`shutdown_background`]: method@Runtime::shutdown_background
/// [`shutdown_timeout`]: method@Runtime::shutdown_timeout
#[derive(Debug)]
pub struct Runtime {
    /// Task scheduler
    scheduler: Scheduler,

    /// Handle to runtime, also contains driver handles
    handle: Handle,

    /// Blocking pool handle, used to signal shutdown
    blocking_pool: BlockingPool,
}

/// The flavor of a `Runtime`.
///
/// This is the return type for [`Handle::runtime_flavor`](crate::runtime::Handle::runtime_flavor()).
#[derive(Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum RuntimeFlavor {
    /// The flavor that executes all tasks on the current thread.
    CurrentThread,
    /// The flavor that executes tasks across multiple threads.
    MultiThread,
    /// The flavor that executes tasks across multiple threads.
    #[cfg(tokio_unstable)]
    MultiThreadAlt,
}

/// The runtime scheduler is either a multi-thread or a current-thread executor.
#[derive(Debug)]
pub(super) enum Scheduler {
    /// Execute all tasks on the current-thread.
    CurrentThread(CurrentThread),

    /// Execute tasks across multiple threads.
    #[cfg(feature = "rt-multi-thread")]
    MultiThread(MultiThread),

    /// Execute tasks across multiple threads.
    #[cfg(all(tokio_unstable, feature = "rt-multi-thread"))]
    MultiThreadAlt(MultiThreadAlt),
}

impl Runtime {
    pub(super) fn from_parts(
        scheduler: Scheduler,
        handle: Handle,
        blocking_pool: BlockingPool,
    ) -> Runtime {
        Runtime {
            scheduler,
            handle,
            blocking_pool,
        }
    }

    /// Creates a new runtime instance with default configuration values.
    ///
    /// This results in the multi threaded scheduler, I/O driver, and time driver being
    /// initialized.
    ///
    /// Most applications will not need to call this function directly. Instead,
    /// they will use the  [`#[tokio::main]` attribute][main]. When a more complex
    /// configuration is necessary, the [runtime builder] may be used.
    ///
    /// See [module level][mod] documentation for more details.
    ///
    /// # Examples
    ///
    /// Creating a new `Runtime` with default configuration values.
    ///
    /// ```
    /// use tokio::runtime::Runtime;
    ///
    /// let rt = Runtime::new()
    ///     .unwrap();
    ///
    /// // Use the runtime...
    /// ```
    ///
    /// [mod]: index.html
    /// [main]: ../attr.main.html
    /// [threaded scheduler]: index.html#threaded-scheduler
    /// [runtime builder]: crate::runtime::Builder
    #[cfg(feature = "rt-multi-thread")]
    #[cfg_attr(docsrs, doc(cfg(feature = "rt-multi-thread")))]
    pub fn new() -> std::io::Result<Runtime> {
        Builder::new_multi_thread().enable_all().build()
    }

    /// Returns a handle to the runtime's spawner.
    ///
    /// The returned handle can be used to spawn tasks that run on this runtime, and can
    /// be cloned to allow moving the `Handle` to other threads.
    ///
    /// Calling [`Handle::block_on`] on a handle to a `current_thread` runtime is error-prone.
    /// Refer to the documentation of [`Handle::block_on`] for more.
    ///
    /// # Examples
    ///
    /// ```
    /// use tokio::runtime::Runtime;
    ///
    /// let rt = Runtime::new()
    ///     .unwrap();
    ///
    /// let handle = rt.handle();
    ///
    /// // Use the handle...
    /// ```
    pub fn handle(&self) -> &Handle {
        &self.handle
    }

    /// Spawns a future onto the Tokio runtime.
    ///
    /// This spawns the given future onto the runtime's executor, usually a
    /// thread pool. The thread pool is then responsible for polling the future
    /// until it completes.
    ///
    /// The provided future will start running in the background immediately
    /// when `spawn` is called, even if you don't await the returned
    /// `JoinHandle`.
    ///
    /// See [module level][mod] documentation for more details.
    ///
    /// [mod]: index.html
    ///
    /// # Examples
    ///
    /// ```
    /// use tokio::runtime::Runtime;
    ///
    /// # fn dox() {
    /// // Create the runtime
    /// let rt = Runtime::new().unwrap();
    ///
    /// // Spawn a future onto the runtime
    /// rt.spawn(async {
    ///     println!("now running on a worker thread");
    /// });
    /// # }
    /// ```
    #[track_caller]
    pub fn spawn<F>(&self, future: F) -> JoinHandle<F::Output>
    where
        F: Future + Send + 'static,
        F::Output: Send + 'static,
    {
        self.handle.spawn(future)
    }

    /// Runs the provided function on an executor dedicated to blocking operations.
    ///
    /// # Examples
    ///
    /// ```
    /// use tokio::runtime::Runtime;
    ///
    /// # fn dox() {
    /// // Create the runtime
    /// let rt = Runtime::new().unwrap();
    ///
    /// // Spawn a blocking function onto the runtime
    /// rt.spawn_blocking(|| {
    ///     println!("now running on a worker thread");
    /// });
    /// # }
    /// ```
    #[track_caller]
    pub fn spawn_blocking<F, R>(&self, func: F) -> JoinHandle<R>
    where
        F: FnOnce() -> R + Send + 'static,
        R: Send + 'static,
    {
        self.handle.spawn_blocking(func)
    }

    /// Runs a future to completion on the Tokio runtime. This is the
    /// runtime's entry point.
    ///
    /// This runs the given future on the current thread, blocking until it is
    /// complete, and yielding its resolved result. Any tasks or timers
    /// which the future spawns internally will be executed on the runtime.
    ///
    /// # Non-worker future
    ///
    /// Note that the future required by this function does not run as a
    /// worker. The expectation is that other tasks are spawned by the future here.
    /// Awaiting on other futures from the future provided here will not
    /// perform as fast as those spawned as workers.
    ///
    /// # Multi thread scheduler
    ///
    /// When the multi thread scheduler is used this will allow futures
    /// to run within the io driver and timer context of the overall runtime.
    ///
    /// Any spawned tasks will continue running after `block_on` returns.
    ///
    /// # Current thread scheduler
    ///
    /// When the current thread scheduler is enabled `block_on`
    /// can be called concurrently from multiple threads. The first call
    /// will take ownership of the io and timer drivers. This means
    /// other threads which do not own the drivers will hook into that one.
    /// When the first `block_on` completes, other threads will be able to
    /// "steal" the driver to allow continued execution of their futures.
    ///
    /// Any spawned tasks will be suspended after `block_on` returns. Calling
    /// `block_on` again will resume previously spawned tasks.
    ///
    /// # Panics
    ///
    /// This function panics if the provided future panics, or if called within an
    /// asynchronous execution context.
    ///
    /// # Examples
    ///
    /// ```no_run
    /// use tokio::runtime::Runtime;
    ///
    /// // Create the runtime
    /// let rt  = Runtime::new().unwrap();
    ///
    /// // Execute the future, blocking the current thread until completion
    /// rt.block_on(async {
    ///     println!("hello");
    /// });
    /// ```
    ///
    /// [handle]: fn@Handle::block_on
    #[track_caller]
    pub fn block_on<F: Future>(&self, future: F) -> F::Output {
        #[cfg(all(
            tokio_unstable,
            tokio_taskdump,
            feature = "rt",
            target_os = "linux",
            any(target_arch = "aarch64", target_arch = "x86", target_arch = "x86_64")
        ))]
        let future = super::task::trace::Trace::root(future);

        #[cfg(all(tokio_unstable, feature = "tracing"))]
        let future = crate::util::trace::task(
            future,
            "block_on",
            None,
            crate::runtime::task::Id::next().as_u64(),
        );

        let _enter = self.enter();

        match &self.scheduler {
            Scheduler::CurrentThread(exec) => exec.block_on(&self.handle.inner, future),
            #[cfg(feature = "rt-multi-thread")]
            Scheduler::MultiThread(exec) => exec.block_on(&self.handle.inner, future),
            #[cfg(all(tokio_unstable, feature = "rt-multi-thread"))]
            Scheduler::MultiThreadAlt(exec) => exec.block_on(&self.handle.inner, future),
        }
    }

    /// Enters the runtime context.
    ///
    /// This allows you to construct types that must have an executor
    /// available on creation such as [`Sleep`] or [`TcpStream`]. It will
    /// also allow you to call methods such as [`tokio::spawn`].
    ///
    /// [`Sleep`]: struct@crate::time::Sleep
    /// [`TcpStream`]: struct@crate::net::TcpStream
    /// [`tokio::spawn`]: fn@crate::spawn
    ///
    /// # Example
    ///
    /// ```
    /// use tokio::runtime::Runtime;
    /// use tokio::task::JoinHandle;
    ///
    /// fn function_that_spawns(msg: String) -> JoinHandle<()> {
    ///     // Had we not used `rt.enter` below, this would panic.
    ///     tokio::spawn(async move {
    ///         println!("{}", msg);
    ///     })
    /// }
    ///
    /// fn main() {
    ///     let rt = Runtime::new().unwrap();
    ///
    ///     let s = "Hello World!".to_string();
    ///
    ///     // By entering the context, we tie `tokio::spawn` to this executor.
    ///     let _guard = rt.enter();
    ///     let handle = function_that_spawns(s);
    ///
    ///     // Wait for the task before we end the test.
    ///     rt.block_on(handle).unwrap();
    /// }
    /// ```
    pub fn enter(&self) -> EnterGuard<'_> {
        self.handle.enter()
    }

    /// Shuts down the runtime, waiting for at most `duration` for all spawned
    /// work to stop.
    ///
    /// See the [struct level documentation](Runtime#shutdown) for more details.
    ///
    /// # Examples
    ///
    /// ```
    /// use tokio::runtime::Runtime;
    /// use tokio::task;
    ///
    /// use std::thread;
    /// use std::time::Duration;
    ///
    /// fn main() {
    ///    let runtime = Runtime::new().unwrap();
    ///
    ///    runtime.block_on(async move {
    ///        task::spawn_blocking(move || {
    ///            thread::sleep(Duration::from_secs(10_000));
    ///        });
    ///    });
    ///
    ///    runtime.shutdown_timeout(Duration::from_millis(100));
    /// }
    /// ```
    pub fn shutdown_timeout(mut self, duration: Duration) {
        // Wakeup and shutdown all the worker threads
        self.handle.inner.shutdown();
        self.blocking_pool.shutdown(Some(duration));
    }

    /// Shuts down the runtime, without waiting for any spawned work to stop.
    ///
    /// This can be useful if you want to drop a runtime from within another runtime.
    /// Normally, dropping a runtime will block indefinitely for spawned blocking tasks
    /// to complete, which would normally not be permitted within an asynchronous context.
    /// By calling `shutdown_background()`, you can drop the runtime from such a context.
    ///
    /// Note however, that because we do not wait for any blocking tasks to complete, this
    /// may result in a resource leak (in that any blocking tasks are still running until they
    /// return.
    ///
    /// See the [struct level documentation](Runtime#shutdown) for more details.
    ///
    /// This function is equivalent to calling `shutdown_timeout(Duration::from_nanos(0))`.
    ///
    /// ```
    /// use tokio::runtime::Runtime;
    ///
    /// fn main() {
    ///    let runtime = Runtime::new().unwrap();
    ///
    ///    runtime.block_on(async move {
    ///        let inner_runtime = Runtime::new().unwrap();
    ///        // ...
    ///        inner_runtime.shutdown_background();
    ///    });
    /// }
    /// ```
    pub fn shutdown_background(self) {
        self.shutdown_timeout(Duration::from_nanos(0));
    }

    /// Returns a view that lets you get information about how the runtime
    /// is performing.
    pub fn metrics(&self) -> crate::runtime::RuntimeMetrics {
        self.handle.metrics()
    }
}

#[allow(clippy::single_match)] // there are comments in the error branch, so we don't want if-let
impl Drop for Runtime {
    fn drop(&mut self) {
        match &mut self.scheduler {
            Scheduler::CurrentThread(current_thread) => {
                // This ensures that tasks spawned on the current-thread
                // runtime are dropped inside the runtime's context.
                let _guard = context::try_set_current(&self.handle.inner);
                current_thread.shutdown(&self.handle.inner);
            }
            #[cfg(feature = "rt-multi-thread")]
            Scheduler::MultiThread(multi_thread) => {
                // The threaded scheduler drops its tasks on its worker threads, which is
                // already in the runtime's context.
                multi_thread.shutdown(&self.handle.inner);
            }
            #[cfg(all(tokio_unstable, feature = "rt-multi-thread"))]
            Scheduler::MultiThreadAlt(multi_thread) => {
                // The threaded scheduler drops its tasks on its worker threads, which is
                // already in the runtime's context.
                multi_thread.shutdown(&self.handle.inner);
            }
        }
    }
}

impl std::panic::UnwindSafe for Runtime {}

impl std::panic::RefUnwindSafe for Runtime {}
