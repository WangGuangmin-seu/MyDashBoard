"""U.S. Treasury Fiscal Data — TGA cash balance (daily) and MTS receipts/outlays
(monthly). No API key required (spec §11).

Live-verified (spec §4.2):

* Daily Treasury Statement, operating cash balance:
    v1/accounting/dts/operating_cash_balance
  The TGA level is the row with account_type ==
  "Treasury General Account (TGA) Closing Balance"; its value lives in
  `open_today_bal` (millions USD; `close_today_bal` is now always "null").

* Monthly Treasury Statement, table 1 (receipts/outlays/deficit):
    v1/accounting/mts/mts_table_1
  This table is laid out by fiscal year: every monthly report repeats a row per
  calendar month plus YTD / FY subtotals. The authoritative figure for a given
  report month is the row whose `classification_desc` equals that report's own
  calendar-month name. Values are in whole USD; `current_month_dfct_sur_amt` is
  positive for a deficit, negative for a surplus.

All monetary series are normalised to USD billions for a consistent unit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..contract import CollectorContext, Collector, DataStatus, Observation, SeriesMeta
from .base import get_json, make_client

ROOT = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
DTS_CASH = ROOT + "v1/accounting/dts/operating_cash_balance"
MTS_TABLE_1 = ROOT + "v1/accounting/mts/mts_table_1"

TGA_CLOSING = "Treasury General Account (TGA) Closing Balance"

_MONTHS = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December",
}
_MILLIONS_TO_B = 1e-3   # DTS open_today_bal is in millions
_DOLLARS_TO_B = 1e-9    # MTS amounts are in whole dollars


class TreasuryCollector:
    id = "treasury"
    series = [
        SeriesMeta(
            series_id="treasury.tga.closing_balance",
            display_name="Treasury General Account — closing balance",
            unit="USD billions",
            source="treasury",
            # DTS is business-daily with a ~1 day publish lag; 3d SLA (6d
            # tolerance) absorbs weekends and holidays without false alarms.
            expected_interval=timedelta(days=3),
            precision=1,
            direction_good="neutral",
            description="Daily TGA closing cash balance (Daily Treasury Statement).",
        ),
        SeriesMeta(
            series_id="treasury.mts.receipts",
            display_name="Federal receipts (monthly)",
            unit="USD billions",
            source="treasury",
            expected_interval=timedelta(days=31),
            precision=1,
            direction_good="up",
            description="Gross federal receipts for the month (Monthly Treasury Statement, table 1).",
        ),
        SeriesMeta(
            series_id="treasury.mts.outlays",
            display_name="Federal outlays (monthly)",
            unit="USD billions",
            source="treasury",
            expected_interval=timedelta(days=31),
            precision=1,
            direction_good="neutral",
            description="Gross federal outlays for the month (Monthly Treasury Statement, table 1).",
        ),
        SeriesMeta(
            series_id="treasury.mts.deficit",
            display_name="Federal deficit (monthly)",
            unit="USD billions",
            source="treasury",
            expected_interval=timedelta(days=31),
            precision=1,
            direction_good="down",
            description="Monthly budget deficit (positive) or surplus (negative), MTS table 1.",
        ),
    ]

    async def fetch(self, ctx: CollectorContext) -> list[Observation]:
        async with make_client() as client:
            obs: list[Observation] = []
            obs += await self._fetch_tga(client, ctx.now)
            obs += await self._fetch_mts(client, ctx.now)
        if not obs:
            raise RuntimeError("Treasury returned zero observations — refusing to report success")
        return obs

    async def _fetch_tga(self, client, now: datetime) -> list[Observation]:
        out: list[Observation] = []
        for row in await self._paginate(
            client,
            DTS_CASH,
            fields="record_date,account_type,open_today_bal",
            extra={"filter": f"account_type:eq:{TGA_CLOSING}"},
        ):
            out.append(
                Observation(
                    series_id="treasury.tga.closing_balance",
                    observed_at=_date(row["record_date"]),
                    as_of=now,
                    value=_num(row.get("open_today_bal"), _MILLIONS_TO_B),
                    status=DataStatus.CONFIRMED,
                    source=self.id,
                )
            )
        return out

    async def _fetch_mts(self, client, now: datetime) -> list[Observation]:
        rows = await self._paginate(
            client,
            MTS_TABLE_1,
            fields=(
                "record_date,classification_desc,current_month_gross_rcpt_amt,"
                "current_month_gross_outly_amt,current_month_dfct_sur_amt"
            ),
        )
        out: list[Observation] = []
        for row in rows:
            observed = _date(row["record_date"])
            # Keep only the row describing the report's own calendar month.
            if row.get("classification_desc") != _MONTHS[observed.month]:
                continue
            for series_id, field, factor in (
                ("treasury.mts.receipts", "current_month_gross_rcpt_amt", _DOLLARS_TO_B),
                ("treasury.mts.outlays", "current_month_gross_outly_amt", _DOLLARS_TO_B),
                ("treasury.mts.deficit", "current_month_dfct_sur_amt", _DOLLARS_TO_B),
            ):
                out.append(
                    Observation(
                        series_id=series_id,
                        observed_at=observed,
                        as_of=now,
                        value=_num(row.get(field), factor),
                        status=DataStatus.CONFIRMED,
                        source=self.id,
                    )
                )
        return out

    async def _paginate(
        self, client, url: str, *, fields: str, extra: dict | None = None
    ) -> list[dict]:
        """Walk fiscaldata pagination (page[number]/page[size]) to completion."""
        rows: list[dict] = []
        page = 1
        while True:
            params = {
                "fields": fields,
                "sort": "record_date",
                "page[size]": 10000,
                "page[number]": page,
            }
            if extra:
                params.update(extra)
            data = await get_json(client, url, params=params)
            batch = data.get("data", [])
            rows.extend(batch)
            meta = data.get("meta", {})
            total_pages = meta.get("total-pages", 1)
            if page >= total_pages or not batch:
                break
            page += 1
        return rows


def _date(raw: str) -> datetime:
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


def _num(raw: object, factor: float) -> float | None:
    """fiscaldata sends numbers as strings, and 'null' as a literal string."""
    if raw is None or raw == "" or raw == "null":
        return None
    return float(raw) * factor


_: Collector = TreasuryCollector()
