"""Phase 0 environment check for the Tamil + English voice agent.

Verifies, without building any pipeline:
  1. Python is 3.11.
  2. pipecat-ai is installed and its services/transports/VAD for this
     project's architecture import cleanly.
  3. Ollama is reachable and the configured LLM model is pulled.
  4. The separate Piper HTTP server (../piper-server, port 5000) is
     reachable and returns a real WAV file.

Run with: uv run python phase0_check.py
"""

import asyncio
import sys

import aiohttp

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
PIPER_URL = "http://localhost:5000/synthesize"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))


def check_python_version() -> None:
    major, minor = sys.version_info[:2]
    check(
        "Python 3.11",
        (major, minor) == (3, 11),
        f"running {sys.version.split()[0]}",
    )


def check_pipecat_imports() -> None:
    try:
        import pipecat

        version = getattr(pipecat, "__version__", "unknown")
    except Exception as e:
        check("pipecat-ai import", False, repr(e))
        return
    check("pipecat-ai import", True, f"version {version}")

    imports = [
        ("SmallWebRTCTransport", "pipecat.transports.smallwebrtc.transport"),
        ("SileroVADAnalyzer", "pipecat.audio.vad.silero"),
        ("MoonshineSTTService", "pipecat.services.moonshine.stt"),
        ("OLLamaLLMService", "pipecat.services.ollama.llm"),
        ("PiperHttpTTSService", "pipecat.services.piper.tts"),
    ]
    for class_name, module_name in imports:
        try:
            module = __import__(module_name, fromlist=[class_name])
            getattr(module, class_name)
            check(f"import {class_name}", True, module_name)
        except Exception as e:
            check(f"import {class_name}", False, repr(e))


async def check_ollama(session: aiohttp.ClientSession) -> None:
    try:
        async with session.get(f"{OLLAMA_URL}/api/tags", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                check("Ollama reachable", False, f"HTTP {resp.status}")
                return
            data = await resp.json()
    except Exception as e:
        check("Ollama reachable", False, repr(e))
        return
    check("Ollama reachable", True, OLLAMA_URL)

    names = [m.get("name") for m in data.get("models", [])]
    check(f"Ollama has model {OLLAMA_MODEL}", OLLAMA_MODEL in names, f"models: {names}")

    try:
        payload = {
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": "Reply with exactly one word: OK"}],
        }
        async with session.post(
            f"{OLLAMA_URL}/v1/chat/completions", json=payload, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            ok = resp.status == 200
            body = await resp.json() if ok else await resp.text()
    except Exception as e:
        check("Ollama chat completion", False, repr(e))
        return
    reply = body["choices"][0]["message"]["content"] if ok else str(body)
    check("Ollama chat completion", ok, f"reply: {reply!r}")


async def check_piper(session: aiohttp.ClientSession) -> None:
    try:
        payload = {"text": "This is a test."}
        async with session.post(
            PIPER_URL, json=payload, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status != 200:
                check("Piper HTTP server", False, f"HTTP {resp.status}: {await resp.text()}")
                return
            audio = await resp.read()
    except Exception as e:
        check("Piper HTTP server", False, repr(e))
        return
    is_wav = audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    check("Piper HTTP server returns WAV", is_wav, f"{len(audio)} bytes")


async def main() -> None:
    check_python_version()
    check_pipecat_imports()
    async with aiohttp.ClientSession() as session:
        await check_ollama(session)
        await check_piper(session)

    print()
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} checks passed")
    if passed != len(results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
