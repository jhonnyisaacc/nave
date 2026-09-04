"""Canonical Cava RSS → transcript → evidence → context pipeline."""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from research.cava.transcript import Transcript, TranscriptProvider, TranscriptUnavailable
from research.cava.corroboration import CavaCorroboration, CavaCorroborator
from research.core.contracts import (
    EvidenceKind,
    EvidenceReference,
    PointInTime,
    ResearchResult,
    ResearchStatus,
    RunMetadata,
)
from research.core.store import ResearchStore


CAVA_CHANNEL_ID = "UCvCCLJkQpRg0NdT3zNcI08A"
CAVA_RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CAVA_CHANNEL_ID}"
_ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class CavaVideo:
    video_id: str
    title: str
    published_at: datetime
    url: str


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("RSS timestamps must include a timezone")
    return parsed.astimezone(UTC)


def parse_rss(xml_text: str) -> list[CavaVideo]:
    """Parse and deduplicate Atom entries, newest first."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid YouTube RSS XML: {exc}") from exc
    videos: dict[str, CavaVideo] = {}
    for entry in root.findall(f"{_ATOM}entry"):
        video_id = (entry.findtext(f"{_ATOM}id") or "").strip()
        if video_id.startswith("yt:video:"):
            video_id = video_id.removeprefix("yt:video:")
        title = (entry.findtext(f"{_ATOM}title") or "").strip()
        published = (entry.findtext(f"{_ATOM}published") or "").strip()
        if not video_id or not title or not published:
            continue
        try:
            published_at = _parse_timestamp(published)
        except ValueError:
            continue
        videos[video_id] = CavaVideo(
            video_id=video_id,
            title=title,
            published_at=published_at,
            url=f"https://www.youtube.com/watch?v={video_id}",
        )
    return sorted(videos.values(), key=lambda item: item.published_at, reverse=True)


def _classify_claim(text: str) -> EvidenceKind:
    lowered = text.lower()
    if any(marker in lowered for marker in ("podría", "podria", "quizá", "quizas", "probablemente")):
        return EvidenceKind.HYPOTHESIS
    if any(marker in lowered for marker in ("porque", "por tanto", "esto significa", "implica")):
        return EvidenceKind.INFERENCE
    if any(marker in lowered for marker in ("desconozco", "no sé", "no se", "sin confirmar")):
        return EvidenceKind.UNKNOWN
    return EvidenceKind.FACT


def _transcript_claims(video: CavaVideo, transcript: Transcript, decision_time: datetime) -> list[EvidenceReference]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", transcript.text) if part.strip()]
    claims: list[EvidenceReference] = []
    for index, sentence in enumerate(sentences[:80], start=1):
        claims.append(
            EvidenceReference(
                reference_id=f"cava-{video.video_id}-claim-{index}",
                source="supadata.transcript",
                claim=sentence,
                kind=_classify_claim(sentence),
                confidence=None,
                point_in_time=PointInTime(
                    event_time=video.published_at,
                    available_at=transcript.available_at,
                    decision_time=decision_time,
                ),
                citation=video.url,
                metadata={"speaker_attributed": True, "video_id": video.video_id},
            )
        )
    return claims


def _indicators_and_implications(claims: Iterable[EvidenceReference]) -> tuple[list[str], list[str]]:
    text = " ".join(claim.claim.lower() for claim in claims)
    indicator_map = {
        "inflation": ("inflación", "inflacion", "cpi", "precios"),
        "rates": ("tipo", "tasas", "fed", "bono", "interés", "interes"),
        "dollar": ("dólar", "dolar", "dxy"),
        "copper": ("cobre", "copper"),
        "gold": ("oro", "gold"),
        "liquidity": ("liquidez", "liquidity", "tga", "rrp"),
    }
    indicators = [name for name, terms in indicator_map.items() if any(term in text for term in terms)]
    implications = []
    if "inflation" in indicators or "rates" in indicators:
        implications.append("risk assets require macro confirmation; no standalone trade signal")
    if "liquidity" in indicators:
        implications.append("liquidity context should be consumed by downstream stock and crypto research")
    if not implications:
        implications.append("no deterministic macro implication established from transcript alone")
    return indicators, implications


def _normalize_corroboration(value: Any) -> CavaCorroboration:
    """Keep the old callback contract while exposing richer production output."""
    if isinstance(value, CavaCorroboration):
        return value
    # Compatibility for the original callback contract. Production returns a
    # CavaCorroboration with explicit point-in-time metadata.
    return CavaCorroboration(evidence=tuple(value or ()), legacy_callback=True)


class CavaWorkflow:
    """Provider-injected Cava workflow with fail-closed cursor semantics."""

    workflow_name = "intel.cava.daily"
    strategy_name = "cava-macro-intelligence"
    strategy_version = "1.0.0"

    def __init__(self, *, store: ResearchStore | None = None):
        self.store = store or ResearchStore()

    def unavailable(self, message: str, *, now: datetime | None = None) -> ResearchResult:
        """Record an RSS/provider outage without turning a scheduled run into a crash."""
        started_at = now or datetime.now(UTC)
        result = self._result(
            status=ResearchStatus.DATA_UNAVAILABLE,
            started_at=started_at,
            decision_time=started_at,
            payload={"source": CAVA_RSS_URL, "videos_seen": 0, "cursor_advanced": False},
            warnings=(message,),
        )
        self.store.save_result(result)
        return result

    def _cursor(self) -> set[str]:
        payload = self.store.load_context("cava_cursor") or {}
        processed = payload.get("processed_video_ids") if isinstance(payload, Mapping) else []
        return {str(item) for item in processed or [] if str(item).strip()}

    def _save_cursor(self, processed: set[str], *, last_processed: CavaVideo) -> None:
        self.store.save_context(
            "cava_cursor",
            {
                "processed_video_ids": sorted(processed),
                "last_processed_video_id": last_processed.video_id,
                "last_processed_at": datetime.now(UTC).isoformat(),
            },
        )

    def _result(
        self,
        *,
        status: ResearchStatus,
        started_at: datetime,
        decision_time: datetime,
        payload: Mapping[str, Any],
        evidence: Iterable[EvidenceReference] = (),
        warnings: Iterable[str] = (),
        input_available_at: datetime | None = None,
    ) -> ResearchResult:
        return ResearchResult(
            workflow=self.workflow_name,
            status=status,
            metadata=RunMetadata(
                strategy_name=self.strategy_name,
                strategy_version=self.strategy_version,
                run_id=str(uuid.uuid4()),
                decision_time=decision_time,
                started_at=started_at,
                completed_at=decision_time,
                input_available_at=input_available_at,
            ),
            payload=payload,
            evidence=tuple(evidence),
            warnings=tuple(warnings),
        )

    def run(
        self,
        *,
        rss_xml: str,
        transcript_provider: TranscriptProvider,
        corroborate: Callable[[CavaVideo, list[EvidenceReference], datetime], Any]
        | None = None,
        now: datetime | None = None,
    ) -> ResearchResult:
        started_at = now or datetime.now(UTC)
        decision_time = started_at
        try:
            videos = parse_rss(rss_xml)
        except ValueError as exc:
            result = self._result(
                status=ResearchStatus.DATA_UNAVAILABLE,
                started_at=started_at,
                decision_time=decision_time,
                payload={"source": CAVA_RSS_URL, "videos_seen": 0, "cursor_advanced": False},
                warnings=(str(exc),),
            )
            self.store.save_result(result)
            return result

        seen = self._cursor()
        new_videos = [video for video in videos if video.video_id not in seen]
        rss_evidence = [
            EvidenceReference(
                reference_id="cava-rss",
                source="youtube.rss",
                claim=f"RSS exposed {len(videos)} valid video entries",
                kind=EvidenceKind.FACT,
                confidence=1.0,
                point_in_time=PointInTime(available_at=started_at, decision_time=decision_time),
                citation=CAVA_RSS_URL,
            )
        ]
        if not new_videos:
            result = self._result(
                status=ResearchStatus.NO_SETUP,
                started_at=started_at,
                decision_time=decision_time,
                payload={
                    "source": CAVA_RSS_URL,
                    "videos_seen": len(videos),
                    "new_videos": 0,
                    "cursor_advanced": False,
                    "message": "No new Cava video requires processing",
                },
                evidence=rss_evidence,
                input_available_at=started_at,
            )
            self.store.save_result(result)
            return result

        video = new_videos[0]
        try:
            transcript = transcript_provider.fetch(video.video_id)
        except TranscriptUnavailable as exc:
            result = self._result(
                status=ResearchStatus.INSUFFICIENT_EVIDENCE,
                started_at=started_at,
                decision_time=decision_time,
                payload={
                    "video": {"id": video.video_id, "title": video.title, "url": video.url},
                    "published_at": video.published_at.isoformat(),
                    "cursor_advanced": False,
                    "evidence_quality": "RSS_ONLY",
                    "downstream_implications": {
                        "stocks": "UNKNOWN",
                        "crypto": "UNKNOWN",
                        "options": "UNKNOWN",
                        "shorts": "UNKNOWN",
                    },
                },
                evidence=rss_evidence,
                warnings=(f"transcript unavailable: {exc}",),
                input_available_at=started_at,
            )
            self.store.save_result(result)
            return result

        claims = _transcript_claims(video, transcript, decision_time)
        corroboration = _normalize_corroboration(
            (corroborate or CavaCorroborator())(video, claims, decision_time)
        )
        indicators, implications = _indicators_and_implications(claims)
        evidence = [*rss_evidence, *claims, *corroboration.evidence]
        warnings: list[str] = list(corroboration.warnings)
        if not corroboration.evidence:
            warnings.append("no eligible authoritative corroboration was found; transcript claims remain speaker-attributed")
        context_validated = bool(
            claims
            and corroboration.evidence
            and (
                corroboration.legacy_callback
                or any(
                    item.point_in_time.availability == "ELIGIBLE"
                    and item.kind is EvidenceKind.FACT
                    for item in corroboration.evidence
                )
            )
        )
        corroboration_status = (
            "VALIDATED"
            if context_validated and not corroboration.warnings and not corroboration.contradictions
            else "PARTIAL"
            if context_validated
            else "UNAVAILABLE"
        )
        all_indicators = [*({"topic": item} for item in indicators), *corroboration.indicators]
        payload = {
            "video": {"id": video.video_id, "title": video.title, "url": video.url},
            "published_at": video.published_at.isoformat(),
            "transcript": {"source": transcript.source, "language": transcript.language, "characters": len(transcript.text)},
            "claims": [claim.to_dict() for claim in claims],
            "corroboration": [item.to_dict() for item in corroboration.evidence],
            "relevant_indicators": indicators,
            "corroboration_indicators": all_indicators,
            "contradictions": list(corroboration.contradictions),
            "contradictions_uncertainty": warnings,
            "corroboration_sources": list(corroboration.sources),
            "corroboration_status": corroboration_status,
            "macro_implications": implications,
            "downstream_implications": {
                "stocks": "REVIEW macro context with company evidence",
                "crypto": "REVIEW regime and liquidity confirmation",
                "options": "REVIEW volatility and catalyst evidence",
                "shorts": "REVIEW sector/technical confirmation before any candidate",
            },
            "evidence_quality": "VALIDATED" if context_validated else "TRANSCRIPT_ONLY",
            "confidence": 0.7 if context_validated else 0.35,
            "cursor_advanced": context_validated,
        }
        result = self._result(
            status=ResearchStatus.SETUP_FOUND if context_validated else ResearchStatus.INSUFFICIENT_EVIDENCE,
            started_at=started_at,
            decision_time=decision_time,
            payload=payload,
            evidence=evidence,
            warnings=warnings,
            input_available_at=min(started_at, transcript.available_at),
        )
        self.store.save_result(result)
        if context_validated:
            processed = seen | {video.video_id}
            self._save_cursor(processed, last_processed=video)
            self.store.save_context(
                "cava",
                {
                    "validated": True,
                    "source_video_id": video.video_id,
                    "title": video.title,
                    "published_at": video.published_at.isoformat(),
                    "validated_at": decision_time.isoformat(),
                    "evidence_quality": payload["evidence_quality"],
                    "claims": payload["claims"],
                    "corroboration": payload["corroboration"],
                    "indicators": payload["corroboration_indicators"],
                    "contradictions": payload["contradictions"],
                    "sources": payload["corroboration_sources"],
                    "macro_regime_implications": implications,
                    "confidence": payload["confidence"],
                },
            )
        return result
