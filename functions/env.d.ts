/**
 * Ambient TypeScript declarations for the test + build setup.
 *
 * Two pieces wired up here:
 *
 *  1. `cloudflare:test` module augmentation: the test pool's
 *     `env` is typed as an empty `ProvidedEnv` by default. We
 *     extend it with our wrangler.toml-declared bindings so
 *     `env.DB` is well-typed everywhere in tests.
 *
 *  2. Vite's `?raw` import suffix: lets us pull `.sql` files in as
 *     string literals at build time. The test setup uses this for
 *     the schema + seed.
 */

declare module "cloudflare:test" {
  // Wrangler's D1 binding name from wrangler.toml plus Pages'
  // auto-provisioned ASSETS binding (the static-assets fetcher
  // Pages exposes inside Functions; tests don't exercise it but
  // `PagesFunction<Env>` requires its presence on the env type).
  interface ProvidedEnv {
    DB: D1Database;
    ASSETS: Fetcher;
  }
}

declare module "*.sql?raw" {
  const content: string;
  export default content;
}
