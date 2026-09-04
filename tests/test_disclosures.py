from datetime import UTC, datetime

from research.core.contracts import ResearchStatus
from research.core.store import ResearchStore
from research.disclosures import (
    DisclosureWorkflow,
    SourceFamily,
    normalize_congress,
    normalize_executive,
)
from research.disclosure_providers import OfficialHouseDisclosureProvider, OfficialOGEExecutiveDisclosureProvider
import httpx


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def congress_record(**overrides):
    record = {
        "firstName": "Nancy",
        "lastName": "Pelosi",
        "owner": "Joint",
        "symbol": "ABC",
        "type": "Purchase",
        "transactionDate": "2026-07-01",
        "disclosureDate": "2026-08-01",
        "amount": "$15,001 - $50,000",
        "link": "https://official.example/congress/1",
    }
    return {**record, **overrides}


def test_congress_normalization_and_timeliness():
    record = normalize_congress(congress_record())
    assert record.source_family is SourceFamily.CONGRESS
    assert record.subject_filer == "Nancy Pelosi"
    assert record.asset == "ABC"
    assert record.transaction_type == "PURCHASE"
    assert record.filing_lag_days == 31
    assert record.timeliness == "TIMELY"


def test_executive_normalization_is_not_mislabeled_congress():
    record = normalize_executive({
        "subject": "Donald Trump",
        "owner": "Household",
        "asset": "XYZ",
        "transaction_type": "SALE",
        "transaction_date": "2026-01-01",
        "disclosure_date": "2026-03-10",
        "source_url": "https://official.example/executive/1",
    })
    assert record.source_family is SourceFamily.EXECUTIVE
    assert record.subject_filer == "Donald Trump"
    assert record.timeliness == "STALE"


def test_sync_deduplicates_and_preserves_source_provenance(tmp_path):
    workflow = DisclosureWorkflow(store=ResearchStore(tmp_path))
    first = workflow.sync_payload(
        congress_records=[congress_record(), congress_record()],
        executive_records=[
            {
                "subject": "Donald Trump",
                "asset": "XYZ",
                "type": "SALE",
                "transaction_date": "2026-08-01",
                "disclosure_date": "2026-08-02",
                "source_url": "https://official.example/executive/1",
            }
        ],
        now=NOW,
    )
    assert first.status is ResearchStatus.SETUP_FOUND
    assert first.payload["fetched_total"] == 2
    assert {row["source_family"] for row in first.payload["records"]} == {"congress", "executive"}
    assert first.payload["portfolio_candidate_consumed"] is False
    second = workflow.sync_payload(congress_records=[congress_record()], now=NOW)
    assert second.status is ResearchStatus.NO_SETUP
    assert second.payload["new_total"] == 0


def test_delayed_disclosure_is_visible_not_a_buy_signal(tmp_path):
    workflow = DisclosureWorkflow(store=ResearchStore(tmp_path))
    result = workflow.sync_payload(
        congress_records=[congress_record(transactionDate="2025-01-01", disclosureDate="2026-08-01")],
        now=NOW,
    )
    row = result.payload["records"][0]
    assert row["timeliness"] == "STALE"
    assert result.payload["disclosure_is_not_a_buy_signal"] is True


def test_fmp_dataclass_field_names_are_normalized():
    record = normalize_congress({
        "firstName": "Nancy",
        "lastName": "Pelosi",
        "owner": "Joint",
        "symbol": "NVDA",
        "transaction_type": "Purchase",
        "asset_description": "NVIDIA Corporation",
        "transaction_date": "2026-08-01",
        "disclosure_date": "2026-08-20",
        "amount_range": "$15,001 - $50,000",
        "link": "https://house.example/filing/1",
    })
    assert record is not None
    assert record.transaction_type == "PURCHASE"
    assert record.asset == "NVDA"
    assert record.amount_range == "$15,001 - $50,000"


def test_congress_and_executive_provider_failures_are_isolated(tmp_path):
    class Failing:
        def fetch(self):
            raise RuntimeError("source offline")

    class Executive:
        def fetch(self):
            return [{
                "subject": "Donald Trump",
                "asset": "PUBLIC_FINANCIAL_DISCLOSURE",
                "transaction_type": "ANNUAL_REPORT",
                "source_url": "https://oge.example/trump.pdf",
            }]

    result = DisclosureWorkflow(store=ResearchStore(tmp_path)).sync_files(
        congress_provider=Failing(), executive_provider=Executive(), now=NOW
    )
    assert result.status is ResearchStatus.SETUP_FOUND
    assert result.payload["records"][0]["source_family"] == "executive"
    assert any("congress provider unavailable" in warning for warning in result.warnings)


def test_official_house_provider_returns_filing_level_evidence():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text='<input type="hidden" name="__RequestVerificationToken" value="token" />')
        return httpx.Response(200, text='<a href="/public_disc/ptr-pdfs/2026/20033725.pdf">filing</a>')

    provider = OfficialHouseDisclosureProvider(
        subjects=("Nancy Pelosi",),
        filing_year=2026,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    rows = provider.fetch()
    assert rows[0]["subject"] == "Nancy Pelosi"
    assert rows[0]["transaction_type"] == "FILING"
    assert rows[0]["source_url"].endswith("20033725.pdf")


def test_official_oge_provider_preserves_trump_source_url():
    provider = OfficialOGEExecutiveDisclosureProvider(
        document_urls=("https://extapps2.oge.gov/201/Trump-05.08.2026.pdf",)
    )
    row = provider.fetch()[0]
    assert row["subject"] == "Donald Trump"
    assert row["asset"] == "PUBLIC_FINANCIAL_DISCLOSURE"
    assert row["disclosure_date"] == "2026-05-08"
