"""
Send typed text through the same Ollama model and Piper voice bot.py uses,
and save each reply as a .wav file - a way to test the LLM + TTS legs of the
pipeline without a microphone or the WebRTC Playground.

bot.py's pipeline has no RTVIProcessor, so the Playground's WebRTC session
has no path for typed text to reach the LLM (see CLAUDE.md Status). This
script talks to Ollama and Piper directly over HTTP instead, using the same
config, system prompt and model settings as bot.py (imported from it, not
duplicated) so the numbers are representative of what the bot would produce.

The Ollama call streams (verified against Ollama's actual SSE output: `data:
{...}` chunks in OpenAI's delta format, ending `data: [DONE]`), so it reports:

  - time_to_first_token: first non-empty streamed content chunk.
  - time_to_first_sentence: first point the streamed text forms a complete
    sentence, found with pipecat's own match_endofsentence() (the same
    sentence-boundary logic PiperHttpTTSService uses internally to decide
    when to start speaking), not a hand-rolled regex.
  - time_to_first_audio: wall-clock time until the first sentence's WAV is
    ready. TTS for that sentence starts as soon as it's detected in the
    stream and runs concurrently with the rest of the LLM output still
    arriving (an asyncio task, same idea as bot.py's real pipeline starting
    TTS on sentence 1 while the LLM keeps generating sentence 2+), so this
    is usually less than time_to_first_sentence + that sentence's TTS time.
    Piper's HTTP server returns a complete WAV per request (no chunked audio
    streaming), so this is the earliest point audio could start playing.

The full reply is also synthesized as one final Piper call and saved to
test_audio/reply_NN.wav, same as before.

Run:
    uv run python text_chat_test.py
    uv run python text_chat_test.py "Custom sentence one" "Custom sentence two"
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import aiohttp

from bot import MAX_REPLY_TOKENS, OLLAMA_BASE_URL, OLLAMA_MODEL, PIPER_URL, PIPER_VOICE, SYSTEM_PROMPT
from pipecat.utils.string import match_endofsentence

OUT_DIR = Path("test_audio")

DEFAULT_SENTENCES = [
    "What's the capital of France?",
    "Give me a one-sentence tip for staying focused while working.",
    "Tell me a fun fact about octopuses.",
]


async def synthesize(session: aiohttp.ClientSession, text: str) -> tuple[bytes, float]:
    start = time.perf_counter()
    async with session.post(
        PIPER_URL,
        json={"text": text, "voice": PIPER_VOICE},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as r:
        r.raise_for_status()
        audio = await r.read()
    elapsed = time.perf_counter() - start
    return audio, elapsed


async def _timed_synthesize(
    session: aiohttp.ClientSession, text: str, turn_start: float
) -> tuple[bytes, float, float]:
    """Synthesize `text` and also report the elapsed time since `turn_start`
    at the moment synthesis actually finished - not when the caller gets
    around to awaiting the task, which matters since this runs concurrently
    with continued LLM streaming."""
    audio, tts_secs = await synthesize(session, text)
    return audio, tts_secs, time.perf_counter() - turn_start


async def stream_llm_reply(session: aiohttp.ClientSession, text: str, turn_start: float) -> dict:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "max_tokens": MAX_REPLY_TOKENS,
        "temperature": 0.6,
        "stream": True,
    }
    url = OLLAMA_BASE_URL.rstrip("/") + "/chat/completions"

    ttft = None
    first_sentence = None
    t_first_sentence = None
    first_sentence_task: asyncio.Task | None = None
    full_text = ""

    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as r:
        r.raise_for_status()
        async for raw_line in r.content:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            data = line[len("data: ") :]
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            content = chunk["choices"][0].get("delta", {}).get("content")
            if not content:
                continue
            if ttft is None:
                ttft = time.perf_counter() - turn_start
            full_text += content
            if first_sentence is None:
                end = match_endofsentence(full_text)
                if end > 0:
                    first_sentence = full_text[:end].strip()
                    t_first_sentence = time.perf_counter() - turn_start
                    # Kick off TTS for the first sentence now, concurrently
                    # with the rest of the LLM stream still arriving - this
                    # is what bot.py's real pipeline does (TTS starts on
                    # sentence 1 while the LLM keeps generating sentence 2+).
                    first_sentence_task = asyncio.create_task(
                        _timed_synthesize(session, first_sentence, turn_start)
                    )

    llm_total = time.perf_counter() - turn_start
    full_text = full_text.strip()
    if first_sentence_task is None:
        # Reply never hit a detected sentence boundary (e.g. a very short
        # reply with no terminal punctuation) - treat the whole thing as
        # "the first sentence" and synthesize it now.
        first_sentence = full_text
        t_first_sentence = llm_total
        first_sentence_task = asyncio.create_task(
            _timed_synthesize(session, first_sentence, turn_start)
        )

    _, first_sentence_tts_secs, time_to_first_audio = await first_sentence_task

    return {
        "full_text": full_text,
        "first_sentence": first_sentence,
        "ttft": ttft,
        "t_first_sentence": t_first_sentence,
        "llm_total": llm_total,
        "first_sentence_tts_secs": first_sentence_tts_secs,
        "time_to_first_audio": time_to_first_audio,
    }


async def main() -> None:
    sentences = sys.argv[1:] or DEFAULT_SENTENCES
    OUT_DIR.mkdir(exist_ok=True)

    async with aiohttp.ClientSession() as session:
        for i, sentence in enumerate(sentences, start=1):
            print(f"\n[{i}] User: {sentence}")

            turn_start = time.perf_counter()
            llm = await stream_llm_reply(session, sentence, turn_start)
            print(f"    Bot:  {llm['full_text']}")
            print(f"    time_to_first_token={llm['ttft']:.2f}s")
            print(
                f"    time_to_first_sentence={llm['t_first_sentence']:.2f}s "
                f"(\"{llm['first_sentence']}\")"
            )
            print(
                f"    time_to_first_audio={llm['time_to_first_audio']:.2f}s "
                f"(first-sentence TTS took {llm['first_sentence_tts_secs']:.2f}s, "
                "run concurrently with the rest of the LLM stream)"
            )
            print(f"    llm_total={llm['llm_total']:.2f}s")

            audio, tts_secs = await synthesize(session, llm["full_text"])
            out_path = OUT_DIR / f"reply_{i:02d}.wav"
            out_path.write_bytes(audio)
            print(f"    tts_total={tts_secs:.2f}s  -> {out_path} ({len(audio)} bytes)")
            print(f"    total={time.perf_counter() - turn_start:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
