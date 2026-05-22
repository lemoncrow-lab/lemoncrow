// Synthetic A/B fixture — C# event bus with typed subscriptions.
// NOT for production use. Generated to exercise tree-sitter outline.

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

namespace EventBus
{
    public interface IEvent { }

    public interface IHandler<in TEvent> where TEvent : IEvent
    {
        Task HandleAsync(TEvent evt, CancellationToken ct = default);
    }

    public interface IEventBus
    {
        IDisposable Subscribe<TEvent>(IHandler<TEvent> handler) where TEvent : IEvent;
        IDisposable Subscribe<TEvent>(Func<TEvent, CancellationToken, Task> handler) where TEvent : IEvent;
        Task PublishAsync<TEvent>(TEvent evt, CancellationToken ct = default) where TEvent : IEvent;
        Task PublishAsync(IEvent evt, CancellationToken ct = default);
    }

    public sealed class EventBusException : Exception
    {
        public EventBusException(string message, Exception? inner = null)
            : base(message, inner) { }
    }

    public sealed class HandlerRegistration : IDisposable
    {
        private readonly Action _remove;
        private int _disposed;

        internal HandlerRegistration(Action remove)
        {
            _remove = remove;
        }

        public void Dispose()
        {
            if (Interlocked.Exchange(ref _disposed, 1) == 0)
                _remove();
        }
    }

    public sealed class EventBusOptions
    {
        public bool ThrowOnHandlerError { get; init; } = false;
        public int MaxConcurrentHandlers { get; init; } = 16;
        public TimeSpan DefaultTimeout { get; init; } = TimeSpan.FromSeconds(30);
    }

    public sealed class EventBusMetrics
    {
        public long Published { get; internal set; }
        public long Delivered { get; internal set; }
        public long Errors { get; internal set; }
        public long ActiveSubscriptions { get; internal set; }
    }

    public sealed class InProcessEventBus : IEventBus, IDisposable
    {
        private readonly EventBusOptions _options;
        private readonly ConcurrentDictionary<Type, List<WeakReference<object>>> _handlers = new();
        private readonly ReaderWriterLockSlim _lock = new(LockRecursionPolicy.NoRecursion);
        private long _published;
        private long _delivered;
        private long _errors;
        private bool _disposed;

        public InProcessEventBus(EventBusOptions? options = null)
        {
            _options = options ?? new EventBusOptions();
        }

        public IDisposable Subscribe<TEvent>(IHandler<TEvent> handler) where TEvent : IEvent
        {
            return Subscribe<TEvent>((evt, ct) => handler.HandleAsync(evt, ct));
        }

        public IDisposable Subscribe<TEvent>(Func<TEvent, CancellationToken, Task> handler) where TEvent : IEvent
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            var type = typeof(TEvent);
            var wrapper = new FuncHandlerWrapper<TEvent>(handler);
            var weakRef = new WeakReference<object>(wrapper);

            _lock.EnterWriteLock();
            try
            {
                if (!_handlers.TryGetValue(type, out var list))
                {
                    list = new List<WeakReference<object>>();
                    _handlers[type] = list;
                }
                list.Add(weakRef);
            }
            finally { _lock.ExitWriteLock(); }

            return new HandlerRegistration(() => RemoveHandler(type, weakRef));
        }

        public async Task PublishAsync<TEvent>(TEvent evt, CancellationToken ct = default) where TEvent : IEvent
        {
            await PublishAsync((IEvent)evt, ct);
        }

        public async Task PublishAsync(IEvent evt, CancellationToken ct = default)
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            Interlocked.Increment(ref _published);

            var type = evt.GetType();
            List<WeakReference<object>> snapshot;
            _lock.EnterReadLock();
            try
            {
                if (!_handlers.TryGetValue(type, out var list)) return;
                snapshot = list.ToList();
            }
            finally { _lock.ExitReadLock(); }

            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(_options.DefaultTimeout);

            var tasks = new List<Task>(snapshot.Count);
            foreach (var weakRef in snapshot)
            {
                if (!weakRef.TryGetTarget(out var target)) continue;
                if (target is IHandlerInvoker invoker)
                {
                    tasks.Add(InvokeHandler(invoker, evt, cts.Token));
                }
            }

            if (tasks.Count == 0) return;

            using var semaphore = new SemaphoreSlim(_options.MaxConcurrentHandlers);
            var throttled = tasks.Select(async t =>
            {
                await semaphore.WaitAsync(cts.Token);
                try { await t; }
                finally { semaphore.Release(); }
            });

            await Task.WhenAll(throttled);
        }

        private async Task InvokeHandler(IHandlerInvoker invoker, IEvent evt, CancellationToken ct)
        {
            try
            {
                await invoker.InvokeAsync(evt, ct);
                Interlocked.Increment(ref _delivered);
            }
            catch (Exception ex)
            {
                Interlocked.Increment(ref _errors);
                if (_options.ThrowOnHandlerError)
                    throw new EventBusException($"Handler failed for {evt.GetType().Name}", ex);
            }
        }

        private void RemoveHandler(Type type, WeakReference<object> weakRef)
        {
            _lock.EnterWriteLock();
            try
            {
                if (_handlers.TryGetValue(type, out var list))
                    list.Remove(weakRef);
            }
            finally { _lock.ExitWriteLock(); }
        }

        public EventBusMetrics GetMetrics()
        {
            long subs = _handlers.Values.Sum(l => l.Count(r => r.TryGetTarget(out _)));
            return new EventBusMetrics
            {
                Published = Interlocked.Read(ref _published),
                Delivered = Interlocked.Read(ref _delivered),
                Errors = Interlocked.Read(ref _errors),
                ActiveSubscriptions = subs,
            };
        }

        public void Dispose()
        {
            if (_disposed) return;
            _disposed = true;
            _lock.Dispose();
        }

        private interface IHandlerInvoker
        {
            Task InvokeAsync(IEvent evt, CancellationToken ct);
        }

        private sealed class FuncHandlerWrapper<TEvent> : IHandlerInvoker where TEvent : IEvent
        {
            private readonly Func<TEvent, CancellationToken, Task> _fn;

            public FuncHandlerWrapper(Func<TEvent, CancellationToken, Task> fn)
            {
                _fn = fn;
            }

            public Task InvokeAsync(IEvent evt, CancellationToken ct)
            {
                return evt is TEvent typed ? _fn(typed, ct) : Task.CompletedTask;
            }
        }
    }
}
