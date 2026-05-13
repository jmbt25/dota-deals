/**
 * Tests for the /api/* middleware.
 *
 * The middleware can't easily be tested via Pages Functions filesystem
 * routing in vitest-pool-workers (no Pages routing layer in the test
 * pool — see docs/WORKER_API.md for the local-dev caveat). Instead we
 * invoke `onRequest` directly with a hand-rolled `EventContext`, which
 * is exactly what Pages would pass at runtime.
 */

import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";

import { onRequest as middleware } from "../api/_middleware";
import type { ApiError, Env } from "../types";

/** Build an EventContext shape sufficient for the middleware. */
function makeCtx(opts: {
  next: () => Promise<Response>;
  url?: string;
}): EventContext<Env, string, Record<string, unknown>> {
  return {
    request: new Request(opts.url ?? "http://test/api/health"),
    env,
    next: opts.next,
    waitUntil: () => undefined,
    passThroughOnException: () => undefined,
    data: {},
    params: {},
    functionPath: "",
  };
}

describe("/api/* middleware", () => {
  it("sets Content-Type and Cache-Control on every response", async () => {
    const res = await middleware(
      makeCtx({
        next: async () => new Response(JSON.stringify({ ok: true }), { status: 200 }),
      }),
    );
    expect(res.headers.get("Content-Type")).toContain("application/json");
    expect(res.headers.get("Cache-Control")).toBe("no-store, max-age=0");
    expect(res.headers.get("X-Robots-Tag")).toBe("noindex, nofollow");
  });

  it("doesn't override Cache-Control if the handler set one", async () => {
    const res = await middleware(
      makeCtx({
        next: async () =>
          new Response(JSON.stringify({}), {
            status: 200,
            headers: { "Cache-Control": "public, max-age=60" },
          }),
      }),
    );
    expect(res.headers.get("Cache-Control")).toBe("public, max-age=60");
  });

  it("catches thrown errors and returns a structured 500", async () => {
    const res = await middleware(
      makeCtx({
        next: async () => {
          throw new Error("boom");
        },
      }),
    );
    expect(res.status).toBe(500);
    const body = (await res.json()) as ApiError;
    expect(body.error).toBe("internal_error");
    expect(body.status).toBe(500);
    expect(body.message).toBe("boom");
  });

  it("catches non-Error throws and still returns 500", async () => {
    const res = await middleware(
      makeCtx({
        next: async () => {
          throw "not an Error object";
        },
      }),
    );
    expect(res.status).toBe(500);
    const body = (await res.json()) as ApiError;
    expect(body.error).toBe("internal_error");
  });

  it("preserves handler-set status codes for non-200 responses", async () => {
    const res = await middleware(
      makeCtx({
        next: async () =>
          new Response(JSON.stringify({ error: "not_found" }), { status: 404 }),
      }),
    );
    expect(res.status).toBe(404);
  });
});
