/* Synthetic A/B fixture — C ring-buffer with lock-free read and mutex write.
 * NOT for production use. Generated to exercise tree-sitter outline.
 */

#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define RBUF_MAGIC 0xDEADBEEFU
#define RBUF_MAX_NAME 64

typedef struct rbuf_entry {
    uint64_t seq;
    size_t   len;
    char     data[256];
} rbuf_entry_t;

typedef struct rbuf {
    uint32_t       magic;
    size_t         capacity; /* must be power of two */
    volatile size_t head;   /* writer advances */
    volatile size_t tail;   /* reader advances */
    pthread_mutex_t write_lock;
    char            name[RBUF_MAX_NAME];
    rbuf_entry_t   *slots;
} rbuf_t;

static int is_power_of_two(size_t n) {
    return n > 0 && (n & (n - 1)) == 0;
}

rbuf_t *rbuf_create(const char *name, size_t capacity) {
    if (!is_power_of_two(capacity)) {
        errno = EINVAL;
        return NULL;
    }
    rbuf_t *rb = (rbuf_t *)calloc(1, sizeof(rbuf_t));
    if (!rb) return NULL;

    rb->slots = (rbuf_entry_t *)calloc(capacity, sizeof(rbuf_entry_t));
    if (!rb->slots) {
        free(rb);
        return NULL;
    }
    rb->capacity = capacity;
    rb->head = 0;
    rb->tail = 0;
    rb->magic = RBUF_MAGIC;
    strncpy(rb->name, name ? name : "unnamed", RBUF_MAX_NAME - 1);
    pthread_mutex_init(&rb->write_lock, NULL);
    return rb;
}

void rbuf_destroy(rbuf_t *rb) {
    if (!rb) return;
    assert(rb->magic == RBUF_MAGIC);
    pthread_mutex_destroy(&rb->write_lock);
    free(rb->slots);
    rb->magic = 0;
    free(rb);
}

size_t rbuf_size(const rbuf_t *rb) {
    assert(rb && rb->magic == RBUF_MAGIC);
    size_t h = rb->head;
    size_t t = rb->tail;
    return h - t;
}

int rbuf_full(const rbuf_t *rb) {
    return rbuf_size(rb) >= rb->capacity;
}

int rbuf_empty(const rbuf_t *rb) {
    return rb->head == rb->tail;
}

int rbuf_push(rbuf_t *rb, const void *data, size_t len) {
    assert(rb && rb->magic == RBUF_MAGIC);
    if (!data || len == 0 || len > sizeof(((rbuf_entry_t *)0)->data)) {
        return -EINVAL;
    }
    pthread_mutex_lock(&rb->write_lock);
    if (rbuf_full(rb)) {
        pthread_mutex_unlock(&rb->write_lock);
        return -ENOSPC;
    }
    size_t idx = rb->head & (rb->capacity - 1);
    rbuf_entry_t *slot = &rb->slots[idx];
    slot->seq = rb->head;
    slot->len = len;
    memcpy(slot->data, data, len);
    __atomic_fetch_add(&rb->head, 1, __ATOMIC_RELEASE);
    pthread_mutex_unlock(&rb->write_lock);
    return 0;
}

int rbuf_pop(rbuf_t *rb, void *out, size_t *out_len) {
    assert(rb && rb->magic == RBUF_MAGIC);
    if (!out || !out_len) return -EINVAL;
    if (rbuf_empty(rb)) return -ENODATA;

    size_t idx = rb->tail & (rb->capacity - 1);
    rbuf_entry_t *slot = &rb->slots[idx];
    if (slot->seq != rb->tail) return -EAGAIN;

    memcpy(out, slot->data, slot->len);
    *out_len = slot->len;
    __atomic_fetch_add(&rb->tail, 1, __ATOMIC_RELEASE);
    return 0;
}

int rbuf_peek(const rbuf_t *rb, void *out, size_t *out_len) {
    assert(rb && rb->magic == RBUF_MAGIC);
    if (!out || !out_len) return -EINVAL;
    if (rbuf_empty(rb)) return -ENODATA;

    size_t idx = rb->tail & (rb->capacity - 1);
    const rbuf_entry_t *slot = &rb->slots[idx];
    if (slot->seq != rb->tail) return -EAGAIN;

    memcpy(out, slot->data, slot->len);
    *out_len = slot->len;
    return 0;
}

void rbuf_reset(rbuf_t *rb) {
    assert(rb && rb->magic == RBUF_MAGIC);
    pthread_mutex_lock(&rb->write_lock);
    rb->head = 0;
    rb->tail = 0;
    memset(rb->slots, 0, rb->capacity * sizeof(rbuf_entry_t));
    pthread_mutex_unlock(&rb->write_lock);
}

void rbuf_stats(const rbuf_t *rb, FILE *out) {
    assert(rb && rb->magic == RBUF_MAGIC);
    fprintf(out,
            "rbuf[%s]: capacity=%zu head=%zu tail=%zu size=%zu full=%d\n",
            rb->name, rb->capacity,
            rb->head, rb->tail,
            rbuf_size(rb), rbuf_full(rb));
}

typedef int (*rbuf_foreach_fn)(const void *data, size_t len, void *user);

int rbuf_foreach(const rbuf_t *rb, rbuf_foreach_fn fn, void *user) {
    assert(rb && rb->magic == RBUF_MAGIC);
    size_t t = rb->tail;
    size_t h = rb->head;
    for (; t != h; t++) {
        size_t idx = t & (rb->capacity - 1);
        const rbuf_entry_t *slot = &rb->slots[idx];
        int rc = fn(slot->data, slot->len, user);
        if (rc != 0) return rc;
    }
    return 0;
}

static int _count_callback(const void *data, size_t len, void *user) {
    (void)data; (void)len;
    (*(size_t *)user)++;
    return 0;
}

size_t rbuf_count_entries(const rbuf_t *rb) {
    size_t count = 0;
    rbuf_foreach(rb, _count_callback, &count);
    return count;
}

typedef struct rbuf_pool {
    rbuf_t     **bufs;
    size_t       n;
    size_t       cap;
    pthread_mutex_t lock;
} rbuf_pool_t;

rbuf_pool_t *rbuf_pool_create(size_t initial_cap) {
    rbuf_pool_t *pool = (rbuf_pool_t *)calloc(1, sizeof(rbuf_pool_t));
    if (!pool) return NULL;
    pool->bufs = (rbuf_t **)calloc(initial_cap, sizeof(rbuf_t *));
    if (!pool->bufs) { free(pool); return NULL; }
    pool->cap = initial_cap;
    pthread_mutex_init(&pool->lock, NULL);
    return pool;
}

int rbuf_pool_add(rbuf_pool_t *pool, rbuf_t *rb) {
    pthread_mutex_lock(&pool->lock);
    if (pool->n == pool->cap) {
        size_t new_cap = pool->cap * 2;
        rbuf_t **tmp = (rbuf_t **)realloc(pool->bufs, new_cap * sizeof(rbuf_t *));
        if (!tmp) {
            pthread_mutex_unlock(&pool->lock);
            return -ENOMEM;
        }
        pool->bufs = tmp;
        pool->cap = new_cap;
    }
    pool->bufs[pool->n++] = rb;
    pthread_mutex_unlock(&pool->lock);
    return 0;
}

void rbuf_pool_destroy(rbuf_pool_t *pool) {
    if (!pool) return;
    pthread_mutex_lock(&pool->lock);
    for (size_t i = 0; i < pool->n; i++) rbuf_destroy(pool->bufs[i]);
    free(pool->bufs);
    pthread_mutex_unlock(&pool->lock);
    pthread_mutex_destroy(&pool->lock);
    free(pool);
}

size_t rbuf_pool_total_size(const rbuf_pool_t *pool) {
    size_t total = 0;
    for (size_t i = 0; i < pool->n; i++) total += rbuf_size(pool->bufs[i]);
    return total;
}
