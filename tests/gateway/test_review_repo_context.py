"""The repository context pack (G4): what it finds, what it never reads, and that it stays inside its budget.

These tests build a small repository on disk and ask :class:`RepoContext` for the pack of one changed target.
No model and no network are involved; the end-to-end flow with a fake model is in
``test_review_pr_bot_repo_context.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import repo_context as rc
from lemoncrow.pro.capabilities.review.rules import ContentAccessRule
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetSpan

PRICING = (
    'PRICES = {"basic": 10, "pro": 25}\n'
    "REGION_FACTORS = {'eu': 1.0, 'us': 1.2}\n"
    "\n"
    "\n"
    "def quote(plan, seats, region):\n"
    "    base = PRICES[plan] * seats\n"
    "    return base * REGION_FACTORS[region]\n"
)
INVOICE = (
    "from billing.pricing import quote\n"
    "\n"
    "\n"
    "def build_invoice(customer):\n"
    "    total = quote(customer.plan, customer.seats)\n"
    '    return {"customer": customer.id, "total": total}\n'
)
REPORTS = (
    "from billing.pricing import quote\n"
    "\n"
    "\n"
    "def monthly_revenue(customers):\n"
    "    return sum(quote(c.plan, c.seats) for c in customers)\n"
)
TEST_PRICING = (
    'from billing.pricing import quote\n\n\ndef test_quote_scales_with_seats():\n    assert quote("basic", 3) == 30\n'
)
FILES = {
    "src/api/billing/pricing.py": PRICING,
    "src/api/billing/invoice.py": INVOICE,
    "src/api/billing/reports.py": REPORTS,
    "tests/test_pricing.py": TEST_PRICING,
}


def write(root: Path, files: Mapping[str, str]) -> Path:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def target(path: str, symbol: str, start: int, end: int, *, target_id: str = "") -> ReviewTarget:
    return ReviewTarget(
        target_id=target_id or f"t:{path}:{symbol}",
        unit_key=f"unit:{path}:{symbol}",
        kind="symbol",
        path=path,
        label=symbol,
        symbol=symbol,
        start_line=start,
        end_line=end,
        hunk_ordinals=(1,),
        spans=(TargetSpan("new", start, end, 1),),
        state="unreviewed",
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=1,
        attention_level="high",
        reasons=(),
        additions=end - start + 1,
        deletions=0,
        fingerprint_method="test",
    )


QUOTE = target("src/api/billing/pricing.py", "quote", 5, 7)


def context(
    root: Path,
    *,
    targets: Sequence[ReviewTarget] = (QUOTE,),
    effort: str = "auto",
    rules: Sequence[ContentAccessRule] = (),
    trust: Any = "external",
    options: rc.RepoContextOptions | None = None,
    changed: Sequence[str] = ("src/api/billing/pricing.py",),
) -> rc.RepoContext:
    return rc.RepoContext(
        root=lambda: root,
        targets=targets,
        changed_paths=changed,
        effort=effort,
        content_access=rules,
        route_trust=trust,
        options=options or rc.RepoContextOptions(),
    )


def deny(*paths: str) -> ContentAccessRule:
    return ContentAccessRule(
        mode="ai_denied",
        path_scope=tuple(paths),
        origin="trusted_base",
        trust_class="trusted_base_repository",
        source_identity="test",
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return write(tmp_path / "repo", FILES)


def items(block: rc.RepoContextBlock | None) -> list[rc.ContextItem]:
    assert block is not None
    return list(block.items)


# --------------------------------------------------------------------------- #
# what it finds
# --------------------------------------------------------------------------- #


def test_the_pack_finds_callers_in_other_files_tests_and_referenced_definitions(repo: Path) -> None:
    block = context(repo).block_for([QUOTE.target_id])
    found = items(block)
    callers = {item.path: item for item in found if item.kind == "caller"}

    # The untouched callers are the cross-file bug candidates: both are there, with the call visible.
    assert set(callers) == {"src/api/billing/invoice.py", "src/api/billing/reports.py"}
    invoice = callers["src/api/billing/invoice.py"]
    assert "total = quote(customer.plan, customer.seats)" in invoice.text
    assert (invoice.symbol, invoice.why, invoice.in_change) == ("build_invoice", "calls quote", False)
    assert any(item.kind == "test" and item.path == "tests/test_pricing.py" for item in found)
    # PRICES and REGION_FACTORS are defined within the lines the model already sees around the target: not repeated.
    assert not [item for item in found if item.kind == "definition"]


def test_a_constant_defined_far_from_the_changed_lines_is_included(tmp_path: Path) -> None:
    padding = "".join(f"def filler_{n}():\n    return {n}\n\n\n" for n in range(12))
    root = write(
        tmp_path / "repo",
        {
            "src/api/rates.py": 'TAX_RATES = {"eu": 0.2, "us": 0.07}\n\n\n'
            + padding
            + "def gross(net, region):\n    return net * (1 + TAX_RATES[region])\n"
        },
    )
    lines = (root / "src/api/rates.py").read_text().count("\n")
    change = target("src/api/rates.py", "gross", lines - 1, lines)
    found = items(context(root, targets=[change], changed=["src/api/rates.py"]).block_for([change.target_id]))
    (constant,) = [item for item in found if item.kind == "definition"]
    assert (constant.symbol, constant.start_line) == ("TAX_RATES", 1) and "eu" in constant.text


def test_a_definition_of_a_helper_in_another_file_is_included(tmp_path: Path) -> None:
    root = write(
        tmp_path / "repo",
        {
            "src/api/orders.py": "from util.money import round_cents\n\n\ndef total(order):\n    return round_cents(order.amount * 1.2)\n",
            "src/util/money.py": "def round_cents(value):\n    return int(value * 100) / 100\n",
        },
    )
    order = target("src/api/orders.py", "total", 4, 5)
    found = items(context(root, targets=[order], changed=["src/api/orders.py"]).block_for([order.target_id]))
    (helper,) = [item for item in found if item.kind == "definition"]
    assert (helper.path, helper.symbol) == ("src/util/money.py", "round_cents")
    assert "def round_cents(value):" in helper.text


def test_config_that_mentions_an_environment_variable_the_change_reads_is_included(tmp_path: Path) -> None:
    root = write(
        tmp_path / "repo",
        {
            "src/api/client.py": 'import os\n\n\ndef base_url():\n    return os.environ.get("BILLING_API_URL", "")\n',
            "deploy/app.yaml": "env:\n  - name: BILLING_API_URL\n    value: https://billing.internal\n",
            "deploy/other.yaml": "env:\n  - name: SOMETHING_ELSE\n",
        },
    )
    change = target("src/api/client.py", "base_url", 4, 5)
    found = items(context(root, targets=[change], changed=["src/api/client.py"]).block_for([change.target_id]))
    (config,) = [item for item in found if item.kind == "config"]
    assert config.path == "deploy/app.yaml" and "BILLING_API_URL" in config.text


def test_files_outside_the_change_rank_before_callers_the_change_already_touches(repo: Path) -> None:
    both = context(repo, changed=["src/api/billing/pricing.py", "src/api/billing/invoice.py"])
    callers = [item for item in items(both.block_for([QUOTE.target_id])) if item.kind == "caller"]
    assert [item.path for item in callers] == ["src/api/billing/reports.py", "src/api/billing/invoice.py"]
    assert [item.in_change for item in callers] == [False, True]


def test_a_removed_symbol_is_searched_by_name_even_though_its_source_is_gone(tmp_path: Path) -> None:
    root = write(
        tmp_path / "repo",
        {
            "src/api/legacy.py": "VALUE = 1\n",
            "src/api/caller.py": "from api.legacy import old_quote\n\n\ndef run():\n    return old_quote(1, 2)\n",
        },
    )
    gone = replace(
        target("src/api/legacy.py", "old_quote", 1, 3),
        spans=(TargetSpan("old", 1, 3, 1),),
        kind="symbol",
    )
    found = items(context(root, targets=[gone], changed=["src/api/legacy.py"]).block_for([gone.target_id]))
    assert [(item.path, item.why) for item in found if item.kind == "caller"] == [
        ("src/api/caller.py", "calls old_quote")
    ]


def test_a_namesake_definition_elsewhere_is_not_reported_as_a_caller(tmp_path: Path) -> None:
    root = write(
        tmp_path / "repo",
        {
            "src/api/a.py": "def quote(x, y):\n    return x + y\n",
            "src/api/b.py": "def quote(x):\n    return x\n",
        },
    )
    change = target("src/api/a.py", "quote", 1, 2)
    assert context(root, targets=[change], changed=["src/api/a.py"]).block_for([change.target_id]) is None


def test_no_context_is_not_an_error(tmp_path: Path) -> None:
    root = write(tmp_path / "repo", {"src/api/solo.py": "def solo():\n    return 1\n"})
    change = target("src/api/solo.py", "solo", 1, 2)
    pack = context(root, targets=[change], changed=["src/api/solo.py"])
    assert pack.block_for([change.target_id]) is None
    summary = pack.summary()
    assert summary["status"] == "empty" and summary["estimated_tokens"] == 0 and summary["items"] == {}


# --------------------------------------------------------------------------- #
# what it never reads
# --------------------------------------------------------------------------- #


def test_a_path_the_content_access_policy_denies_is_never_in_the_pack(repo: Path) -> None:
    write(
        repo,
        {
            "vault/ledger.py": "from billing.pricing import quote\n\n\ndef price_for_vault(x):\n    return quote('pro', x)  # VAULT-ONLY\n"
        },
    )
    open_pack = context(repo).block_for([QUOTE.target_id])
    assert "VAULT-ONLY" in json.dumps(open_pack.value() if open_pack else {})

    denied = context(repo, rules=[deny("vault/**")])
    block = denied.block_for([QUOTE.target_id])
    wire = json.dumps(block.prompt_blocks() if block else {})
    assert "VAULT-ONLY" not in wire and "vault/ledger.py" not in wire
    assert denied.summary()["scan"]["skipped_by_policy"] == 1
    assert "vault/ledger.py" not in denied.summary()["paths"]


def test_a_customer_local_only_path_is_withheld_from_an_external_route_and_allowed_for_a_local_one(repo: Path) -> None:
    write(
        repo,
        {
            "internal/audit.py": "from billing.pricing import quote\n\n\ndef audit(x):\n    return quote('pro', x, 'eu')\n"
        },
    )
    rule = ContentAccessRule(
        mode="customer_local_only",
        path_scope=("internal/**",),
        origin="trusted_base",
        trust_class="trusted_base_repository",
        source_identity="test",
    )
    external = items(context(repo, rules=[rule], trust="external").block_for([QUOTE.target_id]))
    local = items(context(repo, rules=[rule], trust="customer_local").block_for([QUOTE.target_id]))
    assert "internal/audit.py" not in {item.path for item in external}
    assert "internal/audit.py" in {item.path for item in local}


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.production",
        "config/credentials.json",
        "keys/server.pem",
        "deploy/id_rsa",
        "secrets/token.py",
        ".aws/config",
    ],
)
def test_files_that_look_like_credentials_are_never_read(repo: Path, name: str) -> None:
    write(repo, {name: "SECRET_TOKEN = 'quote(plan, seats) sk-live-abcdefghijklmnopqrstuvwxyz012345'\n"})
    assert rc.is_secret_path(name)
    block = context(repo).block_for([QUOTE.target_id])
    assert "sk-live" not in json.dumps(block.prompt_blocks() if block else {})
    assert name not in (context(repo).summary()["paths"])


def test_a_template_env_file_is_not_treated_as_a_secret() -> None:
    assert not rc.is_secret_path(".env.example") and not rc.is_secret_path("config/app.env.sample")
    assert rc.is_secret_path(".env.local") and rc.is_secret_path("certs/tls.key")


def test_a_file_holding_a_private_key_is_skipped_whatever_it_is_called(repo: Path) -> None:
    write(
        repo,
        {
            # Assembled at runtime so the source never carries a key header the mirror's secret scan flags.
            "src/api/billing/keys.py": 'KEY = """-----BEGIN RSA '
            + "PRIVATE KEY-----\nMIIEow\n-----END RSA "
            + 'PRIVATE KEY-----"""\n'
            "def use():\n    return quote('pro', 1, 'eu')\n"
        },
    )
    block = context(repo).block_for([QUOTE.target_id])
    wire = json.dumps(block.value() if block else {})
    assert block is not None and "PRIVATE KEY" not in wire and "keys.py" not in wire


def test_credentials_inside_an_excerpt_are_redacted(repo: Path) -> None:
    write(
        repo,
        {
            "src/api/billing/gateway.py": "from billing.pricing import quote\n\n\ndef charge(x):\n"
            '    api_key = "sk-live-abcdefghijklmnopqrstuvwxyz012345"\n    return quote("pro", x, "eu")\n'
        },
    )
    block = context(repo).block_for([QUOTE.target_id])
    gateway = [item for item in items(block) if item.path.endswith("gateway.py")]
    assert gateway and all("sk-live" not in item.text for item in gateway)


def test_symlinks_are_not_followed_out_of_the_repository(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside, {"host.py": "def host():\n    return quote('pro', 1, 'eu')  # HOST-FILE\n"})
    (repo / "src/api/billing/link.py").symlink_to(outside / "host.py")
    (repo / "src/api/linked_dir").symlink_to(outside, target_is_directory=True)
    block = context(repo).block_for([QUOTE.target_id])
    assert "HOST-FILE" not in json.dumps(block.value() if block else {})


def test_build_and_vendor_directories_are_not_searched(repo: Path) -> None:
    write(
        repo,
        {
            "node_modules/x/index.js": "quote(1, 2, 3)\n",
            "vendor/lib.py": "def v():\n    return quote(1, 2, 3)\n",
            "dist/out.py": "def d():\n    return quote(1, 2, 3)\n",
        },
    )
    paths = {item.path for item in items(context(repo).block_for([QUOTE.target_id]))}
    assert not {p for p in paths if p.startswith(("node_modules", "vendor", "dist"))}


# --------------------------------------------------------------------------- #
# budgets
# --------------------------------------------------------------------------- #


def crowded(tmp_path: Path, callers: int = 40) -> Path:
    files = dict(FILES)
    for index in range(callers):
        files[f"src/api/users/handler_{index:02d}.py"] = (
            "from billing.pricing import quote\n\n\n"
            f"def handle_{index:02d}(request):\n"
            + "".join(f"    step_{n} = request.values[{n}] + {index}\n" for n in range(30))
            + "    return quote(request.plan, request.seats)\n"
        )
        files[f"tests/test_handler_{index:02d}.py"] = (
            f"from billing.pricing import quote\n\n\ndef test_handler_{index:02d}():\n    assert quote('basic', 1, 'eu') == 10\n"
        )
    return write(tmp_path / "crowded", files)


@pytest.mark.parametrize("effort", ["quick", "auto", "thorough", "deep"])
def test_the_pack_never_exceeds_its_depths_token_budget_or_item_caps(tmp_path: Path, effort: str) -> None:
    budget = rc.BUDGETS[effort]
    block = context(crowded(tmp_path), effort=effort).block_for([QUOTE.target_id])
    found = items(block)

    assert block is not None and block.tokens() <= budget.tokens
    assert sum(1 for item in found if item.kind == "caller") <= budget.callers
    assert sum(1 for item in found if item.kind == "test") <= budget.tests
    assert all(len(item.text) <= budget.snippet_chars + 2 for item in found)
    # The estimate is of what the prompt carries: the serialized blocks and the registry rows that cite the items.
    sent = json.dumps(block.prompt_blocks(), ensure_ascii=False, separators=(",", ":"))
    rows = json.dumps([item.registry_row() for item in block.items], ensure_ascii=False, separators=(",", ":"))
    assert rc.estimate_tokens(sent) + rc.estimate_tokens(rows) == block.tokens()
    assert block.omitted.get("caller", 0) > 0  # there was more than it could carry, and it says so


def test_the_budget_scales_with_the_investigator_ladder() -> None:
    sizes = [rc.BUDGETS[effort].tokens for effort in ("quick", "auto", "thorough", "deep")]
    assert sizes == sorted(sizes) and len(set(sizes)) == 4


def test_a_deeper_review_carries_more_context_than_a_shallower_one(tmp_path: Path) -> None:
    root = crowded(tmp_path)
    tokens = {}
    for effort in ("quick", "auto", "deep"):
        block = context(root, effort=effort).block_for([QUOTE.target_id])
        assert block is not None
        tokens[effort] = block.tokens()
    assert tokens["quick"] < tokens["auto"] < tokens["deep"]


def test_a_tight_token_ceiling_cuts_an_excerpt_instead_of_dropping_the_first_caller(tmp_path: Path) -> None:
    root = crowded(tmp_path, callers=3)
    floor = rc.RepoContextBlock((), {}, 0, False).tokens()
    options = rc.RepoContextOptions(budget=replace(rc.BUDGETS["auto"], tokens=floor + 260))
    block = context(root, options=options).block_for([QUOTE.target_id])
    found = items(block)
    assert block is not None and block.tokens() <= floor + 260
    assert found and found[0].kind == "caller"


def test_the_token_ceiling_can_be_lowered_from_the_environment() -> None:
    assert rc.budget_for("auto", env={"LEMONCROW_REVIEW_REPO_CONTEXT_TOKENS": "900"}).tokens == 900
    assert (
        rc.budget_for("auto", env={"LEMONCROW_REVIEW_REPO_CONTEXT_TOKENS": "nonsense"}).tokens
        == rc.BUDGETS["auto"].tokens
    )
    assert rc.repo_context_enabled({}) and not rc.repo_context_enabled({"LEMONCROW_REVIEW_REPO_CONTEXT": "off"})


def test_the_scan_stops_at_its_file_limit_and_says_so(tmp_path: Path) -> None:
    root = crowded(tmp_path, callers=30)
    options = rc.RepoContextOptions(budget=replace(rc.BUDGETS["auto"], scan_files=10))
    pack = context(root, options=options)
    block = pack.block_for([QUOTE.target_id])
    assert block is not None and block.scan_truncated
    assert pack.summary()["scan"]["truncated"] is True and pack.summary()["scan"]["files_read"] == 10


# --------------------------------------------------------------------------- #
# determinism and the optional index
# --------------------------------------------------------------------------- #


def test_the_same_snapshot_gives_byte_identical_packs_whatever_the_directory(tmp_path: Path) -> None:
    first = context(crowded(tmp_path / "a")).block_for([QUOTE.target_id])
    second = context(crowded(tmp_path / "b")).block_for([QUOTE.target_id])
    assert first is not None and second is not None
    assert json.dumps(first.prompt_blocks(), sort_keys=True) == json.dumps(second.prompt_blocks(), sort_keys=True)
    assert [item.ref for item in first.items] == [item.ref for item in second.items]


def test_a_pack_for_several_targets_names_the_target_each_excerpt_is_for(tmp_path: Path) -> None:
    root = write(
        tmp_path / "repo",
        {
            "src/api/a.py": "def alpha(x):\n    return x + 1\n",
            "src/api/b.py": "def beta(x):\n    return x + 2\n",
            "src/api/use.py": "from api.a import alpha\nfrom api.b import beta\n\n\ndef both(x):\n    return alpha(x) + beta(x)\n",
        },
    )
    alpha, beta = target("src/api/a.py", "alpha", 1, 2), target("src/api/b.py", "beta", 1, 2)
    pack = context(root, targets=[alpha, beta], changed=["src/api/a.py", "src/api/b.py"])
    (shared,) = [item for item in items(pack.block_for([alpha.target_id, beta.target_id])) if item.kind == "caller"]
    assert set(shared.target_ids) == {alpha.target_id, beta.target_id}
    only_alpha = items(pack.block_for([alpha.target_id]))
    assert all(item.target_ids == (alpha.target_id,) for item in only_alpha)


class FakeIndex:
    """A CodeIntelligenceProvider that knows one reference, and one that the Snapshot does not agree with."""

    def __init__(self, references: list[dict[str, Any]], *, available: bool = True) -> None:
        self.references_rows = references
        self.available = available
        self.asked: list[str] = []

    def readiness(self) -> Any:
        from lemoncrow.pro.capabilities.review.code_intelligence import (
            CodeIntelligenceLayerReadiness,
            CodeIntelligenceReadiness,
        )

        layer = CodeIntelligenceLayerReadiness("ready" if self.available else "unavailable")
        return CodeIntelligenceReadiness("fake", layer, layer, layer, layer)

    def find_symbol(self, symbol_name: str, **_: Any) -> Mapping[str, Any] | None:
        self.asked.append(symbol_name)
        return {"id": f"sym:{symbol_name}"}

    def references(self, symbol: Mapping[str, Any], *, limit: int = 20) -> Mapping[str, Any]:
        return {"references": self.references_rows[:limit]}


def test_an_index_locates_references_but_the_bytes_come_from_the_snapshot(repo: Path) -> None:
    write(
        repo,
        {
            "src/api/billing/odd.py": "def indirect(f):\n    handler = f\n    return handler\n\n\nquote_alias = 1  # quote\n"
        },
    )
    index = FakeIndex(
        [
            {"path": "src/api/billing/reports.py", "line": 5},
            {"path": "src/api/billing/odd.py", "line": 6},  # mentions quote in a comment: the Snapshot agrees
            {"path": "src/api/billing/gone.py", "line": 3},  # the index is stale: the Snapshot has no such file
            {"path": "src/api/billing/invoice.py", "line": 99},  # a line the Snapshot does not have
        ]
    )
    block = context(repo, options=rc.RepoContextOptions(provider=index)).block_for([QUOTE.target_id])
    callers = {item.path: item for item in items(block) if item.kind == "caller"}
    assert index.asked == ["quote"]
    assert callers["src/api/billing/reports.py"].via == "code_intelligence"
    assert "gone.py" not in json.dumps([item.path for item in items(block)])
    assert "monthly_revenue" in callers["src/api/billing/reports.py"].text  # re-read from the Snapshot, not the index


def test_an_unavailable_or_failing_index_changes_nothing(repo: Path) -> None:
    class Broken(FakeIndex):
        def references(self, symbol: Mapping[str, Any], *, limit: int = 20) -> Mapping[str, Any]:
            raise RuntimeError("index offline")

    plain = context(repo).block_for([QUOTE.target_id])
    for provider in (FakeIndex([], available=False), Broken([])):
        block = context(repo, options=rc.RepoContextOptions(provider=provider)).block_for([QUOTE.target_id])
        assert block is not None and plain is not None
        assert [item.ref for item in block.items] == [item.ref for item in plain.items]


def test_a_failure_while_reading_the_snapshot_never_raises(tmp_path: Path) -> None:
    def broken() -> Path:
        raise OSError("snapshot unavailable")

    pack = rc.RepoContext(
        root=broken,
        targets=[QUOTE],
        changed_paths=[],
        effort="auto",
        content_access=(),
        route_trust="external",
        options=rc.RepoContextOptions(),
    )
    assert pack.block_for([QUOTE.target_id]) is None
    assert pack.summary()["status"] == "unavailable" and pack.summary()["reason"] == "OSError"


def test_sent_tokens_are_only_what_was_recorded() -> None:
    pack = context(Path("."), targets=[])
    assert pack.sent_tokens == 0
    pack.record_sent(120)
    pack.record_sent(80)
    assert pack.summary()["estimated_tokens"] == 200 and pack.summary()["investigations_with_context"] == 2
