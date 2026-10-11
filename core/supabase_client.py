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

    async def update_printer_live_status(self, printer_id, status: dict):
        """Overwrites the printer row's current-state snapshot (not a log --
        see the add_bambu_live_status_to_printers migration). Called from the
        cron-tick poll, once per printer, every ~15 minutes."""
        get_client().table("printers").update({
            "live_status": json.dumps(status),
            "live_status_updated_at": datetime.utcnow().isoformat(),
        }).eq("id", printer_id).execute()

    # ── Print queue ───────────────────────────────────────────────────────────
    # Backs the dashboard's "Print Queue" tab -- the single source of truth
    # that replaced q.thirddimension.pk's queue_data.json (see the
    # create_print_queue migration + the one-time 373-row data migration).
    # `printer` here is a free-text label ('bambu1'/'bambu2'/...) matching the
    # `printers.name` column, NOT a foreign key -- same convention q.tdpk used.
    async def list_queue(self, statuses=None, printer=None):
        try:
            q = get_client().table("print_queue").select("*").order("created_at", desc=True)
            if statuses:
                q = q.in_("status", statuses if isinstance(statuses, list) else statuses.split(","))
            if printer:
                q = q.eq("printer", printer)
            return q.execute().data
        except Exception:
            return []

    async def get_queue_job(self, job_id):
        res = get_client().table("print_queue").select("*").eq("id", job_id).execute()
        return res.data[0] if res.data else None

    async def find_queue_job_by_line_item(self, shopify_order_id, shopify_item_id):
        res = (get_client().table("print_queue").select("id")
               .eq("shopify_order_id", str(shopify_order_id))
               .eq("shopify_item_id", str(shopify_item_id)).execute())
        return res.data[0] if res.data else None

    async def create_queue_job(self, data): return await self._insert("print_queue", data)
    async def update_queue_job(self, job_id, data): await self._update("print_queue", job_id, data)
    async def delete_queue_job(self, job_id): await self._delete("print_queue", job_id)

    async def list_queue_by_printer_status(self, printer, status):
        """Oldest-first -- used by the auto-sync job to pick which queued job
        a printer should pick up next, and to find the job currently printing
        on a given printer."""
        res = (get_client().table("print_queue").select("*")
               .eq("printer", printer).eq("status", status)
               .order("created_at", desc=False).execute())
        return res.data

    async def list_queue_by_phone(self, phone):
        try:
            res = (get_client().table("print_queue").select("*")
                   .eq("phone", phone).order("created_at", desc=True).execute())
            return res.data
        except Exception:
            return []

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

    # ── Automation autonomy toggles (Meta ads/social) ────────────────────────
    # Defensive like everything else here: if automation_schema.sql hasn't been
    # run yet, reads return a hardcoded all-off default rather than 500ing, so
    # the Marketing tab/MCP tools stay safe (ask-first) even before setup.
    _AUTOMATION_DEFAULTS = {
        "ads_pause_resume": {"enabled": False, "config": {}},
        "ads_budget_change": {"enabled": False, "config": {"ceiling_minor": None}},
        "social_publish": {"enabled": False, "config": {}},
        "comment_reply": {"enabled": False, "config": {}},
    }

    async def get_automation_settings(self):
        try:
            res = get_client().table("automation_settings").select("*").execute()
            found = {row["key"]: row for row in res.data}
        except Exception:
            found = {}
        out = {}
        for key, default in self._AUTOMATION_DEFAULTS.items():
            row = found.get(key)
            out[key] = {
                "enabled": bool(row["enabled"]) if row else default["enabled"],
                "config": row.get("config", {}) if row else default["config"],
            }
        return out

    async def set_automation_setting(self, key, enabled=None, config=None):
        if key not in self._AUTOMATION_DEFAULTS:
            raise ValueError(f"Unknown automation setting: {key}")
        data = {"key": key, "updated_at": datetime.utcnow().isoformat()}
        if enabled is not None:
            data["enabled"] = enabled
        if config is not None:
            data["config"] = config
        get_client().table("automation_settings").upsert(data).execute()
        settings = await self.get_automation_settings()
        return settings[key]

    # ── In-app agent (Phase 3) ───────────────────────────────────────────────
    async def create_agent_conversation(self, title=None):
        res = get_client().table("agent_conversations").insert({"title": title or "New conversation"}).execute()
        return res.data[0]

    async def list_agent_conversations(self, limit=30):
        try:
            res = (get_client().table("agent_conversations").select("*")
                   .order("updated_at", desc=True).limit(limit).execute())
            return res.data
        except Exception:
            return []

    async def get_agent_conversation(self, conversation_id):
        res = get_client().table("agent_conversations").select("*").eq("id", conversation_id).execute()
        return res.data[0] if res.data else None

    async def delete_agent_conversation(self, conversation_id):
        get_client().table("agent_conversations").delete().eq("id", conversation_id).execute()

    async def rename_agent_conversation(self, conversation_id, title):
        get_client().table("agent_conversations").update({
            "title": title, "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", conversation_id).execute()

    async def list_agent_messages(self, conversation_id):
        try:
            res = (get_client().table("agent_messages").select("*")
                   .eq("conversation_id", conversation_id).order("created_at").execute())
            return res.data
        except Exception:
            return []

    async def add_agent_message(self, conversation_id, role, content, tokens_in=0, tokens_out=0):
        res = get_client().table("agent_messages").insert({
            "conversation_id": conversation_id, "role": role, "content": content,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
        }).execute()
        return res.data[0] if res.data else None

    async def bump_agent_conversation_tokens(self, conversation_id, tokens_in, tokens_out, title=None):
        conv = await self.get_agent_conversation(conversation_id)
        if not conv:
            return
        data = {
            "total_input_tokens": (conv.get("total_input_tokens") or 0) + tokens_in,
            "total_output_tokens": (conv.get("total_output_tokens") or 0) + tokens_out,
            "updated_at": datetime.utcnow().isoformat(),
        }
        if title:
            data["title"] = title
        get_client().table("agent_conversations").update(data).eq("id", conversation_id).execute()

    # ── Filament inventory ────────────────────────────────────────────────────
    async def list_filament_types(self): return await self._list("filament_types", order_col="color", desc=False)
    async def create_filament_type(self, data): return await self._insert("filament_types", data)
    async def update_filament_type(self, tid, data): await self._update("filament_types", tid, data)
    async def delete_filament_type(self, tid): await self._delete("filament_types", tid)

    async def list_filament_purchases(self, from_date=None, to_date=None):
        return await self._list_ranged("filament_purchases", "purchase_date", from_date, to_date)
    async def create_filament_purchase(self, data): return await self._insert("filament_purchases", data)
    async def delete_filament_purchase(self, pid): await self._delete("filament_purchases", pid)

    async def list_filament_consumption(self, days=90):
        try:
            since = (datetime.utcnow() - timedelta(days=days)).isoformat()
            res = (get_client().table("filament_consumption").select("*")
                   .gte("created_at", since).order("created_at", desc=True).execute())
            return res.data
        except Exception:
            return []

    async def list_filament_needs_review(self, limit=50):
        try:
            res = (get_client().table("filament_consumption").select("*")
                   .eq("matched", "needs_review").order("created_at", desc=False).limit(limit).execute())
            return res.data
        except Exception:
            return []

    async def create_filament_consumption(self, data): return await self._insert("filament_consumption", data)

    async def resolve_filament_consumption(self, consumption_id, filament_type_id, grams=None):
        data = {"filament_type_id": filament_type_id, "matched": "manual"}
        if grams is not None:
            data["grams"] = grams
        await self._update("filament_consumption", consumption_id, data)

    async def order_has_filament_consumption(self, shopify_order_id):
        try:
            res = (get_client().table("filament_consumption").select("id")
                   .eq("shopify_order_id", shopify_order_id).limit(1).execute())
            return bool(res.data)
        except Exception:
            return False

    async def get_product_filament_link(self, product_id, variant_id=None):
        try:
            q = get_client().table("product_filament_links").select("*").eq("product_id", product_id)
            q = q.eq("variant_id", variant_id) if variant_id is not None else q.is_("variant_id", "null")
            res = q.execute()
            return res.data[0] if res.data else None
        except Exception:
            return None

    async def upsert_product_filament_link(self, product_id, variant_id, filament_type_id, grams_per_unit):
        try:
            get_client().table("product_filament_links").upsert({
                "product_id": product_id, "variant_id": variant_id,
                "filament_type_id": filament_type_id, "grams_per_unit": grams_per_unit,
                "updated_at": datetime.utcnow().isoformat(),
            }, on_conflict="product_id,variant_id").execute()
        except Exception:
            pass

    # ── Agent reminders (follow-ups the Manager chat sets for itself) ────────
    async def create_reminder(self, due_at, message, context=None):
        return await self._insert("agent_reminders", {
            "due_at": due_at, "message": message, "context": context or {},
        })

    async def list_reminders(self, status=None):
        try:
            q = get_client().table("agent_reminders").select("*").order("due_at")
            if status:
                q = q.eq("status", status)
            return q.execute().data
        except Exception:
            return []

    async def list_due_reminders(self):
        try:
            now = datetime.utcnow().isoformat()
            res = (get_client().table("agent_reminders").select("*")
                   .eq("status", "pending").lte("due_at", now).execute())
            return res.data
        except Exception:
            return []

    async def mark_reminder_sent(self, reminder_id):
        await self._update("agent_reminders", reminder_id, {"status": "sent", "sent_at": datetime.utcnow().isoformat()})

    async def cancel_reminder(self, reminder_id):
        await self._update("agent_reminders", reminder_id, {"status": "cancelled"})

    # ── Digest send-log (idempotency for the twice-daily digest) ─────────────
    async def digest_already_sent(self, slot):
        try:
            res = get_client().table("digest_log").select("id").eq("slot", slot).limit(1).execute()
            return bool(res.data)
        except Exception:
            return False

    async def mark_digest_sent(self, slot):
        try:
            get_client().table("digest_log").insert({"slot": slot}).execute()
        except Exception:
            pass

db = Database()
