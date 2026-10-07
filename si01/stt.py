"""Распознавание голосовых для обычного разговора. Whisper-совместимый API (OpenAI, Groq и т.п.).

Выключено по умолчанию (STT_ENABLED=false) — тогда SI-01 просит написать текстом.
Для проверки личности голосовое НЕ распознаётся, а просто пересылается админам.
"""

import logging

import aiohttp

log = logging.getLogger(__name__)


class SpeechToText:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.url = base_url.rstrip("/") + "/audio/transcriptions"
        self.api_key = api_key
        self.model = model

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str | None:
        form = aiohttp.FormData()
        form.add_field("model", self.model)
        form.add_field("language", "ru")
        form.add_field("file", audio, filename=filename, content_type="audio/ogg")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(self.url, data=form, headers={"Authorization": f"Bearer {self.api_key}"},
                                  timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status != 200:
                        log.warning("STT %s: %s", r.status, (await r.text())[:300])
                        return None
                    return ((await r.json()).get("text") or "").strip() or None
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("STT failed: %s", e)
            return None
