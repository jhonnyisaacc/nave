"""Supadata-backed YouTube transcript acquisition.

The API key is read at runtime from ``SUPADATA_API_KEY`` (or the compatible
``SUPADATA_API_TOKEN`` alias).  It is never persisted in a result or report.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

import httpx


SUPADATA_TRANSCRIPT_URL = "https://api.supadata.ai/v1/transcript"


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    source: str
    available_at: datetime


class TranscriptUnavailable(RuntimeError):
    """Transcript was not available and must not advance the video cursor."""


class TranscriptProvider(Protocol):
    def fetch(self, video_id: str) -> Transcript: ...


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "").strip()
            for item in content
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        return " ".join(parts).strip()
    return ""


class SupadataTranscriptProvider:
    """Fetch a YouTube transcript using the current Supadata API contract."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = SUPADATA_TRANSCRIPT_URL,
        language: str | None = "es",
        timeout_seconds: float = 45.0,
        poll_attempts: int = 3,
        poll_wait_seconds: float = 2.0,
        http: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Any] | None = None,
    ):
        self.api_key = api_key or os.getenv("SUPADATA_API_KEY") or os.getenv("SUPADATA_API_TOKEN")
        self.base_url = base_url.rstrip("/")
        self.language = language
        self.timeout_seconds = timeout_seconds
        self.poll_attempts = max(0, poll_attempts)
        self.poll_wait_seconds = max(0.0, poll_wait_seconds)
        self._http = http
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or time.sleep

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.timeout_seconds)
        return self._http

    def _request(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise TranscriptUnavailable("SUPADATA_API_KEY is not configured")
        try:
            response = self._client().get(
                url,
                params=params,
                headers={"x-api-key": self.api_key, "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TranscriptUnavailable(f"Supadata transcript request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise TranscriptUnavailable("Supadata returned a non-object transcript payload")
        return payload

    def _as_transcript(self, payload: dict[str, Any]) -> Transcript | None:
        text = _content_text(payload.get("content"))
        if not text:
            return None
        return Transcript(
            text=text,
            language=str(payload.get("lang") or "") or None,
            source="supadata",
            available_at=self._clock(),
        )

    def fetch(self, video_id: str) -> Transcript:
        if not video_id.strip():
            raise TranscriptUnavailable("video ID is required")
        params: dict[str, Any] = {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "text": "true",
            "mode": "auto",
        }
        if self.language:
            params["lang"] = self.language
        payload = self._request(self.base_url, params=params)
        transcript = self._as_transcript(payload)
        if transcript:
            return transcript

        job_id = str(payload.get("jobId") or "").strip()
        if not job_id:
            raise TranscriptUnavailable("Supadata returned no transcript content")

        job_url = f"{self.base_url}/{job_id}"
        for attempt in range(self.poll_attempts + 1):
            if attempt:
                self._sleep(self.poll_wait_seconds)
            result = self._request(job_url)
            transcript = self._as_transcript(result)
            if transcript:
                return transcript
            status = str(result.get("status") or "").lower()
            if status in {"failed", "error", "cancelled"}:
                raise TranscriptUnavailable(f"Supadata transcript job {job_id} ended {status}")
        raise TranscriptUnavailable(
            f"Supadata transcript job {job_id} was not ready after {self.poll_attempts + 1} checks"
        )
