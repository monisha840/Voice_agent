"""
Synthesize 5 Tamil sentences with Piper's Tamil voice (ta_IN-rasa_female-medium)
on the WSL2 server, saving each to test_audio/tamil_XX.wav and reporting timing.

Run:
    uv run python tamil_tts_test.py
"""

import asyncio
import time
from pathlib import Path

import aiohttp

from bot import PIPER_URL

TAMIL_VOICE = "ta_IN-rasa_female-medium"

SENTENCES = [
    "வணக்கம், எப்படி இருக்கிறீர்கள்?",
    "இன்று நல்ல வானிலை உள்ளது.",
    "தமிழ் ஒரு அழகான மொழி.",
    "தயவுசெய்து தண்ணீர் கொடுங்கள்.",
    "நான் உங்களுக்கு உதவ விரும்புகிறேன்.",
]

OUT_DIR = Path("test_audio")


async def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)

    async with aiohttp.ClientSession() as session:
        for i, sentence in enumerate(SENTENCES, start=1):
            start = time.perf_counter()
            async with session.post(
                PIPER_URL,
                json={"text": sentence, "voice": TAMIL_VOICE},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                r.raise_for_status()
                audio = await r.read()
            elapsed = time.perf_counter() - start

            out_path = OUT_DIR / f"tamil_{i:02d}.wav"
            out_path.write_bytes(audio)

            is_wav = audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
            print(
                f"[{i}] {sentence!r}\n"
                f"    -> {out_path} ({len(audio)} bytes, wav_header_ok={is_wav}) "
                f"in {elapsed:.2f}s"
            )


if __name__ == "__main__":
    asyncio.run(main())
