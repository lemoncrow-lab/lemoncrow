# Atelier Review Repair Mode

Use this task file when a plan or patch is failing repeatedly:

1. Load context with `task`.
2. Load current run state with `task`.
3. If failure repeats, call `rescue` with exact error signature and recent actions.
4. Apply smallest valid fix.
5. Re-run validation and call `trace`.
