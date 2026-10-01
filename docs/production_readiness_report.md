# Production Readiness Report: Local Voice Agent as a Multi-Tenant SaaS

Date: 2026-09-30. Scope: research and analysis only. No project code was changed.

**Evidence tags used throughout**
- **[CODE]**: read directly from this repo or the installed Pipecat 1.12.0 source.
- **[MEASURED]**: observed by running something on this laptop today.
- **[RESEARCH]**: from web sources (linked at the end). Vendor blogs are marketing-grade; treat as directional.
- **[INFERRED]**: my reasoning from the above. Not verified.

---

## Executive verdict

**No. As built, this is a single-user prototype, not a multi-tenant production system.** It handles roughly 2 comfortable simultaneous calls on this laptop, has no concept of a customer/tenant, stores nothing durable and nothing securely, has no authentication on its call endpoint, and has no accuracy evidence for Tamil or for regulated-sector use. The *pipeline design* (Pipecat: VAD, STT, LLM, TTS with interruption) is a sound foundation and much of it is reusable. The *system around it* does not exist yet.

Top 3 blockers, in priority order (detail in Section 7):
1. No tenancy, auth, secure storage, retention, or audit layer. Nothing can be sold to a bank, hospital, or agency without this.
2. No accuracy/safety evidence, and the early data is worrying: 4B models produced garbled Tamil and a fabricated answer.
3. Capacity: CPU-only, one shared Ollama serving one request at a time, no session management. Cheapest of the three to fix, but it forces a hosting change.

---

## 1. Concurrency: what the code supports today

**[CODE]**
- `bot(runner_args)` in `bot.py` is called by the Pipecat runner once per connection. `_start_bot_session` in `pipecat/runner/run.py` is just `asyncio.create_task`. There is no session cap, semaphore, queue, or admission control.
- **Per-session objects** (created inside `run_bot`): `Pipeline`, `MoonshineSTTService` (loads its own ONNX model in `__init__`), `SileroVADAnalyzer`, `OLLamaLLMService` client, `PiperHttpTTSService` client, `LLMContext`.
- **Shared, out-of-process backends (contention points):** one Ollama server, one Piper HTTP server (WSL2), and for Tamil one IndicConformer HTTP server (WSL2). Nothing in this repo pools, load-balances, or throttles them.
- Sessions do not conflict on state, since each has its own pipeline. But they all hit the same three servers.
- **Design flaws that surface under concurrency:**
  - `session_id` is a **second-resolution timestamp** (`bot.py:244`). Calls connecting in the same second get the same ID. My 3- and 5-call tests connected within milliseconds of each other, so they would have collided in the latency log.
  - `LANGUAGE`, the model and the system prompt are process-wide globals read from `.env`. "Language per session" does not exist; it is per process. (`CLAUDE.md` describes a toggle as planned, not built.)
  - Piper is served by `piper.http_server`, which is Flask's development server.
  - `preflight()` runs once per session start; nothing supervises or restarts dead backends mid-call.
- **Unauthenticated endpoint [MEASURED]:** my fake client connected to `ws://localhost:7860/ws` with a hand-written Twilio JSON handshake and dummy credentials, and the server built a full pipeline and started spending LLM/TTS compute. There is no Twilio signature or token validation in the code. Anyone who can reach that URL can start sessions.

**[MEASURED] What actually happens with simultaneous calls**

Method: unmodified copy of `bot.py` run from a scratch directory with its own `.env` (English, Moonshine `small-streaming`, `qwen3:4b-instruct-2507-q4_K_M`, Piper `en_US-lessac-medium`). A scratch client opened N Twilio-format WebSockets at once, each sending a 5.5 s spoken utterance as 8 kHz μ-law. This is the same path a real Twilio call would use. Latency is from the bot's own `UserBotLatencyObserver` logs. Ollama was running with **`OLLAMA_NUM_PARALLEL:1`** (read from its server log).

| Simultaneous calls | Greeting latency (turn 1) | Turn-2 latency | Outcome |
|---|---|---|---|
| 1 (warm) | 2.5 s | 5.9 s | Works |
| 2 | 2.9 s / 4.2 s | 6.8 s / 7.4 s | Works, slight lag |
| 3 | 2.9 / 5.5 / 6.8 s | 8.7 / 12.6 / **25.5 s** | Degraded |
| 5 | 2.7 / 4.0 / 5.3 / 6.6 / 8.3 s | 11.3 / 11.9 / 13.2 / 14.7 / 16.6 s | Poor, but no crash or error |

- Greeting latencies form a **staircase of about 1.3 s per call**, the signature of a single-file queue in front of the LLM. It is not random noise.
- **Failure mode: queue and degrade, not crash.** No errors or tracebacks in any run. From a caller's view, 8 to 25 s of silence per turn is a failed call.
- The first ever session cost **15 s to first speech** (model loads and Ollama cold start), consistent with earlier notes in `CLAUDE.md`.

**Caveats [MEASURED but limited]:** the callers were clean synthetic TTS speech (optimistic for STT), all callers spoke in the same instant (harsher than staggered real calls), English only (the IndicConformer/Tamil path needs NeMo in WSL and was not run), one run per N (no repeats, no statistics).

## 2. Resource math

**[MEASURED]** (Windows process sampling at 1 s; free RAM was only about 2 GB before I started, so the machine is already loaded by other apps.)

| | Idle | 1 call | 5 calls |
|---|---|---|---|
| Bot Python process (RAM) | 138 MB | 457 MB | 1,658 MB |
| Ollama `llama-server` (RAM, shared) | n/a | ~2.7 GB | ~2.7 GB (it does not grow with sessions) |
| Piper WSL2 VM (RAM, shared) | n/a | ~1.0 GB | ~1.1 GB |
| Total CPU (16 threads) | n/a | peak 82% | peak 100% |
| CPU core-seconds per call (1 greeting + 1 reply) | n/a | ~11 (bot) + ~33 (LLM) | ~13 (bot) + ~22 (LLM) each |

- **Per-call marginal RAM: about 300 MB** (Moonshine model, Silero, Smart Turn, pipeline). Shared fixed cost about 3.7 GB. RAM is *not* the binding constraint: 13.8 GB usable would in theory hold about 30 sessions' worth of Python.
- **The binding constraint is LLM decode throughput, not RAM.** CPU llama.cpp decodes about 8 to 10 tok/s (`tamil_llm_results.json`: 10.5 tok/s qwen3, 8.0 gemma3). A typical 25 to 100 token reply occupies the LLM for 3 to 10 s. With `NUM_PARALLEL=1` that serializes everything.
- **Bot memory did not fall after sessions ended** (1.6 GB after the 5-call run, 418 MB after the 1-call run). Could be a leak or allocator retention; I did not investigate. This needs a soak test before any production claim.

**[INFERRED] Capacity of this laptop**
- Assume a caller turn every ~12 s and ~3.5 s LLM service time per reply, so each live call uses ~30% of the single LLM lane. That is **about 2 calls comfortably, 3 with visible lag, and 4 or more unusable.**
- Raising `OLLAMA_NUM_PARALLEL` on CPU does not create capacity. It splits the same 8 to 10 tok/s across requests. I did not test this.
- **100+ calls would need on the order of 50 or more machines like this one.** That is arithmetic on my measurement, not a benchmark, but the order of magnitude is not in doubt.

**[RESEARCH] How production self-hosted stacks get concurrency**
- They move the LLM and (increasingly) STT/TTS to **GPUs with batched serving (vLLM/SGLang)**, and run many bot worker processes behind a router. Cramming more concurrent requests per GPU lowers cost/minute but raises queuing delay and time-to-first-token ([Layer3Labs](https://www.layer3labs.io/guides/self-hosted-voice-agent-stack), [voice-cost-bench](https://github.com/asadrizv/voice-cost-bench)).
- Published per-GPU concurrency figures are scarce and mostly vendor-authored. One claim of 88 sessions per B200 ([MindStudio](https://www.mindstudio.ai/blog/phonellm-cost-per-minute-voice-agents)) sits next to internally inconsistent cost figures ($0.00025/min in one place, $0.2232/min in another), so **I do not rely on it.** A gigagpu.com page cites 2 to 5 calls on an RTX 3090 with a 7B LLM, with no method shown.
- The most credible source I found says outright that concurrency per GPU "is the number you cannot look up", so you must measure it for your own models. **Nobody can tell Sirah the concurrency number without a load test on the real stack on real hardware.**
- **CPU-only at meaningful scale:** I found no published benchmark of a CPU-only LLM voice pipeline serving tens of concurrent calls. Piper (about 0.15 to 0.2 real-time factor on a laptop CPU) and Moonshine are cheap on CPU ([Moonshine benchmarks](https://moonshine-voice.readthedocs.io/en/latest/using/benchmarks/)); the LLM is what breaks it. **CPU-only is not realistic beyond a handful of calls.**

## 3. Cost per call

**[RESEARCH] Inputs (prices change; verify before quoting to anyone)**

| Item | Number | Source |
|---|---|---|
| Twilio US inbound local | $0.0085/min (from $0.0080 at 100k min/mo) | [Twilio](https://twilio.com/voice/pricing) |
| Twilio Media Streams | $0.0040/min (US); $0.0044/min shown on the India page | Twilio pricing pages |
| Twilio India outbound | $0.0699/min local, $0.0496/min mobile. **I could not confirm an India inbound-PSTN rate**; the page fetched showed only browser/SIP inbound | [Twilio India](https://www.twilio.com/en-us/voice/pricing/in) |
| GPU: L4 | from ~$0.39/h (RunPod) | [RunPod listings](https://computeprices.com/providers/runpod/gpus/l4) |
| GPU: RTX 4090 / L40S / A100 80GB | $0.74 / $0.99 / $1.39 per h (RunPod secure, Aug 2026) | [Layer3Labs](https://www.layer3labs.io/guides/self-hosted-voice-agent-stack) |
| GPU: A100 elsewhere | $1.49 to $3.43/h; Lambda $2.06/h | [Jarvislabs](https://jarvislabs.ai/blog/a100-price) |
| GPU: H100 | ~$2.90 to $3.99/h; community $1.80 to $2.40/h | [Spheron](https://www.spheron.network/blog/runpod-h100-pricing-2026/) |
| Commercial voice-agent all-in | $0.07 to $0.30/min; bundled platforms $0.10 to $0.18; Vapi-style BYOK $0.13 to $0.31; Deepgram Voice Agent flat $4.50/h ($0.075/min); ElevenLabs ~$0.08/min + LLM | [Techsy](https://techsy.io/es/blog/ai-voice-agent-pricing), [HackerNoon](https://hackernoon.com/voice-agent-apis-in-2026-compared-which-one-actually-hears-your-users) |

Telephony is roughly **$0.013/min (US) on either approach** and does not disappear by self-hosting. India telephony pricing must come from your actual Indian carrier or Twilio quote.

**[INFERRED] Self-hosted cost per minute** = GPU $/h ÷ (60 × concurrent calls × utilisation), plus telephony, plus people.
Using the L40S at $0.99/h (about $723/month if on 24/7) and **assumed** concurrency, because it has not been measured for this stack:

| Scenario | Assumption | GPU cost/min |
|---|---|---|
| Low volume, ~10,000 call-min/month | GPU 24/7, almost idle | ~$0.072 (about equal to commercial APIs) |
| Small production | 10 concurrent at 30% average utilisation | ~$0.0055 |
| High volume (100+ concurrent) | ~4 to 8 L40S-class GPUs at high utilisation, about $3 to $6k/month | $0.002 to $0.01, **only if** per-GPU concurrency is around 15 to 30 (unmeasured) |

**Crossover point [RESEARCH + INFERRED]:** the Layer3Labs guide puts hardware-only break-even against a $0.08/min managed price at **about 6,750 to 9,030 minutes/month**, but **about 56,750 minutes/month once you count ~$4k/month of engineering time**. That guide's assumptions are its own. My arithmetic ($723 ÷ $0.08 ≈ 9,000) reproduces its hardware-only number.

**Is Sirah above or below?** I don't know Sirah's volume. **[INFERRED]** One always-busy line for a working month is about 10,000 minutes, so "1 to 10 simultaneous calls" at realistic occupancy is roughly 3,000 to 100,000 minutes/month. That straddles the engineering-inclusive crossover. In year one, with few customers, Sirah is more likely **below or near** the threshold. Self-hosting would then save little against commercial APIs while adding the cost of running 24/7 GPU infrastructure and carrying compliance yourselves. **The MD should supply expected minutes per month per customer; that number decides this.**

**Important accuracy caveat on the cost model:** the LLM and Tamil models in this repo are 4B. If Tamil quality requires a 9B to 30B+ model or a commercial API (see Section 6), GPU sizing and cost rise substantially. The estimates above assume the small-model stack works well enough, which is not established.

## 4. Storage and data handling

**[CODE] What exists**
- The only persistence is `logs/latency.jsonl` (`bot.py:349`): per-turn timing breakdowns plus session timestamp, language, and model names. **No transcripts, audio, caller ID, or customer ID are written to it.** (`.gitignore` excludes `logs/`.)
- **No database, no ORM, no object storage, no recording feature.** `.env` has only `LOG_DIR` as a storage-related variable; no DB URL, bucket, or key.
- **Transcripts do leak into console logs [MEASURED]:** with `TranscriptionLogObserver` and DEBUG logging on, stdout contained caller transcripts and the full LLM context. Anyone who redirects stdout to a file (I did for testing) creates an unencrypted, unmanaged store of caller speech.
- Appends to `latency.jsonl` are unlocked and unbounded (no rotation).

**[RESEARCH] Typical retention drivers (general practice, not legal advice)**
- **HIPAA (medical, US):** required documentation kept 6 years; no single rule for recorded calls, which follow state medical-record and program rules ([Accountable HQ](https://www.accountablehq.com/post/hipaa-minimum-retention-period-the-6-year-requirement-explained)).
- **Banking (India):** RBI-related recovery calls must be recorded and retained at least 6 months under a mandate effective 1 Jan 2027; KYC records at least 5 years after the relationship ends ([Exotel](https://exotel.com/rbi-loan-recovery-guidelines), [Gridlines](https://gridlines.io/blogs/video-kyc-audit-trail/)). UK FCA: at least 6 months.
- **Government:** varies by agency. Examples: FEMA 6 years, Services Australia 7 years, CMS Medicare enrollment 10 years.
- **India data location:** RBI requires payment-system data to be stored only in India. The DPDP Act permits cross-border transfer except to restricted countries, with consent ([Treelife](https://treelife.in/?p=16560), [IndiaLaw](https://www.indialaw.in/blog/cyber-law/building-compliance-in-the-cloud-rbis-ifs-cloud-and-the-future-of-financial-data-localization-under-dpdpa-2023//pdf/)). Practically, a bank customer will expect India-region hosting.

**Honest answer for this project: none of the required capabilities exist.** There is no encrypted storage, no retention or deletion policy engine, no per-tenant data isolation, no consent/recording-notice handling, no PII/PHI redaction, no access control or audit trail, and no key management.

## 5. Multi-tenant readiness

**[CODE] There is no concept of a customer or tenant anywhere.** Specifically:
- No tenant/customer/account identifier, table, config file, or request field.
- The system prompt is one global constant (`SYSTEM_PROMPT_EN/TA`); models, voice and language come from a single `.env`, so every caller of the process gets the same persona and knowledge.
- Twilio credentials are one pair of global env vars (`TWILIO_ACCOUNT_SID/AUTH_TOKEN`), not per-tenant.
- No mapping from dialled number to customer; no per-tenant usage metering, quotas, billing, or rate limits; no admin API or dashboard.
- No authentication on `/ws` (Section 1), and no session-scoping beyond the (collision-prone) timestamp.
- The bot has **no tools or integrations** by design. It cannot look up an account, check a balance, or book anything. Customer support in banking/telecom/medical requires backend integrations and human handoff; neither exists.

This is a greenfield layer, not a "mostly there" one.

## 6. Accuracy and reliability risk

**[MEASURED/CODE] Issues already observed in this project**
- **Qwen3 ignored length instructions in Tamil** (`tamil_llm_results.json`): about 100 tokens median (max 221) at 10.5 tok/s and 11.1 s average per reply, despite a "one or two short sentences" prompt. Gemma3 was terse (27 tokens median) at 4.1 s average.
- **Gemma3 fabricated a live-weather answer** to "how is the weather today?" (it asserted clear skies and sun), even though the system prompt says it cannot check weather. My read of Tamil text is not expert; a Tamil speaker should confirm. This is precisely the behaviour that is unacceptable in a bank or clinic.
- **Qwen3 output was visibly garbled Tamil** on several answers, and `bot.py` documents a hallucinated dish name and stray markdown from Qwen3. English testing showed the model claiming to set alarms and inventing the date; the fixes are prompt patches and a regex sanitizer (`bot.py:100-152`), not guarantees.
- **Tamil STT on phone audio is unverified.** `CLAUDE.md` records the open risk. There is **no WER measurement anywhere in the repo**, no evaluation set, and no regression suite. Today's English 8 kHz test used clean synthetic speech and transcribed correctly, which says little about real callers.
- **Latency data is not decision-grade.** The 71-row `latency.jsonl` reports a median of 11.3 s and a p90 of 810 s because "first request" counts idle time from connect to first speech, and rows mix languages, models and one user.

**[RESEARCH] What commercial vendors publish for regulated buyers**
- The standard checklist: **SOC 2 Type II, ISO 27001, HIPAA (with BAA), PCI DSS, GDPR**, plus telephony-consent rules (TCPA), data residency, PII/PHI redaction, third-party pen-testing, and a financially backed uptime SLA ([Trillet](https://trillet.ai/blogs/voice-ai-compliance-standards-explained), [Telnyx](https://telnyx.com/resources/soc-2-voice-ai-agents), [Kommunicate](https://www.kommunicate.io/blog/voice-ai-security-compliance-checklist/)). A SOC 2 report is only meaningful if it covers the call-path layers, not just the orchestration API.
- I did not find verified vendor accuracy or uptime numbers in my searches (only marketing). I am not quoting any.
- I could not retrieve a reliable IndicConformer Tamil WER on telephony audio; the search snippets were inconsistent and I did not use them.

**Equivalent evidence in this stack today: none.** No certifications, no SLA, no pen-test, no accuracy benchmark, no monitoring/alerting, no incident process. Using open-source models does not by itself prevent any of these; a bank will still ask for the evidence.

## 7. Bottom line

**As currently built, it cannot handle "many customers, many simultaneous calls" in production.** It is a solid single-user prototype of the conversational pipeline. A working, measured demo is a real asset; but the distance to a regulated SaaS is large, and it is mostly not in the pipeline code.

**Top 3 blockers, in priority order**

1. **No tenancy, security, or data-governance layer (architecture).**
   - Missing: tenant model and per-tenant config (prompt, voice, language, number mapping); authentication on the call endpoint (verify Twilio signatures or tokens); a database; encrypted recording/transcript storage with per-tenant isolation and keys; configurable retention/deletion; consent announcements; PII redaction; audit logs; RBAC; India-region hosting; per-tenant metering and rate limits.
   - Also needed for real support work: backend integrations (tools) and human handoff.
   - Order of effort: months of engineering, plus ISO 27001 / SOC 2 (typically 6 to 12 months, my general-knowledge estimate; not researched here). Blocks any bank, hospital or government sale.

2. **No accuracy/safety evidence, and early evidence is negative (model choices plus process).**
   - 4B models hallucinate and follow instructions unreliably, especially in Tamil.
   - Needed: a labelled evaluation set per language and per sector (WER on real 8 kHz phone audio, task success, hallucination/refusal rate), guardrails and grounding (retrieval from customer-approved knowledge, refuse-and-escalate policy), and likely a larger open model or a commercial API for Tamil and for anything advice-like.
   - Decision to make: whether Tamil quality justifies self-hosting at all in year one.

3. **Capacity and hosting (hosting plus architecture).**
   - Measured limit is about 2 to 3 calls on this laptop. The fix is a GPU host with batched serving (vLLM/SGLang), STT/TTS as scalable services, multiple bot workers behind a router with admission control, proper health checks, supervision and autoscaling, in an India-region cloud.
   - Cost is manageable ($0.7 to $3.9/h per GPU), but the concurrency-per-GPU number must be **measured on the real stack** before pricing anything. Also run a soak test for the memory growth I saw.

**Suggested honest path for the MD (my recommendation, [INFERRED]):** treat year one as a hybrid. Build the tenancy/security/data layer on top of Pipecat (it is transport- and model-agnostic), use commercial STT/LLM/TTS APIs behind the same interfaces for launch customers to get compliant and accurate quickly, and keep the open-source stack as a cost-reduction option once volume passes roughly 50k minutes/month and an evaluation set proves quality.

---

## Addendum (2026-10-01): effect of the Twilio to Exotel decision

`CLAUDE.md` now records Exotel as the primary telephony provider (Twilio code kept). What that changes in this report. Sources: `CLAUDE.md` plus its cited Exotel docs; I have not re-verified them.

- **Telephony cost and compliance (Section 3, 4): improves, unquantified.** I have no Exotel price. The Twilio figures above no longer apply to the primary path. Get written quotes for inbound number rental, per-minute rate, streaming support, and where recordings are stored. India-based providers also ease the data-location expectations in Section 4.
- **STT risk (Section 6): partly reduced.** Exotel streams 16-bit PCM at a configurable rate (16 kHz in use), so the Twilio 8 kHz mu-law concern no longer applies on this path. Real-line quality is still untested.
- **Concurrency (Section 1, 2): unchanged.** My measurements used the Twilio-format path on the earlier `bot.py`; the Exotel transport is built by hand in a newer `bot.py` that I did not re-test. The LLM bottleneck is transport-independent, so I expect the same limit, but that is inferred.
- **New blocker: no call-hangup mechanism found (open item in `CLAUDE.md`).** Calls may only end when the caller hangs up. In production this means stuck sessions holding compute, plus billing and compliance implications. Ask Exotel support before launch.
- **Unchanged:** no authentication on `/ws`, no tenancy, no storage layer, no accuracy evidence. Note that `ExotelFrameSerializer` takes no credentials, so there is no shared secret to validate. Authentication must come from network controls (allow-listing Exotel's IPs, if they publish them) or a token in the stream URL. I did not verify either option.

## Appendix: methodology and reproducibility
- Test harness (not committed): `bot.py` and `stt_indic.py` copied unchanged into the session scratchpad with its own `.env` (`LANGUAGE=en`, dummy Twilio credentials). `load_client.py` opens N concurrent Twilio-format WebSockets and streams 8 kHz μ-law speech (from `test_audio/reply_02.wav`). `monitor.ps1` samples process RAM/CPU once per second. Piper was started manually in WSL2 for the test and left stopped afterwards.
- Project files were not modified; `git status` before and after showed the same changes. Only new file: this report.
- Not done: Tamil/IndicConformer concurrency, real phone audio, staggered-arrival load, repeated runs, GPU or CPU-parallel Ollama settings, long-duration soak.

## Sources
- Twilio: [US pricing](https://twilio.com/voice/pricing), [India pricing](https://www.twilio.com/en-us/voice/pricing/in)
- GPU: [Jarvislabs A100](https://jarvislabs.ai/blog/a100-price), [Spheron H100](https://www.spheron.network/blog/runpod-h100-pricing-2026/), [RunPod L4](https://computeprices.com/providers/runpod/gpus/l4)
- Voice stack economics: [Layer3Labs](https://www.layer3labs.io/guides/self-hosted-voice-agent-stack), [voice-cost-bench](https://github.com/asadrizv/voice-cost-bench), [MindStudio (unreliable)](https://www.mindstudio.ai/blog/phonellm-cost-per-minute-voice-agents), [Techsy](https://techsy.io/es/blog/ai-voice-agent-pricing), [HackerNoon](https://hackernoon.com/voice-agent-apis-in-2026-compared-which-one-actually-hears-your-users)
- Moonshine/Piper: [Moonshine benchmarks](https://moonshine-voice.readthedocs.io/en/latest/using/benchmarks/)
- Retention/data law: [Exotel RBI](https://exotel.com/rbi-loan-recovery-guidelines), [Gridlines](https://gridlines.io/blogs/video-kyc-audit-trail/), [Accountable HQ HIPAA](https://www.accountablehq.com/post/hipaa-minimum-retention-period-the-6-year-requirement-explained), [Treelife](https://treelife.in/?p=16560), [IndiaLaw](https://www.indialaw.in/blog/cyber-law/building-compliance-in-the-cloud-rbis-ifs-cloud-and-the-future-of-financial-data-localization-under-dpdpa-2023//pdf/)
- Compliance: [Trillet](https://trillet.ai/blogs/voice-ai-compliance-standards-explained), [Telnyx](https://telnyx.com/resources/soc-2-voice-ai-agents), [Kommunicate](https://www.kommunicate.io/blog/voice-ai-security-compliance-checklist/)
