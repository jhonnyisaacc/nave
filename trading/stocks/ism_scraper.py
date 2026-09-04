"""
ISM "Report On Business®" scraper.

Two data paths:

  1. **Headline PMI via OpenBB/FRED** — the FRED series ``NAPM`` (Manufacturing
     composite PMI) and ``NMFBAI`` (Services Business Activity Index) are
     obtained through the repository's OpenBB adapter in the production
     portfolio context path, with the parsed release value as a fallback.

  2. **Industry rankings via the ISM press release** — the monthly
     "Manufacturing ISM® Report On Business®" press releases publish an
     ordered list of industries *reporting growth* and *reporting
     contraction*. These lists are rendered as plain static HTML on
     ismworld.org, so ``httpx`` + ``BeautifulSoup`` is enough. A
     ``Playwright``-backed fetcher is reserved as a fallback for sources
     that turn out to be JS-rendered (e.g. Investing.com).

Only the industry ordering matters for the screener — the raw PMI value
is a sanity check.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from html import unescape
from typing import Literal

import httpx

from trading.stocks.mapping import GICS_MAPPING, sector_for_ism_industry  # noqa: F401

logger = logging.getLogger(__name__)


# Known press-release URLs. These change monthly; the landing pages we hit
# contain links to the latest report, so we also support passing a URL
# directly to :meth:`ISMReportFetcher.fetch_report`.
ISM_MANUFACTURING_LANDING = (
    "https://www.ismworld.org/supply-management-news-and-reports/reports/"
    "ism-report-on-business/pmi/"
)
ISM_SERVICES_LANDING = (
    "https://www.ismworld.org/supply-management-news-and-reports/reports/"
    "ism-report-on-business/services/"
)
ISM_SITEMAP_URL = "https://www.ismworld.org/sitemap.xml"


ReportKind = Literal["manufacturing", "services"]


@dataclass
class ISMIndustryRanking:
    """An ISM industry labelled as expanding, contracting, or unchanged."""

    industry: str
    trend: Literal["expanding", "contracting", "unchanged"]
    rank: int
    gics_sector: str | None = None


@dataclass
class ISMReport:
    """Parsed ISM report: headline PMI + ordered industry rankings."""

    kind: ReportKind
    report_month: str  # e.g. "March 2026"
    pmi: float | None
    expanding: list[ISMIndustryRanking] = field(default_factory=list)
    contracting: list[ISMIndustryRanking] = field(default_factory=list)
    source_url: str | None = None

    def by_sector(self, trend: str = "expanding") -> list[str]:
        """Unique GICS sectors in the requested trend bucket, ranked."""
        bucket = self.expanding if trend == "expanding" else self.contracting
        seen: dict[str, None] = {}
        for item in bucket:
            if item.gics_sector and item.gics_sector not in seen:
                seen[item.gics_sector] = None
        return list(seen.keys())


class ISMReportFetcher:
    """Fetch the official ISM release for its prose industry rankings.

    Headline index values belong to OpenBB/FRED. The release parser exists
    because the current OpenBB adapter does not expose the ranked industry
    sentences; it does not scrape FRED, CFTC, or market-history data.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        use_playwright: bool = False,
        user_agent: str = (
            "Mozilla/5.0 (compatible; nave-research/0.1; +https://github.com/jhonnyisaacc/nave)"
        ),
    ):
        self.timeout_seconds = timeout_seconds
        self.use_playwright = use_playwright
        self.user_agent = user_agent

    # ── Public API ---------------------------------------------------
    def fetch_report(
        self,
        kind: ReportKind = "manufacturing",
        *,
        url: str | None = None,
    ) -> ISMReport:
        """Fetch and parse the latest ISM report for ``kind``."""
        if url:
            html = self._fetch_html(url)
            return self._parse(html, kind=kind, source_url=url)
        try:
            target = self._resolve_latest_release(kind)
            html = self._fetch_html(target)
        except Exception as exc:  # noqa: BLE001 - provider fallback
            logger.debug(
                "ISM live fetch failed (%s); falling back to fixture landing URL", exc
            )
            target = ISM_MANUFACTURING_LANDING if kind == "manufacturing" else ISM_SERVICES_LANDING
            html = self._fetch_html(target)
        return self._parse(html, kind=kind, source_url=target)

    # ── Fetch layer --------------------------------------------------
    def _fetch_html(self, url: str) -> str:
        if self.use_playwright:
            return self._fetch_with_playwright(url)
        headers = {"User-Agent": self.user_agent}
        with httpx.Client(timeout=self.timeout_seconds, headers=headers) as c:
            resp = c.get(url, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
        if _looks_like_ism_captcha(html, url=url):
            logger.info(
                "ISM anti-bot page detected for %s — retrying with curl fallback", url)
            curl_html = self._fetch_with_curl(url)
            if curl_html:
                return curl_html
        return html

    def _fetch_with_playwright(self, url: str) -> str:
        """Async Playwright path; compiled on demand so the import stays optional."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional path
            raise RuntimeError(
                "Playwright is not installed. Run `pip install playwright` and "
                "`python -m playwright install chromium`, or set use_playwright=False."
            ) from exc

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=self.user_agent)
            page.goto(url, wait_until="networkidle",
                      timeout=int(self.timeout_seconds * 1000))
            html = page.content()
            browser.close()
            return html

    def _fetch_with_curl(self, url: str) -> str:
        proc = subprocess.run(
            ["curl", "-sL", url],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"curl fetch failed for {url!r}: exit={proc.returncode}")
        # Treat a suspiciously small response as a failure so the caller can
        # fall back to the landing URL rather than silently parsing an empty body.
        if len(proc.stdout) < 200:
            raise RuntimeError(
                f"curl returned an unexpectedly small response ({len(proc.stdout)} bytes) "
                f"for {url!r}; treating as failure."
            )
        return proc.stdout

    def _resolve_latest_release(self, kind: ReportKind) -> str:
        """Resolve the latest report URL.

        ISM's old landing URLs are now unstable and often protected by
        anti-bot pages. We resolve the latest "ISM PMI Reports Roundup"
        article from the public sitemap and follow its PRNewswire source
        link, where the full industry expansion/contraction lists are
        consistently published.
        """
        roundup_url = self._resolve_latest_roundup_url(kind)
        if roundup_url:
            prnewswire_url = self._extract_prnewswire_url(
                roundup_url, kind=kind)
            if prnewswire_url:
                return prnewswire_url
            return roundup_url
        return ISM_MANUFACTURING_LANDING if kind == "manufacturing" else ISM_SERVICES_LANDING

    def _resolve_latest_roundup_url(self, kind: ReportKind) -> str | None:
        try:
            headers = {"User-Agent": self.user_agent}
            with httpx.Client(timeout=self.timeout_seconds, headers=headers) as c:
                resp = c.get(ISM_SITEMAP_URL, follow_redirects=True)
                resp.raise_for_status()
                sitemap = resp.text
        except Exception as exc:  # noqa: BLE001 - optional sitemap fallback
            logger.debug("ISM sitemap fetch failed: %s", exc)
            return None

        all_urls = re.findall(
            r"<loc>(https://www\.ismworld\.org[^<]+)</loc>", sitemap)
        roundup_urls = [
            u
            for u in all_urls
            if "ism-pmi-reports-roundup" in u and f"-{kind}/" in u
        ]
        if not roundup_urls:
            return None
        return max(roundup_urls)

    def _extract_prnewswire_url(self, roundup_url: str, *, kind: ReportKind) -> str | None:
        try:
            html = self._fetch_html(roundup_url)
        except Exception as exc:  # noqa: BLE001 - optional release fallback
            logger.debug("Failed to fetch ISM roundup page %s: %s",
                         roundup_url, exc)
            return None
        links = re.findall(
            r'https://www\.prnewswire\.com/news-releases/[^"\'\s<]+', html)
        if not links:
            return None
        preferred = [u for u in links if f"{kind}-pmi" in u]
        return unescape(preferred[0] if preferred else links[0])

    # ── Parse layer --------------------------------------------------
    def _parse(self, html: str, *, kind: ReportKind, source_url: str) -> ISMReport:
        """Extract PMI + expanding/contracting industry ordering from the HTML."""
        text = _strip_html(html)

        month = _extract_report_month(
            html,
            text=text,
            kind=kind,
            source_url=source_url,
        )
        pmi = _extract_pmi(text, kind, source_url=source_url)
        expanding = _parse_industry_list(text, trend="expanding")
        contracting = _parse_industry_list(text, trend="contracting")

        _attach_sectors(expanding)
        _attach_sectors(contracting)

        return ISMReport(
            kind=kind,
            report_month=month,
            pmi=pmi,
            expanding=expanding,
            contracting=contracting,
            source_url=source_url,
        )


# ── Parsing helpers ---------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_MONTH_PATTERN = (
    r"(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)"
)

_MONTH_RE = re.compile(
    rf"(?P<month>{_MONTH_PATTERN}\s+\d{{4}})"
)

_PMI_RE_MANUF = re.compile(
    r"(?:Manufacturing\s+PMI|PMI®)[^\d]{0,40}(\d{2}\.\d)", re.IGNORECASE
)
_PMI_RE_SERVICES = re.compile(
    r"Services\s+(?:PMI|Index)[^\d]{0,40}(\d{2}\.\d)", re.IGNORECASE
)


def _strip_html(html: str) -> str:
    """Return plain text with normalized whitespace."""
    try:
        from bs4 import BeautifulSoup  # lazy import so the rest stays testable

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ")
    except ImportError:
        # Keep the no-BeautifulSoup path semantically equivalent for tests and
        # minimal deployments: script/style contents are not report text.
        without_code = re.sub(
            r"<\s*(script|style|noscript)\b[^>]*>.*?<\s*/\s*\1\s*>",
            " ",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = _TAG_RE.sub(" ", without_code)
    return _WHITESPACE_RE.sub(" ", unescape(text)).strip()


def _looks_like_ism_captcha(html: str, *, url: str) -> bool:
    if "ismworld.org" not in url:
        return False
    lowered = html.lower()
    return "captcha_form" in lowered and "google.com/recaptcha/api.js" in lowered


def _extract_report_month(
    html: str,
    *,
    text: str,
    kind: ReportKind,
    source_url: str = "",
) -> str:
    kind_label = "Manufacturing" if kind == "manufacturing" else "Services"

    # PR Newswire release slugs contain the report month and year.  Prefer this
    # over generic body text: roundup pages often include historical references
    # (for example, "February 2022") before the current report content.
    if "prnewswire.com" in source_url.lower():
        slug_match = re.search(
            rf"(?P<month>{_MONTH_PATTERN})[-_](?P<year>\d{{4}})",
            source_url,
            re.IGNORECASE,
        )
        if slug_match:
            return f"{slug_match.group('month').capitalize()} {slug_match.group('year')}"

    for heading in _extract_heading_texts(html):
        month = _extract_kind_aligned_month(heading, kind_label)
        if month:
            return month

    month = _extract_kind_aligned_month(text, kind_label)
    if month:
        return month

    match = _MONTH_RE.search(text)
    return match.group("month") if match else "Unknown"


def _extract_kind_aligned_month(text: str, kind_label: str) -> str | None:
    if not text:
        return None
    patterns = (
        rf"(?P<month>{_MONTH_PATTERN}\s+\d{{4}})\s+{kind_label}\b",
        rf"{kind_label}\s+(?:ISM(?:®)?\s+)?(?:Report\s+On\s+Business(?:®)?\s+)?(?:for\s+)?(?P<month>{_MONTH_PATTERN}\s+\d{{4}})",
        rf"{kind_label}[^\n\r]{{0,120}}?(?P<month>{_MONTH_PATTERN}\s+\d{{4}})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group("month")
    return None


def _extract_heading_texts(html: str) -> list[str]:
    values: list[str] = []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ") if soup.title else ""
        if title:
            values.append(title)
        for tag in soup.find_all(["h1", "h2"]):
            heading = tag.get_text(" ")
            if heading:
                values.append(heading)
    except ImportError:
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if title_match:
            values.append(_strip_html(title_match.group(1)))
        for heading_match in re.finditer(r"<h[12][^>]*>(.*?)</h[12]>", html, re.IGNORECASE | re.DOTALL):
            values.append(_strip_html(heading_match.group(1)))

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _WHITESPACE_RE.sub(" ", value).strip()
        if clean and clean not in seen:
            normalized.append(clean)
            seen.add(clean)
    return normalized


def _extract_pmi(
    text: str,
    kind: ReportKind,
    *,
    source_url: str | None = None,
) -> float | None:
    """Return headline composite PMI, not threshold references like 'above 47.5'."""
    kind_label = "Manufacturing" if kind == "manufacturing" else "Services"
    if source_url:
        slug_match = re.search(r"pmi-at-(\d{2}(?:-\d)?)-", source_url, re.IGNORECASE)
        if slug_match is not None:
            return float(slug_match.group(1).replace("-", "."))
        slug_match = re.search(r"pmi-at-(\d{2}(?:\.\d)?)-", source_url, re.IGNORECASE)
        if slug_match is not None:
            return float(slug_match.group(1))

    registered = re.search(
        rf"{kind_label}\s+PMI(?:®)?\s+registered\s+(\d{{2}}\.\d)\s*percent",
        text,
        re.IGNORECASE,
    )
    if registered is not None:
        return float(registered.group(1))

    at_headline = re.search(
        rf"{kind_label}\s+PMI(?:®)?\s+at\s+(\d{{2}}\.\d)\b",
        text,
        re.IGNORECASE,
    )
    if at_headline is not None:
        return float(at_headline.group(1))

    pattern = _PMI_RE_MANUF if kind == "manufacturing" else _PMI_RE_SERVICES
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - 24): match.start()].lower()
        if re.search(r"\b(above|below)\s*$", prefix):
            continue
        try:
            return float(match.group(1))
        except ValueError:
            continue
    return None


# Phrasing used in both Manufacturing and Services reports. ISM lists
# industries in order: "The 10 industries reporting growth ... in order are:
# X; Y; Z. The four industries reporting contraction ... are: A; B;"
_EXPANDING_RE = re.compile(
    r"industries\s+reporting\s+(?:growth|an increase in new orders)[^:]*:\s*(?P<body>[^.]+)\.",
    re.IGNORECASE,
)
_CONTRACTING_RE = re.compile(
    r"industries\s+reporting\s+(?:a\s+decrease\s+in|contraction)[^:]*:\s*(?P<body>[^.]+)\.",
    re.IGNORECASE,
)


def _parse_industry_list(
    text: str, *, trend: Literal["expanding", "contracting"]
) -> list[ISMIndustryRanking]:
    pattern = _EXPANDING_RE if trend == "expanding" else _CONTRACTING_RE
    match = pattern.search(text)
    if match is None:
        return []
    body = match.group("body")
    items = _split_industries(body)
    return [
        ISMIndustryRanking(industry=name, trend=trend, rank=i)
        for i, name in enumerate(items, start=1)
    ]


def _split_industries(body: str) -> list[str]:
    """Split a ``"; "``-delimited industry list into clean names.

    ISM uses ``;`` as the item separator, and the Oxford comma style
    ``"...; and <last>"`` to introduce the tail. Normalize that tail to
    a plain ``";"`` before splitting so ``"and "`` doesn't survive as a
    prefix on the final item (e.g. ``"and Computer Products"``).
    """
    normalized = re.sub(r"[;,]\s*and\s+", "; ", body, flags=re.IGNORECASE)
    # Fallback: catch a bare " and " between the last two items.
    normalized = re.sub(r"\s+and\s+", "; ", normalized, flags=re.IGNORECASE)
    raw = re.split(r"\s*;\s*", normalized)
    cleaned: list[str] = []
    for chunk in raw:
        name = chunk.strip().rstrip(".").strip('"')
        if not name:
            continue
        cleaned.append(name.lower())
    return cleaned


def _attach_sectors(rankings: Iterable[ISMIndustryRanking]) -> None:
    """Resolve the GICS sector for each ranking, in place."""
    for r in rankings:
        r.gics_sector = sector_for_ism_industry(r.industry)
