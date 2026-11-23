from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from lemoncrow.gateway.cli import cli

ROOT = Path(__file__).resolve().parents[1]
CURRENT_DOCS = [
    ROOT / "README.md",
    ROOT / "docs" / "README.md",
    ROOT / "docs" / "architecture" / "runtime-lite.md",
    ROOT / "docs" / "architecture" / "oss-private-code-intelligence.md",
    ROOT / "docs" / "reference" / "architecture.md",
    ROOT / "docs" / "reference" / "cli.md",
    ROOT / "docs" / "reference" / "open-source-boundary.md",
    ROOT / "docs" / "setup" / "installation.md",
    ROOT / "docs" / "setup" / "privacy.md",
    ROOT / "docs" / "hosts" / "all-agent-clis.md",
    ROOT / "docs" / "hosts" / "host-capability-matrix.md",
    ROOT / "docs" / "hosts" / "pi-install.md",
    ROOT / "docs" / "legal" / "licensing.md",
]


def test_public_code_help_is_hosted_and_has_no_local_pro_mode() -> None:
    result = CliRunner().invoke(cli, ["runtime", "code", "--help"])
    assert result.exit_code == 0, result.output
    assert "Hosted coding agent" in result.output
    for hidden in ("--internal-private", "--provider", "--budget", "--max-cost", "--engine"):
        assert hidden not in result.output
    assert not any(line.lstrip().startswith("--pro ") for line in result.output.splitlines())


def test_legacy_pro_flag_is_removed_not_redirected() -> None:
    result = CliRunner().invoke(cli, ["runtime", "code", "--pro"])
    assert result.exit_code == 2
    assert "No such option '--pro'" in result.output
    assert "commercial plan" not in result.output


def test_pi_host_lifecycle_is_public() -> None:
    result = CliRunner().invoke(cli, ["host", "--help"])
    assert result.exit_code == 0, result.output
    for action in ("status", "install", "update", "build", "remove"):
        assert action in result.output
    status_help = CliRunner().invoke(cli, ["host", "status", "--help"])
    assert status_help.exit_code == 0, status_help.output
    assert "lemoncode" not in status_help.output
    assert "pi" in status_help.output


def test_hosted_runtime_discovery_does_not_overclaim_unwired_features() -> None:
    import importlib.util

    source = ROOT / "server" / "src" / "lemoncrow_server_core" / "runtime_product_surface.py"
    spec = importlib.util.spec_from_file_location("_runtime_product_surface_contract", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    capabilities = module.hosted_runtime_capabilities()
    for present in (
        "remote_code_search",
        "semantic_hybrid_search",
        "server_graph",
        "revision_bound_views",
        "blob_source_sync",
        "tenant_scoped_auth",
    ):
        assert capabilities[present] is True
    for gap in (
        "cross_workspace_routing",
        "owned_loop",
        "provider_routing",
        "budget_spend_controls",
        "prompt_cache_orchestration",
    ):
        assert capabilities[gap] is False


def test_public_lite_index_has_one_canonical_command() -> None:
    public_index = CliRunner().invoke(cli, ["runtime", "index", "--help"])
    assert public_index.exit_code == 0, public_index.output
    assert "Workspaces Lite" in public_index.output

    legacy_code_index = CliRunner().invoke(cli, ["runtime", "code", "index"])
    assert legacy_code_index.exit_code == 1
    assert "internal private-development tooling" in legacy_code_index.output
