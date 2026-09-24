"""File-graph analytics over the authoritative Layer-3 server index.

These operations replace the public ``SemanticFileMemoryCapability`` path for
hosted/loopback execution.  They intentionally preserve the public graph result
shapes while deriving every repository-structure fact from the tenant's
revision-bound manifest + link layer.  No per-worktree file index is built.
"""

from __future__ import annotations

import math
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from .boundary import addressable_in
from .contracts import EdgeKind, IndexBackend, IndexLayerStatus, ManifestEntry

STRUCTURAL_GRAPH_KINDS = frozenset({"blast_radius", "dead_code", "cycles", "coupling", "centrality", "topology"})
SERVER_GRAPH_KINDS = STRUCTURAL_GRAPH_KINDS | {"pr_risk"}


def _is_test_path(path: str) -> bool:
    norm = path.replace("\\", "/").lower()
    name = norm.rsplit("/", 1)[-1]
    return (
        "/test" in f"/{norm}"
        or name.startswith("test_")
        or name.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
    )


def _is_entrypoint(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in {
        "__init__.py",
        "__main__.py",
        "main.py",
        "main.go",
        "main.rs",
        "index.ts",
        "index.tsx",
        "index.js",
        "index.jsx",
    }


def _complexity(data: bytes) -> int:
    """Small deterministic complexity proxy for ranking/risk, not a parser."""
    text = data.decode("utf-8", errors="replace")
    score = 1
    patterns = (
        r"\bif\b",
        r"\belif\b",
        r"\belse\b",
        r"\bfor\b",
        r"\bwhile\b",
        r"\bcase\b",
        r"\bcatch\b",
        r"\bexcept\b",
        r"&&",
        r"\|\|",
    )
    for pattern in patterns:
        score += len(re.findall(pattern, text))
    return score


def _line_count(data: bytes) -> int:
    return 0 if not data else data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Kosaraju SCC; stack-safe and deterministic."""
    nodes = sorted(graph)
    for deps in graph.values():
        nodes = sorted(set(nodes) | deps)
    seen: set[str] = set()
    order: list[str] = []
    for root in nodes:
        if root in seen:
            continue
        stack: list[tuple[str, bool]] = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                order.append(node)
                continue
            if node in seen:
                continue
            seen.add(node)
            stack.append((node, True))
            for nxt in sorted(graph.get(node, set()), reverse=True):
                if nxt not in seen:
                    stack.append((nxt, False))
    reverse: dict[str, set[str]] = {node: set() for node in nodes}
    for src, deps in graph.items():
        for dst in deps:
            reverse.setdefault(dst, set()).add(src)
    seen.clear()
    out: list[list[str]] = []
    for root in reversed(order):
        if root in seen:
            continue
        component: list[str] = []
        component_stack = [root]
        seen.add(root)
        while component_stack:
            node = component_stack.pop()
            component.append(node)
            for nxt in sorted(reverse.get(node, set()), reverse=True):
                if nxt not in seen:
                    seen.add(nxt)
                    component_stack.append(nxt)
        out.append(component)
    return out


class ServerGraphAnalytics:
    __slots__ = ("_backend", "_cap", "_org_id", "_repo_root", "_view_id")

    def __init__(
        self,
        backend: IndexBackend,
        *,
        org_id: str,
        view_id: str,
        content_size_cap: int,
        repo_root: str | Path | None,
    ) -> None:
        self._backend = backend
        self._cap = content_size_cap
        self._org_id = org_id
        self._view_id = view_id
        self._repo_root = Path(repo_root).resolve() if repo_root else None

    def run(
        self, kind: str, *, path: str | None = None, paths: list[str] | None = None, limit: int = 50
    ) -> tuple[dict[str, Any], IndexLayerStatus]:
        status = self._backend.links.refresh(self._org_id, self._view_id)
        if kind == "centrality":
            payload = self.centrality(limit=limit)
        elif kind == "blast_radius":
            if not path:
                raise ValueError("path is required for kind='blast_radius'")
            payload = self.blast_radius(path)
        elif kind == "dead_code":
            payload = self.dead_code(limit=limit)
        elif kind == "cycles":
            payload = self.cycles(limit=limit)
        elif kind == "coupling":
            payload = self.coupling(limit=limit)
        elif kind == "topology":
            payload = self.topology(limit=limit)
        elif kind == "pr_risk":
            targets = paths or ([path] if path else [])
            if not targets:
                raise ValueError("pr_risk requires 'paths' (or 'path') -- the changed files")
            payload = self.pr_risk(targets)
        else:
            raise ValueError(f"unsupported server graph kind: {kind}")
        payload["kind"] = kind
        payload["backend"] = "server_index"
        payload["view_revision"] = self._backend.views.get(self._org_id, self._view_id).view_revision
        return payload, status

    def _members(self) -> dict[str, ManifestEntry]:
        state = self._backend.views.get(self._org_id, self._view_id)
        membership = self._backend.views.membership(self._org_id, self._view_id)
        reachable = addressable_in(
            self._backend.provenance,
            state,
            tuple(sorted({entry.content_digest for entry in membership.values()})),
        )
        return {
            path: entry
            for path, entry in membership.items()
            if entry.size <= self._cap and entry.content_digest in reachable
        }

    def _import_graph(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        members = self._members()
        forward: dict[str, set[str]] = {path: set() for path in members}
        reverse: dict[str, set[str]] = {path: set() for path in members}
        for edge in self._backend.links.edges(self._org_id, self._view_id):
            if edge.kind is not EdgeKind.IMPORT or edge.src_path not in members or edge.dst_path not in members:
                continue
            if edge.src_path == edge.dst_path:
                continue
            forward[edge.src_path].add(edge.dst_path)
            reverse[edge.dst_path].add(edge.src_path)
        return forward, reverse

    def blast_radius(self, modified_path: str, *, max_transitive_depth: int = 3) -> dict[str, Any]:
        path = self._normalize_path(modified_path)
        _forward, reverse = self._import_graph()
        direct = sorted(reverse.get(path, set()))
        visited = set(direct)
        frontier = set(direct)
        transitive: list[str] = []
        for _ in range(max(0, max_transitive_depth - 1)):
            next_frontier: set[str] = set()
            for current in sorted(frontier):
                for importer in sorted(reverse.get(current, set())):
                    if importer in visited:
                        continue
                    visited.add(importer)
                    next_frontier.add(importer)
                    transitive.append(importer)
            frontier = next_frontier
            if not frontier:
                break
        affected_tests = sorted(path for path in visited if _is_test_path(path))
        total = len(direct) + len(transitive)
        risk = "low" if total == 0 else "medium" if total <= 3 else "high" if total <= 10 else "critical"
        return {
            "modified_file": path,
            "direct_importers": direct,
            "transitive_importers": transitive,
            "affected_tests": affected_tests,
            "risk_level": risk,
        }

    def dead_code(self, *, limit: int = 50) -> dict[str, Any]:
        members = self._members()
        _forward, reverse = self._import_graph()
        candidates: list[dict[str, Any]] = []
        for path, entry in members.items():
            if _is_test_path(path) or _is_entrypoint(path) or reverse.get(path):
                continue
            artifact = self._backend.analysis.get(self._org_id, entry.content_digest, entry.parser_profile)
            if artifact is None or not artifact.definitions:
                continue
            data = self._backend.content.get(self._org_id, entry.content_digest) or b""
            exports = sorted({definition.name for definition in artifact.definitions if definition.exported})
            candidates.append(
                {
                    "path": path,
                    "language": artifact.language,
                    "exports": exports[:20],
                    "complexity_score": _complexity(data),
                    "lines_total": _line_count(data),
                }
            )
        candidates.sort(key=lambda row: (-int(row["complexity_score"]), str(row["path"])))
        return {
            "analyzed_files": len(members),
            "dead_file_count": len(candidates),
            "dead_files": candidates[:limit],
            "truncated": len(candidates) > limit,
        }

    def cycles(self, *, limit: int = 50) -> dict[str, Any]:
        forward, _reverse = self._import_graph()
        cycles = [sorted(component) for component in _tarjan_scc(forward) if len(component) >= 2]
        cycles.sort(key=lambda cycle: (-len(cycle), cycle[0] if cycle else ""))
        return {
            "analyzed_files": len(forward),
            "cycle_count": len(cycles),
            "cycles": cycles[:limit],
            "truncated": len(cycles) > limit,
        }

    def coupling(self, *, limit: int = 50) -> dict[str, Any]:
        forward, reverse = self._import_graph()
        rows: list[dict[str, Any]] = []
        for path in sorted(forward):
            efferent = len(forward[path])
            afferent = len(reverse.get(path, set()))
            total = afferent + efferent
            if total == 0:
                continue
            rows.append(
                {
                    "path": path,
                    "afferent": afferent,
                    "efferent": efferent,
                    "total_coupling": total,
                    "instability": round(efferent / total, 4),
                }
            )
        rows.sort(key=lambda row: (-int(row["total_coupling"]), str(row["path"])))
        return {
            "analyzed_files": len(forward),
            "coupled_file_count": len(rows),
            "files": rows[:limit],
            "truncated": len(rows) > limit,
        }

    def topology(self, *, limit: int = 50) -> dict[str, Any]:
        forward, _reverse = self._import_graph()

        def module_of(path: str) -> str:
            norm = path.replace("\\", "/")
            return norm.rsplit("/", 1)[0] if "/" in norm else "."

        counts: dict[str, int] = defaultdict(int)
        efferent: dict[str, set[str]] = defaultdict(set)
        for path, deps in forward.items():
            module = module_of(path)
            counts[module] += 1
            for dep in deps:
                dep_module = module_of(dep)
                if dep_module != module:
                    efferent[module].add(dep_module)
        afferent: dict[str, set[str]] = defaultdict(set)
        for module, deps in efferent.items():
            for dep in deps:
                afferent[dep].add(module)
        modules: list[dict[str, Any]] = [
            {
                "module": module,
                "files": counts[module],
                "depends_on": sorted(efferent[module]),
                "efferent_modules": len(efferent[module]),
                "afferent_modules": len(afferent[module]),
            }
            for module in counts
        ]
        modules.sort(
            key=lambda row: (-(int(row["efferent_modules"]) + int(row["afferent_modules"])), str(row["module"]))
        )
        return {
            "analyzed_files": len(forward),
            "module_count": len(counts),
            "modules": modules[:limit],
            "hotspots": self.coupling(limit=limit)["files"][:10],
            "truncated": len(modules) > limit,
        }

    def centrality(self, *, limit: int = 50) -> dict[str, Any]:
        members = self._members()
        edges: list[tuple[str, str]] = []
        for edge in self._backend.links.edges(self._org_id, self._view_id):
            if edge.kind is not EdgeKind.CALL or edge.src_path not in members or edge.dst_path not in members:
                continue
            caller = f"{edge.src_path}:{edge.src_line}"
            edges.append((caller, edge.symbol))
        nodes = sorted({node for pair in edges for node in pair})
        incoming: dict[str, int] = defaultdict(int)
        outgoing: dict[str, int] = defaultdict(int)
        adjacency: dict[str, list[str]] = defaultdict(list)
        for caller, callee in edges:
            if not caller or not callee or caller == callee:
                continue
            outgoing[caller] += 1
            incoming[callee] += 1
            adjacency[caller].append(callee)
        if not nodes:
            return {"node_count": 0, "edge_count": 0, "ranking": [], "truncated": False}
        score = dict.fromkeys(nodes, 1.0 / len(nodes))
        damping = 0.85
        for _ in range(40):
            nxt = dict.fromkeys(nodes, (1.0 - damping) / len(nodes))
            dangling = sum(score[node] for node in nodes if not adjacency.get(node))
            if dangling:
                share = damping * dangling / len(nodes)
                for node in nodes:
                    nxt[node] += share
            for caller in nodes:
                callees = adjacency.get(caller, [])
                if not callees:
                    continue
                share = damping * score[caller] / len(callees)
                for callee in callees:
                    nxt[callee] += share
            if sum(abs(nxt[node] - score[node]) for node in nodes) < 1.0e-7:
                score = nxt
                break
            score = nxt
        norm = float(max(1, len(nodes) - 1))
        ranking: list[dict[str, Any]] = [
            {
                "symbol": node,
                "in_degree": incoming[node],
                "out_degree": outgoing[node],
                "degree": round((incoming[node] + outgoing[node]) / norm, 4),
                "in_degree_centrality": round(incoming[node] / norm, 4),
                "out_degree_centrality": round(outgoing[node] / norm, 4),
                "eigenvector": round(score[node], 6),
            }
            for node in nodes
        ]
        ranking.sort(
            key=lambda row: (
                -float(row["eigenvector"]),
                -(int(row["in_degree"]) + int(row["out_degree"])),
                str(row["symbol"]),
            )
        )
        return {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "ranking": ranking[:limit],
            "truncated": len(nodes) > limit,
        }

    def pr_risk(self, paths: list[str], *, window_days: int = 180) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for raw in paths:
            path = self._normalize_path(raw)
            impact = self.blast_radius(path)
            impacted = len(impact["direct_importers"]) + len(impact["transitive_importers"])
            affected_tests = list(impact["affected_tests"])
            member = self._members().get(path)
            data = self._backend.content.get(self._org_id, member.content_digest) if member else None
            complexity = _complexity(data or b"") if member else 0
            churn = self._git_churn(path, window_days=window_days)
            blast_factor = min(1.0, math.log2(impacted + 1) / 4.0) if impacted else 0.0
            complexity_factor = min(1.0, complexity / 20.0)
            churn_factor = float(churn["score"])
            test_gap = 0.0 if affected_tests else 1.0
            # Preserve the public heuristic's intent: blast + churn dominate,
            # with test gap and complexity as supporting factors.
            score = round(
                min(1.0, 0.35 * blast_factor + 0.25 * churn_factor + 0.25 * test_gap + 0.15 * complexity_factor), 4
            )
            tier = "low" if score < 0.25 else "medium" if score < 0.5 else "high" if score < 0.75 else "critical"
            rows.append(
                {
                    "path": raw,
                    "score": score,
                    "tier": tier,
                    "factors": {
                        "blast_radius": {
                            "impacted_files": impacted,
                            "affected_tests": affected_tests,
                            "factor": round(blast_factor, 4),
                        },
                        "churn": {
                            "commit_count": churn["commit_count"],
                            "factor": round(churn_factor, 4),
                            "available": churn["available"],
                        },
                        "test_gap": {"missing_tests": not affected_tests, "factor": test_gap},
                        "complexity": {"score": complexity, "factor": round(complexity_factor, 4)},
                    },
                    "risk_level": impact["risk_level"],
                }
            )
        overall = round(max((float(row["score"]) for row in rows), default=0.0), 4)
        tier = "low" if overall < 0.25 else "medium" if overall < 0.5 else "high" if overall < 0.75 else "critical"
        return {
            "overall_score": overall,
            "overall_tier": tier,
            "file_count": len(rows),
            "files": sorted(rows, key=lambda row: -float(row["score"])),
            "weights": {"blast_radius": 0.35, "churn": 0.25, "test_gap": 0.25, "complexity": 0.15},
            "heuristic": True,
        }

    def _normalize_path(self, raw: str) -> str:
        path = raw.replace("\\", "/")
        if self._repo_root is not None:
            candidate = Path(raw)
            if candidate.is_absolute():
                try:
                    path = candidate.resolve().relative_to(self._repo_root).as_posix()
                except (OSError, ValueError):
                    path = candidate.as_posix()
        return path.removeprefix("./")

    def _git_churn(self, path: str, *, window_days: int) -> dict[str, Any]:
        if self._repo_root is None or not (self._repo_root / ".git").exists():
            return {"commit_count": 0, "score": 0.0, "available": False}
        try:
            result = subprocess.run(
                ["git", "log", f"--since={max(1, window_days)} days ago", "--format=%H", "--", path],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {"commit_count": 0, "score": 0.0, "available": False}
        if result.returncode != 0:
            return {"commit_count": 0, "score": 0.0, "available": False}
        count = len([line for line in result.stdout.splitlines() if line.strip()])
        return {"commit_count": count, "score": min(1.0, count / 20.0), "available": True}
