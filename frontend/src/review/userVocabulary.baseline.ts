// lc-debt: existing review copy predates SPEC 2; each entry is cleared by its retiredBy task

export interface VocabularyBaselineEntry {
  /** Repo-relative path of the file that still carries the phrase. */
  file: string;
  /** The SPEC 2 never-use word or phrase, as the scanner lists it. */
  term: string;
  /** Hits of `term` in `file` today. The test fails above this and also below it (the list only shrinks). */
  count: number;
  /** The TASKS.md task that rewrites or retires this surface. */
  retiredBy: string;
}

export const VOCABULARY_BASELINE: readonly VocabularyBaselineEntry[] = [
  { file: "enterprise/frontend/src/review/HostedReviewReader.tsx", term: "Not yet", count: 2, retiredBy: "P8-3" },
  { file: "frontend/src/pages/Home.tsx", term: "measured", count: 2, retiredBy: "P5-1" },
  { file: "frontend/src/review/analytics/ReviewAnalytics.tsx", term: "measured", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/assumptions/Assumptions.tsx", term: "inferred", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/assumptions/Assumptions.tsx", term: "measured", count: 2, retiredBy: "P8-3" },
  { file: "frontend/src/review/assumptions/fixtures.ts", term: "inferred", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/blindSpots/blindSpotsModel.ts", term: "inferred", count: 2, retiredBy: "P8-3" },
  { file: "frontend/src/review/blindSpots/blindSpotsModel.ts", term: "Not yet", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/blindSpots/blindSpotsModel.ts", term: "unresolved", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/challenge/Challenge.tsx", term: "inferred", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/changeStory/changeStoryModel.ts", term: "inferred", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/ContextDrawer.tsx", term: "unresolved", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/FinishSheet.tsx", term: "unresolved", count: 1, retiredBy: "P2-4" },
  { file: "frontend/src/review/hostedQueue.ts", term: "unresolved", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/investigate/investigateModel.ts", term: "measured", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/investigation/fixtures.ts", term: "inferred", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/investigation/impactReadiness.ts", term: "measured", count: 2, retiredBy: "P8-3" },
  { file: "frontend/src/review/ReviewFeedbackPanel.tsx", term: "Not yet", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/ReviewFileSemantics.tsx", term: "inferred", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/ReviewFileSemantics.tsx", term: "pointer", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/ReviewHostedQueue.tsx", term: "unresolved", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/reviewMapModel.ts", term: "measured", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/reviewPrimaryAction.ts", term: "unresolved", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/ReviewReader.tsx", term: "Guided", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/ReviewReader.tsx", term: "unresolved", count: 1, retiredBy: "P8-3" },
  { file: "frontend/src/review/ReviewReadinessSheet.tsx", term: "unresolved", count: 5, retiredBy: "P2-4" },
  { file: "frontend/src/review/xray/xrayModel.ts", term: "measured", count: 2, retiredBy: "P8-3" },
];
