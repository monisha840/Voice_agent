#
# Phase 1: English voice agent, fully local, CPU only.
#
#   Browser mic -> SmallWebRTC -> Silero VAD + Smart Turn v3 -> Moonshine STT
#   -> Ollama (qwen3:4b-instruct-2507) -> Piper (HTTP server in WSL2) -> browser
#
# Phase 1b: same pipeline, reachable over a real phone call via Twilio Media
# Streams (see CLAUDE.md "Twilio phone integration"). Twilio's WebSocket
# connects to /ws; create_transport() auto-detects it and builds the
# TwilioFrameSerializer, so the pipeline code below never branches on
# transport type.
#
# Phase 1c: Exotel as a second telephony provider (kept alongside Twilio, not
# replacing it - see CLAUDE.md). Exotel needs a hand-built transport in bot()
# below instead of create_transport()'s automatic path: Exotel supports 8/16/
# 24kHz (16kHz is Exotel's own recommended rate, and a native match for
# IndicConformer's Tamil STT - no resampling loss), but
# pipecat.runner.utils._create_telephony_transport() always builds
# ExotelFrameSerializer with its 8kHz default and gives no way to override it
# (verified by reading that function's source - it unconditionally overwrites
# params.serializer). So for Exotel only, bot() builds the transport itself.
#
# Written against pipecat-ai 1.12.0 (checked against the v1.12.0 tag's
# examples/voice/voice-moonshine.py, voice-piper.py and the service sources).
#
# Run:      uv run bot.py -t webrtc   (run_bot.ps1 does this)
# Browser:  http://localhost:7860/client
# Twilio:   the /ws route is registered regardless of -t (verified live -
#           a fake Twilio handshake against `-t webrtc` was auto-detected
#           and built a full pipeline), so point a TwiML Bin's <Stream> at
#           wss://<ngrok-host>/ws. Only `-t twilio` would break the browser
#           flow (it restricts /start to twilio-only) - don't use that.
#

import json
import os
import re
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
from pipecat.processors.aggregators.llm_context import NOT_GIVEN, LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.text_transformer import StatelessTextTransformer
from pipecat.runner.types import RunnerArguments, WebSocketRunnerArguments
from pipecat.runner.utils import create_transport, parse_telephony_websocket
from pipecat.serializers.exotel import ExotelFrameSerializer
from pipecat.services.moonshine.stt import MoonshineSTTService
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.services.piper.tts import PiperHttpTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner

import banking_tools
from stt_indic import IndicConformerSTTService

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Config (override any of these in .env)
# ---------------------------------------------------------------------------
# "en" (Moonshine STT + English Piper voice) or "ta" (IndicConformer STT,
# WSL2, + Tamil Piper voice). Fixed per session - see CLAUDE.md Architecture.
LANGUAGE = os.getenv("LANGUAGE", "en")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
# gemma3:4b for Tamil: in a live comparison it was both far terser (Part C:
# ~27 vs ~117 avg completion tokens) and noticeably more coherent (qwen3
# hallucinated a non-existent dish name on a recipe question; gemma3 named a
# real one). qwen3 stays the default for English per CLAUDE.md.
OLLAMA_MODEL = os.getenv(
    "OLLAMA_MODEL",
    "gemma3:4b" if LANGUAGE == "ta" else "qwen3:4b-instruct-2507-q4_K_M",
)
# piper-tts 1.8.0's HTTP server synthesises on POST /synthesize.
PIPER_URL = os.getenv("PIPER_URL", "http://localhost:5000/synthesize")
# tiny | base | tiny-streaming | base-streaming | small-streaming | medium-streaming
MOONSHINE_MODEL = os.getenv("MOONSHINE_MODEL", "small-streaming")
MAX_REPLY_TOKENS = int(os.getenv("MAX_REPLY_TOKENS", "120"))

# Exotel supports 8/16/24kHz (set via ?sample-rate= on the Voicebot Applet's
# stream URL - see CLAUDE.md). This must match whatever's configured there.
# 16kHz is Exotel's own recommended rate and IndicConformer's native rate.
EXOTEL_SAMPLE_RATE = int(os.getenv("EXOTEL_SAMPLE_RATE", "16000"))

INDIC_STT_URL = os.getenv("INDIC_STT_URL", "http://localhost:5001/transcribe")
PIPER_VOICE = os.getenv(
    "PIPER_VOICE",
    "ta_IN-rasa_female-medium" if LANGUAGE == "ta" else "en_US-lessac-medium",
)

LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LATENCY_LOG = LOG_DIR / "latency.jsonl"

# {current_datetime} is filled in per-session in run_bot() (not here - a
# session can start any time, and Ollama has no clock of its own). Live
# testing showed the model otherwise guesses a plausible-sounding but wrong
# date (e.g. "today is July 26, 2024"), and separately, when asked to set
# alarms or check the weather, confidently claims to have done so - and later
# invents a fake "I'm busy checking the weather" excuse to refuse unrelated
# questions - even though bot.py has no alarm, calendar or weather tool at
# all. Both instructions below are direct fixes for those two live findings.
SYSTEM_PROMPT_EN = (
    "You are a friendly voice assistant. Your replies are spoken aloud, so use "
    "plain conversational English. Never use emojis, bullet points, numbered "
    "lists, or formatting characters like asterisks, hashes or underscores - "
    "just plain spoken sentences, nothing else. Reply in one or two short "
    "sentences unless the user asks for more.\n\n"
    "The current date and time is {current_datetime}. Use this as the real, "
    "correct answer whenever asked about today's date, day, or time - never "
    "guess or invent one.\n\n"
    "{tools_guidance}"
)
SYSTEM_PROMPT_TA = (
    "You are a friendly voice assistant. Your replies are spoken aloud, so "
    "reply only in Tamil, in plain conversational language. Never use emojis, "
    "bullet points, numbered lists, or formatting characters like asterisks, "
    "hashes or underscores - just plain spoken Tamil sentences, nothing else. "
    "Reply in one or two short sentences unless the user asks for more.\n\n"
    "The current date and time is {current_datetime}. Use this as the real, "
    "correct answer whenever asked about today's date, day, or time - never "
    "guess or invent one.\n\n"
    "{tools_guidance}"
)
# Tool guidance (appended via {tools_guidance}). The hard rule - no account data
# before verification - is ALSO enforced in code (banking_tools._require_verified);
# the prompt only makes the model behave politely around it.
TOOLS_GUIDANCE = (
    "You are a customer-service agent for a bank. You can ONLY do what your "
    "tools allow: verify_customer, get_account_balance, get_recent_transactions, "
    "get_ticket_status, transfer_to_human and end_call. You cannot set alarms, "
    "check weather, browse the internet or do anything else - if asked, say "
    "plainly that you can't, never pretend, and never promise to report back "
    "later.\n\n"
    "Rules: Before sharing any account information, ask for the customer ID "
    "and 4 digit PIN and call verify_customer. Never state a balance, "
    "transaction or ticket detail unless it came from a tool result in this "
    "conversation - never guess or invent numbers. If the caller has already "
    "said their customer ID and PIN, call verify_customer immediately with "
    "them instead of asking again. You perform actions ONLY by calling tools: "
    "never say you are transferring, verifying or ending the call unless you "
    "call that tool in the same turn. Examples: caller says 'I want a human' -> "
    "call transfer_to_human, then speak. Caller says 'that is all, bye' -> call "
    "end_call, then speak a short goodbye. Read amounts in rupees as "
    "plain words. If verification is locked, the caller is upset, asks for a "
    "person, or you cannot help, call transfer_to_human. When the caller says "
    "they need nothing else, call end_call."
)
# STOPGAP: Tamil runs gemma3:4b, which Ollama refuses to use with tools, so
# Tamil sessions are CHAT ONLY - no banking actions in Tamil yet. This is a
# deliberate pause, not a design: whether 4B models are even the right size for
# tool use is still open (see CLAUDE.md Part E), so no Tamil workaround is built.
TOOLS_ENABLED = LANGUAGE != "ta"
CHAT_ONLY_GUIDANCE = (
    "You have no real-world tools: you cannot look up accounts, balances or "
    "tickets, set alarms or reminders, check live weather, browse the "
    "internet, or take any action outside this conversation. If asked to do "
    "something like that, say plainly (in Tamil) that you can't actually do "
    "it - never pretend you did, never invent account details, and never "
    "promise to check something and report back later."
)
ACTIVE_GUIDANCE = TOOLS_GUIDANCE if TOOLS_ENABLED else CHAT_ONLY_GUIDANCE
SYSTEM_PROMPT_TEMPLATE = SYSTEM_PROMPT_TA if LANGUAGE == "ta" else SYSTEM_PROMPT_EN

# The system prompt asks the LLM not to use markdown/emoji, but a 4B model
# won't always comply (confirmed live: qwen3 wrapped a Tamil recipe answer in
# "**bold**" markers despite the instruction) - Piper would read those symbols
# aloud. Strip them in code instead of relying on the prompt alone.
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"  # symbols, pictographs, emoticons, transport, supplemental
    "\U00002600-\U000027bf"  # misc symbols, dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicator symbols (flags)
    "]+",
    flags=re.UNICODE,
)
_MARKDOWN_RE = re.compile(r"[*_`#]+")
_LIST_BULLET_RE = re.compile(r"(?m)^\s*[-•]\s+")


def strip_markdown_and_emoji(text: str) -> str:
    text = _EMOJI_RE.sub("", text)
    text = _LIST_BULLET_RE.sub("", text)
    text = _MARKDOWN_RE.sub("", text)
    return text

# The dev runner picks params by transport name. "webrtc" serves the browser
# UI; "twilio" is used for real phone calls over Twilio's Media Streams
# WebSocket - pipecat.runner.utils.create_transport() auto-detects Twilio's
# connection from the WebSocket handshake and builds the TwilioFrameSerializer
# for us, so this is the only Twilio-specific line bot.py needs (verified
# against the installed pipecat-ai 1.12.0 source - see CLAUDE.md Rules).
# Both run from the same process; run_bot() below is transport-agnostic.
# Exotel is intentionally NOT listed here - it needs a custom sample rate
# create_transport() can't set, so bot() below builds its transport by hand
# instead of going through this dict. See the Phase 1c comment at the top.
transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
    "twilio": lambda: FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}


# ---------------------------------------------------------------------------
# Preflight: fail fast with a clear message instead of a silent bot.
# ---------------------------------------------------------------------------
class PreflightError(RuntimeError):
    """Ollama or Piper is unreachable or misconfigured; the bot cannot run."""


async def preflight(session: aiohttp.ClientSession) -> None:
    # Ollama: is the server up and is the model pulled?
    tags_url = OLLAMA_BASE_URL.removesuffix("/v1") + "/api/tags"
    try:
        async with session.get(tags_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            names = [m["name"] for m in (await r.json()).get("models", [])]
    except Exception as e:  # noqa: BLE001
        msg = f"Preflight FAILED: cannot reach Ollama at {tags_url}: {e}"
        logger.error(msg)
        raise PreflightError(msg) from e
    if OLLAMA_MODEL not in names:
        msg = f"Preflight FAILED: Ollama is up but '{OLLAMA_MODEL}' is not pulled. Have: {names}"
        logger.error(msg)
        raise PreflightError(msg)
    # The agent is tool-driven; Ollama rejects every request with tools
    # attached if the model lacks the "tools" capability (gemma3:4b does).
    if TOOLS_ENABLED:
        show_url = OLLAMA_BASE_URL.removesuffix("/v1") + "/api/show"
        try:
            async with session.post(
                show_url, json={"model": OLLAMA_MODEL}, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                caps = (await r.json()).get("capabilities", [])
        except Exception as e:  # noqa: BLE001
            msg = f"Preflight FAILED: could not read capabilities of '{OLLAMA_MODEL}': {e}"
            logger.error(msg)
            raise PreflightError(msg) from e
        if "tools" not in caps:
            msg = (
                f"Preflight FAILED: '{OLLAMA_MODEL}' does not support tool calling "
                f"(capabilities: {caps}). Set OLLAMA_MODEL to a tools-capable model."
            )
            logger.error(msg)
            raise PreflightError(msg)
        logger.info(f"Preflight OK: Ollama has {OLLAMA_MODEL} (tools supported)")
    else:
        logger.info(f"Preflight OK: Ollama has {OLLAMA_MODEL} (chat only, tools disabled)")

    # Piper: does a tiny synthesis return a WAV?
    try:
        async with session.post(
            PIPER_URL,
            json={"text": "ok", "voice": PIPER_VOICE},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            status = r.status
            body = await r.read()
    except Exception as e:  # noqa: BLE001
        msg = f"Preflight FAILED: cannot reach Piper at {PIPER_URL}: {e} (is the WSL2 server running?)"
        logger.error(msg)
        raise PreflightError(msg) from e
    if status != 200 or body[:4] != b"RIFF":
        msg = (
            f"Preflight FAILED: Piper at {PIPER_URL} returned HTTP {status}. "
            "piper-tts 1.8.0 expects POST /synthesize - check PIPER_URL in .env."
        )
        logger.error(msg)
        raise PreflightError(msg)
    logger.info(f"Preflight OK: Piper at {PIPER_URL} (voice {PIPER_VOICE})")

    # IndicConformer (Tamil sessions only): is the WSL2 server up?
    if LANGUAGE == "ta":
        health_url = INDIC_STT_URL.removesuffix("/transcribe") + "/health"
        try:
            async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                status = r.status
        except Exception as e:  # noqa: BLE001
            msg = (
                f"Preflight FAILED: cannot reach IndicConformer STT at {INDIC_STT_URL}: "
                f"{e} (is the WSL2 indic-stt-server running?)"
            )
            logger.error(msg)
            raise PreflightError(msg) from e
        if status != 200:
            msg = f"Preflight FAILED: IndicConformer STT health check at {health_url} returned HTTP {status}."
            logger.error(msg)
            raise PreflightError(msg)
        logger.info(f"Preflight OK: IndicConformer STT at {INDIC_STT_URL}")


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

        # STT: Moonshine (English, CPU via ONNX, in-process) or IndicConformer
        # (Tamil, via the WSL2 indic-stt-server - see stt_indic.py).
        if LANGUAGE == "ta":
            stt = IndicConformerSTTService(base_url=INDIC_STT_URL, aiohttp_session=session)
        else:
            stt = MoonshineSTTService(
                settings=MoonshineSTTService.Settings(model=MOONSHINE_MODEL),
            )

        # LLM: Ollama through its OpenAI-compatible endpoint. No real key needed.
        # Ground the model in the real current date/time per session - it has
        # no clock of its own and would otherwise guess (see SYSTEM_PROMPT_EN/TA).
        current_datetime = datetime.now().strftime("%A, %d %B %Y, %I:%M %p")
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            current_datetime=current_datetime, tools_guidance=ACTIVE_GUIDANCE
        )
        llm = OLLamaLLMService(
            base_url=OLLAMA_BASE_URL,
            settings=OLLamaLLMService.Settings(
                model=OLLAMA_MODEL,
                system_instruction=system_prompt,
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

        # Belt-and-suspenders: the system prompt already asks for no
        # markdown/emoji, but a 4B model doesn't always comply, and Piper
        # would read leftover symbols aloud. Strip them before they reach TTS.
        sanitize_for_speech = StatelessTextTransformer(strip_markdown_and_emoji)

        # Context = conversation history. Silero VAD detects speech; the default
        # stop strategy (Smart Turn v3, local ONNX) decides when you've finished.
        context = LLMContext(tools=banking_tools.TOOLS if TOOLS_ENABLED else NOT_GIVEN)
        call_state = banking_tools.SessionState()
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
                sanitize_for_speech,
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
            app_resources=call_state,  # -> params.app_resources in tool handlers
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
                "lang": LANGUAGE,
                "stt": "indic-conformer" if LANGUAGE == "ta" else f"moonshine/{MOONSHINE_MODEL}",
                "llm": OLLAMA_MODEL,
                "tts": f"piper/{PIPER_VOICE}",
                "breakdown": breakdown.model_dump(mode="json"),
            }
            with LATENCY_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # ---- end-of-call: tools only set a flag; we hang up after the goodbye
        # has actually been spoken (an assistant turn with real content).
        @assistant_aggregator.event_handler("on_assistant_turn_stopped")
        async def on_assistant_turn_stopped(aggregator, message):
            if call_state.end_requested and message.content.strip():
                logger.info(f"Ending call: {call_state.end_reason}")
                await worker.end(reason=call_state.end_reason)

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
    """Entry point the Pipecat dev runner calls for each browser/phone session."""
    # Exotel needs its own path (see Phase 1c comment at the top): peek at the
    # handshake first so we can hand-build ExotelFrameSerializer with our
    # sample rate. parse_telephony_websocket() caches its result on the
    # websocket, so create_transport()'s own internal call to it below
    # (for Twilio/webrtc/etc.) re-reads the cache instead of re-consuming the
    # single-use message stream - confirmed by reading its source.
    if isinstance(runner_args, WebSocketRunnerArguments) and runner_args.transport_type is None:
        transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
        if transport_type == "exotel":
            runner_args.transport_type = transport_type
            runner_args.call_data = call_data
            serializer = ExotelFrameSerializer(
                stream_sid=call_data.stream_id,
                call_sid=call_data.call_id,
                params=ExotelFrameSerializer.InputParams(
                    exotel_sample_rate=EXOTEL_SAMPLE_RATE
                ),
            )
            transport = FastAPIWebsocketTransport(
                websocket=runner_args.websocket,
                params=FastAPIWebsocketParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                    add_wav_header=False,
                    serializer=serializer,
                ),
            )
            await run_bot(transport, runner_args)
            return

    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
