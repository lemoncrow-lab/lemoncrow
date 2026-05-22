// Synthetic A/B fixture — Kotlin key-value store with TTL and LRU eviction.
// NOT for production use. Generated to exercise tree-sitter outline.

package store

import java.time.Instant
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.locks.ReentrantReadWriteLock
import kotlin.concurrent.read
import kotlin.concurrent.write
import kotlin.math.min

data class StoreEntry<V>(
    val value: V,
    val insertedAt: Instant,
    val ttlSeconds: Long?,
    var accessCount: Long = 0,
    var lastAccessed: Instant = Instant.now(),
)

data class StoreStats(
    val size: Int,
    val hits: Long,
    val misses: Long,
    val evictions: Long,
    val expirations: Long,
)

class StoreException(message: String, cause: Throwable? = null) : RuntimeException(message, cause)

interface KVStore<K, V> {
    fun get(key: K): V?
    fun put(key: K, value: V, ttlSeconds: Long? = null)
    fun remove(key: K): Boolean
    fun contains(key: K): Boolean
    fun size(): Int
    fun clear()
    fun stats(): StoreStats
}

class LruTtlStore<K, V>(
    private val capacity: Int = 1024,
    private val defaultTtlSeconds: Long? = null,
) : KVStore<K, V> {

    private val map = LinkedHashMap<K, StoreEntry<V>>(capacity, 0.75f, true)
    private val lock = ReentrantReadWriteLock()
    private var hits = 0L
    private var misses = 0L
    private var evictions = 0L
    private var expirations = 0L

    override fun get(key: K): V? = lock.read {
        val entry = map[key] ?: run { misses++; return@read null }
        val ttl = entry.ttlSeconds ?: defaultTtlSeconds
        if (ttl != null && entry.insertedAt.plusSeconds(ttl).isBefore(Instant.now())) {
            lock.write { map.remove(key); expirations++ }
            misses++
            return@read null
        }
        entry.accessCount++
        entry.lastAccessed = Instant.now()
        hits++
        entry.value
    }

    override fun put(key: K, value: V, ttlSeconds: Long?) = lock.write {
        if (map.size >= capacity && !map.containsKey(key)) {
            val oldest = map.keys.first()
            map.remove(oldest)
            evictions++
        }
        map[key] = StoreEntry(
            value = value,
            insertedAt = Instant.now(),
            ttlSeconds = ttlSeconds ?: defaultTtlSeconds,
        )
    }

    override fun remove(key: K): Boolean = lock.write { map.remove(key) != null }

    override fun contains(key: K): Boolean = lock.read { map.containsKey(key) }

    override fun size(): Int = lock.read { map.size }

    override fun clear() = lock.write { map.clear() }

    override fun stats(): StoreStats = lock.read {
        StoreStats(
            size = map.size,
            hits = hits,
            misses = misses,
            evictions = evictions,
            expirations = expirations,
        )
    }

    fun purgeExpired(): Int = lock.write {
        val now = Instant.now()
        val before = map.size
        val toRemove = map.entries.filter { (_, e) ->
            val ttl = e.ttlSeconds ?: defaultTtlSeconds
            ttl != null && e.insertedAt.plusSeconds(ttl).isBefore(now)
        }.map { it.key }
        toRemove.forEach { map.remove(it); expirations++ }
        before - map.size
    }

    fun hotKeys(limit: Int = 10): List<K> = lock.read {
        map.entries
            .sortedByDescending { it.value.accessCount }
            .take(min(limit, map.size))
            .map { it.key }
    }

    fun snapshot(): Map<K, V> = lock.read {
        map.entries.associate { (k, e) -> k to e.value }
    }
}

class NamespacedStore<K, V>(
    private val delegate: KVStore<K, V>,
    private val namespace: String,
    private val separator: String = ":",
) {
    @Suppress("UNCHECKED_CAST")
    private fun ns(key: K): K = "$namespace$separator$key" as K

    fun get(key: K): V? = delegate.get(ns(key))
    fun put(key: K, value: V, ttlSeconds: Long? = null) = delegate.put(ns(key), value, ttlSeconds)
    fun remove(key: K): Boolean = delegate.remove(ns(key))
    fun contains(key: K): Boolean = delegate.contains(ns(key))
}

object StoreFactory {
    fun <K, V> lruTtl(
        capacity: Int = 1024,
        defaultTtlSeconds: Long? = null,
    ): LruTtlStore<K, V> = LruTtlStore(capacity, defaultTtlSeconds)

    fun <K, V> namespaced(
        namespace: String,
        capacity: Int = 1024,
    ): NamespacedStore<K, V> {
        val inner = LruTtlStore<K, V>(capacity)
        return NamespacedStore(inner, namespace)
    }
}

fun <K, V> KVStore<K, V>.getOrPut(key: K, ttlSeconds: Long? = null, block: () -> V): V {
    return get(key) ?: block().also { put(key, it, ttlSeconds) }
}

fun <K, V> KVStore<K, V>.getOrThrow(key: K): V {
    return get(key) ?: throw StoreException("Key not found: $key")
}

fun <K, V> KVStore<K, V>.putAll(entries: Map<K, V>, ttlSeconds: Long? = null) {
    entries.forEach { (k, v) -> put(k, v, ttlSeconds) }
    }

    fun <K, V> KVStore<K, V>.computeIfAbsent(key: K, ttlSeconds: Long? = null, compute: (K) -> V): V {
    return get(key) ?: compute(key).also { put(key, it, ttlSeconds) }
    }

    // ---------- Metrics store ----------

    data class MetricPoint(
    val name: String,
    val value: Double,
    val tags: Map<String, String> = emptyMap(),
    val timestamp: Instant = Instant.now(),
    )

    interface MetricSink {
    fun record(point: MetricPoint)
    fun flush(): List<MetricPoint>
    }

    class InMemoryMetricSink : MetricSink {
    private val buffer = mutableListOf<MetricPoint>()
    private val lock = ReentrantReadWriteLock()

    override fun record(point: MetricPoint) = lock.write { buffer.add(point) }

    override fun flush(): List<MetricPoint> = lock.write {
        val copy = buffer.toList()
        buffer.clear()
        copy
    }

    fun size(): Int = lock.read { buffer.size }
    }

    class TaggedMetricSink(
    private val delegate: MetricSink,
    private val baseTags: Map<String, String>,
    ) : MetricSink {
    override fun record(point: MetricPoint) {
        delegate.record(point.copy(tags = baseTags + point.tags))
    }

    override fun flush(): List<MetricPoint> = delegate.flush()
    }

    class SamplingMetricSink(
    private val delegate: MetricSink,
    private val sampleRate: Double = 0.1,
    ) : MetricSink {
    override fun record(point: MetricPoint) {
        if (Math.random() < sampleRate) delegate.record(point)
    }

    override fun flush(): List<MetricPoint> = delegate.flush()
    }

    // ---------- Store metrics integration ----------

    fun <K, V> LruTtlStore<K, V>.recordMetrics(sink: MetricSink, name: String) {
    val s = stats()
    sink.record(MetricPoint("$name.hits", s.hits.toDouble()))
    sink.record(MetricPoint("$name.misses", s.misses.toDouble()))
    sink.record(MetricPoint("$name.evictions", s.evictions.toDouble()))
    sink.record(MetricPoint("$name.size", s.size.toDouble()))
    }

    object MetricSinkFactory {
    fun inMemory(): InMemoryMetricSink = InMemoryMetricSink()
    fun tagged(delegate: MetricSink, vararg tags: Pair<String, String>): TaggedMetricSink =
        TaggedMetricSink(delegate, mapOf(*tags))
    fun sampling(delegate: MetricSink, rate: Double = 0.1): SamplingMetricSink =
        SamplingMetricSink(delegate, rate)
    }
