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

db = Database()
