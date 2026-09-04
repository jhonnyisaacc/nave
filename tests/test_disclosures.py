from datetime import UTC, datetime

from research.core.contracts import ResearchStatus
from research.core.store import ResearchStore
from research.disclosures import (
    DisclosureWorkflow,
    SourceFamily,
    normalize_congress,
    normalize_executive,
)


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
