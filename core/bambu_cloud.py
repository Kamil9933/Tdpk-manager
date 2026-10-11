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

import os
import httpx

BAMBU_CLOUD_EMAIL = os.getenv("BAMBU_CLOUD_EMAIL", "")
BAMBU_CLOUD_PASSWORD = os.getenv("BAMBU_CLOUD_PASSWORD", "")

BASE = "https://api.bambulab.com"
UA = "bambu_network_agent/01.09.05.01"


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


def _safe_json(resp) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"_non_json_body": resp.text[:500]}
