#!/usr/bin/env bash
# The launch benchmark: LemonCrow and the DIY baseline on the 50-pull-request golden-comment corpus, three runs each,
# then both scored against the answer key by the same judge. See docs/reference/review-benchmark.md.
#
#   evals/review/launch_benchmark.sh plan [OUT_DIR]   print the call bound and the maximum spend; call nothing
#   evals/review/launch_benchmark.sh run  [OUT_DIR]   spend it (needs the claude CLI signed in)
#
# Other reviewers' saved comments are scored afterwards with `golden-score
# --reviewer-comments DIR --reviewer-label NAME`; the doc has the command. Reports hold reviewer comment text, so they
# go outside the checkout.
set -euo pipefail

mode="${1:-plan}"
out="${2:-$HOME/lc-bench-reports}"
corpus="evals/review/golden_comment_prs.yaml"
name="launch"
bench=(uv run lemoncrow dev eval review benchmark --corpus "$corpus" --reviewer lemoncrow,diy --repeat 3
  --depth thorough --no-pushes --max-usd 150 --max-calls 1600 --workdir "$HOME/lc-bench-work" --out-dir "$out" --name "$name")

case "$mode" in
  plan)
    "${bench[@]}" --plan
    ;;
  run)
    export LEMONCROW_ROOT="${LEMONCROW_ROOT:-$HOME/lc-bench-root}" LEMONCROW_LLM_BACKEND="${LEMONCROW_LLM_BACKEND:-claude}"
    uv run lemoncrow settings set intelligence.driver claude
    uv run lemoncrow settings set intelligence.model sonnet
    "${bench[@]}" || [ $? -eq 3 ] # 3: the spending ceiling stopped it; the partial report is still scored below
    for depth in thorough diy; do
      # 3: a pull request could not be scored; the report is still written, so score the other reviewer too
      uv run lemoncrow dev eval review golden-score --corpus "$corpus" --report "$out/$name.json" \
        --depth "$depth" --out-dir "$out" --name "$name-$depth" || [ $? -eq 3 ]
    done
    ;;
  *)
    echo "usage: $0 plan|run [OUT_DIR]" >&2
    exit 2
    ;;
esac
