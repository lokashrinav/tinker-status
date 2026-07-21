import asyncio
import json
import os
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

# Documented at https://tinker-docs.thinkingmachines.ai/compatible-apis/openai
# Still labeled beta — could change without notice.
OAI_BASE_URL = "https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

CHECK_TIMEOUT = 60


def now_utc():
    return datetime.now(timezone.utc).isoformat()


async def with_timeout(coro, timeout=CHECK_TIMEOUT):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Timed out after {timeout}s")


def pick_model(supported_models: list[str]) -> str | None:
    for pref in PREFERRED_MODELS:
        if pref in supported_models:
            return pref
    return supported_models[0] if supported_models else None


# ── Checks ──────────────────────────────────────────────────────────────────


async def check_reachability(service_client):
    try:
        start = time.time()
        capabilities = await with_timeout(
            service_client.get_server_capabilities_async()
        )
        model_names = [m.model_name if hasattr(m, 'model_name') else str(m) for m in capabilities.supported_models]
        chosen = pick_model(model_names)
        return {
            "service": "reachability",
            "status": "up",
            "latency_ms": round((time.time() - start) * 1000, 1),
            "meta": {"supported_models": model_names, "chosen_model": chosen},
            "_chosen_model": chosen,
        }
    except Exception as e:
        return {
            "service": "reachability",
            "status": "down",
            "error": str(e),
        }


async def check_sampling(sampling_client, tokenizer):
    try:
        start = time.time()
        prompt = types.ModelInput.from_ints(tokenizer.encode("2+2="))
        params = types.SamplingParams(max_tokens=16, temperature=0.0)
        result = await with_timeout(
            sampling_client.sample_async(
                prompt=prompt, num_samples=1, sampling_params=params
            )
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
        training_client = await with_timeout(
            service_client.create_lora_training_client_async(
                base_model=model, rank=8
            )
        )
        latency = round((time.time() - start) * 1000, 1)
        # Release the training queue slot immediately so we don't hold
        # resources that block other users.
        try:
            training_client.holder.close()
        except Exception:
            pass
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
    for r in results:
        meta = {k: v for k, v in (r.get("meta") or {}).items()}
        rows.append({
            "checked_at": now_utc(),
            "service": r["service"],
            "status": r["status"],
            "latency_ms": r.get("latency_ms"),
            "error": r.get("error"),
            "meta": json.dumps(meta) if meta else None,
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


SCRIPT_TIMEOUT = 240


async def main():
    print("=== Tinker Health Check ===\n", flush=True)

    service_client = None
    try:
        print("Connecting to Tinker ...", flush=True)
        service_client = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, tinker.ServiceClient),
            timeout=CHECK_TIMEOUT,
        )
        print("      connected.\n", flush=True)
    except Exception as e:
        print(f"      ServiceClient failed: {e}\n", flush=True)
        results = [
            {"service": svc, "status": "down", "error": f"ServiceClient init failed: {e}"}
            for svc in ("reachability", "sampling", "openai_compatible", "training_infra")
        ]
        print("=== Results ===")
        print(json.dumps(results, indent=2))
        await push_results(results)
        return

    try:
        results = []

        print("[1/4] Reachability ...", flush=True)
        reachability = await check_reachability(service_client)
        results.append(reachability)
        print(f"      -> {reachability['status']}\n", flush=True)

        if reachability["status"] == "down":
            print("Service unreachable — skipping remaining checks.")
            for svc in ("sampling", "openai_compatible", "training_infra"):
                results.append({"service": svc, "status": "down", "error": "skipped: service unreachable"})
        else:
            model = reachability.get("_chosen_model")
            if not model:
                print("No models available — skipping model-dependent checks.")
                for svc in ("sampling", "openai_compatible", "training_infra"):
                    results.append({"service": svc, "status": "down", "error": "no models available on server"})
            else:
                print(f"      using model: {model}\n")

                print("Preparing sampling client + tokenizer ...", flush=True)
                sampling_client = None
                tokenizer = None
                try:
                    sampling_client = service_client.create_sampling_client(
                        base_model=model, retry_config=NO_RETRY,
                    )
                    tokenizer = sampling_client.get_tokenizer()
                    print("      done.\n", flush=True)
                except Exception as e:
                    print(f"      tokenizer download failed (likely HF rate-limit): {e}\n")
                    print("      will skip sampling check.\n")

                print("[2/4] Sampling ...", flush=True)
                if sampling_client and tokenizer:
                    sampling = await check_sampling(sampling_client, tokenizer)
                    results.append(sampling)
                    print(f"      -> {sampling['status']}\n", flush=True)
                else:
                    print("      -> skipped (tokenizer unavailable, not a Tinker issue)\n")

                print("[3/4] OpenAI-compatible ...", flush=True)
                oai = await check_openai_compatible(model)
                results.append(oai)
                print(f"      -> {oai['status']}\n", flush=True)

                print("[4/4] Training infra ...", flush=True)
                training = await check_training_client(service_client, model)
                results.append(training)
                print(f"      -> {training['status']}\n", flush=True)

        print("=== Results ===")
        print(json.dumps(results, indent=2))

        await push_results(results)
    finally:
        try:
            service_client.holder.close()
        except Exception:
            pass


async def main_with_timeout():
    try:
        await asyncio.wait_for(main(), timeout=SCRIPT_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"\n*** Script exceeded {SCRIPT_TIMEOUT}s global timeout ***", flush=True)
        results = [
            {"service": svc, "status": "down", "error": f"check script timed out after {SCRIPT_TIMEOUT}s"}
            for svc in ("reachability", "sampling", "openai_compatible", "training_infra")
        ]
        await push_results(results)


if __name__ == "__main__":
    asyncio.run(main_with_timeout())
