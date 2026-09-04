"""Primary public disclosure providers for the normalized NAVE workflow."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import urljoin

import httpx


HOUSE_SEARCH_URL = "https://disclosures-clerk.house.gov/FinancialDisclosure"
HOUSE_SEARCH_RESULT_URL = "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewMemberSearchResult"
OGE_INDEX_URL = "https://extapps2.oge.gov/201/Presiden.nsf/"


def _links(html: str, *, base_url: str) -> list[str]:
    return [urljoin(base_url, href) for href in re.findall(r'''href=["']([^"']+)["']''', html, re.I)]


class OfficialHouseDisclosureProvider:
    """Return official House filing records for priority representatives.

    The House index is an official public filing source.  These records are
    filing-level evidence, deliberately not inferred trade rows.
    """

    def __init__(
        self,
        *,
        subjects: Sequence[str] = ("Nancy Pelosi",),
        filing_year: int | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.subjects = tuple(subjects)
        self.filing_year = filing_year
        self.http = http or httpx.Client(timeout=20.0, follow_redirects=True)

    def fetch(self) -> list[dict[str, str | None]]:
        index = self.http.get(HOUSE_SEARCH_URL)
        index.raise_for_status()
        token_matches = re.findall(
            r'''name=["'](__RequestVerificationToken|[^"']*token[^"']*)["'][^>]*value=["']([^"']*)["']''',
            index.text,
            re.I,
        )
        hidden = {name: value for name, value in token_matches}
        year = str(self.filing_year or datetime.now().year)
        output: list[dict[str, str | None]] = []
        for subject in self.subjects:
            last_name = subject.split()[-1]
            data = {**hidden, "LastName": last_name, "FilingYear": year, "State": "", "District": ""}
            response = self.http.post(HOUSE_SEARCH_RESULT_URL, data=data)
            response.raise_for_status()
            for link in _links(response.text, base_url=HOUSE_SEARCH_URL):
                if "ptr-pdf" not in link.lower() and not link.lower().endswith(".pdf"):
                    continue
                output.append(
                    {
                        "subject": subject,
                        "owner": "filer/household",
                        "asset": "FINANCIAL_DISCLOSURE_FILING",
                        "transaction_type": "FILING",
                        "transaction_date": None,
                        "disclosure_date": None,
                        "source_url": link,
                        "confidence": 0.95,
                        "provider": "official_house_disclosures",
                    }
                )
        seen: set[str] = set()
        return [row for row in output if not (row["source_url"] in seen or seen.add(str(row["source_url"])))]


class OfficialOGEExecutiveDisclosureProvider:
    """Return public OGE filing records for the current priority executive."""

    def __init__(
        self,
        *,
        subject: str = "Donald Trump",
        index_url: str = OGE_INDEX_URL,
        document_urls: Sequence[str] | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.subject = subject
        self.index_url = index_url
        self.document_urls = tuple(document_urls or ())
        self.http = http or httpx.Client(timeout=20.0, follow_redirects=True)

    def fetch(self) -> list[dict[str, str | None]]:
        urls = list(self.document_urls)
        if not urls:
            response = self.http.get(self.index_url)
            response.raise_for_status()
            needle = self.subject.split()[-1].lower()
            urls = [url for url in _links(response.text, base_url=self.index_url) if needle in url.lower()]
        return [
            {
                "subject": self.subject,
                "owner": "filer/household",
                "asset": "PUBLIC_FINANCIAL_DISCLOSURE",
                "transaction_type": "ANNUAL_REPORT",
                "transaction_date": None,
                "disclosure_date": _date_from_url(url),
                "source_url": url,
                "confidence": 0.95,
                "provider": "official_oge",
            }
            for url in urls
            if url.lower().endswith((".pdf", ".nsf")) or "disclosure" in url.lower()
        ]


def _date_from_url(value: str) -> str | None:
    match = re.search(r"(\d{2})[.-](\d{2})[.-](\d{4})", value)
    return f"{match.group(3)}-{match.group(1)}-{match.group(2)}" if match else None


__all__ = ["OfficialHouseDisclosureProvider", "OfficialOGEExecutiveDisclosureProvider"]
