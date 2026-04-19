# tinker-status

Uptime monitor for the [Tinker](https://thinkingmachines.ai/tinker) API. I got tired of not knowing whether my code was broken or Tinker was down, so I built this.

**Live:** [lokashrinav.github.io/tinker-status](https://lokashrinav.github.io/tinker-status)

## What it checks

On each run (see **Scheduling** below), four things get hit independently:

- **API reachability** calls `get_server_capabilities()`. Cheapest call. If this fails, everything else is skipped.
- **Inference** sends `"2+2="` to Llama-3.2-1B via `sample_async()`. Tests the whole inference pipeline, not just a ping.
- **OpenAI-compatible endpoint** hits `POST /v1/completions` against the [beta endpoint](https://tinker-docs.thinkingmachines.ai/compatible-apis/openai). Different code path from the SDK, and a lot of people actually use this one.
- **Training** calls `create_lora_training_client()` with rank 8. Doesn't actually run `forward_backward()` because doing that 144 times a day would cost real money. Just confirms the training infra will accept a client.

60s timeout on every call. Hangs count as down.

## How it works

GitHub Actions runs `check.py` when the workflow is triggered. The script hits the Tinker SDK and writes results to a Supabase Postgres table. The status page is a static HTML file on GitHub Pages that reads from Supabase using the anon key (read only, RLS enforced). No servers, no backend, everything on free tiers.

## Scheduling

This repo does **not** use GitHub’s `schedule` trigger: on the free tier it often runs late or skips runs. Instead, trigger the workflow on a timer you control.

**Until you configure Option A below, checks only run when you trigger the workflow manually** (Actions → Run workflow).

**Option A — External HTTP cron (recommended, $0)**  
Use any service that can send a `POST` on an interval (many people use [cron-job.org](https://cron-job.org); it’s an established, donation-funded scheduler with an open-source history—use only if you’re comfortable with their terms).

1. Create a **classic** personal access token with the **`workflow`** scope (or a **fine-grained** token for this repo only with **Actions: Read and write**).
2. Add a scheduled **HTTPS** job that runs every 5–10 minutes:

   - **URL:** `https://api.github.com/repos/<OWNER>/<REPO>/actions/workflows/check.yml/dispatches`
   - **Method:** `POST`
   - **Header:** `Authorization: Bearer <YOUR_PAT>`
   - **Header:** `Accept: application/vnd.github+json`
   - **Header:** `X-GitHub-Api-Version: 2022-11-28` (optional but good practice)
   - **Body (JSON):** `{"ref":"main"}` (use your default branch name if not `main`)
   - **Header:** `Content-Type: application/json` (required when sending a JSON body)

3. Store the PAT only in the cron provider’s secret field, not in the repo.

**Troubleshooting:** If you see **404** from GitHub, the request is almost always **GET** instead of **POST**. The dispatch URL does not support GET; cron-job.org must use **POST** with the JSON body (see their FAQ: custom HTTP methods). Use workflow id `261565601` in the URL if `check.yml` in the path misbehaves:  
`https://api.github.com/repos/lokashrinav/tinker-status/actions/workflows/261565601/dispatches`

**Option B — Manual**  
In GitHub: **Actions → Tinker Health Check → Run workflow**.

After you set Option A up, runs stay on a steady interval; the status page’s 24h bars still aggregate hourly.

## Stuff that's broken or not great

- If GitHub Actions is down, checks won’t run and the page can look stale—same as any GH-hosted monitor.
- One check location (GitHub's runners, somewhere in the US). Regional outages won't show up.
- Training check is shallow. Client init works does not mean the training loop works.
- The OpenAI endpoint URL is hardcoded. If Tinker moves it, this will throw false red until I notice.
- No alerting yet. You have to look at the page.

## Fork it

Three GitHub secrets:

| Secret | Where |
|---|---|
| `TINKER_API_KEY` | [Tinker Console](https://tinker-console.thinkingmachines.ai/) |
| `SUPABASE_URL` | Supabase dashboard, Settings, API |
| `SUPABASE_SERVICE_KEY` | Same page, the `service_role` key |

Run `supabase_schema.sql` in the Supabase SQL Editor. RLS is on so the anon key can only read and the service key handles writes. Status page deploys from `/docs` via GitHub Pages.

## Cost

GitHub Actions is free within limits. Supabase free tier is plenty for frequent checks. Tinker usage stays small because the training step only creates a LoRA client (no full training loop).
