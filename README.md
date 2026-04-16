# tinker-status

Uptime monitor for the [Tinker](https://thinkingmachines.ai/tinker) API. I got tired of not knowing whether my code was broken or Tinker was down, so I built this.

**Live:** [lokashrinav.github.io/tinker-status](https://lokashrinav.github.io/tinker-status)

## What it checks

Every 10 minutes, four things get hit independently:

- **API reachability** calls `get_server_capabilities()`. Cheapest call. If this fails, everything else is skipped.
- **Inference** sends `"2+2="` to Llama-3.2-1B via `sample_async()`. Tests the whole inference pipeline, not just a ping.
- **OpenAI-compatible endpoint** hits `POST /v1/completions` against the [beta endpoint](https://tinker-docs.thinkingmachines.ai/compatible-apis/openai). Different code path from the SDK, and a lot of people actually use this one.
- **Training** calls `create_lora_training_client()` with rank 8. Doesn't actually run `forward_backward()` because doing that 144 times a day would cost real money. Just confirms the training infra will accept a client.

60s timeout on every call. Hangs count as down.

## How it works

GitHub Actions runs `check.py` on a cron schedule. The script hits the Tinker SDK and writes results to a Supabase Postgres table. The status page is a static HTML file on GitHub Pages that reads from Supabase using the anon key (read only, RLS enforced). No servers, no backend, everything on free tiers.

## Stuff that's broken or not great

- GitHub Actions cron drifts. `*/10 * * * *` really means "roughly every 10 minutes, sometimes 30." If you need real monitoring, use a real monitoring tool.
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

GitHub Actions is free. Supabase free tier handles the ~576 rows/day easily. Tinker usage is negligible at this check frequency.
