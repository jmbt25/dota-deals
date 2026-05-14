# Deployment

## Architecture

```
┌─────────────────────────────┐         ┌──────────────────┐
│  GitHub Actions             │         │  Cloudflare D1   │
│  (cron: every 8h)           │◄───────►│  dota-deals      │
│                             │  HTTP   │  (ENAM region)   │
│  wrangler d1 migrate        │  REST   └──────────────────┘
│  universe refresh   (00h)   │
│  ingest                     │
│  signals compute    (16h)   │
│  score              (16h)   │
└─────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  Cloudflare Pages → dotadeals.com                            │
│                                                              │
│   Static frontend (public/)  STALE through Phase 12          │
│   serves the public/data/ JSON committed at Phase 10 ship.   │
│                                                              │
│   Pages Functions (functions/api/*)  LIVE as of Phase 11     │
│   /api/health, /api/report/latest, /api/report/:date,        │
│   /api/items/:id, /api/runs — read directly from D1.         │
│                                                              │
│   Deploys: `wrangler pages deploy public ...` from local     │
│   (Git auto-deploy intentionally off — see "Lessons from     │
│   Phase 11 deploy" below for why).                           │
│                                                              │
│   Phase 12 wires the frontend at fetch() to /api/, ending    │
│   the static-files stale window.                             │
└──────────────────────────────────────────────────────────────┘
```

Two pieces of infra outside the repo:

1. **Cloudflare D1 database** — `dota-deals`, region ENAM. UUID
   `cbd9fdf6-127b-4295-aeb9-5c1ea9aca9a7`. See `docs/D1_MIGRATION.md`
   for the storage architecture and the operational details.
2. **Cloudflare Pages project** — serves `public/` as a static site
   plus the Pages Functions in `functions/`. Deploys happen via
   `wrangler pages deploy public --project-name=dota-deals
   --branch=main` from an operator shell (the only place we still
   pull this trigger; see "Lessons from Phase 11 deploy" for the
   history). The project was recreated from scratch on 2026-05-14
   after the previous project was lost during a build-token
   troubleshooting session; the new project has **no Git
   integration** by design.

Everything below is one-time setup. Once it's done the pipeline runs
unattended; you only return for failures, feature work, or shipping
a new Worker bundle (which is now a deliberate operator command, not
an on-push side effect).

## Known gap during Phases 10 – 12

The live frontend at `dotadeals.com` is intentionally stale through
this window:

- **Phase 10** (this commit): the pipeline stops generating
  `public/data/*.json`. The committed files frozen at the Phase 10
  ship commit are what Pages keeps serving.
- **Phase 11** (this commit): a TypeScript Worker (Pages Functions)
  serves the same wire format the static files used, reading D1
  directly. Endpoints live at `/api/health`, `/api/report/latest`,
  `/api/report/:date`, `/api/items/:id`, `/api/runs`. The frontend
  isn't pointed at them yet, so the live page stays stale.
- **Phase 12**: the frontend's `fetch()` calls are pointed at the
  Worker. From this point the page is live against D1 again.
- **Phase 13**: the static-files path retires; `publish/` module
  and `public/data/` are deleted.

This is deliberate. No production users are watching during the v1
build-out, so trading "frontend stale for a week or two" for
"cleaner cutover commits" was a conscious decision. If you find a
user during this window, the message is: "the data is current in
D1 (visible via `wrangler d1 execute --remote ...`), the
public-facing view is on a planned-stale period through Phase 12."

## One-time: GitHub Actions secrets

Repo Settings → Secrets and variables → Actions. The pipeline needs
four secrets:

| Name | Value | Used by |
|---|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | your Cloudflare account ID (dashboard URL or "Account ID" widget) | `dota-deals` CLI via pydantic-settings |
| `CLOUDFLARE_D1_DATABASE_ID` | `cbd9fdf6-127b-4295-aeb9-5c1ea9aca9a7` | `dota-deals` CLI |
| `CLOUDFLARE_D1_API_TOKEN` | a token with scope `Account → D1 → Edit` on this database only | `dota-deals` CLI |
| `CLOUDFLARE_API_TOKEN` | a separate token with `D1: Edit` + `Workers Scripts: Edit` + `Cloudflare Pages: Edit` scopes | `wrangler` CLI (migrate step in CI; `pages deploy` for operator-driven Phase 11+ deploys) |

The two D1 tokens are deliberately distinct:

- `CLOUDFLARE_D1_API_TOKEN` is narrowly scoped to one database. The
  Python CLI uses it for every D1 read/write at pipeline time. If
  it leaks, blast radius is one database.
- `CLOUDFLARE_API_TOKEN` is what wrangler reads from the env (a fixed
  variable name; you can't rename it). It covers the migrate step
  in CI and the Pages deploy from the operator shell, so its scope
  is broader. Treat it as the higher-privilege secret.

Create the wrangler token: dashboard → My Profile → API Tokens →
Create Custom Token. Permissions: `Account → D1 → Edit`,
`Account → Workers Scripts → Edit`, `Account → Cloudflare Pages →
Edit`. Account Resources: your account. Cloudflare shows the
token value exactly once. (Phase 10 originally provisioned this
token with only the first two scopes; Phase 11 added the Pages
scope. See "One-time: Cloudflare Pages" → "Token scope" for the
in-place edit path if your token is already created.)

## One-time: Cloudflare Pages

Phase 11 changed how Pages deploys work: the project is provisioned
via `wrangler` from an operator shell, **not** the dashboard's
"Connect to Git" flow. The Cloudflare-managed build container is
deliberately not in the deploy path — it has burned us twice with
the "build token belongs to a user who has left your organization"
error (see "Lessons from Phase 11 deploy" below for the full story).

### Bootstrap a fresh Pages project

Run from the project root with your wrangler token (the one with
`Cloudflare Pages: Edit` scope) loaded into `CLOUDFLARE_API_TOKEN`:

```bash
# 1. Create the empty Pages project.
npx wrangler pages project create dota-deals --production-branch=main

# 2. Deploy the static assets + Pages Functions.
npx wrangler pages deploy public --project-name=dota-deals --branch=main
```

The deploy command **must pass `public`, not `.`** — wrangler walks
the static-asset directory you point it at, and if you point it at
the project root it tries to upload every cache directory the dev
toolchain has accumulated (.mypy_cache, .venv, node_modules), which
trips Pages' 25 MiB-per-file limit on the larger SQLite-backed
caches. The `functions/` directory is auto-discovered from the
project root regardless of what static-asset path you pass.

The repo's [`.cfignore`](../.cfignore) defends against the same
trap from a different angle — even if a future operator passes `.`
by accident, the dev-cache directories get filtered out before
upload.

`npm run deploy` runs this command for you with the right args.

### Custom domain

Re-attach `dotadeals.com` after the first successful deploy:

1. Cloudflare dashboard → Workers & Pages → `dota-deals` → **Custom
   domains** → **Set up a custom domain**
2. Enter `dotadeals.com`. If the domain is on Cloudflare DNS,
   the dashboard handles the records itself.

This is a one-time action per Pages project. If the project is ever
recreated again (it shouldn't be — wrangler-from-local should keep
the same project alive indefinitely), the custom domain must be
re-attached because attachments don't survive project deletion.

### Token scope

The wrangler token needs the four scopes below. Phase 10's GHA
secret had only the first two; Phase 11 added the third (which is
the new requirement for `wrangler pages deploy`). The fourth is
optional:

| Scope | Used by | Required? |
|---|---|---|
| `Account → D1 → Edit` | `wrangler d1 migrations apply` in CI | Yes |
| `Account → Workers Scripts → Edit` | future `wrangler deploy` for non-Pages Workers | Yes |
| `Account → Cloudflare Pages → Edit` | `wrangler pages deploy` | **Yes (added in Phase 11)** |
| `User → Memberships → Read` | silences a startup warning when `CLOUDFLARE_ACCOUNT_ID` isn't set | No |

If the token is already provisioned and missing the Pages scope,
dashboard → My Profile → API Tokens → edit the token in place
(Cloudflare lets you add permissions to existing tokens without
rotation). The token value stays the same; no need to update
`.env` or repo secrets.

### Git integration is intentionally OFF

The Pages project has **no GitHub connection**. Pushing to main
does NOT trigger a deploy. This is by design — Cloudflare's
build container relies on a separate build token that has
invalidated twice in our short history (Phase 8 and again during
the Phase 11 ship). Routing the deploy through `wrangler pages
deploy` from an authenticated shell bypasses the build container
entirely; the only token in play is the wrangler one, which the
operator controls.

Trade-off accepted: no PR deploy previews (Cloudflare's Pages
build container handled those). For a single-developer v1 with
no preview-environment workflow this is a small loss. If preview
deploys become valuable in v2+, the right move is probably a
GitHub Action that runs `wrangler pages deploy` against a branch-
named project, not reconnecting the dashboard Git integration.

## One-time: D1 schema

The pipeline's first step on every run is
`wrangler d1 migrations apply dota-deals --remote`, which is
idempotent. But to seed an empty database manually before the first
scheduled run (recommended), do it once from your local environment:

```bash
wrangler d1 create dota-deals       # already done; UUID captured above
wrangler d1 migrations apply dota-deals --remote
wrangler d1 execute dota-deals --remote \
    --command "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
```

Verify the table list is the expected nine plus wrangler's housekeeping
(`_cf_KV`, `d1_migrations`, `sqlite_sequence`).

`docs/D1_MIGRATION.md` has the full storage-layer architecture; this
section is just the operational quickstart.

## Pipeline schedule

The workflow at `.github/workflows/pipeline.yml` runs on cron and on
manual dispatch. Three stages, gated by the resolved hour:

| Time (UTC) | What runs |
|---|---|
| `00:00` | wrangler d1 migrate + universe refresh + ingest |
| `08:00` | wrangler d1 migrate + ingest |
| `16:00` | wrangler d1 migrate + ingest + signals + score |

The migrate step runs every invocation (idempotent; no-op when
nothing new is in `migrations/`). This means a freshly-committed
migration applies before the next pipeline step touches the schema
it expects.

## Manual runs

**Actions → pipeline → Run workflow.** One input:

| `skip_ingest` | Behavior |
|---|---|
| **off** (default) | Same as a scheduled run, except universe refresh is always skipped on manual dispatch. Late-day stages (signals + score) always run on manual dispatch. Use to force a fresh compute between scheduled slots. |
| **on** | No Steam hits. Re-runs signals + score against existing data. Use after a fix to one of those stages to recompute against the same ingest. |

The workflow's run summary (top of each run page in the GHA UI)
prints the resolved `run_universe / run_ingest / run_late` flags so
you can verify which stages actually executed.

## Trigger the first post-Phase-10 run

After secrets are set and the workflow file is on `main`:

1. **GitHub → Actions → pipeline → Run workflow.** Leave
   `skip_ingest` off.
2. **Watch the run.** It should:
   - Apply D1 migrations (no-op).
   - Skip universe refresh (manual dispatch ≠ 00 UTC).
   - Run ingest against the existing items table.
   - Run signals + score (manual dispatch always triggers them).
3. **Verify in D1.** From a local shell:
   ```bash
   wrangler d1 execute dota-deals --remote \
       --command "SELECT kind, status, items_ok, items_failed, items_quarantined, started_at \
                  FROM runs ORDER BY started_at DESC LIMIT 5"
   ```
   The last three rows should be the just-completed ingest +
   signals + scoring runs from the manual dispatch.

The next scheduled cron run is the real test. If something
fails in production, the fix is a follow-up commit, not a
workflow-config tweak — diagnose from the Actions log and the
D1 query log (Cloudflare dashboard → Workers & Pages → D1 →
dota-deals → Logs).

## Failure recovery

Failures show up as red ❌ in the Actions tab and (if you've enabled
it in your GitHub notification settings) as email. The run summary
always prints the flag table; combine that with the failed step's
logs to diagnose.

| Symptom | Cause | Fix |
|---|---|---|
| `D1ConfigError: CLOUDFLARE_ACCOUNT_ID is not set` (or similar) | A secret is missing or empty. | Add it under Settings → Secrets. The exact missing field is in the error message. |
| `D1AuthError: D1 authentication failed (HTTP 401)` | The `CLOUDFLARE_D1_API_TOKEN` was rotated or never had `D1: Edit` on this database. | Recreate the token in the Cloudflare dashboard, update the GitHub secret. |
| `D1NotFoundError: D1 endpoint returned 404` | Wrong `CLOUDFLARE_ACCOUNT_ID` or `CLOUDFLARE_D1_DATABASE_ID`. | Verify both via `wrangler d1 list`. |
| `wrangler: command not found` in the migrate step | Node version mismatch or stale runner cache. | Re-run the workflow; the `npx` invocation pulls wrangler on demand. If persistent, pin the Node version in the workflow. |
| `Authentication error [code: 10000]` from wrangler in the migrate step | `CLOUDFLARE_API_TOKEN` (the wrangler-side token, distinct from `CLOUDFLARE_D1_API_TOKEN`) is missing or has wrong scope. | Create the token with `Account → D1 → Edit + Account → Workers Scripts → Edit`, save as the `CLOUDFLARE_API_TOKEN` repo secret. |
| `signals run finished status=partial` with high `items_failed` | Most items have insufficient signal coverage (warmup, or an ingest-side regression broke the prior day's writes). | Inspect via `wrangler d1 execute --remote --command "SELECT signal_name, value, metadata_json FROM signals ORDER BY computed_for DESC LIMIT 20"`. |
| `ingest run finished status=partial` | Steam returned 4xx/5xx for some items. Not a workflow failure — just a partial day. | Investigate in the next run's summary; if persistent, check Steam status or the rarity tag values. |
| `too many SQL variables at offset N: SQLITE_ERROR` | A repository bulk-read passed > 100 bound parameters in one statement. The async repo's `_BULK_QUERY_CHUNK_SIZE=90` should prevent this for in-tree code; if a new call site triggers it, the fix is to chunk the IN clause. | Reduce chunk size or split the query. |
| Workflow times out at 30m | Probably the ingest hit a sustained 429 storm. | Wait an hour; re-run with `skip_ingest=on` to push out the late-day stages on existing data. |

The "rolling back a bad publish" section that used to live here is
gone with the publish step. Phase 11 reintroduces a "rolling back
the Worker" story when the Worker exists.

## Lessons from Phase 11 deploy

Mirrors the "Lessons the smoke tests taught us" section in
`docs/D1_MIGRATION.md`. Each item below is a reality-only failure
the migration playbook didn't anticipate; each one shaped the
deploy story this doc now records.

1. **Recurring "build token belongs to a user who has left your
   organization."** Cloudflare's Pages build container uses an
   internal build token that has invalidated twice in our short
   project history (Phase 8 first occurrence; Phase 11 second).
   The documented dashboard rotation ("Pages → project → Settings
   → Builds → API token → Create new") didn't take on the second
   occurrence — no error surfaced from the rotation itself, but
   the next build failed with the same token error. Workaround
   that ships: bypass the build container entirely by running
   `wrangler pages deploy` from an operator shell. No build
   container → no build token → no recurrence path. The cost is
   no Git auto-deploy, which the project doesn't depend on.

2. **`wrangler pages deploy .` tries to upload everything in the
   project root.** First Phase 11 deploy attempt failed on
   `.mypy_cache/3.12/cache.db` exceeding Pages' 25 MiB-per-file
   limit. The pre-Phase-11 deploy command was
   `wrangler pages deploy public ...`, and the Phase 11 instructions
   incorrectly changed it to `wrangler pages deploy . ...` (on the
   theory that `.` was needed to pick up the new `functions/`
   directory). That theory is wrong: `functions/` is auto-discovered
   from the project root regardless of the static-asset arg passed
   to deploy. Fix in this commit: the deploy command stays as
   `wrangler pages deploy public ...`, plus a [`.cfignore`](../.cfignore)
   at the repo root that excludes dev caches as a belt-and-braces
   defense.

3. **Pages projects can be silently deleted during dashboard
   troubleshooting.** Between the Phase 11 deploy attempt and the
   investigation, the entire Pages project `dota-deals` was removed
   from the account — `wrangler pages project list` returned an
   empty array. The custom domain attachment, environment variables,
   and Git connection went with it. The actual sequence wasn't
   captured (the operator was clicking around in response to the
   build-token error), but the outcome is: **whatever you do in the
   Pages dashboard's "Settings" or "Manage" tabs while troubleshooting
   a broken deploy can include destructive operations that don't
   prompt or page-confirm in obvious ways.** The wrangler-from-local
   bootstrap above is reproducible; if you suspect the project has
   gone sideways again, recreating it via `wrangler pages project
   create` is a 30-second recovery.

4. **There is a ghost Worker named `dota-deals` on the account.**
   Separate from the Pages project, with `last_deployed_from:
   dash_template` and a Hello-world body. Almost certainly created
   accidentally from a dashboard template during the same
   troubleshooting session. It's not serving traffic (workers.dev
   subdomain disabled) and is unrelated to the Pages deploys, but
   the name collision in the dashboard makes it easy to mis-target
   when reading error messages that say "Worker > Settings > Builds"
   (which is the new unified Workers Builds wording for what used
   to be a Pages-specific build). Recommended cleanup: delete the
   ghost Worker once you're confident the Pages project is healthy.

## Things not in scope for v1

- **Monitoring beyond GHA's built-in failure emails.** The Actions
  tab shows red ❌ on failure and emails you (if your GitHub
  notification settings allow). No PagerDuty, no Slack — overkill
  for an 8-hourly cron with no SLO.
- **Concurrency lockfiles.** The workflow's `concurrency` block
  prevents two runs from overlapping. D1's transactional batch
  writes mean a pathological overlap still wouldn't corrupt
  table state.
- **Multi-environment (staging / prod).** v1 deploys directly to
  `dotadeals.com`. A staging environment would be a second D1
  database + a second Pages project pointed at a branch.
