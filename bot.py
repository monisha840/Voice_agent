#
# Phase 1: English voice agent, fully local, CPU only.
#
#   Browser mic -> SmallWebRTC -> Silero VAD + Smart Turn v3 -> Moonshine STT
#   -> Ollama (qwen3:4b-instruct-2507) -> Piper (HTTP server in WSL2) -> browser
#
# Written against pipecat-ai 1.12.0 (checked against the v1.12.0 tag's
# examples/voice/voice-moonshine.py, voice-piper.py and the service sources).
#
# Run:   uv run bot.py -t webrtc
# Open:  http://localhost:7860/client
#

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.observers.loggers.transcription_log_observer import TranscriptionLogObserver
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.moonshine.stt import MoonshineSTTService
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.services.piper.tts import PiperHttpTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Config (override any of these in .env)
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
# piper-tts 1.8.0's HTTP server synthesises on POST /synthesize.
PIPER_URL = os.getenv("PIPER_URL", "http://localhost:5000/synthesize")
PIPER_VOICE = os.getenv("PIPER_VOICE", "en_US-lessac-medium")
# tiny | base | tiny-streaming | base-streaming | small-streaming | medium-streaming
MOONSHINE_MODEL = os.getenv("MOONSHINE_MODEL", "small-streaming")
MAX_REPLY_TOKENS = int(os.getenv("MAX_REPLY_TOKENS", "120"))

LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LATENCY_LOG = LOG_DIR / "latency.jsonl"

SYSTEM_PROMPT = (
    "You are a friendly voice assistant. Your replies are spoken aloud, so use "
    "plain conversational English with no emojis, lists, markdown or symbols. "
    "Reply in one or two short sentences unless the user asks for more."
)

# Only the WebRTC transport is used. The runner picks params by transport name.
transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}


# ---------------------------------------------------------------------------
# Preflight: fail fast with a clear message instead of a silent bot.
# ---------------------------------------------------------------------------
async def preflight(session: aiohttp.ClientSession) -> None:
    # Ollama: is the server up and is the model pulled?
    tags_url = OLLAMA_BASE_URL.removesuffix("/v1") + "/api/tags"
    try:
        async with session.get(tags_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            names = [m["name"] for m in (await r.json()).get("models", [])]
        if OLLAMA_MODEL not in names:
            logger.error(f"Ollama is up but '{OLLAMA_MODEL}' is not pulled. Have: {names}")
        else:
            logger.info(f"Preflight OK: Ollama has {OLLAMA_MODEL}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Preflight FAILED: cannot reach Ollama at {tags_url}: {e}")

    # Piper: does a tiny synthesis return a WAV?
    try:
        async with session.post(
            PIPER_URL,
            json={"text": "ok", "voice": PIPER_VOICE},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            body = await r.read()
        if r.status == 200 and body[:4] == b"RIFF":
            logger.info(f"Preflight OK: Piper at {PIPER_URL} (voice {PIPER_VOICE})")
        else:
            logger.error(
                f"Preflight FAILED: Piper at {PIPER_URL} returned HTTP {r.status}. "
                "piper-tts 1.8.0 expects POST /synthesize - check PIPER_URL in .env."
            )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Preflight FAILED: cannot reach Piper at {PIPER_URL}: {e} (is the WSL2 server running?)")


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    logger.info("Starting bot")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # `async with` is Python's try/finally for resources, like `await using` in TS.
    async with aiohttp.ClientSession() as session:
        await preflight(session)

        # STT: Moonshine, English, CPU via ONNX Runtime. Model downloads once.
        stt = MoonshineSTTService(
            settings=MoonshineSTTService.Settings(model=MOONSHINE_MODEL),
        )

        # LLM: Ollama through its OpenAI-compatible endpoint. No real key needed.
        llm = OLLamaLLMService(
            base_url=OLLAMA_BASE_URL,
            settings=OLLamaLLMService.Settings(
                model=OLLAMA_MODEL,
                system_instruction=SYSTEM_PROMPT,
                max_tokens=MAX_REPLY_TOKENS,
                temperature=0.6,
            ),
        )

        # TTS: Piper runs as a separate HTTP server (keeps GPL code out of this app).
        # Pipecat's TTSService splits the LLM stream into sentences before calling
        # Piper, and resamples Piper's 22.05 kHz WAV to the pipeline's output rate.
        tts = PiperHttpTTSService(
            base_url=PIPER_URL,
            aiohttp_session=session,
            settings=PiperHttpTTSService.Settings(voice=PIPER_VOICE),
        )

        # Context = conversation history. Silero VAD detects speech; the default
        # stop strategy (Smart Turn v3, local ONNX) decides when you've finished.
        context = LLMContext()
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
        )

        # Like an Express middleware chain, but frames flow both ways.
        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                user_aggregator,
                llm,
                tts,
                transport.output(),
                assistant_aggregator,
            ]
        )

        latency_observer = UserBotLatencyObserver()

        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                enable_metrics=True,  # needed for the per-service latency breakdown
                enable_usage_metrics=True,
            ),
            observers=[latency_observer, TranscriptionLogObserver()],
            idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
            processor_unusable_policy=ProcessorUnusablePolicy.END,
        )
        runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
        await runner.add_workers(worker)

        # ---- latency logging (success criterion 5) --------------------------
        turn_no = 0

        @latency_observer.event_handler("on_first_bot_speech_latency")
        async def on_first_bot_speech_latency(observer, latency_seconds):
            logger.info(f"First bot speech {latency_seconds:.3f}s after connect")

        @latency_observer.event_handler("on_latency_breakdown")
        async def on_latency_breakdown(observer, breakdown):
            nonlocal turn_no
            turn_no += 1
            logger.info(f"TURN {turn_no} user->bot latency {breakdown.total_secs:.3f}s")
            for line in breakdown.turn_contribution_lines():
                logger.info(f"    {line}")
            record = {
                "ts": time.time(),
                "session": session_id,
                "turn": turn_no,
                "lang": "en",
                "stt": f"moonshine/{MOONSHINE_MODEL}",
                "llm": OLLAMA_MODEL,
                "tts": f"piper/{PIPER_VOICE}",
                "breakdown": breakdown.model_dump(mode="json"),
            }
            with LATENCY_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # ---- connection lifecycle -------------------------------------------
        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info("Client connected")
            # Ollama has no "developer" role; OLLamaLLMService converts it for us.
            context.add_message(
                {"role": "developer", "content": "Greet the user in one short sentence."}
            )
            await worker.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info("Client disconnected")
            await runner.cancel()

        await runner.run()


async def bot(runner_args: RunnerArguments):
    """Entry point the Pipecat dev runner calls for each browser session."""
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
