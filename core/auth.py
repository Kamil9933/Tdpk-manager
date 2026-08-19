import os, secrets, hashlib, pyotp
from datetime import datetime, timedelta
from fastapi import HTTPException, Request
from core.supabase_client import db

def hash_pin(pin: str) -> str:
    salt = os.urandom(32).hex()
    h = hashlib.sha256((salt + pin).encode()).hexdigest()
    return f"{salt}:{h}"

def verify_pin(pin: str) -> bool:
    stored = os.getenv("PIN_HASH", "")
    if not stored or ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hashlib.sha256((salt + pin).encode()).hexdigest() == h

def generate_totp_secret() -> str:
    return pyotp.random_base32()

def verify_totp(code: str) -> bool:
    secret = os.getenv("TOTP_SECRET", "")
    if not secret:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)

async def create_session() -> str:
    token = secrets.token_urlsafe(48)
    expires_at = datetime.utcnow() + timedelta(hours=8)
    await db.create_session(token, expires_at)
    return token

async def delete_session(token: str):
    await db.delete_session(token)

async def verify_session(request: Request, return_bool: bool = False):
    token = request.cookies.get("session")
    if not token:
        if return_bool: return False
        raise HTTPException(status_code=401, detail="Not authenticated")
    valid = await db.verify_session(token)
    if not valid:
        if return_bool: return False
        raise HTTPException(status_code=401, detail="Session expired")
    await db.touch_session(token)
    return True
