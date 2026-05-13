/**
 * Pages Functions middleware that runs for every `/api/*` route.
 *
 * Filesystem convention: a file named `_middleware.ts` in a Pages
 * Functions directory runs before any sibling/descendant handler. We
 * use that to centralize:
 *
 *  - Common response headers (`Content-Type`, `Cache-Control`,
 *    `X-Robots-Tag`).
 *  - Top-level error catching: any uncaught exception in a handler
 *    becomes a structured 500 JSON body rather than Cloudflare's
 *    default HTML error page. The frontend depends on JSON for every
 *    `/api/*` response, including failures.
 *  - Request/response logging via `console.log` — Workers' console
 *    output is captured by Cloudflare's log pipeline and visible in
 *    the dashboard's "Real-time logs" view.
 */

import type { ApiError, Env } from "../types";

/** Default cache policy for API responses. Read-only endpoints over
 * tables that change a few times per day; aggressive edge caching
 * would create stale-data confusion that's not worth the saved
 * latency. */
const DEFAULT_CACHE_CONTROL = "no-store, max-age=0";

/** Hides API responses from crawlers regardless of robots.txt — these
 * are not browser-facing URLs and we don't want them indexed. */
const DEFAULT_ROBOTS = "noindex, nofollow";

export const onRequest: PagesFunction<Env> = async (ctx) => {
  const started = Date.now();
  const method = ctx.request.method;
  const url = new URL(ctx.request.url);
  const path = url.pathname;

  let response: Response;
  try {
    response = await ctx.next();
  } catch (err) {
    response = errorResponse(err);
    console.error(JSON.stringify({
      event: "api_uncaught_error",
      path,
      method,
      error: errorMessage(err),
    }));
  }

  // Apply default headers. `Content-Type` is forced — /api/* is a
  // JSON-only contract and we don't want a handler that forgot to
  // call `c.json()` (or constructed a raw Response with a string body)
  // accidentally returning the WHATWG default of `text/plain` and
  // breaking the frontend's parser. `Cache-Control` is permissive:
  // handlers can override it (e.g., for a future cacheable endpoint),
  // we only set the default if the handler didn't.
  const out = new Response(response.body, response);
  out.headers.set("Content-Type", "application/json; charset=utf-8");
  if (!out.headers.has("Cache-Control")) {
    out.headers.set("Cache-Control", DEFAULT_CACHE_CONTROL);
  }
  out.headers.set("X-Robots-Tag", DEFAULT_ROBOTS);

  console.log(JSON.stringify({
    event: "api_request",
    path,
    method,
    status: out.status,
    duration_ms: Date.now() - started,
  }));

  return out;
};

/** Render an uncaught error as a structured 500 JSON body. */
function errorResponse(err: unknown): Response {
  const body: ApiError = {
    error: "internal_error",
    message: errorMessage(err),
    status: 500,
  };
  return new Response(JSON.stringify(body), {
    status: 500,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

/** Extract a human-readable message from any thrown value. */
function errorMessage(err: unknown): string {
  if (err instanceof Error) {
    return err.message;
  }
  if (typeof err === "string") {
    return err;
  }
  return "uncaught non-Error value";
}
