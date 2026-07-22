import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

import httpx
import tinker
from tinker import types
from tinker.lib.retry_handler import RetryConfig

PREFERRED_MODELS = [
    "Qwen/Qwen3-8B",
    "meta-llama/Llama-3.2-3B",
    "Qwen/Qwen3-30B-A3B",
]

NO_RETRY = RetryConfig(enable_retry_logic=False)

OAI_BASE_URL = "https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1"

CHECK_TIMEOUT = 60
SCRIPT_TIMEOUT = 240
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


# ── Checks ──────────────────────────────────────────────────────────────────


async def check_reachability():
    api_key = os.environ.get("TINKER_API_KEY", "")
    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
            resp = await client.get(
                f"{OAI_BASE_URL}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            body = resp.json()

        print(f"      /models response keys: {list(body.keys()) if isinstance(body, dict) else type(body).__name__}", flush=True)
        if isinstance(body, dict):
            for k, v in body.items():
                if isinstance(v, list):
                    print(f"      {k}: {len(v)} items", flush=True)
                    if v:
                        print(f"      first item keys: {list(v[0].keys()) if isinstance(v[0], dict) else v[0]}", flush=True)

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


async def check_sampling(service_client, model):
    sampling_client = None
    tokenizer = None
    loop = asyncio.get_running_loop()
    try:
        sampling_client = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: service_client.create_sampling_client(
                    base_model=model, retry_config=NO_RETRY,
                ),
            ),
            timeout=CHECK_TIMEOUT,
        )
        tokenizer = await asyncio.wait_for(
            loop.run_in_executor(None, sampling_client.get_tokenizer),
            timeout=CHECK_TIMEOUT,
        )
    except Exception as e:
        return {
            "service": "sampling",
            "status": "down",
            "error": f"client/tokenizer setup failed: {e}",
        }

    try:
        start = time.time()
        prompt = types.ModelInput.from_ints(tokenizer.encode("2+2="))
        params = types.SamplingParams(max_tokens=16, temperature=0.0)
        def _sample():
            return sampling_client.sample(
                prompt=prompt, num_samples=1, sampling_params=params
            )
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _sample),
            timeout=CHECK_TIMEOUT,
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


async def check_openai_compatible(model: str):
    api_key = os.environ.get("TINKER_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "prompt": "2+2=",
        "max_tokens": 16,
        "temperature": 0.0,
    }

    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
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


async def check_training_client(service_client, model: str):
    try:
        start = time.time()
        loop = asyncio.get_running_loop()
        def _create():
            return service_client.create_lora_training_client(
                base_model=model, rank=8
            )
        await asyncio.wait_for(
            loop.run_in_executor(None, _create),
            timeout=CHECK_TIMEOUT,
        )
        latency = round((time.time() - start) * 1000, 1)
        return {
            "service": "training_infra",
            "status": "up",
            "latency_ms": latency,
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
    """Guarantee every service has exactly one result row."""
    present = {r["service"] for r in results}
    for svc in ALL_SERVICES:
        if svc not in present:
            results.append({"service": svc, "status": "down", "error": reason})
    return results


# ── Main ────────────────────────────────────────────────────────────────────


async def main():
    print("=== Tinker Health Check ===\n", flush=True)

    loop = asyncio.get_running_loop()
    results = []

    # 1. Reachability — HTTP /models (no gRPC dependency)
    print("[1/4] Reachability ...", flush=True)
    reachability = await check_reachability()
    results.append(reachability)
    print(f"      -> {reachability['status']}\n", flush=True)

    model = reachability.get("_chosen_model") if reachability["status"] == "up" else None

    if not model:
        print("No model available — marking remaining checks as down.\n", flush=True)
        for svc in ("sampling", "openai_compatible", "training_infra"):
            results.append({"service": svc, "status": "down", "error": "no model available"})
        results = ensure_all_services(results, "skipped: not reached in check flow")
        print("=== Results ===")
        print(json.dumps(results, indent=2))
        await push_results(results)
        return

    print(f"      using model: {model}\n")

    # 2. OpenAI-compatible — HTTP, independent of gRPC
    print("[2/4] OpenAI-compatible ...", flush=True)
    oai = await check_openai_compatible(model)
    results.append(oai)
    print(f"      -> {oai['status']}\n", flush=True)

    # 3-4. gRPC-dependent checks (sampling + training)
    service_client = None
    try:
        print("Connecting gRPC client ...", flush=True)
        service_client = await asyncio.wait_for(
            loop.run_in_executor(None, tinker.ServiceClient),
            timeout=CHECK_TIMEOUT,
        )
        print("      connected.\n", flush=True)
    except Exception as e:
        print(f"      gRPC connection failed: {e}\n", flush=True)

    if service_client:
        print("[3/4] Sampling ...", flush=True)
        sampling = await check_sampling(service_client, model)
        results.append(sampling)
        print(f"      -> {sampling['status']}\n", flush=True)

        print("[4/4] Training infra ...", flush=True)
        training = await check_training_client(service_client, model)
        results.append(training)
        print(f"      -> {training['status']}\n", flush=True)
    else:
        print("[3/4] Sampling ... -> skipped (gRPC unavailable)", flush=True)
        results.append({"service": "sampling", "status": "down", "error": "gRPC connection failed"})
        print("[4/4] Training infra ... -> skipped (gRPC unavailable)", flush=True)
        results.append({"service": "training_infra", "status": "down", "error": "gRPC connection failed"})

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
