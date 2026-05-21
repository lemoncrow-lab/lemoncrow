//! Sample Rust file used as an A/B fixture for the generic outline fallback.
//! Patterned after real Rust crates — not copied from any specific project.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

/// In-memory cache with TTL and a fixed maximum capacity (LRU eviction).
pub struct Cache<K: Eq + std::hash::Hash + Clone, V: Clone> {
    inner: Arc<RwLock<Inner<K, V>>>,
    ttl: Duration,
    capacity: usize,
}

struct Inner<K, V> {
    map: HashMap<K, Entry<V>>,
    order: Vec<K>,
}

struct Entry<V> {
    value: V,
    inserted: Instant,
    hits: u64,
}

impl<K, V> Cache<K, V>
where
    K: Eq + std::hash::Hash + Clone,
    V: Clone,
{
    pub fn new(capacity: usize, ttl: Duration) -> Self {
        Self {
            inner: Arc::new(RwLock::new(Inner {
                map: HashMap::with_capacity(capacity),
                order: Vec::with_capacity(capacity),
            })),
            ttl,
            capacity,
        }
    }

    pub fn get(&self, key: &K) -> Option<V> {
        let mut guard = self.inner.write().ok()?;
        let entry = guard.map.get_mut(key)?;
        if entry.inserted.elapsed() > self.ttl {
            guard.map.remove(key);
            guard.order.retain(|k| k != key);
            return None;
        }
        entry.hits += 1;
        Some(entry.value.clone())
    }

    pub fn put(&self, key: K, value: V) {
        let mut guard = self.inner.write().expect("cache poisoned");
        if guard.map.len() >= self.capacity && !guard.map.contains_key(&key) {
            if let Some(evict) = guard.order.first().cloned() {
                guard.map.remove(&evict);
                guard.order.remove(0);
            }
        }
        if !guard.map.contains_key(&key) {
            guard.order.push(key.clone());
        }
        guard.map.insert(
            key,
            Entry {
                value,
                inserted: Instant::now(),
                hits: 0,
            },
        );
    }

    pub fn len(&self) -> usize {
        self.inner.read().map(|g| g.map.len()).unwrap_or(0)
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn purge_expired(&self) -> usize {
        let mut guard = self.inner.write().expect("cache poisoned");
        let ttl = self.ttl;
        let before = guard.map.len();
        let now = Instant::now();
        guard
            .map
            .retain(|_, e| now.duration_since(e.inserted) <= ttl);
        guard.order.retain(|k| guard.map.contains_key(k));
        before - guard.map.len()
    }
}

/// Statistics about a cache snapshot.
#[derive(Debug, Clone)]
pub struct Stats {
    pub size: usize,
    pub total_hits: u64,
    pub avg_hits: f64,
}

impl<K, V> Cache<K, V>
where
    K: Eq + std::hash::Hash + Clone,
    V: Clone,
{
    pub fn stats(&self) -> Stats {
        let guard = self.inner.read().expect("cache poisoned");
        let size = guard.map.len();
        let total_hits: u64 = guard.map.values().map(|e| e.hits).sum();
        let avg_hits = if size > 0 {
            total_hits as f64 / size as f64
        } else {
            0.0
        };
        Stats {
            size,
            total_hits,
            avg_hits,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn put_then_get_returns_value() {
        let cache: Cache<String, u64> = Cache::new(8, Duration::from_secs(60));
        cache.put("a".into(), 1);
        assert_eq!(cache.get(&"a".into()), Some(1));
    }

    #[test]
    fn capacity_eviction_keeps_newest() {
        let cache: Cache<String, u64> = Cache::new(2, Duration::from_secs(60));
        cache.put("a".into(), 1);
        cache.put("b".into(), 2);
        cache.put("c".into(), 3);
        assert!(cache.get(&"a".into()).is_none());
        assert!(cache.get(&"c".into()).is_some());
    }
}
