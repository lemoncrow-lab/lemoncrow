# Failure Recovery

- **id:** `rb-swe-general-failure-recovery`
- **domain:** `swe.general`
- **status:** `active`

## Situation
When a task fails or produces unexpected output and a recovery path must be determined.


## Triggers
- error
- failure
- retry
- debug

## Dead ends
- Retrying the same action without understanding the root cause
- Silently swallowing errors and moving on

## Procedure
1. Capture the full error message and context
2. Identify whether the failure is transient, configuration, or logic
3. Apply the smallest targeted fix first
4. Verify the fix with the original failing scenario before moving on
