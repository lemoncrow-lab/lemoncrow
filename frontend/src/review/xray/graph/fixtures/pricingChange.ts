/**
 * Fixture (a): the LemonCrow demo's pricing change (examples/demo), as the backend would draw it with a recorded
 * before and after run. The Team plan goes from $49 to $59, a fourth plan is new, the review-summary counts move to
 * a renamed route, and previews get 180 seconds instead of 120.
 */
import type { XRayGraph } from "../xrayGraphTypes";

const WEB = "examples/demo/frontend/src";
const API = "examples/demo/backend/app.py";
const BASE_RUN = "run_base_7f3a";
const HEAD_RUN = "run_head_7f3a";

const plansBefore = {
  currency: "USD",
  plans: [
    { name: "Starter", monthly: 0, seats: 3 },
    { name: "Team", monthly: 49, seats: 25 },
    { name: "Business", monthly: 199, seats: 100 },
  ],
};
const plansAfter = {
  currency: "USD",
  plans: [
    { name: "Starter", monthly: 0, seats: 3 },
    { name: "Team", monthly: 59, seats: 25 },
    { name: "Business", monthly: 199, seats: 100 },
    { name: "Enterprise", monthly: 899, seats: 500 },
  ],
};
const plansResponse = {
  type: "PlansResponse",
  shape: "{ currency: string; plans: { name: string; monthly: number; seats: number }[] }",
  before: plansBefore,
  after: plansAfter,
  status: { before: 200, after: 200 },
  evidence: "recorded" as const,
  evidence_ref: "ev_plans_response",
  behavior_key: "api:GET /api/plans",
  redacted: false,
};
const settingsBefore = { artifact_limit_mb: 25, network: false, preview_timeout_seconds: 120, required_reviewers: 1, sandbox: "bubblewrap" };
const settingsAfter = { ...settingsBefore, preview_timeout_seconds: 180 };

export const PRICING_CHANGE: XRayGraph = {
  version: 1,
  review_id: "rev_demo_pricing",
  revision_id: "rv_demo_pricing_2",
  digest: "5c1e0f7a9b2d4c6e8f0a1b3c5d7e9f1a2b4c6d8e0f1a3b5c7d9e1f2a4b6c8d0e",
  title: "Team plan goes to $59 and review counts move to a new route",
  summary: "The pricing API returns Team at $59 and a new Enterprise plan; the overview reads its counts from /api/review-summary; previews time out after 180s.",
  provenance: {
    producer: "index+ai",
    model: "claude-sonnet-4-5",
    generated_at: "2026-11-18T16:42:07Z",
    recorded_runs: [
      { ref: BASE_RUN, recipe: "demo smoke: web + API", side: "base", at: "2026-11-18T16:31:12Z" },
      { ref: HEAD_RUN, recipe: "demo smoke: web + API", side: "head", at: "2026-11-18T16:36:48Z" },
    ],
  },
  lanes: [
    { id: "web", label: "React app", platform: "Vite" },
    { id: "api", label: "FastAPI", platform: "Python" },
    { id: "data", label: "Data files", platform: "Repository" },
  ],
  nodes: [
    { id: "ui_app", label: "App", kind: "ui", lane: "web", delta: "unchanged", subtitle: "App.jsx · routes", badges: [], anchors: [{ path: `${WEB}/App.jsx`, start_line: 18, end_line: 42, side: "head" }], source: "static" },
    { id: "ui_pricing", label: "PricingPage", kind: "ui", lane: "web", delta: "modified", subtitle: "pages/PricingPage.jsx", badges: ["+4 / -2"], anchors: [{ path: `${WEB}/pages/PricingPage.jsx`, start_line: 3, end_line: 29, side: "head" }], source: "recorded" },
    { id: "ui_overview", label: "OverviewPage", kind: "ui", lane: "web", delta: "modified", subtitle: "pages/OverviewPage.jsx", badges: ["+2 / -2"], anchors: [{ path: `${WEB}/pages/OverviewPage.jsx`, start_line: 4, end_line: 19, side: "head" }], source: "recorded" },
    { id: "ui_surface", label: "SurfaceCard", kind: "ui", lane: "web", delta: "modified", subtitle: "components/SurfaceCard.jsx", badges: ["+1 / -1"], anchors: [{ path: `${WEB}/components/SurfaceCard.jsx`, start_line: 4, end_line: 11, side: "head" }], source: "static" },
    { id: "api_plans", label: "GET /api/plans", kind: "route", lane: "api", delta: "modified", subtitle: "app.py · plans()", badges: ["+2 / -1"], anchors: [{ path: API, start_line: 15, end_line: 24, side: "head" }], source: "recorded" },
    { id: "api_summary_old", label: "GET /api/reviews/summary", kind: "route", lane: "api", delta: "removed", subtitle: "app.py · old path", badges: [], anchors: [{ path: API, start_line: 48, end_line: 53, side: "base" }], source: "recorded" },
    { id: "api_summary", label: "GET /api/review-summary", kind: "route", lane: "api", delta: "added", subtitle: "app.py · review_summary()", badges: [], anchors: [{ path: API, start_line: 26, end_line: 28, side: "head" }], source: "recorded" },
    { id: "api_settings", label: "GET /api/settings", kind: "route", lane: "api", delta: "modified", subtitle: "app.py · settings()", badges: ["+1 / -1"], anchors: [{ path: API, start_line: 34, end_line: 45, side: "head" }], source: "recorded" },
    { id: "data_plans", label: "plans.csv", kind: "datastore", lane: "data", delta: "modified", subtitle: "data/plans.csv", badges: ["+2 / -1"], anchors: [{ path: "examples/demo/data/plans.csv", start_line: 1, end_line: 5, side: "head" }], source: "recorded" },
    { id: "data_content", label: "content.json", kind: "config", lane: "data", delta: "modified", subtitle: "frontend/src/content.json", badges: ["+1 / -1"], anchors: [{ path: `${WEB}/content.json`, start_line: 1, end_line: 6, side: "head" }], source: "static" },
  ],
  edges: [
    { id: "e_app_pricing", from: "ui_app", to: "ui_pricing", kind: "render", label: "route /pricing", delta: "unchanged", evidence: "static", anchors: [{ path: `${WEB}/App.jsx`, start_line: 35, end_line: 35, side: "head" }] },
    { id: "e_app_overview", from: "ui_app", to: "ui_overview", kind: "render", label: "route /", delta: "unchanged", evidence: "static", anchors: [{ path: `${WEB}/App.jsx`, start_line: 34, end_line: 34, side: "head" }] },
    { id: "e_overview_surface", from: "ui_overview", to: "ui_surface", kind: "render", label: "renders cards", delta: "unchanged", evidence: "static", anchors: [{ path: `${WEB}/pages/SurfacesPage.jsx`, start_line: 12, end_line: 20, side: "head" }] },
    { id: "e_pricing_plans", from: "ui_pricing", to: "api_plans", kind: "http", label: "fetch plans", delta: "modified", evidence: "recorded", hero: true, anchors: [{ path: `${WEB}/pages/PricingPage.jsx`, start_line: 9, end_line: 14, side: "head" }], recorded: { ref: HEAD_RUN, count: 1 } },
    { id: "e_plans_csv", from: "api_plans", to: "data_plans", kind: "data", label: "reads plan rows", delta: "modified", evidence: "recorded", anchors: [{ path: API, start_line: 15, end_line: 17, side: "head" }], recorded: { ref: HEAD_RUN, count: 1 } },
    { id: "e_overview_summary_old", from: "ui_overview", to: "api_summary_old", kind: "http", label: "fetch counts", delta: "removed", evidence: "recorded", anchors: [{ path: `${WEB}/pages/OverviewPage.jsx`, start_line: 6, end_line: 8, side: "base" }], recorded: { ref: BASE_RUN, count: 1 } },
    { id: "e_overview_summary", from: "ui_overview", to: "api_summary", kind: "http", label: "fetch counts", delta: "added", evidence: "recorded", anchors: [{ path: `${WEB}/pages/OverviewPage.jsx`, start_line: 6, end_line: 8, side: "head" }], recorded: { ref: HEAD_RUN, count: 1 } },
    { id: "e_surface_settings", from: "ui_surface", to: "api_settings", kind: "http", label: "fetch preview limits", delta: "modified", evidence: "recorded", anchors: [{ path: `${WEB}/components/SurfaceCard.jsx`, start_line: 5, end_line: 6, side: "head" }], recorded: { ref: HEAD_RUN, count: 3 } },
    { id: "e_overview_content", from: "ui_overview", to: "data_content", kind: "data", label: "imports CTA copy", delta: "modified", evidence: "static", anchors: [{ path: `${WEB}/pages/OverviewPage.jsx`, start_line: 2, end_line: 2, side: "head" }] },
    { id: "e_surface_content", from: "ui_surface", to: "data_content", kind: "data", label: "reads card labels", delta: "unchanged", evidence: "inferred", anchors: [] },
  ],
  flows: [
    {
      id: "f_pricing",
      title: "Load the pricing page",
      summary: "Recorded on both sides: the page asks for plans, the route reads plans.csv, and Team comes back at $59.",
      participants: [{ node: "ui_pricing" }, { node: "api_plans" }, { node: "data_plans" }],
      steps: [
        {
          id: "s_request",
          from: "ui_pricing",
          to: "api_plans",
          label: "GET /api/plans",
          kind: "sync",
          delta: "modified",
          evidence: "recorded",
          recorded: { ref: HEAD_RUN, count: 1 },
          anchors: [{ path: `${WEB}/pages/PricingPage.jsx`, start_line: 9, end_line: 14, side: "head" }],
          payload: {
            request: {
              type: "GET /api/plans",
              before: { method: "GET", path: "/api/plans", headers: { accept: "application/json", cookie: "[masked]" } },
              after: { method: "GET", path: "/api/plans", headers: { accept: "application/json", cookie: "[masked]" } },
              evidence: "recorded",
              evidence_ref: "ev_plans_request",
              redacted: true,
            },
            response: plansResponse,
          },
        },
        { id: "s_read", from: "api_plans", to: "data_plans", label: "read plan rows", kind: "sync", delta: "modified", evidence: "recorded", recorded: { ref: HEAD_RUN, count: 1 }, anchors: [{ path: API, start_line: 15, end_line: 17, side: "head" }] },
        { id: "s_rows", from: "data_plans", to: "api_plans", label: "4 rows (was 3)", kind: "return", delta: "modified", evidence: "recorded", recorded: { ref: HEAD_RUN, count: 1 }, anchors: [{ path: "examples/demo/data/plans.csv", start_line: 2, end_line: 5, side: "head" }] },
        {
          id: "s_response",
          from: "api_plans",
          to: "ui_pricing",
          label: "200 PlansResponse",
          kind: "return",
          delta: "modified",
          evidence: "recorded",
          recorded: { ref: HEAD_RUN, count: 1 },
          note: "Team monthly goes 49 → 59; Enterprise is a new fourth plan.",
          anchors: [{ path: API, start_line: 15, end_line: 24, side: "head" }],
          payload: { response: plansResponse },
        },
        { id: "s_render", from: "ui_pricing", to: "ui_pricing", label: "render plan cards", kind: "self", delta: "modified", evidence: "recorded", recorded: { ref: HEAD_RUN, count: 4 }, repeat: 4, anchors: [{ path: `${WEB}/pages/PricingPage.jsx`, start_line: 15, end_line: 25, side: "head" }] },
      ],
    },
    {
      id: "f_overview",
      title: "Open the overview",
      summary: "The overview now reads its counts from the renamed route, and the cards read the longer preview limit.",
      participants: [{ node: "ui_overview" }, { node: "ui_surface" }, { node: "api_summary" }, { node: "api_settings" }],
      steps: [
        {
          id: "o_counts",
          from: "ui_overview",
          to: "api_summary",
          label: "GET /api/review-summary",
          kind: "sync",
          delta: "added",
          evidence: "recorded",
          recorded: { ref: HEAD_RUN, count: 1 },
          note: "Before, the page called /api/reviews/summary.",
          anchors: [{ path: `${WEB}/pages/OverviewPage.jsx`, start_line: 6, end_line: 8, side: "head" }],
          payload: {
            response: {
              type: "ReviewSummary",
              shape: "{ open: number; reviewed: number; stale_marks: number }",
              before: { open: 4, reviewed: 11, stale_marks: 1 },
              after: { open: 2, reviewed: 13, stale_marks: 3 },
              status: { before: 200, after: 200 },
              evidence: "recorded",
              evidence_ref: "ev_summary_response",
              behavior_key: "api:GET /api/review-summary",
              redacted: false,
            },
          },
        },
        { id: "o_counts_back", from: "api_summary", to: "ui_overview", label: "200 counts", kind: "return", delta: "added", evidence: "recorded", recorded: { ref: HEAD_RUN, count: 1 }, anchors: [{ path: API, start_line: 26, end_line: 28, side: "head" }] },
        { id: "o_cards", from: "ui_overview", to: "ui_surface", label: "render surface cards", kind: "sync", delta: "unchanged", evidence: "static", repeat: 3, anchors: [{ path: `${WEB}/pages/SurfacesPage.jsx`, start_line: 12, end_line: 20, side: "head" }] },
        {
          id: "o_settings",
          from: "ui_surface",
          to: "api_settings",
          label: "GET /api/settings",
          kind: "async",
          delta: "modified",
          evidence: "recorded",
          recorded: { ref: HEAD_RUN, count: 3 },
          anchors: [{ path: `${WEB}/components/SurfaceCard.jsx`, start_line: 5, end_line: 6, side: "head" }],
          payload: {
            response: {
              type: "Settings",
              before: settingsBefore,
              after: settingsAfter,
              status: { before: 200, after: 200 },
              evidence: "recorded",
              evidence_ref: "ev_settings_response",
              behavior_key: "api:GET /api/settings",
              redacted: false,
            },
          },
        },
        { id: "o_settings_back", from: "api_settings", to: "ui_surface", label: "200 settings", kind: "return", delta: "modified", evidence: "recorded", recorded: { ref: HEAD_RUN, count: 3 }, note: "Previews run offline and now get 180 seconds instead of 120.", anchors: [{ path: API, start_line: 34, end_line: 45, side: "head" }] },
      ],
    },
  ],
  views: [
    {
      id: "v_overview",
      title: "Whole change",
      lens: "architecture",
      summary: "Three pages, four routes and two data files; two calls were recorded on both sides.",
      scope: { kind: "all" },
      children: [
        { id: "v_price_path", title: "Price path", lens: "architecture", summary: "Where the Team price comes from now.", scope: { kind: "nodes", nodes: ["ui_pricing", "api_plans", "data_plans"] }, children: [] },
        { id: "v_counts", title: "Review counts", lens: "architecture", summary: "The overview's counts move from /api/reviews/summary to /api/review-summary.", scope: { kind: "nodes", nodes: ["ui_overview", "ui_surface", "api_summary_old", "api_summary", "api_settings", "data_content"] }, children: [] },
        { id: "v_flow_pricing", title: "Load the pricing page", lens: "flow", scope: { kind: "flow", flow: "f_pricing" }, children: [] },
        { id: "v_flow_overview", title: "Open the overview", lens: "flow", scope: { kind: "flow", flow: "f_overview" }, children: [] },
      ],
    },
  ],
  walkthrough: {
    steps: [
      {
        id: "t_ask",
        heading: "The pricing page asks the API for plans",
        body: "PricingPage calls GET /api/plans when it loads. A run recorded this call on both sides of the change.",
        stage: { kind: "view", view: "v_overview" },
        focus: { kind: "selection", nodes: ["ui_pricing", "api_plans"], edges: ["e_pricing_plans"] },
      },
      {
        id: "t_csv",
        heading: "plans.csv now prices Team at $59",
        body: "The route reads plan rows from plans.csv. The Team row changed from 49 to 59, and an Enterprise row is new.",
        stage: { kind: "view", view: "v_overview" },
        focus: { kind: "selection", nodes: ["api_plans", "data_plans"], edges: ["e_plans_csv"] },
      },
      {
        id: "t_response",
        heading: "The response carries the new price",
        body: "Recorded before and after: monthly goes 49 → 59 for Team and the status stays 200.",
        stage: { kind: "flow", flow: "f_pricing" },
        focus: { kind: "selection", messages: ["s_request", "s_response"] },
      },
      {
        id: "t_counts",
        heading: "Review counts move to a new route",
        body: "OverviewPage now calls /api/review-summary and the old path is gone. stale_marks goes from 1 to 3.",
        stage: { kind: "view", view: "v_counts" },
        focus: { kind: "selection", nodes: ["api_summary_old", "api_summary"], edges: ["e_overview_summary_old", "e_overview_summary"] },
      },
      {
        id: "t_previews",
        heading: "Offline previews get more time",
        body: "GET /api/settings returns preview_timeout_seconds 180 instead of 120. Previews run with the network off.",
        stage: { kind: "flow", flow: "f_overview" },
        focus: { kind: "selection", messages: ["o_settings", "o_settings_back"] },
      },
    ],
  },
  degraded: ["Two docs changed too (README.md, docs/architecture.md); docs are not drawn."],
};
