# tinker-status

Uptime monitor for the [Tinker](https://thinkingmachines.ai/tinker) API. I got tired of not knowing whether my code was broken or Tinker was down, so I built this.

**Live:** [lokashrinav.github.io/tinker-status](https://lokashrinav.github.io/tinker-status)

## What it checks

Every 10 minutes, four independent checks run:

| Check | What it does |
|---|---|
| **API** | `get_server_capabilities()` — cheapest call, gate for the rest |
| **Inference** | Sends `"2+2="` to Llama-3.2-1B via `sample_async()` |
| **OpenAI-compatible** | `POST /v1/completions` against the [beta endpoint](https://tinker-docs.thinkingmachines.ai/compatible-apis/openai) |
| **Training** | `create_lora_training_client()` with rank 8 (init only, no actual training) |

60s timeout on every call. Hangs count as down. If API fails, the rest are skipped and marked down.

## Architecture

```
GitHub Actions (cron */10)  →  check.py  →  Supabase Postgres
                                                    ↓
Static HTML on GitHub Pages  ←  get_status_summary() RPC (one call)
```

- **check.py** hits the Tinker SDK, writes rows to Supabase via the service key.
- **get_status_summary()** is a Postgres function that aggregates ticks, uptime, latency percentiles, and incidents server-side.
- **Status page** calls the RPC with the anon key (read-only, RLS enforced) and renders everything client-side. One HTTP request, no pagination.

No servers, no backend beyond Supabase, everything on free tiers.

## Scheduling

The workflow runs on GitHub's `schedule` trigger every 10 minutes. GitHub may occasionally delay or skip a run under load. An external HTTP cron (e.g. [cron-job.org](https://cron-job.org)) can serve as a backup — see the workflow dispatch setup below.

<details>
<summary>Optional: external cron backup</summary>

1. Create a PAT with the `workflow` scope.
2. Set up a `POST` every 5–10 min:
   - **URL:** `https://api.github.com/repos/<OWNER>/<REPO>/actions/workflows/check.yml/dispatches`
   - **Headers:** `Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`, `Content-Type: application/json`
   - **Body:** `{"ref":"main"}`
3. Store the PAT only in the cron provider's secret field.

If both schedule and external cron fire, you get a duplicate row — harmless.
</details>

## Known limitations

- Single US-based check location (GitHub runners). Regional issues won't show.
- Training check only confirms client init, not the full training pipeline.
- OpenAI endpoint URL is hardcoded. If Tinker moves it, false red until updated.
- No alerting — you have to look at the page.

## Fork it

Three GitHub secrets:

| Secret | Where |
|---|---|
| `TINKER_API_KEY` | [Tinker Console](https://tinker-console.thinkingmachines.ai/) |
| `SUPABASE_URL` | Supabase dashboard → Settings → API |
| `SUPABASE_SERVICE_KEY` | Same page, the `service_role` key |

Then in the Supabase SQL Editor, run `supabase_schema.sql` (table + RLS) then `supabase_functions.sql` (aggregation RPC). Deploy the status page from `/docs` via GitHub Pages.

## Cost

Everything fits within free tiers: GitHub Actions, Supabase, GitHub Pages. Tinker usage is minimal — the training step only inits a LoRA client.
