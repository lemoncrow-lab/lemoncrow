"""Deep rendering diffs: route selection, bounded matrix, DOM/a11y, determinism, retention (Phase 9R R7 item 13)."""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.render_diff import (
    ProtectedPage,
    RenderConfig,
    RenderUnavailable,
    ax_changes,
    page_changes,
    parse_render_config,
    plan_cells,
    protect_page,
    select_routes,
)
from lemoncrow.pro.capabilities.review.render_driver import (
    ChromeCdpDriver,
    DeterminismControls,
    InteractionStep,
    PageCapture,
    RenderCell,
    RenderEnvironment,
    Viewport,
    capture_from_wire,
    capture_to_wire,
    chrome_executable,
    screenshot_identity,
)
from tests.gateway.test_review_check_runs_api import HEADERS, _review
from tests.gateway.test_review_hermetic import needs_bwrap


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


DESKTOP = Viewport("desktop", 1280, 800)


# --------------------------------------------------------------------------- #
# route selection and the bounded cell plan
# --------------------------------------------------------------------------- #


def _config(**raw: Any) -> RenderConfig:
    return parse_render_config(
        {
            "static_root": "public",
            "routes": ["/", "/about.html"],
            "component_routes": {"src/components/Nav*": ["/", "/about.html"], "src/pages/Pricing.tsx": ["/pricing"]},
            **raw,
        }
    )


def test_routes_are_the_affected_ones_when_the_mapping_is_known_and_the_configured_set_otherwise() -> None:
    config = _config()
    known = select_routes(config, ["src/pages/Pricing.tsx", "public/about.html", "README.md"])
    assert (known.mapping, known.routes) == ("known", ("/pricing", "/about.html"))
    assert known.note == "Rendered only the 2 affected route(s)"

    unknown = select_routes(config, ["src/pages/Pricing.tsx", "src/widgets/Chart.tsx"], infer=lambda _path: None)
    assert unknown.mapping == "unknown" and unknown.routes == ("/pricing", "/", "/about.html")
    assert unknown.unmapped == ("src/widgets/Chart.tsx",)
    assert "mapping is unknown for src/widgets/Chart.tsx" in unknown.note and "configured routes" in unknown.note

    inferred = select_routes(config, ["src/widgets/Chart.tsx"], infer=lambda _path: ("/dashboard",))
    assert (inferred.mapping, inferred.routes) == ("known", ("/dashboard",))
    nothing = select_routes(config, ["server/app.py"])
    assert nothing.mapping == "none" and nothing.routes == ()


def test_the_matrix_is_bounded_by_depth_and_by_budget() -> None:
    config = _config(
        viewports=[{"id": "desktop", "width": 1280, "height": 800}, {"id": "mobile", "width": 390, "height": 844}],
        themes=["light", "dark"],
        max_cells=3,
        seconds_per_cell=10,
        interactions=[{"id": "open-menu", "route": "/", "steps": [{"click": "#menu"}]}],
    )
    selection = select_routes(config, ["public/index.html"])
    shallow = plan_cells(config, selection, entry="visual.dom_a11y", budget_seconds=None)
    assert [cell.id for cell in shallow.cells] == ["/@desktop/light", "flow:open-menu@desktop/light"]
    assert not shallow.limited_by_depth and not shallow.limited_by_budget

    deep = plan_cells(config, selection, entry="visual.matrix", budget_seconds=None)
    assert [cell.id for cell in deep.cells] == ["/@desktop/light", "/@desktop/dark", "/@mobile/light"]
    assert deep.limited_by_depth is True and deep.dropped == ("/@mobile/dark", "flow:open-menu@desktop/light")

    # 10 s per cell on two sides: 45 s affords two cells.
    budgeted = plan_cells(config, selection, entry="visual.matrix", budget_seconds=45)
    assert [cell.id for cell in budgeted.cells] == ["/@desktop/light", "/@desktop/dark"]
    assert budgeted.limited_by_budget is True and budgeted.estimate_seconds == 40
    assert len(budgeted.dropped) == 3


# --------------------------------------------------------------------------- #
# DOM / accessibility tree / page deltas
# --------------------------------------------------------------------------- #


def test_the_accessibility_diff_detects_a_changed_role_and_label() -> None:
    base = ['RootWebArea "Shop"', '  button "Save"', '  button "Menu"', '  heading "Cart" [level=2]']
    head = ['RootWebArea "Shop"', '  button "Submit"', '  link "Menu"', '  heading "Cart" [level=3]']
    changes = ax_changes(base, head)
    by_kind = {row["kind"]: row for row in changes}
    assert by_kind["label"]["base"]["name"] == "Save" and by_kind["label"]["head"]["name"] == "Submit"
    assert by_kind["role"]["base"]["role"] == "button" and by_kind["role"]["head"]["role"] == "link"
    assert by_kind["state"]["head"]["state"] == "level=3"
    assert ax_changes(base, base) == []


def _page(**fields: Any) -> ProtectedPage:
    return ProtectedPage(RenderCell("/", DESKTOP), "captured", **fields)


def test_page_changes_carry_network_console_layout_shift_and_performance_deltas_only() -> None:
    base = _page(
        screenshot_digest="px:sha256:1",
        network=("GET /app.js -> 200",),
        console=(),
        cls=0.01,
        metrics={"TaskDuration": 0.1},
    )
    head = _page(
        screenshot_digest="px:sha256:2",
        network=("GET /app.js -> 200", "GET https://cdn.example.com/x.js -> blocked"),
        console=("error: TypeError: x is undefined",),
        cls=0.25,
        metrics={"TaskDuration": 0.5},
    )
    changes = page_changes(base, head)
    assert sorted(changes) == ["console", "layout_shift", "network", "performance", "screenshot"]
    assert changes["network"] == {"added": ["GET https://cdn.example.com/x.js -> blocked"], "removed": []}
    assert changes["layout_shift"] == {"base": 0.01, "head": 0.25}
    # Below tolerance: no layout-shift or performance delta.
    assert (
        page_changes(
            base, _page(screenshot_digest="px:sha256:1", network=base.network, cls=0.03, metrics={"TaskDuration": 0.15})
        )
        == {}
    )


def _png(seed: bytes, size: tuple[int, int] = (24, 16)) -> bytes:
    from PIL import Image

    image = Image.new("RGB", size, (seed[0], seed[1], seed[2]))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_protect_page_masks_and_redacts_before_storing_and_withholds_an_unmaskable_screenshot(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.run_evidence import RunArtifactStore

    token = "AK" + "IA" + "Q" * 16  # assembled at runtime: credential-shaped literals never ship
    artifacts = RunArtifactStore(tmp_path / "runs")
    capture = PageCapture(
        "/@desktop/light",
        "captured",
        screenshot=_png(b"\x10\x20\x30"),
        mask_regions=((0, 0, 8, 8),),
        dom_lines=("<body>", f"  <p>key {token}</p>"),
        ax_lines=(f'paragraph "key {token}"',),
        console=(f"error: leaked {token}",),
    )
    page = protect_page(capture, RenderCell("/", DESKTOP), review_id="review-1", artifacts=artifacts, policy=None)
    assert page.masked_regions == 1 and page.screenshot_ref.startswith("run-artifact:review-1:")
    stored = b"".join(artifacts.read("review-1", ref.rsplit(":", 1)[1]) or b"" for ref in (page.dom_ref, page.ax_ref))
    assert token.encode() not in stored and token not in json.dumps(page.to_wire()) + "".join(page.console)
    masked = artifacts.read("review-1", page.screenshot_ref.rsplit(":", 1)[1]) or b""
    from PIL import Image

    assert Image.open(io.BytesIO(masked)).convert("RGB").getpixel((2, 2)) == (0, 0, 0)

    broken = PageCapture("/@desktop/light", "captured", screenshot=b"not a png", mask_regions=((0, 0, 1, 1),))
    withheld = protect_page(broken, RenderCell("/", DESKTOP), review_id="review-1", artifacts=artifacts, policy=None)
    assert withheld.screenshot_ref == "" and withheld.screenshot_withheld


# --------------------------------------------------------------------------- #
# the API with a deterministic driver at the driver seam
# --------------------------------------------------------------------------- #

_AX_RE = re.compile(r'<(button|a) aria-label="([^"]*)"')
_ROLE = {"button": "button", "a": "link"}


class FakeDriver:
    """Renders a page deterministically from its HTML: DOM lines, an AX tree, a seeded PNG."""

    def __init__(self, browsers: Sequence[str] = ("FakeChrome 1.0",)) -> None:
        self.browsers = list(browsers)
        self.environment_calls = 0
        self.captures: list[tuple[str, ...]] = []

    def environment(self) -> RenderEnvironment:
        browser = self.browsers[min(self.environment_calls, len(self.browsers) - 1)]
        self.environment_calls += 1
        return RenderEnvironment(True, browser=browser, fonts_digest="fonts:sha256:fixture")

    def capture(
        self,
        site: Path,
        cells: Sequence[RenderCell],
        controls: DeterminismControls,
        *,
        spa_fallback: bool,
        timeout: float,
    ) -> list[PageCapture]:
        self.captures.append(tuple(cell.id for cell in cells))
        out: list[PageCapture] = []
        for cell in cells:
            path = site / ("index.html" if cell.route == "/" else cell.route.lstrip("/"))
            if not path.is_file():
                out.append(PageCapture(cell.id, "failed", "404"))
                continue
            html = path.read_text(encoding="utf-8")
            seed = hashlib.sha256(f"{html}|{cell.viewport.id}|{cell.theme}".encode()).digest()
            ax = tuple(f'  {_ROLE[tag]} "{label}"' for tag, label in _AX_RE.findall(html))
            out.append(
                PageCapture(
                    cell.id,
                    "captured",
                    screenshot=_png(seed),
                    mask_regions=((0, 0, 4, 4),) if controls.mask_selectors else (),
                    dom_lines=tuple(line.strip() for line in html.splitlines() if line.strip()),
                    ax_lines=('RootWebArea "Fixture"', *ax),
                    console=tuple(re.findall(r"<!-- console: (.*?) -->", html)),
                )
            )
        return out


RENDER_CONFIG = """\
render:
  static_root: public
  routes: [/, /about.html]
  mask: [".clock"]
  viewports:
    - {id: desktop, width: 1280, height: 800}
    - {id: mobile, width: 390, height: 844}
  themes: [light, dark]
  max_cells: 3
"""

INDEX = '<html><body>\n<button aria-label="Save">Save</button>\n<button aria-label="Menu">=</button>\n</body></html>\n'
ABOUT = '<html><body>\n<a aria-label="Home" href="/">Home</a>\n</body></html>\n'


def _site(config: str = RENDER_CONFIG, *, index: str = INDEX) -> dict[str, str]:
    return {
        "public/index.html": index,
        "public/about.html": ABOUT,
        "public/style.css": "body { margin: 0 }\n",
        ".lemoncrow/review.yaml": config,
    }


def _render(review: SimpleNamespace, entry: str = "visual.dom_a11y", *, pick: bool = True) -> Any:
    return review.client.post(
        f"/api/reviews/{review.review_id}/runtime-observation/run",
        headers=HEADERS,
        json={"revision_id": review.revision_id, "entry": entry, "pick": pick},
    )


def _visual_targets(review: SimpleNamespace) -> list[dict[str, Any]]:
    behavior = review.client.get(f"/api/reviews/{review.review_id}/behavior", headers=HEADERS).json()["behavior"]
    return [row for row in (behavior or {}).get("targets") or [] if row["producer"] == "lemoncrow.render_diff"]


def _compute(review: SimpleNamespace) -> dict[str, int]:
    rows = review.client.get(f"/api/reviews/{review.review_id}/run-plan", headers=HEADERS).json()["compute"]["rows"]
    out: dict[str, int] = {}
    for row in rows:
        if row["stage"] == "run":
            key = f"{row['entry_id']}@{row['executor']}"
            out[key] = out.get(key, 0) + int(row["runs"])
    return out


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    from lemoncrow.pro.capabilities.review import runtime_observation_api

    fake = FakeDriver()
    monkeypatch.setattr(runtime_observation_api, "default_render_driver", lambda: fake)
    return fake


def test_a_render_diff_reports_role_and_label_changes_as_visual_deltas_with_evidence(
    tmp_path: Path, driver: FakeDriver
) -> None:
    head = INDEX.replace('aria-label="Save">', 'aria-label="Submit">').replace(
        '<button aria-label="Menu">=</button>', '<a aria-label="Menu">=</a>'
    )
    review = _review(tmp_path, base=_site(), head={"public/index.html": head})
    skipped = _render(review, pick=False)
    assert skipped.status_code == 409 and driver.captures == [] and _compute(review) == {}

    response = _render(review)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["selection"]["mapping"] == "known" and result["selection"]["routes"] == ["/"]
    assert [cell["id"] for cell in result["plan"]["cells"]] == ["/@desktop/light"]
    assert driver.captures == [("/@desktop/light",), ("/@desktop/light",)]
    assert result["comparable"] is True and result["browser"] == "FakeChrome 1.0"
    [page] = result["pages"]
    kinds = {
        (row["kind"], (row["base"] or {}).get("name"), (row["head"] or {}).get("name"))
        for row in page["changes"]["a11y"]["changes"]
    }
    assert ("label", "Save", "Submit") in kinds and ("role", "Menu", "Menu") in kinds
    assert sorted(page["changes"]) == ["a11y", "dom", "screenshot"]
    assert result["sides"]["head"]["pages"][0]["screenshot"]["masked_regions"] == 1
    assert _compute(review) == {"visual.dom_a11y@local": 2}

    targets = {row["title"]: row for row in _visual_targets(review)}
    assert set(targets) == {
        "Accessibility tree · /@desktop/light",
        "DOM · /@desktop/light",
        "Screenshot · /@desktop/light",
    }, targets
    a11y = targets["Accessibility tree · /@desktop/light"]
    assert a11y["kind"] == "visual" and a11y["severity"] == "high"
    evidence = review.client.get(
        f"/api/reviews/{review.review_id}/behavior/targets/{a11y['behavior_key']}", headers=HEADERS
    ).json()["target"]["evidence"]
    assert evidence["refs"][0]["ref"].startswith("artifact:")
    assert {ref["ref"].split(":", 2)[1] for ref in evidence["refs"][1:]} == {"test"}

    shot = page["changes"]["screenshot"]["head"]["ref"].rsplit(":", 1)[1]
    served = review.client.get(f"/api/reviews/{review.review_id}/runtime-observation/artifacts/{shot}", headers=HEADERS)
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff" and "no-store" in served.headers["cache-control"]
    assert served.content.startswith(b"\x89PNG")
    unknown = review.client.get(
        f"/api/reviews/{review.review_id}/runtime-observation/artifacts/{'0' * 64}", headers=HEADERS
    )
    assert unknown.status_code == 404
    assert (
        review.client.get(
            f"/api/reviews/{review.review_id}/runtime-observation/artifacts/..%2fx", headers=HEADERS
        ).status_code
        == 404
    )

    state = review.client.get(f"/api/reviews/{review.review_id}/runtime-observation", headers=HEADERS).json()
    assert [row["entry"] for row in state["render"]] == ["visual.dom_a11y"]

    # A repeat request reuses both stored sides: no new render, no new compute.
    again = _render(review)
    assert again.status_code == 200 and again.json()["executions"] == 0 and again.json()["reused"] == 2
    assert len(driver.captures) == 2 and _compute(review) == {"visual.dom_a11y@local": 2}


def test_the_same_fingerprint_yields_identical_digests_and_no_delta(tmp_path: Path, driver: FakeDriver) -> None:
    # A stylesheet change maps to no known route: the configured routes render, and the result says so.
    review = _review(tmp_path, base=_site(), head={"public/style.css": "body { margin: 1px }\n"})
    response = _render(review)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["selection"]["mapping"] == "unknown" and result["selection"]["routes"] == ["/", "/about.html"]
    assert any("mapping is unknown for public/style.css" in reason for reason in result["reasons"])
    assert result["comparable"] is True and result["pages"] == [] and result["unchanged"] == 2
    base, head = result["sides"]["base"], result["sides"]["head"]
    assert base["digest"] == head["digest"]
    assert [page["digest"] for page in base["pages"]] == [page["digest"] for page in head["pages"]]
    assert _visual_targets(review) == []


def test_a_fingerprint_mismatch_is_not_comparable_and_never_a_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import runtime_observation_api

    # The browser updated between the base and the head capture.
    drifting = FakeDriver(browsers=("FakeChrome 1.0", "FakeChrome 2.0"))
    monkeypatch.setattr(runtime_observation_api, "default_render_driver", lambda: drifting)
    head = INDEX.replace('aria-label="Save">', 'aria-label="Submit">')
    review = _review(tmp_path, base=_site(), head={"public/index.html": head})
    response = _render(review)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["comparable"] is False and result["pages"] == []
    assert result["environment"]["base"] != result["environment"]["head"]
    assert "toolchain" in " ".join(result["differences"])
    assert any("not comparable (never a regression)" in reason for reason in result["reasons"])
    assert _visual_targets(review) == []


def test_the_render_sandbox_finds_a_local_python_and_the_debian_chromium_launcher_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: str(self).startswith("/etc/chromium") or real_exists(self))
    args = ChromeCdpDriver(chrome="/usr/bin/chromium", bwrap="/usr/bin/bwrap")._sandbox(tmp_path, tmp_path)
    assert "/usr/local/bin" in args[args.index("PATH") + 1].split(":")
    for path in ("/etc/chromium", "/etc/chromium.d"):
        assert args[args.index(path) - 1 : args.index(path) + 2] == ["--ro-bind", path, path]


def test_a_host_that_cannot_render_reports_not_run_and_never_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import runtime_observation_api

    class NoBrowser(FakeDriver):
        def environment(self) -> RenderEnvironment:
            return RenderEnvironment(False, reason="rendering diffs need bubblewrap")

        def capture(self, *_args: Any, **_kwargs: Any) -> list[PageCapture]:
            raise RenderUnavailable("rendering diffs need bubblewrap")

    monkeypatch.setattr(runtime_observation_api, "default_render_driver", NoBrowser)
    review = _review(tmp_path, base=_site(), head={"public/index.html": INDEX.replace("Save", "Submit")})
    response = _render(review)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert {side["status"] for side in result["sides"].values()} == {"NOT_RUN"}
    assert result["comparable"] is False and result["unchanged"] == 0 and result["pages"] == []
    assert any("head was not rendered" in reason for reason in result["reasons"])
    assert not any("fingerprints differ" in reason for reason in result["reasons"])
    assert _visual_targets(review) == []


def test_a_page_neither_side_captured_is_not_counted_as_unchanged(tmp_path: Path, driver: FakeDriver) -> None:
    # /about.html is absent on both sides, so both captures of it fail.
    base = {name: text for name, text in _site().items() if name != "public/about.html"}
    review = _review(tmp_path, base=base, head={"public/style.css": "body { margin: 1px }\n"})
    response = _render(review)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["unchanged"] == 1 and result["not_rendered"] == 1 and result["pages"] == []


def test_a_side_that_did_not_render_is_not_comparable_and_counts_nothing_as_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import runtime_observation_api

    class HeadCannotRender(FakeDriver):
        def capture(self, site: Path, cells: Sequence[RenderCell], *args: Any, **kwargs: Any) -> list[PageCapture]:
            if self.captures:
                raise RenderUnavailable("the app build failed (exit 2)")
            return super().capture(site, cells, *args, **kwargs)

    monkeypatch.setattr(runtime_observation_api, "default_render_driver", HeadCannotRender)
    review = _review(tmp_path, base=_site(), head={"public/index.html": INDEX.replace("Save", "Submit")})

    result = _render(review).json()["result"]

    assert result["sides"]["base"]["status"] == "PASS" and result["sides"]["head"]["status"] == "NOT_RUN"
    assert result["comparable"] is False and result["pages"] == [] and result["unchanged"] == 0
    assert any("head was not rendered: the app build failed (exit 2)" in row for row in result["reasons"])
    assert _visual_targets(review) == []


def test_render_cells_and_controls_survive_the_executor_boundary() -> None:
    step = InteractionStep("click", "#go")
    cell = RenderCell("/a", Viewport("mobile", 390, 844), "dark", "checkout", (step,))
    assert RenderCell.from_wire(cell.to_wire()) == cell
    controls = DeterminismControls(mask_selectors=(".clock",))
    assert DeterminismControls.from_wire(controls.to_wire()) == controls
    for broken in ({**cell.to_wire(), "id": "/b"}, {**cell.to_wire(), "route": "a"}, {**cell.to_wire(), "steps": "x"}):
        with pytest.raises(ValueError):
            RenderCell.from_wire(broken)


def test_a_capture_round_trips_and_a_foreign_row_is_a_failed_cell() -> None:
    capture = PageCapture(
        "c",
        "captured",
        screenshot=b"\x89PNG-bytes",
        mask_regions=((1, 2, 3, 4),),
        dom_lines=("<p>",),
        ax_lines=('button "Go"',),
        network=("GET /",),
        console=("hi",),
        cls=0.5,
        lcp_ms=12.0,
        metrics={"TaskDuration": 0.1},
    )
    assert capture_from_wire("c", capture_to_wire(capture)) == capture
    assert capture_from_wire("other", capture_to_wire(capture)).status == "failed"
    failed = capture_from_wire("c", capture_to_wire(PageCapture("c", "failed", "404")))
    assert (failed.status, failed.reason) == ("failed", "404")


def test_the_matrix_is_limited_by_depth_and_by_budget_and_says_so(tmp_path: Path, driver: FakeDriver) -> None:
    head = INDEX.replace('aria-label="Save">', 'aria-label="Submit">')
    deep = _review(tmp_path / "deep", base=_site(), head={"public/index.html": head})
    response = _render(deep, "visual.matrix")
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert [cell["id"] for cell in result["plan"]["cells"]] == ["/@desktop/light", "/@desktop/dark", "/@mobile/light"]
    assert result["plan"]["limited_by_depth"] is True and result["plan"]["dropped"] == ["/@mobile/dark"]
    assert "Limited by depth (render.max_cells): 1 cell(s) not rendered" in result["reasons"]
    planned_minutes = float(
        deep.client.get(f"/api/reviews/{deep.review_id}/run-plan", headers=HEADERS).json()["plan"]["budget"]["planned"][
            "minutes"
        ]
    )

    # A ceiling just above the plan: the entry is admitted with its estimate (40 min for base+head)
    # plus ~0.6 s of slack, which affords ten of the twelve 240 s cells (120 s per cell, both sides).
    tight_config = (
        RENDER_CONFIG.replace("max_cells: 3", "max_cells: 12").replace(
            "    - {id: mobile, width: 390, height: 844}\n",
            "    - {id: mobile, width: 390, height: 844}\n    - {id: tablet, width: 820, height: 1180}\n",
        )
        + f"  seconds_per_cell: 120\nreview:\n  budget:\n    per_review_minutes: {planned_minutes + 0.01}\n"
    )
    both = {"public/index.html": head, "public/about.html": ABOUT.replace("Home", "Start")}
    tight = _review(tmp_path / "tight", base=_site(tight_config), head=dict(both))
    response = _render(tight, "visual.matrix")
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["plan"]["limited_by_budget"] is True and result["plan"]["limited_by_depth"] is False
    assert len(result["plan"]["cells"]) == 10
    # Affected routes in changed-path order (about.html, then index.html); the last cells are dropped.
    assert result["plan"]["dropped"] == ["/@tablet/light", "/@tablet/dark"]
    assert f"Limited by budget: {len(result['plan']['dropped'])} cell(s) not rendered" in result["reasons"]


def test_render_artifacts_follow_raw_retention_and_a_lapsed_one_is_gone(tmp_path: Path, driver: FakeDriver) -> None:
    from lemoncrow.pro.capabilities.review.run_evidence import RunArtifactStore, review_reference_resolver

    token = "gh" + "p_" + "R" * 36  # assembled at runtime
    head = INDEX.replace("</body>", f"<p>{token}</p>\n</body>")
    review = _review(tmp_path, base=_site(), head={"public/index.html": head})
    response = _render(review)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    [page] = result["pages"]
    dom_digest = page["changes"]["dom"]["head"]["ref"].rsplit(":", 1)[1]
    url = f"/api/reviews/{review.review_id}/runtime-observation/artifacts/{dom_digest}"
    dom = review.client.get(url, headers=HEADERS)
    assert dom.status_code == 200 and dom.headers["content-type"].startswith("text/plain")
    assert token not in dom.text and "<p>" in dom.text
    everything = json.dumps(result) + "".join(
        (review.store.read_evidence_artifact(item) or b"").decode()
        for item in review.store.list_evidence(review.review_id)
    )
    assert token not in everything

    # Raw records carry no stable key and no judgment cites them: the closer drops the bytes after the window.
    aged = RunArtifactStore.for_store(
        review.store.root, clock=lambda: time.time() + 40 * 86400, resolver=review_reference_resolver(review.store)
    )
    removed = aged.expire(resolver=aged.resolver)
    assert f"run-artifact:{review.review_id}:{dom_digest}" in removed
    gone = review.client.get(url, headers=HEADERS)
    assert gone.status_code == 410 and "re-run" in gone.json()["detail"]


# --------------------------------------------------------------------------- #
# a real headless browser (skipped without bubblewrap or a non-snap Chrome)
# --------------------------------------------------------------------------- #

needs_chrome = pytest.mark.skipif(not chrome_executable(), reason="no non-snap Chrome/Chromium on this host")

REAL_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fixture</title>
<style>body { font: 16px sans-serif; margin: 24px } .clock { color: #333 }</style></head>
<body>
<h1>Orders</h1>
<p class="clock">Rendered at <span id="now"></span></p>
<p>Lucky number <span id="rand"></span></p>
<button aria-label="LABEL">Go</button>
<img src="https://cdn.example.com/pixel.png" alt="">
<script>
document.getElementById("now").textContent = new Date().toISOString();
document.getElementById("rand").textContent = String(Math.random());
console.error("fixture error");
</script>
</body></html>
"""


@needs_bwrap
@needs_chrome
def test_a_real_sandboxed_browser_is_deterministic_and_sees_a_label_change(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.render_driver import ChromeCdpDriver

    driver = ChromeCdpDriver()
    assert driver.environment().available, driver.environment().reason
    controls = DeterminismControls(mask_selectors=(".clock",))
    cells = [
        RenderCell("/", DESKTOP),
        RenderCell("/", Viewport("mobile", 390, 844), "dark"),
        RenderCell("/changed/", DESKTOP),
    ]
    site = tmp_path / "site"
    changed_site = site / "changed"
    changed_site.mkdir(parents=True)
    (site / "index.html").write_text(REAL_PAGE.replace("LABEL", "Checkout"), encoding="utf-8")
    (changed_site / "index.html").write_text(REAL_PAGE.replace("LABEL", "Pay now"), encoding="utf-8")

    first = driver.capture(site, cells, controls, spa_fallback=False, timeout=120)
    second = driver.capture(site, cells, controls, spa_fallback=False, timeout=120)
    assert [page.status for page in first] == ["captured", "captured", "captured"], [page.reason for page in first]
    for one, two in zip(first, second, strict=True):
        # Frozen clock, seeded randomness, pinned locale/timezone: byte-identical renders.
        assert screenshot_identity(one.screenshot) == screenshot_identity(two.screenshot)
        assert one.dom_lines == two.dom_lines and one.ax_lines == two.ax_lines
    page = first[0]
    assert page.mask_regions, "the .clock region is masked"
    assert any("2024-01-01T00:00:00.000Z" in line for line in page.dom_lines) is False  # masked text
    assert any("cdn.example.com" in row and "blocked" in row for row in page.network)
    assert any("fixture error" in row for row in page.console)
    labels = [row for row in ax_changes(first[0].ax_lines, first[2].ax_lines) if row["kind"] == "label"]
    assert labels and labels[0]["base"]["name"] == "Checkout" and labels[0]["head"]["name"] == "Pay now"
