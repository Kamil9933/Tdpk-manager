import os, secrets, hashlib, hmac, time, pyotp
from datetime import datetime, timedelta
from fastapi import HTTPException, Request
from core.supabase_client import db

# New PIN hashes use PBKDF2 (slow on purpose, so a leaked PIN_HASH can't be
# brute-forced in seconds -- a 4-6 digit PIN has at most a million values).
# The old single-SHA256 "salt:hash" format still verifies, so the PIN_HASH
# already set on Render keeps working; re-run /setup to upgrade it.
PBKDF2_ITERATIONS = 600_000

# Login guess limit, per client IP, in memory (single Render instance; a
# restart resets it, which is fine -- the TOTP code is the real barrier).
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60
_failed_logins = {}  # ip -> [timestamps of recent failures]

def hash_pin(pin: str) -> str:
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${h}"

def verify_pin(pin: str) -> bool:
    stored = os.getenv("PIN_HASH", "")
    if stored.startswith("pbkdf2$"):
        try:
            _, iterations, salt, h = stored.split("$")
            calc = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(iterations)).hex()
        except ValueError:
            return False
        return hmac.compare_digest(calc, h)
    if not stored or ":" not in stored:
        return False
    salt, h = stored.split(":", 1)  # legacy format
    return hmac.compare_digest(hashlib.sha256((salt + pin).encode()).hexdigest(), h)

def client_ip(request: Request) -> str:
    # Render sits behind a proxy; the first X-Forwarded-For entry is the client.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")

def _recent_failures(ip: str, now: float):
    recent = [t for t in _failed_logins.get(ip, []) if now - t < LOCKOUT_SECONDS]
    _failed_logins[ip] = recent
    return recent

def login_locked_for(ip: str) -> int:
    """Seconds until this IP may try again (0 = not locked)."""
    now = time.time()
    recent = _recent_failures(ip, now)
    if len(recent) < MAX_FAILED_LOGINS:
        return 0
    return int(LOCKOUT_SECONDS - (now - recent[0])) + 1

def record_failed_login(ip: str):
    _recent_failures(ip, time.time()).append(time.time())

def clear_failed_logins(ip: str):
    _failed_logins.pop(ip, None)

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
    try:
        await db.purge_expired_sessions()
    except Exception:
        pass  # housekeeping must never block a login
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
