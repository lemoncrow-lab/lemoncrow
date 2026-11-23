/**
 * Fixture (b): a large change for scale, 45 nodes in 4 lanes with 3 flows. A checkout splits one "charge" call into
 * authorise and capture, with a capture worker, a retry scheduler and a new payment_attempts table.
 */
import type { XRayDelta, XRayEdge, XRayEdgeKind, XRayEvidence, XRayGraph, XRayNode, XRayNodeKind } from "../xrayGraphTypes";

const HEAD = "run_head_c0ff";
const BASE = "run_base_c0ff";

type N = [id: string, label: string, kind: XRayNodeKind, lane: string, delta: XRayDelta, subtitle: string, path: string, lines: [number, number], badges?: string[]];

const NODES: N[] = [
  ["w_checkout", "CheckoutPage", "ui", "web", "modified", "app/checkout/page.tsx", "apps/web/app/checkout/page.tsx", [1, 120], ["+38 / -12"]],
  ["w_cart", "CartSummary", "ui", "web", "unchanged", "components/CartSummary.tsx", "apps/web/components/CartSummary.tsx", [1, 64]],
  ["w_payform", "PaymentForm", "ui", "web", "modified", "components/PaymentForm.tsx", "apps/web/components/PaymentForm.tsx", [1, 140], ["+51 / -30"]],
  ["w_address", "AddressForm", "ui", "web", "unchanged", "components/AddressForm.tsx", "apps/web/components/AddressForm.tsx", [1, 90]],
  ["w_confirm", "OrderConfirmation", "ui", "web", "modified", "app/checkout/done/page.tsx", "apps/web/app/checkout/done/page.tsx", [1, 58], ["+9 / -3"]],
  ["w_usecheckout", "useCheckout", "function", "web", "modified", "hooks/useCheckout.ts", "apps/web/hooks/useCheckout.ts", [1, 88], ["+22 / -14"]],
  ["w_client", "apiClient", "module", "web", "unchanged", "lib/apiClient.ts", "apps/web/lib/apiClient.ts", [1, 75]],
  ["w_price", "PriceTag", "ui", "web", "unchanged", "components/PriceTag.tsx", "apps/web/components/PriceTag.tsx", [1, 30]],
  ["w_promo", "PromoCodeInput", "ui", "web", "unchanged", "components/PromoCodeInput.tsx", "apps/web/components/PromoCodeInput.tsx", [1, 52]],
  ["w_history", "OrderHistoryPage", "ui", "web", "unchanged", "app/orders/page.tsx", "apps/web/app/orders/page.tsx", [1, 70]],
  ["w_row", "OrderRow", "ui", "web", "modified", "components/OrderRow.tsx", "apps/web/components/OrderRow.tsx", [1, 44], ["+6 / -2"]],
  ["w_retry", "RetryBanner", "ui", "web", "added", "components/RetryBanner.tsx", "apps/web/components/RetryBanner.tsx", [1, 41], ["new"]],
  ["a_checkout", "POST /api/checkout", "route", "api", "modified", "routes/checkout.ts", "services/api/src/routes/checkout.ts", [1, 96], ["+17 / -9"]],
  ["a_authorize", "POST /api/payments/authorize", "route", "api", "added", "routes/payments.ts", "services/api/src/routes/payments.ts", [12, 58], ["new"]],
  ["a_capture", "POST /api/payments/capture", "route", "api", "added", "routes/payments.ts", "services/api/src/routes/payments.ts", [60, 104], ["new"]],
  ["a_charge", "POST /api/payments/charge", "route", "api", "removed", "routes/payments.ts", "services/api/src/routes/payments.ts", [10, 71]],
  ["a_orders", "GET /api/orders", "route", "api", "unchanged", "routes/orders.ts", "services/api/src/routes/orders.ts", [1, 40]],
  ["a_order", "GET /api/orders/:id", "route", "api", "modified", "routes/orders.ts", "services/api/src/routes/orders.ts", [42, 80], ["+5 / -1"]],
  ["a_handler", "checkoutHandler", "handler", "api", "modified", "handlers/checkout.ts", "services/api/src/handlers/checkout.ts", [1, 130], ["+28 / -19"]],
  ["a_payments", "PaymentService", "class", "api", "modified", "services/PaymentService.ts", "services/api/src/services/PaymentService.ts", [1, 210], ["+64 / -41"]],
  ["a_orderSvc", "OrderService", "class", "api", "unchanged", "services/OrderService.ts", "services/api/src/services/OrderService.ts", [1, 160]],
  ["a_pricecalc", "priceCalculator", "function", "api", "unchanged", "pricing/calculator.ts", "services/api/src/pricing/calculator.ts", [1, 72]],
  ["a_promo", "promoValidator", "function", "api", "unchanged", "pricing/promo.ts", "services/api/src/pricing/promo.ts", [1, 48]],
  ["a_auth", "authMiddleware", "function", "api", "unchanged", "middleware/auth.ts", "services/api/src/middleware/auth.ts", [1, 55]],
  ["a_rate", "rateLimiter", "function", "api", "modified", "middleware/rateLimit.ts", "services/api/src/middleware/rateLimit.ts", [1, 39], ["+4 / -2"]],
  ["k_capture", "captureWorker", "job", "workers", "added", "jobs/capture.ts", "services/workers/src/jobs/capture.ts", [1, 118], ["new"]],
  ["k_charge", "chargeWorker", "job", "workers", "removed", "jobs/charge.ts", "services/workers/src/jobs/charge.ts", [1, 96]],
  ["k_receipt", "receiptMailer", "job", "workers", "modified", "jobs/receipt.ts", "services/workers/src/jobs/receipt.ts", [1, 66], ["+8 / -5"]],
  ["k_ledger", "ledgerSync", "job", "workers", "modified", "jobs/ledger.ts", "services/workers/src/jobs/ledger.ts", [1, 102], ["+12 / -7"]],
  ["k_refund", "refundWorker", "job", "workers", "unchanged", "jobs/refund.ts", "services/workers/src/jobs/refund.ts", [1, 84]],
  ["k_webhook", "POST /webhooks/stripe", "route", "workers", "modified", "webhooks/stripe.ts", "services/workers/src/webhooks/stripe.ts", [1, 140], ["+33 / -10"]],
  ["k_retry", "retryScheduler", "job", "workers", "added", "jobs/retry.ts", "services/workers/src/jobs/retry.ts", [1, 77], ["new"]],
  ["k_fraud", "fraudCheck", "function", "workers", "unchanged", "risk/fraud.ts", "services/workers/src/risk/fraud.ts", [1, 60]],
  ["k_inventory", "inventoryReserve", "job", "workers", "unchanged", "jobs/inventory.ts", "services/workers/src/jobs/inventory.ts", [1, 70]],
  ["k_analytics", "analyticsEmitter", "function", "workers", "unchanged", "events/analytics.ts", "services/workers/src/events/analytics.ts", [1, 35]],
  ["d_orders", "orders", "datastore", "data", "unchanged", "Postgres table", "db/schema/orders.sql", [1, 40]],
  ["d_payments", "payments", "datastore", "data", "modified", "Postgres table · +status", "db/schema/payments.sql", [1, 52], ["+6 / -1"]],
  ["d_attempts", "payment_attempts", "datastore", "data", "added", "Postgres table", "db/schema/payment_attempts.sql", [1, 31], ["new"]],
  ["d_cache", "Redis cache", "datastore", "data", "unchanged", "sessions · carts", "services/api/src/cache.ts", [1, 40]],
  ["d_queue", "payments queue", "queue", "data", "modified", "BullMQ · payments", "services/workers/src/queues.ts", [1, 28], ["+3 / -3"]],
  ["d_stripe", "Stripe", "external", "data", "modified", "PaymentIntents API", "", [0, 0]],
  ["d_postmark", "Postmark", "external", "data", "unchanged", "Email API", "", [0, 0]],
  ["d_ledger", "Ledger API", "external", "data", "unchanged", "finance ledger", "", [0, 0]],
  ["d_flags", "feature flags", "config", "data", "modified", "config/flags.json", "config/flags.json", [1, 22], ["+1 / -0"]],
  ["d_migration", "0042_payment_attempts", "module", "data", "added", "db/migrations", "db/migrations/0042_payment_attempts.sql", [1, 31], ["new"]],
];

type E = [from: string, to: string, kind: XRayEdgeKind, label: string, delta: XRayDelta, evidence: XRayEvidence];

const EDGES: E[] = [
  ["w_checkout", "w_cart", "render", "renders", "unchanged", "static"],
  ["w_checkout", "w_address", "render", "renders", "unchanged", "static"],
  ["w_checkout", "w_payform", "render", "renders", "modified", "static"],
  ["w_cart", "w_price", "render", "", "unchanged", "static"],
  ["w_cart", "w_promo", "render", "", "unchanged", "static"],
  ["w_payform", "w_usecheckout", "call", "submit()", "modified", "static"],
  ["w_usecheckout", "w_client", "call", "post()", "unchanged", "static"],
  ["w_payform", "w_retry", "render", "on capture failure", "added", "static"],
  ["w_history", "w_row", "render", "renders rows", "unchanged", "static"],
  ["w_confirm", "w_price", "render", "", "unchanged", "static"],
  ["w_usecheckout", "a_checkout", "http", "POST /api/checkout", "modified", "recorded"],
  ["w_usecheckout", "a_authorize", "http", "POST authorize", "added", "recorded"],
  ["w_usecheckout", "a_charge", "http", "POST charge", "removed", "recorded"],
  ["w_confirm", "a_order", "http", "GET order", "modified", "recorded"],
  ["w_history", "a_orders", "http", "GET orders", "unchanged", "static"],
  ["w_promo", "a_checkout", "http", "validate code", "unchanged", "inferred"],
  ["w_retry", "a_capture", "http", "retry capture", "added", "inferred"],
  ["a_checkout", "a_auth", "call", "", "unchanged", "static"],
  ["a_checkout", "a_rate", "call", "", "modified", "static"],
  ["a_checkout", "a_handler", "call", "handle()", "modified", "recorded"],
  ["a_handler", "a_pricecalc", "call", "total()", "unchanged", "static"],
  ["a_handler", "a_promo", "call", "apply()", "unchanged", "static"],
  ["a_handler", "a_orderSvc", "call", "create()", "unchanged", "recorded"],
  ["a_authorize", "a_payments", "call", "authorize()", "added", "recorded"],
  ["a_capture", "a_payments", "call", "capture()", "added", "static"],
  ["a_charge", "a_payments", "call", "charge()", "removed", "static"],
  ["a_order", "a_orderSvc", "call", "get()", "modified", "static"],
  ["a_orders", "a_orderSvc", "call", "list()", "unchanged", "static"],
  ["a_payments", "d_stripe", "http", "create PaymentIntent", "modified", "recorded"],
  ["a_payments", "d_payments", "data", "insert payment", "modified", "recorded"],
  ["a_payments", "d_attempts", "data", "insert attempt", "added", "recorded"],
  ["a_payments", "d_queue", "event", "enqueue capture", "added", "recorded"],
  ["a_payments", "d_flags", "dependency", "split_capture flag", "added", "static"],
  ["a_orderSvc", "d_orders", "data", "read / write", "unchanged", "static"],
  ["a_orderSvc", "d_cache", "data", "cache order", "unchanged", "static"],
  ["a_rate", "d_cache", "data", "token bucket", "modified", "static"],
  ["a_auth", "d_cache", "data", "session", "unchanged", "static"],
  ["d_queue", "k_capture", "event", "capture job", "added", "recorded"],
  ["d_queue", "k_charge", "event", "charge job", "removed", "static"],
  ["k_capture", "d_stripe", "http", "capture intent", "added", "recorded"],
  ["k_capture", "d_attempts", "data", "update attempt", "added", "recorded"],
  ["k_charge", "d_stripe", "http", "create charge", "removed", "static"],
  ["k_capture", "k_fraud", "call", "score()", "unchanged", "static"],
  ["k_capture", "k_retry", "call", "schedule on 402", "added", "static"],
  ["k_retry", "d_queue", "event", "re-enqueue", "added", "inferred"],
  ["d_stripe", "k_webhook", "http", "payment_intent.succeeded", "modified", "recorded"],
  ["k_webhook", "d_payments", "data", "mark settled", "modified", "recorded"],
  ["k_webhook", "k_ledger", "call", "sync()", "modified", "recorded"],
  ["k_webhook", "k_receipt", "event", "send receipt", "modified", "recorded"],
  ["k_ledger", "d_ledger", "http", "POST entry", "unchanged", "recorded"],
  ["k_receipt", "d_postmark", "http", "POST /email", "unchanged", "recorded"],
  ["k_refund", "d_stripe", "http", "create refund", "unchanged", "static"],
  ["k_refund", "d_payments", "data", "mark refunded", "unchanged", "static"],
  ["k_inventory", "d_orders", "data", "reserve stock", "unchanged", "static"],
  ["k_webhook", "k_analytics", "event", "payment event", "unchanged", "inferred"],
  ["d_migration", "d_attempts", "dependency", "creates", "added", "static"],
  ["a_order", "d_attempts", "data", "read attempts", "added", "static"],
  ["w_row", "w_price", "render", "", "unchanged", "static"],
  ["k_receipt", "d_orders", "data", "read order", "unchanged", "static"],
  ["a_handler", "k_inventory", "event", "reserve", "unchanged", "static"],
];

const anchorFor = (n: N): XRayNode["anchors"] =>
  n[2] === "external" ? [] : [{ path: n[6], start_line: n[7][0], end_line: n[7][1], side: n[4] === "removed" ? "base" : "head" }];

const nodes: XRayNode[] = NODES.map((n) => ({
  id: n[0],
  label: n[1],
  kind: n[2],
  lane: n[3],
  delta: n[4],
  subtitle: n[5],
  badges: n[8] ?? [],
  anchors: anchorFor(n),
  source: n[2] === "external" ? "recorded" : "static",
}));
const byId = new Map(NODES.map((n) => [n[0], n]));

const edges: XRayEdge[] = EDGES.map(([from, to, kind, label, delta, evidence], index) => {
  const source = byId.get(from)!;
  const edge: XRayEdge = {
    id: `e${String(index + 1).padStart(2, "0")}_${from}_${to}`,
    from,
    to,
    kind,
    delta,
    evidence,
    anchors: source[2] === "external" ? [] : [{ path: source[6], start_line: source[7][0], end_line: source[7][0] + 4, side: delta === "removed" ? "base" : "head" }],
  };
  if (label) edge.label = label;
  if (evidence === "recorded") edge.recorded = { ref: delta === "removed" ? BASE : HEAD, count: 1 };
  if (from === "w_usecheckout" && to === "a_authorize") edge.hero = true;
  return edge;
});
const edgeId = (from: string, to: string) => edges.find((e) => e.from === from && e.to === to)!.id;

const anchor = (id: string) => anchorFor(byId.get(id)!);

export const SCALE_CHANGE: XRayGraph = {
  version: 1,
  review_id: "rev_checkout_split",
  revision_id: "rv_checkout_split_4",
  digest: "9a8b7c6d5e4f30211f2e3d4c5b6a79880f1e2d3c4b5a69788796a5b4c3d2e1f0",
  title: "Checkout splits charge into authorise and capture",
  summary: "Payments are authorised at checkout and captured by a new worker, with retries on a declined capture and a payment_attempts table that records each try.",
  provenance: {
    producer: "index+ai",
    model: "claude-sonnet-4-5",
    generated_at: "2026-11-17T09:12:44Z",
    recorded_runs: [
      { ref: BASE, recipe: "checkout e2e (Playwright)", side: "base", at: "2026-11-17T08:58:03Z" },
      { ref: HEAD, recipe: "checkout e2e (Playwright)", side: "head", at: "2026-11-17T09:04:51Z" },
    ],
  },
  lanes: [
    { id: "web", label: "Web app", platform: "Next.js" },
    { id: "api", label: "API", platform: "Node · Express" },
    { id: "workers", label: "Workers", platform: "BullMQ" },
    { id: "data", label: "Data & services", platform: "Postgres · Redis" },
  ],
  nodes,
  edges,
  flows: [
    {
      id: "f_pay",
      title: "Pay for an order",
      summary: "Checkout authorises the card, then a worker captures it.",
      participants: [{ node: "w_payform" }, { node: "a_authorize" }, { node: "a_payments" }, { node: "d_stripe" }, { node: "d_queue" }, { node: "k_capture" }],
      steps: [
        {
          id: "p1", from: "w_payform", to: "a_authorize", label: "POST /api/payments/authorize", kind: "sync", delta: "added", evidence: "recorded", anchors: anchor("w_usecheckout"),
          payload: {
            request: { type: "AuthorizeRequest", before: null, after: { order_id: "ord_1042", amount: 5900, currency: "usd", payment_method: "pm_[masked]" }, evidence: "recorded", evidence_ref: "ev_authorize_req", redacted: true },
            response: { type: "AuthorizeResponse", before: null, after: { payment_id: "pay_88", status: "requires_capture" }, status: { after: 202 }, evidence: "recorded", evidence_ref: "ev_authorize_resp", behavior_key: "api:POST /api/payments/authorize", redacted: false },
          },
        },
        { id: "p2", from: "a_authorize", to: "a_payments", label: "authorize(order)", kind: "sync", delta: "added", evidence: "recorded", anchors: anchor("a_authorize") },
        {
          id: "p3", from: "a_payments", to: "d_stripe", label: "create PaymentIntent", kind: "sync", delta: "modified", evidence: "recorded", anchors: anchor("a_payments"),
          payload: {
            request: { type: "PaymentIntentCreate", before: { amount: 5900, currency: "usd", capture_method: "automatic" }, after: { amount: 5900, currency: "usd", capture_method: "manual" }, evidence: "recorded", evidence_ref: "ev_intent_req", redacted: false },
            response: { type: "PaymentIntent", before: { id: "pi_[masked]", status: "succeeded" }, after: { id: "pi_[masked]", status: "requires_capture" }, status: { before: 200, after: 200 }, evidence: "recorded", evidence_ref: "ev_intent_resp", redacted: true },
          },
        },
        { id: "p4", from: "d_stripe", to: "a_payments", label: "requires_capture", kind: "return", delta: "modified", evidence: "recorded", anchors: [] },
        { id: "p5", from: "a_payments", to: "d_queue", label: "enqueue capture", kind: "async", delta: "added", evidence: "recorded", anchors: anchor("a_payments") },
        { id: "p6", from: "a_payments", to: "a_authorize", label: "payment pay_88", kind: "return", delta: "added", evidence: "recorded", anchors: anchor("a_payments") },
        { id: "p7", from: "a_authorize", to: "w_payform", label: "202 requires_capture", kind: "return", delta: "added", evidence: "recorded", anchors: anchor("a_authorize") },
        { id: "p8", from: "d_queue", to: "k_capture", label: "capture job", kind: "async", delta: "added", evidence: "recorded", anchors: anchor("d_queue") },
        { id: "p9", from: "k_capture", to: "d_stripe", label: "capture intent", kind: "sync", delta: "added", evidence: "recorded", note: "Captures within 5 minutes of authorisation; a 402 hands the job to retryScheduler.", anchors: anchor("k_capture") },
        { id: "p10", from: "d_stripe", to: "k_capture", label: "succeeded", kind: "return", delta: "added", evidence: "recorded", anchors: [] },
      ],
    },
    {
      id: "f_webhook",
      title: "Stripe webhook settles a payment",
      participants: [{ node: "d_stripe" }, { node: "k_webhook" }, { node: "d_payments" }, { node: "k_ledger" }, { node: "k_receipt" }, { node: "d_postmark" }],
      steps: [
        { id: "h1", from: "d_stripe", to: "k_webhook", label: "payment_intent.succeeded", kind: "async", delta: "modified", evidence: "recorded", anchors: [] },
        { id: "h2", from: "k_webhook", to: "k_webhook", label: "verify signature", kind: "self", delta: "unchanged", evidence: "static", anchors: anchor("k_webhook") },
        { id: "h3", from: "k_webhook", to: "d_payments", label: "UPDATE status = settled", kind: "sync", delta: "modified", evidence: "recorded", anchors: anchor("k_webhook") },
        { id: "h4", from: "k_webhook", to: "k_ledger", label: "sync(payment)", kind: "sync", delta: "modified", evidence: "recorded", anchors: anchor("k_webhook") },
        { id: "h5", from: "k_ledger", to: "k_webhook", label: "entry id", kind: "return", delta: "modified", evidence: "recorded", anchors: anchor("k_ledger") },
        { id: "h6", from: "k_webhook", to: "k_receipt", label: "send receipt", kind: "async", delta: "modified", evidence: "recorded", anchors: anchor("k_webhook") },
        { id: "h7", from: "k_receipt", to: "d_postmark", label: "POST /email", kind: "sync", delta: "unchanged", evidence: "recorded", repeat: 2, anchors: anchor("k_receipt") },
      ],
    },
    {
      id: "f_retry",
      title: "Retry a declined capture",
      summary: "A model's reading of the new retry path; no run reached it.",
      participants: [{ node: "k_capture" }, { node: "k_retry" }, { node: "d_queue" }, { node: "d_attempts" }],
      steps: [
        { id: "r1", from: "k_capture", to: "d_attempts", label: "record attempt (402)", kind: "sync", delta: "added", evidence: "static", anchors: anchor("k_capture") },
        { id: "r2", from: "k_capture", to: "k_retry", label: "schedule(backoff)", kind: "sync", delta: "added", evidence: "static", anchors: anchor("k_capture") },
        { id: "r3", from: "k_retry", to: "d_queue", label: "re-enqueue with delay", kind: "async", delta: "added", evidence: "inferred", repeat: 3, note: "Up to three tries, 1, 5 and 25 minutes apart.", anchors: anchor("k_retry") },
        { id: "r4", from: "k_retry", to: "k_capture", label: "scheduled", kind: "return", delta: "added", evidence: "inferred", anchors: anchor("k_retry") },
      ],
    },
  ],
  views: [
    {
      id: "v_all",
      title: "Whole change",
      lens: "architecture",
      scope: { kind: "all" },
      children: [
        { id: "v_payment_path", title: "Payment path", lens: "architecture", scope: { kind: "nodes", nodes: ["w_payform", "w_usecheckout", "a_authorize", "a_capture", "a_charge", "a_payments", "d_stripe", "d_queue", "k_capture", "k_charge", "d_attempts"] }, children: [] },
        { id: "v_pay", title: "Pay for an order", lens: "flow", scope: { kind: "flow", flow: "f_pay" }, children: [] },
        { id: "v_webhook", title: "Webhook settles", lens: "flow", scope: { kind: "flow", flow: "f_webhook" }, children: [] },
        { id: "v_retry", title: "Retry a capture", lens: "flow", scope: { kind: "flow", flow: "f_retry" }, children: [] },
      ],
    },
  ],
  walkthrough: {
    steps: [
      { id: "t1", heading: "Checkout now only authorises", body: "useCheckout posts to the new authorize route; the old charge route is gone.", stage: { kind: "view", view: "v_all" }, focus: { kind: "selection", nodes: ["a_authorize", "a_charge"], edges: [edgeId("w_usecheckout", "a_authorize"), edgeId("w_usecheckout", "a_charge")] } },
      { id: "t2", heading: "Stripe holds the money first", body: "The PaymentIntent is created with capture_method manual, so the card is held, not charged.", stage: { kind: "flow", flow: "f_pay" }, focus: { kind: "selection", messages: ["p3", "p4"] } },
      { id: "t3", heading: "A worker captures it", body: "captureWorker takes the job from the payments queue and captures the intent.", stage: { kind: "view", view: "v_payment_path" }, focus: { kind: "selection", nodes: ["k_capture", "d_queue"], edges: [edgeId("d_queue", "k_capture"), edgeId("k_capture", "d_stripe")] } },
      { id: "t4", heading: "Declines are retried", body: "A 402 schedules up to three retries; no run reached this path, so it is drawn from the code.", stage: { kind: "flow", flow: "f_retry" }, focus: { kind: "all" } },
    ],
  },
  degraded: ["12 test files changed and are not drawn.", "Stripe, Postmark and Ledger API are external: no anchor in this repository."],
};
