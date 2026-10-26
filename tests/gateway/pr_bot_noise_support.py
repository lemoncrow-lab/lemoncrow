"""Fakes for the noise-control and intent tests: a recording model and a GitHub that holds a description and issues."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution
from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError
from tests.gateway.pr_bot_support import NUMBER, SLUG, FakeGitHub, FakeModel, World, build_world, one_call_per_file

CONFIG = ".lemoncrow/review.yaml"


def review_yaml(**review: Any) -> str:
    """A ``.lemoncrow/review.yaml`` whose ``review:`` block holds *review*."""

    return yaml.safe_dump({"review": review}, sort_keys=False)


class Recorder:
    """Wraps a fake model and keeps every prompt (and schema) it is shown."""

    def __init__(self, inner: FakeModel) -> None:
        self.inner = inner
        self.prompts: list[dict[str, Any]] = []
        self.schemas: list[dict[str, Any]] = []
        self.systems: list[str] = []

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        self.prompts.append(json.loads(kwargs["prompt"]))
        self.schemas.append(kwargs["output_schema"])
        self.systems.append(kwargs["system"])
        return self.inner(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def investigator_prompts(self) -> list[dict[str, Any]]:
        return [prompt for prompt in self.prompts if "hypothesis" in prompt]

    def investigations(self) -> list[tuple[str, dict[str, Any], dict[str, Any], str]]:
        """``(category, prompt, schema, system)`` of every investigator call."""

        return [
            (prompt["hypothesis"]["value"]["category"], prompt, schema, system)
            for prompt, schema, system in zip(self.prompts, self.schemas, self.systems, strict=True)
            if "hypothesis" in prompt
        ]


class IntentGitHub(FakeGitHub):
    """A fake GitHub whose pull request has a description and whose repository has issues.

    ``issues`` maps a number to the row ``GET repos/OWNER/REPO/issues/N`` returns; ``unreadable`` maps a number to the
    error the token gets for it. Every issue read is kept in ``issue_reads``.
    """

    def __init__(self, origin: Any) -> None:
        super().__init__(origin)
        self.body = ""
        self.issues: dict[int, dict[str, Any]] = {}
        self.unreadable: dict[int, str] = {}
        self.issue_reads: list[int] = []

    def _pull(self) -> dict[str, Any]:
        return {**super()._pull(), "body": self.body}

    def __call__(self, args: Any, stdin: str | None = None) -> str:
        call = list(args)
        if len(call) == 2 and call[0] == "api":
            found = re.fullmatch(rf"repos/{SLUG}/issues/(\d+)", call[1])
            if found is not None and int(found[1]) != NUMBER:
                number = int(found[1])
                self.issue_reads.append(number)
                if number in self.unreadable:
                    raise GitHubCliError(self.unreadable[number])
                if number not in self.issues:
                    raise GitHubCliError("Not Found (HTTP 404)")
                return json.dumps(
                    {"number": number, "html_url": f"https://github.com/{SLUG}/issues/{number}", **self.issues[number]}
                )
        return super().__call__(args, stdin)


def intent_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> World:
    """``build_world`` with a GitHub that can hold a pull request description and issues.

    Each changed file gets a call of its own: whether an investigator is shown the author's intent depends on the
    categories of its call, so these tests need a call about the cache that is not also about authorization.
    """

    one_call_per_file(monkeypatch)
    world = build_world(tmp_path, monkeypatch, files)
    world.github = IntentGitHub(world.origin)
    for name in ("LEMONCROW_REVIEW_INTENT", "LEMONCROW_REVIEW_INTENT_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    return world


def clear_noise_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LEMONCROW_REVIEW_STRICTNESS", "LEMONCROW_REVIEW_INLINE_COMMENTS", "LEMONCROW_REVIEW_MAX_FILES"):
        monkeypatch.delenv(name, raising=False)


class IntentAwareModel(FakeModel):
    """A fake model that reads the author's stated intent when it is shown one.

    * a changed line ``MISMATCH: <title>`` is a confirmed finding that says the code contradicts the intent, but only
      when the prompt carries ``pr_description`` (a model that is not shown the intent cannot know);
    * a line ``BUG: <title>`` is not reported when the description says ``INTENDED: <title>``.
    """

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        properties = kwargs["output_schema"].get("properties", {})
        if "hypotheses" in properties or "answer" in properties or "violated" in json.dumps(kwargs["output_schema"]):
            return super().__call__(**kwargs)
        prompt = json.loads(kwargs["prompt"])
        hypothesis = prompt["hypothesis"]["value"]
        targets: list[str] = hypothesis["target_ids"]
        source = prompt["snapshot_source"]["value"]
        text = "\n".join(str(source[target].get("text", "")) for target in targets if target in source)
        described = str(((prompt.get("pr_description") or {}).get("value") or {}).get("body", ""))
        mismatch = re.search(r"MISMATCH: (.+)", text)
        bug = re.search(r"BUG: (.+)", text)
        if bug is not None and f"INTENDED: {bug.group(1).strip()}" in described:
            kwargs = {**kwargs, "prompt": kwargs["prompt"].replace("BUG: ", "NOTE: ")}
        execution = super().__call__(**kwargs)
        if mismatch is None or "intent_mismatch" not in properties:
            return execution
        path = str(source[targets[0]]["path"])
        title = mismatch.group(1).strip()
        result = {
            **execution.result,
            "status": "confirmed",
            "title": title,
            "explanation": f"The description says otherwise, and the change in {path} {title}.",
            "consequence": "The change does not do what it was described to do.",
            "evidence_refs": [f"target:{target}" for target in targets],
            "intent_mismatch": True,
        }
        if title in self.severity:
            result["severity"] = self.severity[title]
        return StructuredExecution(
            result=result,
            driver="openai",
            model=self.model,
            usage=ModelCallUsage(calls=1, input_tokens=self.input_tokens, output_tokens=self.output_tokens),
        )
