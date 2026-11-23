"""Container image builds: what may be built, and what a build is allowed to mean."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import artifact_inspection, shipped_artifacts
from lemoncrow.pro.environment import image_build, oci
from lemoncrow.pro.environment.image_build import ContainerImageRunner, build_argv, dockerfile_refusal

BASE = "registry.example/base@sha256:" + "a" * 64
FROM = f"FROM {BASE}\n"
KEY = "AKIA" + "QWERTYUIOPASDFGH"


@pytest.mark.parametrize(
    "text",
    [
        FROM,
        "FROM scratch\nCOPY a /a\n",
        f"ARG X=1\n{FROM}",
        f"# a comment\n\n{FROM}RUN echo $HOME\n",
        f"FROM {BASE} AS one\nRUN true\nFROM one\nCOPY --from=one /a /b\nCOPY --from=0 /a /c\n",
        f"FROM {BASE} AS one\nFROM {BASE}\nRUN --mount=type=bind,from=one,target=/x true\n",
        f"{FROM}RUN <<EOF\necho one\nEOF\nCOPY a /a\n",
        f"{FROM}ADD local.tar /x\n",
        f"{FROM}RUN a \\\n  # note\n  b\n",
    ],
)
def test_a_dockerfile_inside_the_policy_is_allowed(text: str) -> None:
    assert dockerfile_refusal(text, [BASE]) == ""


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "no FROM"),
        ("FROM alpine:3.20\n", "pins by digest"),
        ("FROM registry.example/base@sha256:" + "b" * 64 + "\n", "pins by digest"),
        ("ARG I=x\nFROM $I\n", "variables"),
        (f"FROM --platform=linux/amd64 {BASE}\n", "flags"),
        ("FROM later\nFROM scratch AS later\n", "pins by digest"),
        (f"# syntax=docker/dockerfile:1\n{FROM}", "syntax"),
        (f"# escape=`\n{FROM}", "escape"),
        (f"{FROM}ADD https://example.com/x /x\n", "remote"),
        (f"{FROM}ADD git@example.com:a/b.git /x\n", "remote"),
        (f"{FROM}ADD $URL /x\n", "remote"),
        (f"{FROM}ADD --checksum=sha256:0 a /x\n", "ADD --checksum"),
        (f"{FROM}COPY --from=alpine:3.20 /a /b\n", "earlier stage"),
        (f"{FROM}COPY --from=1 /a /b\n", "earlier stage"),
        (f"{FROM}RUN --network=host true\n", "--network"),
        (f"{FROM}RUN --security=insecure true\n", "--security"),
        (f"{FROM}RUN --mount=type=secret,id=x true\n", "secret"),
        (f"{FROM}RUN --mount=type=ssh true\n", "secret"),
        (f"{FROM}RUN --mount=type=bind,from=alpine,target=/x true\n", "earlier stage"),
        (f"{FROM}ONBUILD RUN true\n", "ONBUILD"),
        ("FROM\talpine:3.20\n", "pins by digest"),
        (f"{FROM}ADD\thttps://example.com/x /x\n", "remote"),
        (f"{FROM}RUN\t--network=host true\n", "--network"),
        (f'{FROM}ADD ["git@example.com:a/b", "/x"]\n', "remote"),
        (f"{FROM}ADD github.com/a/b /x\n", "remote"),
        (f"{FROM}RUN echo a<<EOF\nADD https://example.com/x /x\nEOF\n", "remote"),  # not a heredoc to the engine
        (f"{FROM}RUN <<EOF\nRUN --network=host true\nEOF\n", "--network"),
        (f"{FROM}RUN --mount=$M true\n", "variables"),
        (f"ARG BUILDKIT_SYNTAX=example/frontend\n{FROM}", "BUILDKIT_"),
        ("RUN true\n" + FROM, "before the first FROM"),
        (f'{FROM}COPY "unterminated /a\n', "parsed"),
    ],
)
def test_a_dockerfile_outside_the_policy_is_refused(text: str, why: str) -> None:
    assert why in dockerfile_refusal(text, [BASE])


def test_the_build_cannot_pull_cache_or_reach_a_network() -> None:
    for engine, pull in (("docker", "--pull=false"), ("podman", "--pull=never")):
        client = oci.OciClient(oci.OciRuntime(engine, engine, True), environ={})  # type: ignore[arg-type]
        argv = build_argv(client, dockerfile=Path("/c/D"), context=Path("/c"), archive=Path("/o/r.tar"))
        assert {pull, "--no-cache", "--network=none"} <= set(argv) and "type=tar,dest=/o/r.tar" in argv
        assert argv[-1] == "/c"


@pytest.mark.parametrize(("size", "kept"), [(4096, 4096), (8192, 8192), (8193, 8192), (65536, 8192)])
def test_the_archive_bound_holds_at_and_past_the_limit(tmp_path: Path, size: int, kept: int) -> None:
    out = tmp_path / "out"
    script = f"import sys; open(sys.argv[1], 'wb', buffering=0).write(b'x' * {size})"
    command = image_build.bounded_command([sys.executable, "-c", script, str(out)], 8192)
    subprocess.run(command, capture_output=True, check=False)
    assert out.stat().st_size == kept


def _rootfs(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, text in files.items():
            raw = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


class _Build:
    """The runner around a scripted engine: ``result`` is what the build does."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dockerfile: str | None = FROM) -> None:
        self.commands: list[list[str]] = []
        self.pulled: list[str] = []
        self.archive: bytes | None = _rootfs({"app/a.txt": "hello\n"})
        self.code = 0
        self.tmp = tmp_path
        self.dockerfile = dockerfile
        self.dockerfile_name = "Dockerfile"
        monkeypatch.setattr(oci, "detect_runtime", lambda: oci.OciRuntime("docker", "docker", True))
        monkeypatch.setattr(oci.OciClient, "ensure_image", lambda _self, image: self.pulled.append(image))
        monkeypatch.setattr(image_build, "run_limited_bounded", self.engine)
        self.records = {"old"}
        self.pruned: list[str] = []
        monkeypatch.setattr(image_build, "_buildx", self.buildx)

    def buildx(self, _client: Any, *args: str) -> tuple[int, str]:
        if args[0] == "du":
            return 0, "".join(f"ID:   {item}\nSize: 1B\n\n" for item in sorted(self.records))
        self.pruned.append(args[-1])
        self.records.discard(args[-1].removeprefix("id="))
        return 0, ""

    def engine(self, command: list[str], **_kwargs: Any) -> Any:
        self.commands.append(command)
        self.records.add("new")
        target = next(item for item in command if item.startswith("type=tar,dest=")).split("dest=", 1)[1]
        if self.archive is not None:
            Path(target).write_bytes(self.archive)
        return SimpleNamespace(returncode=self.code, stdout="step 1", stderr="")

    def materialize(self, *_args: Any, target: Path, **_kwargs: Any) -> None:
        target.mkdir(parents=True)
        if self.dockerfile is not None:
            (target / "Dockerfile").write_text(self.dockerfile)

    def run(self, images: tuple[str, ...] = (BASE,)) -> Any:
        host = SimpleNamespace(
            materialize_review_side=self.materialize,
            ReviewSnapshotUnavailable=LookupError,
            protect_text=lambda text: (text, 0),
            inspect_container_archive=artifact_inspection.inspect_container_archive,
        )
        context = SimpleNamespace(
            runner_host=host,
            store_root=self.tmp,
            source_root=self.tmp,
            repo_root=self.tmp,
            store=None,
            revision=SimpleNamespace(review_id="r", id="v"),
        )
        runtime = SimpleNamespace(
            id="rt",
            values={"dockerfile": self.dockerfile_name},
            strings=lambda _key: list(images),
            string=lambda _key, default="": default,
        )
        return ContainerImageRunner().run(context, runtime, SimpleNamespace(id="s", provider="p"), side="new")  # type: ignore[arg-type]


def _report(result: Any) -> dict[str, Any]:
    return dict(json.loads(bytes.fromhex(result.data["report"])))


def test_a_built_image_is_inspected_on_the_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build = _Build(monkeypatch, tmp_path)
    result = build.run()
    report = _report(result)
    assert result.status == "passed" and result.exit_code == 0 and build.pulled == [BASE]
    assert report["inspection"] == "host" and report["artifacts"][0]["kind"] == "image"
    accepted, why = shipped_artifacts.inspect_image_report(report)
    assert not why and accepted is not None
    assert not list(tmp_path.rglob("rootfs.tar")) and not list(tmp_path.rglob("workspace"))


@pytest.mark.parametrize("code", [0, 1])
def test_a_build_removes_only_the_builder_records_it_added(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int
) -> None:
    build = _Build(monkeypatch, tmp_path)
    build.code = code
    build.run()
    assert build.pruned == ["id=new"]


def test_a_parent_record_goes_once_its_child_has(monkeypatch: pytest.MonkeyPatch) -> None:
    client = oci.OciClient(oci.OciRuntime("docker", "docker", True), environ={})
    records = {"old", "child", "parent"}

    def buildx(_client: Any, *args: str) -> tuple[int, str]:
        if args[0] == "du":
            return 0, "".join(f"ID: {item}\n" for item in sorted(records))
        record = args[-1].removeprefix("id=")
        if record != "parent" or "child" not in records:
            records.discard(record)
        return 0, ""

    monkeypatch.setattr(image_build, "_buildx", buildx)
    image_build.drop_new_records(client, frozenset({"old"}))
    assert records == {"old"}


def test_records_that_will_not_go_end_the_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    client = oci.OciClient(oci.OciRuntime("docker", "docker", True), environ={})
    calls: list[str] = []

    def buildx(_client: Any, *args: str) -> tuple[int, str]:
        calls.append(args[0])
        return 0, "ID: stuck\n" if args[0] == "du" else ""

    monkeypatch.setattr(image_build, "_buildx", buildx)
    image_build.drop_new_records(client, frozenset())
    assert calls == ["du", "prune", "du"]


def test_a_builder_that_cannot_be_listed_is_left_alone_and_changes_no_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = _Build(monkeypatch, tmp_path)
    monkeypatch.setattr(image_build, "_buildx", lambda *_args: (1, ""))
    assert build.run().status == "passed" and build.pruned == []


def test_podman_has_no_builder_records_to_list() -> None:
    client = oci.OciClient(oci.OciRuntime("podman", "podman", True), environ={})
    assert image_build.build_records(client) is None


def test_a_dockerfile_reached_through_a_link_out_of_the_tree_is_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Dockerfile").write_text(FROM)
    build = _Build(monkeypatch, tmp_path, None)
    plain = build.materialize

    def linked(*args: Any, target: Path, **kwargs: Any) -> None:
        plain(*args, target=target, **kwargs)
        (target / "deploy").symlink_to(outside)

    build.materialize = linked  # type: ignore[method-assign]
    build.dockerfile_name = "deploy/Dockerfile"
    result = build.run()
    assert result.status == "failed" and build.commands == []


def test_a_credential_in_the_image_is_a_finding_without_its_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = _Build(monkeypatch, tmp_path)
    build.archive = _rootfs({"app/creds.txt": f"aws_access_key_id = {KEY}\n"})
    result = build.run()
    assert result.exit_code == image_build.EXIT_FINDINGS and KEY not in json.dumps(_report(result))


def test_a_failed_build_is_reported_as_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build = _Build(monkeypatch, tmp_path)
    build.code, build.archive = 1, None
    result = build.run()
    assert result.status == "failed" and _report(result)["exit"] == image_build.EXIT_TOOL_FAILED


def test_a_refused_dockerfile_never_reaches_the_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build = _Build(monkeypatch, tmp_path, "FROM alpine:3.20\n")
    result = build.run()
    assert result.status == "unavailable" and "pins by digest" in result.summary
    assert build.commands == [] and build.pulled == []


@pytest.mark.parametrize("code", [0, 1, -25])  # a client may survive the write limit and still exit 0
def test_an_image_over_the_bound_is_not_run_rather_than_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int
) -> None:
    build = _Build(monkeypatch, tmp_path)
    monkeypatch.setattr(image_build, "MAX_ARCHIVE_BYTES", 16)
    build.code = code
    result = build.run()
    assert result.status == "unavailable" and "inspection bound" in result.summary


def test_an_image_exactly_at_the_bound_is_inspected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build = _Build(monkeypatch, tmp_path)
    assert build.archive is not None
    monkeypatch.setattr(image_build, "MAX_ARCHIVE_BYTES", len(build.archive))
    assert build.run().status == "passed"


def test_a_build_that_writes_no_archive_is_not_a_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build = _Build(monkeypatch, tmp_path)
    build.archive = None
    assert build.run().status == "unavailable"


def test_without_an_engine_nothing_is_built(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build = _Build(monkeypatch, tmp_path)

    def missing() -> Any:
        raise oci.OciUnavailable("no container engine")

    monkeypatch.setattr(oci, "detect_runtime", missing)
    result = build.run()
    assert result.status == "unavailable" and "no container engine" in result.summary and build.commands == []


def _shipped(**extra: Any) -> tuple[list[Any], list[str]]:
    errors: list[str] = []
    return shipped_artifacts.parse_specs([{"id": "img", "builder": "container", **extra}], errors), errors


def test_only_a_configured_build_with_pinned_bases_executes() -> None:
    specs, errors = _shipped(base_images=[BASE], dockerfile="deploy/Dockerfile")
    assert not errors and specs[0].executable is True
    values = shipped_artifacts.runtime_values(specs[0])
    assert values["base_images"] == [BASE] and values["dockerfile"] == "deploy/Dockerfile"
    bare, errors = _shipped()
    assert not errors and bare[0].executable is False and "base_images" in bare[0].setup_hint


@pytest.mark.parametrize(
    "extra", [{"base_images": ["alpine:3.20"]}, {"base_images": BASE}, {"dockerfile": "../Dockerfile"}]
)
def test_an_unusable_build_definition_is_reported(extra: dict[str, Any]) -> None:
    specs, errors = _shipped(**extra)
    assert specs == [] and errors


def test_other_builders_take_no_image_keys() -> None:
    errors: list[str] = []
    assert shipped_artifacts.parse_specs([{"id": "p", "builder": "python", "base_images": [BASE]}], errors) == []
    assert errors


@pytest.mark.parametrize(
    "report",
    [{}, {"exit": 0}, {"exit": 3, "builder": "container"}, {"exit": 0, "builder": "container", "artifacts": []}],
)
def test_a_malformed_image_report_is_refused(report: dict[str, Any]) -> None:
    accepted, why = shipped_artifacts.inspect_image_report(report)
    assert accepted is None and why
