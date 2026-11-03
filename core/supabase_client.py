import os, json, asyncio, threading
from io import BytesIO
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

_client: Client = None


# supabase-py shares ONE synchronous HTTP/2 connection, which breaks when
# several threads use it at once ("Resource temporarily unavailable", and
# every request on it fails -- seen live on 2026-11-02 when the dashboard
# loaded ~10 endpoints in parallel). So calls run one at a time, as they did
# before, but in a worker thread: a slow query waits its turn without
# freezing the event loop (Shopify/PostEx/Gemini calls keep going).
_db_lock = threading.Lock()


def _execute_locked(query):
    with _db_lock:
        return query.execute()


async def _run(query):
    return await asyncio.to_thread(_execute_locked, query)

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
        await _run(get_client().table("sessions").insert({"token":token,"expires_at":expires_at.isoformat()}))

    async def verify_session(self, token):
        res = await _run(get_client().table("sessions").select("id,expires_at").eq("token",token))
        if not res.data: return False
        expires = datetime.fromisoformat(res.data[0]["expires_at"].replace("Z","+00:00"))
        return expires > datetime.utcnow().replace(tzinfo=expires.tzinfo)

    async def touch_session(self, token):
        await _run(get_client().table("sessions").update({"last_active":datetime.utcnow().isoformat()}).eq("token",token))

    async def delete_session(self, token):
        await _run(get_client().table("sessions").delete().eq("token",token))

    async def purge_expired_sessions(self):
        await _run(get_client().table("sessions").delete().lt("expires_at", datetime.utcnow().isoformat()))

    # ── Operation logs ────────────────────────────────────────────────────────
    async def create_operation_log(self, operation_name, scope, details, product_count):
        res = await _run(get_client().table("operation_log").insert({
            "operation_name":operation_name,
            "scope":json.dumps(scope),
            "details":json.dumps(details),
            "product_count":product_count,
            "performed_by":"admin"
        }))
        return res.data[0]["id"]

    async def complete_operation_log(self, log_id, result):
        await _run(get_client().table("operation_log").update({"details":json.dumps(result)}).eq("id",log_id))

    async def get_operation_logs(self, page=1, per_page=30):
        start = (page-1)*per_page
        res = await _run(get_client().table("operation_log").select("id,operation_name,scope,product_count,performed_at,performed_by,details").order("performed_at",desc=True).range(start,start+per_page-1))
        return {"logs":res.data,"page":page}

    async def get_operation_log(self, log_id):
        res = await _run(get_client().table("operation_log").select("*").eq("id", log_id))
        return res.data[0] if res.data else None

    # ── Snapshots ─────────────────────────────────────────────────────────────
    async def save_snapshot(self, log_id, products):
        await _run(get_client().table("product_snapshots").insert({"operation_log_id":log_id,"snapshot_data":json.dumps(products)}))

    async def get_snapshot(self, log_id):
        res = await _run(get_client().table("product_snapshots").select("snapshot_data,created_at").eq("operation_log_id",log_id))
        if not res.data: return None
        row = res.data[0]
        created = datetime.fromisoformat(row["created_at"].replace("Z","+00:00"))
        if datetime.utcnow().replace(tzinfo=created.tzinfo) - created > timedelta(days=30): return None
        row["snapshot_data"] = json.loads(row["snapshot_data"]) if isinstance(row["snapshot_data"],str) else row["snapshot_data"]
        return row

    # ── Automation rules ──────────────────────────────────────────────────────
    async def get_automation_rules(self):
        try:
            res = await _run(get_client().table("automation_rules").select("*").order("created_at",desc=True))
            return {"rules": res.data}
        except:
            return {"rules": []}

    async def create_automation_rule(self, data):
        res = await _run(get_client().table("automation_rules").insert({
            "name": data.get("name","Unnamed rule"),
            "condition": data.get("condition"),
            "condition_value": data.get("condition_value",""),
            "action": data.get("action"),
            "action_params": json.dumps(data.get("action_params",{})),
            "enabled": True,
        }))
        return res.data[0]["id"]

    async def delete_automation_rule(self, rule_id):
        await _run(get_client().table("automation_rules").delete().eq("id",rule_id))

    # ── Notifications ─────────────────────────────────────────────────────────
    async def get_notifications(self):
        try:
            res = await _run(get_client().table("notifications").select("*").order("created_at",desc=True).limit(20))
            unread = sum(1 for n in res.data if not n.get("read"))
            return {"notifications": res.data, "unread": unread}
        except:
            return {"notifications": [], "unread": 0}

    async def add_notification(self, type: str, title: str, message: str = ""):
        try:
            await _run(get_client().table("notifications").insert({"type":type,"title":title,"message":message}))
        except:
            pass

    async def mark_notifications_read(self):
        try:
            await _run(get_client().table("notifications").update({"read":True}).eq("read",False))
        except:
            pass

    # ── Saved views ───────────────────────────────────────────────────────────
    async def get_saved_views(self):
        try:
            res = await _run(get_client().table("saved_views").select("*").order("created_at",desc=True))
            return {"views": res.data}
        except:
            return {"views": []}

    async def create_saved_view(self, data):
        res = await _run(get_client().table("saved_views").insert({
            "name": data.get("name","My View"),
            "filters": json.dumps(data.get("filters",{})),
        }))
        return res.data[0]["id"]

    async def delete_saved_view(self, view_id):
        await _run(get_client().table("saved_views").delete().eq("id",view_id))

    # ── Cost settings ─────────────────────────────────────────────────────────
    async def get_cost_settings(self):
        try:
            res = await _run(get_client().table("cost_settings").select("key,value"))
            return {row["key"]: row["value"] for row in res.data}
        except:
            return {}

    async def update_cost_settings(self, settings: dict):
        for key, value in settings.items():
            try:
                await _run(get_client().table("cost_settings").upsert({"key":key,"value":str(value),"updated_at":datetime.utcnow().isoformat()}))
            except:
                pass

    # ── Cleanup ───────────────────────────────────────────────────────────────
    async def purge_old_snapshots(self):
        cutoff = (datetime.utcnow()-timedelta(days=30)).isoformat()
        await _run(get_client().table("product_snapshots").delete().lt("created_at",cutoff))

    # ── Accounting: generic helpers ──────────────────────────────────────────────
    # These tables only exist once accounting_schema.sql has been run, so every
    # read is defensive (returns empty rather than 500ing) in case it hasn't yet.
    async def _list(self, table, order_col="created_at", desc=True):
        try:
            res = await _run(get_client().table(table).select("*").order(order_col, desc=desc))
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
            return (await _run(q)).data
        except Exception:
            return []

    async def _insert(self, table, data):
        res = await _run(get_client().table(table).insert(data))
        return res.data[0] if res.data else None

    async def _update(self, table, row_id, data):
        await _run(get_client().table(table).update(data).eq("id", row_id))

    async def _delete(self, table, row_id):
        await _run(get_client().table(table).delete().eq("id", row_id))

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
        await _run(get_client().table("printer_usage_logs").insert(row))
        # keep the cached running total on the printer row in sync
        current = await _run(get_client().table("printers").select("total_hours").eq("id", printer_id))
        total = float((current.data[0]["total_hours"] if current.data else 0) or 0) + float(hours)
        await _run(get_client().table("printers").update({"total_hours": total}).eq("id", printer_id))
        return total

    async def list_printer_usage(self, printer_id):
        return await self._list_ranged("printer_usage_logs", "log_date", eq={"printer_id": printer_id})

    async def update_printer_live_status(self, printer_id, status: dict):
        """Overwrites the printer row's current-state snapshot (not a log --
        see the add_bambu_live_status_to_printers migration). Called from the
        cron-tick poll (and the queue tab refresh), once per printer."""
        await _run(get_client().table("printers").update({
            "live_status": json.dumps(status),
            "live_status_updated_at": datetime.utcnow().isoformat(),
        }).eq("id", printer_id))

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
            return (await _run(q)).data
        except Exception:
            return []

    async def get_queue_job(self, job_id):
        res = await _run(get_client().table("print_queue").select("*").eq("id", job_id))
        return res.data[0] if res.data else None

    async def find_queue_job_by_line_item(self, shopify_order_id, shopify_item_id):
        res = (await _run(get_client().table("print_queue").select("id")
               .eq("shopify_order_id", str(shopify_order_id))
               .eq("shopify_item_id", str(shopify_item_id))))
        return res.data[0] if res.data else None

    async def create_queue_job(self, data): return await self._insert("print_queue", data)
    async def update_queue_job(self, job_id, data): await self._update("print_queue", job_id, data)
    async def delete_queue_job(self, job_id): await self._delete("print_queue", job_id)

    async def list_queue_by_printer_status(self, printer, status):
        """Oldest-first -- used by the auto-sync job to pick which queued job
        a printer should pick up next, and to find the job currently printing
        on a given printer."""
        res = (await _run(get_client().table("print_queue").select("*")
               .eq("printer", printer).eq("status", status)
               .order("created_at", desc=False)))
        return res.data

    async def list_queue_by_phone(self, phone):
        try:
            res = (await _run(get_client().table("print_queue").select("*")
                   .eq("phone", phone).order("created_at", desc=True)))
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
            res = await _run(get_client().table("automation_settings").select("*"))
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
        await _run(get_client().table("automation_settings").upsert(data))
        settings = await self.get_automation_settings()
        return settings[key]

    # ── In-app agent (Phase 3) ───────────────────────────────────────────────
    async def create_agent_conversation(self, title=None):
        res = await _run(get_client().table("agent_conversations").insert({"title": title or "New conversation"}))
        return res.data[0]

    async def list_agent_conversations(self, limit=30):
        try:
            res = (await _run(get_client().table("agent_conversations").select("*")
                   .order("updated_at", desc=True).limit(limit)))
            return res.data
        except Exception:
            return []

    async def get_agent_conversation(self, conversation_id):
        res = await _run(get_client().table("agent_conversations").select("*").eq("id", conversation_id))
        return res.data[0] if res.data else None

    async def delete_agent_conversation(self, conversation_id):
        await _run(get_client().table("agent_conversations").delete().eq("id", conversation_id))

    async def rename_agent_conversation(self, conversation_id, title):
        await _run(get_client().table("agent_conversations").update({
            "title": title, "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", conversation_id))

    async def list_agent_messages(self, conversation_id):
        try:
            res = (await _run(get_client().table("agent_messages").select("*")
                   .eq("conversation_id", conversation_id).order("created_at")))
            return res.data
        except Exception:
            return []

    async def add_agent_message(self, conversation_id, role, content, tokens_in=0, tokens_out=0):
        res = await _run(get_client().table("agent_messages").insert({
            "conversation_id": conversation_id, "role": role, "content": content,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
        }))
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
        await _run(get_client().table("agent_conversations").update(data).eq("id", conversation_id))

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
            res = (await _run(get_client().table("filament_consumption").select("*")
                   .gte("created_at", since).order("created_at", desc=True)))
            return res.data
        except Exception:
            return []

    async def list_filament_needs_review(self, limit=50):
        try:
            res = (await _run(get_client().table("filament_consumption").select("*")
                   .eq("matched", "needs_review").order("created_at", desc=False).limit(limit)))
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
            res = (await _run(get_client().table("filament_consumption").select("id")
                   .eq("shopify_order_id", shopify_order_id).limit(1)))
            return bool(res.data)
        except Exception:
            return False

    async def get_product_filament_link(self, product_id, variant_id=None):
        try:
            q = get_client().table("product_filament_links").select("*").eq("product_id", product_id)
            q = q.eq("variant_id", variant_id) if variant_id is not None else q.is_("variant_id", "null")
            res = await _run(q)
            return res.data[0] if res.data else None
        except Exception:
            return None

    async def list_product_filament_links(self):
        return await self._list("product_filament_links", order_col="product_id", desc=False)

    async def upsert_product_filament_link(self, product_id, variant_id, filament_type_id, grams_per_unit):
        try:
            await _run(get_client().table("product_filament_links").upsert({
                "product_id": product_id, "variant_id": variant_id,
                "filament_type_id": filament_type_id, "grams_per_unit": grams_per_unit,
                "updated_at": datetime.utcnow().isoformat(),
            }, on_conflict="product_id,variant_id"))
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
            return (await _run(q)).data
        except Exception:
            return []

    async def list_due_reminders(self):
        try:
            now = datetime.utcnow().isoformat()
            res = (await _run(get_client().table("agent_reminders").select("*")
                   .eq("status", "pending").lte("due_at", now)))
            return res.data
        except Exception:
            return []

    async def mark_reminder_sent(self, reminder_id):
        await self._update("agent_reminders", reminder_id, {"status": "sent", "sent_at": datetime.utcnow().isoformat()})

    async def cancel_reminder(self, reminder_id):
        await self._update("agent_reminders", reminder_id, {"status": "cancelled"})

    # ── Digest send-log (idempotency for the twice-daily digest) ─────────────
    async def claim_slot(self, slot):
        """Atomically claims a cron job slot (digest, ad-spend sync, ...) by
        inserting its digest_log row -- `slot` is unique, so only one caller
        ever gets True. Any failure (duplicate, or the database being
        unreachable) returns False: skipping a run is safer than sending a
        digest twice or logging an expense twice."""
        try:
            res = await _run(get_client().table("digest_log").insert({"slot": slot}))
            return bool(res.data)
        except Exception:
            return False

    # ── SEO (tables from seo_schema.sql, written by seo/seo_report.py) ──────
    async def latest_seo_run(self):
        try:
            res = await _run(get_client().table("seo_runs").select("*").order("started_at", desc=True).limit(1))
            return res.data[0] if res.data else None
        except Exception:
            return None

    async def count_seo_suggestions(self, status="draft"):
        try:
            res = await _run(get_client().table("seo_suggestions").select("id", count="exact").eq("status", status))
            return res.count or 0
        except Exception:
            return 0

    async def list_seo_suggestions(self, status=None):
        try:
            q = get_client().table("seo_suggestions").select("*").order("created_at", desc=True)
            if status:
                q = q.eq("status", status)
            return (await _run(q)).data
        except Exception:
            return []

    async def get_seo_suggestion(self, suggestion_id):
        res = await _run(get_client().table("seo_suggestions").select("*").eq("id", suggestion_id))
        return res.data[0] if res.data else None

    async def update_seo_suggestion(self, suggestion_id, data):
        await self._update("seo_suggestions", suggestion_id, data)

    # ── SEO history for the Rankings tab (read-only) ──
    SEO_HISTORY_MAX_ROWS = 20000

    async def seo_history(self, table, columns, order_col, since=None, since_col=None):
        """Every row (paged past PostgREST's 1,000-row cap), oldest first.
        Returns None if the table doesn't exist yet (seo_rankings before its
        SQL has been run); other errors propagate."""
        rows, start, page = [], 0, 1000
        while start < self.SEO_HISTORY_MAX_ROWS:
            q = get_client().table(table).select(columns).order(order_col).range(start, start + page - 1)
            if since and since_col:
                q = q.gte(since_col, since)
            try:
                res = await _run(q)
            except Exception as e:
                if "PGRST205" in str(e) or "does not exist" in str(e) or "Could not find the table" in str(e):
                    return None
                raise
            rows += res.data
            if len(res.data) < page:
                break
            start += page
        return rows

    # ── Social media files (bucket from social_media_schema.sql) ─────────────
    # Instagram and Facebook fetch media by public URL, so edited photos and
    # videos live in a public Supabase Storage bucket. Files sit at the bucket
    # root; the object name (path) doubles as the media_id the agent tools use.
    async def upload_media(self, file_bytes, filename, content_type):
        body, content_type, ext = prepare_media(file_bytes, content_type)
        path = media_path(filename, ext)
        await _storage(lambda b: b.upload(path, body, {"content-type": content_type, "upsert": "false"}))
        return {"path": path, "public_url": media_public_url(path),
                "size": len(body), "content_type": content_type}

    async def list_media(self):
        rows = await _storage(lambda b: b.list("", {"limit": 1000, "offset": 0,
                                                    "sortBy": {"column": "created_at", "order": "desc"}}))
        return [_media_item(r) for r in rows if r.get("id")]  # folders have no id

    async def get_media(self, path):
        return next((m for m in await self.list_media() if m["path"] == path), None)

    async def delete_media(self, path):
        await _storage(lambda b: b.remove([path]))

    # ── Media edit jobs (media_jobs_schema.sql) ──
    async def create_media_job(self, data):
        return await self._insert("media_jobs", data)

    async def get_media_job(self, job_id):
        res = await _run(get_client().table("media_jobs").select("*").eq("id", job_id))
        return res.data[0] if res.data else None

    async def update_media_job(self, job_id, data):
        await self._update("media_jobs", job_id, {**data, "updated_at": datetime.now(timezone.utc).isoformat()})

    async def list_media_jobs(self, limit=20):
        res = await _run(get_client().table("media_jobs").select("*").order("created_at", desc=True).limit(limit))
        return res.data

    async def fail_stale_media_jobs(self):
        """At startup: any job still queued/running was cut off by a restart."""
        await _run(get_client().table("media_jobs").update({"status": "failed", "error": "The server restarted during this edit. Run it again."})
                   .in_("status", ["queued", "running"]))

    # ── Brand assets (private media-assets bucket): logo, font, music ──
    async def upload_media_asset(self, kind, file_bytes, filename):
        path, content_type = media_asset_path(kind, filename, file_bytes)
        if kind == "font":  # one font at a time: drop the other format
            await _assets(lambda b: b.remove(["font.ttf", "font.otf"]))
        await _assets(lambda b: b.upload(path, file_bytes, {"content-type": content_type, "upsert": "true"}))
        return {"path": path, "kind": kind, "size": len(file_bytes)}

    async def list_media_assets(self):
        root = await _assets(lambda b: b.list("", {"limit": 100}))
        music = await _assets(lambda b: b.list("music", {"limit": 100}))
        out = [{"path": r["name"], "kind": "logo" if r["name"] == "logo.png" else "font",
                "size": (r.get("metadata") or {}).get("size")} for r in root if r.get("id")]
        out += [{"path": f"music/{r['name']}", "kind": "music", "size": (r.get("metadata") or {}).get("size")}
                for r in music if r.get("id")]
        return out

    async def delete_media_asset(self, path):
        await _assets(lambda b: b.remove([path]))

    async def fetch_media_assets(self, folder, music=None):
        """Downloads the logo, font and (if asked for) one music file into
        `folder`; returns {"logo": path, "font": path, "music": path}, leaving
        out anything not uploaded. A missing music file is an error, a missing
        logo/font just means the built-in fallbacks are used."""
        names = {a["path"] for a in await self.list_media_assets()}
        out = {}
        for key, candidates in (("logo", ["logo.png"]), ("font", ["font.ttf", "font.otf"])):
            for name in candidates:
                if name in names:
                    out[key] = _write(folder, name, await _assets(lambda b, n=name: b.download(n)))
                    break
        if music:
            name = music if music.startswith("music/") else f"music/{music}"
            if name not in names:
                raise MediaError(f"No music file called '{music}'. Upload it under Media > Brand assets first.")
            out["music"] = _write(folder, name.replace("/", "_"), await _assets(lambda b: b.download(name)))
        return out

    async def purge_old_media(self, days):
        """Deletes files older than `days`. Idempotent: a second run finds
        nothing old enough left to delete."""
        old = media_older_than(await self.list_media(), datetime.now(timezone.utc) - timedelta(days=days))
        if old:
            await _storage(lambda b: b.remove([m["path"] for m in old]))
        return [m["path"] for m in old]


MEDIA_BUCKET = "social-media"
MEDIA_MAX_BYTES = 50 * 1024 * 1024
# Upload type -> extension. PNG is accepted but stored as JPEG (Instagram's
# feed only takes JPEG); videos are stored exactly as uploaded.
MEDIA_TYPES = {"image/jpeg": "jpg", "image/png": "png", "video/mp4": "mp4", "video/quicktime": "mov"}
_EXT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "mp4": "video/mp4", "mov": "video/quicktime"}


class MediaError(ValueError):
    """A file that can't be stored, with a message fit to show the user."""


async def _storage(fn):
    """Storage calls also go through supabase-py's sync client, so they take
    the same lock as table queries (see _db_lock)."""
    bucket = get_client().storage.from_(MEDIA_BUCKET)
    return await asyncio.to_thread(lambda: _with_lock(fn, bucket))


def _with_lock(fn, bucket):
    with _db_lock:
        return fn(bucket)


ASSETS_BUCKET = "media-assets"
ASSET_TYPES = {
    "logo": {"png": "image/png"},
    "font": {"ttf": "font/ttf", "otf": "font/otf"},
    "music": {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav"},
}
ASSET_MAX_BYTES = 20 * 1024 * 1024


async def _assets(fn):
    bucket = get_client().storage.from_(ASSETS_BUCKET)
    return await asyncio.to_thread(lambda: _with_lock(fn, bucket))


def _write(folder, name, data):
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def media_asset_path(kind, filename, file_bytes):
    """Fixed names for the logo and font (one of each), music under music/."""
    import re
    if kind not in ASSET_TYPES:
        raise MediaError("Asset kind must be logo, font or music.")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed = ASSET_TYPES[kind]
    if ext not in allowed:
        raise MediaError(f"A {kind} must be a {' or '.join('.' + e for e in allowed)} file.")
    if not file_bytes:
        raise MediaError("The file is empty.")
    if len(file_bytes) > ASSET_MAX_BYTES:
        raise MediaError("Asset files are limited to 20 MB.")
    if kind == "logo":
        from PIL import Image
        try:
            if Image.open(BytesIO(file_bytes)).mode not in ("RGBA", "LA", "P"):
                raise MediaError("The logo PNG has no transparency, so it would show as a box. Export it with a transparent background.")
        except MediaError:
            raise
        except Exception:
            raise MediaError("That PNG could not be read.")
        return "logo.png", allowed[ext]
    if kind == "font":
        return f"font.{ext}", allowed[ext]
    stem = re.sub(r"[^a-z0-9]+", "-", filename.rsplit(".", 1)[0].lower()).strip("-")[:50] or "track"
    return f"music/{stem}.{ext}", allowed[ext]


def media_content_type(content_type, filename=""):
    """Browsers sometimes send a blank or generic type (application/octet-stream);
    fall back to the file extension then."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "image/jpg":
        ct = "image/jpeg"
    if ct not in MEDIA_TYPES:
        ct = _EXT_TYPES.get(filename.rsplit(".", 1)[-1].lower(), ct) if "." in filename else ct
    return ct


def prepare_media(file_bytes, content_type):
    """Validates a file and returns (bytes, content_type, extension) ready to
    store. PNG becomes JPEG; nothing else is re-encoded."""
    if content_type not in MEDIA_TYPES:
        raise MediaError(f"Unsupported file type '{content_type or 'unknown'}'. "
                         "Upload a JPEG or PNG image, or an MP4 or MOV video.")
    if not file_bytes:
        raise MediaError("The file is empty.")
    if len(file_bytes) > MEDIA_MAX_BYTES:
        raise MediaError(f"File is {len(file_bytes) / 1048576:.1f} MB; the limit is 50 MB.")
    if content_type == "image/png":
        return png_to_jpeg(file_bytes), "image/jpeg", "jpg"
    return file_bytes, content_type, MEDIA_TYPES[content_type]


def png_to_jpeg(png_bytes, quality=92):
    from io import BytesIO
    from PIL import Image
    try:
        img = Image.open(BytesIO(png_bytes))
        img.load()
    except Exception:
        raise MediaError("That PNG could not be read. Is the file damaged?")
    if img.mode in ("RGBA", "LA", "P"):
        # JPEG has no transparency: flatten onto white, not the default black.
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    out = BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def media_path(filename, ext, now=None):
    """Unique object name: UTC timestamp + random suffix + a safe version of
    the original name, e.g. 20261103-141502-a1b2c3-batman-lamp.jpg."""
    import re, secrets
    now = now or datetime.now(timezone.utc)
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60].strip("-") or "media"
    return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}-{stem}.{ext}"


def media_public_url(path):
    return f"{os.getenv('SUPABASE_URL', '').rstrip('/')}/storage/v1/object/public/{MEDIA_BUCKET}/{path}"


def _media_item(row):
    meta = row.get("metadata") or {}
    path = row["name"]
    ct = meta.get("mimetype") or _EXT_TYPES.get(path.rsplit(".", 1)[-1].lower(), "")
    return {"path": path, "public_url": media_public_url(path), "size": meta.get("size"),
            "content_type": ct, "kind": "video" if ct.startswith("video/") else "image",
            "created_at": row.get("created_at")}


def media_older_than(items, cutoff):
    old = []
    for m in items:
        try:
            created = datetime.fromisoformat((m.get("created_at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue  # unknown age: keep it
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < cutoff:
            old.append(m)
    return old

db = Database()
