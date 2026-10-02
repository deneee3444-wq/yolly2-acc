"""
CyberLink MyEdit Online Otomatik Hesap ve Bonus Oluşturucu
Sadece app.py ve index.html ile çalışan Flask Web Uygulaması.
"""

import os
import sys
import time
import json
import base64
import random
import re
import string
import threading
from datetime import datetime
from collections import deque

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, jsonify, request
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ==============================================================================
# 1. NEON.COM POSTGRESQL BAĞLANTI ADRESİ (CONNECTION STRING)
# ==============================================================================
# Neon.com konsolundan aldığınız bağlantı adresini (Connection String) buraya yapıştırın.
NEON_DATABASE_URL = "postgresql://neondb_owner:npg_76MHSUtWVifm@ep-mute-math-b4cduhfm-pooler.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"


# ==============================================================================
# 2. SABİTLER VE API ENDPOINT'LERİ
# ==============================================================================
INIT_URL = "https://cse.cyberlink.com/cse/v2/init"
MEMBER_INIT_URL = "https://mauth.cyberlink.com/member-auth/v1/init"
SIGNUP_URL = "https://mauth.cyberlink.com/member-auth/public/sign-up"
LOGIN_URL = "https://mauth.cyberlink.com/member-auth/public/sign-in"
TOKEN_EXCHANGE_URL = "https://cse.cyberlink.com/cse/v2/getCseTokenByMember"
DAILY_BONUS_URL = "https://credit.cyberlink.com/v1/member/daily-bonus/get"
CREDIT_KEY_URL = "https://credit.cyberlink.com/v1/app/key"
CREDIT_TASK_BONUS_GET_URL = "https://credit.cyberlink.com/v1/app/task-bonus/get"
CREDIT_MEMBER_REMAIN_URL = "https://credit.cyberlink.com/v2/member/remain"

AES_IV = b"CyberLinkCSE"          # CSE modülü için 12 byte IV
CREDIT_IV = b"CyberLinkCredit"    # Credit modülü için 16 byte IV / AAD
MEMBER_AUTH_IV = b"CLMemberAuth"  # MemberAuth modülü için IV
SID_AOL_POL = "ae44600d"          # MyEdit Audio/Photo (AOL_POL) Service ID

MEMBER_AUTH_PUB_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtUnkrBbgQQLnHdk8d7LsDtC/rkQa9rTe7"
    "ZHwqf7jT1fqMGKFqa/4ESplrcyd6xmqt5m65v+IXBxhNFaqPZOrfMTxD5Kg1ZhlecfytcLR2Tuzg6"
    "MXVnDBzTJgU46rIRyzuippauieeoQZGNghxfDeOOveihZBYNwIYl3zK4DXZckm/Ils5wn3ZFEdJja"
    "ZEV4JFj6vOMDlORmRoCCZZ1xYvIbjSbXdRM9XsPuOK99ucwS750xycVB4qkAzrUvfJLiBw4rQgA7s"
    "g44/iMAlt2X71yLP6zYVVzuHQDcvQiWDJfymZfooPPehRf0cW+amWW4qmNsfhQVvY7AVijCBuC3QMw"
    "IDAQAB"
)
MEMBER_AUTH_KEY_ID = 2

MEMBER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://mauth.cyberlink.com",
    "Referer": "https://mauth.cyberlink.com/auth/myedit/signup?mode=myedit&isBusiness=false&lang=ENU",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://myedit.online",
    "Referer": "https://myedit.online/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}

DEFAULT_DOMAIN = "tempmailt.com"

POLL_COMPONENTS = [
    "frontend.components.action",
    "frontend.components.token-login",
    "frontend.components.check-mail",
    "frontend.components.inbox-message",
]


# ==============================================================================
# 3. GLOBAL DURUM VE LOG YÖNETİMİ
# ==============================================================================
log_lock = threading.Lock()
logs_history = deque(maxlen=1500)
log_counter = 0

state_lock = threading.Lock()
app_state = {
    "is_running": False,
    "auto_mode": False,
    "countdown_remaining": 0,
    "current_email": None,
    "current_status": "Hazır",
    "total_created": 0,
    "total_credits": 0,
    "last_email": None,
    "last_password": None
}

created_accounts = []


def add_log(message: str, level: str = "INFO"):
    """Sistem loglarına thread-safe olarak ekler ve konsola da yazdırır."""
    global log_counter
    timestamp = datetime.now().strftime("%H:%M:%S")
    with log_lock:
        log_counter += 1
        item = {
            "id": log_counter,
            "time": timestamp,
            "level": level,
            "message": message
        }
        logs_history.append(item)
    print(f"[{timestamp}] [{level}] {message}", flush=True)


# ==============================================================================
# 4. NEON VERİTABANI İŞLEMLERİ
# ==============================================================================
def check_neon_db():
    """Neon bağlantısını test eder ve tablo yoksa oluşturur."""
    if not NEON_DATABASE_URL or NEON_DATABASE_URL.strip() == "":
        return False, "Bağlantı adresi girilmedi"
    try:
        import psycopg2
        conn = psycopg2.connect(NEON_DATABASE_URL, connect_timeout=8)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                password VARCHAR(255) NOT NULL,
                member_id VARCHAR(100),
                member_token TEXT,
                cl_token TEXT,
                credits INT DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                status VARCHAR(50) DEFAULT 'SUCCESS'
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        return True, "Bağlantı başarılı"
    except Exception as e:
        return False, str(e)


def fetch_accounts_from_db():
    """Neon PostgreSQL'den kayıtlı tüm hesapları getirir."""
    if not NEON_DATABASE_URL or NEON_DATABASE_URL.strip() == "":
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(NEON_DATABASE_URL, connect_timeout=8)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                password VARCHAR(255) NOT NULL,
                member_id VARCHAR(100),
                member_token TEXT,
                cl_token TEXT,
                credits INT DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                status VARCHAR(50) DEFAULT 'SUCCESS'
            );
        """)
        cur.execute("""
            SELECT id, email, password, credits, member_id, 
                   to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') as ctime,
                   status
            FROM accounts 
            ORDER BY id DESC;
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        result = []
        for r in rows:
            result.append({
                "id": r[0],
                "email": r[1],
                "password": r[2],
                "credits": r[3] or 0,
                "member_id": r[4] or "-",
                "created_at": r[5] or "",
                "db_saved": True
            })
        return result
    except Exception as e:
        print(f"[DB HATA] fetch_accounts_from_db: {e}", flush=True)
        return None


def reset_neon_db():
    """Neon PostgreSQL veritabanındaki hesapları tamamen temizler ve tabloyu baştan kurar."""
    if not NEON_DATABASE_URL or NEON_DATABASE_URL.strip() == "":
        return False, "Neon Connection String girilmedi."
    try:
        import psycopg2
        conn = psycopg2.connect(NEON_DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS accounts CASCADE;")
        cur.execute("""
            CREATE TABLE accounts (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                password VARCHAR(255) NOT NULL,
                member_id VARCHAR(100),
                member_token TEXT,
                cl_token TEXT,
                credits INT DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                status VARCHAR(50) DEFAULT 'SUCCESS'
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        return True, "Veritabanı sıfırlandı ve tablo sıfırdan oluşturuldu."
    except Exception as e:
        return False, str(e)


def save_account_to_db(email, password, member_id, member_token, cl_token, credits):
    """Hesap verilerini Neon PostgreSQL veritabanına kaydeder."""
    if not NEON_DATABASE_URL or NEON_DATABASE_URL.strip() == "":
        add_log("[DB UYARI] Neon Connection String girilmediği için hesap sadece oturum listesine eklendi.", level="WARN")
        return False

    try:
        import psycopg2
        conn = psycopg2.connect(NEON_DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                password VARCHAR(255) NOT NULL,
                member_id VARCHAR(100),
                member_token TEXT,
                cl_token TEXT,
                credits INT DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                status VARCHAR(50) DEFAULT 'SUCCESS'
            );
        """)
        cur.execute("""
            INSERT INTO accounts (email, password, member_id, member_token, cl_token, credits, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
        """, (email, password, str(member_id or ""), str(member_token or ""), str(cl_token or ""), int(credits or 0), 'SUCCESS'))
        inserted_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        add_log(f"[DB] Hesap Neon PostgreSQL veritabanına kaydedildi! (ID: {inserted_id})", level="SUCCESS")
        return True
    except Exception as e:
        add_log(f"[DB HATA] Veritabanına kaydedilirken hata oluştu: {e}", level="ERROR")
        return False


# ==============================================================================
# 5. TEMP MAIL (cmail.asia) İŞLEMLERİ
# ==============================================================================
def generate_random_username(length: int = 10) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=length))


class TempMailClient:
    """temp-mail.asia Livewire Entegrasyonu (cmail.asia)"""
    BASE_URL = "https://temp-mail.asia"

    def __init__(self, domain: str = DEFAULT_DOMAIN):
        self.domain = domain
        self.box = None
        self.email = None
        self.session = requests.Session()
        self.csrf = None
        self.components = {}
        self.lw_headers = {}
        self._seen_ids = set()

    def get_email(self, length: int = 10, domain: str = DEFAULT_DOMAIN) -> str:
        self.domain = domain or DEFAULT_DOMAIN
        init_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "upgrade-insecure-requests": "1",
        }

        resp = self.session.get(f"{self.BASE_URL}/", headers=init_headers, timeout=30)
        resp.raise_for_status()
        html = resp.text

        csrf_match = re.search(r'data-csrf=["\']([^"\']+)["\']', html)
        email_match = re.search(r"const email\s*=\s*'([^']+)'", html)
        self.csrf = csrf_match.group(1) if csrf_match else None
        default_email = email_match.group(1) if email_match else None

        soup = BeautifulSoup(html, "html.parser")
        self.components = {}
        for el in soup.find_all(attrs={"wire:snapshot": True}):
            raw_snap = el.get("wire:snapshot", "")
            try:
                snap_data = json.loads(raw_snap)
                name = snap_data.get("memo", {}).get("name", "")
                if name:
                    self.components[name] = {"snapshot": raw_snap, "name": name}
            except Exception:
                pass

        self.lw_headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "tr-TR,tr;q=0.9",
            "Content-Type": "application/json",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
            "User-Agent": init_headers["User-Agent"],
            "x-livewire": "1",
            "x-csrf-token": self.csrf,
        }

        self.box = random.choice(string.ascii_lowercase) + generate_random_username(length - 1)
        target_email = f"{self.box}@{self.domain}"

        check_mail_comp = self.components.get("frontend.components.check-mail")
        if check_mail_comp and self.csrf:
            change_payload = {
                "_token": self.csrf,
                "components": [
                    {
                        "snapshot": check_mail_comp["snapshot"],
                        "updates": {
                            "username": self.box,
                            "domain": self.domain,
                        },
                        "calls": [
                            {
                                "method": "checkEmailAddress",
                                "params": [],
                                "metadata": {},
                            }
                        ],
                    }
                ],
            }
            try:
                change_resp = self.session.post(
                    f"{self.BASE_URL}/livewire/update",
                    headers=self.lw_headers,
                    json=change_payload,
                    timeout=30,
                )
                if change_resp.ok:
                    for c in change_resp.json().get("components", []):
                        if c.get("snapshot"):
                            self.components["frontend.components.check-mail"]["snapshot"] = c["snapshot"]
                    self.email = target_email
                else:
                    self.email = default_email or target_email
            except Exception as e:
                add_log(f"[Temp Mail] checkEmailAddress uyarısı: {e}", level="WARN")
                self.email = default_email or target_email
        else:
            self.email = default_email or target_email

        add_log(f"[Temp Mail] Oluşturulan E-posta: {self.email}", level="SUCCESS")
        return self.email

    def _build_poll_payload(self) -> dict:
        api_components = []
        for name in POLL_COMPONENTS:
            comp = self.components.get(name)
            if not comp:
                continue

            if name == "frontend.components.inbox-message":
                calls = [
                    {
                        "method": "__dispatch",
                        "params": ["syncEmail", {"email": self.email}],
                        "metadata": {},
                    },
                    {
                        "method": "__dispatch",
                        "params": ["fetchMessages", {}],
                        "metadata": {},
                    },
                ]
            else:
                calls = [
                    {
                        "method": "__dispatch",
                        "params": ["syncEmail", {"email": self.email}],
                        "metadata": {},
                    },
                ]
            api_components.append(
                {
                    "snapshot": comp["snapshot"],
                    "updates": {},
                    "calls": calls,
                }
            )
        return {"_token": self.csrf, "components": api_components}

    def _extract_activation_link(self, text: str) -> str:
        trace_links = re.findall(
            r'https?://membership\.cyberlink\.com/prog/event/autoedm/trace_mem\.jsp\?[^\s"\'<>]+',
            text,
        )
        for link in trace_links:
            link = link.replace("&amp;", "&").rstrip("\"'")
            if any(k in link for k in ["account-activate", "Activate", "active-member"]):
                return link

        general_links = re.findall(
            r'https?://[^\s"\'<>]*(?:cyberlink|myedit)[^\s"\'<>]*(?:activate|confirm|verify|token)[^\s"\'<>]*',
            text,
            re.IGNORECASE,
        )
        for link in general_links:
            link = link.replace("&amp;", "&").rstrip("\"'")
            if any(k in link for k in ["activate", "confirm", "verify"]):
                return link

        try:
            soup = BeautifulSoup(text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].replace("&amp;", "&").rstrip("\"'")
                if any(k in href for k in ["trace_mem", "activate", "confirm", "verify", "token"]) and any(
                    d in href for d in ["cyberlink", "myedit"]
                ):
                    return href
        except Exception:
            pass

        if trace_links:
            return trace_links[0].replace("&amp;", "&").rstrip("\"'")

        return None

    def _fetch_message_content(self, msg_id, inbox_snapshot) -> str:
        view_payload = {
            "_token": self.csrf,
            "components": [
                {
                    "snapshot": inbox_snapshot,
                    "updates": {},
                    "calls": [{"method": "updateView", "params": [msg_id], "metadata": {}}],
                }
            ],
        }
        view_resp = self.session.post(
            f"{self.BASE_URL}/livewire/update",
            headers=self.lw_headers,
            json=view_payload,
            timeout=30,
        )
        if not view_resp.ok:
            return ""

        all_text = []
        for comp_resp in view_resp.json().get("components", []):
            effects_html = comp_resp.get("effects", {}).get("html", "")
            if effects_html:
                all_text.append(effects_html)
            snap_str = comp_resp.get("snapshot", "")
            if snap_str:
                try:
                    snap = json.loads(snap_str)
                    msgs = snap.get("data", {}).get("messages", [])
                    if msgs and isinstance(msgs[0], list):
                        for group in msgs[0]:
                            if isinstance(group, list):
                                for m in group:
                                    if isinstance(m, dict) and "content" in m:
                                        all_text.append(m["content"])
                except Exception:
                    pass
        return "\n".join(all_text)

    def wait_for_activation_link(self, timeout: int = 75) -> str:
        add_log(f"[Temp Mail] Gelen kutusu sorgulanıyor ({self.email})...", level="INFO")
        deadline = time.time() + timeout
        inbox_snapshot = None

        while time.time() < deadline:
            if not app_state["is_running"]:
                raise InterruptedError("İşlem kullanıcı tarafından durduruldu.")

            inbox_keys = []
            try:
                payload = self._build_poll_payload()
                resp = self.session.post(
                    f"{self.BASE_URL}/livewire/update",
                    headers=self.lw_headers,
                    json=payload,
                    timeout=20,
                )
                if resp.ok:
                    data = resp.json()
                    resp_comps = data.get("components", [])
                    active_names = [n for n in POLL_COMPONENTS if n in self.components]

                    for i, rc in enumerate(resp_comps):
                        new_snap = rc.get("snapshot", "")
                        if not new_snap or i >= len(active_names):
                            continue
                        target_name = active_names[i]
                        self.components[target_name]["snapshot"] = new_snap

                        if target_name == "frontend.components.inbox-message":
                            try:
                                snap = json.loads(new_snap)
                                inbox_msgs = snap.get("data", {}).get("inbox_messages", [])
                                if isinstance(inbox_msgs, list):
                                    for item in inbox_msgs:
                                        if isinstance(item, dict) and "keys" in item:
                                            inbox_keys = item["keys"]
                                            inbox_snapshot = new_snap
                            except Exception:
                                pass
            except Exception as e:
                add_log(f"[Temp Mail] Poll uyarısı: {e}", level="WARN")

            unseen_keys = [k for k in inbox_keys if k not in self._seen_ids]
            if unseen_keys:
                for msg_id in unseen_keys:
                    self._seen_ids.add(msg_id)
                    add_log(f"[+] Yeni mail alındı! (ID: {msg_id})", level="SUCCESS")
                    content = self._fetch_message_content(msg_id, inbox_snapshot)
                    link = self._extract_activation_link(content)
                    if link:
                        add_log(f"  -> Aktivasyon linki yakalandı: {link[:80]}...", level="SUCCESS")
                        return link

            time.sleep(3)

        raise TimeoutError("Aktivasyon maili zaman aşımına uğradı (gelmedi)!")


# ==============================================================================
# 6. CYBERLINK ŞİFRELEME VE API İŞLEMLERİ
# ==============================================================================
def get_member_auth_public_key():
    try:
        resp = requests.post(MEMBER_INIT_URL, json={"p": "myedit"}, headers=MEMBER_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("info", {})
        return data["public_key"], data["id"]
    except Exception as e:
        add_log(f"[!] MemberAuth init uyarısı: {e}, statik anahtar kullanılıyor.", level="WARN")
        return MEMBER_AUTH_PUB_KEY, MEMBER_AUTH_KEY_ID


def create_member_auth_payload(user_data: dict):
    pub_key_b64, key_id = get_member_auth_public_key()
    der_bytes = base64.b64decode(pub_key_b64)
    public_key = serialization.load_der_public_key(der_bytes)

    aes_key = AESGCM.generate_key(bit_length=256)
    rsa_encrypted_aes_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    a_param = base64.b64encode(rsa_encrypted_aes_key).decode("utf-8")

    json_bytes = json.dumps(user_data, separators=(",", ":")).encode("utf-8")
    aesgcm = AESGCM(aes_key)
    cipher_bytes = aesgcm.encrypt(MEMBER_AUTH_IV, json_bytes, None)
    data_param = base64.b64encode(cipher_bytes).decode("utf-8")

    return {
        "a": a_param,
        "data": data_param,
        "k": key_id,
    }, aes_key


def decrypt_member_auth_response(response_b64: str, aes_key: bytes):
    enc_bytes = base64.b64decode(response_b64)
    aesgcm = AESGCM(aes_key)
    decrypted_bytes = aesgcm.decrypt(MEMBER_AUTH_IV, enc_bytes, None)
    return json.loads(decrypted_bytes.decode("utf-8"))


def get_server_public_key():
    resp = requests.post(INIT_URL, json={"p": "myedit"}, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data["public_key"], data["id"]


def create_payload(user_data: dict):
    pub_key_b64, key_id = get_server_public_key()
    der_bytes = base64.b64decode(pub_key_b64)
    public_key = serialization.load_der_public_key(der_bytes)

    aes_key = AESGCM.generate_key(bit_length=256)
    rsa_encrypted_aes_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    a_param = base64.b64encode(rsa_encrypted_aes_key).decode("utf-8")

    json_bytes = json.dumps(user_data, separators=(",", ":")).encode("utf-8")
    aesgcm = AESGCM(aes_key)
    cipher_bytes = aesgcm.encrypt(AES_IV, json_bytes, None)
    data_param = base64.b64encode(cipher_bytes).decode("utf-8")

    return {
        "a": a_param,
        "data": data_param,
        "k": str(key_id),
    }, aes_key


def decrypt_response(response_b64: str, aes_key: bytes):
    enc_bytes = base64.b64decode(response_b64)
    aesgcm = AESGCM(aes_key)
    decrypted_bytes = aesgcm.decrypt(AES_IV, enc_bytes, None)
    return json.loads(decrypted_bytes.decode("utf-8"))


def get_credit_server_public_key():
    resp = requests.get(CREDIT_KEY_URL, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()["result"]
    return data["key"], data["id"]


def create_credit_payload(data_dict: dict):
    pub_key_b64, key_id = get_credit_server_public_key()
    der_bytes = base64.b64decode(pub_key_b64)
    public_key = serialization.load_der_public_key(der_bytes)

    aes_key = AESGCM.generate_key(bit_length=256)
    rsa_encrypted_aes_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    a_param = base64.b64encode(rsa_encrypted_aes_key).decode("utf-8")

    json_bytes = json.dumps(data_dict, separators=(",", ":")).encode("utf-8")
    aesgcm = AESGCM(aes_key)
    cipher_bytes = aesgcm.encrypt(CREDIT_IV, json_bytes, CREDIT_IV)
    data_param = base64.b64encode(cipher_bytes).decode("utf-8")

    return {
        "a": a_param,
        "data": data_param,
        "id": str(key_id),
    }


def signup(email: str, password: str, lang: str = "enu"):
    user_data = {
        "email": email,
        "password": password,
        "language": lang.upper(),
        "rec_upgrade": 0,
        "sid": "myedit",
        "nJoint": 62
    }
    payload, aes_key = create_member_auth_payload(user_data)
    res = requests.post(SIGNUP_URL, json=payload, headers=MEMBER_HEADERS, timeout=30)
    res.raise_for_status()
    body = res.json()
    if body.get("status") == "SUCCESS" and "info" in body:
        decrypted = decrypt_member_auth_response(body["info"], aes_key)
        return {"status": "SUCCESS", "info": decrypted}
    return body


def activate_account(activation_url: str):
    add_log("Aktivasyon isteği gönderiliyor...", level="INFO")
    session = requests.Session()
    session.headers.update({
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    resp = session.get(activation_url, allow_redirects=True, timeout=30)
    if resp.status_code == 200:
        add_log("[+] HESAP BAŞARIYLA AKTİFLEŞTİRİLDİ!", level="SUCCESS")
        return True
    else:
        add_log(f"[-] Aktivasyon uyarısı (HTTP {resp.status_code})", level="WARN")
        return False


def login(email: str, password: str):
    user_data = {
        "email": email,
        "password": password,
        "recaptcha": None,
        "sid": "myedit",
    }
    payload, aes_key = create_member_auth_payload(user_data)
    res = requests.post(LOGIN_URL, json=payload, headers=MEMBER_HEADERS, timeout=30)
    res.raise_for_status()
    body = res.json()
    if body.get("status") == "SUCCESS" and "info" in body:
        decrypted = decrypt_member_auth_response(body["info"], aes_key)
        return {"status": "SUCCESS", "info": decrypted}
    return body


def get_cse_token_by_member(member_token: str):
    user_data = {"memberToken": member_token}
    payload, aes_key = create_payload(user_data)
    res = requests.post(TOKEN_EXCHANGE_URL, json=payload, headers=HEADERS, timeout=30)
    res.raise_for_status()
    body = res.json()
    if "response" in body:
        return decrypt_response(body["response"], aes_key)
    return body


def get_daily_bonus(member_token: str):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {member_token}",
        "Origin": "https://myedit.online",
        "Referer": "https://myedit.online/",
        "User-Agent": HEADERS["User-Agent"],
    }
    payload = {"sid": SID_AOL_POL}
    res = requests.post(DAILY_BONUS_URL, json=payload, headers=headers, timeout=30)
    res.raise_for_status()
    return res.json()


def claim_task_bonus(member_token: str, feature_id: str = "TextToImage", claim_credit: int = None):
    try:
        data_obj = {
            "sid": SID_AOL_POL,
            "unique_id": "",
            "version": "temp_one_time_free",
            "event_id": feature_id,
            "member_token": member_token,
        }
        if claim_credit is not None:
            data_obj["claim_credit"] = claim_credit
        payload = create_credit_payload(data_obj)
        res = requests.post(CREDIT_TASK_BONUS_GET_URL, json=payload, headers=HEADERS, timeout=30)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        return None


def check_task_bonus(member_token: str, feature_id: str = "TextToImage"):
    try:
        check_url = "https://credit.cyberlink.com/v1/app/task-bonus/check"
        data_obj = {
            "sid": SID_AOL_POL,
            "unique_id": "",
            "version": "temp_one_time_free",
            "event_id": feature_id,
            "member_token": member_token,
        }
        payload = create_credit_payload(data_obj)
        res = requests.post(check_url, json=payload, headers=HEADERS, timeout=30)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        return None


def get_member_remaining_credits(member_token: str):
    try:
        url = f"{CREDIT_MEMBER_REMAIN_URL}?detail=true&lang=ENU&sid={SID_AOL_POL}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": HEADERS["User-Agent"],
            "Authorization": f"Bearer {member_token}",
            "Origin": "https://myedit.online",
            "Referer": "https://myedit.online/",
        }
        res = requests.get(url, headers=headers, timeout=15)
        res.raise_for_status()
        credits_json = res.json()
        return credits_json.get("total_remain", 0)
    except Exception as e:
        add_log(f"[!] Kredi sorgulama hatası: {e}", level="WARN")
        return 0


def collect_all_bonuses(member_token: str):
    add_log("[Bonuses] Günlük bonus ve AI görev bonusları toplanıyor...", level="INFO")
    
    # 1. Günlük Bonus (+3 Kredi)
    try:
        daily_res = get_daily_bonus(member_token)
        add_log(f"  -> Günlük Bonus: {daily_res.get('result', daily_res)}", level="BONUS")
    except Exception as e:
        add_log(f"  [!] Günlük bonus hatası: {e}", level="WARN")

    # 2. Aktif Görev Bonusları
    active_tasks = [
        "TextToImage", "AICollage", "AIReplacement", "TextToVideo",
        "ImageToVideo", "Storytelling", "LyricsToSong", "VideoToVideo",
        "AICinematicShorts", "VideoAutoReframe", "AITryOn_1", "AITryOn_2",
        "AITryOn", "TrendingAITemplates", "AIHairstyleV2"
    ]

    for task_id in active_tasks:
        if not app_state["is_running"]:
            break
        try:
            check_task_bonus(member_token, feature_id=task_id)
            claim_task_bonus(member_token, feature_id=task_id)
        except Exception:
            pass

    total_credits = get_member_remaining_credits(member_token)
    add_log(f"  -> Toplam Toplanan Kredi: {total_credits} Kredi", level="BONUS")
    return total_credits


# ==============================================================================
# 7. HESAP OLUŞTURMA İŞLEMCİSİ (WORKER)
# ==============================================================================
def create_single_account():
    """Tek bir hesap oluşturma ve bonus toplama sürecini baştan sona işletir."""
    email = None
    password = "CyberLink123!"

    add_log("==================================================", level="INFO")
    add_log("YENİ HESAP OLUŞTURMA SÜRECİ BAŞLATILDI", level="INFO")

    # 1. Temp Mail Al (cmail.asia)
    add_log(f"[Temp Mail] {DEFAULT_DOMAIN} üzerinden geçici mail alınıyor...", level="INFO")
    temp_mail = TempMailClient(domain=DEFAULT_DOMAIN)
    email = temp_mail.get_email(domain=DEFAULT_DOMAIN)
    with state_lock:
        app_state["current_email"] = email
        app_state["current_status"] = "Kayıt yapılıyor..."

    # 2. Kayıt İsteği
    add_log("[1/4] Kaydolma İsteği Gönderiliyor...", level="INFO")
    signup_res = signup(email, password)
    if signup_res.get("status") != "SUCCESS":
        add_log(f"[x] Kayıt başarısız oldu: {signup_res}", level="ERROR")
        return False
    add_log("[+] Kayıt isteği başarılı!", level="SUCCESS")

    # 3. Aktivasyon Maili Bekleme
    with state_lock:
        app_state["current_status"] = "Aktivasyon bekleniyor..."
    add_log("[2/4] Temp Mail Kutusu Otomatik Sorgulanıyor...", level="INFO")
    activation_url = temp_mail.wait_for_activation_link(timeout=75)

    # 4. Aktifleştirme
    with state_lock:
        app_state["current_status"] = "Aktifleştiriliyor..."
    add_log("[3/4] Hesap Otomatik Aktifleştiriliyor...", level="INFO")
    activate_account(activation_url)

    add_log("Aktivasyon tamamlandı. Sunucu güncellemesi için 3 saniye bekleniyor...", level="INFO")
    time.sleep(3)

    # 5. Giriş ve Token Alımı
    with state_lock:
        app_state["current_status"] = "Giriş yapılıyor..."
    add_log("[4/4] Giriş Yapılıyor ve Krediler Toplanıyor...", level="INFO")
    login_res = login(email, password)

    if login_res.get("status") != "SUCCESS":
        add_log(f"[x] Giriş başarısız oldu: {login_res}", level="ERROR")
        return False

    info = login_res.get("info", {})
    member_token = info.get("memberToken")
    member_id = info.get("memberId")

    add_log(f"  -> Giriş Başarılı! Member ID: {member_id}", level="SUCCESS")

    cl_token = None
    try:
        cse_res = get_cse_token_by_member(member_token)
        cl_token = cse_res.get("cltoken")
        add_log(f"  -> CL Token Alındı: {cl_token[:25]}..." if cl_token else "  -> CL Token: Alınamadı", level="INFO")
    except Exception as e:
        add_log(f"  [!] CSE token hatası: {e}", level="WARN")

    # 6. Bonusları Topla
    with state_lock:
        app_state["current_status"] = "Bonuslar toplanıyor..."
    credits = 0
    if member_token:
        credits = collect_all_bonuses(member_token)
    else:
        add_log("[!] memberToken olmadığı için bonus çekilemedi.", level="WARN")

    # 7. Veritabanına Kaydet
    with state_lock:
        app_state["current_status"] = "Veritabanına kaydediliyor..."
    db_saved = save_account_to_db(email, password, member_id, member_token, cl_token, credits)

    # 8. Oturum Hafızasına Ekle ve İstatistikleri Güncelle
    account_record = {
        "id": len(created_accounts) + 1,
        "email": email,
        "password": password,
        "credits": credits,
        "member_id": member_id or "-",
        "created_at": datetime.now().strftime("%H:%M:%S"),
        "db_saved": db_saved
    }
    created_accounts.insert(0, account_record)

    with state_lock:
        app_state["total_created"] += 1
        app_state["total_credits"] += credits
        app_state["last_email"] = email
        app_state["last_password"] = password

    add_log(f"[✓ TAMAMLANDI] Hesap Hazır: {email} | Şifre: {password} | Kredi: {credits}", level="SUCCESS")
    add_log("==================================================", level="INFO")
    return True


def worker_loop():
    """Arka planda tek seferlik veya döngüsel (otomatik mod) hesap üretimini yönetir."""
    add_log(f"[SİSTEM] Hesap oluşturma süreci başlatıldı. Mod: {'Otomatik (1 Dakikada 1 Hesap)' if app_state['auto_mode'] else 'Tek Seferlik'}", level="INFO")

    while True:
        with state_lock:
            if not app_state["is_running"]:
                break
            is_auto = app_state["auto_mode"]

        try:
            create_single_account()
        except InterruptedError:
            add_log("[SİSTEM] Kullanıcı işlemi durdurdu.", level="WARN")
            break
        except Exception as e:
            add_log(f"[HATA] Beklenmeyen hata oluştu: {e}", level="ERROR")

        with state_lock:
            if not app_state["is_running"]:
                break
            if not is_auto:
                add_log("[SİSTEM] Tek seferlik hesap oluşturma tamamlandı.", level="SUCCESS")
                app_state["is_running"] = False
                app_state["current_status"] = "Tamamlandı"
                break

        # Otomatik Mod: 1 Dakika (60 saniye) bekleme
        add_log("[Oto Mod] Yeni hesap için 1 dakika (60 sn) geri sayım başladı...", level="INFO")
        for remaining in range(1, 0, -1):
            with state_lock:
                if not app_state["is_running"]:
                    break
                app_state["countdown_remaining"] = remaining
                app_state["current_status"] = f"Sonraki hesap: {remaining} sn"
            time.sleep(1)

        with state_lock:
            app_state["countdown_remaining"] = 0
            if not app_state["is_running"]:
                break

    with state_lock:
        app_state["is_running"] = False
        app_state["current_status"] = "Durduruldu"
        app_state["countdown_remaining"] = 0
    add_log("[SİSTEM] Süreç sona erdi.", level="INFO")


# ==============================================================================
# 8. FLASK UYGULAMASI VE HTTP ENDPOINT'LERİ (SSE KULLANILMAZ)
# ==============================================================================
# Sadece app.py ve index.html ile çalışması için template_folder geçerli dizin olarak ayarlandı
app = Flask(__name__, template_folder=".")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def get_status():
    """Mevcut çalışma durumu ve veritabanı bağlantı kontrolünü döner."""
    db_ok, db_msg = check_neon_db()
    with state_lock:
        state_copy = dict(app_state)
    state_copy["db_connected"] = db_ok
    state_copy["db_message"] = db_msg
    return jsonify(state_copy)


@app.route("/api/start", methods=["POST"])
def start_worker():
    """Hesap oluşturma işlemini başlatır."""
    data = request.get_json(silent=True) or {}
    auto_mode = bool(data.get("auto_mode", False))

    with state_lock:
        if app_state["is_running"]:
            return jsonify({"status": "error", "message": "İşlem zaten çalışıyor."}), 400
        app_state["is_running"] = True
        app_state["auto_mode"] = auto_mode
        app_state["current_status"] = "Başlatılıyor..."
        app_state["countdown_remaining"] = 0

    thread = threading.Thread(target=worker_loop, daemon=True)
    thread.start()
    return jsonify({"status": "ok", "auto_mode": auto_mode})


@app.route("/api/stop", methods=["POST"])
def stop_worker():
    """Çalışan işlemi durdurur."""
    with state_lock:
        app_state["is_running"] = False
        app_state["current_status"] = "Durduruluyor..."
        app_state["countdown_remaining"] = 0
    add_log("[SİSTEM] Durdurma sinyali gönderildi.", level="WARN")
    return jsonify({"status": "ok"})


@app.route("/api/updates", methods=["GET"])
def get_updates():
    """
    SSE OLMADAN, basit fetch polling ile:
    - Son ID'den sonraki yeni logları
    - Güncel durumu (DB'den senkronize edilmiş toplam sayılarla)
    - Veritabanındaki veya oturumdaki tüm hesapları tek yanıtta döner.
    """
    last_id = int(request.args.get("last_log_id", 0))

    with log_lock:
        new_logs = [l for l in logs_history if l["id"] > last_id]
        current_max_id = log_counter

    with state_lock:
        state_copy = dict(app_state)

    db_ok, _ = check_neon_db()
    state_copy["db_connected"] = db_ok

    # Veritabanı bağlıysa kayıtlı tüm hesapları DB'den çek
    db_accounts = fetch_accounts_from_db() if db_ok else None
    if db_accounts is not None:
        accounts_to_send = db_accounts
        # İstatistikleri DB ile senkronize et
        state_copy["total_created"] = len(db_accounts)
        state_copy["total_credits"] = sum(a.get("credits", 0) for a in db_accounts)
        if db_accounts and not state_copy["last_email"]:
            state_copy["last_email"] = db_accounts[0].get("email")
    else:
        accounts_to_send = created_accounts

    return jsonify({
        "logs": new_logs,
        "last_log_id": current_max_id,
        "status": state_copy,
        "accounts": accounts_to_send
    })


@app.route("/api/export", methods=["GET"])
def export_accounts():
    """Tüm hesap listesini TXT veya CSV dosyası olarak indirilmeye sunar."""
    export_format = request.args.get("format", "txt").lower()
    db_ok, _ = check_neon_db()
    accounts = fetch_accounts_from_db() if db_ok else None
    if accounts is None:
        accounts = created_accounts

    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    if export_format == "csv":
        from flask import Response
        lines = ["ID,Email,Password,Credits,MemberID,CreatedAt"]
        for a in accounts:
            lines.append(f'"{a.get("id", "")}","{a.get("email", "")}","{a.get("password", "")}",{a.get("credits", 0)},"{a.get("member_id", "")}","{a.get("created_at", "")}"')
        csv_content = "\n".join(lines)
        return Response(
            csv_content,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=hesaplar_{timestamp_str}.csv"}
        )
    else:
        # TXT formatı: email:password ve detaylar
        from flask import Response
        lines = [
            f"# CyberLink MyEdit Online Hesap Listesi ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})",
            f"# Toplam Hesap: {len(accounts)}",
            "# Format: email:password | Kredi | Member ID | Kayıt Tarihi",
            "-" * 70
        ]
        for a in accounts:
            lines.append(f"{a.get('email')}:{a.get('password')} | {a.get('credits', 0)} Kredi | ID: {a.get('member_id', '-')} | {a.get('created_at', '')}")

        lines.append("\n" + "=" * 70)
        lines.append("# SADECE EMAIL:PASSWORD LISTESI:")
        lines.append("=" * 70)
        for a in accounts:
            lines.append(f"{a.get('email')}:{a.get('password')}")

        txt_content = "\n".join(lines)
        return Response(
            txt_content,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=hesaplar_{timestamp_str}.txt"}
        )


@app.route("/api/db/clear", methods=["POST"])
def clear_database():
    """Tüm veritabanını temizler ve tabloyu baştan sıfırdan kurar."""
    db_ok, msg = check_neon_db()
    if not db_ok:
        # DB yoksa sadece oturum hafızasını temizle
        created_accounts.clear()
        with state_lock:
            app_state["total_created"] = 0
            app_state["total_credits"] = 0
            app_state["last_email"] = None
        add_log("[SİSTEM] Oturumdaki hesap listesi temizlendi.", level="WARN")
        return jsonify({"status": "ok", "message": "Oturum hesapları temizlendi."})

    success, res_msg = reset_neon_db()
    if success:
        created_accounts.clear()
        with state_lock:
            app_state["total_created"] = 0
            app_state["total_credits"] = 0
            app_state["last_email"] = None
        add_log("[DB SIFIRLAMA] Veritabanı başarıyla sıfırlandı ve tablo baştan kuruldu!", level="WARN")
        return jsonify({"status": "ok", "message": res_msg})
    else:
        add_log(f"[DB HATA] Veritabanı sıfırlanırken hata: {res_msg}", level="ERROR")
        return jsonify({"status": "error", "message": res_msg}), 500


@app.route("/api/clear_logs", methods=["POST"])
def clear_logs():
    """Arayüzdeki log geçmişini temizler."""
    with log_lock:
        logs_history.clear()
    add_log("[SİSTEM] Log geçmişi temizlendi.", level="INFO")
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    db_ok, db_msg = check_neon_db()
    print("==================================================")
    print("  CyberLink MyEdit Online - Flask Web Sunucusu")
    print(f"  Veritabanı (Neon): {'BAĞLI' if db_ok else 'BEKLENİYOR (' + db_msg + ')'}")
    print("  Arayüz: http://127.0.0.1:5000")
    print("==================================================")
    app.run(host="0.0.0.0", port=5000, debug=False)
