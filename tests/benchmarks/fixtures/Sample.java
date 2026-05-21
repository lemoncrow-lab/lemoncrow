// Sample Java file used as an A/B fixture for the generic outline fallback.
// Patterned after real Java services — not copied from any specific project.
package com.example.payments;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantReadWriteLock;

/**
 * Coordinates payment intents across providers with idempotency guarantees.
 */
public final class PaymentService {

    private final Map<String, PaymentIntent> intents = new ConcurrentHashMap<>();
    private final ReentrantReadWriteLock auditLock = new ReentrantReadWriteLock();
    private final List<AuditEvent> audit = new java.util.ArrayList<>();
    private final ProviderRouter router;
    private final Clock clock;

    public PaymentService(ProviderRouter router, Clock clock) {
        if (router == null) throw new IllegalArgumentException("router required");
        if (clock == null) throw new IllegalArgumentException("clock required");
        this.router = router;
        this.clock = clock;
    }

    public PaymentIntent createIntent(String idempotencyKey, BigDecimal amount, String currency) {
        if (idempotencyKey == null || idempotencyKey.isBlank()) {
            throw new IllegalArgumentException("idempotencyKey required");
        }
        return intents.computeIfAbsent(idempotencyKey, k -> {
            PaymentIntent created = new PaymentIntent(
                UUID.randomUUID().toString(),
                amount,
                currency,
                IntentStatus.REQUIRES_CONFIRMATION,
                clock.now()
            );
            recordAudit("intent.created", created.id(), Map.of("amount", amount.toPlainString()));
            return created;
        });
    }

    public PaymentIntent confirm(String intentId, ConfirmationToken token) throws PaymentException {
        PaymentIntent intent = require(intentId);
        if (intent.status() != IntentStatus.REQUIRES_CONFIRMATION) {
            throw new PaymentException("intent not confirmable: " + intent.status());
        }
        ProviderResult result = router.pickProvider(intent).process(intent, token);
        IntentStatus next = switch (result.outcome()) {
            case AUTHORIZED -> IntentStatus.AUTHORIZED;
            case CAPTURED -> IntentStatus.SUCCEEDED;
            case DECLINED -> IntentStatus.FAILED;
            case REVIEW -> IntentStatus.REVIEW;
        };
        PaymentIntent updated = intent.withStatus(next);
        intents.put(intent.id(), updated);
        recordAudit("intent.confirmed", intent.id(), Map.of("outcome", result.outcome().name()));
        return updated;
    }

    public PaymentIntent refund(String intentId, BigDecimal amount) throws PaymentException {
        PaymentIntent intent = require(intentId);
        if (intent.status() != IntentStatus.SUCCEEDED) {
            throw new PaymentException("intent not refundable: " + intent.status());
        }
        router.pickProvider(intent).refund(intent, amount);
        recordAudit("intent.refunded", intent.id(), Map.of("amount", amount.toPlainString()));
        return intent.withStatus(IntentStatus.REFUNDED);
    }

    private PaymentIntent require(String id) throws PaymentException {
        PaymentIntent intent = intents.get(id);
        if (intent == null) throw new PaymentException("unknown intent: " + id);
        return intent;
    }

    private void recordAudit(String type, String intentId, Map<String, String> meta) {
        auditLock.writeLock().lock();
        try {
            audit.add(new AuditEvent(type, intentId, clock.now(), new HashMap<>(meta)));
        } finally {
            auditLock.writeLock().unlock();
        }
    }

    public List<AuditEvent> auditLog() {
        auditLock.readLock().lock();
        try {
            return List.copyOf(audit);
        } finally {
            auditLock.readLock().unlock();
        }
    }

    public enum IntentStatus {
        REQUIRES_CONFIRMATION,
        AUTHORIZED,
        SUCCEEDED,
        FAILED,
        REVIEW,
        REFUNDED,
    }

    public record PaymentIntent(String id, BigDecimal amount, String currency,
                                IntentStatus status, Instant createdAt) {
        public PaymentIntent withStatus(IntentStatus next) {
            return new PaymentIntent(id, amount, currency, next, createdAt);
        }
    }

    public record AuditEvent(String type, String intentId, Instant at, Map<String, String> meta) {}

    public interface Clock { Instant now(); }

    public interface ProviderRouter { Provider pickProvider(PaymentIntent intent); }

    public interface Provider {
        ProviderResult process(PaymentIntent intent, ConfirmationToken token) throws PaymentException;
        void refund(PaymentIntent intent, BigDecimal amount) throws PaymentException;
    }

    public record ConfirmationToken(String token, String fingerprint) {}
    public record ProviderResult(Outcome outcome, String providerId, String reference) {}
    public enum Outcome { AUTHORIZED, CAPTURED, DECLINED, REVIEW }
    public static final class PaymentException extends Exception {
        public PaymentException(String msg) { super(msg); }
    }
}
