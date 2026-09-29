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
