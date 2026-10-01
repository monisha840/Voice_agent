# Local Tamil + English voice agent

## Rules
- 100% free and open source. CPU only. No API keys, no paid services.
- Python 3.11 with uv. Windows 11 (use WSL2 only if a dependency fails to install on Windows).
- Laptop: Ryzen 7 5825U, 16 GB RAM (13.8 GB usable), no usable GPU.
- Before writing any Pipecat code, read the installed library source and the
  cloned examples in ../pipecat. Never write Pipecat API calls from memory.
- After writing code, run it or its tests, and report what actually happened.
  Say clearly what is verified and what is assumed.

## Architecture
Browser mic -> SmallWebRTC -> Silero VAD -> STT -> context aggregator
-> Ollama LLM (streaming) -> sentence splitter -> TTS -> audio out.
Interruption cancels LLM and TTS. Language is fixed per session (Tamil/English toggle).

## Models
- English STT: Moonshine (built into Pipecat), then compare Parakeet via parakeet-api.
- Tamil STT: AI4Bharat IndicConformer via a custom SegmentedSTTService.
  Fallback: vasista22/whisper-tamil-medium.
- LLM: Ollama, qwen3:4b-instruct-2507-q4_K_M. For Tamil, benchmark Qwen3 4B,
  Gemma 3 4B and Tamil-LLaMA.
- TTS: Piper via PiperHttpTTSService (separate Piper HTTP server on port 5000).
  Tamil voice: ta_IN-rasa_female-medium.

## Rejected
Nemotron blueprint (needs GPU), Pipecat's ParakeetSTTService (cloud, needs key),
plain Whisper base/small for Tamil, MMS-TTS Tamil (non-commercial), Kokoro for Tamil.

## Telephony integration (Part D, in progress)
Decision (2026-09-30): switching primary telephony provider from Twilio to
Exotel. Twilio's code is kept in bot.py, not deleted - may still be wanted
later - but Exotel is the one being actively pursued for a real number.
Both are second transport paths alongside the existing WebRTC one - all run
from the same bot.py process, selected per-connection (browser -> webrtc,
Twilio/Exotel -> their own WebSocket). Built against pipecat-ai 1.12.0's
installed source (see Rules above - no ../pipecat clone exists on this
machine, so both were verified from `.venv/Lib/site-packages/pipecat`
directly, not from memory).

### Exotel (primary, in progress)

Done and verified:
- `bot()` in bot.py builds Exotel's transport by hand instead of going
  through `transport_params`/`create_transport()`'s automatic path - needed
  because `pipecat.runner.utils._create_telephony_transport()` always
  constructs `ExotelFrameSerializer` with its 8kHz default and gives no way
  to override it (confirmed by reading that function's source: it
  unconditionally overwrites `params.serializer`). `EXOTEL_SAMPLE_RATE`
  (`.env`, default `16000`) controls the rate our side expects; it must
  match whatever `?sample-rate=` is set to on the Exotel Voicebot Applet's
  stream URL.
- **[RESEARCH, Exotel's own docs, support.exotel.com]** Resolves the
  "8kHz vs 16kHz" question: it's not fixed, it's a configurable query param
  on the Voicebot Applet URL - 8kHz (default), 16kHz (Exotel's own
  recommended rate), or 24kHz. We're using 16kHz, which is also
  IndicConformer's native rate (no resampling loss on input, unlike Twilio's
  fixed 8kHz).
- **[CODE]** Exotel's wire audio is raw 16-bit PCM, not mu-law like Twilio -
  confirmed by reading `pipecat/serializers/exotel.py`: it only resamples,
  no `pcm_to_ulaw`/`ulaw_to_pcm` calls anywhere in the file.
- **[CODE]** Exotel's handshake uses snake_case field names (`stream_sid`,
  `call_sid`, `account_sid`), unlike Twilio's camelCase - already correctly
  handled by Pipecat's existing `parse_telephony_websocket`/`ExotelCallData`.
- Confirmed live, with no real Exotel account, the same way Twilio was
  tested: opened a raw WebSocket to `ws://localhost:7860/ws` and sent
  Exotel's exact `start`/`media` JSON handshake by hand (fake_exotel_ws.py,
  scratchpad, not committed - 16-bit PCM silence, not mu-law, since that's
  Exotel's real wire format). Server logged "Detected transport: exotel",
  correctly parsed `stream_id`/`call_id`/`account_sid`/`from`/`to`, built the
  full STT->LLM->TTS pipeline (identical linking to the Twilio/webrtc paths),
  and sent 3 real `media` (audio) events back over the socket.
  Piper and IndicConformer were down (WSL2 stopped) during this run, so
  their `/synthesize` and `/health` endpoints were stood in for by two
  stdlib-only stub servers in the scratchpad (not committed, not a
  replacement for the real Tamil TTS/STT quality - that was already proven
  separately in Part C) - purely to isolate today's test to what actually
  changed: the Exotel transport code.
- No `.env` variables are required for Exotel itself (unlike Twilio's
  `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`) - `ExotelFrameSerializer` takes
  no credentials at all. `EXOTEL_SAMPLE_RATE` is optional (defaults to 16000).

Open item, not resolved:
- **No call-termination (hangup) API found.** `ExotelFrameSerializer` has no
  hangup logic at all (unlike Twilio's `_hang_up_call` REST call). Checked
  Exotel's own Voicebot Applet docs and their Call API docs specifically for
  a way to end an in-progress call by Call SID - not documented anywhere I
  could find; their Call API only covers placing calls, not ending one
  already live. Did not implement a speculative call against an unconfirmed
  endpoint. Today, a call presumably only ends when the caller hangs up or
  the App Bazaar flow itself routes past the Voicebot Applet to a Hangup
  applet - neither is confirmed. Needs either contacting Exotel support or
  finding an undocumented endpoint.

Assumed, not yet verified (needs a real Exotel number):
- The above proves the WebSocket plumbing, 16kHz sample rate, and pipeline
  wiring work. It does NOT prove a real phone call sounds right end-to-end -
  ngrok's tunnel, Exotel's actual Voicebot Applet behavior, and real phone
  audio (even at 16kHz, real callers/lines differ from clean test silence)
  are all still untested.

### Twilio (kept in code, not the active choice for now)

Done and verified:
- `transport_params["twilio"]` added (FastAPIWebsocketParams) alongside the
  existing `"webrtc"` entry. No new dependencies needed -
  `_transport_routes_enabled("telephony")` was already True in this venv
  (fastapi 0.141.1 + websockets 17.1 came in transitively via `[runner]`).
- Confirmed live, with no real Twilio account, by opening a raw WebSocket to
  `ws://localhost:7860/ws` and sending Twilio's exact `connected`/`start`/
  `media` JSON handshake by hand (fake_twilio_ws.py, run from the scratchpad,
  not committed): the server auto-detected "twilio", built a
  TwilioFrameSerializer, and linked the full STT->LLM->TTS pipeline
  identically to the WebRTC path.
- Confirmed `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are not optional:
  `TwilioFrameSerializer` raises immediately if either is missing (its
  `auto_hang_up` default is True). Add both to `.env` under exactly those
  two names before testing.
- Confirmed the dev runner's `/ws` route is registered regardless of the
  `-t` flag - `-t webrtc` (what `run_bot.ps1` already uses, unchanged) does
  NOT block Twilio's connection. Ran the fake-handshake test twice: once
  with `.env` missing the Twilio vars (got the expected ValueError, not a
  silent failure), then again with dummy `TWILIO_ACCOUNT_SID`/
  `TWILIO_AUTH_TOKEN` env vars (not real, just to clear the check) - the
  pipeline ran a real greeting through Ollama and Piper and sent 18 `media`
  (audio) events back over the raw socket, base64 mu-law, correct streamSid.
- Only `-t twilio` is unusable here - it restricts `/start` to twilio-only,
  which would 400 the browser UI's own `/start` call. Not used; no code
  depends on it.

Assumed, not yet verified (needs a real Twilio number, pending the
regulatory bundle):
- The above proves the WebSocket plumbing and pipeline wiring work. It does
  NOT prove a real phone call sounds right end-to-end - ngrok's tunnel,
  Twilio's actual Media Streams behavior, and real 8kHz phone audio (see the
  open risk below) are all still untested.

Open risk, not yet tested:
- Twilio's Media Streams audio is 8kHz mono mu-law - narrower than the audio
  Moonshine and IndicConformer were likely tuned/evaluated on. Pipecat's
  TwilioFrameSerializer resamples between 8kHz mu-law and the pipeline's
  sample rate automatically, but whether STT accuracy holds up on genuinely
  bandwidth-limited phone audio is unverified until a real test call happens.
  Don't pre-emptively change anything for this - check STT quality once the
  WebSocket path is validated end-to-end.

## Agent tools (Part E, 2026-10-01, started)

Built against pipecat-ai 1.12.0's installed source: tools are async "direct
functions" listed in `LLMContext(tools=[...])`; per-session state goes via
`PipelineWorker(app_resources=...)` -> `params.app_resources`.
Files: `banking_tools.py` (MOCK in-memory bank; verify_customer,
get_account_balance, get_recent_transactions, get_ticket_status,
transfer_to_human, end_call), `test_banking_tools.py`, `tool_call_eval.py`.

Verified:
- 6 offline tests pass: account data is gated IN CODE until verify_customer
  succeeds; 3 failed attempts lock the session even if the right PIN follows;
  unknown ID and wrong PIN give identical errors.
- Headless Pipecat pipeline + real Ollama (qwen3): tool registered, handler got
  app_resources, `end_call` -> goodbye spoken -> `worker.end()` ended the run.
  The assistant turn right after a tool call is empty, hence the
  `message.content.strip()` guard in bot.py.
- Measured failure rates, `tool_call_eval.py`, qwen3:4b-instruct-2507-q4_K_M,
  10 identical runs per scenario (results in `tool_call_eval_results_*.json`).
  Failures are deterministic (0/10 or 10/10), not "sometimes":

  | scenario | shipped prompt | prompt WITHOUT the 2 examples |
  |---|---|---|
  | no balance leak before verification | 0% fail | not run |
  | 3 wrong PINs -> lockout | 0% | not run |
  | ticket lookup | 0% | not run |
  | weather refused, no tool | 0% | not run |
  | asks for human -> transfer_to_human called | 0% | **100%** (says "I'll transfer you", no call) |
  | "bye" -> end_call called | 0% | **100%** (says goodbye, no call) |
  | ID+PIN+question in one message -> verify AND answer | **100%** | not run (verifies, then asks "what would you like to know?") |
  | ID+PIN+"bye" in one message -> verify AND end_call | **100%** | **100%** (verifies, says goodbye, no end_call) |

  Pattern: with 4B, a single-intent turn works, but the model makes ONE tool
  call and then answers in text; it never chains a second tool call in the same
  turn. The two worked examples in TOOLS_GUIDANCE fix only the single-intent
  transfer/end cases; they do not fix multi-intent messages. Never rely on
  end_call alone (idle timeout / caller hang-up still end calls).

**Tool-calling reliability must be re-tested on bigger models.** These numbers
are for a 4B model only and say nothing about larger ones. Re-run
`uv run tool_call_eval.py <model>` (one command, exits 1 on any failing
scenario) during the GPU bake-off with Qwen3 8B/14B and Sarvam-30B before
deciding anything about this feature; it may behave very differently on a more
capable model. Do not build workarounds for 4B (e.g. the two-step Tamil
design, or code-side chaining) until that bake-off shows whether 4B is even the
right size.

**Tamil is chat-only for now (stopgap).** `gemma3:4b` (Tamil default) does not
support tools in Ollama (HTTP 400). So `TOOLS_ENABLED = LANGUAGE != "ta"` in
bot.py: Tamil sessions get no tools and a chat-only prompt
(CHAT_ONLY_GUIDANCE); **no banking actions in Tamil yet.** English sessions
keep tools, and preflight fails fast if the English model lacks the "tools"
capability. Verified: preflight passes with gemma3 in chat-only mode (stops
only at Piper, WSL2 down), and a headless Pipecat pipeline with gemma3 +
Tamil prompt answered a Tamil "what is my balance?" with a refusal ("I can't
check your balance"), no error, no invented number. That test caught a real
bug (LLMContext rejects tools=None; bot.py now passes NOT_GIVEN). My own
reading of Tamil is basic - a Tamil speaker should confirm reply quality.

Not done / assumed: bot.py not run end-to-end with audio (Piper/WSL2 down);
transfer_to_human is a STUB (tells caller, ends call; no real transfer); mock
data only; Tamil tool-calling untested by design.

## Status (2026-09-29, after Part C)

Done and verified:
- `.venv` deleted and rebuilt with `uv sync`. Plain hardlinking failed
  (`os error 396`, likely a leftover cloud-file attribute from the OneDrive
  move). Fixed permanently via `[tool.uv] link-mode = "copy"` in
  pyproject.toml - re-tested `uv sync` with `UV_LINK_MODE` unset and it
  worked from the pinned setting alone.
- All of bot.py's actual imports (Moonshine STT, OLLamaLLMService,
  `PiperHttpTTSService`, SileroVADAnalyzer, etc.) succeed in the rebuilt venv.
- `phase0_check.py` run: 11/11 checks pass.
- `.gitignore` covers `.env`, `.venv`, `__pycache__`, model-weight
  extensions, a `models/` folder, `*.wav` test audio, and `logs/`
  (runtime-generated per-turn latency data).
- `run_bot.ps1` starts/stops cleanly, no leftover process on port 7860.
- `preflight()` in bot.py now raises `PreflightError` (not just logs) when
  Ollama or Piper is unreachable, so startup actually stops instead of
  continuing into a broken pipeline. Tested all three states: Piper stopped
  (killed and restarted the real WSL2 process), Ollama unreachable
  (simulated via a bad URL rather than stopping the shared native-Windows
  service), and both healthy - all behaved correctly.
- **First full live end-to-end voice test succeeded**: browser mic -> VAD ->
  Moonshine STT -> Ollama -> Piper TTS -> speaker, transcription and reply
  both correct, confirmed by the user via the Playground UI. Getting there
  surfaced two one-time cold-start costs, now understood: Moonshine's STT
  model needed a one-off ~54s download (fully blocks the pipeline until
  done - now cached on disk, won't recur), and Ollama's first LLM call after
  being idle takes ~10s to reload the model into memory (matches the
  text_chat_test.py finding below).
- `text_chat_test.py` now streams the Ollama call and reports
  time_to_first_token, time_to_first_sentence, and time_to_first_audio (TTS
  for the first sentence runs concurrently with the rest of the LLM stream,
  same as bot.py's real pipeline). Re-ran 3x back to back: sentence 1's
  `llm_total` was 1.34s/1.38s/1.34s every time - confirms the original 9.6s
  in the first-ever run was one-time Ollama model-load warm-up, not a
  recurring cost.
- Tamil LLM comparison: 10 everyday-topic Tamil questions run against
  `qwen3:4b-instruct-2507-q4_K_M` and newly-pulled `gemma3:4b`, same system
  prompt, saved to `tamil_llm_results.json` (20 records: answer, char
  length, tokens, timing, tokens/sec). Objective numbers only - qwen3
  averaged 11.07s/reply at 10.48 tok/s (116.8 completion tokens/reply);
  gemma3 averaged 4.07s/reply at 7.99 tok/s (26.6 completion tokens/reply,
  much terser despite an identical "short answer" instruction). Tamil
  quality itself was deliberately not judged - that's a human review step.
- Tamil TTS: 5 sentences synthesized via Piper's `ta_IN-rasa_female-medium`
  voice (WSL2 server) to `test_audio/tamil_01..05.wav`, 0.15s-1.39s each.
  Confirmed real audio (22050Hz mono 16-bit, via Python's `wave` module,
  durations scaling with sentence length) and a clean server log with no
  errors.

Assumed, not fully verified:
- Ollama's ~5-minute idle keep-alive causing the cold-start is inferred
  from consistent timing, not confirmed by waiting out the window and
  re-testing on a fresh cold call.
- Tamil TTS sentence 1's slower time (1.39s vs 0.15-0.40s) looks like a
  one-time voice-load cost but wasn't re-tested with a second run to
  confirm it stays fast afterward.
- A WebRTC renegotiation glitch appeared during the first (failed) live
  test attempt - likely the browser reconnecting after the server sat
  frozen through Moonshine's download - but the exact trigger wasn't
  root-caused, only worked around by reconnecting cleanly.
- Confirmed (not just assumed) as a real limitation: speaking Tamil into
  the bot today produces garbled/hallucinated English text, because
  Moonshine STT is English-only. This is expected at this phase, not a bug.

Next, in order:
1. Listen to `test_audio/tamil_01..05.wav` and judge the Piper Tamil voice
   quality (human review).
2. Read `tamil_llm_results.json` and pick which model to use for Tamil,
   weighing answer quality (human review) against the timing/throughput
   data already gathered above.
3. Build actual Tamil STT: AI4Bharat IndicConformer via a custom
   `SegmentedSTTService` (fallback `vasista22/whisper-tamil-medium`), plus
   the Tamil/English session-language toggle mentioned in Architecture -
   neither exists in bot.py yet, which is still English-only (Phase 1).
