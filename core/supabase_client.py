import os, json
from datetime import datetime, timedelta
from supabase import create_client, Client

_client: Client = None

def get_client() -> Client:
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        _client = create_client(url, key)
    return _client

class Database:
    # ── Sessions ──────────────────────────────────────────────────────────────
    async def create_session(self, token, expires_at):
        get_client().table("sessions").insert({"token":token,"expires_at":expires_at.isoformat()}).execute()

    async def verify_session(self, token):
        res = get_client().table("sessions").select("id,expires_at").eq("token",token).execute()
        if not res.data: return False
        expires = datetime.fromisoformat(res.data[0]["expires_at"].replace("Z","+00:00"))
        return expires > datetime.utcnow().replace(tzinfo=expires.tzinfo)

    async def touch_session(self, token):
        get_client().table("sessions").update({"last_active":datetime.utcnow().isoformat()}).eq("token",token).execute()

    async def delete_session(self, token):
        get_client().table("sessions").delete().eq("token",token).execute()

    # ── Operation logs ────────────────────────────────────────────────────────
    async def create_operation_log(self, operation_name, scope, details, product_count):
        res = get_client().table("operation_log").insert({
            "operation_name":operation_name,
            "scope":json.dumps(scope),
            "details":json.dumps(details),
            "product_count":product_count,
            "performed_by":"admin"
        }).execute()
        return res.data[0]["id"]

    async def complete_operation_log(self, log_id, result):
        get_client().table("operation_log").update({"details":json.dumps(result)}).eq("id",log_id).execute()

    async def get_operation_logs(self, page=1, per_page=30):
        start = (page-1)*per_page
        res = get_client().table("operation_log").select("id,operation_name,scope,product_count,performed_at,performed_by,details").order("performed_at",desc=True).range(start,start+per_page-1).execute()
        return {"logs":res.data,"page":page}

    # ── Snapshots ─────────────────────────────────────────────────────────────
    async def save_snapshot(self, log_id, products):
        get_client().table("product_snapshots").insert({"operation_log_id":log_id,"snapshot_data":json.dumps(products)}).execute()

    async def get_snapshot(self, log_id):
        res = get_client().table("product_snapshots").select("snapshot_data,created_at").eq("operation_log_id",log_id).execute()
        if not res.data: return None
        row = res.data[0]
        created = datetime.fromisoformat(row["created_at"].replace("Z","+00:00"))
        if datetime.utcnow().replace(tzinfo=created.tzinfo) - created > timedelta(days=30): return None
        row["snapshot_data"] = json.loads(row["snapshot_data"]) if isinstance(row["snapshot_data"],str) else row["snapshot_data"]
        return row

    # ── Automation rules ──────────────────────────────────────────────────────
    async def get_automation_rules(self):
        try:
            res = get_client().table("automation_rules").select("*").order("created_at",desc=True).execute()
            return {"rules": res.data}
        except:
            return {"rules": []}

    async def create_automation_rule(self, data):
        res = get_client().table("automation_rules").insert({
            "name": data.get("name","Unnamed rule"),
            "condition": data.get("condition"),
            "condition_value": data.get("condition_value",""),
            "action": data.get("action"),
            "action_params": json.dumps(data.get("action_params",{})),
            "enabled": True,
        }).execute()
        return res.data[0]["id"]

    async def delete_automation_rule(self, rule_id):
        get_client().table("automation_rules").delete().eq("id",rule_id).execute()

    # ── Notifications ─────────────────────────────────────────────────────────
    async def get_notifications(self):
        try:
            res = get_client().table("notifications").select("*").order("created_at",desc=True).limit(20).execute()
            unread = sum(1 for n in res.data if not n.get("read"))
            return {"notifications": res.data, "unread": unread}
        except:
            return {"notifications": [], "unread": 0}

    async def add_notification(self, type: str, title: str, message: str = ""):
        try:
            get_client().table("notifications").insert({"type":type,"title":title,"message":message}).execute()
        except:
            pass

    async def mark_notifications_read(self):
        try:
            get_client().table("notifications").update({"read":True}).eq("read",False).execute()
        except:
            pass

    # ── Saved views ───────────────────────────────────────────────────────────
    async def get_saved_views(self):
        try:
            res = get_client().table("saved_views").select("*").order("created_at",desc=True).execute()
            return {"views": res.data}
        except:
            return {"views": []}

    async def create_saved_view(self, data):
        res = get_client().table("saved_views").insert({
            "name": data.get("name","My View"),
            "filters": json.dumps(data.get("filters",{})),
        }).execute()
        return res.data[0]["id"]

    async def delete_saved_view(self, view_id):
        get_client().table("saved_views").delete().eq("id",view_id).execute()

    # ── Cost settings ─────────────────────────────────────────────────────────
    async def get_cost_settings(self):
        try:
            res = get_client().table("cost_settings").select("key,value").execute()
            return {row["key"]: row["value"] for row in res.data}
        except:
            return {}

    async def update_cost_settings(self, settings: dict):
        for key, value in settings.items():
            try:
                get_client().table("cost_settings").upsert({"key":key,"value":str(value),"updated_at":datetime.utcnow().isoformat()}).execute()
            except:
                pass

    # ── Cleanup ───────────────────────────────────────────────────────────────
    async def purge_old_snapshots(self):
        cutoff = (datetime.utcnow()-timedelta(days=30)).isoformat()
        get_client().table("product_snapshots").delete().lt("created_at",cutoff).execute()

    # ── Accounting: generic helpers ──────────────────────────────────────────────
    # These tables only exist once accounting_schema.sql has been run, so every
    # read is defensive (returns empty rather than 500ing) in case it hasn't yet.
    async def _list(self, table, order_col="created_at", desc=True):
        try:
            res = get_client().table(table).select("*").order(order_col, desc=desc).execute()
            return res.data
        except Exception:
            return []

    async def _list_ranged(self, table, date_col, from_date=None, to_date=None, eq=None, desc=True):
        try:
            q = get_client().table(table).select("*").order(date_col, desc=desc)
            if from_date: q = q.gte(date_col, from_date)
            if to_date: q = q.lte(date_col, to_date)
            if eq:
                for k, v in eq.items():
                    q = q.eq(k, v)
            return q.execute().data
        except Exception:
            return []

    async def _insert(self, table, data):
        res = get_client().table(table).insert(data).execute()
        return res.data[0] if res.data else None

    async def _update(self, table, row_id, data):
        get_client().table(table).update(data).eq("id", row_id).execute()

    async def _delete(self, table, row_id):
        get_client().table(table).delete().eq("id", row_id).execute()

    # ── Vendors ───────────────────────────────────────────────────────────────
    async def list_vendors(self): return await self._list("vendors", order_col="name", desc=False)
    async def create_vendor(self, data): return await self._insert("vendors", data)
    async def update_vendor(self, vid, data): await self._update("vendors", vid, data)
    async def delete_vendor(self, vid): await self._delete("vendors", vid)

    # ── Vendor purchases ──────────────────────────────────────────────────────
    async def list_vendor_purchases(self, from_date=None, to_date=None):
        return await self._list_ranged("vendor_purchases", "purchase_date", from_date, to_date)
    async def create_vendor_purchase(self, data): return await self._insert("vendor_purchases", data)
    async def update_vendor_purchase(self, pid, data): await self._update("vendor_purchases", pid, data)
    async def delete_vendor_purchase(self, pid): await self._delete("vendor_purchases", pid)

    # ── Printers ──────────────────────────────────────────────────────────────
    async def list_printers(self): return await self._list("printers", order_col="name", desc=False)
    async def create_printer(self, data): return await self._insert("printers", data)
    async def update_printer(self, pid, data): await self._update("printers", pid, data)
    async def delete_printer(self, pid): await self._delete("printers", pid)

    async def log_printer_usage(self, printer_id, hours, log_date=None, notes=""):
        row = {
            "printer_id": printer_id, "hours": hours,
            "log_date": log_date or datetime.utcnow().date().isoformat(), "notes": notes,
        }
        get_client().table("printer_usage_logs").insert(row).execute()
        # keep the cached running total on the printer row in sync
        current = get_client().table("printers").select("total_hours").eq("id", printer_id).execute()
        total = float((current.data[0]["total_hours"] if current.data else 0) or 0) + float(hours)
        get_client().table("printers").update({"total_hours": total}).eq("id", printer_id).execute()
        return total

    async def list_printer_usage(self, printer_id):
        return await self._list_ranged("printer_usage_logs", "log_date", eq={"printer_id": printer_id})

    async def log_printer_maintenance(self, data): return await self._insert("printer_maintenance_logs", data)
    async def list_printer_maintenance(self, printer_id=None, from_date=None, to_date=None):
        eq = {"printer_id": printer_id} if printer_id else None
        return await self._list_ranged("printer_maintenance_logs", "log_date", from_date, to_date, eq=eq)
    async def delete_printer_maintenance(self, mid): await self._delete("printer_maintenance_logs", mid)

    # ── Custom orders ─────────────────────────────────────────────────────────
    async def list_custom_orders(self, from_date=None, to_date=None):
        return await self._list_ranged("custom_orders", "order_date", from_date, to_date)
    async def create_custom_order(self, data): return await self._insert("custom_orders", data)
    async def update_custom_order(self, oid, data): await self._update("custom_orders", oid, data)
    async def delete_custom_order(self, oid): await self._delete("custom_orders", oid)

    # ── Outsourced production jobs ────────────────────────────────────────────
    async def list_outsourced_jobs(self, from_date=None, to_date=None):
        return await self._list_ranged("outsourced_jobs", "date_sent", from_date, to_date)
    async def create_outsourced_job(self, data): return await self._insert("outsourced_jobs", data)
    async def update_outsourced_job(self, jid, data): await self._update("outsourced_jobs", jid, data)
    async def delete_outsourced_job(self, jid): await self._delete("outsourced_jobs", jid)

    # ── Expenses ──────────────────────────────────────────────────────────────
    async def list_expenses(self, from_date=None, to_date=None):
        return await self._list_ranged("expenses", "expense_date", from_date, to_date)
    async def create_expense(self, data): return await self._insert("expenses", data)
    async def delete_expense(self, eid): await self._delete("expenses", eid)

    # ── Employees & payments ──────────────────────────────────────────────────
    async def list_employees(self): return await self._list("employees", order_col="name", desc=False)
    async def create_employee(self, data): return await self._insert("employees", data)
    async def update_employee(self, eid, data): await self._update("employees", eid, data)
    async def delete_employee(self, eid): await self._delete("employees", eid)

    async def list_employee_payments(self, employee_id=None, from_date=None, to_date=None):
        eq = {"employee_id": employee_id} if employee_id else None
        return await self._list_ranged("employee_payments", "payment_date", from_date, to_date, eq=eq)
    async def create_employee_payment(self, data): return await self._insert("employee_payments", data)
    async def delete_employee_payment(self, pid): await self._delete("employee_payments", pid)

    # ── Cash accounts & transactions ──────────────────────────────────────────
    async def list_cash_accounts(self): return await self._list("cash_accounts", order_col="name", desc=False)
    async def create_cash_account(self, data): return await self._insert("cash_accounts", data)
    async def delete_cash_account(self, aid): await self._delete("cash_accounts", aid)

    async def list_cash_transactions(self, account_id=None, from_date=None, to_date=None):
        eq = {"account_id": account_id} if account_id else None
        return await self._list_ranged("cash_transactions", "txn_date", from_date, to_date, eq=eq)
    async def create_cash_transaction(self, data): return await self._insert("cash_transactions", data)
    async def delete_cash_transaction(self, tid): await self._delete("cash_transactions", tid)

    # ── PostEx payouts (settlement batches, for reconciliation) ──────────────
    async def list_postex_payouts(self, from_date=None, to_date=None):
        return await self._list_ranged("postex_payouts", "payout_date", from_date, to_date)
    async def create_postex_payout(self, data): return await self._insert("postex_payouts", data)
    async def delete_postex_payout(self, pid): await self._delete("postex_payouts", pid)

db = Database()
