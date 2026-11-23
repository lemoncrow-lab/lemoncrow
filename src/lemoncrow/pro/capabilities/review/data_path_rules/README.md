# Data path rules for Review's experimental data flow difference

These are the taint rules `review/data_path.py` runs with [Opengrep](https://github.com/opengrep/opengrep) v2.0.0
(cross-file taint) on the base and the head of a Snapshot. The producer keys each flow it finds on its source and its
sink and reports the flows added, removed and rerouted between the two sides. It is experimental: it shows only the
flows these rules let the engine find, a minority of the real ones (about 17 % of hand-traced flows in the spike,
`docs/reports/2026-11-15-data-flow-engine-spike.md`).

## Provenance and licence

Every rule here was written for LemonCrow and is licensed under Apache-2.0 like the rest of this repository
(SPDX-License-Identifier in each file). The source and sink lists come from LemonCrow's own catalogs written for the
spike. None of them is copied from, or adapted from, another rule set:

- The Semgrep registry and the `semgrep-rules` repository are under the **Semgrep Rules License v1.0**, which does
  not allow redistributing them inside another product.
- The `opengrep-rules` repository adds the **Commons Clause** and is for "research, testing & benchmarking".

The Opengrep binary (LGPL-2.1) is never vendored or downloaded by a review: a local `lc review` finds the pinned
release the way it finds the security scan's (`LEMONCROW_DATA_PATH_OPENGREP_BIN`, then `LEMONCROW_OPENGREP_BIN`, then
`opengrep` on `PATH`), and runs it only when its SHA-256 equals the pin in `pr_bot/opengrep_install.py`
(`DATA_PATH_PINS`).

## What they cover

| File | Languages | Sources | Propagators (selector flows and dispatch) |
| --- | --- | --- | --- |
| `django.yaml` | Python | `request.data`, `query_params`, `GET`, `POST`, `FILES`, `body`, `headers`, `COOKIES`, `META`; `self.kwargs`; URL parameters of view handlers, `@action` and `@api_view` functions; the serializer of `perform_create/update/destroy`; `self.get_object()` | `Model.objects.get(pk=...)` and `get_object_or_404(Model, pk=...)` carry the request's taint to the row they return |
| `express.yaml` | TypeScript, JavaScript | `req.params`, `query`, `body`, `headers`, `cookies`, `files`, `file`; `req.get/header/param(...)`; the value of a `router.param` callback | `Object.assign(row, req.body)` taints `row` |
| `spring.yaml` | Java | parameters annotated `@RequestBody`, `@PathVariable`, `@RequestParam`, `@RequestHeader`, `@CookieValue`, `@ModelAttribute`, `@RequestPart`; `MultipartFile` parameters; `request.getParameter(...)` and the other servlet readers | `em.find(Entity.class, id)` and `repository.getReferenceById(id)` carry the id's taint to the entity |

Each file has one rule per kind of sink: `db` (writes and queries), `http` (outgoing requests), `file` (file writes)
and `log` (log lines). A rule's id is `lemoncrow.data-path.<framework>.<kind>`; the producer reads the kind from it.
Sanitizers are few on purpose (`len`, `bool`, `isinstance`; `Boolean`, `typeof`, `.length`; `isEmpty`, `size`,
`Objects.isNull`): a value that no longer carries the request's data.

Not covered, so named in every report: other languages and frameworks, Django generic views that write without a
handler of their own, Spring application events and aspects, build-time generated code, and calls deeper than the
depth the producer runs at (2 for TypeScript and JavaScript, 4 for Python and Java).

## Changing a rule

1. Change the rule in all four rules of its file: they share sources, propagators and sanitizers, written out in
   each rule because Opengrep rejects YAML anchors. `tests/gateway/test_review_data_path.py` fails when they differ.
2. Extend the fixture repositories in `tests/fixtures/data_path/` (a base and a head each) when the change should
   find a new flow.
3. Run the real-binary test with the pinned release:

   ```bash
   LEMONCROW_DATA_PATH_OPENGREP_BIN=/path/to/opengrep-v2.0.0 uv run pytest tests/gateway/test_review_data_path.py -q
   ```

A change to any rule file changes the rules digest, so a stored report from older rules is not served as current.
