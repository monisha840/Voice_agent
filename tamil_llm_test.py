"""
Ask the same 10 everyday-topic Tamil questions to two Ollama models and record
the raw results - answer text, reply length, tokens/sec, and timing - to a
JSON file. This script does NOT judge Tamil quality; that's for a human
reviewer. It only measures what's objectively measurable.

Questions are simple everyday topics (weather, food, time, sleep, etc.)
expecting short answers, matching how a voice assistant would actually be used.

Each request uses the same minimal system prompt ("reply only in Tamil, in
1-2 short sentences") so both models get an identical, fair instruction.

Uses Ollama's non-streaming /v1/chat/completions endpoint - verified it
returns a "usage" field with completion_tokens (checked with a raw curl
before writing this), which is what tokens/sec is computed from.

Run:
    uv run python tamil_llm_test.py
"""

import asyncio
import json
import time
from pathlib import Path

import aiohttp

from bot import OLLAMA_BASE_URL

MODELS = ["qwen3:4b-instruct-2507-q4_K_M", "gemma3:4b"]

SYSTEM_PROMPT = "Reply only in Tamil, in 1-2 short sentences."

QUESTIONS = [
    "இன்று வானிலை எப்படி இருக்கிறது?",
    "உங்களுக்கு பிடித்த உணவு எது?",
    "இப்போது நேரம் என்ன?",
    "நீங்கள் எந்த ஊரில் இருந்து வருகிறீர்கள்?",
    "உங்கள் விருப்பமான நிறம் எது?",
    "காலை உணவிற்கு என்ன சாப்பிடலாம்?",
    "வார இறுதி நாட்களில் நீங்கள் என்ன செய்வீர்கள்?",
    "தண்ணீர் குடிப்பது ஏன் முக்கியம்?",
    "உங்களுக்கு பிடித்த பருவகாலம் எது?",
    "நல்ல தூக்கத்திற்கு என்ன செய்ய வேண்டும்?",
]

OUT_PATH = Path("tamil_llm_results.json")


async def ask(session: aiohttp.ClientSession, model: str, question: str) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "stream": False,
    }
    url = OLLAMA_BASE_URL.rstrip("/") + "/chat/completions"

    start = time.perf_counter()
    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as r:
        r.raise_for_status()
        body = await r.json()
    elapsed = time.perf_counter() - start

    answer = body["choices"][0]["message"]["content"].strip()
    completion_tokens = body.get("usage", {}).get("completion_tokens")
    tokens_per_sec = (completion_tokens / elapsed) if completion_tokens else None

    return {
        "model": model,
        "question": question,
        "answer": answer,
        "reply_length_chars": len(answer),
        "completion_tokens": completion_tokens,
        "elapsed_secs": round(elapsed, 3),
        "tokens_per_sec": round(tokens_per_sec, 2) if tokens_per_sec else None,
    }


async def main() -> None:
    results = []
    async with aiohttp.ClientSession() as session:
        for model in MODELS:
            print(f"\n=== {model} ===")
            for i, question in enumerate(QUESTIONS, start=1):
                record = await ask(session, model, question)
                results.append(record)
                print(
                    f"[{i:2d}] {question}\n"
                    f"     -> {record['answer']}\n"
                    f"     chars={record['reply_length_chars']} "
                    f"tokens={record['completion_tokens']} "
                    f"tok/s={record['tokens_per_sec']} "
                    f"time={record['elapsed_secs']}s"
                )

    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved {len(results)} records to {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
