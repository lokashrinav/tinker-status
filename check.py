import asyncio
import json
import os
import time
from datetime import datetime, timezone

import httpx
import tinker
from tinker import types
from tinker.lib.retry_handler import RetryConfig

BASE_MODEL = "meta-llama/Llama-3.2-1B"

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


# ── Checks ──────────────────────────────────────────────────────────────────


async def check_reachability(service_client):
    try:
        start = time.time()
        capabilities = await with_timeout(
            service_client.get_server_capabilities_async()
        )
        model_names = [str(m) for m in capabilities.supported_models]
        return {
            "service": "reachability",
            "status": "up",
            "latency_ms": round((time.time() - start) * 1000, 1),
            "meta": {"supported_models": model_names},
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


async def check_training_client(service_client):
    try:
        start = time.time()
        training_client = await with_timeout(
            service_client.create_lora_training_client_async(
                base_model=BASE_MODEL, rank=8
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
    try:
        results = []

        # Pre-create sampling client (retries disabled) and download the
        # tokenizer before the timed checks so a slow HuggingFace download
        # doesn't count against the inference health check.
        print("Preparing sampling client + tokenizer ...")
        sampling_client = service_client.create_sampling_client(
            base_model=BASE_MODEL, retry_config=NO_RETRY,
        )
        tokenizer = sampling_client.get_tokenizer()
        print("      done.\n")

        print("=== Tinker Health Check ===\n")

        print("[1/4] Reachability ...")
        reachability = await check_reachability(service_client)
        results.append(reachability)
        print(f"      -> {reachability['status']}\n")

        if reachability["status"] == "down":
            print("Service unreachable — skipping remaining checks.")
            for svc in ("sampling", "openai_compatible", "training_infra"):
                results.append({"service": svc, "status": "down", "error": "skipped: service unreachable"})
        else:
            print("[2/4] Sampling ...")
            sampling = await check_sampling(sampling_client, tokenizer)
            results.append(sampling)
            print(f"      -> {sampling['status']}\n")

            print("[3/4] OpenAI-compatible ...")
            oai = await check_openai_compatible()
            results.append(oai)
            print(f"      -> {oai['status']}\n")

            print("[4/4] Training infra ...")
            training = await check_training_client(service_client)
            results.append(training)
            print(f"      -> {training['status']}\n")

        print("=== Results ===")
        print(json.dumps(results, indent=2))

        await push_results(results)
    finally:
        try:
            service_client.holder.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
