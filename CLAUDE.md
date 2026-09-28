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
