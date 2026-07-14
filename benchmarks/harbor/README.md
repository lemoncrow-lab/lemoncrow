# Known verifier/task issues — terminal-bench-2.1

Audited the failing trials from run `results/lemoncrow/2026-07-07__02-24-29` (89 tasks, 1 attempt each, 19 non-1.0 trials) to separate genuine agent failures from failures caused by a bug in the task's own verifier/environment. Bar for inclusion here: a *correct* solution would still fail the exact same check. Being hard, slow, or timing out does not qualify on its own.

## Investigated, then downgraded from "verifier bug" — counts against score

### `torch-pipeline-parallelism`

**Looked like** an environment split at first: `/app` ships with no `requirements.txt`/`pyproject.toml`, the agent runs an unconstrained `pip install torch transformers` and lands `transformers==5.13.x`, while the verifier pins an older version. Verifier fails with:
```
TypeError: create_causal_mask() got an unexpected keyword argument 'inputs_embeds'. Did you mean 'input_embeds'?
```

**Verifier's actual pin, read from the cached task package** (`~/.cache/harbor/tasks/packages/terminal-bench/torch-pipeline-parallelism/<digest>/tests/test.sh`): `uvx -w transformers==4.55.0 -w torch==2.7.0 -w pytest==8.4.1 pytest ...`. Not unpinned on the verifier side — pinned, just not visible to the agent inside `/app`.

**Live-tested both versions side by side** (scratch venvs, `transformers==4.55.0`/`torch==2.7.0` vs `transformers==5.13.0`/latest torch) calling both the public and internal API with a small `LlamaModel`:

| API | transformers 4.55.0 (verifier) | transformers 5.13.0 (agent) |
|---|---|---|
| `model(inputs_embeds=...)` — public `forward()` | OK | OK |
| `transformers.masking_utils.create_causal_mask(inputs_embeds=...)` — internal helper | `TypeError: ... Did you mean 'input_embeds'?` | OK |

The public API is stable across both pins — `inputs_embeds` works identically in `LlamaModel.forward()` on either version. Only the internal `create_causal_mask` helper renamed its kwarg between 4.55.0 and 5.13.0. The task's own **reference solution** (`solution/solve.sh` in the same cached package) never calls `create_causal_mask` at all — it builds each pipeline stage as a real `nn.Module` and calls `model(input_tensor)[0]`, letting the model's own `forward()` construct the mask internally.

**Verdict:** AGENT_BUG, not a verifier bug. The agent chose to reimplement mask construction by calling an internal, version-fragile helper instead of the public, version-stable `forward()` the reference solution uses. `/app` genuinely has no version pin, so the agent's own installed `transformers` doesn't match the verifier's — but that gap only bites because of an avoidable internal-API choice. Not excluded from scoring. Relevant persona guidance already in place (`solve.md`: "prefer stable documented APIs over internal modules and version-dependent APIs") — this task is the concrete case that guidance targets; it has not yet been observed to fix this specific failure in a live trial.

## No confirmed genuine verifier bugs in this run

Every failing trial in `2026-07-07__02-24-29` traces to either an agent-side defect or a resource/time budget, not a broken check. Nothing here is currently excluded from the scored total.

## Investigated and ruled OUT — genuine agent failures, count against score

Each of these was checked against the same bar (would a correct solution still fail this check?) and the answer was no — the agent's own solution has an actual defect.

- **`build-cython-ext__WgvgzVP`** — looked like a numpy/Cython version mismatch at first glance (`AttributeError: module 'numpy' has no attribute 'int'` inside compiled `ccomplexity.pyx`). Checked the agent's edit history: it fixed `np.int`/`np.float`/`np.bool` in `cinvariants.pyx`, `chelpers.pyx`, `periodiccell.py`, `openknot.py` — but never touched `ccomplexity.pyx`, which still calls the removed `np.int` and was never tested. Incomplete fix, not an environment issue.
- **`cancel-async-tasks__z5YxwUm`** — shutdown handler re-cancels tasks already unwinding from a prior cancellation, re-delivering `CancelledError` into their own cleanup `finally` block before the `"Cleaned up."` print runs. Real asyncio-semantics bug in the agent's code.
- **`configure-git-webserver__cB6ABbS`**, **`dna-insert__s4aFwhH`**, **`extract-elf__LXSRgZj`**, **`filter-js-from-html__q3j8MH6`**, **`largest-eigenval__S5TfBDh`**, **`model-extraction-relu-logits__KFkx8f6`**, **`mteb-retrieve__8ynkVqK`**, **`pytorch-model-cli__eG9koge`**, **`raman-fitting__wyk7kRk`**, **`train-fasttext__k9uxF8U`**, **`video-processing__gHKBJ7V`** — each has a concrete correctness gap versus the spec (wrong output, missed threshold, wrong values against a private test set, etc.), confirmed by reading the verifier assertion and the agent's actual produced artifact. No environment/grading anomaly in any of these.

## Separate categories — not verifier bugs, listed for awareness only

These failed because of something other than either the agent's reasoning or the verifier's correctness. Not proposed for exclusion; call these out explicitly if a future scoring policy wants to treat them differently.

- **Timeouts on legitimately slow work** (`AgentTimeoutError`, no shortcut available): `extract-moves-from-video__Z86ZEqh` (per-frame OCR over a full video), `gpt2-codegolf__TJw3TjH` (from-scratch NumPy GPT-2 inference), `make-doom-for-mips__fhs9sxW` (cross-compile toolchain + dependency install), `rstan-to-pystan__TWyGF5v` (Stan/Cholesky posterior fit). In each, the trajectory shows real, non-thrashing work still in flight when the clock ran out — a task-difficulty/budget issue, not a broken check.
- **API policy refusal**: `protein-assembly__TkGo3dV` — Claude declined the request outright ("appears to violate our Usage Policy") on step 2, before doing any work, on a benign fusion-protein-design task. Neither an agent reasoning bug nor a verifier bug — a safety-classifier false positive upstream of both.

## Method

For each failing trial: read `result.json` (`verifier_result`, `exception_info`), `verifier/test-stdout.txt`, and the agent's own tool-call trajectory (`agent/claude-run.json`). Classified as a verifier bug only when the verifier's own environment/setup — not the agent's solution — is the demonstrated cause, with the exact mismatched versions/paths quoted above. Extend this file when a new run surfaces another reproducible instance of a task-level (not trial-level) issue.
