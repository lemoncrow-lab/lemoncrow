import { fireEvent, render as renderPlain, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, vi } from "vitest";

import { LOCAL_PRODUCT_CAPABILITIES, ProductCapabilitiesProvider, createProductCapabilities } from "../lib/RuntimeCapabilities";
import * as reviewApi from "../review/reviewApi";
import ReviewDevices from "./ReviewDevices";

// Hosted Review serves the connections: the pairing form shows under a Hosted Review account.
const HOSTED = createProductCapabilities({ review: true, runtime: false, mode: "hosted" });
function render(ui: ReactElement) {
  return renderPlain(<ProductCapabilitiesProvider value={HOSTED}>{ui}</ProductCapabilitiesProvider>);
}

const expire = 1_900_000_000;
const device = {
  worker_id: "machine-1", owner_id: "user-1", device_kind: "laptop" as const,
  execution_classes: '["runtime_validation"]', expires_at: expire,
  revoked_at: null as number | null,
};
function enterRepo() {
  fireEvent.change(screen.getByRole("textbox", { name: "Repository ID" }), {
    target: { value: "repo-1" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Load connections" }));
}
afterEach(() => { vi.restoreAllMocks(); });

it("says in one line how to connect where there is no Hosted Review account, and reads nothing", () => {
  const request = vi.spyOn(reviewApi, "requestBrowserJson");
  renderPlain(<ProductCapabilitiesProvider value={LOCAL_PRODUCT_CAPABILITIES}><ReviewDevices /></ProductCapabilitiesProvider>);
  expect(screen.getByRole("heading", { name: "Review connections" })).toBeInTheDocument();
  expect(screen.getByTestId("review-connections-no-account")).toHaveTextContent(/open Settings › Review connections in Hosted Review.*lcr connect/);
  expect(screen.queryByRole("form", { name: "Select Review repository" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Browse my repositories" })).not.toBeInTheDocument();
  expect(request).not.toHaveBeenCalled();
});

it("opens a Reader-linked current revision without queuing, pairing or sending source", async () => {
  const before = window.location.pathname + window.location.search;
  window.history.replaceState(null, "", "/settings/devices?repo_id=repo-allowed&review_id=review-1&revision_id=rev-1");
  const request = vi.spyOn(reviewApi, "requestBrowserJson")
    .mockResolvedValue({ devices: [] });
  try {
    render(<ReviewDevices />);
    expect(await screen.findByText("No machines found.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Repository ID" })).toHaveValue("repo-allowed");
    expect(screen.getByRole("textbox", { name: "Review ID" })).toHaveValue("review-1");
    expect(screen.getByRole("textbox", { name: "Current revision ID" })).toHaveValue("rev-1");
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith("/v1/review-devices?repo_id=repo-allowed");
    expect(screen.getByRole("button", { name: "Create approved task" })).toBeDisabled();
  } finally {
    window.history.replaceState(null, "", before);
  }
});

it("refuses malformed Reader-linked repository paths before any read", async () => {
  const before = window.location.pathname + window.location.search;
  window.history.replaceState(null, "", "/settings/devices?repo_id=..%2Fsecret&review_id=review-1&revision_id=rev-1");
  const request = vi.spyOn(reviewApi, "requestBrowserJson");
  try {
    render(<ReviewDevices />);
    expect(request).not.toHaveBeenCalled();
    expect(screen.queryByText("No machines found.")).not.toBeInTheDocument();
  } finally {
    window.history.replaceState(null, "", before);
  }
});

it("discovers only repositories authorized for Review management", async () => {
  const request = vi.spyOn(reviewApi, "requestBrowserJson")
    .mockResolvedValueOnce({ repositories: [{ repo_id: "repo-allowed" }, { repo_id: "repo-second" }] })
    .mockResolvedValueOnce({ devices: [] });
  render(<ReviewDevices />);
  expect(request).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Browse my repositories" }));
  const picker = await screen.findByRole("combobox", { name: "Authorized repositories" });
  fireEvent.click(picker);
  fireEvent.click(await screen.findByRole("option", { name: "repo-allowed" }));
  expect(screen.getByRole("textbox", { name: "Repository ID" })).toHaveValue("repo-allowed");
  fireEvent.click(screen.getByRole("button", { name: "Load connections" }));
  await screen.findByText("No machines found.");
  expect(request).toHaveBeenNthCalledWith(1, "/v1/review-devices/repositories");
  expect(request).toHaveBeenNthCalledWith(2, "/v1/review-devices?repo_id=repo-allowed");
});


it("refuses repository discovery errors without inventing access", async () => {
  vi.spyOn(reviewApi, "requestBrowserJson").mockRejectedValue(Error("403 Forbidden"));
  render(<ReviewDevices />);
  fireEvent.click(screen.getByRole("button", { name: "Browse my repositories" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("403 Forbidden");
  expect(screen.queryByRole("combobox", { name: "Authorized repositories" })).not.toBeInTheDocument();
});


it("does not fetch any credentials until an authorized repository is chosen", async () => {
  const request = vi.spyOn(reviewApi, "requestBrowserJson").mockResolvedValue({ devices: [] });
  render(<ReviewDevices />);
  expect(request).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("textbox", { name: "Repository ID" }), {
    target: { value: "../another-tenant" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Load connections" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("valid Hosted Review repository");
  expect(request).not.toHaveBeenCalled();
});

it("says when the machine list is bounded", async () => {
  vi.spyOn(reviewApi, "requestBrowserJson").mockResolvedValueOnce({ devices: [device], truncated: true });
  render(<ReviewDevices />);
  enterRepo();
  expect(await screen.findByText(/Showing the first 1 machines, active ones first/)).toBeInTheDocument();
});

it("loads machines, generates a one-time code, and does not disclose a machine token", async () => {
  const request = vi.spyOn(reviewApi, "requestBrowserJson")
    .mockResolvedValueOnce({ devices: [{ ...device, last_seen_at: Date.now() / 1000 }] })
    .mockResolvedValueOnce({
      pairing_code: "lcrp.one-time-code", expires_at: Date.now() / 1000 + 300, repo_id: "repo-1",
    });
  const view = render(<ReviewDevices />);
  enterRepo();
  expect(await screen.findByText(/Credential active · online status unknown/)).toBeInTheDocument();
  expect(screen.getByText(/Last machine check-in/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Create one-time code" }));
  expect(await screen.findByRole("textbox", { name: "One-time pairing code" })).toHaveValue("lcrp.one-time-code");
  expect(request).toHaveBeenNthCalledWith(2, "/v1/review-devices/pair", {
    method: "POST",
    body: JSON.stringify({
      repo_id: "repo-1", kind: "laptop",
      execution_classes: ["runtime_validation"], credential_lifetime_s: 7 * 86400,
    }),
  });
  expect(screen.getByText(/separately qualified workers/)).toBeInTheDocument();
  expect(screen.getByText(/Unattended agent execution on a connected machine is experimental/)).toHaveTextContent(
    /Not available yet: external sandbox\s+providers as a supported\s+execution target, and model tasks billed by LemonCrow/);
  fireEvent.click(screen.getByRole("button", { name: "Hide code" }));
  expect(screen.queryByRole("textbox", { name: "One-time pairing code" })).not.toBeInTheDocument();
  view.unmount();
  expect(request).toHaveBeenCalledTimes(2);
});

it("supports short-lived sandbox pairing and explicitly opted-in AI class", async () => {
  const request = vi.spyOn(reviewApi, "requestBrowserJson")
    .mockResolvedValueOnce({ devices: [] })
    .mockResolvedValueOnce({ pairing_code: "lcrp.sandbox", expires_at: Date.now() / 1000 + 300, repo_id: "repo-1" });
  render(<ReviewDevices />);
  enterRepo();
  await screen.findByText("No machines found.");
  fireEvent.click(screen.getByRole("combobox", { name: "Machine type" }));
  fireEvent.click(screen.getByRole("option", { name: "Agent sandbox" }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Allow budgeted Review AI tasks/ }));
  fireEvent.click(screen.getByRole("button", { name: "Create one-time code" }));
  await screen.findByRole("textbox", { name: "One-time pairing code" });
  expect(request).toHaveBeenNthCalledWith(2, "/v1/review-devices/pair", {
    method: "POST",
    body: JSON.stringify({
      repo_id: "repo-1", kind: "sandbox",
      execution_classes: ["runtime_validation", "review_intelligence"],
      credential_lifetime_s: 86400,
    }),
  });
});

it("requires explicit second click before revoking access and reloads machine state", async () => {
  const request = vi.spyOn(reviewApi, "requestBrowserJson")
    .mockResolvedValueOnce({ devices: [device] })
    .mockResolvedValueOnce({ revoked: true })
    .mockResolvedValueOnce({ devices: [{ ...device, revoked_at: Date.now() / 1000 }] });
  render(<ReviewDevices />);
  enterRepo();
  await screen.findByText(/Credential active · online status unknown/);
  fireEvent.click(screen.getByRole("button", { name: "Revoke access" }));
  expect(request).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Confirm revoke" }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
  expect(request).toHaveBeenNthCalledWith(2, "/v1/review-devices/revoke", {
    method: "POST", body: JSON.stringify({ repo_id: "repo-1", worker_id: "machine-1" }),
  });
  expect(await screen.findByText(/Revoked/)).toBeInTheDocument();
});

const aiDevice = { ...device, worker_id: "machine-ai", execution_classes: '["runtime_validation","review_intelligence"]' };
const unattendedOff = {
  organization: { enabled: false, updated_by: "", updated_at: null }, devices: [], runs: [], can_administer: true,
};

it("loads unattended execution only on request and enables a machine only with all three limits", async () => {
  const on = {
    ...unattendedOff,
    organization: { enabled: true, updated_by: "admin-1", updated_at: expire },
    devices: [{
      worker_id: "machine-ai", enabled: true, budget: { max_task_usd: 0.25, max_daily_usd: 2, max_daily_tasks: 8 },
      enabled_by: "admin-1", enabled_at: expire, updated_by: "admin-1", updated_at: expire,
      today: { day: "2030-03-17", tasks: 1, charged_usd: 0.01 },
    }],
    runs: [{
      run_id: "run-1", worker_id: "machine-ai", day: "2030-03-17", state: "stopped", job_id: "job-9",
      review_id: "review-1", task_kind: "investigate", task_cap_usd: 0.25, cost_usd: null, enabled_by: "admin-1",
      daily_cap_usd: 2, daily_task_cap: 8, reason: "stopped by an administrator", stopped_by: "admin-2", created_at: expire,
    }],
  };
  const request = vi.spyOn(reviewApi, "requestBrowserJson")
    .mockResolvedValueOnce({ devices: [aiDevice, device] })
    .mockResolvedValueOnce(unattendedOff)
    .mockResolvedValueOnce({ enabled: true, stopped_runs: 0, revoked: 0 })
    .mockResolvedValueOnce({ ...unattendedOff, organization: on.organization })
    .mockResolvedValueOnce({ status: on, stopped_runs: 0, revoked: 0 })
    .mockResolvedValueOnce({ status: { ...on, devices: [{ ...on.devices[0], enabled: false }] }, stopped_runs: 1, revoked: 1 });
  render(<ReviewDevices />);
  enterRepo();
  await screen.findAllByRole("button", { name: /machine-1/ });
  expect(request).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Show unattended execution" }));
  expect(await screen.findByText(/Organization: off/)).toBeInTheDocument();
  expect(request).toHaveBeenNthCalledWith(2, "/v1/review-devices/unattended?repo_id=repo-1");
  // Only machines paired for Review AI tasks can be given an unattended budget.
  expect(within(screen.getByRole("list", { name: "Unattended machines" })).queryByRole("button", { name: /machine-1/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Allow for the organization" }));
  expect(await screen.findByText(/Organization: on/)).toBeInTheDocument();
  expect(request).toHaveBeenNthCalledWith(3, "/v1/review-devices/unattended/organization", {
    method: "PUT", body: JSON.stringify({ enabled: true }),
  });
  fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
  const form = screen.getByRole("form", { name: "Unattended limits for machine-ai" });
  fireEvent.change(screen.getByRole("textbox", { name: "Max USD per task" }), { target: { value: "0.25" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Max USD per day" }), { target: { value: "0.10" } });
  fireEvent.submit(form);
  expect(await screen.findByRole("alert")).toHaveTextContent(/daily limit at least that large/);
  expect(request).toHaveBeenCalledTimes(4);
  fireEvent.change(screen.getByRole("textbox", { name: "Max USD per day" }), { target: { value: "2" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Max tasks per day" }), { target: { value: "8" } });
  fireEvent.click(screen.getByRole("button", { name: "Save limits and turn on" }));
  expect(await screen.findByText(/\$0\.25 per task · \$2\.00 and 8 tasks per UTC day/)).toBeInTheDocument();
  expect(request).toHaveBeenNthCalledWith(5, "/v1/review-devices/unattended", {
    method: "PUT", body: JSON.stringify({
      repo_id: "repo-1", worker_id: "machine-ai", enabled: true, max_task_usd: 0.25, max_daily_usd: 2, max_daily_tasks: 8,
    }),
  });
  const runs = screen.getByRole("list", { name: "Unattended runs" });
  // The run leads with the machine kind; machine and job IDs sit behind copy buttons.
  expect(runs).toHaveTextContent(/Laptop · investigate · stopped/);
  expect(within(runs).getByRole("button", { name: "Copy job ID job-9" })).toBeInTheDocument();
  expect(within(runs).getByRole("button", { name: "Copy machine ID machine-ai" })).toBeInTheDocument();
  expect(runs).toHaveTextContent(/not reported \(charged the task cap\)/);
  expect(runs).toHaveTextContent(/enabled by admin-1 · stopped by admin-2/);
  fireEvent.click(screen.getByRole("button", { name: "Stop now" }));
  await waitFor(() => expect(screen.getByRole("list", { name: "Unattended machines" })).toHaveTextContent(/Laptop · off/));
  expect(request).toHaveBeenNthCalledWith(6, "/v1/review-devices/unattended", {
    method: "PUT", body: JSON.stringify({ repo_id: "repo-1", worker_id: "machine-ai", enabled: false }),
  });
});

it("shows unattended settings read-only to a member who is not an organization admin", async () => {
  vi.spyOn(reviewApi, "requestBrowserJson")
    .mockResolvedValueOnce({ devices: [aiDevice] })
    .mockResolvedValueOnce({ ...unattendedOff, can_administer: false });
  render(<ReviewDevices />);
  enterRepo();
  await screen.findAllByRole("button", { name: /machine-ai/ });
  fireEvent.click(screen.getByRole("button", { name: "Show unattended execution" }));
  expect(await screen.findByText(/Only an organization admin can change these settings/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Allow for the organization" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Turn on" })).not.toBeInTheDocument();
});

it("never presents an unauthenticated list as an empty authorized list", async () => {
  vi.spyOn(reviewApi, "requestBrowserJson").mockRejectedValue(Error("403 Forbidden"));
  render(<ReviewDevices />);
  enterRepo();
  expect(await screen.findByRole("alert")).toHaveTextContent("403 Forbidden");
  expect(screen.queryByText("No machines found.")).not.toBeInTheDocument();
});

it("requires human external-source consent and current revision before queuing AI work", async () => {
  const request = vi.spyOn(reviewApi, "requestBrowserJson")
    .mockResolvedValueOnce({ devices: [] })
    .mockResolvedValueOnce({
      job_id: "job_exact", review_id: "review-1", revision_id: "revision-1",
      task_kind: "investigate", state: "queued", publication_state: "pending",
      deadline_at: Date.now() / 1000 + 900, attempt: 0, observation: null,
    });
  const uuid = vi.spyOn(crypto, "randomUUID").mockReturnValue("request-1" as `${string}-${string}-${string}-${string}-${string}`);
  try {
    render(<ReviewDevices />);
    enterRepo();
    await screen.findByText("No machines found.");
    expect(screen.getByRole("button", { name: "Create approved task" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Review ID" }), { target: { value: "review-1" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Current revision ID" }), { target: { value: "revision-1" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /approve sending bounded/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create approved task" }));
    expect(await screen.findByText(/Task job_exact · queued/)).toBeInTheDocument();
    expect(screen.getByText(/lcr task run job_exact/)).toBeInTheDocument();
    expect(screen.getByText(/lcr pending --watch --class review_intelligence/)).toBeInTheDocument();
    expect(request).toHaveBeenNthCalledWith(2, "/v1/review-agent-tasks", {
      method: "POST", body: JSON.stringify({
        repo_id: "repo-1", review_id: "review-1", revision_id: "revision-1",
        task_kind: "investigate", request_id: "request-1", external_source_approved: true,
      }),
    });
    expect(screen.getByRole("button", { name: "Create approved task" })).toBeDisabled();
  } finally { uuid.mockRestore(); }
});

it("renders unverified AI observations without implying review approval", async () => {
  const request = vi.spyOn(reviewApi, "requestBrowserJson")
    .mockResolvedValueOnce({ devices: [] })
    .mockResolvedValueOnce({
      job_id: "job_observed", review_id: "review-1", revision_id: "revision-1", task_kind: "investigate",
      state: "queued", publication_state: "pending", attempt: 0, deadline_at: Date.now() / 1000 + 900, observation: null,
    })
    .mockResolvedValueOnce({
      job_id: "job_observed", review_id: "review-1", revision_id: "revision-1", task_kind: "investigate",
      state: "succeeded", publication_state: "not_applicable", attempt: 1, deadline_at: Date.now() / 1000 + 900,
      observation: { text: "Possible bug in call graph", provenance: "author_agent_unverified",
        included_source_paths: ["src/main.py"], source_files_available: 4,
        input_tokens: 100, output_tokens: 30, estimated_cost_micros: 120 },
    });
  vi.spyOn(crypto, "randomUUID").mockReturnValue("request-2" as `${string}-${string}-${string}-${string}-${string}`);
  render(<ReviewDevices />);
  enterRepo();
  await screen.findByText("No machines found.");
  fireEvent.change(screen.getByRole("textbox", { name: "Review ID" }), { target: { value: "review-1" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Current revision ID" }), { target: { value: "revision-1" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /approve sending bounded/ }));
  fireEvent.click(screen.getByRole("button", { name: "Create approved task" }));
  await screen.findByText(/Task job_observed · queued/);
  fireEvent.click(screen.getByRole("button", { name: "Refresh task" }));
  expect(await screen.findByText("Possible bug in call graph")).toBeInTheDocument();
  expect(screen.getByText(/Author-agent observation · not independently verified/)).toBeInTheDocument();
  expect(screen.getByText(/1 of 4 available source files/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Cancel task" })).not.toBeInTheDocument();
  expect(request).toHaveBeenNthCalledWith(3, "/v1/review-agent-tasks/job_observed?repo_id=repo-1");
});

it("resumes previous signed tasks after a page reload and allows cancellation", async () => {
  const existing = {
    job_id: "job_saved", review_id: "review-1", revision_id: "revision-1",
    task_kind: "investigate", state: "queued", publication_state: "pending",
    attempt: 0, deadline_at: Date.now() / 1000 + 900, observation: null,
  };
  const request = vi.spyOn(reviewApi, "requestBrowserJson")
    .mockResolvedValueOnce({ devices: [] })
    .mockResolvedValueOnce({ tasks: [existing] })
    .mockResolvedValueOnce({ ...existing, state: "cancelled" });
  render(<ReviewDevices />);
  enterRepo();
  await screen.findByText("No machines found.");
  fireEvent.click(screen.getByRole("button", { name: "Load previous AI tasks" }));
  const saved = await screen.findByRole("button", { name: /investigate · job_saved · queued/ });
  fireEvent.click(saved);
  expect(await screen.findByText(/Task job_saved · queued/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Cancel task" }));
  expect(await screen.findByText(/Task job_saved · cancelled/)).toBeInTheDocument();
  expect(request).toHaveBeenNthCalledWith(2, "/v1/review-agent-tasks?repo_id=repo-1");
  expect(request).toHaveBeenNthCalledWith(3, "/v1/review-agent-tasks/job_saved/cancel", {
    method: "POST", body: JSON.stringify({ repo_id: "repo-1" }),
  });
});
