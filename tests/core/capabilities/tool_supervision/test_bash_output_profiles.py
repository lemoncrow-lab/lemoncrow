"""Command-aware compiler and build-system Bash output profiles."""

from __future__ import annotations

from lemoncrow.pro.capabilities.tool_supervision.bash_output_profiles import compact_profiled_output


def _compact(command: str, text: str, budget: int = 1000, exit_code: int = 0):
    return compact_profiled_output(command, text, budget=budget, exit_code=exit_code)


def test_typescript_diagnostics_group_codes_and_keep_error() -> None:
    warnings = [f"src/w{i}.ts({i + 1},2): warning TS6133: unused symbol value{i}" for i in range(60)]
    error = "src/app.ts(9,4): error TS2322: Type 'string' is not assignable to type 'number'."
    result = _compact("tsc --noEmit", "\n".join([*warnings[:30], error, *warnings[30:]]), 700, 2)
    assert "TS2322" in result.text
    assert "errors=1" in result.text
    assert "warnings=60" in result.text
    assert "diagnostic-adjacent lines omitted" in result.text
    assert result.lossy is True


def test_cargo_profile_collapses_compile_phases_but_keeps_failure() -> None:
    phases = [f"   Compiling crate_{i} v0.1.{i}" for i in range(50)]
    failure = ["error[E0308]: mismatched types", "  --> src/main.rs:9:4", "help: convert the value"]
    result = _compact("cargo build", "\n".join([*phases, *failure]), 800, 101)
    assert "[lc cargo: compilingx50" in result.text
    assert "error[E0308]" in result.text
    assert "help: convert" in result.text


def test_go_profile_summarizes_successful_packages_and_keeps_failed_package() -> None:
    success = [f"ok\texample.com/pkg{i}\t0.{i:03d}s" for i in range(40)]
    failure = ["--- FAIL: TestBroken (0.01s)", "    broken_test.go:42: got 1 want 2", "FAIL\texample.com/broken\t0.02s"]
    result = _compact("go test ./...", "\n".join([*success, *failure]), 800, 1)
    assert "[lc go packages: ok=40" in result.text
    assert "TestBroken" in result.text
    assert "broken_test.go:42" in result.text


def test_gradle_profile_summarizes_task_statuses_and_keeps_failed_task() -> None:
    tasks = [f"> Task :module{i}:compileJava UP-TO-DATE" for i in range(50)]
    tasks += ["> Task :app:test FAILED", "FAILURE: Build failed with an exception."]
    result = _compact("./gradlew build", "\n".join(tasks), 700, 1)
    assert "[lc gradle tasks: total=51" in result.text
    assert "up-to-date=50" in result.text
    assert ":app:test FAILED" in result.text
    assert "Build failed" in result.text


def test_maven_profile_collapses_plugin_phases_and_keeps_error() -> None:
    phases = [f"[INFO] --- maven-compiler-plugin:3.12.{i}:compile (default-compile) @ app ---" for i in range(30)]
    result = _compact(
        "mvn verify", "\n".join([*phases, "[ERROR] Failed to execute goal", "[INFO] BUILD FAILURE"]), 700, 1
    )
    assert "[lc maven:" in result.text
    assert "Failed to execute goal" in result.text
    assert "BUILD FAILURE" in result.text


def test_ninja_profile_collapses_progress_and_keeps_compiler_error() -> None:
    phases = [f"[{i}/100] Compiling CXX object src/file{i}.o" for i in range(1, 81)]
    failure = ["src/main.cc:42:9: error: no matching function", "   42 | call(value);", "      | ^~~~"]
    result = _compact("ninja -j8", "\n".join([*phases, *failure]), 700, 1)
    assert "[lc native-build:" in result.text
    assert "no matching function" in result.text
    assert "42 | call(value)" in result.text


def test_docker_build_profile_groups_step_logs_and_keeps_error() -> None:
    lines = [f"#7 downloading layer chunk {i}/60" for i in range(60)]
    lines += ["#8 ERROR: process /bin/sh -c make failed", "failed to solve: process exited with code 2"]
    result = _compact("docker build .", "\n".join(lines), 700, 1)
    assert "[lc docker step #7: 60 log lines" in result.text
    assert "#8 ERROR" in result.text
    assert "failed to solve" in result.text


def test_unmatched_command_is_unchanged() -> None:
    text = "one\ntwo\nthree"
    result = _compact("echo hello", text)
    assert result.text == text
    assert result.lossy is False
