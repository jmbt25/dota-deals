# Deployment

## Architecture

```
┌─────────────────────────┐         ┌──────────────────┐
│   GitHub Actions        │         │   Cloudflare R2  │
│   (cron: every 8h)      │◄────────┤   dota_deals.db  │
│                         │         └──────────────────┘
│  db pull                │
│  universe refresh (00h) │
│  ingest                 │         ┌──────────────────┐
│  signals compute (16h)  │────────►│  GitHub `main`   │
│  score          (16h)   │  commit │  public/data/    │
│  publish        (16h)   │         └────────┬─────────┘
│  db push                │                  │
└─────────────────────────┘                  │ auto-deploy
                                             ▼
                                   ┌──────────────────────┐
                                   │  Cloudflare Pages    │
                                   │  dotadeals.com       │
                                   └──────────────────────┘
```

Three pieces of infra outside the repo:

1. **Cloudflare R2 bucket** — holds the canonical SQLite DB between runs.
2. **Cloudflare Pages project** — serves `public/` as a static site.
3. **GitHub Actions secrets** — let the workflow authenticate to R2.

Everything below is one-time setup. Once it's done the pipeline runs
unattended; you only return for failures or feature work.

## One-time: create the R2 bucket

In the Cloudflare dashboard:

1. **R2 → Create bucket.** Name: `dota-deals` (or your choice — record it
   for the `R2_BUCKET` secret below). Region: `auto`. No object lifecycle
   rules needed.
2. **(Recommended) Enable versioning.** R2 → Settings → Object Versioning →
   Enable. Lets you roll back to a previous DB if a workflow run leaves a
   corrupt file. Not required, but cheap insurance — the SQLite file is
   small.
3. **No CORS configuration needed.** R2 only serves the DB to the workflow
   itself, never to the browser. The frontend reads JSON from Pages, not
   R2.
4. **Create an API token.** R2 → Manage API Tokens → Create API Token.
   Permissions: **Object Read & Write** scoped to the `dota-deals` bucket.
   Save the **Access Key ID** and **Secret Access Key** — Cloudflare shows
   the secret exactly once.
5. **Record the S3 endpoint.** R2 → Overview → "S3 API URL" — looks like
   `https://<account>.r2.cloudflarestorage.com`. This becomes
   `R2_ENDPOINT`.

## One-time: GitHub secrets

Repo Settings → Secrets and variables → Actions → New repository secret.
Add four:

| Name | Value |
|---|---|
| `R2_ENDPOINT` | `https://<account>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | `dota-deals` (or whatever you named the bucket) |
| `R2_ACCESS_KEY_ID` | from the R2 API token |
| `R2_SECRET_ACCESS_KEY` | from the R2 API token |

The workflow uses these as environment variables; the dota-deals CLI reads
them via `pydantic-settings`. No other auth needed — the workflow's
`GITHUB_TOKEN` is auto-provided and has `contents: write` per the explicit
`permissions:` block.

## One-time: Cloudflare Pages

We use Cloudflare's **GitHub integration** rather than a `wrangler pages
deploy` step in a workflow. Reasoning: the pipeline workflow already
commits to `main` on every full-report run; Pages picks up the commit and
deploys automatically. Adding a deploy workflow would mean a second token
to rotate, a second place for things to go wrong, and a deploy that races
the commit. Simpler is better here.

1. **Cloudflare → Pages → Create application → Connect to Git.** Choose
   this repo. Branch: `main`.
2. **Build settings.** Cloudflare's current Pages UI requires the
   Deploy command field to be non-empty. With the default
   `npx wrangler pages deploy public` it fails (wrangler can't infer
   which Pages project to push to), so the command needs an explicit
   `--project-name`. Substitute the name you gave the Pages project at
   step 1 — it's the prefix of your `*.pages.dev` subdomain and also
   appears in the dashboard URL.

   | Field | Value |
   |---|---|
   | Framework preset | **None** — this is a static site |
   | Build command | **(empty)** |
   | Build output directory | **`public`** |
   | Root directory | **(empty / project root)** |
   | Deploy command | **`npx wrangler pages deploy public --project-name=YOUR_PROJECT_NAME`** (substitute your Pages project name). The `--project-name` flag is what makes wrangler deploy here instead of failing with "Must specify a project name." |

   Wrangler deploys to whatever `--project-name` resolves to — if you
   copy this command into another repo, double-check the name. It's an
   easy way to accidentally publish to the wrong project.

   *(If you'd rather not pin the project name in the dashboard, commit
   a `wrangler.toml` at the repo root with `name = "your-project-name"`
   and `pages_build_output_dir = "public"`. The deploy command then
   reduces to `npx wrangler pages deploy` and wrangler reads the rest.
   The dashboard approach is simpler for a single deployment target.)*
3. **Environment variables (Production, required).** Settings → Environment
   variables → Production → Add variable:

   | Name | Value | Why |
   |---|---|---|
   | `SKIP_DEPENDENCY_INSTALL` | `1` | The pipeline's `pyproject.toml` at the repo root makes Cloudflare auto-detect a Python project and try to `pip install .` before serving. The frontend doesn't need Python at all; this tells Pages to skip the install step entirely. Without it, the build fails with `Package 'dota-deals' requires a different Python: 3.13.3 not in '<3.13,>=3.12'`. |

   (Alternative if you can't skip for some reason: set
   `PYTHON_VERSION=3.12.10` instead. The install will succeed but burn
   build minutes on dependencies the frontend never uses. Prefer the
   skip variable.)

4. **Save and Deploy.** First deploy may serve an empty site if no
   `public/data/*.json` exists yet — that's the cold-start case the
   frontend handles (it'll render the error state at first, then the
   warmup state once the first scheduled run completes).
5. **Note the `*.pages.dev` URL** Cloudflare gives you. Open it; you
   should see the dota-deals UI in error state (no data yet) or warmup
   state (after the first scheduled run).

## One-time: custom domain

Once the `*.pages.dev` URL works:

1. **Cloudflare → Pages → your project → Custom domains → Set up custom
   domain.** Enter `dotadeals.com`.
2. **DNS:** if the domain is registered through Cloudflare, Pages adds
   the CNAME automatically. Otherwise add a CNAME at your registrar
   pointing `dotadeals.com` → `<project>.pages.dev`.
3. **Wait for SSL.** Cloudflare provisions an Edge certificate within
   a few minutes. The custom domain shows "Active" when ready.

## Trigger the first run

After secrets are set:

1. **GitHub → Actions → pipeline → Run workflow.** Leave `skip_ingest`
   unchecked.
2. **Watch the run.** The first run will:
   - Pull the DB — finds nothing in R2 → creates an empty local file
     and logs `first_run=true`.
   - Universe refresh — only if you triggered the manual run while UTC
     hour happens to be 00; for the typical "trigger anytime" case,
     this step is skipped on manual dispatch by design. To force it,
     wait for the 00 UTC scheduled run or trigger manually at 00 UTC.
   - Ingest — requires the items table to be populated, so the first
     manual run before any universe refresh will produce
     `items_failed = <items_in_file>` (the items list comes from the
     DB; an empty DB means an empty items.txt, and ingest exits cleanly
     with zero work).
   - Late-day stages — run because manual dispatch always triggers
     them. With an empty DB they produce a warmup `health.json` and
     an empty `latest.json`.
   - Commit `public/data/` — if anything changed.
   - Push DB to R2 — the empty SQLite gets uploaded as the canonical
     baseline.

The recommended cold-start sequence is therefore:

1. Wait for the next 00 UTC scheduled run (universe + ingest).
2. Wait for the next 16 UTC scheduled run (signals + score + publish).
3. Verify the frontend at `*.pages.dev` shows the warmup view with
   real `data_coverage` numbers.

## Manual runs

Use **Actions → pipeline → Run workflow** any time. Two checkboxes:

| `skip_ingest` | Behavior |
|---|---|
| **off** (default) | Same as a scheduled run, except universe refresh is always skipped on manual dispatch. The late-day stages always run. Use this to force a fresh report between scheduled slots. |
| **on** | No Steam hits. Re-runs signals + score + publish against existing data. Use after a fix to one of those stages — re-render without re-ingesting. |

The workflow's run summary (top of each run page in the GHA UI) prints
the resolved `run_universe / run_ingest / run_late` flags so you can
verify which stages actually executed.

## Failure recovery

Failures show up as red ❌ in the Actions tab and (if you've enabled it
in your GitHub notification settings) as email. The run summary always
prints the flag table; combine that with the failed step's logs to
diagnose.

| Symptom | Cause | Fix |
|---|---|---|
| `R2ConfigError: requires R2_ENDPOINT, …` | A secret is missing or empty. | Add it under Settings → Secrets. The exact missing field is in the error. |
| `R2CredentialsError: rejected credentials` | The API token was rotated or never had write access. | Recreate the token (R2 → Manage API Tokens), update `R2_ACCESS_KEY_ID` + `R2_SECRET_ACCESS_KEY`. |
| `R2BucketMissing: dota-deals does not exist` | Wrong `R2_BUCKET` name, or bucket was deleted. | Verify the name; recreate the bucket if needed. |
| `ingest run finished status=partial` | Steam returned 4xx/5xx for some items. Not a workflow failure — just a partial day. | Investigate in the next run's run summary; if persistent, check Steam status or the rarity tag values in `ingest/universe.py`. |
| `network error` from `db pull` / `db push` | Transient Cloudflare blip. | Re-run via workflow_dispatch. The retry loop in `r2.py` handles short blips; a longer outage surfaces here. |
| Workflow times out at 30m | Probably the ingest hit a sustained 429 storm. | Wait an hour; re-run with `skip_ingest=true` to push out the late-day stages on existing data. |
| **Pages build fails:** `Package 'dota-deals' requires a different Python: 3.13.3 not in '<3.13,>=3.12'` | Cloudflare Pages saw `pyproject.toml` at the repo root and auto-detected a Python project, then tried to install with its default Python (3.13). The frontend doesn't need any Python deps — the pipeline's pyproject is unrelated. | Set `SKIP_DEPENDENCY_INSTALL=1` in Pages → Settings → Environment variables → Production, then retry the deploy from the Deployments tab. See the "One-time: Cloudflare Pages" section above for the full env-var table. |
| **Pages deploy fails:** `Executing user deploy command: npx wrangler pages deploy public` → `Must specify a project name.` | Cloudflare's current Pages UI requires the Deploy command field, and the default `npx wrangler pages deploy public` doesn't include `--project-name`, so wrangler bails. | Set the Deploy command to `npx wrangler pages deploy public --project-name=YOUR_PROJECT_NAME` (substitute the name from the `*.pages.dev` subdomain) in Pages → Settings → Builds & deployments → Build configurations. Save, retry. See the build-settings table above for the full set. |

## Rolling back a bad publish

Two scenarios:

**Bad public/data/ committed (frontend shows wrong scores).**
The chore commit is on `main`. Revert it:

```bash
git revert <chore-commit-sha>     # creates a "Revert chore: publish ..." commit
git push origin main
```

Cloudflare Pages picks up the revert commit and redeploys within a minute
or two. The previous good `public/data/` content is restored on the live
site. Safe to do at any time; doesn't affect the pipeline.

If you also need to fix the data going forward, run the workflow manually
with `skip_ingest=true` after fixing whatever produced the bad scores
(e.g., a weight tweak in `scoring/buy_score.py`). The new chore commit
will overwrite the reverted state with the corrected report.

**Bad SQLite DB pushed to R2 (subsequent runs use stale/corrupt data).**

* If R2 versioning is enabled (recommended in the setup section):
  Cloudflare dashboard → R2 → your bucket → `dota_deals.db` → Versions →
  restore the previous version. Next workflow run picks up the rolled-
  back state.
* If versioning is not enabled and the DB is unrecoverable: delete the
  R2 object. The next `dota-deals db pull` finds nothing → creates an
  empty local file → the pipeline rebuilds forward from scratch. Loses
  all history; the 30-day warmup window starts over.

Choose versioning during setup. The cost is negligible (a few MB even
with daily snapshots) and the recovery option is real.

## Things not in scope for v1

- **Monitoring beyond GHA's built-in failure emails.** The Actions tab
  shows red ❌ on failure and emails you (if your GitHub notification
  settings allow). No PagerDuty, no Slack — overkill for an 8-hourly
  cron with no SLO.
- **Concurrency lockfiles.** The workflow's `concurrency` block prevents
  two runs from overlapping. R2's two-phase upload pattern means a
  pathological overlap still wouldn't corrupt the canonical key.
- **Multi-environment (staging / prod).** v1 deploys directly to
  `dotadeals.com`. If you ever want a staging slot, the cleanest path is
  a second R2 bucket + a second Pages project on a different branch.
  `R2_DB_KEY` already supports `prod/dota_deals.db` vs
  `staging/dota_deals.db` if you want them sharing a bucket.
