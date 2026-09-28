#!/usr/bin/env python3
"""
MiniPix TOKEN EXTRACTOR BOT — Public Telegram Bot
==================================================
Koi bhi user Telegram se OTP login karke apna poora MiniPix
token/data JSON file me download kar sakta hai.

Commands:
  /start     → Welcome + instructions
  /login     → Start OTP login flow
  /cancel    → Cancel current operation
"""

import os
import sys
import io
import json
import time
import random
import uuid
import hashlib
import base64
import threading
from datetime import date, datetime
from typing import Dict, Any, Optional

try:
    import requests
except ImportError:
    sys.stdout.write("❌ 'requests' missing → pip install requests python-telegram-bot\n")
    sys.exit(1)

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
    from telegram.ext import (
        Application,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        ConversationHandler,
        ContextTypes,
        filters,
    )
except Exception as _err:
    sys.stdout.write(f"❌ python-telegram-bot missing: {_err}\n")
    sys.stdout.write("    → pip install python-telegram-bot==21.11\n")
    sys.exit(1)

# ───────────────────────── CONFIG ─────────────────────────
API_BASE      = "https://api.minipix.co/v4"
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
STATE_GC_SEC  = 10 * 60               # Auto-clean user state after 10 min

# Conversation states
WAIT_PHONE, WAIT_OTP = range(2)

# ───────────────────────── DEVICE CONSTANTS ───────────────
_OKHTTP_VERSIONS = [
    "okhttp/4.11.0", "okhttp/4.12.0", "okhttp/4.10.0",
    "okhttp/4.9.3",  "okhttp/4.9.2",  "okhttp/4.8.1", "okhttp/4.7.2",
]
_APP_VERSIONS  = ["332"]
_DEVICE_BRANDS = [
    "Xiaomi", "Xiaomi Redmi", "Xiaomi Poco", "Samsung", "OnePlus",
    "Realme", "OPPO", "Vivo", "Motorola", "Nokia",
    "Infinix", "Tecno", "iQOO", "Nothing", "Google Pixel",
]
_DEVICE_MODELS = {
    "Xiaomi":        ["Redmi Note 12","Redmi Note 11","Redmi Note 10","Mi 11 Lite","Redmi 12","Poco X5","Poco M6 Pro","Redmi Note 13"],
    "Xiaomi Redmi": ["Redmi Note 12 Pro","Redmi Note 11S","Redmi 10 Prime","Redmi A2 Plus","Redmi 12C"],
    "Xiaomi Poco":  ["Poco X5 Pro","Poco F5","Poco M6 Pro","Poco C65","Poco X6 Neo"],
    "Samsung":      ["Galaxy M34","Galaxy M14","Galaxy A14","Galaxy A24","Galaxy A34","Galaxy S21 FE","Galaxy F34"],
    "OnePlus":      ["OnePlus Nord CE 3","OnePlus Nord 2T","OnePlus 11R","OnePlus Nord CE 4"],
    "Realme":       ["Realme Narzo 60X","Realme 11X","Realme C55","Realme Narzo N55","Realme 12"],
    "OPPO":         ["OPPO A78","OPPO A58","OPPO F23","OPPO Reno 8T","OPPO K12x"],
    "Vivo":         ["Vivo Y36","Vivo Y27","Vivo T2x","Vivo V27e","Vivo Y100A"],
    "Motorola":     ["Moto G54","Moto G32","Moto Edge 40 Neo","Moto G14","Moto G62"],
    "Nokia":        ["Nokia G42","Nokia C32","Nokia G11 Plus","Nokia HMD Pulse+"],
    "Infinix":      ["Infinix HOT 30i","Infinix SMART 7","Infinix NOTE 30","Infinix ZERO 30"],
    "Tecno":        ["Tecno Spark 10","Tecno POP 7","Tecno POVA 5","Tecno CAMON 20"],
    "iQOO":         ["iQOO Z7 Lite","iQOO Z7s","iQOO Neo 7","iQOO Z9 Lite"],
    "Nothing":      ["Nothing Phone 2","Nothing Phone 1","Nothing Phone 2a"],
    "Google Pixel": ["Pixel 7a","Pixel 6a","Pixel 8","Pixel 7","Pixel 8a"],
}
_OS_VERSIONS = ["Android 13","Android 14","Android 12","Android 11","Android 15"]

# ───────────────────────── THREAD-SAFE USER STATE ─────────
_state_lock = threading.Lock()
_user_state: Dict[int, Dict[str, Any]] = {}

def _gc_state(uid: Optional[int] = None):
    now = time.time()
    with _state_lock:
        if uid is not None:
            if uid in _user_state:
                del _user_state[uid]
            return
        dead = [k for k, v in _user_state.items()
                if now - v.get("_ts", 0) > STATE_GC_SEC]
        for k in dead:
            del _user_state[k]

def _get_state(uid: int) -> Dict[str, Any]:
    _gc_state()
    with _state_lock:
        if uid not in _user_state:
            _user_state[uid] = {"_ts": time.time()}
        st = _user_state[uid]
        st["_ts"] = time.time()
        return st

def _set_state_field(uid: int, **fields):
    with _state_lock:
        if uid not in _user_state:
            _user_state[uid] = {"_ts": time.time()}
        _user_state[uid]["_ts"] = time.time()
        _user_state[uid].update(fields)

# ───────────────────────── HELPERS ────────────────────────
def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    sys.stdout.write(f"[{ts}] {msg}\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass

def _rand_hex(n):
    return "".join(random.choices("0123456789abcdef", k=n))

def generate_device_id():
    if random.random() < 0.3:
        return str(uuid.uuid4()).replace("-", "")[:16]
    if random.random() < 0.5:
        return _rand_hex(16)
    return hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[:16]

def generate_device_info():
    brand  = random.choice(_DEVICE_BRANDS)
    models = _DEVICE_MODELS.get(brand) or ["Generic Device"]
    model  = random.choice(models)
    os_ver = random.choice(_OS_VERSIONS)
    return f"{brand} {model}; {os_ver}"

def generate_headers():
    return {
        "user-agent":      random.choice(_OKHTTP_VERSIONS),
        "accept-encoding": "gzip",
        "x-app-version":   random.choice(_APP_VERSIONS),
    }

def _jitter(base_ms, amount=0.5, min_ms=5):
    base = base_ms / 1000.0
    half = base * amount
    lo = max(min_ms / 1000.0, base - half)
    return random.uniform(lo, base + half)

def _slp(base_ms):
    time.sleep(_jitter(base_ms, 0.6, 10))

def decode_jwt_payload(token):
    if not token:
        return {}
    t = token.strip()
    if t.lower().startswith("bearer "):
        t = t.split(None, 1)[1].strip()
    parts = t.split(".")
    if len(parts) < 2:
        return {}
    p = parts[1].replace("-", "+").replace("_", "/")
    rem = len(p) % 4
    if rem:
        p += "=" * (4 - rem)
    try:
        raw = base64.urlsafe_b64decode(p.encode("utf-8"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}

def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in str(raw or "").strip() if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 10:
        return "+91" + digits
    return "+" + digits

# ───────────────────────── MINIPIX API CLIENT ─────────────
class MiniPixClient:
    def __init__(self):
        self.session       = requests.Session()
        self.device_id     = generate_device_id()
        self.device_info   = generate_device_info()
        self.phone         = None
        self.access_token  = None
        self.refresh_token = None
        self.quiz_tokens   = None
        self.user_id       = None
        self.profile_id    = None
        self.device_frozen = False
        self._reset_headers()

    def _reset_headers(self):
        for k in list(self.session.headers.keys()):
            del self.session.headers[k]
        self.session.headers.update(generate_headers())

    def _integrity_stub(self):
        raw = (f"{self.device_id}:{self.user_id or 'u'}:"
               f"{int(time.time())}:{random.random()}")
        return (hashlib.sha256(raw.encode()).hexdigest()
                + "."
                + hashlib.md5(raw[::-1].encode()).hexdigest())

    def _req(self, method, path, **kw):
        kw.setdefault("timeout", 15)
        url = f"{API_BASE}{path}"
        try:
            r = self.session.request(method, url, **kw)
        except Exception as e:
            return 0, str(e)
        ct = r.headers.get("content-type", "")
        try:
            if "application/json" in ct:
                data = r.json()
            else:
                data = r.text
        except Exception:
            data = r.text[:1000]
        return r.status_code, data

    def generate_otp(self, phone):
        self.phone = phone
        self.device_id   = generate_device_id()
        self.device_info = generate_device_info()
        self._reset_headers()
        _slp(random.randint(150, 450))

        payload = {"phone_number": phone}
        hdrs = {
            "content-type":        "application/json; charset=utf-8",
            "x-device-id":         self.device_id,
            "x-minipix-integrity": self._integrity_stub(),
        }
        sc, d = self._req(
            "POST", "/login/generate-otp",
            headers=hdrs,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 403 and isinstance(d, dict) and d.get("code") == "DEVICE_INTEGRITY_REQUIRED":
            hdrs["x-minipix-integrity-error"] = "ERR_8000"
            sc, d = self._req(
                "POST", "/login/generate-otp",
                headers=hdrs,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
        if sc == 200 and isinstance(d, dict):
            ok = d.get("message") == "OTP sent" or d.get("success")
            if ok:
                return d.get("session_token") or d.get("sessionToken"), None
            msg = d.get("message") or d.get("error") or "Unknown error"
            return None, msg
        return None, f"HTTP {sc}"

    def verify_otp(self, session_token, otp):
        _slp(random.randint(600, 1600))
        payload = {
            "client_id":     "android",
            "device_id":     self.device_id,
            "device_info":   self.device_info,
            "otp":           otp,
            "phone_number":  self.phone,
            "session_token": session_token,
        }
        hdrs = {
            "content-type":        "application/json; charset=utf-8",
            "x-device-id":         self.device_id,
            "x-minipix-integrity": self._integrity_stub(),
        }
        sc, d = self._req(
            "POST", "/login/verify-otp",
            headers=hdrs,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if not (sc == 200 and isinstance(d, dict) and d.get("access_token")):
            msg = None
            if isinstance(d, dict):
                msg = d.get("message") or d.get("error")
            return False, msg or f"HTTP {sc}", None

        self.access_token  = d["access_token"]
        self.refresh_token = d.get("refresh_token")
        self.quiz_tokens   = d.get("quiz_tokens") or d.get("quizSessionTokens")
        self.user_id       = d.get("id") or d.get("_id")
        self.profile_id    = d.get("masterProfile") or d.get("master_profile")

        jwt = decode_jwt_payload(self.access_token)
        if isinstance(jwt, dict):
            if jwt.get("nonce"):
                self.device_id     = str(jwt["nonce"])
                self.device_frozen = True
            if not self.user_id:
                self.user_id = jwt.get("id") or jwt.get("userId") or jwt.get("sub")
            if not self.profile_id:
                self.profile_id = jwt.get("masterProfile")
            if not self.phone:
                self.phone = jwt.get("mobile") or jwt.get("phone")

        self.session.headers["authorization"] = f"Bearer {self.access_token}"
        return True, None, d

    def build_json_dump(self, raw_verify_resp=None):
        jwt = decode_jwt_payload(self.access_token or "")
        out = {
            "telegram_bot": "minipix_token_extractor",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "phone":        self.phone,
            "user_id":      self.user_id,
            "profile_id":   self.profile_id,
            "device_id":    self.device_id,
            "device_info":  self.device_info,
            "tokens": {
                "access_token":  self.access_token  or "",
                "refresh_token": self.refresh_token or "",
                "quiz_tokens":   self.quiz_tokens,
            },
            "jwt_payload": jwt if jwt else None,
        }
        return out

# ───────────────────────── TELEGRAM BOT HANDLERS ──────────
def _keyboard_cancel():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel Login", callback_data="bot_cancel"),
    ]])

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    txt = (
        f"👋 Namaste {u.first_name}!\n\n"
        f"🎮 Yeh MiniPix Token Extractor Bot hai.\n"
        f"Koi bhi user OTP login karke apna poora token data\n"
        f"JSON file me download kar sakta hai.\n\n"
        f"✅ Steps:\n"
        f"  1. Send /login\n"
        f"  2. Apna phone number daalein (+91...)\n"
        f"  3. OTP aayega -> OTP daalein\n"
        f"  4. Verify ho jayega -> JSON file bhej di jayegi\n\n"
        f"⚠️ Security: Tokens apne paas hi rakhein.\n"
        f"/login se shuru karein — /cancel se cancel."
    )
    await update.message.reply_text(txt, reply_markup=_keyboard_cancel())

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    _gc_state(uid)
    if getattr(update, "message", None):
        await update.message.reply_text("✅ Cancelled. /login se phir se shuru karein.")
    elif getattr(update, "callback_query", None):
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "✅ Cancelled. /login se phir se shuru karein."
        )
    return ConversationHandler.END

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    _set_state_field(uid, client=MiniPixClient(), stage="phone")
    txt = ("📱 Step 1/2 — Phone Number\n\n"
           "Apna MiniPix registered mobile number daalein:\n"
           "  • 10-digit (India): 98XXXXXX11\n"
           "  • Full format:      +9198XXXXXX11")
    await update.message.reply_text(txt, reply_markup=_keyboard_cancel())
    return WAIT_PHONE

async def _send_generic_err(update: Update, text: str):
    m = getattr(update, "message", None) or (
        getattr(update, "callback_query", None) and update.callback_query.message
    )
    if m is not None:
        await m.reply_text(text, reply_markup=_keyboard_cancel())

async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    st  = _get_state(uid)
    client: Optional[MiniPixClient] = st.get("client")
    if client is None:
        await _send_generic_err(update, "❌ State expired. /login se phir se shuru karein.")
        return ConversationHandler.END

    phone_in = (update.message.text or "").strip()
    phone = normalize_phone(phone_in)
    if not phone:
        await update.message.reply_text("❌ Invalid phone number. Phir se daalein:",
                                        reply_markup=_keyboard_cancel())
        return WAIT_PHONE

    _log(f"[user:{uid}] generate_otp -> phone={phone}")
    await update.message.reply_text(f"📡 Generating OTP for {phone}...")
    session_tok, err = client.generate_otp(phone)
    if session_tok is None:
        txt = f"❌ OTP Generate FAIL:\n{err or 'Unknown'}\n\n/cancel karein ya phir naya number daalein."
        await update.message.reply_text(txt, reply_markup=_keyboard_cancel())
        return WAIT_PHONE

    _set_state_field(uid, stage="otp", session_token=session_tok, phone=phone, otp_attempts=0)
    txt = f"✅ OTP send ho gaya! Registered mobile {phone} par check karein.\n\nStep 2/2 — Enter 6-digit OTP:"
    await update.message.reply_text(txt, reply_markup=_keyboard_cancel())
    return WAIT_OTP

async def otp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    st  = _get_state(uid)
    client: Optional[MiniPixClient] = st.get("client")
    sess  = st.get("session_token")
    phone = st.get("phone")
    if client is None or not sess:
        await _send_generic_err(update, "❌ State expired. /login se phir se shuru karein.")
        return ConversationHandler.END

    otp = "".join(ch for ch in (update.message.text or "") if ch.isdigit())
    if len(otp) < 4:
        await update.message.reply_text("❌ OTP invalid (4+ digits chahiye). Phir se daalein:", reply_markup=_keyboard_cancel())
        return WAIT_OTP

    attempts = st.get("otp_attempts", 0) + 1
    sent = await update.message.reply_text("🔐 Verifying OTP & extracting tokens...", reply_markup=_keyboard_cancel())

    ok, err, raw = client.verify_otp(sess, otp)
    if not ok:
        _set_state_field(uid, otp_attempts=attempts)
        if attempts < 3:
            txt = f"❌ OTP Verify FAIL:\n{err or 'Unknown'}\n\nPhir se 6-digit OTP daalein (attempt {attempts}/3):"
            await sent.edit_text(txt, reply_markup=_keyboard_cancel())
            return WAIT_OTP
        txt = f"❌ 3 baar galat OTP. Flow cancel.\n{err}\n\n/login se naya session shuru karein."
        try:
            await sent.edit_text(txt, reply_markup=None)
        except Exception:
            await update.message.reply_text(txt)
        _gc_state(uid)
        return ConversationHandler.END

    dump = client.build_json_dump(raw_verify_resp=raw)
    fname_base = "".join(ch for ch in (phone or str(uid)) if ch.isalnum())
    fname = f"minipix_tokens_{fname_base}.json"
    payload_bytes = json.dumps(dump, indent=2, ensure_ascii=False).encode("utf-8")
    bio = io.BytesIO(payload_bytes)
    bio.name = fname

    tg_user = update.effective_user
    caption = (
        f"✅ TOKEN EXTRACT HO GAYA!\n\n"
        f"👤 Telegram: @{tg_user.username or f'{tg_user.id}'}\n"
        f"📱 Phone     : {dump['phone']}\n"
        f"🪪 user_id   : {dump['user_id'] or '?'}\n"
        f"🧭 profile_id: {dump['profile_id'] or '?'}\n"
        f"📱 device_id : {dump['device_id']}\n"
    )
    try:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=InputFile(bio, filename=fname),
            caption=caption,
        )
    except Exception as e:
        err_txt = f"⚠️ File send failed ({e})."
        try:
            await sent.edit_text(err_txt)
        except Exception:
            await update.message.reply_text(err_txt)
    else:
        try:
            await sent.delete()
        except Exception:
            pass
    finally:
        bio.close()

    _gc_state(uid)
    return ConversationHandler.END

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    _gc_state(uid)
    try:
        await q.edit_message_text("✅ Login Cancelled. /login se phir se shuru karein.", reply_markup=None)
    except Exception:
        pass
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log(f"ERROR: {context.error!r}")

def build_application(token: str):
    app = Application.builder().token(token).build()
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_cmd)],
        states={
            WAIT_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, phone_handler),
            ],
            WAIT_OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, otp_handler),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CallbackQueryHandler(cancel_callback, pattern=r"^bot_cancel$"),
        ],
        allow_reentry=True,
        conversation_timeout=600,
        per_message=True,
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^bot_cancel$"))
    app.add_handler(login_conv)
    app.add_error_handler(error_handler)
    return app

def main():
    if not BOT_TOKEN:
        sys.stdout.write("❌ TELEGRAM_BOT_TOKEN env var missing.\n")
        sys.exit(2)

    _log("Bot starting...")
    app = build_application(BOT_TOKEN)
    _log("Polling started — press Ctrl+C to stop.")
    try:
        app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        _log("Interrupted — shutting down.")

if __name__ == "__main__":
    main()
