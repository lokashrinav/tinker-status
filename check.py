import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

import httpx

PREFERRED_MODELS = [
    "Qwen/Qwen3-8B",
    "meta-llama/Llama-3.2-3B",
    "Qwen/Qwen3-30B-A3B",
]

OAI_BASE_URL = "https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1"

CHECK_TIMEOUT = 30
SCRIPT_TIMEOUT = 120
ALL_SERVICES = ("reachability", "sampling", "openai_compatible", "training_infra")


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"FATAL: {name} env var is missing or empty", flush=True)
        sys.exit(1)
    return val


SUPABASE_URL = require_env("SUPABASE_URL")
SUPABASE_SERVICE_KEY = require_env("SUPABASE_SERVICE_KEY")


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def pick_model(supported_models: list[str]) -> str | None:
    for pref in PREFERRED_MODELS:
        if pref in supported_models:
            return pref
    return supported_models[0] if supported_models else None


# ── Checks (all HTTP, no gRPC) ─────────────────────────────────────────────


async def check_reachability(client):
    api_key = os.environ.get("TINKER_API_KEY", "")
    try:
        start = time.time()
        resp = await client.get(
            f"{OAI_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        body = resp.json()

        model_ids = [m["id"] for m in body.get("data", [])]
        chosen = pick_model(model_ids)
        return {
            "service": "reachability",
            "status": "up",
            "latency_ms": round((time.time() - start) * 1000, 1),
            "meta": {"supported_models": model_ids, "chosen_model": chosen},
            "_chosen_model": chosen,
        }
    except Exception as e:
        return {
            "service": "reachability",
            "status": "down",
            "error": str(e),
        }


async def check_completions(client, model: str, service_name: str):
    api_key = os.environ.get("TINKER_API_KEY", "")
    try:
        start = time.time()
        resp = await client.post(
            f"{OAI_BASE_URL}/completions",
            json={
                "model": model,
                "prompt": "2+2=",
                "max_tokens": 16,
                "temperature": 0.0,
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        body = resp.json()

        output_text = body["choices"][0]["text"]
        return {
            "service": service_name,
            "status": "up",
            "latency_ms": round((time.time() - start) * 1000, 1),
            "meta": {"response_snippet": output_text[:100]},
        }
    except Exception as e:
        return {
            "service": service_name,
            "status": "down",
            "error": str(e),
        }


async def check_training(client):
    api_key = os.environ.get("TINKER_API_KEY", "")
    try:
        start = time.time()
        resp = await client.get(
            f"{OAI_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
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
    ts = now_utc()
    for r in results:
        meta = {k: v for k, v in (r.get("meta") or {}).items()}
        rows.append({
            "checked_at": ts,
            "service": r["service"],
            "status": r["status"],
            "latency_ms": r.get("latency_ms"),
            "error": r.get("error"),
            "meta": json.dumps(meta) if meta else None,
        })

    try:
        async with httpx.AsyncClient(timeout=30) as client:
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
        print(f"Pushed {len(rows)} rows to Supabase.", flush=True)
    except Exception as e:
        print(f"*** FAILED to push results to Supabase: {e} ***", flush=True)
        print(f"    Rows that would have been pushed: {json.dumps(rows, indent=2)}", flush=True)


def ensure_all_services(results: list[dict], reason: str) -> list[dict]:
    present = {r["service"] for r in results}
    for svc in ALL_SERVICES:
        if svc not in present:
            results.append({"service": svc, "status": "down", "error": reason})
    return results


# ── Main ────────────────────────────────────────────────────────────────────


async def main():
    print("=== Tinker Health Check ===\n", flush=True)

    results = []

    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        # 1. Reachability
        print("[1/4] Reachability ...", flush=True)
        reachability = await check_reachability(client)
        results.append(reachability)
        print(f"      -> {reachability['status']}\n", flush=True)

        if reachability["status"] == "down":
            for svc in ("sampling", "openai_compatible", "training_infra"):
                results.append({"service": svc, "status": "down", "error": "API unreachable"})
        else:
            model = reachability.get("_chosen_model") or PREFERRED_MODELS[0]
            print(f"      using model: {model}\n")

            # 2. Sampling (inference via HTTP completions)
            print("[2/4] Sampling ...", flush=True)
            sampling = await check_completions(client, model, "sampling")
            results.append(sampling)
            print(f"      -> {sampling['status']}\n", flush=True)

            # 3. OpenAI-compatible (same endpoint, separate status)
            print("[3/4] OpenAI-compatible ...", flush=True)
            oai = await check_completions(client, model, "openai_compatible")
            results.append(oai)
            print(f"      -> {oai['status']}\n", flush=True)

            # 4. Training infra (API responsive = infra up)
            print("[4/4] Training infra ...", flush=True)
            training = await check_training(client)
            results.append(training)
            print(f"      -> {training['status']}\n", flush=True)

    results = ensure_all_services(results, "skipped: not reached in check flow")

    print("=== Results ===")
    print(json.dumps(results, indent=2))

    await push_results(results)


async def main_with_timeout():
    try:
        await asyncio.wait_for(main(), timeout=SCRIPT_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"\n*** Script exceeded {SCRIPT_TIMEOUT}s global timeout ***", flush=True)
        results = [
            {"service": svc, "status": "down", "error": f"check script timed out after {SCRIPT_TIMEOUT}s"}
            for svc in ALL_SERVICES
        ]
        await push_results(results)
    except Exception as e:
        print(f"\n*** Unhandled exception in main: {e} ***", flush=True)
        results = [
            {"service": svc, "status": "down", "error": f"unhandled: {e}"}
            for svc in ALL_SERVICES
        ]
        await push_results(results)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main_with_timeout())
    finally:
        os._exit(0)
