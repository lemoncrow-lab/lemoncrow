// Synthetic A/B fixture — Swift async HTTP cache layer.
// NOT for production use. Generated to exercise tree-sitter outline.

import Foundation

// MARK: - Errors

public enum CacheError: Error, LocalizedError {
    case keyNotFound(String)
    case encodingFailed(Error)
    case decodingFailed(Error)
    case storageFull(capacity: Int)

    public var errorDescription: String? {
        switch self {
        case .keyNotFound(let k): return "Key not found: \(k)"
        case .encodingFailed(let e): return "Encoding failed: \(e.localizedDescription)"
        case .decodingFailed(let e): return "Decoding failed: \(e.localizedDescription)"
        case .storageFull(let cap): return "Cache is full (capacity: \(cap))"
        }
    }
}

// MARK: - Protocol

public protocol AsyncCache<Value>: Sendable {
    associatedtype Value: Codable & Sendable
    func get(forKey key: String) async throws -> Value
    func set(_ value: Value, forKey key: String, ttl: TimeInterval?) async throws
    func delete(forKey key: String) async throws -> Bool
    func flush() async
    func stats() async -> CacheStats
}

public struct CacheStats: Sendable {
    public var hits: Int
    public var misses: Int
    public var size: Int
    public var evictions: Int

    public var hitRate: Double {
        let total = hits + misses
        guard total > 0 else { return 0 }
        return Double(hits) / Double(total)
    }
}

// MARK: - Entry

private struct CacheEntry<V: Codable>: Codable {
    let value: V
    let insertedAt: Date
    let ttl: TimeInterval?
    var accessCount: Int

    var isExpired: Bool {
        guard let ttl else { return false }
        return Date().timeIntervalSince(insertedAt) > ttl
    }
}

// MARK: - In-Memory Implementation

public actor MemoryCache<V: Codable & Sendable>: AsyncCache {
    public typealias Value = V

    private var store: [String: CacheEntry<V>] = [:]
    private var insertionOrder: [String] = []
    private let capacity: Int
    private var stats_: CacheStats = CacheStats(hits: 0, misses: 0, size: 0, evictions: 0)

    public init(capacity: Int = 256) {
        self.capacity = capacity
    }

    public func get(forKey key: String) async throws -> V {
        guard var entry = store[key] else {
            stats_.misses += 1
            throw CacheError.keyNotFound(key)
        }
        if entry.isExpired {
            store.removeValue(forKey: key)
            insertionOrder.removeAll { $0 == key }
            stats_.misses += 1
            throw CacheError.keyNotFound(key)
        }
        entry.accessCount += 1
        store[key] = entry
        stats_.hits += 1
        return entry.value
    }

    public func set(_ value: V, forKey key: String, ttl: TimeInterval? = nil) async throws {
        if store[key] == nil && store.count >= capacity {
            evictLRU()
        }
        if store[key] == nil {
            insertionOrder.append(key)
        }
        store[key] = CacheEntry(value: value, insertedAt: Date(), ttl: ttl, accessCount: 0)
        stats_.size = store.count
    }

    public func delete(forKey key: String) async throws -> Bool {
        guard store.removeValue(forKey: key) != nil else { return false }
        insertionOrder.removeAll { $0 == key }
        stats_.size = store.count
        return true
    }

    public func flush() async {
        store.removeAll()
        insertionOrder.removeAll()
        stats_.size = 0
    }

    public func stats() async -> CacheStats {
        return stats_
    }

    public func purgeExpired() async -> Int {
        let before = store.count
        let expired = store.filter { $0.value.isExpired }.map { $0.key }
        expired.forEach { key in
            store.removeValue(forKey: key)
            insertionOrder.removeAll { $0 == key }
        }
        stats_.size = store.count
        return before - store.count
    }

    private func evictLRU() {
        guard let oldest = insertionOrder.first else { return }
        store.removeValue(forKey: oldest)
        insertionOrder.removeFirst()
        stats_.evictions += 1
    }
}

// MARK: - Layered Cache

public actor LayeredCache<V: Codable & Sendable>: AsyncCache {
    public typealias Value = V

    private let l1: MemoryCache<V>
    private let l2: MemoryCache<V>

    public init(l1Capacity: Int = 64, l2Capacity: Int = 512) {
        l1 = MemoryCache(capacity: l1Capacity)
        l2 = MemoryCache(capacity: l2Capacity)
    }

    public func get(forKey key: String) async throws -> V {
        if let v = try? await l1.get(forKey: key) { return v }
        let v = try await l2.get(forKey: key)
        try await l1.set(v, forKey: key)
        return v
    }

    public func set(_ value: V, forKey key: String, ttl: TimeInterval? = nil) async throws {
        try await l1.set(value, forKey: key, ttl: ttl)
        try await l2.set(value, forKey: key, ttl: ttl)
    }

    public func delete(forKey key: String) async throws -> Bool {
        let r1 = try await l1.delete(forKey: key)
        let r2 = try await l2.delete(forKey: key)
        return r1 || r2
    }

    public func flush() async {
        await l1.flush()
        await l2.flush()
    }

    public func stats() async -> CacheStats {
        let s1 = await l1.stats()
        let s2 = await l2.stats()
        return CacheStats(
            hits: s1.hits + s2.hits,
            misses: s1.misses + s2.misses,
            size: s1.size + s2.size,
            evictions: s1.evictions + s2.evictions
        )
    }
}

// MARK: - Factory

public enum CacheFactory {
    public static func memory<V: Codable & Sendable>(capacity: Int = 256) -> MemoryCache<V> {
        MemoryCache(capacity: capacity)
    }

    public static func layered<V: Codable & Sendable>(
        l1Capacity: Int = 64,
        l2Capacity: Int = 512
    ) -> LayeredCache<V> {
        LayeredCache(l1Capacity: l1Capacity, l2Capacity: l2Capacity)
    }
}
