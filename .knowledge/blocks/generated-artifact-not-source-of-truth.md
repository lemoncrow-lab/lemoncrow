# Generated Artifact Is Not Source Of Truth

- **id:** `generated-artifact-not-source-of-truth`
- **domain:** `source.truth`
- **status:** `active`
- **task_types:** bug_fix, content_fix, codegen_fix

## Situation

Generated text, cached output, compiled assets, and rendered views are often useful observations, but they are not authoritative on their own.

## Triggers

- generated copy
- ai output
- cache
- compiled asset
- rendered output
- hallucinated fact

## Dead ends

- trust generated output as canonical
- invent facts in a generated artifact
- reconcile a conflict by editing only the rendered layer

## Procedure

1. Treat generated output as observation, not authority.
2. Compare it against the structured or authored source.
3. Correct the source inputs.
4. Regenerate the output.
5. Diff the result to ensure no unsupported facts remain.

## Verification

- Contradiction was resolved at the source.
- Regenerated output matches the source.
- No untraceable facts remain in the output.

## Failure signals

- generated text contradicts structured data
- cached or compiled output is manually edited
- untraceable fact appears in the final output

## When not to apply

Hand-authored deliverables where the edited file itself is the canonical source rather than a generated artifact.
