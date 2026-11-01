"""Review names the other repositories of the reviewer's workspace a change reaches (P4,
docs/planning/2026-11-01-multi-repo-workspace.md section 11.4).

Real Git repositories side by side in a folder, or nested as submodules: the packet's ``workspace_impact`` slice, its
degraded names, its round trip through the packet decoder, and where ``lc review`` and ``lc review --ai`` show it.
It is never a finding, and a git hook never scans the workspace.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest
from lemoncrow_client.config import load_config

from lemoncrow.pro.capabilities.review import hosted, workspace_impact
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.models import ReviewPacket, review_packet_from_dict
from lemoncrow.pro.capabilities.review.packet import build_review_packet
from lemoncrow.pro.capabilities.review.pr_bot import local
from lemoncrow.pro.capabilities.review.render import render_review
from lemoncrow.pro.capabilities.review.workspace_impact import (
    FAILED,
    PARTIAL,
    UNAVAILABLE,
    WORKSPACE_ENV,
    changed_by_member,
    heading,
    impact_lines,
    reader_payload,
    workspace_for,
)
from tests.gateway.pr_bot_support import BASE
from tests.gateway.test_cli_review_local import make_lab
from tests.gateway.test_review_live_capture import _FakeRemote

WORKSPACE_NAMES = {FAILED, PARTIAL, UNAVAILABLE}

BILLING = {
    "pyproject.toml": '[project]\nname = "billing"\nversion = "1.0"\n',
    "billing/__init__.py": "",
    "billing/invoices.py": "def total(lines):\n    return sum(lines)\n",
}
BILLING_CHANGED = {"billing/invoices.py": "def total(lines):\n    return round(sum(lines), 2)\n"}
BAZAAR = {
    "pyproject.toml": '[project]\nname = "bazaar"\nversion = "1.0"\ndependencies = ["billing"]\n',
    "bazaar/__init__.py": "",
    "bazaar/checkout.py": "from billing import invoices\n\n\ndef pay(lines):\n    return invoices.total(lines)\n",
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No ast-grep download (``sg`` is refused outright) and no workspace from the caller's environment."""

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _identify(root: Path) -> None:
    for key, value in (("user.name", "W"), ("user.email", "w@example.invalid"), ("commit.gpgsign", "false")):
        _git(root, "config", key, value)


def _repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _identify(root)
    _write(root, files)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _packet(repo: Path, tmp_path: Path, **kwargs: Any) -> ReviewPacket:
    rng = resolve_rev_range(repo, working_tree=True)
    return build_review_packet(repo, rng, store_root=tmp_path / "store", **kwargs)


def _reached(packet: ReviewPacket) -> list[str]:
    return [hit["repo"] for hit in packet.workspace_impact.get("reached", [])]


@pytest.fixture
def projects(tmp_path: Path) -> Path:
    """A folder of two repositories: bazaar imports billing."""

    root = tmp_path / "projects"
    root.mkdir()
    _repo(root / "billing", BILLING)
    _repo(root / "bazaar", BAZAAR)
    return root


@pytest.fixture
def platform(tmp_path: Path) -> Path:
    """``projects/platform`` has billing as a submodule and imports it; ``projects/bazaar`` imports it too."""

    source = _repo(tmp_path / "sources" / "billing", BILLING)
    root = tmp_path / "projects"
    platform = _repo(root / "platform", {"shop/__init__.py": "", "shop/cart.py": "from billing import invoices\n"})
    _git(platform, "submodule", "add", "-q", str(source), "billing")
    _git(platform, "commit", "-qm", "billing")
    _identify(platform / "billing")
    _repo(root / "bazaar", BAZAAR)
    return platform


# --------------------------------------------------------------------------- #
# the packet slice
# --------------------------------------------------------------------------- #


def test_a_change_another_repository_imports_names_that_repository_with_its_evidence(
    projects: Path, tmp_path: Path
) -> None:
    billing = projects / "billing"
    _write(billing, BILLING_CHANGED)
    packet = _packet(billing, tmp_path, with_workspace=True)
    found = packet.workspace_impact
    assert found["repo"] == "billing" and found["root"] == str(projects.resolve())
    assert found["changed"] == [{"repo": "billing", "files": ["billing/billing/invoices.py"]}]  # root-relative
    assert _reached(packet) == ["bazaar"]
    hit = found["reached"][0]
    assert hit["hops"] == 1 and hit["path"] == ["billing", "bazaar"]
    links = [link for edge in hit["edges"] for link in edge["links"]]
    assert any(link["consumer"]["path"] == "bazaar/bazaar/checkout.py" for link in links), links
    assert not WORKSPACE_NAMES & set(packet.degraded)
    assert packet.stats["files"] == 1  # an enrichment: the reviewed change is this repository's alone


def test_the_slice_is_built_only_when_asked_and_only_with_the_impact_pass(
    projects: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    def recorded(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], tuple[str, ...]]:
        calls.append(args)
        return {}, ()

    monkeypatch.setattr(workspace_impact, "collect_workspace_impact", recorded)
    billing = projects / "billing"
    _write(billing, BILLING_CHANGED)
    assert _packet(billing, tmp_path).workspace_impact == {}
    assert _packet(billing, tmp_path, with_workspace=True, with_impact=False).workspace_impact == {}
    assert calls == []  # the scan never started


def test_a_repository_with_no_workspace_has_no_slice_and_no_degraded_name(tmp_path: Path) -> None:
    alone = _repo(tmp_path / "alone" / "billing", BILLING)
    _write(alone, BILLING_CHANGED)
    assert workspace_for(alone, {}) is None
    packet = _packet(alone, tmp_path, with_workspace=True)
    assert packet.workspace_impact == {} and not WORKSPACE_NAMES & set(packet.degraded)


def test_a_scan_that_fails_costs_a_degraded_name_never_the_review(
    projects: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_client import workspace_graph

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the scan broke")

    monkeypatch.setattr(workspace_graph, "impact", broken)
    billing = projects / "billing"
    _write(billing, BILLING_CHANGED)
    packet = _packet(billing, tmp_path, with_workspace=True)
    assert packet.workspace_impact == {} and FAILED in packet.degraded
    assert packet.stats["files"] == 1


def test_a_member_the_scan_could_not_see_marks_the_slice_partial(
    projects: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_client import workspace_graph

    real = workspace_graph.impact

    def truncated(*args: Any, **kwargs: Any) -> Any:
        from dataclasses import replace

        return replace(real(*args, **kwargs), incomplete=("bazaar",))

    monkeypatch.setattr(workspace_graph, "impact", truncated)
    billing = projects / "billing"
    _write(billing, BILLING_CHANGED)
    packet = _packet(billing, tmp_path, with_workspace=True)
    assert PARTIAL in packet.degraded and _reached(packet) == ["bazaar"]
    assert "partial: bazaar was not fully scanned; reach through it may be missing" in impact_lines(
        packet.workspace_impact
    )


# --------------------------------------------------------------------------- #
# which workspace: the variable, the folder, the superproject
# --------------------------------------------------------------------------- #


def test_a_submodule_belongs_to_the_folder_around_its_outermost_superproject(platform: Path, tmp_path: Path) -> None:
    projects = platform.parent.resolve()
    billing = platform / "billing"
    assert workspace_for(billing, {}) == (projects, "platform/billing")
    assert workspace_for(platform, {}) == (projects, "platform")
    # The variable comes first when it lists the repository: here the superproject alone.
    assert workspace_for(billing, {WORKSPACE_ENV: str(platform)}) == (platform.resolve(), "billing")
    # A variable naming a workspace the repository is not in is passed over.
    assert workspace_for(billing, {WORKSPACE_ENV: str(tmp_path / "sources")}) == (projects, "platform/billing")
    _write(billing, BILLING_CHANGED)
    packet = _packet(billing, tmp_path, with_workspace=True)
    assert packet.workspace_impact["repo"] == "platform/billing"
    assert sorted(_reached(packet)) == ["bazaar", "platform"]


def test_a_superproject_without_a_folder_workspace_is_its_own_workspace(tmp_path: Path) -> None:
    outer = _repo(tmp_path / "outer", {"README.md": "# outer\n"})
    source = _repo(tmp_path / "sources" / "billing", BILLING)
    platform = outer / "platform"
    platform.mkdir()
    _git(platform, "init", "-q", "-b", "main")
    _identify(platform)
    _write(platform, {"shop/__init__.py": "", "shop/cart.py": "from billing import invoices\n"})
    _git(platform, "add", "-A")
    _git(platform, "commit", "-qm", "base")
    _git(platform, "submodule", "add", "-q", str(source), "billing")
    _git(platform, "commit", "-qm", "billing")
    # The folder holding the superproject is inside another repository's work tree: not a workspace of folders.
    assert workspace_for(platform / "billing", {}) == (platform.resolve(), "billing")


def test_a_moved_submodule_pointer_changes_the_files_the_move_changed(platform: Path) -> None:
    billing = platform / "billing"
    old = _git(billing, "rev-parse", "HEAD")
    _write(billing, BILLING_CHANGED)
    _git(billing, "commit", "-qam", "round")
    new = _git(billing, "rev-parse", "HEAD")
    projects = platform.parent
    rows = [("billing", None, (old, new)), ("shop/cart.py", "shop/basket.py", None)]
    assert changed_by_member(projects, "platform", rows) == {
        "platform/billing": ["billing/invoices.py"],
        "platform": ["shop/cart.py", "shop/basket.py"],
    }
    # A pointer whose commits the checkout does not have changed nothing that can be listed.
    assert changed_by_member(projects, "platform", [("billing", None, (old, "0" * 40))]) == {}


def test_reviewing_a_superproject_whose_submodule_moved_reaches_what_uses_the_submodule(
    platform: Path, tmp_path: Path
) -> None:
    billing = platform / "billing"
    _write(billing, BILLING_CHANGED)
    _git(billing, "commit", "-qam", "round")
    packet = _packet(platform, tmp_path, with_workspace=True)
    found = packet.workspace_impact
    assert found["repo"] == "platform"
    assert found["changed"] == [{"repo": "platform/billing", "files": ["platform/billing/billing/invoices.py"]}]
    assert "bazaar" in _reached(packet)


# --------------------------------------------------------------------------- #
# the packet contract and the text
# --------------------------------------------------------------------------- #


def test_the_slice_survives_the_decoder_and_is_shown_after_the_reading_order_never_as_attention(
    projects: Path, tmp_path: Path
) -> None:
    billing = projects / "billing"
    _write(billing, BILLING_CHANGED)
    packet = _packet(billing, tmp_path, with_workspace=True)
    decoded = review_packet_from_dict(json.loads(json.dumps(packet.to_dict())))
    assert decoded.workspace_impact == packet.workspace_impact and decoded.workspace_impact
    text = render_review(packet, no_color=True)
    assert "WORKSPACE IMPACT  1 other repository can be affected (" in text
    assert "this repository is billing)" in text
    section = text.index("WORKSPACE IMPACT")
    assert text.index("START HERE") < section < text.index("EXECUTION EVIDENCE")
    assert "  bazaar  1 hop  medium  direct\n" in text
    assert "    import billing: bazaar/bazaar/checkout.py:1 uses billing/billing/\n" in text
    assert "Not findings: repositories that depend on what changed, for a person to check." in text
    assert "bazaar" not in text[: text.index("START HERE")]  # nothing in ATTENTION or the change story
    assert "WORKSPACE IMPACT" in render_review(packet)  # the Rich renderer too
    # A packet with no workspace reads exactly as before.
    assert "WORKSPACE IMPACT" not in render_review(_packet(billing, tmp_path), no_color=True)


def _hit(repo: str, links: int = 1) -> dict[str, Any]:
    edge_links = [
        {
            "key": f"k{index}",
            "confidence": "high",
            "provider": {"path": "billing/billing/__init__.py", "line": 0, "detail": "PROVIDER SOURCE LINE"},
            "consumer": {"path": f"{repo}/main.py", "line": index + 1, "detail": "CONSUMER SOURCE LINE"},
        }
        for index in range(links)
    ]
    return {
        "repo": repo,
        "hops": 1,
        "confidence": "high",
        "direct": True,
        "path": ["billing", repo],
        "edges": [{"kind": "package", "confidence": "high", "direct": True, "links": edge_links, "more": 0}],
    }


def test_the_text_is_bounded_quotes_no_source_and_carries_no_escape_from_another_repository() -> None:
    payload = {"root": "/w", "repo": "billing", "reached": [_hit(f"r{index}", links=3) for index in range(7)]}
    lines = impact_lines(payload)
    assert sum(1 for line in lines if not line.startswith(" ") and " hop" in line) == 5
    assert "... 2 more (--all lists every one)" in lines
    assert lines.count("  ... 1 more package evidence") == 5
    assert "r6  1 hop  high  direct" in impact_lines(payload, show_all=True)
    assert not any("SOURCE LINE" in line for line in impact_lines(payload, show_all=True))
    assert "  package k0: r0/main.py:1 uses billing/billing/__init__.py" in lines
    hostile = {"root": "/w\x1b[2J", "repo": "billing", "reached": [_hit("evil\x1b]8;;x\x07name")]}
    text = "\n".join([heading(hostile), *impact_lines(hostile)])
    assert "\x1b" not in text and "\x07" not in text and "evil]8;;xname" in text
    assert heading({"root": "/w", "repo": ".", "reached": []}).startswith("no other repository depends on what changed")


def test_the_reader_receives_every_value_but_the_source_lines() -> None:
    stored = {"root": "/w", "repo": "billing", "reached": [_hit("bazaar", links=2)], "incomplete": ["shop"]}
    sent = reader_payload({"workspace_impact": stored})
    assert "SOURCE LINE" not in json.dumps(sent)
    link = sent["reached"][0]["edges"][0]["links"][1]
    assert link == {
        "key": "k1",
        "confidence": "high",
        "provider": {"path": "billing/billing/__init__.py", "line": 0},
        "consumer": {"path": "bazaar/main.py", "line": 2},
    }
    assert sent["incomplete"] == ["shop"] and sent["reached"][0]["hops"] == 1
    assert "detail" in stored["reached"][0]["edges"][0]["links"][0]["provider"]  # the stored packet is untouched
    assert reader_payload({"workspace_impact": {}}) == {} == reader_payload(None) == reader_payload({})


# --------------------------------------------------------------------------- #
# lc review --ai: a person at the command sees it; a git hook never scans
# --------------------------------------------------------------------------- #

LEDGER = {"ledger/__init__.py": "", "ledger/books.py": "def balance(rows):\n    return sum(rows)\n"}
LEDGER_CHANGED = {"ledger/books.py": "def balance(rows):\n    return round(sum(rows), 2)\n"}


def test_review_ai_at_the_command_shows_the_reached_repositories_after_the_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = make_lab(tmp_path, monkeypatch, {**BASE, **LEDGER})
    _repo(tmp_path / "shop", {"shop/__init__.py": "", "shop/pay.py": "from ledger import books\n"})
    lab.write(LEDGER_CHANGED)
    out = lab.run().stdout
    assert "Workspace impact  1 other repository can be affected (" in out, out
    assert "  shop  1 hop  " in out and "Not findings:" in out
    assert out.index("LemonCrow review · ") < out.index("Workspace impact")
    if "Findings" in out:
        assert out.index("Findings") < out.index("Workspace impact")
    wire = lab.wire()
    assert wire["workspace_impact"]["repo"] == "repo"
    assert [hit["repo"] for hit in wire["workspace_impact"]["reached"]] == ["shop"]
    assert all(row["path"] != "shop/pay.py" for row in wire["findings"])  # never a finding


def _recording(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []
    real = workspace_impact.collect_workspace_impact

    def recorded(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], tuple[str, ...]]:
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(workspace_impact, "collect_workspace_impact", recorded)
    return calls


def test_a_git_hook_review_never_scans_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab = make_lab(tmp_path, monkeypatch, {**BASE, **LEDGER})
    _repo(tmp_path / "shop", {"shop/__init__.py": "", "shop/pay.py": "from ledger import books\n"})
    calls: list[Any] = []

    def recorded(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], tuple[str, ...]]:
        calls.append(args)
        return {}, ()

    monkeypatch.setattr(workspace_impact, "collect_workspace_impact", recorded)
    lab.write(LEDGER_CHANGED)
    report = local.run_local_review(local.LocalOptions(lab.repo, lab.store, working_tree=True))
    assert calls == [] and report.workspace_impact == {}
    assert "workspace_impact" not in report.to_wire()
    assert "Workspace impact" not in local.render_text(report)


# --------------------------------------------------------------------------- #
# lc review on a Review server: a local server stores the slice, a hosted one never receives it
# --------------------------------------------------------------------------- #


class _Remote(_FakeRemote):
    def review_manifest(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(capture_options=())


def _server_config(repo: Path, state: Path, mode: str) -> Any:
    return load_config(
        {
            "LEMONCROW_URL": "http://127.0.0.1:7420",
            "LEMONCROW_INSTALL_MODE": mode,
            "LEMONCROW_HOME": str(state),
            "HOME": str(state.parent),
        },
        cwd=repo,
    )


def _capture(repo: Path, state: Path, mode: str, **kwargs: Any) -> _FakeRemote:
    hosted.capture_server_review(
        _server_config(repo, state, mode),
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=state,
        session_id=None,
        limit=20,
        with_impact=True,
        with_provenance=False,
        **kwargs,
    )
    remote = _FakeRemote.last
    assert remote is not None
    return remote


def test_a_local_server_capture_publishes_first_then_enriches_with_the_slice(
    projects: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted, "RemoteSession", _Remote)
    calls = _recording(monkeypatch)
    billing = projects / "billing"
    _write(billing, BILLING_CHANGED)
    remote = _capture(billing, tmp_path / "state", "local", with_workspace=True)
    # The first readable Snapshot never waits for the scan: it joins the post-publication impact pass.
    assert remote.captured_packets[0]["workspace_impact"] == {}
    found = remote.enriched_packets[-1]["workspace_impact"]
    assert found["repo"] == "billing" and [hit["repo"] for hit in found["reached"]] == ["bazaar"]
    assert len(calls) == 1

    # Every other caller (agent and scoped captures, `lc review --check`) leaves it off: no scan.
    calls.clear()
    remote = _capture(billing, tmp_path / "state-off", "local")
    assert calls == []
    assert all(not packet["workspace_impact"] for packet in remote.captured_packets + remote.enriched_packets)


def test_a_hosted_server_never_receives_the_slice_and_no_scan_runs(
    projects: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted, "RemoteSession", _Remote)
    calls = _recording(monkeypatch)
    billing = projects / "billing"
    _write(billing, BILLING_CHANGED)
    remote = _capture(billing, tmp_path / "state", "hosted", with_workspace=True)
    assert calls == []
    assert remote.captured_packets and remote.captured_packets[0]["workspace_impact"] == {}

    # Even a packet that carries a slice reaches a hosted server without it.
    real = hosted.build_review_packet_with_blobs

    def carrying(*args: Any, **kwargs: Any) -> Any:
        build = real(*args, **kwargs)
        return replace(build, packet=replace(build.packet, workspace_impact={"repo": "billing", "reached": [{}]}))

    monkeypatch.setattr(hosted, "build_review_packet_with_blobs", carrying)
    remote = _capture(billing, tmp_path / "state-carrying", "hosted")
    assert [packet["workspace_impact"] for packet in remote.captured_packets] == [{}]


def test_lc_review_turns_the_slice_on_for_a_local_server_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.cli.commands import review as review_command

    seen: list[dict[str, Any]] = []

    def capture(*_args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        raise ValueError("stop after the capture call")

    monkeypatch.setattr(hosted, "capture_server_review", capture)
    options: dict[str, Any] = {
        "store_root": tmp_path,
        "session_id": None,
        "limit": 20,
        "no_provenance": False,
        "html_path": None,
        "effective_open": False,
        "as_json": True,
        "show_all": False,
        "no_color": True,
        "fell_back": False,
        "show_units": False,
        "mark_targets": (),
        "mark_state": "",
        "show_marks": False,
        "comment_body": None,
        "comment_target": None,
        "comment_kind": "",
        "reply_to": None,
        "show_comments": False,
        "since_my_review": False,
        "send_feedback": False,
        "finish_review": False,
        "reopen_review": False,
        "discard_review": False,
    }
    rng = SimpleNamespace(mode="working_tree")
    for hosted_server, no_impact in ((False, False), (False, True), (True, False)):
        config = SimpleNamespace(hosted=hosted_server, authenticated=True)
        with pytest.raises(click.ClickException, match="stop after the capture call"):
            review_command._review_server_capture(config, tmp_path, rng, no_impact=no_impact, **options)
    assert [kwargs["with_workspace"] for kwargs in seen] == [True, False, False]
