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

Flow:
  /login → Phone → Generate OTP → Enter OTP → Verify →
  → Send JSON file (minipix_tokens_<phone>.json)

Setup:
  export TELEGRAM_BOT_TOKEN="123456789:ABCdef..."
  python token_bot.py
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
import argparse
from datetime import date, datetime
from typing import Dict, Any, Optional

try:
    import requests
except ImportError:
    sys.stdout.write("❌ 'requests' missing → pip install requests python-telegram-bot\n")
    sys.exit(1)

try:
    from curl_cffi import requests as curl_requests  # type: ignore
    HAS_CURL_CFFI = True
except Exception:
    HAS_CURL_CFFI = False
    curl_requests = None

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
    from telegram.ext import (
        Application,
        CommandHandler,
        MessageHandler,
        ConversationHandler,
        ContextTypes,
        CallbackQueryHandler,
        filters,
    )
except Exception as _err:
    sys.stdout.write(f"❌ python-telegram-bot missing: {_err}\n")
    sys.stdout.write("   → pip install python-telegram-bot==21.11\n")
    sys.exit(1)

# ───────────────────────── CONFIG ─────────────────────────
API_BASE      = "https://api.minipix.co/v4"
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
STATE_GC_SEC  = 10 * 60              # Auto-clean user state after 10 min

# Conversation states
WAIT_PHONE, WAIT_OTP = range(2)

# ───────────────────────── DEVICE CONSTANTS ───────────────
# ⚠️ CAPTURE-GROUNDED VALUES — THESE ARE NOT RANDOM!
# From 103 captures (mixpix_API_capture_data.txt L226 / L211-214):
#   x-app-version = 328   in ALL 103 captures (STATIC)
#   user-agent    = okhttp/4.12.0   in ALL 94 logged occurrences
#   accept-encoding = gzip
_APP_VERSION      = "328"
_UA_OKHTTP        = "okhttp/4.12.0"
_ACCEPT_ENCODING  = "gzip"

# Only used in POST body (device_id/device_info) — NOT in pre-login headers!
_DEVICE_BRANDS = [
    "Xiaomi", "Xiaomi Redmi", "Xiaomi Poco", "Samsung", "OnePlus",
    "Realme", "OPPO", "Vivo", "Motorola", "Nokia",
    "Infinix", "Tecno", "iQOO", "Nothing", "Google Pixel",
]
_DEVICE_MODELS = {
    "Xiaomi":       ["Redmi Note 12","Redmi Note 11","Redmi Note 10","Mi 11 Lite","Redmi 12","Poco X5","Poco M6 Pro","Redmi Note 13"],
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
_user_state: Dict[int, Dict[str, Any]] = {}     # key = telegram_user_id

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
def _p(msg: str = ""):
    try:
        sys.stdout.write(str(msg) + "\n")
        sys.stdout.flush()
    except Exception:
        try:
            sys.stdout.buffer.write((str(msg) + "\n").encode("utf-8", errors="replace"))
            sys.stdout.buffer.flush()
        except Exception:
            pass

def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    _p(f"[{ts}] {msg}")

def _print_section(title: str):
    bar = "=" * 70
    _p(f"\n{bar}")
    _p(f"  {title}")
    _p(bar)

def _rand_hex(n):
    return "".join(random.choices("0123456789abcdef", k=n))

def generate_device_id():
    if random.random() < 0.3:
        return str(uuid.uuid4()).replace("-", "")[:16]
    if random.random() < 0.5:
        return _rand_hex(16)
    if random.random() < 0.6:
        return hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[:16]
    return hashlib.sha1(str(random.random()).encode()).hexdigest()[:16]

def generate_device_info():
    brand  = random.choice(_DEVICE_BRANDS)
    models = _DEVICE_MODELS.get(brand) or ["Generic Device"]
    model  = random.choice(models)
    os_ver = random.choice(_OS_VERSIONS)
    sep    = random.choice(["; ", " | ", "/", "__"])
    return random.choice([
        f"{brand} {model}{sep}{os_ver}",
        f"{model}{sep}{os_ver}",
        f"{brand}/{model}/{os_ver}",
        f"{os_ver} {brand} {model}",
        f"{model} {os_ver}",
    ])

def generate_headers():
    """Session-level BASE headers — matches capture 1:1.
    NO x-device-id / NO x-minipix-integrity here — those are per-call only
    on integrity-gated endpoints (OTP, quiz, attest)."""
    return {
        "user-agent":      _UA_OKHTTP,
        "accept-encoding": _ACCEPT_ENCODING,
        "x-app-version":   _APP_VERSION,
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
        try:
            raw = base64.b64decode(p.encode("utf-8"))
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in str(raw or "").strip() if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 10:
        return "+91" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if not digits.startswith("0") and not raw.startswith("+"):
        return "+" + digits
    return "+" + digits

# ───────────────────────── MINIPIX API CLIENT ─────────────
class MiniPixClient:
    def __init__(self, verbose: bool = False, use_curl_cffi: Optional[bool] = None):
        self.verbose       = verbose
        self._last_req_log = None
        # curl_cffi = better TLS/JA3 fingerprint (bypasses WAF blocks on python-requests)
        if use_curl_cffi is None:
            self.use_curl = HAS_CURL_CFFI
        else:
            self.use_curl = bool(use_curl_cffi and HAS_CURL_CFFI)
        if self.use_curl:
            self.session = curl_requests.Session(impersonate="chrome124")
            # For android-app-like JA3: impersonate="chrome120" on Android or "chrome124"
        else:
            self.session = requests.Session()
        # Device state
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
        if self.verbose:
            tls = "curl_cffi(JA3=chrome124)" if self.use_curl else "python-requests(standard)"
            _p(f"[INIT] HTTP engine  = {tls}")
            _p(f"[INIT] device_id    = {self.device_id}")
            _p(f"[INIT] device_info  = {self.device_info}")
            _p(f"[INIT] session hdrs = {dict(self.session.headers)}")
            if not self.use_curl and HAS_CURL_CFFI is False:
                _p("[INIT] 💡 Tip: pip install curl_cffi → better TLS JA3 bypass for AWS WAF 403")

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
        extra_hdrs = kw.get("headers") or {}
        data = kw.get("data")
        if self.verbose:
            _p()
            _p(f"─── REQUEST ──────────────────────────────────────────")
            _p(f"  METHOD : {method}")
            _p(f"  URL    : {url}")
            merged_hdrs = dict(self.session.headers)
            merged_hdrs.update(extra_hdrs)
            _p(f"  HEADERS: {json.dumps(merged_hdrs, indent=2, ensure_ascii=False)}")
            if data is not None:
                if isinstance(data, (bytes, bytearray)):
                    try:
                        _p(f"  BODY   : {data.decode('utf-8', errors='replace')}")
                    except Exception:
                        _p(f"  BODY   : <{len(data)} bytes>")
                else:
                    _p(f"  BODY   : {data!r}")
            _p(f"──────────────────────────────────────────────────────")
        try:
            r = self.session.request(method, url, **kw)
        except Exception as e:
            if self.verbose:
                _p(f"  ❌ EXCEPTION: {e!r}")
            return 0, str(e)
        ct = r.headers.get("content-type", "")
        try:
            if "application/json" in ct:
                data = r.json()
            else:
                t = r.text
                try:
                    data = json.loads(t)
                except Exception:
                    data = t
        except Exception:
            data = r.text[:1000]
        if self.verbose:
            _p(f"─── RESPONSE ─────────────────────────────────────────")
            _p(f"  STATUS : {r.status_code}")
            resp_hdrs = dict(r.headers)
            _print_keys = ["content-type", "www-authenticate", "x-request-id",
                           "x-ratelimit-remaining", "x-error-code"]
            shown = {k: resp_hdrs[k] for k in _print_keys if k in resp_hdrs}
            if shown:
                _p(f"  HEADERS: {json.dumps(shown, indent=2, ensure_ascii=False)}")
            if isinstance(data, dict):
                _p(f"  BODY   : {json.dumps(data, indent=2, ensure_ascii=False)}")
            else:
                s = str(data)
                if len(s) > 1500:
                    s = s[:1500] + "… [TRUNCATED]"
                _p(f"  BODY   : {s}")
            if r.status_code == 403:
                _p(f"  ⚠️  403 FORBIDDEN — Device integrity / auth issue")
            _p(f"──────────────────────────────────────────────────────")
        self._last_req_log = {"status": r.status_code, "data": data}
        return r.status_code, data

    def _login_header_strategies(self, include_integrity=False):
        """Generator of header permutations for login endpoints (gen-otp / verify-otp).
        Ordered: capture-grounded minimal FIRST (most likely to pass WAF), then
        progressively add headers if 403/empty.
        Capture #103 (gen-otp) & #102 (verify-otp) had NO x-device-id, NO x-minipix-integrity,
        NO x-client-id — just 5 base headers. So strategy #1 = exactly that."""
        base_ct = "application/json; charset=utf-8"
        # ---- Strategy 1: EXACT capture headers (NO extras) — BEST CHANCE ----
        s1 = {
            "content-type": base_ct,
            # NOTE: no x-device-id, no x-minipix-integrity, no x-client-id here!
        }
        yield ("CAPTURE_MINIMAL_v1", s1)
        # ---- Strategy 2: same but include device_id header (x-device-id) ----
        s2 = dict(s1)
        s2["x-device-id"] = self.device_id
        yield ("ADD_X_DEVICE_ID_v2", s2)
        # ---- Strategy 3: + integrity stub (old behaviour) ----
        s3 = dict(s2)
        if include_integrity:
            s3["x-minipix-integrity"] = self._integrity_stub()
        yield ("ADD_INTEGRITY_STUB_v3", s3)
        # ---- Strategy 4: + integrity-error: ERR_8000 (pre-emptive) ----
        s4 = dict(s3)
        if include_integrity:
            s4["x-minipix-integrity-error"] = "ERR_8000"
        yield ("PRE_EMPTIVE_ERR_8000_v4", s4)
        # ---- Strategy 5: add x-client-id: android ----
        s5 = dict(s4)
        s5["x-client-id"] = "android"
        yield ("ADD_X_CLIENT_ANDROID_v5", s5)
        # ---- Strategy 6: try newer app version (might be geo/version dependent) ----
        s6 = dict(s5)
        s6["x-app-version"] = "332"
        s6["user-agent"]   = "okhttp/4.12.0"
        yield ("APP_VERSION_332_v6", s6)
        # ---- Strategy 7: ERR_4000 variant (from device_register.py original) ----
        s7 = dict(s5)
        if include_integrity:
            s7["x-minipix-integrity-error"] = "ERR_4000"
        s7["x-client-id"] = "minipix_quiz"
        s7["x-app-version"] = "3"
        yield ("MINIPIX_QUIZ_ERR4000_v7", s7)

    def generate_otp(self, phone):
        self.phone = phone
        self.device_id   = generate_device_id()
        self.device_info = generate_device_info()
        self._reset_headers()
        if self.verbose:
            _p(f"\n[GEN-OTP] New device generated for phone={phone}")
            _p(f"[GEN-OTP] device_id={self.device_id}  device_info={self.device_info}")
        _slp(random.randint(150, 450))

        payload = {"phone_number": phone}
        last_err = None
        last_sc = 0

        for idx, (label, hdrs) in enumerate(self._login_header_strategies(include_integrity=True), 1):
            if self.verbose:
                _p(f"\n▶️  [ATTEMPT {idx}/7] generate-otp strategy → {label}")
                merged = dict(self.session.headers)
                merged.update(hdrs)
                _p(f"   headers = {json.dumps(merged, indent=2, ensure_ascii=False)}")
            _slp(random.randint(200, 600))

            sc, d = self._req(
                "POST", "/login/generate-otp",
                headers=hdrs,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
            last_sc = sc
            last_err = d

            if sc == 403 and isinstance(d, dict) and d.get("code") == "DEVICE_INTEGRITY_REQUIRED":
                if self.verbose:
                    _p(f"\n🔁 [FALLBACK] 403 DEVICE_INTEGRITY_REQUIRED → adding ERR_8000, retrying same strategy once")
                hdrs_retry = dict(hdrs)
                hdrs_retry["x-minipix-integrity-error"] = "ERR_8000"
                _slp(random.randint(200, 500))
                sc, d = self._req(
                    "POST", "/login/generate-otp",
                    headers=hdrs_retry,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                )
                last_sc = sc
                last_err = d

            # Success check
            if sc == 200 and isinstance(d, dict):
                ok = d.get("message") == "OTP sent" or d.get("success")
                if ok:
                    if self.verbose:
                        _p(f"\n✅ [GEN-OTP OK] Strategy '{label}' WORKED on attempt {idx}!")
                    return d.get("session_token") or d.get("sessionToken"), None
                msg = d.get("message") or d.get("error") or "Unknown error"
                last_err = f"{msg} | {json.dumps(d, ensure_ascii=False)[:400]}"

            # Non-403 terminal-ish status — stop wasting retries? (400 = bad phone, 429 = rate limit)
            if sc in (400, 422) and isinstance(d, dict):
                if self.verbose:
                    _p(f"   ⏹️  Stopping retries — {sc} is client-input error (not WAF/integrity)")
                break
            if sc == 429:
                if self.verbose:
                    _p(f"   ⏹️  Stopping retries — 429 RATE LIMIT hit")
                break

        # ---- All attempts failed ----
        if isinstance(last_err, dict):
            msg = last_err.get("message") or last_err.get("error") or f"HTTP {last_sc}"
            return None, f"{msg} | {json.dumps(last_err, ensure_ascii=False)[:400]}"
        return None, f"HTTP {last_sc}: {str(last_err)[:300]}"

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
        last_err = None
        last_sc = 0
        raw_resp = None

        for idx, (label, hdrs) in enumerate(self._login_header_strategies(include_integrity=True), 1):
            if self.verbose:
                _p(f"\n▶️  [ATTEMPT {idx}/7] verify-otp strategy → {label}")
                merged = dict(self.session.headers)
                merged.update(hdrs)
                _p(f"   headers = {json.dumps(merged, indent=2, ensure_ascii=False)}")
            _slp(random.randint(200, 600))

            sc, d = self._req(
                "POST", "/login/verify-otp",
                headers=hdrs,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
            last_sc = sc
            last_err = d
            raw_resp = d

            if sc == 403 and isinstance(d, dict) and d.get("code") == "DEVICE_INTEGRITY_REQUIRED":
                if self.verbose:
                    _p(f"\n🔁 [FALLBACK] 403 DEVICE_INTEGRITY_REQUIRED → adding ERR_8000, retrying same strategy once")
                hdrs_retry = dict(hdrs)
                hdrs_retry["x-minipix-integrity-error"] = "ERR_8000"
                _slp(random.randint(200, 500))
                sc, d = self._req(
                    "POST", "/login/verify-otp",
                    headers=hdrs_retry,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                )
                last_sc = sc
                last_err = d
                raw_resp = d

            if sc == 200 and isinstance(d, dict) and d.get("access_token"):
                if self.verbose:
                    _p(f"\n✅ [VERIFY-OTP OK] Strategy '{label}' WORKED on attempt {idx}!")
                break
            raw_resp = d
            if sc in (400, 422):
                if self.verbose and isinstance(d, dict):
                    code = d.get("code") or d.get("message") or ""
                    if "invalid" in str(code).lower() or "otp" in str(code).lower():
                        _p(f"   ⏹️  Stopping retries — OTP wrong (400 client error)")
                        break
            if sc == 401:
                if self.verbose:
                    _p(f"   ⏹️  Stopping retries — 401 = session_token invalid/expired")
                break
        else:
            # For loop exhausted without break = no access_token found
            msg = None
            if isinstance(last_err, dict):
                msg = last_err.get("message") or last_err.get("error")
            return False, msg or f"HTTP {last_sc}: {str(last_err)[:300]}", raw_resp

        d = raw_resp
        self.access_token  = d["access_token"]
        self.refresh_token = d.get("refresh_token")
        self.quiz_tokens   = (d.get("quiz_tokens")
                              or d.get("quizSessionTokens")
                              or d.get("quiz_session_tokens"))
        self.user_id       = d.get("id") or d.get("_id")
        self.profile_id    = d.get("masterProfile") or d.get("master_profile")

        jwt = decode_jwt_payload(self.access_token)
        if isinstance(jwt, dict):
            if jwt.get("nonce"):
                old_dev = self.device_id
                self.device_id     = str(jwt["nonce"])
                self.device_frozen = True
                if self.verbose:
                    _p(f"\n🔒 [JWT BIND] nonce={jwt['nonce']} → device_id FROZEN")
                    _p(f"   old: {old_dev}")
                    _p(f"   new: {self.device_id}")
            if not self.user_id:
                self.user_id = (jwt.get("id") or jwt.get("userId") or jwt.get("user_id")
                                or jwt.get("_id") or jwt.get("uid") or jwt.get("sub"))
            if not self.profile_id:
                self.profile_id = (jwt.get("masterProfile") or jwt.get("master_profile")
                                   or jwt.get("pid"))
            if not self.phone:
                self.phone = jwt.get("mobile") or jwt.get("phone")

        self.session.headers["authorization"] = f"Bearer {self.access_token}"
        if self.verbose:
            _p(f"\n✅ [LOGIN OK] access_token received")
            _p(f"   user_id    = {self.user_id}")
            _p(f"   profile_id = {self.profile_id}")
            _p(f"   Bearer prefix set on session")

        try:
            _slp(300)
            self._attest()
        except Exception:
            pass
        try:
            _slp(150)
            self._fetch_profile()
        except Exception:
            pass
        try:
            _slp(150)
            self._open_app()
        except Exception:
            pass

        return True, None, d

    def _attest(self):
        hdrs = {
            "x-device-id":         self.device_id,
            "x-minipix-integrity": self._integrity_stub(),
        }
        sc, d = self._req("POST", "/integrity/attest", headers=hdrs, data=b"")
        if sc == 403 and isinstance(d, dict) and d.get("code") == "DEVICE_INTEGRITY_REQUIRED":
            hdrs["x-minipix-integrity-error"] = "ERR_8000"
            self._req("POST", "/integrity/attest", headers=hdrs, data=b"")

    def _fetch_profile(self):
        uid = self.user_id or "me"
        sc, d = self._req("GET", f"/users/{uid}")
        if sc == 200 and isinstance(d, dict):
            if not self.user_id:
                self.user_id = d.get("_id") or d.get("id")
            if not self.profile_id:
                self.profile_id = d.get("master_profile")
                if not self.profile_id:
                    pro = d.get("profiles") or {}
                    for k in pro.keys():
                        self.profile_id = k
                        break

    def _open_app(self):
        if not (self.user_id and self.profile_id):
            return
        body = json.dumps({"date": date.today().isoformat()}).encode("utf-8")
        hdrs = {
            "content-type": "application/json; charset=utf-8",
            "x-device-id":  self.device_id,
        }
        self._req(
            "PATCH",
            f"/users/{self.user_id}/profiles/{self.profile_id}/open_app",
            headers=hdrs, data=body,
        )

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
            "device_bound_via_nonce": self.device_frozen,
            "tokens": {
                "access_token":  self.access_token  or "",
                "refresh_token": self.refresh_token or "",
                "quiz_tokens":   self.quiz_tokens,
            },
            "jwt_payload": jwt if jwt else None,
        }
        if isinstance(raw_verify_resp, dict):
            raw_safe = dict(raw_verify_resp)
            raw_safe.pop("access_token",  None)
            raw_safe.pop("refresh_token", None)
            raw_safe.pop("quiz_tokens",   None)
            if raw_safe:
                out["login_response_extra"] = raw_safe
        return out

# ───────────────────────── LOCAL CLI TEST MODE ────────────
def _input(prompt: str = "") -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        _p()
        _p("\n❌ Cancelled by user.")
        sys.exit(1)

def run_cli_mode():
    _print_section("MiniPix TOKEN EXTRACTOR — LOCAL CLI TEST MODE")
    _p("Telegram bot ki zarurat nahi. Direct terminal se login karo.")
    _p(f"API_BASE = {API_BASE}")
    _p("Har request ka poora dump (req/resp + 403 + integrity) dikhega.\n")

    use_curl = globals().get("_default_use_curl")
    client = MiniPixClient(verbose=True, use_curl_cffi=use_curl)

    _print_section("STEP 1 — Phone Number")
    _p("Phone number daalein (10-digit India ya +91...):")
    while True:
        raw = _input("> ").strip()
        phone = normalize_phone(raw)
        if phone:
            break
        _p("❌ Invalid number. Phir se daalein (e.g. 9876543210 ya +919876543210):")

    _print_section("STEP 2 — Generate OTP")
    _p(f"📡 Sending OTP request to {phone} ...")
    session_tok, err = client.generate_otp(phone)
    if session_tok is None:
        _p("\n❌❌❌ OTP GENERATE FAILED ❌❌❌")
        _p(f"Error: {err or 'Unknown'}")
        _p("\n💡 Agar 403 / DEVICE_INTEGRITY_REQUIRED aa raha hai:")
        _p("   → Server real Play Integrity / SafetyNet check kar raha hai.")
        _p("   → Stubbed integrity header (x-minipix-integrity) fail ho raha.")
        _p("   → Iska matlab actual Android app + signed APK + Play Integrity chahiye.")
        _p("   → Capture me actual integrity token dekho aur reproduce karo.")
        sys.exit(4)

    _p(f"\n✅ OTP SENT! session_token = {str(session_tok)[:60]}…")
    _p(f"   Registered mobile `{phone}` par 6-digit OTP aayega.")

    _print_section("STEP 3 — Verify OTP")
    for attempt in range(1, 4):
        _p(f"\nEnter 6-digit OTP (attempt {attempt}/3):")
        otp_raw = _input("> ").strip()
        otp = "".join(ch for ch in otp_raw if ch.isdigit())
        if len(otp) < 4:
            _p("❌ OTP kam hai (4+ digits chahiye). Phir se try karein.")
            continue

        ok, err, raw_resp = client.verify_otp(session_tok, otp)
        if ok:
            _print_section("✅ LOGIN SUCCESS — TOKENS EXTRACTED")
            dump = client.build_json_dump(raw_verify_resp=raw_resp)

            fname_base = "".join(ch for ch in (phone or "local") if ch.isalnum())
            fname = f"minipix_tokens_{fname_base}.json"
            out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(dump, f, indent=2, ensure_ascii=False)

            _p(f"📁 JSON saved  : {out_path}")
            _p(f"📱 Phone       : {dump['phone']}")
            _p(f"🪪 user_id     : {dump['user_id'] or '?'}")
            _p(f"🧭 profile_id  : {dump['profile_id'] or '?'}")
            _p(f"📱 device_id   : {dump['device_id']}")
            _p(f"🔗 nonce_bind  : {'✅ YES (frozen)' if dump['device_bound_via_nonce'] else '❌ No'}")
            _p(f"🔐 Access Tkn  : {str(dump['tokens']['access_token'])[:60]}…")
            _p(f"🔄 Refresh     : {'✅ present' if dump['tokens']['refresh_token'] else '—'}")
            qt = dump['tokens']['quiz_tokens']
            _p(f"🧠 Quiz Tkns   : {len(qt) if isinstance(qt, list) else ('✅' if qt else '—')}")
            _p()
            _p("Full JSON file upar di gayi path par hai.")
            return

        _p(f"\n❌ OTP VERIFY FAILED (attempt {attempt}/3):")
        _p(f"   Error: {err or 'Unknown'}")
        if attempt == 3:
            _p("\n❌ 3 attempts fail ho gaye. Session khatam.")
            _p("\n💡 Troubleshooting:")
            _p("   • 403 DEVICE_INTEGRITY_REQUIRED → Real Play Integrity chahiye")
            _p("   • 400 Invalid OTP → OTP galat hai, naya OTP generate karo")
            _p("   • 429 Too Many → Rate limit, 1-2 min wait karo")
            sys.exit(5)
        _p("   Naya OTP daalein ya CTRL+C se exit karo.")

# ───────────────────────── TELEGRAM BOT HANDLERS ──────────
def _keyboard_cancel():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel Login", callback_data="bot_cancel"),
    ]])

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    txt = (
        f"👋 Namaste *{u.first_name}*!\n\n"
        f"🎮 Yeh *MiniPix Token Extractor Bot* hai.\n"
        f"Koi bhi user OTP login karke apna poora token data\n"
        f"JSON file me download kar sakta hai.\n\n"
        f"✅ *Steps:*\n"
        f"  1. Send /login\n"
        f"  2. Apna phone number daalein (+91... ya 10-digit)\n"
        f"  3. OTP aayega → OTP daalein\n"
        f"  4. Verify ho jayega → JSON file bhej di jayegi\n\n"
        f"📁 *JSON file me kya hoga:*\n"
        f"  • Bearer Access Token\n"
        f"  • Refresh Token\n"
        f"  • Quiz Session Tokens\n"
        f"  • user_id, profile_id, phone\n"
        f"  • device_id (JWT nonce)\n"
        f"  • JWT payload decoded\n\n"
        f"⚠️  Security: Tokens apne paas hi rakhein — kisi se mat share karein.\n"
        f"/login se shuru karein — /cancel se cancel."
    )
    await update.message.reply_markdown(txt, reply_markup=_keyboard_cancel())

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
    txt = ("📱 *Step 1/2 — Phone Number*\n\n"
           "Apna MiniPix registered mobile number daalein:\n"
           "  • 10-digit (India): `98XXXXXX11`\n"
           "  • Full format:      `+9198XXXXXX11`")
    await update.message.reply_markdown(txt, reply_markup=_keyboard_cancel())
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

    _log(f"[user:{uid}] generate_otp → phone={phone}")
    await update.message.reply_text(
        f"📡 Generating OTP for `{phone}`...",
        parse_mode="Markdown",
    )
    session_tok, err = client.generate_otp(phone)
    if session_tok is None:
        txt = (f"❌ OTP Generate FAIL:\n`{err or 'Unknown'}`\n\n"
               f"/cancel karein ya phir naya number daalein.")
        await update.message.reply_markdown(txt, reply_markup=_keyboard_cancel())
        return WAIT_PHONE

    _set_state_field(uid, stage="otp",
                     session_token=session_tok,
                     phone=phone,
                     otp_attempts=0)
    _log(f"[user:{uid}] otp generated → ok")
    txt = (f"✅ OTP send ho gaya! Registered mobile `{phone}` par check karein.\n\n"
           f"*Step 2/2 — Enter 6-digit OTP:*")
    await update.message.reply_markdown(txt, reply_markup=_keyboard_cancel())
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
        await update.message.reply_text(
            "❌ OTP invalid (4+ digits chahiye). Phir se daalein:",
            reply_markup=_keyboard_cancel(),
        )
        return WAIT_OTP

    attempts = st.get("otp_attempts", 0) + 1
    _log(f"[user:{uid}] verify_otp attempt={attempts} phone={phone}")
    sent = await update.message.reply_text(
        "🔐 Verifying OTP & extracting tokens...",
        reply_markup=_keyboard_cancel(),
    )

    ok, err, raw = client.verify_otp(sess, otp)
    if not ok:
        _set_state_field(uid, otp_attempts=attempts)
        if attempts < 3:
            txt = (f"❌ OTP Verify FAIL:\n`{err or 'Unknown'}`\n\n"
                   f"Phir se 6-digit OTP daalein (attempt {attempts}/3):")
            await sent.edit_text(txt, reply_markup=_keyboard_cancel(),
                                 parse_mode=None)
            return WAIT_OTP
        txt = (f"❌ 3 baar galat OTP. Flow cancel.\n`{err}`\n\n"
               f"/login se naya session shuru karein.")
        try:
            await sent.edit_text(txt, reply_markup=None, parse_mode=None)
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
        f"📱 Phone     : `{dump['phone']}`\n"
        f"🪪 user_id   : `{dump['user_id'] or '?'}`\n"
        f"🧭 profile_id: `{dump['profile_id'] or '?'}`\n"
        f"📱 device_id : `{dump['device_id']}`\n"
        f"🔗 nonce_bind: {'✅ YES (frozen)' if dump['device_bound_via_nonce'] else '❌ No'}\n"
        f"🔐 Access Tkn: `{str(dump['tokens']['access_token'])[:40]}…`\n"
        f"🔄 Refresh   : {'✅ present' if dump['tokens']['refresh_token'] else '—'}\n"
        f"🧠 Quiz Tkns : {len(dump['tokens']['quiz_tokens']) if isinstance(dump['tokens']['quiz_tokens'], list) else ('✅' if dump['tokens']['quiz_tokens'] else '—')}\n\n"
        f"⚠️  *Security:* Ye file delete ya apne paas hi rakhein.\n"
        f"   Yahi Token AffiliateGuru panel / MiniPix bot me use karo."
    )
    try:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=InputFile(bio, filename=fname),
            caption=caption,
            parse_mode="Markdown",
        )
    except Exception as e:
        err_txt = (f"⚠️  File send failed ({e}). Yaha text me data hai:\n\n"
                   f"```json\n"
                   f"{json.dumps(dump, indent=2, ensure_ascii=False)[:3500]}\n"
                   f"```")
        try:
            await sent.edit_text(err_txt, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(err_txt, parse_mode="Markdown")
    else:
        try:
            await sent.delete()
        except Exception:
            pass
    finally:
        bio.close()

    _log(f"[user:{uid}] SUCCESS phone={phone} user_id={dump['user_id']}")
    _gc_state(uid)
    return ConversationHandler.END

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    _gc_state(uid)
    try:
        await q.edit_message_text(
            "✅ Login Cancelled. /login se phir se shuru karein.",
            reply_markup=None,
        )
    except Exception:
        pass
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log(f"ERROR(update={update!r}): {context.error!r}")

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
        ],
        allow_reentry=True,
        conversation_timeout=600,
        per_chat=True,
        per_user=True,
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^bot_cancel$"))
    app.add_handler(login_conv)
    app.add_error_handler(error_handler)
    return app

def main():
    parser = argparse.ArgumentParser(
        description="MiniPix Token Extractor — Telegram Bot + Local CLI Tester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick ping test (no input needed — check 403/WAF):
  python token_bot.py --test-request

  # Full CLI interactive login (OTP + extract token JSON):
  python token_bot.py --local

  # Force-enable curl_cffi JA3 bypass (install first: pip install curl_cffi):
  python token_bot.py --local --curl

  # Force-disable curl_cffi (use plain requests):
  python token_bot.py --test-request --no-curl

  # Telegram Bot mode:
  set TELEGRAM_BOT_TOKEN=xxx
  python token_bot.py
""",
    )
    parser.add_argument(
        "--local", "--cli", action="store_true", dest="local_mode",
        help="Run in LOCAL CLI TEST MODE (no Telegram bot — verbose 403/integrity debug)",
    )
    parser.add_argument(
        "--test-request", action="store_true", dest="test_ping",
        help="Just ping API_BASE with a simple test request & exit (check 403/WAF)",
    )
    curl_group = parser.add_mutually_exclusive_group()
    curl_group.add_argument(
        "--curl", "--curl-cffi", action="store_true", dest="force_curl",
        help="Force curl_cffi HTTP backend (JA3 TLS fingerprint bypass — needs: pip install curl_cffi)",
    )
    curl_group.add_argument(
        "--no-curl", action="store_true", dest="no_curl",
        help="Force plain python-requests backend (disable curl_cffi even if installed)",
    )
    args = parser.parse_args()

    use_curl = None
    if args.force_curl:
        use_curl = True
        if not HAS_CURL_CFFI:
            _p("❌ --curl flag given but curl_cffi is NOT installed.")
            _p("   Install: pip install curl_cffi")
            _p("   (curl_cffi mimics Chrome/Android TLS JA3 signature → WAF blocks ko bypass karta hai)")
            sys.exit(10)
    if args.no_curl:
        use_curl = False

    if args.test_ping:
        _print_section("API PING TEST (no auth)")
        c = MiniPixClient(verbose=True, use_curl_cffi=use_curl)
        _p(f"Testing GET /users/me (expected 401)\n")
        sc, d = c._req("GET", "/users/me")
        _p(f"\nFinal status = {sc}")
        if sc == 0:
            _p(f"Network error / connection failed: {d}")
        elif sc == 401:
            _p("✅ Network OK — 401 = expected (no token)")
        elif sc == 403:
            _p("⚠️  403 — WAF: Try: 1) India VPN, 2) pip install curl_cffi then --curl")
        elif sc == 200:
            _p("✅ 200 — unusual without token — check response")
        else:
            _p(f"Got status {sc} — see response above")
        sys.exit(0)

    if args.local_mode:
        try:
            # Pass through curl flag
            import __main__
            globals()["_cli_use_curl"] = use_curl
            run_cli_mode.__globals__["_default_use_curl"] = use_curl
            run_cli_mode()
        except KeyboardInterrupt:
            _p("\n\n❌ Interrupted. Bye!")
        return

    if not BOT_TOKEN:
        sys.stdout.write(
            "\n"
            "═══════════════════════════════════════════════════════════\n"
            "  MiniPix Token Extractor — Usage Options:\n"
            "\n"
            "  OPTION 1 — Local CLI Test (NO TELEGRAM BOT NEEDED)\n"
            "  Direct terminal me OTP login test karo, 403/integrity debug:\n"
            "    python token_bot.py --local\n"
            "\n"
            "  OPTION 2 — Telegram Bot Mode\n"
            "  TELEGRAM_BOT_TOKEN set karein phir run karein:\n"
            "    Windows: set TELEGRAM_BOT_TOKEN=123456789:ABCxyz...\n"
            "    Linux:   export TELEGRAM_BOT_TOKEN=123456789:ABCxyz...\n"
            "    python token_bot.py\n"
            "\n"
            "  BONUS — API Ping test (quick connectivity + 403 check):\n"
            "    python token_bot.py --test-request\n"
            "═══════════════════════════════════════════════════════════\n"
        )
        sys.exit(2)

    # Import check
    try:
        MiniPixClient()._req  # noqa
    except Exception as e:
        sys.stdout.write(f"❌ Startup check fail: {e}\n")
        sys.exit(3)

    _log("Bot starting...")
    app = build_application(BOT_TOKEN)
    _log("Polling started — press Ctrl+C to stop.")
    try:
        app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        _log("Interrupted — shutting down.")

if __name__ == "__main__":
    main()
