import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import * as reviewApi from "./reviewApi";
import { MAX_AUTO_RUNS, MAX_POLLS, OUTCOME_LABELS, getPartProof, loadPartProof, partOutcome, reachLine, refreshPartProof, resetPartProof, usePartProof, type PartProofProjection, type ProofOutcome } from "./partProof";
import { NoTestCommandLine, PartProofBadge, PartProofNote } from "./PartProofBadge";

vi.mock("./reviewApi", () => ({ requestBrowserJson: vi.fn() }));
const request = vi.mocked(reviewApi.requestBrowserJson);

const REASONS = { no_sandbox: "no sandbox is available here, and tests never run unsandboxed on their own" };
function record(revision: string, overrides: Partial<PartProofProjection> = {}): PartProofProjection {
  return { revision_id: revision, head_identity: `head-${revision}`, base_identity: "base", reach: "file", default: { o: "not_tested" }, files: {}, recorded: true, source: "auto", labels: {}, details: {}, reasons: REASONS, ...overrides };
}

beforeEach(() => { request.mockReset(); resetPartProof(); });
afterEach(() => resetPartProof());

describe("P11-1 the part proof vocabulary", () => {
  it("has a fixed, non-empty label for every outcome, with the exact words", () => {
    expect(OUTCOME_LABELS).toEqual({ proven: "Proven", exercised: "Exercised", fails: "Fails", not_tested: "Not tested", couldnt_run: "Couldn't run", not_run_yet: "Not run yet", over_budget: "Not run: over this review's budget" });
  });

  it("every outcome reads as a label and a sentence, never blank, even with no record at all", () => {
    for (const outcome of Object.keys(OUTCOME_LABELS) as ProofOutcome[]) {
      const view = partOutcome(record("r1", { files: { "a.py": { o: outcome, r: outcome === "couldnt_run" ? "no_sandbox" : undefined } } }), "a.py");
      expect(view.outcome).toBe(outcome);
      expect(view.label.trim()).not.toBe("");
      expect(view.text.trim()).not.toBe("");
    }
    expect(partOutcome(null, "a.py")).toMatchObject({ outcome: "not_run_yet", label: "Not run yet" });
    expect(partOutcome(record("r1", { default: { o: "pass" as ProofOutcome } }), "a.py").outcome).toBe("not_run_yet");
  });

  it("Not tested is a neutral word, never a pass, and never claims symbol or line coverage", () => {
    const view = partOutcome(record("r1"), "docs/readme.md");
    expect(view).toMatchObject({ outcome: "not_tested", label: "Not tested", tone: "neutral" });
    expect(view.tone).not.toBe("success");
    expect(view.text).toMatch(/import graph/);
    expect(partOutcome(record("r1", { files: { "a.py": { o: "exercised", t: ["tests/test_a.py"], n: 4 } } }), "a.py").tone).toBe("neutral");
    expect(reachLine(partOutcome(record("r1", { files: { "a.py": { o: "exercised", t: ["tests/test_a.py"], n: 4 } } }), "a.py"))).toBe("Reached by tests/test_a.py and 3 more, by imports (not line or symbol coverage).");
  });

  it("proven is the only success, fails and couldn't run are warnings, and couldn't run says why", () => {
    const proof = record("r1", { files: { "p.py": { o: "proven" }, "f.py": { o: "fails" }, "c.py": { o: "couldnt_run", r: "no_sandbox" }, "u.py": { o: "couldnt_run", r: "mystery" } } });
    expect(partOutcome(proof, "p.py").tone).toBe("success");
    expect(partOutcome(proof, "f.py").tone).toBe("warning");
    expect(partOutcome(proof, "c.py")).toMatchObject({ tone: "warning", text: `Couldn't run: ${REASONS.no_sandbox}.` });
    expect(partOutcome(proof, "u.py").text).toBe("Couldn't run: the tests did not complete.");
  });

  it("the badge and note render the label for a part nothing reaches", () => {
    render(<><PartProofBadge proof={record("r1")} path="orphan.py" /><PartProofNote proof={record("r1")} path="orphan.py" /></>);
    expect(screen.getByTestId("part-proof")).toHaveTextContent("Not tested");
    expect(screen.getByTestId("part-proof")).toHaveAttribute("data-outcome", "not_tested");
    expect(screen.getByTestId("part-proof-note").textContent).toMatch(/No test reaches this file/);
  });
});

describe("P11-1 one record per revision, read once", () => {
  it("many parts, one GET: nothing scales with the number of files", async () => {
    request.mockResolvedValue(record("rev1", { files: { "a.py": { o: "proven" } } }));
    const views = [renderHook(() => usePartProof("r1", "rev1")), renderHook(() => usePartProof("r1", "rev1")), renderHook(() => usePartProof("r1", "rev1"))];
    await waitFor(() => expect(views[0].result.current?.recorded).toBe(true));
    for (const view of views) expect(partOutcome(view.result.current, "a.py").outcome).toBe("proven");
    for (let index = 0; index < 10_000; index += 1) partOutcome(views[0].result.current, `src/file${index}.py`);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0][0]).toBe("/api/reviews/r1/part-proof?revision_id=rev1");
  });

  it("starts the run once per revision when no record exists and the reader may write, parts in view first", async () => {
    request
      .mockResolvedValueOnce(record("rev1", { recorded: false, default: { o: "not_run_yet" } }))
      .mockResolvedValueOnce(record("rev1", { files: { "a.py": { o: "proven" } } }));
    await loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: ["a.py", "b.py"], autoRun: true });
    await loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: ["a.py"], autoRun: true });
    expect(request).toHaveBeenCalledTimes(2);
    expect(request.mock.calls[1][0]).toBe("/api/reviews/r1/part-proof/run");
    expect(JSON.parse(String(request.mock.calls[1][1]?.body))).toEqual({ revision_id: "rev1", in_view: ["a.py", "b.py"] });
    expect(partOutcome(getPartProof("r1", "rev1"), "a.py").label).toBe("Proven");
  });

  it("never runs for a reader who cannot write, or when the server says why it will not", async () => {
    request.mockResolvedValueOnce(record("rev1", { recorded: false, default: { o: "not_run_yet" } }));
    await loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: [], autoRun: false });
    expect(request).toHaveBeenCalledTimes(1);
    request.mockResolvedValueOnce(record("rev2", { recorded: false, default: { o: "couldnt_run", r: "no_sandbox" } }));
    await loadPartProof({ reviewId: "r1", revisionId: "rev2", inView: [], autoRun: true });
    expect(request).toHaveBeenCalledTimes(2);
    expect(partOutcome(getPartProof("r1", "rev2"), "a.py").text).toContain("no sandbox");
  });

  it("a new revision never shows an older record, and a record for another revision is refused", async () => {
    request.mockResolvedValueOnce(record("rev1", { files: { "a.py": { o: "proven" } } }));
    await loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: [], autoRun: false });
    request.mockResolvedValueOnce(record("rev1", { files: { "a.py": { o: "proven" } } })); // the server answered with rev1 for rev2
    await loadPartProof({ reviewId: "r1", revisionId: "rev2", inView: [], autoRun: false });
    expect(getPartProof("r1", "rev2")?.files).toEqual({}); // never rev1's files
    expect(partOutcome(getPartProof("r1", "rev2"), "a.py").label).not.toBe("Proven");
    expect(partOutcome(getPartProof("r1", "rev1"), "a.py").label).toBe("Proven");
  });

  it("a failed read is a neutral Couldn't load that claims nothing about tests, never Not run yet and never blank", async () => {
    request.mockRejectedValue(new Error("offline"));
    await loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: [], autoRun: true });
    const view = partOutcome(getPartProof("r1", "rev1"), "a.py");
    expect(view).toMatchObject({ label: "Couldn't load", tone: "neutral", unavailable: true });
    expect(view.text).not.toMatch(/Tests reach/);
    expect(view.text.trim()).not.toBe("");
    expect(request).toHaveBeenCalledTimes(1); // no run is started for a record that could not even be read
  });

  it("a failed re-read keeps the record already held instead of blanking it", async () => {
    request.mockResolvedValueOnce(record("rev1", { files: { "a.py": { o: "proven" } } }));
    await loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: [], autoRun: false });
    request.mockRejectedValueOnce(new Error("offline"));
    await refreshPartProof("r1", "rev1");
    expect(partOutcome(getPartProof("r1", "rev1"), "a.py").label).toBe("Proven");
  });

  it("a refresh after a reviewer's Run test replaces the record every screen reads", async () => {
    request.mockResolvedValueOnce(record("rev1", { default: { o: "not_run_yet" }, recorded: false }));
    await loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: [], autoRun: false });
    request.mockResolvedValueOnce(record("rev1", { source: "run_test", files: { "a.py": { o: "fails", t: ["tests/test_a.py"], n: 1 } } }));
    const screenA = renderHook(() => usePartProof("r1", "rev1"));
    await act(async () => { await refreshPartProof("r1", "rev1"); });
    expect(partOutcome(screenA.result.current, "a.py")).toMatchObject({ outcome: "fails", label: "Fails" });
  });
});


describe("P11-1 a record that is still being written", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });
  const unfinished = (revision: string, extra: Partial<PartProofProjection> = {}) => record(revision, { complete: false, running: false, files: { "a.py": { o: "not_run_yet", t: ["tests/test_a.py"], n: 1 } }, ...extra });
  const tick = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
  const gets = () => request.mock.calls.filter(([path, init]) => !init && String(path).includes("/part-proof?")).length;
  const posts = () => request.mock.calls.filter(([, init]) => init?.method === "POST").length;

  it("a recorded run that stopped between batches is resumed by a reader who may write", async () => {
    request.mockResolvedValueOnce(unfinished("rev1")).mockResolvedValueOnce(record("rev1", { complete: true, files: { "a.py": { o: "exercised" } } }));
    await loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: ["a.py"], autoRun: true });
    expect(posts()).toBe(1);
    expect(partOutcome(getPartProof("r1", "rev1"), "a.py").outcome).toBe("exercised");
  });

  it("a reader who may not write never starts a run for it", async () => {
    request.mockResolvedValue(unfinished("rev1"));
    await loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: [], autoRun: false });
    expect(posts()).toBe(0);
  });

  it("rights that arrive after the first render still start the run (the GET is not the run)", async () => {
    request.mockResolvedValueOnce(record("rev1", { recorded: false, default: { o: "not_run_yet" } })).mockResolvedValueOnce(record("rev1", { files: { "a.py": { o: "proven" } } }));
    const view = renderHook(({ canWrite }) => usePartProof("r1", "rev1", { autoRun: canWrite }), { initialProps: { canWrite: false } });
    await tick(0);
    expect(gets()).toBe(1);
    expect(posts()).toBe(0);
    view.rerender({ canWrite: true });
    await tick(0);
    expect(posts()).toBe(1);
    expect(gets()).toBe(1); // still the one read
    expect(partOutcome(view.result.current, "a.py").outcome).toBe("proven");
  });

  it("re-reads while the record is running, once per revision per interval however many screens ask, and stops when it is complete", async () => {
    request.mockImplementation(async (path) => (gets() >= 4 && String(path).includes("/part-proof?") ? record("rev1", { complete: true, files: { "a.py": { o: "exercised" } } }) : unfinished("rev1", { running: true })));
    const screens = [renderHook(() => usePartProof("r1", "rev1")), renderHook(() => usePartProof("r1", "rev1")), renderHook(() => usePartProof("r1", "rev1"))];
    await tick(0);
    expect(gets()).toBe(1);
    await tick(2000); expect(gets()).toBe(2);
    await tick(4000); expect(gets()).toBe(3);
    await tick(8000); expect(gets()).toBe(4);
    expect(partOutcome(screens[0].result.current, "a.py").outcome).toBe("exercised");
    await tick(120_000);
    expect(gets()).toBe(4); // complete: no more reads
    expect(posts()).toBe(0); // someone else's run was underway; it never started a second one
  });

  it("stops re-reading when the screen unmounts", async () => {
    request.mockResolvedValue(unfinished("rev1", { running: true }));
    const view = renderHook(() => usePartProof("r1", "rev1"));
    await tick(0);
    await tick(2000);
    expect(gets()).toBe(2);
    view.unmount();
    await tick(120_000);
    expect(gets()).toBe(2);
  });

  it("is bounded: a record that never completes is read at most MAX_POLLS more times, and runs at most MAX_AUTO_RUNS times", async () => {
    request.mockImplementation(async (_path, init) => (init?.method === "POST" ? unfinished("rev1") : unfinished("rev1")));
    renderHook(() => usePartProof("r1", "rev1", { autoRun: true }));
    await tick(0);
    await tick(10 * 60_000);
    expect(gets()).toBe(1 + MAX_POLLS);
    expect(posts()).toBe(MAX_AUTO_RUNS);
  });

  it("an older GET that comes back after a newer refresh never overwrites it", async () => {
    let releaseOld!: (value: PartProofProjection) => void;
    request.mockImplementationOnce(() => new Promise<PartProofProjection>((resolve) => { releaseOld = resolve; }));
    const loading = loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: [], autoRun: false });
    request.mockResolvedValueOnce(record("rev1", { source: "run_test", files: { "a.py": { o: "fails" } } }));
    await refreshPartProof("r1", "rev1");
    expect(partOutcome(getPartProof("r1", "rev1"), "a.py").outcome).toBe("fails");
    releaseOld(record("rev1", { recorded: false, default: { o: "not_run_yet" } })); // the stale answer
    await loading;
    expect(partOutcome(getPartProof("r1", "rev1"), "a.py").outcome).toBe("fails");
  });
});

describe("P11-1 a row says why the tests could not run, in text", () => {
  it("shows the reason beside the badge when asked, and nothing extra for any other outcome", () => {
    const proof = record("r1", { files: { "c.py": { o: "couldnt_run", r: "no_sandbox" }, "p.py": { o: "proven" } } });
    const view = render(<><PartProofBadge proof={proof} path="c.py" showReason /><PartProofBadge proof={proof} path="p.py" showReason /></>);
    expect(screen.getAllByTestId("part-proof-reason")).toHaveLength(1);
    expect(screen.getByTestId("part-proof-reason").textContent).toBe(`Couldn't run: ${REASONS.no_sandbox}.`);
    view.rerender(<PartProofBadge proof={proof} path="c.py" />);
    expect(screen.queryByTestId("part-proof-reason")).toBeNull();
  });
});

describe("a repository with no test command", () => {
  it("says it once on the screen, quietly, and no part repeats a warning chip", () => {
    const proof = record("r1", { default: { o: "couldnt_run", r: "no_recipe" }, reasons: { ...REASONS, no_recipe: "this repo declares no test recipe" } });
    expect(partOutcome(proof, "a.py").noTestCommand).toBe(true);
    render(<><NoTestCommandLine proof={proof} /><PartProofBadge proof={proof} path="a.py" showReason saidOnScreen /><PartProofNote proof={proof} path="a.py" /></>);
    expect(screen.getByTestId("no-test-command")).toHaveTextContent("No test command set up");
    expect(screen.queryByTestId("part-proof")).toBeNull();
    expect(screen.queryByTestId("part-proof-reason")).toBeNull();
    expect(screen.queryByTestId("part-proof-note")).toBeNull();
  });

  it("a lone part without the screen line reads the same plain words, not a chip", () => {
    const proof = record("r1", { files: { "a.py": { o: "couldnt_run", r: "no_recipe" } } });
    render(<><NoTestCommandLine proof={proof} /><PartProofBadge proof={proof} path="a.py" showReason /></>);
    expect(screen.queryByTestId("no-test-command")).toBeNull();
    expect(screen.getByTestId("part-proof")).toHaveTextContent("No test command set up");
  });
});

describe("P11-4 a result the repository's CI reported", () => {
  const HEAD = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
  const LINE = "Reported by CI on bbbbbbb, not run by LemonCrow";
  const CI_REASONS = { ...REASONS, no_ci_result: "CI reported no test result for this file, and a hosted review does not map files to tests itself" };
  const ci = (overrides: Partial<PartProofProjection> = {}) => record("rev1", { source: "ci", head_identity: HEAD, default: { o: "couldnt_run", r: "no_ci_result" }, reasons: CI_REASONS, complete: true, files: { "tests/test_a.py": { o: "proven" }, "tests/test_b.py": { o: "exercised" } }, ...overrides });

  it("carries the exact source line on a part CI reported, and on no other", () => {
    expect(partOutcome(ci(), "tests/test_a.py")).toMatchObject({ outcome: "proven", label: "Proven", ci: LINE });
    expect(partOutcome(ci(), "tests/test_b.py")).toMatchObject({ outcome: "exercised", ci: LINE });
    // A part with no CI result reads the honest default: no CI claim, and its reason.
    const silent = partOutcome(ci(), "pkg/app.py");
    expect(silent).toMatchObject({ outcome: "couldnt_run", ci: "" });
    expect(silent.text).toBe(`Couldn't run: ${CI_REASONS.no_ci_result}.`);
    // A result LemonCrow ran never reads as CI's, even when it names the same file.
    expect(partOutcome(record("rev1", { files: { "a.py": { o: "proven" } } }), "a.py").ci).toBe("");
    expect(partOutcome(null, "a.py").ci).toBe("");
  });

  it("P12-2, P13-2: a part whose check could not be believed says why in a short label, and claims nothing of CI", () => {
    const labels: Record<string, string> = {
      untrusted_workflow: "untrusted workflow",
      workflow_unreadable: "the GitHub App cannot read workflow runs; it needs Actions: Read",
      proof_report_missing: "the proof report artifact is missing or expired",
      proof_report_invalid: "the proof report artifact could not be read",
      proof_report_unattested: "the proof report is not attested by GitHub for this pull request",
      proof_unsandboxed: "the proof ran without a sandbox, so what it reported cannot be believed",
    };
    const reasons = { ...CI_REASONS, ...labels };
    for (const [code, label] of Object.entries(labels)) {
      const untrusted = ci({ reasons, files: { "tests/test_a.py": { o: "couldnt_run", r: code } } });
      const view = partOutcome(untrusted, "tests/test_a.py");
      expect(view).toMatchObject({ outcome: "couldnt_run", ci: "" });
      expect(view.text).toBe(`Couldn't run: ${label}.`);
    }
    // A skipped test CI itself reported is still CI's, with its line.
    const skipped = ci({ files: { "tests/test_a.py": { o: "couldnt_run", r: "no_ci_result" } } });
    expect(partOutcome(skipped, "tests/test_a.py").ci).toBe(LINE);
    // So is a run that could not start its sandbox: the report was believed, it simply says nothing ran.
    const sandboxMissing = "the CI proof job has no working bubblewrap sandbox, so it ran nothing";
    const missing = ci({ reasons: { ...CI_REASONS, proof_sandbox_missing: sandboxMissing }, files: { "tests/test_a.py": { o: "couldnt_run", r: "proof_sandbox_missing" } } });
    expect(partOutcome(missing, "tests/test_a.py")).toMatchObject({ outcome: "couldnt_run", ci: LINE, text: `Couldn't run: ${sandboxMissing}.` });
    // So is a head failure the pull request's changed dependency file may explain: a failure it is not shown to own.
    const depsChanged = "the pull request changes a dependency file the CI proof run does not install, so a failure on head may come from the base's dependencies";
    const deps = ci({ reasons: { ...CI_REASONS, proof_deps_changed: depsChanged }, files: { "tests/test_a.py": { o: "couldnt_run", r: "proof_deps_changed" } } });
    expect(partOutcome(deps, "tests/test_a.py")).toMatchObject({ outcome: "couldnt_run", ci: LINE, text: `Couldn't run: ${depsChanged}.` });
    // A part CI named no row for, when its sandbox was missing, says that instead of "no result" (and, as ever for such a part, no CI line).
    const none = ci({ reasons: { ...CI_REASONS, proof_sandbox_missing: sandboxMissing }, default: { o: "couldnt_run", r: "proof_sandbox_missing" }, files: {} });
    expect(partOutcome(none, "pkg/app.py")).toMatchObject({ outcome: "couldnt_run", ci: "", text: `Couldn't run: ${sandboxMissing}.` });
    const untrusted = ci({ reasons, files: { "tests/test_a.py": { o: "couldnt_run", r: "untrusted_workflow" } } });
    render(<PartProofBadge proof={untrusted} path="tests/test_a.py" showReason />);
    expect(screen.getByTestId("part-proof-reason")).toHaveTextContent("untrusted workflow");
    expect(screen.queryByTestId("part-proof-source")).toBeNull();
  });

  it("makes the source visible and distinct on the badge, and says nothing of CI for a LemonCrow-run result", () => {
    render(<><PartProofBadge proof={ci()} path="tests/test_a.py" /><PartProofBadge proof={record("rev1", { files: { "a.py": { o: "proven" } } })} path="a.py" /></>);
    const badges = screen.getAllByTestId("part-proof");
    expect(badges[0]).toHaveAttribute("data-source", "ci");
    expect(badges[0].className).toContain("border-dashed");
    expect(badges[1]).not.toHaveAttribute("data-source");
    expect(badges[1].className).not.toContain("border-dashed");
    expect(screen.getAllByTestId("part-proof-source")).toHaveLength(1);
    expect(screen.getByTestId("part-proof-source")).toHaveTextContent(LINE);
  });

  it("the Focus note carries the line once, and a badge beside it can leave it out", () => {
    render(<><PartProofBadge proof={ci()} path="tests/test_a.py" showSource={false} /><PartProofNote proof={ci()} path="tests/test_a.py" /></>);
    expect(screen.getAllByTestId("part-proof-source")).toHaveLength(1);
    expect(screen.getByTestId("part-proof-note")).toHaveTextContent(LINE);
  });

  it("a part CI said nothing about shows its reason and no CI line", () => {
    render(<PartProofBadge proof={ci()} path="pkg/app.py" showReason />);
    expect(screen.getByTestId("part-proof")).toHaveAttribute("data-outcome", "couldnt_run");
    expect(screen.getByTestId("part-proof-reason")).toHaveTextContent(CI_REASONS.no_ci_result);
    expect(screen.queryByTestId("part-proof-source")).toBeNull();
  });

  it("never starts a run: a hosted record is complete, and a hosted default is not Not run yet", async () => {
    request.mockResolvedValue(ci());
    await loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: ["tests/test_a.py"], autoRun: true });
    request.mockClear();
    resetPartProof();
    request.mockResolvedValue(ci({ recorded: false, files: {} }));
    await loadPartProof({ reviewId: "r1", revisionId: "rev1", inView: [], autoRun: true });
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls.every(([url]) => !String(url).endsWith("/run"))).toBe(true);
  });

  it("markup in a reason or text is rendered as text, never as elements", () => {
    const hostile = ci({ details: { proven: "<img src=x onerror=alert(1)>" }, reasons: { no_ci_result: "<script>alert(1)</script>" } });
    const { container } = render(<><PartProofNote proof={hostile} path="tests/test_a.py" /><PartProofBadge proof={hostile} path="pkg/app.py" showReason /></>);
    expect(container.querySelector("img, script")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
  });
});
