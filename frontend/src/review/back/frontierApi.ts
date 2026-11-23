import { authenticatedFetch } from "../reviewApi";

/**
 * The server's D6 frontier notes for a Review (GET /reviews/{id}/frontier): plain sentences about how the base moved.
 * Nothing is inferred here. An empty list, an old server without notes or a failed read all give []: a frontier read never
 * turns a usable screen into an error.
 */
export async function fetchFrontierNotes(reviewId: string): Promise<string[]> {
  try {
    const response = await authenticatedFetch(`/api/reviews/${encodeURIComponent(reviewId)}/frontier`);
    if (!response.ok) return [];
    const body = await response.json() as { notes?: unknown } | null;
    return Array.isArray(body?.notes)
      ? body.notes.filter((note): note is string => typeof note === "string" && note.trim().length > 0)
      : [];
  } catch {
    return [];
  }
}
