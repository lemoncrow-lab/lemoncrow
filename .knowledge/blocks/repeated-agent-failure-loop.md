# Repeated Agent Failure Loop

- **id:** `repeated-agent-failure-loop`
- **domain:** `debugging`
- **status:** `active`
- **task_types:** debugging, test_fixing

## Situation
An agent has tried the same command or the same fix twice and seen the same failure. Continuing to retry is wasted budget and risks correlated damage.


## Triggers
- test failed
- same error
- retry
- loop
- stuck

## Dead ends
- running the failing command a third time without changes
- re-applying the same patch with cosmetic edits
- adding more print statements instead of forming a hypothesis
- increasing parallelism to shake out the failure

## Procedure
1. Stop. Do not run the failing command again.
2. Summarize the invariant being fought in one sentence.
3. List the assumptions that were tested and the assumptions that were not.
4. Search ReasonBlocks for the failure signature.
5. If no match exists, open the smallest reproducer and inspect it.
6. Form one new hypothesis before any further command.

## Verification
- A new hypothesis was written down.
- The next command tests that hypothesis specifically.

## Failure signals
- same error signature 2+ times
- same tool called 3+ times with same args
- growing, not shrinking, error context

## When not to apply
Flaky infrastructure where a clean retry is the documented procedure, such as a transient network error with bounded backoff.


## Scope
- tool_patterns: bash, run_tests
