"""
Business logic for the Accounts tab: P&L generation, printer depreciation,
cash account balances, and PostEx cash-collection reconciliation.

Design notes (read this before changing the numbers):
- Revenue and costs are counted on the date they're logged/dated (cash-ish basis,
  not formal accrual accounting) -- e.g. a vendor purchase counts in the month you
  logged it, not the month the filament is actually used. This keeps the model
  simple and matches how a small operation actually tracks money.
- "Recurring" expenses are NOT auto-expanded across months. You log each
  occurrence yourself (e.g. rent for August, then rent for September); the
  recurring/recurrence_period fields are just a label so you remember it repeats.
  Auto-generating entries risks silently double-counting or drifting from what
  actually happened.
- This is an operational P&L for your own visibility, not a compliance-grade
  ledger -- it doesn't do double-entry bookkeeping or tax filing. Export the
  numbers for your accountant if you need that.
"""

from datetime import datetime, date


def _to_date(d):
    if isinstance(d, date):
        return d
    if not d:
        return None
    try:
        return datetime.fromisoformat(str(d)[:10]).date()
    except ValueError:
        return None


def straight_line_depreciation(printer, as_of=None):
    """Straight-line depreciation for a single printer, as of a given date."""
    as_of = as_of or datetime.utcnow().date()
    cost = float(printer.get("purchase_cost", 0) or 0)
    salvage = float(printer.get("salvage_value", 0) or 0)
    life_years = float(printer.get("useful_life_years", 0) or 0) or 3
    purchase_date = _to_date(printer.get("purchase_date"))

    depreciable_base = max(cost - salvage, 0)
    annual = depreciable_base / life_years if life_years > 0 else 0
    monthly = annual / 12

    if not purchase_date:
        age_months = 0
    else:
        age_months = (as_of.year - purchase_date.year) * 12 + (as_of.month - purchase_date.month)
        age_months = max(age_months, 0)

    max_months = life_years * 12
    accumulated = min(monthly * age_months, depreciable_base) if max_months else 0
    book_value = max(cost - accumulated, salvage)
    fully_depreciated = age_months >= max_months if max_months else False

    return {
        "printer_id": printer.get("id"),
        "name": printer.get("name"),
        "purchase_cost": cost,
        "salvage_value": salvage,
        "useful_life_years": life_years,
        "annual_depreciation": round(annual, 2),
        "monthly_depreciation": round(monthly, 2),
        "age_months": age_months,
        "accumulated_depreciation": round(accumulated, 2),
        "book_value": round(book_value, 2),
        "fully_depreciated": fully_depreciated,
    }


def months_in_range(from_date, to_date):
    """Approximate number of months covered by a date range (min 1)."""
    f, t = _to_date(from_date), _to_date(to_date)
    if not f or not t:
        return 1
    months = (t.year - f.year) * 12 + (t.month - f.month) + 1
    return max(months, 1)


def calc_cash_balance(account, transactions):
    opening = float(account.get("opening_balance", 0) or 0)
    total_in = sum(float(t.get("amount", 0) or 0) for t in transactions if t.get("direction") == "in")
    total_out = sum(float(t.get("amount", 0) or 0) for t in transactions if t.get("direction") == "out")
    return round(opening + total_in - total_out, 2)


def reconcile_postex(postex_summary, payouts):
    """Compare what PostEx's own API says was delivered (should've been paid to you)
    against what you've actually logged as received via postex_payouts."""
    expected_collected = float(postex_summary.get("delivered_cod", 0) or 0)
    actually_received = sum(float(p.get("net_amount", 0) or 0) for p in payouts)
    total_fees = sum(float(p.get("fee_deducted", 0) or 0) for p in payouts)
    gap = round(expected_collected - actually_received - total_fees, 2)
    return {
        "expected_collected": round(expected_collected, 2),
        "actually_received": round(actually_received, 2),
        "total_courier_fees": round(total_fees, 2),
        "unreconciled_gap": gap,
        "note": (
            "Positive gap = PostEx shows more delivered COD than you've logged as "
            "received (payout not logged yet, or still pending settlement). "
            "Negative gap = you've logged more than PostEx's delivered total for "
            "this window -- check your date range or a payout logged twice."
        ),
    }


def generate_pnl(
    from_date, to_date,
    shopify_orders=None, custom_orders=None,
    vendor_purchases=None, outsourced_jobs=None,
    expenses=None, employee_payments=None,
    printer_maintenance=None, printers=None,
    postex_payouts=None,
):
    shopify_orders = shopify_orders or []
    custom_orders = custom_orders or []
    vendor_purchases = vendor_purchases or []
    outsourced_jobs = outsourced_jobs or []
    expenses = expenses or []
    employee_payments = employee_payments or []
    printer_maintenance = printer_maintenance or []
    printers = printers or []
    postex_payouts = postex_payouts or []

    # ── Revenue ──────────────────────────────────────────────────────────────
    shopify_revenue = sum(float(o.get("total_price", 0) or 0) for o in shopify_orders)
    shopify_tax = sum(float(o.get("total_tax", 0) or 0) for o in shopify_orders)
    custom_revenue = sum(float(c.get("sale_price", 0) or 0) for c in custom_orders)
    custom_tax = sum(float(c.get("tax_amount", 0) or 0) for c in custom_orders)
    total_revenue = round(shopify_revenue + custom_revenue, 2)
    total_tax = round(shopify_tax + custom_tax, 2)

    # ── Cost of goods sold ──────────────────────────────────────────────────
    material_cost = sum(
        float(p.get("total_cost", 0) or 0) for p in vendor_purchases
        if p.get("category") in ("filament", "packaging", "parts")
    )
    outsourced_cost = sum(float(j.get("cost_charged", 0) or 0) for j in outsourced_jobs)
    maintenance_cost = sum(float(m.get("cost", 0) or 0) for m in printer_maintenance)

    n_months = months_in_range(from_date, to_date)
    depreciation_total = 0.0
    depreciation_detail = []
    for printer in printers:
        if printer.get("status") == "retired":
            continue
        dep = straight_line_depreciation(printer, as_of=_to_date(to_date))
        period_dep = round(dep["monthly_depreciation"] * n_months, 2)
        depreciation_total += period_dep
        depreciation_detail.append({**dep, "period_depreciation": period_dep})

    cogs_total = round(material_cost + outsourced_cost + maintenance_cost + depreciation_total, 2)

    # ── Operating expenses (everything logged in `expenses` for the period) ──
    opex_by_category = {}
    for e in expenses:
        cat = e.get("category", "other")
        opex_by_category[cat] = round(opex_by_category.get(cat, 0) + float(e.get("amount", 0) or 0), 2)
    opex_total = round(sum(opex_by_category.values()), 2)

    # ── Courier fees (from logged PostEx settlement batches) ────────────────
    courier_fees = round(sum(float(p.get("fee_deducted", 0) or 0) for p in postex_payouts), 2)

    # ── Labor ─────────────────────────────────────────────────────────────
    labor_cost = round(sum(float(p.get("amount", 0) or 0) for p in employee_payments), 2)

    total_costs = round(cogs_total + opex_total + courier_fees + labor_cost, 2)
    net_profit = round(total_revenue - total_costs, 2)
    margin_pct = round((net_profit / total_revenue * 100), 1) if total_revenue else 0.0

    # ── Cash actually collected (rough, see module docstring) ───────────────
    prepaid_cash = sum(
        float(o.get("total_price", 0) or 0) for o in shopify_orders
        if str(o.get("financial_status", "")).lower() == "paid"
    )
    postex_cash = sum(float(p.get("net_amount", 0) or 0) for p in postex_payouts)
    custom_cash = sum(float(c.get("amount_paid", 0) or 0) for c in custom_orders)
    cash_collected = round(prepaid_cash + postex_cash + custom_cash, 2)

    return {
        "period": {"from": str(from_date), "to": str(to_date), "months": n_months},
        "revenue": {
            "shopify": round(shopify_revenue, 2),
            "custom_orders": round(custom_revenue, 2),
            "total": total_revenue,
            "tax_included": total_tax,
        },
        "cogs": {
            "materials_packaging": round(material_cost, 2),
            "outsourced_production": round(outsourced_cost, 2),
            "printer_maintenance": round(maintenance_cost, 2),
            "printer_depreciation": round(depreciation_total, 2),
            "total": cogs_total,
            "depreciation_detail": depreciation_detail,
        },
        "opex": {"by_category": opex_by_category, "total": opex_total},
        "courier_fees": courier_fees,
        "labor_cost": labor_cost,
        "total_costs": total_costs,
        "net_profit": net_profit,
        "margin_pct": margin_pct,
        "cash_collected_estimate": cash_collected,
    }
