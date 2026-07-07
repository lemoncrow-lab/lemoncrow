from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
MODES_DIR = Path("integrations/agents")

HOST_ROLE_IDS = ("code", "explore", "review", "plan", "execute", "research", "solve", "auto", "bare", "general")
SURFACED_ROLE_IDS = (
    "code",
    "explore",
    "execute",
    "plan",
    "research",
    "review",
    "solve",
    "auto",
    "bare",
    "general",
)
# Roles installed by default across every host (Claude/Codex/OpenCode global and
# workspace installs). HOST_ROLE_IDS/SURFACED_ROLE_IDS above remain the full
# catalog of what CAN be installed (unconditionally built by
# build_default_registry) -- an on-demand install feature will let callers
# request a superset of this set (e.g. DEFAULT_ROLE_IDS + ("explore",)).
DEFAULT_ROLE_IDS: tuple[str, ...] = ("code",)
DEFAULT_OWNED_MODEL = "claude-opus-4.8"
READONLY_OWNED_MODEL = "claude-sonnet-4.6"


@dataclass(frozen=True)
class ModeDoc:
    name: str
    skill_description: str
    agent_description: str
    body: str
    source_path: Path


@dataclass(frozen=True)
class ToolPolicy:
    policy_id: str
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    denied_actions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "allowed_tools": list(self.allowed_tools),
            "denied_tools": list(self.denied_tools),
            "denied_actions": list(self.denied_actions),
        }


@dataclass(frozen=True)
class ReviewContract:
    require_first_hand_evidence: bool = True
    verdict_format: str = "json-block"
    default_verdict: str = "NEEDS_FIX"
    checklist_fields: tuple[str, ...] = ("verdict", "checklist", "missing")

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_first_hand_evidence": self.require_first_hand_evidence,
            "verdict_format": self.verdict_format,
            "default_verdict": self.default_verdict,
            "checklist_fields": list(self.checklist_fields),
        }


@dataclass(frozen=True)
class HostProjection:
    surface: str
    host: str
    output_name: str
    frontmatter: tuple[tuple[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "host": self.host,
            "output_name": self.output_name,
            "frontmatter": [
                {"key": key, "value": list(value) if isinstance(value, tuple) else value}
                for key, value in self.frontmatter
            ],
        }


@dataclass(frozen=True)
class PromptDefinition:
    prompt_id: str
    body: str = ""
    source_path: Path | None = None

    def render(self, repo_root: Path | None = None) -> str:
        if self.body:
            return self.body
        if self.source_path is None:
            return ""
        source = _resolve_repo_root(repo_root) / self.source_path
        if not source.exists():
            return ""
        if source_path_looks_like_mode_doc(self.source_path):
            return parse_frontmatter(source.read_text(encoding="utf-8"))[1].rstrip() + "\n"
        return markdown_body(source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "body": self.body,
            "source_path": self.source_path.as_posix() if self.source_path is not None else None,
        }


@dataclass(frozen=True)
class DefaultRole:
    role_id: str
    name: str
    skill_description: str
    agent_description: str
    prompt_source: Path | None
    prompt_body: str
    tool_policy: ToolPolicy
    workflow_usage: tuple[str, ...]
    model_default: str
    effort_default: str
    read_mode_hint: str
    host_projections: tuple[HostProjection, ...] = ()
    review_contract: ReviewContract | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "name": self.name,
            "skill_description": self.skill_description,
            "agent_description": self.agent_description,
            "prompt_source": self.prompt_source.as_posix() if self.prompt_source is not None else None,
            "prompt_body": self.prompt_body,
            "tool_policy": self.tool_policy.to_dict(),
            "workflow_usage": list(self.workflow_usage),
            "model_default": self.model_default,
            "effort_default": self.effort_default,
            "read_mode_hint": self.read_mode_hint,
            "host_projections": [projection.to_dict() for projection in self.host_projections],
            "review_contract": self.review_contract.to_dict() if self.review_contract is not None else None,
        }


@dataclass(frozen=True)
class DefaultWorkflowStep:
    step_id: str
    role_id: str
    phase_prompt_id: str
    effort: str
    read_mode_hint: str
    fork_from: str = ""
    context_mode: str = "inherit"
    requires_plan_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "role_id": self.role_id,
            "phase_prompt_id": self.phase_prompt_id,
            "effort": self.effort,
            "read_mode_hint": self.read_mode_hint,
            "fork_from": self.fork_from,
            "context_mode": self.context_mode,
            "requires_plan_review": self.requires_plan_review,
        }


@dataclass(frozen=True)
class DefaultWorkflow:
    workflow_id: str
    stem_prompt_id: str
    steps: tuple[DefaultWorkflowStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "stem_prompt_id": self.stem_prompt_id,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class BenchmarkProfile:
    profile_id: str
    role_id: str
    workflow_id: str
    retry_limit: int
    command_rules: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "role_id": self.role_id,
            "workflow_id": self.workflow_id,
            "retry_limit": self.retry_limit,
            "command_rules": list(self.command_rules),
        }


@dataclass(frozen=True)
class McpTemplate:
    template_id: str
    host: str
    command: str
    args: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "host": self.host,
            "command": self.command,
            "args": list(self.args),
        }


@dataclass(frozen=True)
class DefaultRegistry:
    roles: dict[str, DefaultRole]
    prompts: dict[str, PromptDefinition]
    workflows: dict[str, DefaultWorkflow]
    benchmark_profiles: dict[str, BenchmarkProfile]
    mcp_templates: dict[str, McpTemplate]

    def surfaced_role_ids(self, surface: str) -> tuple[str, ...]:
        return tuple(
            role_id
            for role_id, role in self.roles.items()
            if any(projection.surface == surface for projection in role.host_projections)
        )

    def projection(self, role_id: str, surface: str) -> HostProjection:
        role = self.roles[role_id]
        for projection in role.host_projections:
            if projection.surface == surface:
                return projection
        raise KeyError(f"missing projection: {role_id}:{surface}")

    def render_prompt(self, role_id: str, repo_root: Path | None = None) -> str:
        role = self.roles[role_id]
        if role.prompt_body:
            return role.prompt_body
        if role.prompt_source is None:
            return ""
        source = _resolve_repo_root(repo_root) / role.prompt_source
        if not source.exists():
            return ""
        return parse_frontmatter(source.read_text(encoding="utf-8"))[1].rstrip() + "\n"

    def render_named_prompt(self, prompt_id: str, repo_root: Path | None = None) -> str:
        return self.prompts[prompt_id].render(repo_root)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "roles": {role_id: role.to_dict() for role_id, role in self.roles.items()},
            "prompts": {prompt_id: prompt.to_dict() for prompt_id, prompt in self.prompts.items()},
            "workflows": {workflow_id: workflow.to_dict() for workflow_id, workflow in self.workflows.items()},
            "benchmark_profiles": {
                profile_id: profile.to_dict() for profile_id, profile in self.benchmark_profiles.items()
            },
            "mcp_templates": {template_id: template.to_dict() for template_id, template in self.mcp_templates.items()},
        }


def _resolve_repo_root(repo_root: Path | None) -> Path:
    return REPO_ROOT if repo_root is None else repo_root


def source_path_looks_like_mode_doc(path: Path) -> bool:
    return path.parts[:2] == ("integrations", "agents")


def markdown_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines).rstrip() + "\n"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError("mode doc is missing frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("mode doc frontmatter is not terminated")
    meta: dict[str, str] = {}
    for raw_line in text[4:end].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"invalid frontmatter line: {raw_line}")
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    body = text[end + len("\n---\n") :].lstrip()
    return meta, body


def load_mode_docs(repo_root: Path | None = None, *, strict: bool = True) -> dict[str, ModeDoc]:
    root = _resolve_repo_root(repo_root)
    docs: dict[str, ModeDoc] = {}

    def _load_from_dir(d: Path, origin: Path) -> None:
        if not d.exists():
            return
        for path in sorted(d.glob("*.md")):
            try:
                meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
                name = meta["mode"]
                if name not in docs:
                    docs[name] = ModeDoc(
                        name=name,
                        skill_description=meta["skill_description"],
                        agent_description=meta["agent_description"],
                        body=body.rstrip() + "\n",
                        source_path=path.relative_to(origin) if path.is_relative_to(origin) else path,
                    )
            except (ValueError, KeyError):
                if strict:
                    raise
                continue

    # 1. Try repository root first (the one passed in)
    _load_from_dir(root / MODES_DIR, root)

    # 2. Fallback to REPO_ROOT for missing roles (useful in source checkout)
    if not all(role_id in docs for role_id in HOST_ROLE_IDS) and root != REPO_ROOT:
        _load_from_dir(REPO_ROOT / MODES_DIR, REPO_ROOT)

    # 3. Fallback to packaged assets for missing roles (useful in installed package)
    if not all(role_id in docs for role_id in HOST_ROLE_IDS):
        try:
            packaged = cast(Any, resources.files("atelier")).joinpath("integrations", "agents")
            if packaged.is_dir():
                for entry in sorted(packaged.glob("*.md"), key=lambda x: x.name):
                    meta, body = parse_frontmatter(entry.read_text(encoding="utf-8"))
                    name = meta["mode"]
                    if name not in docs:
                        docs[name] = ModeDoc(
                            name=name,
                            skill_description=meta["skill_description"],
                            agent_description=meta["agent_description"],
                            body=body.rstrip() + "\n",
                            source_path=Path("integrations/agents") / entry.name,
                        )
        except Exception:
            if strict:
                raise

    return docs


def _projection(surface: str, host: str, output_name: str, items: Iterable[tuple[str, Any]]) -> HostProjection:
    return HostProjection(surface=surface, host=host, output_name=output_name, frontmatter=tuple(items))


def _role_projections() -> dict[str, tuple[HostProjection, ...]]:
    projections: dict[str, tuple[HostProjection, ...]] = {}
    for role_id in SURFACED_ROLE_IDS:
        opencode_name = "atelier" if role_id == "code" else role_id
        antigravity_name = "atelier-code" if role_id == "code" else f"atelier-{role_id}"
        copilot_name = f"atelier.{role_id}"
        projections[role_id] = (
            HostProjection(surface="shared_skill", host="shared", output_name=role_id),
            _projection("copilot_agent", "copilot", copilot_name, ()),
            _projection("claude_agent", "claude", role_id, CLAUDE_STABLE_FRONTMATTER[role_id]),
            _projection("opencode_agent", "opencode", opencode_name, OPENCODE_FRONTMATTER[role_id]),
            _projection(
                "antigravity_agent",
                "antigravity",
                antigravity_name,
                ANTIGRAVITY_FRONTMATTER[role_id],
            ),
        )
    return projections


def _tool_policies() -> dict[str, ToolPolicy]:
    return {
        "code": ToolPolicy(policy_id="code", allowed_tools=("*",)),
        "general": ToolPolicy(policy_id="general", allowed_tools=("*",)),
        # Non-orchestrating roles also deny workflow/schedule: the Workflow and
        # ScheduleWakeup tool schemas cost ~4-5k tokens on EVERY request the
        # subagent makes, and a reviewer/explorer/executor invoking multi-agent
        # orchestration is always a mis-pick. Only code/general/auto keep them.
        "explore": ToolPolicy(
            policy_id="explore",
            allowed_tools=("read", "grep", "search", "node", "explore"),
            denied_actions=("edit", "write", "delete", "agent-spawn", "workflow", "schedule"),
        ),
        "plan": ToolPolicy(
            policy_id="plan",
            allowed_tools=(
                "read",
                "grep",
                "search",
                "symbols",
                "node",
                "explore",
                "web_fetch",
            ),
            denied_actions=("edit", "write", "delete", "workflow", "schedule"),
        ),
        "execute": ToolPolicy(
            policy_id="execute",
            allowed_tools=("*",),
            denied_actions=("agent-spawn", "workflow", "schedule"),
        ),
        "review": ToolPolicy(
            policy_id="review",
            allowed_tools=(
                "read",
                "grep",
                "search",
                "node",
                "explore",
                "verify",
            ),
            denied_actions=("edit", "write", "delete", "workflow", "schedule"),
        ),
        "research": ToolPolicy(
            policy_id="research",
            allowed_tools=("web_fetch", "web_search", "read", "search"),
            denied_actions=("edit", "write", "delete", "workflow", "schedule"),
        ),
        "solve": ToolPolicy(
            policy_id="solve",
            allowed_tools=("*",),
            denied_actions=("agent-spawn", "workflow", "schedule"),
        ),
        # auto denies only what its unattended contract requires (plan gate,
        # questions). Host surfaces shared with vanilla Claude (Agent, Skill,
        # ReportFindings, ToolSearch) stay ENABLED: Atelier does not replace
        # them, and stripping surfaces the baseline keeps would be benchmark
        # gaming, not product behavior.
        "auto": ToolPolicy(
            policy_id="auto",
            allowed_tools=("*",),
            denied_actions=("plan-gate", "ask-user"),
        ),
        "bare": ToolPolicy(
            policy_id="bare",
            allowed_tools=("*",),
            denied_actions=("workflow", "schedule"),
        ),
    }


def _prompt_definitions() -> dict[str, PromptDefinition]:
    return {
        "owned-stem-system": PromptDefinition(
            prompt_id="owned-stem-system",
            body=(
                "You are operating inside Atelier's owned execution runtime. Keep this system prompt stable "
                "across every phase so provider prompt caches stay warm. Treat the current phase prompt as "
                "the only active contract. Preserve first-hand evidence. Do not broaden the task.\n"
                "Read mechanics, shared across phases: compact and outline reads are projections, not literal "
                "source; if you need literal body text, line-sensitive context, or exact snippets, reread with "
                "full=true or an exact range. Capture include_meta=true when a later edit will target compact "
                "text so the projection metadata and mapping survive the handoff; compact projection edits are "
                "only valid for exact spans carried by that mapping, and if a projection edit returns retry_with, "
                "follow that exact reread instead of approximating transformed spans.\n"
                "Confirmation policy, shared across phases: proceed without confirmation only for local, reversible "
                "reads, edits, and tests; before destructive, hard-to-reverse, shared-state, or external "
                "side-effect actions, get user confirmation unless durable repo instructions already authorize "
                "that exact class of action."
            ),
        ),
        "owned-explore-phase": PromptDefinition(
            prompt_id="owned-explore-phase",
            body=(
                "=== PIVOT: YOU ARE NOW IN THE EXPLORE PHASE ===\n"
                "Read only. Do not plan. Do not edit. Use prior context first, then ask targeted questions "
                "of the code only where facts are missing. Prefer compact or outline reads for discovery. Do "
                "not re-read the same file through the same tool. Output only the facts, constraints, "
                "unknowns, and proving checks needed for this task."
            ),
        ),
        "owned-plan-phase": PromptDefinition(
            prompt_id="owned-plan-phase",
            body=(
                "=== PIVOT: YOU ARE NOW IN THE PLAN PHASE ===\n"
                "Do not edit. Produce an execution plan another agent can run without guessing, from "
                "verified facts only. Include: a short Name; Why the change is needed; the exact Files to "
                "create or modify; ordered Steps that each name concrete identifiers, reuse existing "
                "utilities, and flag risky or shared-surface changes; a final Verify step with exact "
                "build/test commands; and Risks plus open questions. Order steps so none depends on a "
                "later step. Use concrete verbs (add/replace/extract/delete/rename), not vague ones "
                "(update/handle/improve), and drop anything the task did not ask for. Reread once and fix "
                "ungrounded references, bad ordering, bundled steps, and a Files list that does not match "
                "the steps."
            ),
        ),
        "owned-critique-phase": PromptDefinition(
            prompt_id="owned-critique-phase",
            body=(
                "=== PIVOT: YOU ARE NOW IN THE PLAN CRITIQUE PHASE ===\n"
                "Do not edit. Attack the plan with fresh, skeptical eyes — you did not write it. Look for: "
                "missing requirements from the request; steps that depend on a later step's output; "
                "ungrounded file, function, or utility names that may not exist; destructive or "
                "irreversible operations with no mitigation; vague steps that cannot be executed without "
                "guessing; scope creep beyond the request; significant changes with no verification; and "
                "design choices that will cause pain later (say why). If the plan is sound, say exactly "
                "why. Otherwise list only actionable fixes."
            ),
        ),
        "owned-refine-plan-phase": PromptDefinition(
            prompt_id="owned-refine-plan-phase",
            body=(
                "=== PIVOT: YOU ARE NOW IN THE PLAN REFINE PHASE ===\n"
                "Do not edit. Produce the complete final plan, not a diff. Address every critique item or "
                "state the evidence that makes it inapplicable. Keep the file list exact and the verification "
                "commands runnable."
            ),
        ),
        "owned-execute-phase": PromptDefinition(
            prompt_id="owned-execute-phase",
            body=(
                "=== PIVOT: YOU ARE NOW IN THE EXECUTE PHASE ===\n"
                "Execute the approved plan sequentially. Read exact content before editing. Change only files "
                "named by the plan unless direct evidence proves the plan missed a required target. After each "
                "significant change, run the nearest useful check. Stop after self-verification."
            ),
        ),
        "owned-review-phase": PromptDefinition(
            prompt_id="owned-review-phase",
            body=(
                "=== PIVOT: YOU ARE NOW IN THE REVIEW PHASE ===\n"
                "Do not edit. Do not trust the implementer's summary. Inspect the filesystem and run direct "
                "checks. Decide whether every requested deliverable is satisfied. If evidence is missing or "
                "ambiguous, use NEEDS_FIX. End with exactly one JSON verdict block with keys verdict, "
                "checklist, and missing."
            ),
        ),
        "owned-fix-phase": PromptDefinition(
            prompt_id="owned-fix-phase",
            body=(
                "=== PIVOT: YOU ARE NOW IN THE FIX PHASE ===\n"
                "The review evidence is the punch list. Fix only cited gaps. Do not restart from scratch unless "
                "the approach is proven wrong. Rerun the checks tied to each gap, then stop for review."
            ),
        ),
    }


def _default_workflows() -> dict[str, DefaultWorkflow]:
    return {
        "owned-execute-review-loop": DefaultWorkflow(
            workflow_id="owned-execute-review-loop",
            stem_prompt_id="owned-stem-system",
            steps=(
                DefaultWorkflowStep(
                    step_id="explore",
                    role_id="explore",
                    phase_prompt_id="owned-explore-phase",
                    effort="adaptive",
                    read_mode_hint="compact",
                ),
                DefaultWorkflowStep(
                    step_id="plan",
                    role_id="plan",
                    phase_prompt_id="owned-plan-phase",
                    effort="medium",
                    read_mode_hint="compact",
                    fork_from="explore",
                ),
                DefaultWorkflowStep(
                    step_id="critique",
                    role_id="review",
                    phase_prompt_id="owned-critique-phase",
                    effort="medium",
                    read_mode_hint="compact",
                    fork_from="plan",
                ),
                DefaultWorkflowStep(
                    step_id="refine",
                    role_id="plan",
                    phase_prompt_id="owned-refine-plan-phase",
                    effort="medium",
                    read_mode_hint="compact",
                    fork_from="critique",
                ),
                DefaultWorkflowStep(
                    step_id="execute",
                    role_id="execute",
                    phase_prompt_id="owned-execute-phase",
                    effort="high",
                    read_mode_hint="exact",
                    fork_from="refine",
                    requires_plan_review=True,
                ),
                DefaultWorkflowStep(
                    step_id="review",
                    role_id="review",
                    phase_prompt_id="owned-review-phase",
                    effort="medium",
                    read_mode_hint="exact",
                    fork_from="refine",
                ),
                DefaultWorkflowStep(
                    step_id="fix",
                    role_id="execute",
                    phase_prompt_id="owned-fix-phase",
                    effort="medium",
                    read_mode_hint="exact",
                    fork_from="review",
                ),
            ),
        ),
        "owned-benchmark-solver": DefaultWorkflow(
            workflow_id="owned-benchmark-solver",
            stem_prompt_id="owned-stem-system",
            steps=(
                DefaultWorkflowStep(
                    step_id="explore",
                    role_id="explore",
                    phase_prompt_id="owned-explore-phase",
                    effort="adaptive",
                    read_mode_hint="compact",
                ),
                DefaultWorkflowStep(
                    step_id="plan",
                    role_id="plan",
                    phase_prompt_id="owned-plan-phase",
                    effort="medium",
                    read_mode_hint="compact",
                    fork_from="explore",
                ),
                DefaultWorkflowStep(
                    step_id="critique",
                    role_id="review",
                    phase_prompt_id="owned-critique-phase",
                    effort="medium",
                    read_mode_hint="compact",
                    fork_from="plan",
                ),
                DefaultWorkflowStep(
                    step_id="refine",
                    role_id="plan",
                    phase_prompt_id="owned-refine-plan-phase",
                    effort="medium",
                    read_mode_hint="compact",
                    fork_from="critique",
                ),
                DefaultWorkflowStep(
                    step_id="execute",
                    role_id="solve",
                    phase_prompt_id="owned-execute-phase",
                    effort="high",
                    read_mode_hint="exact",
                    fork_from="refine",
                ),
                DefaultWorkflowStep(
                    step_id="review",
                    role_id="review",
                    phase_prompt_id="owned-review-phase",
                    effort="medium",
                    read_mode_hint="exact",
                    fork_from="refine",
                ),
            ),
        ),
    }


def _benchmark_profiles() -> dict[str, BenchmarkProfile]:
    return {
        "terminalbench-owned-solver": BenchmarkProfile(
            profile_id="terminalbench-owned-solver",
            role_id="solve",
            workflow_id="owned-benchmark-solver",
            retry_limit=2,
            command_rules=(
                "Treat the benchmark task and provided workspace as ground truth; run non-interactively without waiting for user input.",
                "The benchmark environment is isolated and disposable, but do not assume that outside this profile.",
                "Authorized security and CTF tasks may require the requested payload, bypass, or exploit artifact.",
                "Do not inspect hidden evaluator, harness, expected-output, or test files; use only the task, workspace, exposed checks, and returned feedback.",
                "The canonical grader decides acceptance; optimize for the requested artifact and exposed verification signal.",
                "Install dependencies only when the task or failing check requires them.",
                "Do not hide stderr on install, build, or probe commands.",
                "Never mutate the benchmark harness directory unless the task explicitly names it.",
                "Use a generator script for large artifacts instead of pasting them inline.",
                "Remove scratch files, logs, binaries, and caches before stopping unless the task requests them.",
                "Commit to an artifact early, run the closest check, and iterate against the delta.",
                "Do not repeat a failed command verbatim; change the input, scope, timeout, or approach first.",
            ),
        )
    }


def _mcp_templates() -> dict[str, McpTemplate]:
    return {
        "claude-default": McpTemplate(
            template_id="claude-default",
            host="claude",
            command="atelier",
            args=("mcp", "--host", "claude"),
        ),
        "codex-default": McpTemplate(
            template_id="codex-default",
            host="codex",
            command="atelier",
            args=("mcp", "--host", "codex"),
        ),
        "antigravity-default": McpTemplate(
            template_id="antigravity-default",
            host="antigravity",
            command="atelier",
            args=("mcp", "--host", "antigravity"),
        ),
    }


def build_default_registry(repo_root: Path | None = None) -> DefaultRegistry:
    mode_docs = load_mode_docs(repo_root)
    projections = _role_projections()
    policies = _tool_policies()
    roles: dict[str, DefaultRole] = {}

    for role_id in HOST_ROLE_IDS:
        mode = mode_docs[role_id]
        roles[role_id] = DefaultRole(
            role_id=role_id,
            name=role_id.replace("-", " ").title(),
            skill_description=mode.skill_description,
            agent_description=mode.agent_description,
            prompt_source=Path("integrations/agents") / f"{role_id}.md",
            prompt_body="",
            tool_policy=policies[role_id],
            workflow_usage=_workflow_usage(role_id),
            model_default=_role_default_model(role_id),
            effort_default=_role_effort(role_id),
            read_mode_hint=_role_read_hint(role_id),
            host_projections=projections.get(role_id, ()),
            review_contract=ReviewContract() if role_id == "review" else None,
        )

    return DefaultRegistry(
        roles=roles,
        prompts=_prompt_definitions(),
        workflows=_default_workflows(),
        benchmark_profiles=_benchmark_profiles(),
        mcp_templates=_mcp_templates(),
    )


def _workflow_usage(role_id: str) -> tuple[str, ...]:
    usage = {
        "code": ("owned-execute-review-loop",),
        "explore": ("owned-execute-review-loop", "owned-benchmark-solver"),
        "review": ("owned-execute-review-loop", "owned-benchmark-solver"),
        "plan": ("owned-execute-review-loop", "owned-benchmark-solver"),
        "execute": ("owned-execute-review-loop",),
        "research": (),
        "solve": ("owned-benchmark-solver",),
        "auto": (),
        "bare": (),
        "general": ("owned-execute-review-loop", "owned-benchmark-solver"),
    }
    return usage[role_id]


def _role_effort(role_id: str) -> str:
    return {
        "code": "high",
        "general": "medium",
        "explore": "adaptive",
        "plan": "medium",
        "execute": "high",
        "review": "medium",
        "research": "medium",
        "solve": "high",
        "auto": "high",
        "bare": "high",
    }[role_id]


def _role_default_model(role_id: str) -> str:
    return {
        "code": DEFAULT_OWNED_MODEL,
        "general": DEFAULT_OWNED_MODEL,
        "explore": READONLY_OWNED_MODEL,
        "plan": READONLY_OWNED_MODEL,
        "execute": DEFAULT_OWNED_MODEL,
        "review": READONLY_OWNED_MODEL,
        "research": READONLY_OWNED_MODEL,
        "solve": DEFAULT_OWNED_MODEL,
        "auto": DEFAULT_OWNED_MODEL,
        "bare": DEFAULT_OWNED_MODEL,
    }[role_id]


def _role_read_hint(role_id: str) -> str:
    return {
        "code": "exact",
        "general": "exact",
        "explore": "compact",
        "plan": "compact",
        "execute": "exact",
        "review": "exact",
        "research": "compact",
        "solve": "exact",
        "auto": "exact",
        "bare": "exact",
    }[role_id]


# Single host-neutral source: each role's ``tool_policy.denied_actions`` declares
# the intent (deny mutation, deny sub-agent spawn). Per-host renderers below
# translate that intent into each host's native gating mechanism. Editing the
# policy in ``_tool_policies`` is the only place a restriction is declared.

# Native host tools that have Atelier MCP equivalents. Disallowing them forces
# MCP-grounded file I/O and search across every generated Claude agent while
# keeping the frontmatter small: ``tools: ["*"]`` already grants every MCP tool,
# so an explicit allow-list is unnecessary. ``MultiEdit``/``NotebookEdit`` are
# intentionally omitted (``Edit``/``Write`` plus ``mcp__atelier__edit`` cover the
# real write paths) so the list stays short. Native ``Bash`` is denied so all
# shell work flows through ``mcp__atelier__bash`` (the supervised path) instead
# of native heredocs; without this, agents bypass every Atelier file/search tool
# by doing all I/O through ``Bash``. ``mcp__atelier__bash`` is never denied:
# read-only roles still need shell to run checks and probes (``git diff``,
# ``pytest``, ``ls``), and read-only is enforced by denying the write tools, not
# by removing shell.
#
# Native ``WebFetch`` is denied so URL retrieval flows through the supervised
# ``mcp__atelier__web_fetch`` (telemetry, redaction, savings) instead of the
# always-present native fetch. Native ``WebSearch`` stays enabled because
# Atelier has no web-search equivalent -- denying it would leave research with
# no way to discover sources. (Hermetic benchmarks deny it per-run via the
# claude CLI ``--disallowedTools`` flag, not here -- that's a contamination
# concern, not a product-persona behavior.)
_NATIVE_MCP_OVERRIDDEN: list[str] = [
    "Read",
    "Edit",
    "Write",
    "Grep",
    "Glob",
    "Bash",
    "WebFetch",
]


def _denies_mutation(policy: ToolPolicy) -> bool:
    return bool({"edit", "write", "delete"} & set(policy.denied_actions))


def _denies_spawn(policy: ToolPolicy) -> bool:
    return "agent-spawn" in policy.denied_actions


def _denies_plan_gate(policy: ToolPolicy) -> bool:
    return "plan-gate" in policy.denied_actions


def _denies_ask(policy: ToolPolicy) -> bool:
    return "ask-user" in policy.denied_actions


def _denies_workflow(policy: ToolPolicy) -> bool:
    return "workflow" in policy.denied_actions


def _denies_schedule(policy: ToolPolicy) -> bool:
    return "schedule" in policy.denied_actions


def _claude_disallowed_tools(policy: ToolPolicy) -> list[str]:
    """Render a Claude ``disallowedTools`` deny-list from the host-neutral policy.

    Every host-projected role forces MCP file I/O (native
    read/edit/write/grep/glob/webfetch are denied so the Atelier equivalents are
    used). Read-only roles additionally
    lose sub-agent spawning and the MCP write path. Shell is never denied.
    """
    denied = list(_NATIVE_MCP_OVERRIDDEN)
    if _denies_spawn(policy):
        denied.append("Agent")
    if _denies_mutation(policy):
        # Both install shapes: bare user-scope server (install_claude.sh) and
        # marketplace plugin namespacing — the deny must hold under either.
        denied.extend(("mcp__atelier__edit", "mcp__plugin_atelier_atelier__edit"))
    if _denies_plan_gate(policy):
        # Deny both: ExitPlanMode alone still lets the agent EnterPlanMode and
        # plan instead of execute (EnterPlanMode is a real tool as of claude 2.1).
        denied.extend(("EnterPlanMode", "ExitPlanMode"))
    if _denies_ask(policy):
        denied.append("AskUserQuestion")
    if _denies_workflow(policy):
        denied.append("Workflow")
    if _denies_schedule(policy):
        denied.append("ScheduleWakeup")
    return denied


_CLAUDE_AGENT_COLORS: dict[str, str] = {
    "code": "purple",
    "explore": "blue",
    "review": "yellow",
    "plan": "cyan",
    "execute": "purple",
    "research": "green",
    "solve": "orange",
    "auto": "red",
    "bare": "red",
    "general": "pink",
}


def _build_claude_stable_frontmatter() -> dict[str, tuple[tuple[str, Any], ...]]:
    policies = _tool_policies()
    return {
        role_id: (
            ("name", role_id),
            ("description", ""),
            ("disallowedTools", _claude_disallowed_tools(policies[role_id])),
            ("color", _CLAUDE_AGENT_COLORS[role_id]),
        )
        for role_id in SURFACED_ROLE_IDS
    }


CLAUDE_STABLE_FRONTMATTER: dict[str, tuple[tuple[str, Any], ...]] = _build_claude_stable_frontmatter()


def _opencode_tool_gates(policy: ToolPolicy) -> dict[str, bool]:
    """Translate the host-neutral policy into OpenCode's ``tools`` deny map.

    OpenCode gates native tools by name. Mutation tools are denied for read-only
    roles and ``task`` (sub-agent spawn) wherever spawning is denied. Native
    ``read``/``grep``/``bash`` stay enabled -- "prefer Atelier MCP" is handled by the
    body prose, and shell is never denied.
    """
    gates: dict[str, bool] = {}
    if _denies_mutation(policy):
        gates.update({"write": False, "edit": False, "patch": False})
    if _denies_spawn(policy):
        gates["task"] = False
    return gates


def _build_opencode_frontmatter() -> dict[str, tuple[tuple[str, Any], ...]]:
    policies = _tool_policies()
    out: dict[str, tuple[tuple[str, Any], ...]] = {}
    for role_id in SURFACED_ROLE_IDS:
        items: list[tuple[str, Any]] = [("description", "")]
        if role_id == "code":
            items.append(("mode", "primary"))
        gates = _opencode_tool_gates(policies[role_id])
        if gates:
            items.append(("tools", gates))
        out[role_id] = tuple(items)
    return out


OPENCODE_FRONTMATTER: dict[str, tuple[tuple[str, Any], ...]] = _build_opencode_frontmatter()

# Antigravity carries description-only frontmatter; the per-role text is injected
# at render time from the source ``agent_description`` (see render_simple_agent).
ANTIGRAVITY_FRONTMATTER: dict[str, tuple[tuple[str, Any], ...]] = {
    role_id: (("description", ""),) for role_id in SURFACED_ROLE_IDS
}


__all__ = [
    "ANTIGRAVITY_FRONTMATTER",
    "CLAUDE_STABLE_FRONTMATTER",
    "DEFAULT_OWNED_MODEL",
    "DEFAULT_ROLE_IDS",
    "HOST_ROLE_IDS",
    "OPENCODE_FRONTMATTER",
    "READONLY_OWNED_MODEL",
    "REPO_ROOT",
    "SURFACED_ROLE_IDS",
    "BenchmarkProfile",
    "DefaultRegistry",
    "DefaultRole",
    "DefaultWorkflow",
    "DefaultWorkflowStep",
    "HostProjection",
    "McpTemplate",
    "ModeDoc",
    "PromptDefinition",
    "ReviewContract",
    "ToolPolicy",
    "build_default_registry",
    "load_mode_docs",
    "markdown_body",
    "parse_frontmatter",
    "source_path_looks_like_mode_doc",
]
