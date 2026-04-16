# tinker-status

Uptime monitor for the [Tinker](https://thinkingmachines.ai/tinker) training API. Tinker has no official status page, no community monitoring, and no public status endpoint. This fills that gap.

**Live:** [lokashrinav.github.io/tinker-status](https://lokashrinav.github.io/tinker-status)

---

## What it checks

Four surfaces of the Tinker API, tested independently because they can fail independently:

| Check | What it actually does | Why |
|---|---|---|
| **API** | `get_server_capabilities()` | Cheapest call. Confirms the service is reachable and responding. |
| **Inference** | Sends `"2+2="` to Llama-3.2-1B via `sample_async()` | Tests the full inference pipeline, not just a health endpoint. |
| **OpenAI-Compatible API** | `POST /v1/completions` against the [beta endpoint](https://tinker-docs.thinkingmachines.ai/compatible-apis/openai) | Different code path from the SDK. Many users hit this instead of the native client. |
| **Training** | `create_lora_training_client()` with rank 8 | Verifies the training infra accepts connections and allocates resources. Does not run `forward_backward()` — that would be expensive at 144 checks/day. |

Every call has a 60-second timeout. A hang is treated as downtime.

## Architecture

```
GitHub Actions (cron every 10 min)
        │
        ▼
    check.py ──── Tinker SDK + HTTP ───▶ Tinker API
        │
        ▼
    Supabase (Postgres)
        ▲
        │
    docs/index.html ◀── GitHub Pages
    (reads via anon key, RLS enforced)
```

No servers to manage. The entire thing runs on free tiers — GitHub Actions, Supabase, and GitHub Pages.

## Known limitations

This is a community monitoring tool with honest tradeoffs:

- **GitHub Actions cron is not precise.** `*/10 * * * *` means "roughly every 10 minutes." Under load, GitHub can delay cron jobs by 5–30+ minutes. Short outages between checks are invisible.
- **Single check location.** Runs from GitHub's `ubuntu-latest` runners, likely US-based. Regional issues won't be detected.
- **Training check is shallow.** It only tests client initialization, not the full `forward_backward()` → `optim_step()` loop. A failure in the training pipeline itself would not be caught.
- **OpenAI endpoint URL is hardcoded.** The beta endpoint could change without notice. If Tinker moves the URL, this will report a false outage until updated.
- **No alerting.** This is a dashboard, not a pager. You have to look at it to know something is down.

## Setup (if you want to fork this)

You need three secrets in your GitHub repo (Settings → Secrets → Actions):

| Secret | Where to get it |
|---|---|
| `TINKER_API_KEY` | [Tinker Console](https://tinker-console.thinkingmachines.ai/) |
| `SUPABASE_URL` | Supabase dashboard → Settings → API |
| `SUPABASE_SERVICE_KEY` | Same page — the `service_role` key (not `anon`) |

Then run `supabase_schema.sql` in your Supabase SQL Editor to create the table. The schema enforces RLS: the anon key can only read, writes require the service role key.

The status page deploys via GitHub Pages from the `/docs` folder.

## Cost

- **GitHub Actions:** Free (public repo).
- **Supabase:** Free tier. ~576 rows/day (4 services × 144 checks).
- **Tinker:** The API, inference, and OpenAI checks consume tokens. The training check allocates GPU resources briefly. Monitor your [Tinker billing](https://tinker-console.thinkingmachines.ai/) after the first 24 hours.
