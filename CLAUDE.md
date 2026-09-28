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

## Status (2026-09-28, after move to C:\dev\voice_agent)

Done and verified:
- `.venv` deleted and rebuilt with `uv sync`. Plain hardlinking failed
  (`os error 396`, likely a leftover cloud-file attribute from the OneDrive
  move) - needed `UV_LINK_MODE=copy` to succeed. Worth setting that env var
  if `uv sync` is re-run on this machine and hits the same error.
- All of bot.py's actual imports (Moonshine STT, OLLamaLLMService,
  `PiperHttpTTSService`, SileroVADAnalyzer, etc.) succeed in the rebuilt venv.
- `phase0_check.py` run: 11/11 checks pass - Python 3.11.15, all five
  Pipecat service imports, Ollama reachable with
  `qwen3:4b-instruct-2507-q4_K_M` pulled and answering, Piper HTTP server
  (WSL2, port 5000) reachable and returning real WAV bytes.
- `.gitignore` now covers `.env`, `.venv`, `__pycache__`, common model-weight
  extensions (`*.onnx`, `*.onnx.json`, `*.gguf`, `*.bin`, `*.pt`, `*.ckpt`,
  `*.safetensors`) and a `models/` folder, and `*.wav` test/scratch audio.
- `run_bot.ps1` added (`PYTHONUTF8=1` then `uv run bot.py -t webrtc`).
  Started cleanly - Uvicorn up on `http://localhost:7860` - and stopped
  cleanly with no leftover process on that port.
- First local git commit made (`3646394`). Confirmed `.env` is not in the
  commit (`git ls-tree` and `git show --stat` both come up empty for it).

Untested (assumed working, not confirmed):
- No live spoken/microphone test yet - nobody has opened
  `http://localhost:7860/client` in a browser and actually talked to the
  bot. Preflight checks (Ollama chat, Piper WAV) passed, but the full
  browser mic -> VAD -> STT -> LLM -> TTS -> speaker loop is unverified.

Next: Tamil. AI4Bharat IndicConformer STT via a custom `SegmentedSTTService`
(fallback `vasista22/whisper-tamil-medium`), Tamil LLM benchmarking
(Qwen3 4B vs Gemma 3 4B vs Tamil-LLaMA), and the `ta_IN-rasa_female-medium`
Piper voice - none of this exists in bot.py yet, which is English-only
(Phase 1).
