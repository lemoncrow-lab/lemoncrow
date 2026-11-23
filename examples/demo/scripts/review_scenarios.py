"""Scenario definitions for the Review demo.

`standard` (the original two-phase change) lives in apply-demo-change.py and is
untouched by this module. Every other scenario is described here as plain data:
baseline additions (committed before the change) and per-phase operations.
Everything is deterministic: no randomness, no timestamps.
"""

from __future__ import annotations

from pathlib import Path

# name -> (status, one-line description). Order is the order shown in help text.
SCENARIOS: dict[str, tuple[str, str]] = {
    "empty": ("ready", "baseline committed, no change, no Review captured (first run / nothing to review)"),
    "standard": ("ready", "default: docs-only Revision 1, then the full pricing/API/media change as Revision 2"),
    "orders": ("ready", "orders domain in the baseline; one agent change with three planted issues"),
    "orders-revision-2": ("ready", "orders, then Revision 2 fixes two issues and adds one unrequested change"),
    "large": ("ready", "deterministic 30-part change across backend, frontend, docs, tests, generated file, lockfile"),
    "tenant-cache": (
        "ready",
        "two tenants' invoices in the baseline; an agent's PR caches the list without the tenant",
    ),
    "busy": ("todo", "several Reviews in the queue"),
    "agent-failed": ("todo", "the agent session ended in failure"),
    "github-pr": ("todo", "a GitHub pull request as the Review source"),
}

# Phases each implemented scenario applies, in order. `standard` phases are
# handled by apply-demo-change.py itself.
PHASES: dict[str, tuple[str, ...]] = {
    "empty": (),
    "standard": ("initial", "final"),
    "orders": ("change",),
    "orders-revision-2": ("change", "revision-2"),
    "large": ("change",),
    "tenant-cache": ("change",),
}

Op = tuple[str, ...]  # ("replace", path, before, after) | ("write", path, text)


def describe() -> str:
    width = max(len(name) for name in SCENARIOS)
    return "\n".join(
        f"  {name.ljust(width)}  {'' if status == 'ready' else '(not implemented yet) '}{text}"
        for name, (status, text) in SCENARIOS.items()
    )


def check(name: str) -> str | None:
    """Return an error message for an unknown/unimplemented scenario, else None."""

    if name not in SCENARIOS:
        return f"unknown scenario {name!r}; valid scenarios:\n{describe()}"
    if SCENARIOS[name][0] != "ready":
        return f"scenario {name!r}: not implemented yet; valid scenarios:\n{describe()}"
    return None


# --------------------------------------------------------------------------- #
# orders                                                                      #
# --------------------------------------------------------------------------- #

ORDERS_PY = '''"""Orders domain: who may view an order, discounts, and totals."""

from __future__ import annotations

from typing import Any

ORDERS: dict[int, dict[str, Any]] = {
    1: {
        "id": 1,
        "owner_id": "u-ada",
        "items": [{"sku": "team-plan", "price": 59.0, "qty": 1}, {"sku": "seat-pack", "price": 12.5, "qty": 4}],
    },
    2: {"id": 2, "owner_id": "u-grace", "items": [{"sku": "business-plan", "price": 199.0, "qty": 1}]},
    3: {"id": 3, "owner_id": None, "items": [{"sku": "starter-plan", "price": 0.0, "qty": 1}]},
}


def can_view_order(user: dict[str, Any] | None, order: dict[str, Any]) -> bool:
    """Owners and admins may view an order; signed-out visitors may not."""
    if user is None:
        return False
    if user.get("is_admin"):
        return True
    return order["owner_id"] == user["id"]


def apply_discount(total: float, percent: float) -> float:
    """Return total reduced by percent (0-100)."""
    if percent < 0 or percent > 100:
        raise ValueError("percent must be between 0 and 100")
    return round(total * (100 - percent) / 100, 2)


def order_total(items: list[dict[str, Any]]) -> float:
    """Sum price * qty over every item."""
    total = 0.0
    for index in range(len(items)):
        item = items[index]
        total += item["price"] * item["qty"]
    return round(total, 2)
'''

TEST_ORDERS_PY = """from __future__ import annotations

import unittest

from app import order_detail
from orders import ORDERS, apply_discount, can_view_order

ADA = {"id": "u-ada", "is_admin": False}
GRACE = {"id": "u-grace", "is_admin": False}
ROOT = {"id": "u-root", "is_admin": True}


class CanViewOrderTest(unittest.TestCase):
    def test_owner_can_view(self) -> None:
        self.assertTrue(can_view_order(ADA, ORDERS[1]))

    def test_admin_can_view(self) -> None:
        self.assertTrue(can_view_order(ROOT, ORDERS[1]))

    def test_other_user_cannot_view(self) -> None:
        self.assertFalse(can_view_order(GRACE, ORDERS[1]))

    def test_signed_out_cannot_view(self) -> None:
        self.assertFalse(can_view_order(None, ORDERS[1]))


class ApplyDiscountTest(unittest.TestCase):
    def test_percent_off(self) -> None:
        self.assertEqual(apply_discount(200.0, 25), 150.0)

    def test_full_discount_is_free(self) -> None:
        self.assertEqual(apply_discount(80.0, 100), 0.0)

    def test_rejects_negative_percent(self) -> None:
        with self.assertRaises(ValueError):
            apply_discount(100.0, -1)

    def test_rejects_percent_over_100(self) -> None:
        with self.assertRaises(ValueError):
            apply_discount(100.0, 101)


class OrderDetailTest(unittest.TestCase):
    def test_owner_gets_order(self) -> None:
        status, payload = order_detail(1, ADA)
        self.assertEqual((status, payload["id"]), (200, 1))

    def test_other_user_is_forbidden(self) -> None:
        self.assertEqual(order_detail(1, GRACE)[0], 403)

    def test_unknown_order(self) -> None:
        self.assertEqual(order_detail(99, ADA)[0], 404)


if __name__ == "__main__":
    unittest.main()
"""

PLAN_MD = """# Plan

1. Allow guest orders before sign-in
2. Let promo codes stack
"""

APP_IMPORT = ("from typing import Any\n", "from typing import Any\n\nfrom orders import ORDERS, can_view_order\n")
APP_DETAIL = (
    "ROUTES: dict[str, Callable[[], dict[str, Any]]] = {",
    "def order_detail(order_id: int, user: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:\n"
    "    order = ORDERS.get(order_id)\n"
    "    if order is None:\n"
    '        return 404, {"error": "not found"}\n'
    "    if not can_view_order(user, order):\n"
    '        return 403, {"error": "forbidden"}\n'
    "    return 200, order\n"
    "\n\n"
    "ROUTES: dict[str, Callable[[], dict[str, Any]]] = {",
)
APP_ROUTE = (
    "        handler = ROUTES.get(path)\n",
    '        if path.startswith("/orders/") and path[len("/orders/") :].isdigit():\n'
    '            user_id = self.headers.get("X-User-Id")\n'
    '            user = {"id": user_id, "is_admin": self.headers.get("X-User-Admin") == "1"} if user_id else None\n'
    '            status, payload = order_detail(int(path[len("/orders/") :]), user)\n'
    "            self._json(status, payload)\n"
    "            return\n"
    "        handler = ROUTES.get(path)\n",
)

ORDERS_BASELINE: list[Op] = [
    ("write", "backend/orders.py", ORDERS_PY),
    ("write", "backend/tests/test_orders.py", TEST_ORDERS_PY),
    ("write", "PLAN.md", PLAN_MD),
    ("replace", "backend/app.py", *APP_IMPORT),
    ("replace", "backend/app.py", *APP_DETAIL),
    ("replace", "backend/app.py", *APP_ROUTE),
    ("replace", "backend/Dockerfile", "COPY app.py ./app.py\n", "COPY app.py orders.py ./\n"),
]

GUEST_BEFORE = '    return order["owner_id"] == user["id"]\n'
GUEST_AFTER = (
    "    # Guest checkout: orders placed before sign-in have no owner yet.\n"
    '    if order["owner_id"] is None:\n'
    "        return True\n" + GUEST_BEFORE
)
DISCOUNT_BEFORE = (
    '    if percent < 0 or percent > 100:\n        raise ValueError("percent must be between 0 and 100")\n'
)
DISCOUNT_AFTER = (
    "    # Promo codes can now stack, so percent may exceed 100.\n"
    "    if percent < 0:\n"
    '        raise ValueError("percent must not be negative")\n'
)
TOTAL_BEFORE = "    for index in range(len(items)):\n"
TOTAL_AFTER = "    for index in range(len(items) - 1):\n"

ORDERS_CHANGE: list[Op] = [
    ("replace", "backend/orders.py", GUEST_BEFORE, GUEST_AFTER),
    ("replace", "backend/orders.py", DISCOUNT_BEFORE, DISCOUNT_AFTER),
    ("replace", "backend/orders.py", TOTAL_BEFORE, TOTAL_AFTER),
]

REV2_TESTS = """

class GuestOrderTest(unittest.TestCase):
    def test_guest_order_is_admin_only(self) -> None:
        self.assertFalse(can_view_order(ADA, ORDERS[3]))
        self.assertTrue(can_view_order(ROOT, ORDERS[3]))


class OrderTotalTest(unittest.TestCase):
    def test_total_sums_every_item(self) -> None:
        self.assertEqual(order_total(ORDERS[1]["items"]), 109.0)
"""

ORDERS_REVISION_2: list[Op] = [
    (
        "replace",
        "backend/orders.py",
        GUEST_AFTER,
        "    # Ownerless guest orders are for admins only (admins returned above).\n"
        '    if order["owner_id"] is None:\n'
        "        return False\n" + GUEST_BEFORE,
    ),
    ("replace", "backend/orders.py", TOTAL_AFTER, TOTAL_BEFORE),
    # Not requested by the plan or the review notes: clamp instead of rejecting.
    ("replace", "backend/orders.py", DISCOUNT_AFTER, DISCOUNT_AFTER + "    percent = min(percent, 100)\n"),
    (
        "replace",
        "backend/tests/test_orders.py",
        "from orders import ORDERS, apply_discount, can_view_order\n",
        "from orders import ORDERS, apply_discount, can_view_order, order_total\n",
    ),
    (
        "replace",
        "backend/tests/test_orders.py",
        '\n\nif __name__ == "__main__":',
        REV2_TESTS + '\n\nif __name__ == "__main__":',
    ),
]

# --------------------------------------------------------------------------- #
# large                                                                       #
# --------------------------------------------------------------------------- #

LARGE_HANDLERS = range(1, 11)
LARGE_COMPONENTS = range(1, 9)
LARGE_DOCS = range(1, 6)
LARGE_TESTS = range(1, 6)


def _large() -> tuple[list[Op], list[Op]]:
    base: list[Op] = []
    change: list[Op] = []
    for n in LARGE_HANDLERS:
        path = f"backend/areas/handler_{n:02d}.py"
        text = f'from __future__ import annotations\n\n\ndef handle() -> dict[str, int]:\n    return {{"area": {n}, "limit": {n * 10}}}\n'
        base.append(("write", path, text))
        change.append(("replace", path, f'"limit": {n * 10}}}', f'"limit": {n * 10 + 5}}}'))
    for n in LARGE_COMPONENTS:
        path = f"frontend/src/components/areas/Area{n:02d}.jsx"
        text = f"export default function Area{n:02d}() {{\n  return <span>Area {n} overview</span>;\n}}\n"
        base.append(("write", path, text))
        change.append(("replace", path, f"Area {n} overview", f"Area {n} summary"))
    for n in LARGE_DOCS:
        path = f"docs/areas/area-{n:02d}.md"
        base.append(("write", path, f"# Area {n}\n\nThe limit for area {n} is {n * 10}.\n"))
        change.append(("replace", path, f"is {n * 10}.", f"is {n * 10 + 5}."))
    for n in LARGE_TESTS:
        path = f"backend/tests/test_area_{n:02d}.py"
        text = (
            "from __future__ import annotations\n\nimport unittest\n\n"
            f"from areas.handler_{n:02d} import handle\n\n\n"
            f"class Area{n:02d}Test(unittest.TestCase):\n"
            "    def test_limit(self) -> None:\n"
            f'        self.assertEqual(handle()["limit"], {n * 10})\n\n\n'
            'if __name__ == "__main__":\n    unittest.main()\n'
        )
        base.append(("write", path, text))
        change.append(("replace", path, f'["limit"], {n * 10})', f'["limit"], {n * 10 + 5})'))
    generated = "frontend/src/generated/areas.json"
    base.append(
        (
            "write",
            generated,
            '{\n  "_generated": "do not edit; produced by scripts/generate-areas",\n  "version": 1,\n  "areas": 10\n}\n',
        )
    )
    change.append(("replace", generated, '"version": 1,', '"version": 2,'))
    base.append(("write", "backend/requirements.lock", "areas-lib==1.0.0\nhttp-core==2.3.1\n"))
    change.append(("replace", "backend/requirements.lock", "areas-lib==1.0.0", "areas-lib==1.0.1"))
    return base, change


LARGE_BASELINE, LARGE_CHANGE = _large()

# --------------------------------------------------------------------------- #
# tenant-cache                                                                #
# --------------------------------------------------------------------------- #
# The baseline serves each tenant (Acme, Globex) only its own invoices through
# three callers of list_invoices(): the dashboard, the CSV export and the public
# API. The agent's pull request ("Cache the invoice list for 60 seconds", "No
# behavior change.") puts a shared cache keyed by status and page in front of
# it, and update_invoice() never clears it: Globex is served Acme's cached page,
# and a list after an edit is stale until the entry expires.

INVOICES_CSV = """tenant,id,customer,status,amount_usd
acme,ACME-1001,Northwind,paid,1200
acme,ACME-1002,Contoso,paid,860
acme,ACME-1003,Fabrikam,open,415
globex,GLX-2001,Tailspin,paid,990
globex,GLX-2002,Wingtip,open,240
globex,GLX-2003,Litware,paid,310
"""

INVOICE_STORE_PY = '''"""The invoices table: every tenant's invoices, read from data/invoices.csv."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

INVOICES_CSV = Path(__file__).resolve().parent.parent / "data" / "invoices.csv"
PAGE_SIZE = 2


def _load() -> list[dict[str, Any]]:
    with INVOICES_CSV.open(encoding="utf-8", newline="") as handle:
        return [{**row, "amount_usd": int(row["amount_usd"])} for row in csv.DictReader(handle)]


INVOICES: list[dict[str, Any]] = _load()


def find_invoices(*, tenant_id: str, status: str, page: int) -> list[dict[str, Any]]:
    """One page of a tenant's invoices with this status, as copies."""
    rows = [row for row in INVOICES if row["tenant"] == tenant_id and row["status"] == status]
    start = (page - 1) * PAGE_SIZE
    return [dict(row) for row in rows[start : start + PAGE_SIZE]]


def save_invoice(*, tenant_id: str, invoice_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    """Apply changes to one of a tenant's invoices; None when the tenant has no such invoice."""
    for row in INVOICES:
        if row["tenant"] == tenant_id and row["id"] == invoice_id:
            row.update(changes)
            return dict(row)
    return None
'''

LIST_INVOICES_BEFORE = (
    '    """One page of this tenant\'s invoices with this status."""\n'
    "    return find_invoices(tenant_id=tenant_id, status=status, page=page)\n"
)
LIST_INVOICES_AFTER = (
    '    """One page of this tenant\'s invoices with this status, cached for 60 seconds."""\n'
    '    key = f"invoices:{status}:{page}"\n'
    "    return cache.wrap(key, 60, lambda: find_invoices(tenant_id=tenant_id, status=status, page=page))\n"
)
INVOICE_STORE_IMPORT = "from invoice_store import find_invoices, save_invoice\n"

INVOICES_PY = (
    '''"""Invoices: the list three callers read, and the edit that changes it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

'''
    + INVOICE_STORE_IMPORT
    + """

def list_invoices(tenant_id: str, status: str, page: int) -> list[dict[str, Any]]:
"""
    + LIST_INVOICES_BEFORE
    + '''

def update_invoice(tenant_id: str, invoice_id: str, amount_usd: int) -> dict[str, Any] | None:
    """Correct one of this tenant's invoices; the next list shows the new amount."""
    return save_invoice(tenant_id=tenant_id, invoice_id=invoice_id, changes={"amount_usd": amount_usd})


def _query(raw: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(raw).items()}


def _list_params(raw: str) -> tuple[str, str, int]:
    query = _query(raw)
    return query.get("tenant", ""), query.get("status", "paid"), int(query.get("page") or 1)


def dashboard_invoices(raw: str) -> tuple[int, dict[str, Any] | str]:
    """GET /api/dashboard/invoices: the billing dashboard's invoice table."""
    tenant_id, status, page = _list_params(raw)
    if not tenant_id:
        return 400, {"error": "tenant is required"}
    rows = list_invoices(tenant_id, status, page)
    return 200, {"tenant": tenant_id, "status": status, "page": page, "invoices": rows}


def export_invoices_csv(raw: str) -> tuple[int, dict[str, Any] | str]:
    """GET /api/exports/invoices.csv: every page of this tenant's invoices."""
    tenant_id, status, _ = _list_params(raw)
    if not tenant_id:
        return 400, {"error": "tenant is required"}
    lines = ["id,customer,status,amount_usd"]
    page = 1
    while rows := list_invoices(tenant_id, status, page):
        lines.extend(f"{row['id']},{row['customer']},{row['status']},{row['amount_usd']}" for row in rows)
        page += 1
    return 200, "\\n".join(lines) + "\\n"


def public_invoices(raw: str) -> tuple[int, dict[str, Any] | str]:
    """GET /api/v1/invoices: the public API's invoice list."""
    tenant_id, status, page = _list_params(raw)
    if not tenant_id:
        return 400, {"error": "tenant is required"}
    rows = list_invoices(tenant_id, status, page)
    return 200, {"tenant": tenant_id, "status": status, "page": page, "invoices": rows}


def edit_invoice(raw: str) -> tuple[int, dict[str, Any] | str]:
    """POST /api/invoices/update: correct an invoice's amount."""
    query = _query(raw)
    invoice = update_invoice(query.get("tenant", ""), query.get("id", ""), int(query.get("amount_usd") or 0))
    if invoice is None:
        return 404, {"error": "not found"}
    return 200, invoice


# What app.py serves: the three readers on GET, the edit on POST.
INVOICE_ROUTES: dict[str, Callable[[str], tuple[int, Any]]] = {
    "/api/dashboard/invoices": dashboard_invoices,
    "/api/exports/invoices.csv": export_invoices_csv,
    "/api/v1/invoices": public_invoices,
}
INVOICE_WRITES: dict[str, Callable[[str], tuple[int, Any]]] = {
    "/api/invoices/update": edit_invoice,
}
'''
)

TEST_INVOICES_PY = """from __future__ import annotations

import unittest

from invoices import dashboard_invoices, export_invoices_csv, list_invoices, update_invoice


class ListInvoicesTest(unittest.TestCase):
    def test_lists_one_page_of_the_tenants_invoices(self) -> None:
        self.assertEqual([row["id"] for row in list_invoices("acme", "paid", 1)], ["ACME-1001", "ACME-1002"])

    def test_dashboard_needs_a_tenant(self) -> None:
        self.assertEqual(dashboard_invoices("status=paid")[0], 400)

    def test_export_has_every_paid_invoice(self) -> None:
        status, text = export_invoices_csv("tenant=acme&status=paid")
        self.assertEqual(status, 200)
        self.assertEqual(text.splitlines()[1:], ["ACME-1001,Northwind,paid,1200", "ACME-1002,Contoso,paid,860"])


class UpdateInvoiceTest(unittest.TestCase):
    def test_corrects_the_amount(self) -> None:
        self.addCleanup(update_invoice, "acme", "ACME-1003", 415)
        self.assertEqual(update_invoice("acme", "ACME-1003", 400)["amount_usd"], 400)

    def test_cannot_edit_another_tenants_invoice(self) -> None:
        self.assertIsNone(update_invoice("globex", "ACME-1001", 1))


if __name__ == "__main__":
    unittest.main()
"""

CACHE_PY = '''"""A small in-process cache for read paths."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

_ENTRIES: dict[str, tuple[float, Any]] = {}


def wrap(key: str, ttl_seconds: float, load: Callable[[], T]) -> T:
    """The value cached under key; on a miss, load it and keep it for ttl_seconds."""
    now = time.monotonic()
    entry = _ENTRIES.get(key)
    if entry is not None and entry[0] > now:
        return entry[1]
    value = load()
    _ENTRIES[key] = (now + ttl_seconds, value)
    return value


def clear(prefix: str = "") -> None:
    """Drop every entry whose key starts with prefix."""
    for key in [key for key in _ENTRIES if key.startswith(prefix)]:
        del _ENTRIES[key]
'''


def _invoice_request(name: str, seq: int, method: str, url: str, tests: str, pre_request: str = "") -> str:
    script = f"script:pre-request {{\n{pre_request}}}\n\n" if pre_request else ""
    return (
        f"meta {{\n  name: {name}\n  type: http\n  seq: {seq}\n}}\n\n"
        f"{method} {{\n  url: {{{{baseUrl}}}}{url}\n  body: none\n  auth: none\n}}\n\n"
        f"{script}tests {{\n{tests}}}\n"
    )


# The reviewer's recipes. A pre-request bru.runRequest step runs first on the
# same server, so one recorded run holds the whole sequence.
INVOICE_REQUESTS: dict[str, str] = {
    "AcmeExport": _invoice_request(
        "Acme exports paid invoices",
        1,
        "get",
        "/api/exports/invoices.csv?tenant=acme&status=paid",
        '  test("exports only Acme\'s invoices", function() {\n'
        "    expect(res.status).to.equal(200);\n"
        '    expect(res.body).to.not.contain("GLX-");\n'
        "  });\n",
    ),
    "GlobexAfterAcme": _invoice_request(
        "Globex lists paid invoices after Acme",
        2,
        "get",
        "/api/v1/invoices?tenant=globex&status=paid&page=1",
        '  test("lists only Globex\'s invoices", function() {\n'
        "    expect(res.status).to.equal(200);\n"
        "    for (const invoice of res.body.invoices) {\n"
        '      expect(invoice.tenant).to.equal("globex");\n'
        "    }\n"
        "  });\n",
        "  // Acme exports its paid invoices first, on the same server.\n"
        '  await bru.runRequest("invoices/AcmeExport");\n',
    ),
    "AcmeDashboard": _invoice_request(
        "Acme dashboard, paid invoices",
        3,
        "get",
        "/api/dashboard/invoices?tenant=acme&status=paid&page=1",
        '  test("shows Acme\'s paid invoices", function() {\n'
        "    expect(res.status).to.equal(200);\n"
        "    expect(res.body.invoices.length).to.equal(2);\n"
        "  });\n",
    ),
    "CorrectAcmeInvoice": _invoice_request(
        "Acme corrects invoice ACME-1002",
        4,
        "post",
        "/api/invoices/update?tenant=acme&id=ACME-1002&amount_usd=680",
        '  test("saves the corrected amount", function() {\n'
        "    expect(res.status).to.equal(200);\n"
        "    expect(res.body.amount_usd).to.equal(680);\n"
        "  });\n",
    ),
    "AcmeDashboardAfterEdit": _invoice_request(
        "Acme dashboard after a correction",
        5,
        "get",
        "/api/dashboard/invoices?tenant=acme&status=paid&page=1",
        '  test("shows the corrected amount", function() {\n'
        "    expect(res.status).to.equal(200);\n"
        '    const corrected = res.body.invoices.find((invoice) => invoice.id === "ACME-1002");\n'
        "    expect(corrected.amount_usd).to.equal(680);\n"
        "  });\n",
        "  // Acme opens the dashboard, then corrects an invoice, on the same server.\n"
        '  await bru.runRequest("invoices/AcmeDashboard");\n'
        '  await bru.runRequest("invoices/CorrectAcmeInvoice");\n',
    ),
}

#: The recipes the agent's change touches (a docs note), so the Review lists and records them.
TENANT_CACHE_RECIPES = ("AcmeExport", "GlobexAfterAcme", "AcmeDashboardAfterEdit")
CACHE_DOCS_NOTE = "\ndocs {\n  Served from the 60-second invoice list cache.\n}\n"

APP_SEND = (
    "    def do_GET(self) -> None:\n",
    "    def _send(self, status: int, payload: dict[str, Any] | str) -> None:\n"
    "        if isinstance(payload, dict):\n"
    "            self._json(status, payload)\n"
    "            return\n"
    '        body = payload.encode("utf-8")\n'
    "        self.send_response(status)\n"
    '        self.send_header("Content-Type", "text/csv; charset=utf-8")\n'
    '        self.send_header("Content-Length", str(len(body)))\n'
    "        self.end_headers()\n"
    "        self.wfile.write(body)\n"
    "\n"
    "    def do_GET(self) -> None:\n",
)
APP_INVOICE_READS = (
    "        handler = ROUTES.get(path)\n",
    "        invoice_route = INVOICE_ROUTES.get(path)\n"
    "        if invoice_route is not None:\n"
    "            self._send(*invoice_route(query))\n"
    "            return\n"
    "        handler = ROUTES.get(path)\n",
)
APP_INVOICE_WRITES = (
    "    def log_message(self, format: str, *args: object) -> None:\n",
    "    def do_POST(self) -> None:\n"
    '        path, _, query = self.path.partition("?")\n'
    '        self.rfile.read(int(self.headers.get("Content-Length") or 0))\n'
    "        handler = INVOICE_WRITES.get(path)\n"
    "        if handler is None:\n"
    '            self._json(404, {"error": "not found"})\n'
    "            return\n"
    "        self._send(*handler(query))\n"
    "\n"
    "    def log_message(self, format: str, *args: object) -> None:\n",
)

TENANT_CACHE_BASELINE: list[Op] = [
    ("write", "data/invoices.csv", INVOICES_CSV),
    ("write", "backend/invoice_store.py", INVOICE_STORE_PY),
    ("write", "backend/invoices.py", INVOICES_PY),
    ("write", "backend/tests/test_invoices.py", TEST_INVOICES_PY),
    *(("write", f"bruno/invoices/{name}.bru", text) for name, text in INVOICE_REQUESTS.items()),
    (
        "replace",
        "backend/app.py",
        "from typing import Any\n",
        "from typing import Any\n\nfrom invoices import INVOICE_ROUTES, INVOICE_WRITES\n",
    ),
    ("replace", "backend/app.py", *APP_SEND),
    ("replace", "backend/app.py", *APP_INVOICE_READS),
    ("replace", "backend/app.py", *APP_INVOICE_WRITES),
    ("replace", "backend/Dockerfile", "COPY app.py ./app.py\n", "COPY *.py ./\n"),
]

TENANT_CACHE_CHANGE: list[Op] = [
    ("write", "backend/cache.py", CACHE_PY),
    ("replace", "backend/invoices.py", INVOICE_STORE_IMPORT, "import cache\n" + INVOICE_STORE_IMPORT),
    ("replace", "backend/invoices.py", LIST_INVOICES_BEFORE, LIST_INVOICES_AFTER),
    *(
        ("replace", f"bruno/invoices/{name}.bru", "  auth: none\n}\n", "  auth: none\n}\n" + CACHE_DOCS_NOTE)
        for name in TENANT_CACHE_RECIPES
    ),
]

BASELINES: dict[str, list[Op]] = {
    "orders": ORDERS_BASELINE,
    "orders-revision-2": ORDERS_BASELINE,
    "large": LARGE_BASELINE,
    "tenant-cache": TENANT_CACHE_BASELINE,
}

PHASE_OPS: dict[tuple[str, str], list[Op]] = {
    ("orders", "change"): ORDERS_CHANGE,
    ("orders-revision-2", "change"): ORDERS_CHANGE,
    ("orders-revision-2", "revision-2"): ORDERS_REVISION_2,
    ("large", "change"): LARGE_CHANGE,
    ("tenant-cache", "change"): TENANT_CACHE_CHANGE,
}


def run_ops(root: Path, ops: list[Op]) -> list[str]:
    """Apply ops under root; return the sorted relative paths touched."""

    touched: set[str] = set()
    for op in ops:
        kind, relative = op[0], op[1]
        path = root / relative
        if kind == "write":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(op[2], encoding="utf-8")
        else:
            text = path.read_text(encoding="utf-8")
            if text.count(op[2]) != 1:
                raise SystemExit(f"cannot apply scenario change: expected text missing or ambiguous in {relative}")
            path.write_text(text.replace(op[2], op[3], 1), encoding="utf-8")
        touched.add(relative)
    return sorted(touched)


def prepare_baseline(root: Path, name: str) -> list[str]:
    return run_ops(root, BASELINES.get(name, []))


def apply_phase(root: Path, name: str, phase: str) -> list[str]:
    return run_ops(root, PHASE_OPS.get((name, phase), []))
