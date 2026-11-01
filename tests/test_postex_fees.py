"""Real PostEx courier fees: each parcel logged once, only when its fee is
known, grouped by booking date, nothing before the old estimate's cut-off."""
import asyncio
from datetime import date

from core import postex as px


def parcel(tn, booked, bucket, invoice=0, fee=0, tax=0, rfee=0, rtax=0):
    return {"trackingNumber": tn, "transactionDate": f"{booked}T15:00:00.000+0500", "_bucket": bucket,
            "invoicePayment": invoice, "transactionFee": fee, "transactionTax": tax,
            "reversalFee": rfee, "reversalTax": rtax}


class DB:
    def __init__(self): self.slots, self.payouts = set(), []
    async def claim_slot(self, slot):
        if slot in self.slots:
            return False
        self.slots.add(slot)
        return True
    async def create_postex_payout(self, row): self.payouts.append(row)


class Postex:
    def __init__(self, orders): self.orders, self.calls = orders, []
    async def get_orders_classified(self, status_id=0, from_date=None, to_date=None):
        self.calls.append((from_date, to_date))
        return self.orders, {}


def test_parcel_fee():
    assert px.parcel_fee(parcel("a", "2026-10-20", "delivered", fee=210, tax=33.6)) == (243.6, True)
    assert px.parcel_fee(parcel("b", "2026-10-20", "returned", rfee=210, rtax=33.6)) == (243.6, True)
    assert px.parcel_fee(parcel("c", "2026-10-20", "returned"))[1] is False      # return fee not charged yet
    assert px.parcel_fee(parcel("d", "2026-10-20", "in_transit", fee=210))[1] is False


def test_sync_groups_by_booking_day_and_never_double_counts():
    orders = [parcel("1", "2026-10-20", "delivered", invoice=2199, fee=210, tax=33.6),
              parcel("2", "2026-10-20", "returned", invoice=699, rfee=210, rtax=33.6),
              parcel("3", "2026-10-21", "delivered", invoice=1500, fee=364, tax=58.24),
              parcel("4", "2026-10-21", "returned", invoice=900),                 # return not charged yet
              parcel("5", "2026-10-21", "in_transit", invoice=800, fee=210),
              parcel("6", "2026-10-12", "delivered", invoice=999, fee=210, tax=33.6)]  # covered by the old estimate
    db, api = DB(), Postex(orders)
    r = asyncio.run(px.sync_real_fees(db, api, date(2026, 11, 1)))
    assert api.calls == [("2026-10-17", "2026-11-01")]
    assert r == {"days": 2, "parcels": 3, "fees": 909.44}
    by_day = {p["payout_date"]: p for p in db.payouts}
    assert by_day["2026-10-20"]["gross_amount"] == 2199 and by_day["2026-10-20"]["fee_deducted"] == 487.2
    assert by_day["2026-10-20"]["net_amount"] == 1711.8 and by_day["2026-10-20"]["order_count"] == 2
    assert by_day["2026-10-21"]["fee_deducted"] == 422.24 and "1 delivered, 0 returned" in by_day["2026-10-21"]["notes"]

    # Next tick: parcel 4's return fee has now been charged -> only it is added.
    orders[3]["reversalFee"], orders[3]["reversalTax"] = 154, 24.64
    r2 = asyncio.run(px.sync_real_fees(db, api, date(2026, 11, 1)))
    assert r2 == {"days": 1, "parcels": 1, "fees": 178.64}
    assert db.payouts[-1]["payout_date"] == "2026-10-21" and db.payouts[-1]["gross_amount"] == 0


def test_lookback_caps_the_window():
    api = Postex([])
    asyncio.run(px.sync_real_fees(DB(), api, date(2027, 6, 1)))
    assert api.calls == [("2027-03-03", "2027-06-01")]
