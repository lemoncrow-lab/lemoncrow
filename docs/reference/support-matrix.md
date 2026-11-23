<!-- Generated from the LemonCrow status index. Do not edit by hand. -->

# Support matrix

What LemonCrow supports today, and where. Each status is the present-tense state of the product, not a roadmap.

## Status labels

| Status | Meaning |
| --- | --- |
| Supported | On by default where listed, with contract tests behind it. |
| Preview | Available and selectable, not yet qualified for production use. |
| Available, off by default | Present; a user or operator turns it on. |
| Hosted and Enterprise only | Only in the hosted service or an Enterprise deployment. |
| Not yet available | Present in the product but not to be relied on yet. |
| Not supported | No supported path. |

## Capabilities

| Capability | Status | Where |
| --- | --- | --- |
| Review on your machine | Supported | Local |
| Review-only local engine | Supported | Local |
| Hosted Review | Hosted and Enterprise only | Hosted, Enterprise |
| Review intelligence: Auto | Supported | Local, Hosted |
| Review intelligence: Quick | Preview | Local, Hosted |
| Review intelligence: Thorough | Preview | Local, Hosted |
| Review intelligence: Deep | Preview | Local, Hosted |
| Ask | Supported | Local, Hosted |
| Map and Architecture Delta | Not supported | Local, Hosted |
| Review analytics | Supported | Local, Hosted |
| Effective data flow | Supported | Local, Hosted |
| Diagnostic bundle | Supported | Local, Hosted |
| Auto-approval | Hosted and Enterprise only | Hosted, Enterprise |
| Signed review records | Available, off by default | Local, Hosted |
| Deployments and production outcomes | Available, off by default | Local, Hosted |
| Reproduce an execution locally | Supported | Local |
| Rendered DOM, accessibility and visual diffs | Available, off by default | Local |
| Rendered diffs on a worker or the managed sandbox (Rendered diffs run on your machine only; elsewhere the check is shown as not available, with how to run it.) | Not supported | Hosted |
| Infrastructure render diffs (Helm, Kustomize, Terraform) (Needs the tool installed where the check runs; without it the check is shown as not run, with the missing tool named. Nothing is ever applied.) | Preview | Local, Hosted |
| Alembic, Django and Rails migration execution (Plain SQL migrations run on an SQLite emulation; these frameworks are shown as not run, with how to supply SQL output.) | Not supported | Local, Hosted |
| Container image checks (Images are not built or scanned; packages and archives built by a command are compared.) | Not supported | Local, Hosted |
| SQL observation on PostgreSQL and MySQL (Only SQLite queries are captured; server databases appear as connections.) | Not supported | Local, Hosted |
| Interactive side-by-side preview (At most four previews run at once per repository (per machine locally); hosted previews need a dedicated preview domain.) | Available, off by default | Local, Hosted |
| Workspaces Lite | Supported | Local |
| Local code map | Supported | Local |

## Operating systems

| | Supported | Note |
| --- | --- | --- |
| Linux | Yes | Primary supported operating system. |
| macOS | No | Release builds are published; not yet covered by release qualification. |
| Windows | No | Partial and not currently validated. |

## Python

| | Supported | Note |
| --- | --- | --- |
| Python 3.12 | No | Installs; not yet covered by release qualification. |
| Python 3.13 | Yes |  |

## Browsers (Review Reader)

| | Supported | Note |
| --- | --- | --- |
| Chromium-based desktop browsers | Yes |  |
| Firefox | No | Not tested. |
| Safari / WebKit | No | Not tested. |

## Source hosts (hosted Review)

| | Supported | Note |
| --- | --- | --- |
| GitHub | Yes | Each release is verified against a real, controlled GitHub repository. |
| GitLab | No | Not supported. |
| Bitbucket | No | Not supported. |

## Deployment

| | Supported | Note |
| --- | --- | --- |
| LemonCrow-operated hosted service | Yes |  |
| Self-hosted in your network, outbound-only | Yes | Nothing has to reach in; egress is default-deny and every destination is named by the operator. Checked against the shipped Helm chart, manifests and Terraform module. |
| Self-hosted with no network egress at all (air-gapped) | No | Not yet covered by release qualification. |
| Self-hosted behind an HTTP(S) proxy | No | Not a supported deployment option. |
| Self-hosted with a custom certificate authority | No | Not a supported deployment option. |
