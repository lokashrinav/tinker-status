import asyncio
import json
import os
import time
from datetime import datetime, timezone

import httpx
import tinker
from tinker import types

BASE_MODEL = "meta-llama/Llama-3.2-1B"
OAI_BASE_URL = "https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]


def now_utc():
    return datetime.now(timezone.utc).isoformat()


# ── Checks ──────────────────────────────────────────────────────────────────


def check_reachability(service_client):
    try:
        start = time.time()
        capabilities = service_client.get_server_capabilities()
        return {
            "service": "reachability",
            "status": "up",
            "latency_ms": round((time.time() - start) * 1000, 1),
            "meta": {"supported_models": capabilities.supported_models},
        }
    except Exception as e:
        return {
            "service": "reachability",
            "status": "down",
            "error": str(e),
        }


async def check_sampling(service_client):
    try:
        start = time.time()
        sampling_client = service_client.create_sampling_client(base_model=BASE_MODEL)
        tokenizer = sampling_client.get_tokenizer()

        prompt = types.ModelInput.from_ints(tokenizer.encode("2+2="))
        params = types.SamplingParams(max_tokens=16, temperature=0.0)
        result = await sampling_client.sample_async(
            prompt=prompt, num_samples=1, sampling_params=params
        )

        output_text = tokenizer.decode(result.sequences[0].tokens)
        return {
            "service": "sampling",
            "status": "up",
            "latency_ms": round((time.time() - start) * 1000, 1),
            "meta": {"response_snippet": output_text[:100]},
        }
    except Exception as e:
        return {
            "service": "sampling",
            "status": "down",
            "error": str(e),
        }


async def check_openai_compatible():
    api_key = os.environ.get("TINKER_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": BASE_MODEL,
        "prompt": "2+2=",
        "max_tokens": 16,
        "temperature": 0.0,
    }

    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{OAI_BASE_URL}/completions", json=payload, headers=headers
            )
            resp.raise_for_status()
            body = resp.json()

        output_text = body["choices"][0]["text"]
        return {
            "service": "openai_compatible",
            "status": "up",
            "latency_ms": round((time.time() - start) * 1000, 1),
            "meta": {"response_snippet": output_text[:100]},
        }
    except Exception as e:
        return {
            "service": "openai_compatible",
            "status": "down",
            "error": str(e),
        }


def check_training_client(service_client):
    try:
        start = time.time()
        service_client.create_lora_training_client(
            base_model=BASE_MODEL, rank=8
        )
        return {
            "service": "training_infra",
            "status": "up",
            "latency_ms": round((time.time() - start) * 1000, 1),
        }
    except Exception as e:
        return {
            "service": "training_infra",
            "status": "down",
            "error": str(e),
        }


# ── Supabase writer ────────────────────────────────────────────────────────


async def push_results(results: list[dict]):
    rows = []
    for r in results:
        rows.append({
            "checked_at": now_utc(),
            "service": r["service"],
            "status": r["status"],
            "latency_ms": r.get("latency_ms"),
            "error": r.get("error"),
            "meta": json.dumps(r.get("meta")) if r.get("meta") else None,
        })

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/check_results",
            json=rows,
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
        resp.raise_for_status()
    print(f"Pushed {len(rows)} rows to Supabase.")


# ── Main ────────────────────────────────────────────────────────────────────


async def main():
    service_client = tinker.ServiceClient()
    results = []

    print("=== Tinker Health Check ===\n")

    print("[1/4] Reachability (get_server_capabilities) ...")
    reachability = check_reachability(service_client)
    results.append(reachability)
    print(f"      -> {reachability['status']}\n")

    if reachability["status"] == "down":
        print("Service unreachable — skipping remaining checks.")
        for svc in ("sampling", "openai_compatible", "training_infra"):
            results.append({"service": svc, "status": "down", "error": "skipped: service unreachable"})
    else:
        print("[2/4] Sampling (Llama-3.2-1B, prompt='2+2=') ...")
        sampling = await check_sampling(service_client)
        results.append(sampling)
        print(f"      -> {sampling['status']}\n")

        print("[3/4] OpenAI-compatible endpoint (/completions) ...")
        oai = await check_openai_compatible()
        results.append(oai)
        print(f"      -> {oai['status']}\n")

        print("[4/4] Training infra (create_lora_training_client) ...")
        training = check_training_client(service_client)
        results.append(training)
        print(f"      -> {training['status']}\n")

    print("=== Results ===")
    print(json.dumps(results, indent=2))

    await push_results(results)


if __name__ == "__main__":
    asyncio.run(main())
