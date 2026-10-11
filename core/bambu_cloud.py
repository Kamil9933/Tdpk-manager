"""
Bambu Lab Cloud login -- read-only. This module only ever needs to READ
printer status (what's printing, progress, done), never control a printer,
so it deliberately doesn't implement anything that could start/stop/change a
print. That also means it doesn't need the control-plane permissions Bambu
has been locking down for third-party tools recently; view-only access is a
much smaller ask.

The endpoints below are reverse-engineered (Bambu has no public API docs),
assembled from community write-ups and confirmed against this account's
actual login response -- treat the exact shapes as "this is what Bambu's
servers returned when we tried it", not a stable contract. If Bambu changes
something, login_start/login_verify will start returning unexpected shapes
and this module needs a look.

Flow (your account has 2FA on, confirmed by email-code login):
  1. login_start(email, password) -- POSTs credentials. Bambu's account
     requires an emailed verification code before issuing a token, so this
     also triggers the "send code" call. Returns {"needs_code": True}.
  2. login_verify(email, code) -- completes login with the 6-digit code from
     that email. Returns the raw response so we can see exactly what Bambu
     hands back (access token, any expiry/refresh fields) before building
     anything that depends on its shape.

Nothing here stores the account password anywhere but the Render env var it
already lives in (BAMBU_CLOUD_EMAIL / BAMBU_CLOUD_PASSWORD) -- this module
only ever reads those two, never logs or persists them elsewhere.
"""

import asyncio
import json
import os
import ssl
import time

import httpx

try:
    import aiomqtt
except ImportError:  # keeps the app importable even before the dep lands
    aiomqtt = None

BAMBU_CLOUD_EMAIL = os.getenv("BAMBU_CLOUD_EMAIL", "")
BAMBU_CLOUD_PASSWORD = os.getenv("BAMBU_CLOUD_PASSWORD", "")

BASE = "https://api.bambulab.com"
UA = "bambu_network_agent/01.09.05.01"

# Cloud MQTT broker Bambu's own apps use for live print telemetry (percent,
# remaining time, current file) -- REST only gives coarse online/RUNNING
# state, this is where the detail actually lives. Community-documented
# (OpenBambuAPI project), not officially published by Bambu.
MQTT_HOST = "us.mqtt.bambulab.com"
MQTT_PORT = 8883


async def login_start() -> dict:
    """Attempts the credential-only login first (in case 2FA isn't actually
    enforced every time), and separately fires the email-code request so a
    code is on its way regardless. Returns what Bambu's servers said, raw,
    so we can see the real shape instead of assuming one."""
    if not BAMBU_CLOUD_EMAIL or not BAMBU_CLOUD_PASSWORD:
        return {"ok": False, "error": "BAMBU_CLOUD_EMAIL/BAMBU_CLOUD_PASSWORD not set on the server"}

    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as client:
        login_resp = await client.post(
            f"{BASE}/v1/user-service/user/login",
            json={"account": BAMBU_CLOUD_EMAIL, "password": BAMBU_CLOUD_PASSWORD, "apiError": ""},
        )
        login_data = _safe_json(login_resp)

        # Already got a token without needing a code -- return it as-is.
        if login_data.get("accessToken"):
            return {"ok": True, "needs_code": False, "raw": login_data, "http_status": login_resp.status_code}

        # Explicitly told a code is required -- request one.
        code_resp = await client.post(
            f"{BASE}/v1/user-service/user/sendemail/code",
            json={"email": BAMBU_CLOUD_EMAIL, "type": "codeLogin"},
        )
        code_data = _safe_json(code_resp)

        return {
            "ok": True,
            "needs_code": True,
            "login_response": login_data,
            "login_http_status": login_resp.status_code,
            "send_code_response": code_data,
            "send_code_http_status": code_resp.status_code,
        }


async def login_verify(code: str) -> dict:
    """Completes login with the 6-digit emailed code. Returns the raw
    response untouched -- the caller decides what to do with whatever shape
    Bambu actually sends back."""
    if not BAMBU_CLOUD_EMAIL:
        return {"ok": False, "error": "BAMBU_CLOUD_EMAIL not set on the server"}

    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as client:
        resp = await client.post(
            f"{BASE}/v1/user-service/user/login",
            json={"account": BAMBU_CLOUD_EMAIL, "code": str(code).strip()},
        )
        data = _safe_json(resp)
        return {"ok": resp.status_code == 200 and bool(data.get("accessToken")), "raw": data, "http_status": resp.status_code}


async def list_bound_devices(access_token: str) -> dict:
    """Once we have a token, the next open question is whether this account
    can even list/read its bound printers under Bambu's tightened
    permissions -- this is the actual test of whether the whole approach is
    viable, not just whether login succeeds."""
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA, "Authorization": f"Bearer {access_token}"}) as client:
        resp = await client.get(f"{BASE}/v1/iot-service/api/user/bind")
        return {"http_status": resp.status_code, "raw": _safe_json(resp)}


async def get_print_status(access_token: str) -> dict:
    """Finer-grained than list_bound_devices: this endpoint (used by Bambu's
    own slicer) includes per-device task fields -- task_name (what's being
    printed), progress, prediction (time remaining), task_status, start_time
    -- not just the coarse online/RUNNING state. Community-documented, not
    officially published, so treat field meaning/units as unverified until we
    see a real in-progress job come back through it."""
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA, "Authorization": f"Bearer {access_token}"}) as client:
        resp = await client.get(f"{BASE}/v1/iot-service/api/user/print", params={"force": "true"})
        return {"http_status": resp.status_code, "raw": _safe_json(resp)}


async def get_uid(access_token: str) -> dict:
    """MQTT's cloud username is `u_{uid}` -- this account-profile endpoint is
    the only documented place that number comes from."""
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA, "Authorization": f"Bearer {access_token}"}) as client:
        resp = await client.get(f"{BASE}/v1/design-user-service/my/preference")
        data = _safe_json(resp)
        return {"http_status": resp.status_code, "uid": data.get("uid"), "raw": data}


async def mqtt_get_live_status(access_token: str, uid, dev_id: str, listen_seconds: float = 6.0) -> dict:
    """Connects to Bambu's cloud MQTT broker just long enough to ask for a
    full status push and capture whatever report messages come back, then
    disconnects. This is deliberately NOT a persistent connection -- Render's
    free tier can't hold one open anyway -- it's a short connect/ask/listen/
    close cycle, meant to be called from a periodic poll (the same cron-tick
    pattern already used elsewhere in this project), not a live socket.

    Unverified until tested against a printer mid-print: field names and
    whether a single pushall response is enough (vs needing to merge several
    partial messages, which some models send instead of the full object) are
    both open questions this function is built to answer."""
    if aiomqtt is None:
        return {"ok": False, "error": "aiomqtt is not installed"}
    if not uid:
        return {"ok": False, "error": "no uid -- call get_uid() first"}

    report_topic = f"device/{dev_id}/report"
    request_topic = f"device/{dev_id}/request"
    client_id = f"tdpk-manager-{int(time.time())}"
    ssl_context = ssl.create_default_context()

    messages = []
    merged_print = {}

    async def _collect(client):
        async for message in client.messages:
            try:
                payload = json.loads(message.payload)
            except Exception:
                continue
            messages.append(payload)
            print_block = payload.get("print")
            if isinstance(print_block, dict):
                merged_print.update(print_block)

    try:
        async with aiomqtt.Client(
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            username=f"u_{uid}",
            password=access_token,
            identifier=client_id,
            tls_context=ssl_context,
            timeout=15,
        ) as client:
            await client.subscribe(report_topic)
            await client.publish(request_topic, payload=json.dumps({
                "pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1},
            }))
            try:
                await asyncio.wait_for(_collect(client), timeout=listen_seconds)
            except asyncio.TimeoutError:
                pass

        return {
            "ok": True,
            "dev_id": dev_id,
            "messages_received": len(messages),
            "merged_print_status": merged_print,
            "raw_messages": messages[:10],  # capped -- diagnostic only
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _safe_json(resp) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"_non_json_body": resp.text[:500]}
