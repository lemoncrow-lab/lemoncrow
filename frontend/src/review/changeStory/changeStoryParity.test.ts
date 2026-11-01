// The Reader's Change Story and the pull request bot's port of it (pr_bot/change_story.py) walk the same
// shared fixture; both suites must reproduce its "expected" steps, so the two orderings cannot drift apart.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { buildChangeStory, type ChangeStoryInput } from "./changeStoryModel";

interface ExpectedStep {
  nodeId: string;
  role: string;
  change: string;
  exactness: string;
  side: string;
  line: number;
  via: { edgeId: string; fromId: string; toId: string; exactness: string; change: string } | null;
  findings: { id: string; on: string }[];
}

interface FixtureCase {
  name: string;
  input: ChangeStoryInput;
  expected: {
    ordering: string;
    changedTotal: number;
    omittedChanged: number;
    affectedTotal: number;
    omittedAffected: number;
    findingsKnown: boolean;
    steps: ExpectedStep[];
  };
}

// jsdom gives import.meta.url an http: scheme here, so the file is found from this test's own directory.
const FIXTURE = resolve(__dirname, "../../../../tests/fixtures/change_story/ordering.json");
const { cases } = JSON.parse(readFileSync(FIXTURE, "utf-8")) as { cases: FixtureCase[] };

describe("Change Story ordering parity with the pull request bot", () => {
  it("reads a fixture with cases", () => {
    expect(cases.length).toBeGreaterThanOrEqual(3);
  });

  it.each(cases.map((item) => [item.name, item] as const))("%s", (_name, item) => {
    const story = buildChangeStory(item.input);
    expect({
      ordering: story.ordering,
      changedTotal: story.changedTotal,
      omittedChanged: story.omittedChanged,
      affectedTotal: story.affectedTotal,
      omittedAffected: story.omittedAffected,
      findingsKnown: story.findingsKnown,
      steps: story.steps.map((step) => ({
        nodeId: step.nodeId,
        role: step.role,
        change: step.change,
        exactness: step.exactness,
        side: step.side,
        line: step.line,
        via: step.via
          ? { edgeId: step.via.edgeId, fromId: step.via.fromId, toId: step.via.toId, exactness: step.via.exactness, change: step.via.change }
          : null,
        findings: step.evidence.findings.map((finding) => ({ id: finding.id, on: finding.on })),
      })),
    }).toEqual(item.expected);
  });
});
