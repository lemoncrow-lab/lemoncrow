# Frontend API Response Structure

- **id:** `frontend-api-response-structure`
- **domain:** `frontend`
- **status:** `active`
- **task_types:** implementation, debugging, refactor

## Situation
The agent incorrectly assumes that paginated API methods (like `api.traces()`) return a raw array of items, leading to TypeScript build errors when assigning the response to a state setter expecting an array.

## Triggers
- calling `api.traces()`
- using `.then(res => setX(res))`
- frontend build failures in `Memory.tsx`, `Traces.tsx`, or `Watchdogs.tsx`

## Dead ends
- Assigning `data` instead of `data.items` from an `api.traces()` call.
- Forgetting to import `TraceListResponse` when referencing it in component state or props.

## Procedure
1. **Verify Response Shape:** Always check `frontend/src/api.ts` for the return type of the API method.
2. **Handle Wrapper Objects:** For paginated responses, destructure or access the `.items` property (e.g., `setTraces(data.items)`).
3. **Check Metrics:** Use the `.metrics` property if the component needs statistics like total count or host distribution.
4. **Build Check:** Run `bun run build` in the `frontend` directory to verify type alignment.

## Verification
- The frontend build succeeds without TypeScript errors.
- Component state correctly reflects the `Trace[]` array from `data.items`.
