# Developer Guide: Local Tamil + English Voice Agent

Audience: a developer new to this project. Last updated 2026-10-04, against the code at commit `f965e20`.

Read this first, then `CLAUDE.md` (decision log, measured results) and `docs/production_readiness_report.md` (why this is a prototype, not a SaaS).

**Evidence tags:** **[CODE]** read from the repo. **[MEASURED]** observed by running it (recorded in `CLAUDE.md` or the report). **[ASSUMED]** not verified. Where something is not verified, this guide says so.

---

## 1. What this is

A voice agent that talks over a browser mic or a phone call, in **English or Tamil**, running **100% locally on CPU** with **no API keys and no paid services**. It is built on [Pipecat](https://github.com/pipecat-ai/pipecat) (pinned `pipecat-ai>=1.12.0`).

The English agent is a **mock bank customer-service agent**. It can verify a caller, read a balance and recent transactions, look up a ticket, transfer to a human (stub), and end the call. The Tamil agent is **chat-only** (no tools).

Hard project rules (`CLAUDE.md`):
- Free and open source only. CPU only. No API keys.
- Python 3.11 with `uv`. Windows 11 (WSL2 only for things that will not install on Windows).
- Target laptop: Ryzen 7 5825U, 16 GB RAM (13.8 GB usable), no usable GPU.
- **Never write Pipecat API calls from memory.** Read the installed source in `.venv/Lib/site-packages/pipecat` first. (No `../pipecat` clone exists on this machine.)
- After writing code, run it and report what actually happened. Separate verified from assumed.

## 2. Architecture at a glance

```
                 ┌──────────────────────────── bot.py process (Windows, Python 3.11) ────────────────────────────┐
 Browser mic ──► │ SmallWebRTC ┐                                                                                  │
 Twilio call ──► │ /ws (Twilio)├► transport.input() ► STT ► user_aggregator ► LLM ► sanitize ► TTS ► transport.output() ► assistant_aggregator
 Exotel call ──► │ /ws (Exotel)┘        │            │       (Silero VAD +      │      (strip     │
                 └──────────────────────│────────────│────── Smart Turn v3) ────│──────markdown)──│─────────────┘
                                        │            │                          │                 │
                              (in-process)   Moonshine (EN, in-proc)     Ollama :11434      Piper HTTP :5000
                                                 or                      (native Windows)   (WSL2)
                                         IndicConformer HTTP :5001 (WSL2, Tamil)
```

Frames flow through the `Pipeline` list in order, like middleware that passes both ways. Interruption (user speaks over the bot) cancels in-flight LLM and TTS; this is handled by Pipecat's built-in frame handling, not custom code here. **[CODE]** `bot.py` has no custom interruption logic. **[ASSUMED]** that interruption was exercised in live browser tests; `CLAUDE.md` records only that the basic turn worked end to end.

**Processes you must have running** for a full session:

| Process | Where | Port | Needed for |
|---|---|---|---|
| `bot.py` (Pipecat dev runner, FastAPI) | Windows | 7860 | everything |
| Ollama | native Windows service | 11434 | LLM (always) |
| Piper HTTP server | WSL2 Ubuntu, `~/piper-server` | 5000 | TTS (always) |
| IndicConformer server | WSL2 Ubuntu, `~/indic-stt-server/server.py` | 5001 | Tamil STT only |

Piper and IndicConformer live **outside this repo**, in the WSL2 home directory of the original developer's machine. They are not versioned here. See section 10 for rebuilding them.

## 3. Models and what each does

| Role | Model | Runs where | Why this one |
|---|---|---|---|
| VAD (is someone speaking?) | **Silero VAD** (`SileroVADAnalyzer`) | in-process, ONNX | Pipecat built-in, cheap on CPU |
| Turn end (has the user finished?) | **Smart Turn v3** | in-process, ONNX | Pipecat's default stop strategy once VAD sees silence |
| English STT | **Moonshine** `small-streaming` (`MOONSHINE_MODEL`) | in-process, ONNX, CPU | Pipecat has `MoonshineSTTService` built in; no server or key |
| Tamil STT | **AI4Bharat IndicConformer** `indicconformer_stt_ta_hybrid_ctc_rnnt_large`, RNNT decoder | WSL2, Flask, NeMo | Purpose-built for Indian languages; needs AI4Bharat's NeMo fork, which only installs on Linux |
| English LLM | **`qwen3:4b-instruct-2507-q4_K_M`** via Ollama | native Windows, CPU | Supports tool calling; fits RAM; ~10 tok/s **[MEASURED]** |
| Tamil LLM | **`gemma3:4b`** via Ollama | native Windows, CPU | Terser (~27 vs ~117 completion tokens) and more coherent than qwen3 on 10 Tamil questions **[MEASURED]**. Tamil quality itself was **not judged by a Tamil speaker** |
| English TTS | **Piper** `en_US-lessac-medium` | WSL2 HTTP server | Free, fast on CPU (~0.15–0.2 real-time factor per the report) |
| Tamil TTS | **Piper** `ta_IN-rasa_female-medium` | WSL2 HTTP server | Only free commercial-use Tamil option considered |

**Rejected** (do not re-propose without new evidence): Nemotron blueprint (needs GPU), Pipecat's `ParakeetSTTService` (cloud, needs key), plain Whisper base/small for Tamil, MMS-TTS Tamil (non-commercial licence), Kokoro for Tamil.

**Planned, not done:** compare Parakeet via `parakeet-api` for English STT; fallback Tamil STT `vasista22/whisper-tamil-medium`; benchmark Tamil-LLaMA; bigger-model bake-off on a GPU (Qwen3 8B/14B, Sarvam-30B).

**Why Piper is a separate HTTP server:** keeps GPL-licensed Piper code out of this application's process (comment in `bot.py`).

## 4. Repository map

| File | Purpose |
|---|---|
| [bot.py](../bot.py) | The whole agent: config, prompts, preflight, pipeline, transports, latency logging, call lifecycle |
| [banking_tools.py](../banking_tools.py) | Mock bank data, per-call `SessionState`, and the 6 LLM tools |
| [stt_indic.py](../stt_indic.py) | `IndicConformerSTTService`: custom Pipecat STT that calls the WSL2 server |
| [run_bot.ps1](../run_bot.ps1) | Sets `PYTHONUTF8=1` and runs `uv run bot.py -t webrtc` |
| [test_banking_tools.py](../test_banking_tools.py) | 6 offline tests of the tool logic (no Ollama needed) |
| [tool_call_eval.py](../tool_call_eval.py) | Runs scripted conversations against a real Ollama model and reports tool-call pass rates |
| `tool_call_eval_results_*.json` | Saved eval output (4B model, with and without the prompt examples) |
| [text_chat_test.py](../text_chat_test.py) | Typed text → Ollama (streaming) → Piper → `.wav`; reports time to first token / sentence / audio |
| [tamil_llm_test.py](../tamil_llm_test.py), `tamil_llm_results.json` | 10 Tamil questions to two models; raw answers + timing for human review |
| [tamil_tts_test.py](../tamil_tts_test.py) | Synthesises 5 Tamil sentences to `test_audio/` |
| [latency_report.py](../latency_report.py) | Summarises `logs/latency.jsonl` (median/p90/max + where time goes) |
| [phase0_check.py](../phase0_check.py) | Environment check (11 checks): Python version, imports, Ollama, Piper |
| [main.py](../main.py) | Unused `uv init` stub. Ignore |
| `README.md` | Empty |
| `docs/production_readiness_report.md` | Concurrency, cost, storage, tenancy, accuracy analysis |
| `.env` (git-ignored) | Config; no secrets today |

## 5. `bot.py` in detail

### 5.1 Configuration (env vars, read at import via `load_dotenv(override=True)`)

All are **process-wide**. `.env` overrides the shell.

| Variable | Default | Meaning |
|---|---|---|
| `LANGUAGE` | `en` | `en` or `ta`. Picks STT, LLM, voice, prompt, and whether tools are on |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint (no real key needed) |
| `OLLAMA_MODEL` | `qwen3:4b-instruct-2507-q4_K_M` (en) / `gemma3:4b` (ta) | LLM |
| `PIPER_URL` | `http://localhost:5000/synthesize` | piper-tts 1.8.0 synthesises on `POST /synthesize` |
| `PIPER_VOICE` | `en_US-lessac-medium` (en) / `ta_IN-rasa_female-medium` (ta) | Voice name sent to Piper |
| `MOONSHINE_MODEL` | `small-streaming` | tiny, base, tiny-streaming, base-streaming, small-streaming, medium-streaming |
| `INDIC_STT_URL` | `http://localhost:5001/transcribe` | Tamil STT server |
| `MAX_REPLY_TOKENS` | `120` | Cap on LLM reply length (voice replies must be short) |
| `EXOTEL_SAMPLE_RATE` | `16000` | Must match `?sample-rate=` on the Exotel Voicebot Applet URL |
| `LOG_DIR` | `logs` | Where `latency.jsonl` is written |

**Important:** "language per session" is a **design goal, not built**. Language is per *process*. To serve Tamil and English at once you would run two processes on different ports, or refactor `run_bot()` to take language as an argument. CLAUDE.md's "Tamil/English toggle" is not implemented.

### 5.2 System prompts

Three pieces get combined by `.format()` in `run_bot()`:
1. `SYSTEM_PROMPT_EN` / `SYSTEM_PROMPT_TA`: spoken-style rules (no emojis, bullets, formatting; 1–2 short sentences), the real current date/time (`{current_datetime}`), and a `{tools_guidance}` slot.
2. `TOOLS_GUIDANCE` (English): bank-agent role, the list of allowed tools, rules (verify before sharing data, never invent numbers, call tools instead of *saying* you did), and **two worked examples** (human request → `transfer_to_human`; "bye" → `end_call`).
3. `CHAT_ONLY_GUIDANCE` (Tamil): "you have no tools; say you can't, never pretend".

Each line exists because of a **live failure**, recorded in code comments:
- The model guessed the wrong date → real date injected per session (Ollama has no clock).
- The model claimed to set alarms / check weather → "you can ONLY do what your tools allow".
- The 4B model said "I'll transfer you" without calling the tool → the two worked examples (see section 7).

### 5.3 `strip_markdown_and_emoji` (code-side guardrail)

Wrapped in a `StatelessTextTransformer` placed between the LLM and TTS. A 4B model does not reliably obey "no markdown" (qwen3 wrapped Tamil in `**bold**`), and Piper would read the symbols aloud. It removes emoji ranges, leading `-`/`•` bullets, and `* _ ` #`. **Principle used throughout: enforce in code what matters; the prompt only makes the model polite.**

### 5.4 `transport_params` and the entry point `bot()`

- `transport_params` has `"webrtc"` (browser) and `"twilio"` (phone). `create_transport()` picks one and, for Twilio, auto-detects the WebSocket handshake and builds `TwilioFrameSerializer`.
- **Exotel is hand-built in `bot()`** because Pipecat's `_create_telephony_transport()` always makes `ExotelFrameSerializer` at 8 kHz and overwrites any serializer you pass. So `bot()` peeks at the handshake with `parse_telephony_websocket()` (result is cached on the websocket), and if it is Exotel, builds the serializer with `EXOTEL_SAMPLE_RATE` and a `FastAPIWebsocketTransport` itself. Everything else falls through to `create_transport()`.
- `run_bot(transport, runner_args)` is **transport-agnostic**; the pipeline never branches on transport type.

### 5.5 `preflight()` (fail fast)

Runs at the start of every session and **raises `PreflightError`** (the session does not start) if:
1. Ollama `/api/tags` is unreachable, or the configured model is not pulled.
2. (English only) Ollama `/api/show` capabilities lack `"tools"`.
3. Piper `POST /synthesize` with text `"ok"` does not return HTTP 200 and a body starting `RIFF`.
4. (Tamil only) IndicConformer `/health` is not HTTP 200.

Nothing supervises backends **after** startup; a backend dying mid-call is not handled.

### 5.6 `run_bot()`: building a session

1. Create `logs/`, make a `session_id` (UTC timestamp, **second resolution, collisions possible** under concurrent starts).
2. `preflight()`.
3. **STT:** `IndicConformerSTTService` (ta) or `MoonshineSTTService` (en).
4. **LLM:** `OLLamaLLMService`, `temperature=0.6`, `max_tokens=MAX_REPLY_TOKENS`, `system_instruction` = the formatted prompt.
5. **TTS:** `PiperHttpTTSService`. Pipecat splits the LLM stream into sentences and sends each to Piper as soon as it is complete, then resamples Piper's 22.05 kHz WAV to the output rate. This is how first audio starts before the LLM finishes.
6. **Context:** `LLMContext(tools=banking_tools.TOOLS)` for English, `NOT_GIVEN` for Tamil. (`tools=None` is rejected by `LLMContext`; this was a real bug.) A fresh `SessionState` is created per call.
7. **Aggregators:** `LLMContextAggregatorPair` with `SileroVADAnalyzer`. The user aggregator turns VAD + transcripts into complete user turns (default stop strategy: Smart Turn v3). The assistant aggregator records what the bot said into the context.
8. **Pipeline order:** `transport.input → stt → user_aggregator → llm → sanitize_for_speech → tts → transport.output → assistant_aggregator`.
9. **Worker:** `PipelineWorker` with metrics enabled, `UserBotLatencyObserver` and `TranscriptionLogObserver`, `app_resources=call_state` (this is how tool handlers get at `SessionState`), `idle_timeout_secs` from the runner, and `ProcessorUnusablePolicy.END`.

### 5.7 Event handlers

- **`on_client_connected`:** adds a `developer` message ("Greet the user in one short sentence.") and queues `LLMRunFrame()`, so the bot speaks first. (Ollama has no `developer` role; the service converts it.)
- **`on_client_disconnected`:** `runner.cancel()`.
- **`on_assistant_turn_stopped`:** if a tool set `end_requested` **and** the assistant turn has non-empty content, call `worker.end()`. Tools only set a flag; the call ends *after the goodbye is actually spoken*. The empty-content guard exists because the assistant turn right after a tool call is empty **[MEASURED]**.
- **`on_latency_breakdown`:** logs each turn and appends a JSON line to `logs/latency.jsonl` (see section 9).

## 6. One turn, end to end

1. Audio arrives from the transport (WebRTC Opus, or a WebSocket with Twilio mu-law 8 kHz / Exotel PCM 16-bit).
2. Silero VAD marks speech start; user may interrupt the bot here (Pipecat cancels the bot's current LLM/TTS output).
3. When VAD sees silence, Smart Turn v3 decides if the user has actually finished.
4. **English:** Moonshine transcribes in-process. **Tamil:** `SegmentedSTTService` buffers the utterance, then `IndicConformerSTTService.run_stt()` POSTs one WAV to `:5001/transcribe` (30 s timeout) and yields a `TranscriptionFrame` (or an `ErrorFrame` on failure; empty text yields nothing).
5. User aggregator appends the transcript to `LLMContext` and triggers the LLM.
6. Ollama streams tokens. If it emits a **tool call**, Pipecat runs the matching handler in `banking_tools.py`, feeds the result back, and the LLM continues.
7. `sanitize_for_speech` strips markdown/emoji.
8. TTS splits at sentence boundaries; each sentence is sent to Piper (`POST /synthesize`, returns a full WAV per sentence, no audio streaming), resampled, and played.
9. Assistant aggregator stores the reply. If `end_call` or `transfer_to_human` ran, the call ends after the spoken goodbye.

## 7. Tools (English only): `banking_tools.py`

**All data is MOCK, in memory.** Replace `MOCK_CUSTOMERS`/`MOCK_TICKETS` access with real backend calls and keep the tool surface. Test logins: customer `1001` PIN `4321` (Priya Raman, balance 48,250.75), customer `1002` PIN `9876` (Arun Kumar, balance 1,520.00). Tickets `T-1001` (open) and `T-1002` (resolved).

Tools are async "direct functions": first parameter is `params: FunctionCallParams`; Pipecat extracts the JSON schema from the signature and docstring (so **docstrings are the tool descriptions the LLM sees**). Per-call state arrives as `params.app_resources`.

| Tool | Needs verification | Behaviour |
|---|---|---|
| `verify_customer(customer_id, pin)` | no | Compares SHA-256 of PIN with `hmac.compare_digest`. Success stores `verified_customer_id`. 3 failures set `locked` (even a correct PIN afterwards fails). Unknown ID and wrong PIN return the **same** error |
| `get_account_balance()` | yes | Returns `balance_rupees` |
| `get_recent_transactions(count=3)` | yes | Newest first, count clamped 1–5 |
| `get_ticket_status(ticket_id)` | no | Looks up `MOCK_TICKETS` |
| `transfer_to_human(reason)` | no | **STUB.** Sets end flag; tells the caller a human will call back; call ends. No real transfer |
| `end_call()` | no | Sets end flag; model says goodbye; `bot.py` hangs up after it is spoken |

**Security design:** `_require_verified(state)` gates account data **in code**. The prompt also says it, but a 4B model can be talked out of a prompt rule. `SessionState.audit()` logs tool outcomes and never logs PINs.

**`SessionState` fields:** `verified_customer_id`, `failed_attempts`, `locked`, `end_requested`, `end_reason`, `events` (audit trail).

### 7.1 Known 4B tool-calling limits **[MEASURED]**, `tool_call_eval.py`, 10 runs per scenario

Failures are deterministic (0/10 or 10/10), not random.

| Scenario | Shipped prompt | Without the 2 examples |
|---|---|---|
| No balance leak before verification | 0% fail | not run |
| 3 wrong PINs → lockout | 0% | not run |
| Ticket lookup | 0% | not run |
| Weather refused, no tool | 0% | not run |
| Asks for human → `transfer_to_human` called | 0% | **100% fail** |
| "bye" → `end_call` called | 0% | **100% fail** |
| ID+PIN+question in one message → verify AND answer | **100% fail** | not run |
| ID+PIN+"bye" in one message → verify AND `end_call` | **100% fail** | 100% fail |

Pattern: the 4B model makes **one** tool call, then answers in text. It never chains a second call in the same turn. **Do not build workarounds yet** (CLAUDE.md): re-run `uv run tool_call_eval.py <model>` on bigger models (Qwen3 8B/14B, Sarvam-30B) during the GPU bake-off first. Never rely on `end_call` alone; caller hang-up and the idle timeout also end calls.

### 7.2 Adding a tool

1. Write `async def my_tool(params: FunctionCallParams, arg: str): """Description.\n\nArgs:\n    arg: ...\n"""`.
2. Gate with `_require_verified` if it touches account data.
3. Call `await params.result_callback({...})` exactly once.
4. Append it to `TOOLS`; mention it in `TOOLS_GUIDANCE`.
5. Add an offline test in `test_banking_tools.py` and a scenario in `tool_call_eval.py`; re-run the eval.

## 8. Tamil STT: `stt_indic.py` and the WSL2 server

- `IndicConformerSTTService(SegmentedSTTService)`: Pipecat buffers audio between VAD start/stop and calls `run_stt()` once per utterance. `wants_wav_segments = True` because the server expects a WAV file. Sample rate fixed at 16 kHz (the model's native rate).
- Server (`~/indic-stt-server/server.py`, **outside the repo**): Flask. `GET /health` → `{"status":"ok"}`. `POST /transcribe` with raw WAV bytes → `{"text": "..."}`. Loads the `.nemo` model once on startup using NeMo (`restore_from`, CPU), RNNT decoder, language id `ta`. Env overrides: `INDIC_STT_MODEL_PATH`, `INDIC_STT_DECODER`, `INDIC_STT_LANGUAGE`. The default model path contains a machine-specific HF cache hash.
- It uses a temp file per request and `batch_size=1`; there is no concurrency handling.
- **Tamil is chat-only** because `gemma3:4b` does not support tools in Ollama (HTTP 400). `TOOLS_ENABLED = LANGUAGE != "ta"`.
- Tamil reply quality has been reviewed only by an AI with basic Tamil; **a Tamil speaker must confirm it**. Tamil STT accuracy (WER) has **never been measured**.

## 9. Telephony

Both providers are WebSocket "second transports" served by the same `bot.py` on `/ws`. The dev runner registers `/ws` regardless of `-t`, so `-t webrtc` (what `run_bot.ps1` uses) does not block them. **Do not use `-t twilio`** (it restricts `/start` and breaks the browser UI).

### 9.1 Exotel (primary, in progress)

- Wire audio: raw 16-bit PCM (not mu-law), snake_case handshake fields (`stream_sid`, `call_sid`, `account_sid`).
- Sample rate is a query param on the Voicebot Applet stream URL: `?sample-rate=` 8000 (default), 16000 (recommended), 24000. We use 16000; set `EXOTEL_SAMPLE_RATE` to match.
- `ExotelFrameSerializer` takes **no credentials**.
- **[MEASURED]** with no Exotel account: a hand-written fake handshake over `ws://localhost:7860/ws` was detected as `exotel`, built the full pipeline and returned 3 `media` events (Piper and IndicConformer were stubbed that run).
- **OPEN ITEM: no call-hangup API found.** Nothing can end a live Exotel call from our side. Calls presumably end only when the caller hangs up or the App Bazaar flow routes to a Hangup applet; neither is confirmed. Ask Exotel support.
- **[ASSUMED]**: real phone audio quality, ngrok tunnel behaviour, real Voicebot Applet behaviour. All untested.

### 9.2 Twilio (kept, not the active choice)

- `transport_params["twilio"]`; mu-law 8 kHz, resampled by `TwilioFrameSerializer`.
- **Requires `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` in `.env`**; the serializer raises if either is missing (auto hang-up uses Twilio's REST API).
- **[MEASURED]** with a fake handshake and dummy credentials: pipeline ran, real greeting through Ollama and Piper, 18 `media` events returned.
- **Open risk:** 8 kHz audio may hurt STT accuracy. Unverified. Do not pre-emptively change anything.
- Point a TwiML `<Stream>` at `wss://<ngrok-host>/ws`.

### 9.3 Security gap

`/ws` has **no authentication** **[MEASURED]**: anyone who can reach it can start a session and spend compute. Exotel's serializer has no shared secret, so use network allow-listing or a token in the stream URL (neither verified).

## 10. Setup and running (new machine)

1. Install `uv`, Python 3.11. In the repo: `uv sync`. (`pyproject.toml` sets `link-mode = "copy"` because hardlinking failed with `os error 396` on this OneDrive-moved folder; keep it.)
2. Install Ollama (native Windows). `ollama pull qwen3:4b-instruct-2507-q4_K_M` and `ollama pull gemma3:4b`.
3. In WSL2 Ubuntu, set up **Piper** (`~/piper-server`, `piper-tts` 1.8.0 HTTP server on port 5000) with voices `en_US-lessac-medium` and `ta_IN-rasa_female-medium` in `models/`. The server's launch command is **not recorded in this repo** (its README is empty); the report says it is `piper.http_server` (Flask dev server). Verify against your install.
4. Tamil only: in WSL2 set up `~/indic-stt-server` with AI4Bharat's NeMo fork, download `indicconformer_stt_ta_hybrid_ctc_rnnt_large`, run `server.py` (port 5001, per the `INDIC_STT_URL` default). The server's own docstring says to run it with `.venv/bin/python server.py`, but no `.venv` exists in that folder on this machine, so how NeMo was installed there is **not documented**.
5. Create `.env` (see section 5.1; no keys needed).
6. Check: `uv run python phase0_check.py` (11/11 passed on 2026-09-29).
7. Run: `./run_bot.ps1`, then open `http://localhost:7860/client`.
8. First run costs: Moonshine downloads its model once (~54 s, blocks the pipeline), and Ollama's first call after idle takes ~10 s to load the model.

**Common errors:** `PreflightError` names exactly which backend is down. WSL2 stopped = Piper and IndicConformer both down.

## 11. Testing and evaluation

| Command | What it proves |
|---|---|
| `uv run python phase0_check.py` | Environment and backends reachable |
| `uv run python test_banking_tools.py` or pytest | 6 offline tool-logic tests (gating, lockout, uniform errors, end/transfer flags). **[MEASURED]** pass |
| `uv run tool_call_eval.py [model] [runs] [filter]` | Tool-calling pass rates on a real model. Exits 1 on any failing scenario. `EVAL_STRIP_EXAMPLES=1` removes the prompt examples |
| `uv run python text_chat_test.py ["text" ...]` | LLM + TTS legs without a mic; time to first token/sentence/audio |
| `uv run python tamil_llm_test.py` | Tamil answer comparison data (human review) |
| `uv run python tamil_tts_test.py` | Tamil TTS audio files in `test_audio/` |
| `uv run python latency_report.py [--last N]` | Latency stats from `logs/latency.jsonl` |

There is **no WER measurement, no regression suite, and no CI**.

**Latency logging:** each turn appends to `logs/latency.jsonl`: timestamp, session id, turn number, language, STT/LLM/TTS names, and Pipecat's `breakdown` (total and per-service contributions). Caveat: the first-request row includes idle time from connect, so medians/p90 over the whole file are not decision-grade.

## 12. Measured numbers worth knowing

- English LLM, warm: sentence 1 `llm_total` about 1.34 s. Cold Ollama start: about 10 s once.
- qwen3 4B: ~10.5 tok/s, ~117 completion tokens per Tamil reply. gemma3 4B: ~8 tok/s, ~27 tokens.
- Concurrency (English, Twilio-format path, `OLLAMA_NUM_PARALLEL=1`): 1–2 calls fine, 3 degraded (up to 25 s turns), 5 poor. The LLM is the bottleneck, not RAM (~300 MB per call).
- Bot memory did not fall after calls ended (leak or allocator retention not investigated).

## 13. Known limitations and open items (priority order)

1. **No tenancy, auth, storage, retention, or audit layer.** Greenfield (see the report, sections 4–5).
2. **Accuracy unproven**, especially Tamil (garbled output, one fabricated weather answer observed with gemma3).
3. **Capacity:** about 2 calls on this laptop. Needs GPU hosting and batched serving for more.
4. **Exotel hangup API unknown.**
5. **`transfer_to_human` is a stub**; no real call transfer.
6. **Tamil has no tools.** Revisit after the bigger-model bake-off.
7. **Language is per process**, not per session.
8. `session_id` collisions; unbounded unlocked `latency.jsonl`; transcripts appear in console logs at DEBUG; no backend supervision mid-call.
9. Bot not run end to end with audio for the tool-enabled build (Piper was down at the time). Tool flow was verified headless, and the earlier non-tool voice path was verified live.
10. Piper's server is a Flask dev server; not production grade.
11. The WSL2 servers are not in git and their setup is undocumented.

## 14. Conventions for contributors

- Read the installed Pipecat source before writing any Pipecat call (`.venv/Lib/site-packages/pipecat`). Version is 1.12.0.
- Run your code or its tests and report the actual result. State verified vs assumed.
- Prefer enforcing safety rules **in code**; treat prompts as best effort.
- Record decisions and measured results in `CLAUDE.md` using the same evidence tags.
- Keep Twilio code even though Exotel is primary.
- No paid services, no API keys, CPU only.
