"""
Custom Pipecat STT service for AI4Bharat IndicConformer (Tamil), served over
HTTP by a small Flask server running inside WSL2 (~/indic-stt-server/server.py
in the WSL2 home directory - IndicConformer needs AI4Bharat's own NeMo fork,
which only installs on Linux, so the model itself can't run in-process here).

Built directly against Pipecat's actual base classes, read from the installed
package before writing this (per CLAUDE.md's rule against writing Pipecat API
calls from memory):
  - SegmentedSTTService (pipecat/services/stt_service.py): handles VAD-based
    audio buffering and calls run_stt() once per utterance. A subclass only
    needs wants_wav_segments and run_stt().
  - BaseWhisperSTTService (pipecat/services/whisper/base_stt.py): the
    reference HTTP-backed run_stt() shape (start/stop_processing_metrics
    around the call, TranscriptionFrame on success, ErrorFrame on failure).
"""

from collections.abc import AsyncGenerator

import aiohttp
from loguru import logger

from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.utils.time import time_now_iso8601

INDIC_STT_SAMPLE_RATE = 16000


class IndicConformerSTTService(SegmentedSTTService):
    """Tamil STT via AI4Bharat IndicConformer, called as one HTTP request per
    VAD-detected utterance (see SegmentedSTTService for the buffering)."""

    @property
    def wants_wav_segments(self) -> bool:
        """The WSL2 server expects a WAV file, not raw PCM."""
        return True

    def __init__(self, *, base_url: str, aiohttp_session: aiohttp.ClientSession, **kwargs):
        """Initialize the IndicConformer STT service.

        Args:
            base_url: URL of the WSL2 indic-stt-server's /transcribe endpoint.
            aiohttp_session: Shared HTTP client session (same pattern as
                PiperHttpTTSService's aiohttp_session in bot.py).
            **kwargs: Additional arguments passed to SegmentedSTTService.
        """
        super().__init__(sample_rate=INDIC_STT_SAMPLE_RATE, **kwargs)
        self._base_url = base_url
        self._session = aiohttp_session

    def can_generate_metrics(self) -> bool:
        return True

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Send one WAV segment to the WSL2 server and yield its transcript."""
        try:
            await self.start_processing_metrics()
            async with self._session.post(
                self._base_url,
                data=audio,
                headers={"Content-Type": "application/octet-stream"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                r.raise_for_status()
                body = await r.json()
            await self.stop_processing_metrics()
        except Exception as e:  # noqa: BLE001
            logger.error(f"{self}: IndicConformer STT request failed: {e}")
            yield ErrorFrame(error=f"IndicConformer STT failed: {e}")
            return

        text = (body.get("text") or "").strip()
        if not text:
            return

        logger.debug(f"Transcription: [{text}]")
        yield TranscriptionFrame(text, self._user_id, time_now_iso8601())
