"""VelocityBots paid API accounts, key management, usage metering and manual billing."""
import os, re, json, time, hmac, hashlib, secrets, logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

log = logging.getLogger("velocity_billing")
router = APIRouter()
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "velocitybots_api").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_BOOTSTRAP_TOKEN = os.getenv("ADMIN_BOOTSTRAP_TOKEN", "").strip()
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
LEGACY_API_KEY_ENABLED = os.getenv("LEGACY_API_KEY_ENABLED", "false").lower() == "true"
LEGACY_API_KEY = os.getenv("API_KEY", "").strip()
PAYMENT_UPI_ID = os.getenv("PAYMENT_UPI_ID", "").strip()
PAYMENT_ACCOUNT_NAME = os.getenv("PAYMENT_ACCOUNT_NAME", "VelocityBots").strip()
PAYMENT_QR_URL = os.getenv("PAYMENT_QR_URL", "").strip()
PAYMENT_INSTRUCTIONS = os.getenv("PAYMENT_INSTRUCTIONS", "Pay the exact amount, then submit your transaction reference. Access activates only after manual verification.").strip()
CURRENCY = "INR"
client = None
_db = None


def db():
    global client, _db
    if _db is not None:
        return _db
    if not MONGODB_URI:
        raise HTTPException(503, "Paid API database is not configured. Set MONGODB_URI.")
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
        client.admin.command("ping")
        _db = client[MONGODB_DATABASE]
        _db.users.create_index("email", unique=True)
        _db.api_keys.create_index("key_hash", unique=True)
        _db.payments.create_index("created_at")
        _db.usage.create_index([("user_id", 1), ("created_at", -1)])
        _db.payments.create_index([("method", 1), ("reference", 1)], unique=True)
        _db.sessions.create_index("token_hash", unique=True)
        _db.sessions.create_index("expires_at", expireAfterSeconds=0)
        return _db
    except HTTPException:
        raise
    except Exception:
        log.exception("MongoDB connection failed")
        raise HTTPException(503, "Could not connect to MongoDB. Check MONGODB_URI and database access.")


def utcnow():
    return datetime.now(timezone.utc)


def as_utc_datetime(value):
    """Normalize MongoDB datetimes (which may be naive) to aware UTC."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def key_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "$" + derived.hex()


def password_ok(password: str, stored: str) -> bool:
    try:
        salt_hex, digest = stored.split("$", 1)
        candidate = password_hash(password, bytes.fromhex(salt_hex)).split("$", 1)[1]
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False


def serializer():
    if len(SESSION_SECRET) < 32:
        raise HTTPException(503, "SESSION_SECRET must be set to a random secret of at least 32 characters.")
    return URLSafeTimedSerializer(SESSION_SECRET, salt="velocitybots-session-v1")


def create_session(user_id: str, response: Response):
    token = serializer().dumps({"uid": user_id, "nonce": secrets.token_urlsafe(16)})
    d = db()
    d.sessions.insert_one({"token_hash": key_hash(token), "user_id": user_id, "created_at": utcnow(), "expires_at": utcnow() + timedelta(days=7)})
    response.set_cookie("vb_session", token, max_age=7*24*3600, httponly=True, secure=os.getenv("COOKIE_SECURE", "true").lower() == "true", samesite="lax", path="/")


def current_user(request: Request):
    token = request.cookies.get("vb_session")
    if not token:
        raise HTTPException(401, "Please sign in first.")
    try:
        payload = serializer().loads(token, max_age=7*24*3600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Session expired. Please sign in again.")
    d = db()
    session = d.sessions.find_one({"token_hash": key_hash(token), "expires_at": {"$gt": utcnow()}})
    if not session:
        raise HTTPException(401, "Session expired. Please sign in again.")
    user = d.users.find_one({"_id": payload.get("uid"), "status": "active"})
    if not user:
        raise HTTPException(403, "Account is unavailable.")
    return user


def page_user(request: Request):
    try:
        return current_user(request)
    except HTTPException:
        return None


def public_user(user):
    active = user.get("subscription") or {}
    return {"id": str(user["_id"]), "email": user["email"], "role": user.get("role", "user"), "balance_paise": int(user.get("balance_paise", 0)), "currency": CURRENCY, "subscription": active, "status": user.get("status", "active"), "created_at": user.get("created_at", utcnow()).isoformat()}


PLANS = [
    {"id": "free", "name": "Free", "price_rupees": 0, "quota": 100, "video_quota": 25, "quota_period": "day", "days": 30, "description": "100 API requests/day · 25 video requests/day"},
    {"id": "basic", "name": "Basic", "price_rupees": 39, "quota": 2000, "quota_period": "day", "days": 30, "description": "2,000 API requests per day"},
    {"id": "starter", "name": "Starter", "price_rupees": 89, "quota": 5000, "quota_period": "day", "days": 30, "description": "5,000 API requests per day"},
    {"id": "standard", "name": "Standard", "price_rupees": 149, "quota": 10000, "quota_period": "day", "days": 30, "description": "10,000 API requests per day"},
    {"id": "pro", "name": "Pro", "price_rupees": 299, "quota": 25000, "quota_period": "day", "days": 30, "description": "25,000 API requests per day"},
    {"id": "business", "name": "Business", "price_rupees": 569, "quota": 50000, "quota_period": "day", "days": 30, "description": "50,000 API requests per day"},
    {"id": "superfast", "name": "Superfast", "price_rupees": 999, "quota": 100000, "quota_period": "day", "days": 30, "description": "100,000 API requests per day"},
]


class RegisterBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

class GoogleBody(BaseModel):
    credential: str = Field(min_length=20)

class PaymentBody(BaseModel):
    kind: str = Field(pattern="^(wallet|subscription)$")
    amount_rupees: int = Field(gt=0, le=100000)
    plan_id: Optional[str] = None
    method: str = Field(min_length=2, max_length=40)
    reference: str = Field(min_length=3, max_length=160)
    note: str = Field(default="", max_length=500)

class AdminPaymentBody(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    admin_note: str = Field(default="", max_length=500)

class AdminBootstrapBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    token: str = Field(min_length=20, max_length=256)


@router.get("/account", response_class=HTMLResponse)
async def account_page():
    return HTMLResponse(SITE_HTML)

@router.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(ADMIN_HTML)

@router.get("/api/billing/config")
async def billing_config():
    return {"brand": "VelocityBots API", "currency": CURRENCY, "plans": PLANS, "google_enabled": bool(GOOGLE_CLIENT_ID), "google_client_id": GOOGLE_CLIENT_ID, "manual_payments": True, "payment": {"upi_id": PAYMENT_UPI_ID, "account_name": PAYMENT_ACCOUNT_NAME, "qr_url": PAYMENT_QR_URL, "instructions": PAYMENT_INSTRUCTIONS}}

@router.post("/api/auth/register")
async def register(body: RegisterBody, response: Response):
    email = str(body.email).lower().strip()
    if ADMIN_EMAIL and email == ADMIN_EMAIL:
        raise HTTPException(403, "Owner email must be initialized through the protected owner bootstrap flow.")
    if len(body.password) < 10:
        raise HTTPException(400, "Password must be at least 10 characters.")
    d = db()
    user = {"_id": secrets.token_hex(16), "email": email, "password_hash": password_hash(body.password), "google_sub": None, "role": "user", "status": "active", "balance_paise": 0, "subscription": None, "created_at": utcnow()}
    try:
        d.users.insert_one(user)
    except DuplicateKeyError:
        raise HTTPException(409, "An account with this email already exists.")
    create_session(user["_id"], response)
    return {"ok": True, "user": public_user(user)}

@router.post("/api/auth/login")
async def login(body: LoginBody, response: Response):
    user = db().users.find_one({"email": str(body.email).lower().strip()})
    if not user or not user.get("password_hash") or not password_ok(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password.")
    if user.get("status") != "active":
        raise HTTPException(403, "Account is suspended.")
    create_session(user["_id"], response)
    return {"ok": True, "user": public_user(user)}

@router.post("/api/auth/google")
async def google_login(body: GoogleBody, response: Response):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google sign-in is not configured. Set GOOGLE_CLIENT_ID.")
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        info = id_token.verify_oauth2_token(body.credential, google_requests.Request(), GOOGLE_CLIENT_ID)
        email = str(info.get("email", "")).lower().strip()
        if not email or not info.get("email_verified") or not info.get("sub"):
            raise ValueError("Google email is not verified")
    except Exception:
        raise HTTPException(401, "Google sign-in token could not be verified.")
    d = db()
    user = d.users.find_one({"google_sub": info["sub"]}) or d.users.find_one({"email": email})
    if user:
        if user.get("status") != "active":
            raise HTTPException(403, "Account is suspended.")
        d.users.update_one({"_id": user["_id"]}, {"$set": {"google_sub": info["sub"]}})
        user["google_sub"] = info["sub"]
    else:
        user = {"_id": secrets.token_hex(16), "email": email, "password_hash": None, "google_sub": info["sub"], "role": "user", "status": "active", "balance_paise": 0, "subscription": None, "created_at": utcnow()}
        d.users.insert_one(user)
    create_session(user["_id"], response)
    return {"ok": True, "user": public_user(user)}

@router.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("vb_session")
    if token:
        db().sessions.delete_one({"token_hash": key_hash(token)})
    response.delete_cookie("vb_session", path="/")
    return {"ok": True}

@router.get("/api/me")
async def me(user=Depends(current_user)):
    return {"user": public_user(user)}

@router.post("/api/admin/bootstrap-account")
async def bootstrap_admin_account(body: AdminBootstrapBody, response: Response):
    """Create the initial owner account using a one-time deployment secret."""
    if not ADMIN_EMAIL or not ADMIN_BOOTSTRAP_TOKEN:
        raise HTTPException(503, "Owner bootstrap is not configured.")
    email = str(body.email).lower().strip()
    if email != ADMIN_EMAIL or not hmac.compare_digest(body.token, ADMIN_BOOTSTRAP_TOKEN):
        raise HTTPException(403, "Owner bootstrap denied.")
    d = db()
    if d.users.count_documents({"role": "admin"}) > 0:
        raise HTTPException(409, "An owner account already exists.")
    if d.users.find_one({"email": email}):
        raise HTTPException(409, "This email already has an account. Remove that user record in MongoDB before using the protected initial-owner setup.")
    user = {"_id": secrets.token_hex(16), "email": email, "password_hash": password_hash(body.password), "google_sub": None, "role": "admin", "status": "active", "balance_paise": 0, "subscription": None, "created_at": utcnow()}
    d.users.insert_one(user)
    create_session(user["_id"], response)
    return {"ok": True, "user": public_user(user), "message": "Owner account created. Remove ADMIN_BOOTSTRAP_TOKEN from deployment config now."}

@router.post("/api/plans/activate-free")
async def activate_free_plan(user=Depends(current_user)):
    """Activate the one-time 30-day free plan for this account."""
    d = db()
    if user.get("free_plan_claimed_at"):
        raise HTTPException(409, "The free plan has already been claimed on this account.")
    existing = user.get("subscription") or {}
    now = utcnow()
    if as_utc_datetime(existing.get("expires_at")) and as_utc_datetime(existing.get("expires_at")) > now:
        raise HTTPException(409, "You already have an active subscription.")
    plan = next(p for p in PLANS if p["id"] == "free")
    result = d.users.update_one(
        {"_id": user["_id"], "free_plan_claimed_at": {"$exists": False}},
        {"$set": {
            "free_plan_claimed_at": now,
            "subscription": {
                "plan_id": plan["id"], "plan_name": plan["name"],
                "quota": plan["quota"], "video_quota": plan.get("video_quota"),
                "quota_period": plan.get("quota_period", "day"),
                "period_start": now, "expires_at": now + timedelta(days=plan["days"]),
                "activated_at": now
            }
        }}
    )
    if not result.modified_count:
        raise HTTPException(409, "The free plan has already been claimed on this account.")
    return {"ok": True, "message": "Free plan activated for 30 days."}


@router.get("/api/keys")
async def list_keys(user=Depends(current_user)):
    rows = list(db().api_keys.find({"user_id": user["_id"], "revoked": False}, {"key_hash": 0}))
    return {"keys": [{"id": x["_id"], "label": x.get("label", "API key"), "prefix": x.get("prefix", "vb_****"), "created_at": x["created_at"].isoformat(), "last_used_at": x.get("last_used_at").isoformat() if x.get("last_used_at") else None} for x in rows]}

@router.post("/api/keys")
async def create_key(request: Request, user=Depends(current_user)):
    payload = await request.json()
    label = str(payload.get("label", "My API key")).strip()[:60] or "My API key"
    raw = "vb_" + secrets.token_urlsafe(32)
    d = db()
    if d.api_keys.count_documents({"user_id": user["_id"], "revoked": False}) >= 10:
        raise HTTPException(400, "Maximum 10 active API keys per account.")
    item = {"_id": secrets.token_hex(12), "user_id": user["_id"], "key_hash": key_hash(raw), "prefix": raw[:9] + "...", "label": label, "revoked": False, "created_at": utcnow(), "last_used_at": None}
    d.api_keys.insert_one(item)
    return {"ok": True, "key": raw, "id": item["_id"], "warning": "Copy this key now. The full key is not stored and cannot be shown again."}

@router.delete("/api/keys/{key_id}")
async def revoke_key(key_id: str, user=Depends(current_user)):
    result = db().api_keys.update_one({"_id": key_id, "user_id": user["_id"], "revoked": False}, {"$set": {"revoked": True, "revoked_at": utcnow()}})
    if not result.modified_count:
        raise HTTPException(404, "API key not found or already revoked.")
    return {"ok": True}

@router.get("/api/usage")
async def usage(user=Depends(current_user)):
    d = db()
    sub = user.get("subscription") or {}
    now = utcnow()
    if as_utc_datetime(sub.get("expires_at")) and as_utc_datetime(sub.get("expires_at")) > now:
        window_start = now - timedelta(days=1) if sub.get("quota_period") == "day" else sub.get("period_start", now - timedelta(days=30))
        used = d.usage.count_documents({"user_id": user["_id"], "billing_mode": "subscription", "created_at": {"$gte": window_start}})
        quota = int(sub.get("quota", 0))
    else:
        used, quota = 0, 0
    wallet_used = d.usage.count_documents({"user_id": user["_id"], "billing_mode": "wallet", "created_at": {"$gte": now - timedelta(days=30)}})
    return {"subscription": sub, "subscription_used": used, "subscription_remaining": max(0, quota-used), "wallet_balance_rupees": round(int(user.get("balance_paise", 0))/100, 2), "wallet_requests_last_30_days": wallet_used}

@router.get("/api/usage/recent")
async def recent_usage(user=Depends(current_user)):
    """Return the signed-in customer's recent API activity without exposing key material."""
    rows = list(db().usage.find({"user_id": user["_id"]}, {"_id": 0, "endpoint": 1, "method": 1, "billing_mode": 1, "created_at": 1}).sort("created_at", -1).limit(100))
    return {"activity": [{"endpoint": x.get("endpoint", ""), "method": x.get("method", "GET"), "billing_mode": x.get("billing_mode", ""), "created_at": x["created_at"].isoformat()} for x in rows]}

@router.post("/api/plans/purchase-wallet")
async def purchase_plan_from_wallet(payload: dict, user=Depends(current_user)):
    """Purchase a paid plan instantly from already-approved wallet funds."""
    d = db()
    plan_id = str(payload.get("plan_id", "")).strip()
    plan = next((p for p in PLANS if p["id"] == plan_id and p["price_rupees"] > 0), None)
    if not plan:
        raise HTTPException(400, "Choose a valid paid subscription plan.")
    now = utcnow()
    existing = user.get("subscription") or {}
    existing_expiry = as_utc_datetime(existing.get("expires_at"))
    if existing_expiry and existing_expiry > now:
        raise HTTPException(409, "You already have an active subscription. Wait until it expires before purchasing another plan.")
    cost_paise = int(plan["price_rupees"]) * 100
    # Atomic conditional deduction prevents spending more than the available wallet balance.
    debit = d.users.update_one(
        {"_id": user["_id"], "balance_paise": {"$gte": cost_paise}},
        {"$inc": {"balance_paise": -cost_paise}},
    )
    if not debit.modified_count:
        balance = int(user.get("balance_paise", 0))
        raise HTTPException(402, f"Insufficient wallet balance. This plan costs ₹{plan['price_rupees']}; your available balance is ₹{balance / 100:.2f}. Add wallet funds first.")
    subscription = {
        "plan_id": plan["id"], "plan_name": plan["name"],
        "quota": plan["quota"], "video_quota": plan.get("video_quota"),
        "quota_period": plan.get("quota_period", "day"),
        "period_start": now, "expires_at": now + timedelta(days=plan["days"]),
        "activated_at": now, "payment_source": "wallet",
    }
    try:
        d.users.update_one({"_id": user["_id"]}, {"$set": {"subscription": subscription}})
        d.payments.insert_one({
            "_id": secrets.token_hex(12), "user_id": user["_id"], "email": user["email"],
            "kind": "wallet_subscription", "amount_paise": cost_paise,
            "plan_id": plan["id"], "method": "Wallet", "reference": "wallet-purchase-" + secrets.token_hex(8),
            "note": "Automatically activated using wallet balance", "status": "approved",
            "created_at": now, "reviewed_at": now, "admin_note": "Instant wallet purchase",
        })
    except Exception:
        # Restore funds if activation/recording fails.
        d.users.update_one({"_id": user["_id"]}, {"$inc": {"balance_paise": cost_paise}})
        log.exception("Wallet subscription purchase failed")
        raise HTTPException(500, "Could not activate the plan. Your wallet funds were restored; please try again.")
    return {"ok": True, "message": f"{plan['name']} plan activated for {plan['days']} days.", "subscription": subscription}


@router.post("/api/payments")
async def request_payment(body: PaymentBody, user=Depends(current_user)):
    if body.kind == "subscription":
        raise HTTPException(400, "Paid plans must be purchased from your wallet. Add wallet funds and wait for owner approval only for the top-up.")
    if body.amount_rupees < 1:
        raise HTTPException(400, "Invalid amount.")
    plan = None
    if body.kind == "subscription":
        plan = next((p for p in PLANS if p["id"] == body.plan_id), None)
        if not plan:
            raise HTTPException(400, "Choose a valid subscription plan.")
        if body.amount_rupees != plan["price_rupees"]:
            raise HTTPException(400, "Amount does not match selected plan price.")
    if body.kind == "wallet" and body.plan_id:
        raise HTTPException(400, "Wallet top-ups do not use a plan_id.")
    item = {"_id": secrets.token_hex(12), "user_id": user["_id"], "email": user["email"], "kind": body.kind, "amount_paise": body.amount_rupees*100, "plan_id": plan["id"] if plan else None, "method": body.method.strip(), "reference": body.reference.strip(), "note": body.note.strip(), "status": "pending", "created_at": utcnow(), "reviewed_at": None, "admin_note": ""}
    try:
        db().payments.insert_one(item)
    except DuplicateKeyError:
        raise HTTPException(409, "This payment method/reference has already been submitted.")
    return {"ok": True, "payment_id": item["_id"], "status": "pending", "message": "Submitted for owner review. Credits or subscription activate only after approval."}

@router.get("/api/payments/mine")
async def my_payments(user=Depends(current_user)):
    items = list(db().payments.find({"user_id": user["_id"]}).sort("created_at", -1).limit(100))
    return {"payments": [{"id": p["_id"], "kind": p["kind"], "amount_rupees": p["amount_paise"]/100, "plan_id": p.get("plan_id"), "method": p["method"], "reference": p["reference"], "status": p["status"], "created_at": p["created_at"].isoformat(), "admin_note": p.get("admin_note", "")} for p in items]}

@router.get("/api/admin/summary")
async def admin_summary(user=Depends(current_user)):
    if user.get("role") != "admin": raise HTTPException(403, "Owner access required.")
    d = db()
    return {"users": d.users.count_documents({}), "pending_payments": d.payments.count_documents({"status": "pending"}), "approved_payments": d.payments.count_documents({"status": "approved"}), "api_keys": d.api_keys.count_documents({"revoked": False})}

@router.get("/api/admin/payments")
async def admin_payments(user=Depends(current_user)):
    if user.get("role") != "admin": raise HTTPException(403, "Owner access required.")
    rows = list(db().payments.find().sort("created_at", -1).limit(250))
    return {"payments": [{"id": p["_id"], "email": p["email"], "kind": p["kind"], "amount_rupees": p["amount_paise"]/100, "plan_id": p.get("plan_id"), "method": p["method"], "reference": p["reference"], "note": p.get("note", ""), "status": p["status"], "created_at": p["created_at"].isoformat(), "admin_note": p.get("admin_note", "")} for p in rows]}

@router.post("/api/admin/payments/{payment_id}/review")
async def review_payment(payment_id: str, body: AdminPaymentBody, user=Depends(current_user)):
    if user.get("role") != "admin": raise HTTPException(403, "Owner access required.")
    d = db()
    payment = d.payments.find_one_and_update({"_id": payment_id, "status": "pending"}, {"$set": {"status": "reviewing", "reviewed_by": user["_id"]}}, return_document=ReturnDocument.AFTER)
    if not payment: raise HTTPException(409, "Payment was already reviewed or does not exist.")
    try:
        if body.action == "approve":
            if payment["kind"] == "wallet":
                d.users.update_one({"_id": payment["user_id"]}, {"$inc": {"balance_paise": int(payment["amount_paise"])}})
            else:
                plan = next((p for p in PLANS if p["id"] == payment.get("plan_id")), None)
                if not plan: raise ValueError("Stored plan does not exist")
                now = utcnow()
                d.users.update_one({"_id": payment["user_id"]}, {"$set": {"subscription": {"plan_id": plan["id"], "plan_name": plan["name"], "quota": plan["quota"], "video_quota": plan.get("video_quota"), "quota_period": plan.get("quota_period", "day"), "period_start": now, "expires_at": now + timedelta(days=plan["days"]), "activated_at": now}}})
            final = "approved"
        else:
            final = "rejected"
        d.payments.update_one({"_id": payment_id, "status": "reviewing"}, {"$set": {"status": final, "reviewed_at": utcnow(), "admin_note": body.admin_note.strip()}})
        return {"ok": True, "status": final}
    except Exception:
        d.payments.update_one({"_id": payment_id, "status": "reviewing"}, {"$set": {"status": "pending"}})
        log.exception("Payment review failed")
        raise HTTPException(500, "Could not complete payment review.")

@router.get("/api/admin/usage")
async def admin_usage(user=Depends(current_user)):
    if user.get("role") != "admin": raise HTTPException(403, "Owner access required.")
    d = db()
    rows = list(d.usage.find({}, {"_id": 0, "user_id": 1, "endpoint": 1, "method": 1, "billing_mode": 1, "created_at": 1}).sort("created_at", -1).limit(200))
    ids = list({x.get("user_id") for x in rows if x.get("user_id")})
    users = {u["_id"]: u.get("email", "Unknown") for u in d.users.find({"_id": {"$in": ids}}, {"email": 1})}
    return {"total_requests": d.usage.count_documents({}), "last_24h": d.usage.count_documents({"created_at": {"$gte": utcnow()-timedelta(hours=24)}}), "activity": [{"email": users.get(x.get("user_id"), "Unknown"), "endpoint": x.get("endpoint", ""), "method": x.get("method", "GET"), "billing_mode": x.get("billing_mode", ""), "created_at": x["created_at"].isoformat()} for x in rows]}

@router.get("/api/admin/users")
async def admin_users(user=Depends(current_user)):
    if user.get("role") != "admin": raise HTTPException(403, "Owner access required.")
    rows = list(db().users.find().sort("created_at", -1).limit(500))
    return {"users": [{"id": u["_id"], "email": u["email"], "role": u.get("role", "user"), "status": u.get("status", "active"), "balance_rupees": int(u.get("balance_paise", 0))/100, "subscription": u.get("subscription")} for u in rows]}

@router.post("/api/admin/users/{user_id}/status")
async def admin_user_status(user_id: str, request: Request, user=Depends(current_user)):
    if user.get("role") != "admin": raise HTTPException(403, "Owner access required.")
    body = await request.json(); status = body.get("status")
    if status not in ("active", "suspended"): raise HTTPException(400, "status must be active or suspended")
    if user_id == user["_id"] and status == "suspended": raise HTTPException(400, "You cannot suspend your own account.")
    result = db().users.update_one({"_id": user_id}, {"$set": {"status": status}})
    if not result.matched_count: raise HTTPException(404, "User not found")
    return {"ok": True}


def require_api_key(request: Request, x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"), authorization: Optional[str] = Header(default=None), api_key: Optional[str] = Query(default=None, description="API key (legacy/query compatibility)")):
    supplied = (x_api_key or api_key or "").strip()
    if not supplied and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer": supplied = token.strip()
    if LEGACY_API_KEY_ENABLED and LEGACY_API_KEY and hmac.compare_digest(supplied, LEGACY_API_KEY):
        return {"user_id": "legacy-owner", "billing_mode": "legacy"}
    if not supplied:
        raise HTTPException(401, "Missing API key. Generate a personal key at /account.")
    d = db()
    record = d.api_keys.find_one({"key_hash": key_hash(supplied), "revoked": False})
    if not record:
        raise HTTPException(401, "Invalid or revoked API key.")
    user = d.users.find_one({"_id": record["user_id"], "status": "active"})
    if not user:
        raise HTTPException(403, "Account is suspended or unavailable.")
    now = utcnow()

    # Owner/admin API keys are exempt from subscriptions, quotas, and wallet charges.
    # Keep a usage record for analytics, but never decrement the owner's wallet.
    if user.get("role") == "admin":
        d.usage.insert_one({
            "user_id": user["_id"],
            "key_id": record["_id"],
            "billing_mode": "owner",
            "endpoint": request.url.path,
            "method": request.method,
            "media_type": "video" if request.url.path == "/download" and request.query_params.get("type", "audio").strip().lower() == "video" else None,
            "created_at": now,
        })
        d.api_keys.update_one({"_id": record["_id"]}, {"$set": {"last_used_at": now}})
        return {"user_id": user["_id"], "billing_mode": "owner"}

    sub = user.get("subscription") or {}
    expires_at = sub.get("expires_at")
    # MongoDB may return a naive datetime; treat it as UTC before comparing.
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    active_sub = bool(expires_at and expires_at > now)
    is_video_request = request.url.path == "/download" and request.query_params.get("type", "audio").strip().lower() == "video"
    if active_sub:
        period_start = now - timedelta(days=1) if sub.get("quota_period") == "day" else sub.get("period_start", now-timedelta(days=30))
        used = d.usage.count_documents({"user_id": user["_id"], "billing_mode": "subscription", "created_at": {"$gte": period_start}})
        quota = int(sub.get("quota", 0))
        if used < quota:
            mode = "subscription"
        elif int(user.get("balance_paise", 0)) >= 1:
            mode = "wallet"
        else:
            raise HTTPException(402, "Plan request quota exhausted for the current 24-hour period. Add wallet credits or wait for the quota to reset.")
        video_quota = sub.get("video_quota")
        if mode == "subscription" and is_video_request and video_quota is not None:
            video_used = d.usage.count_documents({"user_id": user["_id"], "media_type": "video", "created_at": {"$gte": now - timedelta(days=1)}})
            if video_used >= int(video_quota):
                if int(user.get("balance_paise", 0)) >= 1:
                    mode = "wallet"
                else:
                    raise HTTPException(402, "Your plan's video request quota is exhausted for the current 24-hour period.")
    elif int(user.get("balance_paise", 0)) >= 1:
        mode = "wallet"
    else:
        raise HTTPException(402, "No active subscription or wallet credits. Visit /account to purchase access.")
    if mode == "wallet":
        updated = d.users.find_one_and_update({"_id": user["_id"], "balance_paise": {"$gte": 1}}, {"$inc": {"balance_paise": -1}}, return_document=ReturnDocument.AFTER)
        if not updated:
            raise HTTPException(402, "Insufficient wallet balance.")
    d.usage.insert_one({"user_id": user["_id"], "key_id": record["_id"], "billing_mode": mode, "endpoint": request.url.path, "method": request.method, "media_type": "video" if is_video_request else None, "created_at": now})
    d.api_keys.update_one({"_id": record["_id"]}, {"$set": {"last_used_at": now}})
    return {"user_id": user["_id"], "billing_mode": mode}

SITE_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VelocityBots API</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#131b30;--line:#293650;--muted:#aab6cf;--blue:#6d8cff;--green:#54d6a0}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#17264a,#0b1020 60%);font:15px system-ui;color:#f5f7ff}header{display:flex;justify-content:space-between;align-items:center;padding:22px max(5%,calc((100% - 1100px)/2));border-bottom:1px solid #25314b}a{color:#9db4ff}button{cursor:pointer;border:0;border-radius:10px;padding:11px 15px;background:var(--blue);color:#fff;font-weight:700}button.secondary{background:#26334f}input,select,textarea{width:100%;padding:12px;border:1px solid var(--line);border-radius:9px;background:#0c1428;color:white;margin:6px 0 12px}main{max-width:1100px;margin:35px auto;padding:0 18px}.hero{padding:35px 0}.hero h1{font-size:clamp(32px,5vw,54px);margin:0 0 12px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:15px}.card{background:#121b30;border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:15px}.hidden{display:none}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.row>*{flex:1}.key{overflow-wrap:anywhere;background:#071022;padding:12px;border-radius:8px}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}.status{padding:4px 8px;border-radius:6px;background:#25334e}.price{font-size:30px;font-weight:800}.notice{padding:12px;background:#182846;border-radius:10px;margin:12px 0}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px;border-bottom:1px solid var(--line);font-size:13px}section{scroll-margin-top:20px}@media(max-width:600px){header{padding:16px}main{margin:10px auto}.hero{padding:22px 0}}
</style></head><body><header><b>◈ VelocityBots API</b><a href="/docs">API documentation ↗</a></header><main><div class="hero"><div class="muted">DEVELOPER PLATFORM · INR BILLING</div><h1>Your APIs.<br><span style="color:#91a7ff">Your keys. Your control.</span></h1><p class="muted">Manage API keys, subscriptions, wallet credits and request usage in one place.</p></div><div id="auth" class="grid"><div class="card"><h2>Create account</h2><form id="register"><label>Email</label><input name="email" type="email" required><label>Password (10+ characters)</label><input name="password" type="password" minlength="10" required><button>Create account</button></form></div><div class="card"><h2>Sign in</h2><form id="login"><label>Email</label><input name="email" type="email" required><label>Password</label><input name="password" type="password" required><button>Sign in</button></form><div id="googleBox" class="notice hidden">Google sign-in can be enabled by configuring GOOGLE_CLIENT_ID in your server environment.</div><div id="googleButton"></div></div></div><div id="app" class="hidden"><div class="row"><div><h2 id="welcome">Dashboard</h2><div class="muted" id="email"></div></div><button class="secondary" onclick="logout()">Sign out</button></div><div class="grid"><div class="card"><div class="muted">Wallet balance</div><div class="price" id="balance">₹0.00</div></div><div class="card"><div class="muted">Subscription</div><h3 id="sub">No active plan</h3><div id="quota" class="muted"></div></div><div class="card"><div class="muted">Requests used (last 24 hours)</div><div class="price" id="used">0</div></div></div><div class="tabs"><button onclick="show('keys')">API keys</button><button class="secondary" onclick="show('plans')">Plans & wallet</button><button class="secondary" onclick="show('payments')">Payments</button><button class="secondary" onclick="show('usage')">Usage & examples</button><button class="secondary hidden" id="adminLink" onclick="location.href='/admin'">Owner panel</button></div><div id="msg" class="notice hidden"></div><section id="keys" class="card"><h2>API keys</h2><p class="muted">Full key is shown only once. Store it securely; never expose it in browser-side public code.</p><form id="newKey" class="row"><input name="label" placeholder="Key label (e.g. Telegram bot)" maxlength="60"><button>Create API key</button></form><div id="newKeyResult"></div><div id="keyList"></div></section><section id="plans" class="hidden"><div class="card"><h2>Plans & wallet</h2><p class="muted">All plans last 30 days. Add wallet funds first; once the owner approves your top-up, paid plans activate instantly from your wallet without another approval.</p><div id="planList" class="grid"></div></div><div class="card"><h2>Payment instructions</h2><div id="paymentInfo" class="notice">Loading payment details…</div><p class="muted">Only pay to the account shown here. Verify the recipient name before sending money. Never share your UPI PIN or OTP.</p></div><div class="card"><h2>Add wallet credits</h2><form id="walletForm"><label>Amount (INR)</label><input name="amount_rupees" type="number" min="1" max="100000" required><label>Payment method</label><select name="method"><option>UPI</option><option>Bank transfer</option><option>Other</option></select><label>Transaction/reference ID</label><input name="reference" required minlength="3"><label>Optional note</label><textarea name="note"></textarea><button>Submit top-up for review</button></form></div></section><section id="payments" class="card hidden"><h2>Payment history</h2><div id="paymentList"></div></section><section id="usage" class="card hidden"><h2>API usage</h2><div class="grid"><div class="notice"><b>Recent API activity</b><div class="muted">Your latest authenticated requests</div></div><div class="notice"><b>Usage safety</b><div class="muted">Never publish your API key in public frontend code.</div></div></div><div id="recentUsage"></div><p>Send your key using <code>X-API-Key</code> or <code>Authorization: Bearer YOUR_KEY</code>. Query-string keys are retained for legacy client compatibility but headers are safer.</p><pre class="key">curl -H "X-API-Key: YOUR_API_KEY" "https://YOUR-API-HOST/search?q=artist%20song"

curl -H "X-API-Key: YOUR_API_KEY" "https://YOUR-API-HOST/download?url=VIDEO_ID&type=audio"</pre><p class="muted">Each authenticated API request consumes one quota unit or ₹0.01 from wallet credits. Wallet usage costs ₹0.01 per API request. Subscription requests use the plan quota first.</p></section></div><div id="toast" class="notice hidden" role="status"></div></main><script>
let me=null,plans=[];const $=id=>document.getElementById(id);function msg(s){$('toast').textContent=s;$('toast').classList.remove('hidden');setTimeout(()=>$('toast').classList.add('hidden'),6000)}async function api(path,opt={}){opt.headers={...(opt.headers||{}),'Content-Type':'application/json'};let r=await fetch(path,{credentials:'same-origin',...opt});let j=await r.json().catch(()=>({detail:r.statusText}));if(!r.ok)throw Error(j.detail||j.message||'Request failed');return j}function formObj(f){return Object.fromEntries(new FormData(f).entries())}async function boot(){try{let c=await api('/api/billing/config');plans=c.plans;renderPlans();renderPaymentInfo(c.payment||{});if(c.google_enabled){loadGoogle(c.google_client_id)}await refresh()}catch(e){msg(e.message)}}async function refresh(){try{let d=await api('/api/me');me=d.user;$('auth').classList.add('hidden');$('app').classList.remove('hidden');$('welcome').textContent='Dashboard';$('email').textContent=me.email;$('balance').textContent='₹'+(me.balance_paise/100).toFixed(2);$('adminLink').classList.toggle('hidden',me.role!=='admin');let u=await api('/api/usage');let s=u.subscription||{};$('sub').textContent=s.plan_name&&new Date(s.expires_at)>new Date()?s.plan_name:'No active plan';$('quota').textContent=s.expires_at?'Expires '+new Date(s.expires_at).toLocaleDateString():'';$('used').textContent=u.subscription_used+' / '+(s.quota||0);await loadKeys();await loadPayments();await loadRecentUsage()}catch(e){$('auth').classList.remove('hidden');$('app').classList.add('hidden')}}function show(id){['keys','plans','payments','usage'].forEach(x=>$(x).classList.toggle('hidden',x!==id));if(id==='usage')loadRecentUsage()}async function loadKeys(){let d=await api('/api/keys');$('keyList').innerHTML=d.keys.length?'<table><tr><th>Label</th><th>Key prefix</th><th>Created</th><th></th></tr>'+d.keys.map(k=>`<tr><td>${esc(k.label)}</td><td>${esc(k.prefix)}</td><td>${new Date(k.created_at).toLocaleDateString()}</td><td><button class="secondary" onclick="revoke('${k.id}')">Revoke</button></td></tr>`).join('')+'</table>':'<p class="muted">No API keys yet.</p>'}async function loadPayments(){let d=await api('/api/payments/mine');$('paymentList').innerHTML=d.payments.length?'<table><tr><th>Date</th><th>Type</th><th>Amount</th><th>Reference</th><th>Status</th><th>Note</th></tr>'+d.payments.map(p=>`<tr><td>${new Date(p.created_at).toLocaleDateString()}</td><td>${esc(p.kind)}</td><td>₹${p.amount_rupees}</td><td>${esc(p.reference)}</td><td>${esc(p.status)}</td><td>${esc(p.admin_note||'')}</td></tr>`).join('')+'</table>':'<p class="muted">No payment requests.</p>'}function renderPaymentInfo(p){let parts=[];parts.push('<b>Recipient:</b> '+esc(p.account_name||'VelocityBots'));if(p.upi_id)parts.push('<div><b>UPI ID:</b> <code id="upiText">'+esc(p.upi_id)+'</code> <button class="secondary" onclick="copyUpi()">Copy UPI ID</button></div>');else parts.push('<div class="muted">UPI ID is not configured yet. Owner: set PAYMENT_UPI_ID in Heroku Config Vars.</div>');if(p.qr_url)parts.push('<div style="margin-top:12px"><img src="'+esc(p.qr_url)+'" alt="Payment QR code" style="max-width:220px;width:100%;border-radius:12px;background:white;padding:8px"></div>');parts.push('<p>'+esc(p.instructions||'Pay the exact amount and submit your transaction reference.')+'</p>');$('paymentInfo').innerHTML=parts.join('')}async function copyUpi(){try{await navigator.clipboard.writeText($('upiText').textContent);msg('UPI ID copied')}catch(e){msg('Copy failed; select the UPI ID manually')}}async function loadRecentUsage(){try{let d=await api('/api/usage/recent');$('recentUsage').innerHTML=d.activity.length?'<table><tr><th>Time</th><th>Method</th><th>Endpoint</th><th>Billing</th></tr>'+d.activity.map(x=>`<tr><td>${new Date(x.created_at).toLocaleString()}</td><td>${esc(x.method)}</td><td>${esc(x.endpoint)}</td><td>${esc(x.billing_mode)}</td></tr>`).join('')+'</table>':'<p class="muted">No API requests recorded yet.</p>'}catch(e){$('recentUsage').textContent=e.message}}function loadGoogle(clientId){let s=document.createElement('script');s.src='https://accounts.google.com/gsi/client';s.async=true;s.defer=true;s.onload=()=>{google.accounts.id.initialize({client_id:clientId,callback:async x=>{try{await api('/api/auth/google',{method:'POST',body:JSON.stringify({credential:x.credential})});await refresh()}catch(e){msg(e.message)}}});google.accounts.id.renderButton($('googleButton'),{theme:'filled_blue',size:'large',shape:'pill',text:'continue_with',width:260})};document.head.appendChild(s)}function renderPlans(){$('planList').innerHTML=plans.map(p=>`<div class="card"><h3>${esc(p.name)}</h3><div class="price">₹${p.price_rupees}<small>/30 days</small></div><p class="muted">${esc(p.description)}</p><button onclick="buyPlan('${p.id}')">${p.price_rupees===0?'Activate free plan':'Choose '+esc(p.name)}</button></div>`).join('')}async function buyPlan(id){let p=plans.find(x=>x.id===id);if(!p)return;if(p.price_rupees===0){if(!confirm('Activate the 30-day free plan? It can only be claimed once per account.'))return;try{await api('/api/plans/activate-free',{method:'POST',body:JSON.stringify({})});msg('Free plan activated for 30 days.');await refresh()}catch(e){msg(e.message)}return;}if(!confirm('Buy '+p.name+' for ₹'+p.price_rupees+' from your wallet?'))return;try{let d=await api('/api/plans/purchase-wallet',{method:'POST',body:JSON.stringify({plan_id:id})});msg(d.message);await refresh();show('plans')}catch(e){msg(e.message)}}$('register').onsubmit=async e=>{e.preventDefault();try{await api('/api/auth/register',{method:'POST',body:JSON.stringify(formObj(e.target))});msg('Account created');await refresh()}catch(x){msg(x.message)}};$('login').onsubmit=async e=>{e.preventDefault();try{await api('/api/auth/login',{method:'POST',body:JSON.stringify(formObj(e.target))});await refresh()}catch(x){msg(x.message)}};$('newKey').onsubmit=async e=>{e.preventDefault();try{let d=await api('/api/keys',{method:'POST',body:JSON.stringify(formObj(e.target))});$('newKeyResult').innerHTML='<div class="notice"><b>Copy this key now — it will not be shown again.</b><div class="key" id="rawKey"></div><button onclick="copyKey()">Copy key</button></div>';$('rawKey').textContent=d.key;e.target.reset();await loadKeys()}catch(x){msg(x.message)}};async function copyKey(){await navigator.clipboard.writeText($('rawKey').textContent);msg('API key copied')}async function revoke(id){if(!confirm('Revoke this API key? It will stop working immediately.'))return;try{await api('/api/keys/'+id,{method:'DELETE'});await loadKeys();msg('Key revoked')}catch(e){msg(e.message)}}$('walletForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/payments',{method:'POST',body:JSON.stringify({...formObj(e.target),kind:'wallet',amount_rupees:Number(new FormData(e.target).get('amount_rupees'))})});e.target.reset();msg('Wallet top-up submitted for owner review. After approval, use the wallet balance to buy a plan instantly.');await loadPayments();show('payments')}catch(x){msg(x.message)}};async function logout(){await api('/api/auth/logout',{method:'POST'});location.reload()}function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}boot();
</script></body></html>'''

ADMIN_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VelocityBots Owner Panel</title><style>body{background:#0b1020;color:#f4f6ff;font:15px system-ui;margin:0}main{max-width:1200px;margin:30px auto;padding:15px}.card{background:#131b30;border:1px solid #293650;border-radius:14px;padding:18px;margin:15px 0}button{background:#6d8cff;color:white;border:0;border-radius:8px;padding:9px 12px;cursor:pointer;margin:3px}input{background:#0b1020;border:1px solid #293650;color:white;padding:10px;border-radius:7px}table{width:100%;border-collapse:collapse;overflow-wrap:anywhere}td,th{text-align:left;padding:9px;border-bottom:1px solid #293650;font-size:13px}.muted{color:#aab6cf}.stats{display:flex;gap:12px;flex-wrap:wrap}.stats div{background:#202d49;padding:15px;border-radius:10px;min-width:130px}a{color:#9db4ff}</style></head><body><main><a href="/account">← Customer dashboard</a><h1>VelocityBots · Owner panel</h1><p class="muted">Review manual payment requests, approve/reject payments, and suspend accounts.</p><div id="stats" class="stats"></div><div class="card"><h2>Pending & recent payments</h2><div id="payments">Loading…</div></div><div class="card"><h2>Accounts</h2><div id="users">Loading…</div></div><div class="card"><h2>API activity analytics</h2><div id="usageStats" class="stats"></div><div id="usageRows">Loading…</div></div><div id="msg"></div></main><script>const $=id=>document.getElementById(id);async function api(p,o={}){let r=await fetch(p,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(o.headers||{})},...o});let j=await r.json().catch(()=>({detail:r.statusText}));if(!r.ok)throw Error(j.detail||'Request failed');return j}function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}async function load(){try{let s=await api('/api/admin/summary');$('stats').innerHTML=[['Accounts',s.users],['Pending payments',s.pending_payments],['Approved payments',s.approved_payments],['Active API keys',s.api_keys]].map(x=>`<div><span class="muted">${x[0]}</span><h2>${x[1]}</h2></div>`).join('');let p=await api('/api/admin/payments');$('payments').innerHTML='<table><tr><th>Email</th><th>Type</th><th>Amount</th><th>Method / reference</th><th>Note</th><th>Status</th><th>Review</th></tr>'+p.payments.map(x=>`<tr><td>${esc(x.email)}</td><td>${esc(x.kind)} ${esc(x.plan_id||'')}</td><td>₹${x.amount_rupees}</td><td>${esc(x.method)}<br>${esc(x.reference)}</td><td>${esc(x.note)}</td><td>${esc(x.status)}</td><td>${x.status==='pending'?`<button onclick="review('${x.id}','approve')">Approve</button><button onclick="review('${x.id}','reject')">Reject</button>`:'—'}</td></tr>`).join('')+'</table>';let u=await api('/api/admin/users');$('users').innerHTML='<table><tr><th>Email</th><th>Role</th><th>Status</th><th>Wallet</th><th>Control</th></tr>'+u.users.map(x=>`<tr><td>${esc(x.email)}</td><td>${esc(x.role)}</td><td>${esc(x.status)}</td><td>₹${x.balance_rupees}</td><td>${x.role==='admin'?'Owner':`<button onclick="status('${x.id}','${x.status==='active'?'suspended':'active'}')">${x.status==='active'?'Suspend':'Reactivate'}</button>`}</td></tr>`).join('')+'</table>';let a=await api('/api/admin/usage');$('usageStats').innerHTML=[['All requests',a.total_requests],['Last 24 hours',a.last_24h]].map(x=>`<div><span class="muted">${x[0]}</span><h2>${x[1]}</h2></div>`).join('');$('usageRows').innerHTML=a.activity.length?'<table><tr><th>Time</th><th>Customer</th><th>Method</th><th>Endpoint</th><th>Billing</th></tr>'+a.activity.map(x=>`<tr><td>${new Date(x.created_at).toLocaleString()}</td><td>${esc(x.email)}</td><td>${esc(x.method)}</td><td>${esc(x.endpoint)}</td><td>${esc(x.billing_mode)}</td></tr>`).join('')+'</table>':'<p class="muted">No API requests yet.</p>'}catch(e){$('msg').textContent=e.message+' — sign in with your configured owner account.'}}async function review(id,action){let note=prompt('Optional admin note (and rejection reason):')||'';if(!confirm(action==='approve'?'Approve payment and activate credit/plan?':'Reject this payment?'))return;try{await api('/api/admin/payments/'+id+'/review',{method:'POST',body:JSON.stringify({action,admin_note:note})});await load()}catch(e){alert(e.message)}}async function status(id,status){if(!confirm('Set account status to '+status+'?'))return;try{await api('/api/admin/users/'+id+'/status',{method:'POST',body:JSON.stringify({status})});await load()}catch(e){alert(e.message)}}load();</script></main></body></html>'''
