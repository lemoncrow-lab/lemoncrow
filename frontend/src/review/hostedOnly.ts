import { requestBrowserJson } from "./reviewApi";
import { fetchReviewEnvironment } from "./reviewEnvironment";

/**
 * One request to a surface that exists only on a hosted workspace (SPEC 7.1 P0-2: notification feed, organization page,
 * team health). A local install is known to have none from its own discovery document (`deployment.mode: local`), so
 * it makes no request and logs no error: the surface is absent, not disabled. An older hosted server that answers a
 * bare 404 is absent too. Any other failure throws, so a hosted server that cannot answer never reads as absent.
 */
export async function hostedJson<T>(path: string, init?: RequestInit): Promise<T | null> {
  const environment = await fetchReviewEnvironment("local");
  // A discovery that failed is not "local": say so (the caller shows a failure) instead of drawing nothing for ever.
  if (!environment.discovered) throw new Error("Couldn't reach the server to find out what it offers.");
  if (environment.mode === "local") return null;
  try {
    return await (init ? requestBrowserJson<T>(path, init) : requestBrowserJson<T>(path));
  } catch (reason) {
    const status = (reason as { status?: number }).status;
    if ((status === 404 || status === 405) && reason instanceof Error && /^(Not Found|Method Not Allowed|404|405)$/.test(reason.message)) return null;
    throw reason;
  }
}

/** True on a hosted workspace; false on a local install (and until discovery has answered, so nothing is drawn early). */
export async function isHostedWorkspace(): Promise<boolean> {
  const environment = await fetchReviewEnvironment("local");
  return environment.discovered ? environment.mode !== "local" : false;
}
