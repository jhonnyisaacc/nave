from datetime import UTC, datetime

import httpx

from research.cava.pipeline import CavaWorkflow, parse_rss
from research.cava.transcript import SupadataTranscriptProvider, Transcript, TranscriptUnavailable
from research.core.contracts import EvidenceKind, EvidenceReference, ResearchStatus
from research.core.store import ResearchStore


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
RSS = f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:new-video</id><title>Nuevo análisis macro</title>
    <published>2026-09-04T10:00:00Z</published>
  </entry>
  <entry>
    <id>yt:video:new-video</id><title>Nuevo análisis macro</title>
    <published>2026-09-04T10:00:00Z</published>
  </entry>
  <entry>
    <id>yt:video:old-video</id><title>Video anterior</title>
    <published>2026-09-03T10:00:00Z</published>
  </entry>
</feed>"""


class FixtureTranscriptProvider:
    def __init__(self, transcript: Transcript | None = None, error: str | None = None):
        self.transcript = transcript
        self.error = error
        self.calls: list[str] = []

    def fetch(self, video_id: str) -> Transcript:
        self.calls.append(video_id)
        if self.error:
            raise TranscriptUnavailable(self.error)
        assert self.transcript is not None
        return self.transcript


def test_rss_deduplicates_video_ids_and_sorts_newest_first():
    videos = parse_rss(RSS)
    assert [video.video_id for video in videos] == ["new-video", "old-video"]


def test_transcript_unavailable_does_not_advance_cursor(tmp_path):
    store = ResearchStore(tmp_path)
    provider = FixtureTranscriptProvider(error="quota temporarily unavailable")
    result = CavaWorkflow(store=store).run(rss_xml=RSS, transcript_provider=provider, now=NOW)

    assert result.status is ResearchStatus.INSUFFICIENT_EVIDENCE
    assert result.payload["cursor_advanced"] is False
    assert store.load_context("cava_cursor") is None
    assert provider.calls == ["new-video"]


def test_validated_context_is_persisted_and_cursor_advances(tmp_path):
    store = ResearchStore(tmp_path)
    transcript = Transcript(
        text="La inflación podría mantenerse alta. Esto significa que la liquidez importa.",
        language="es",
        source="supadata",
        available_at=NOW,
    )
    provider = FixtureTranscriptProvider(transcript=transcript)

    def corroborate(video, claims, decision_time):
        return [
            EvidenceReference(
                reference_id="macro-source",
                source="official.example",
                claim="Official macro series is available",
                kind=EvidenceKind.FACT,
                confidence=0.95,
                citation="https://official.example/macro",
            )
        ]

    result = CavaWorkflow(store=store).run(
        rss_xml=RSS,
        transcript_provider=provider,
        corroborate=corroborate,
        now=NOW,
    )
    assert result.status is ResearchStatus.SETUP_FOUND
    assert result.payload["evidence_quality"] == "VALIDATED"
    assert store.load_context("cava")["source_video_id"] == "new-video"
    assert "new-video" in store.load_context("cava_cursor")["processed_video_ids"]


def test_transcript_only_result_is_explicitly_not_validated(tmp_path):
    store = ResearchStore(tmp_path)
    provider = FixtureTranscriptProvider(
        transcript=Transcript("El dólar es relevante.", "es", "supadata", NOW)
    )
    result = CavaWorkflow(store=store).run(rss_xml=RSS, transcript_provider=provider, now=NOW)
    assert result.status is ResearchStatus.INSUFFICIENT_EVIDENCE
    assert result.payload["evidence_quality"] == "TRANSCRIPT_ONLY"
    assert store.load_context("cava") is None
    assert store.load_context("cava_cursor") is None


def test_supadata_uses_runtime_key_and_supports_synchronous_payload():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["x-api-key"] == "test-key"
        assert request.url.params["url"] == "https://www.youtube.com/watch?v=new-video"
        return httpx.Response(200, json={"content": "Transcript text", "lang": "es"})

    provider = SupadataTranscriptProvider(
        api_key="test-key",
        language="es",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    transcript = provider.fetch("new-video")
    assert transcript.text == "Transcript text"
    assert requests[0].url.params["text"] == "true"
    assert requests[0].url.params["mode"] == "auto"


def test_supadata_async_job_is_polled_without_exposing_key():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/transcript"):
            return httpx.Response(202, json={"jobId": "job-1"})
        return httpx.Response(200, json={"content": [{"text": "Ready"}], "lang": "es"})

    provider = SupadataTranscriptProvider(
        api_key="test-key",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_attempts=1,
        poll_wait_seconds=0,
    )
    assert provider.fetch("new-video").text == "Ready"
    assert paths == ["/v1/transcript", "/v1/transcript/job-1"]
