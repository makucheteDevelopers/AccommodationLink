"""
Accommodation Link — single-file MVP (Gold & Navy theme)
Run: python app.py  ->  http://127.0.0.1:5000
Admin: admin@accommodationlink.co.zw / Admin@123
"""
import os, sqlite3, secrets, string, re, time, json
from functools import wraps
from flask import (Flask, request, session, redirect, url_for, render_template_string,
                   flash, g, send_from_directory, abort, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "accommodation_link.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "pdf"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-on-render-to-a-long-random-key-12345")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

# ---------- DB ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    d = g.pop("db", None)
    if d:
        d.close()

def q(sql, args=(), one=False):
    cur = db().execute(sql, args)
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv

def ex(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()
    return cur.lastrowid

def init_db():
    with app.app_context():
        d = db()
        d.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL CHECK(role IN ('admin','landlord','agent')),
          full_name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, phone TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL, verification_status TEXT DEFAULT 'unverified',
          verified_badge INTEGER DEFAULT 0, blacklisted INTEGER DEFAULT 0, bio TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS verifications(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
          id_doc_url TEXT, title_deed_url TEXT, agent_license_url TEXT,
          status TEXT DEFAULT 'pending', admin_note TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS pricing_tiers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          type TEXT NOT NULL CHECK(type IN ('per_listing','subscription')), duration_days INTEGER NOT NULL,
          listing_limit INTEGER, price_usd REAL NOT NULL, price_zwg REAL, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS listings(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
          type TEXT NOT NULL CHECK(type IN ('rent','sale','shop')), title TEXT NOT NULL, description TEXT,
          price REAL NOT NULL, currency TEXT DEFAULT 'USD', rooms INTEGER, amenities TEXT,
          city TEXT NOT NULL, suburb TEXT, lat REAL, lng REAL, contact_name TEXT, contact_phone TEXT,
          status TEXT DEFAULT 'draft', views INTEGER DEFAULT 0, clicks INTEGER DEFAULT 0,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP, expires_at TEXT);
        CREATE TABLE IF NOT EXISTS listing_photos(
          id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id INTEGER REFERENCES listings(id) ON DELETE CASCADE,
          url TEXT NOT NULL, watermarked INTEGER DEFAULT 0, sort_order INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS payments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER REFERENCES users(id),
          listing_id INTEGER REFERENCES listings(id), tier_id INTEGER REFERENCES pricing_tiers(id),
          type TEXT NOT NULL, amount REAL NOT NULL, currency TEXT DEFAULT 'USD', ref_code TEXT UNIQUE NOT NULL,
          proof_url TEXT, txn_id TEXT, status TEXT DEFAULT 'awaiting_verification', admin_note TEXT,
          verified_by INTEGER, verified_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS subscriptions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER REFERENCES users(id),
          tier_id INTEGER REFERENCES pricing_tiers(id), payment_id INTEGER REFERENCES payments(id),
          starts_at TEXT DEFAULT CURRENT_TIMESTAMP, ends_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reports(
          id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id INTEGER REFERENCES listings(id) ON DELETE CASCADE,
          reason TEXT NOT NULL, note TEXT, reporter_ip TEXT, status TEXT DEFAULT 'open',
          actioned_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS conversations(
          id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL,
          listing_id INTEGER REFERENCES listings(id) ON DELETE CASCADE, agent_id INTEGER REFERENCES users(id),
          tenant_name TEXT, tenant_phone TEXT, tenant_email TEXT, status TEXT DEFAULT 'open',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_activity TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
          sender TEXT NOT NULL CHECK(sender IN ('tenant','agent')), body TEXT NOT NULL,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP, read_at TEXT);
        CREATE TABLE IF NOT EXISTS audit_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT, target_table TEXT,
          target_id INTEGER, meta TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
      if not q("SELECT 1 FROM users WHERE role='admin'", one=True):
            ex("INSERT INTO users(role,full_name,email,phone,password_hash,verification_status,verified_badge) VALUES('admin','Administrator','admin@accommodationlink.co.zw','0770000000',?,'verified',1)", (generate_password_hash("Admin@123"),))
        if not q("SELECT 1 FROM pricing_tiers", one=True):
            ex("INSERT INTO pricing_tiers(name,type,duration_days,price_usd) VALUES('Per-listing','per_listing',30,5.0)")
            ex("INSERT INTO pricing_tiers(name,type,duration_days,listing_limit,price_usd) VALUES('Monthly Unlimited','subscription',30,50,20.0)")
        defaults = {
            "ecocash_number": "0771234567", "ecocash_receiver_name": "Tendai Moyo",
            "whatsapp_support_number": "263771234567", "whatsapp_support_display_name": "Accommodation Link Support",
            "site_email": "info@accommodationlink.co.zw", "default_currency": "USD",
            "watermark_text": "Accommodation Link", "site_credit": "Powered by Tinotenda Tarsh Makuchete",
            "developer_name": "Tinotenda Tarsh Makuchete", "developer_whatsapp": "263771234567",
            "developer_email": "info@accommodationlink.co.zw",
        }
        for k, v in defaults.items():
            if not q("SELECT 1 FROM settings WHERE key=?", (k,), one=True): ex("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))
        d.commit()

# ---------- Helpers ----------
def get_setting(key, default=""):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else default

def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

def current_user():
    uid = session.get("uid")
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True) if uid else None

def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            u = current_user()
            if not u: return redirect(url_for("login"))
            if role and u["role"] != role: abort(403)
            return fn(*a, **k)
        return wrapper
    return deco

def admin_required(fn): return login_required("admin")(fn)

def gen_ref():
    while True:
        code = "AL-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        if not q("SELECT 1 FROM payments WHERE ref_code=?", (code,), one=True): return code

def gen_token():
    while True:
        t = "CONV-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        if not q("SELECT 1 FROM conversations WHERE token=?", (t,), one=True): return t

def allowed_file(fn): return "." in fn and fn.rsplit(".", 1)[1].lower() in ALLOWED_EXT

def watermark_image(filepath, text):
    try:
        if filepath.lower().endswith(".pdf"): return
        img = Image.open(filepath).convert("RGBA")
        txt = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(txt)
        try: font = ImageFont.truetype("arial.ttf", max(20, img.size[0] // 30))
        except Exception: font = ImageFont.load_default()
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception: w, h = len(text) * 12, 20
        pos = (img.size[0] - w - 20, img.size[1] - h - 20)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1): draw.text((pos[0] + dx, pos[1] + dy), text, fill=(0, 0, 0, 120), font=font)
        draw.text(pos, text, fill=(255, 255, 255, 180), font=font)
        Image.alpha_composite(img, txt).convert("RGB").save(filepath)
    except Exception as e: print("Watermark failed:", e)

def save_upload(file, prefix):
    if not file or not file.filename or not allowed_file(file.filename): return None
    ext = file.filename.rsplit(".", 1)[1].lower()
    name = f"{prefix}_{secrets.token_hex(8)}.{ext}"
    path = os.path.join(UPLOAD_DIR, name)
    file.save(path)
    if ext in {"png", "jpg", "jpeg", "webp"}: watermark_image(path, get_setting("watermark_text", "Accommodation Link"))
    return name

@app.route("/uploads/<path:name>")
def uploaded(name): return send_from_directory(UPLOAD_DIR, name)

def active_subscription(uid):
    return q("SELECT * FROM subscriptions WHERE user_id=? AND ends_at > datetime('now') ORDER BY ends_at DESC LIMIT 1", (uid,), one=True)

_rl = {}
def rate_limit(key, max_hits, window_sec):
    now = time.time()
    hits = [t for t in _rl.get(key, []) if now - t < window_sec]
    if len(hits) >= max_hits: return False
    hits.append(now); _rl[key] = hits
    return True

def sanitize_text(s, max_len=2000):
    if not s: return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"https?://\S+", "[link removed]", s)
    return s.strip()[:max_len]

def wa_number(phone):
    if not phone: return ""
    p = re.sub(r"\D", "", phone)
    if p.startswith("0"): p = "263" + p[1:]
    return p

def tel_number(phone):
    if not phone: return ""
    p = re.sub(r"\D", "", phone)
    if p.startswith("0"): p = "+263" + p[1:]
    elif not p.startswith("+"): p = "+" + p
    return p

# ---------- Base Template ----------
BASE_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accommodation Link</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='12' fill='%230a1f44'/%3E%3Ctext x='32' y='44' font-family='Georgia,serif' font-size='32' font-weight='bold' text-anchor='middle' fill='%23d4af37'%3EAL%3C/text%3E%3C/svg%3E">
<style>
:root{--navy:#0a1f44;--navy2:#122a5c;--gold:#d4af37;--gold2:#e8c860;--bg:#f7f6f2;--card:#fff;--line:#e3dfd2;--muted:#6b7280;--brand:var(--gold);--brand2:var(--gold2);}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:#111;display:flex;flex-direction:column;min-height:100vh}
a{color:var(--navy);text-decoration:none}a:hover{color:var(--gold)}
header{background:linear-gradient(135deg,var(--navy) 0%,var(--navy2) 100%);color:#fff;padding:14px 22px;display:flex;align-items:center;gap:18px;flex-wrap:wrap;border-bottom:3px solid var(--gold);box-shadow:0 2px 12px rgba(10,31,68,.25)}
header .logo{font-family:Georgia,"Times New Roman",serif;font-weight:700;font-size:22px;letter-spacing:.5px;color:var(--gold);text-shadow:0 1px 2px rgba(0,0,0,.4)}
header .logo:hover{color:var(--gold2)}
header nav{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
header nav a{color:#eae6da;padding:7px 12px;border-radius:6px;font-size:14px;transition:.15s}
header nav a:hover{background:rgba(212,175,55,.18);color:var(--gold)}
main{max-width:1150px;margin:20px auto;padding:0 14px;flex:1;width:100%}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(10,31,68,.04)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
.listing{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#fff;transition:.15s}
.listing:hover{box-shadow:0 6px 20px rgba(10,31,68,.12);transform:translateY(-2px)}
.listing img{width:100%;height:170px;object-fit:cover;background:#ddd}
.listing .body{padding:10px}
.price{color:var(--navy);font-weight:800;font-size:17px}
.price::before{content:"◆ ";color:var(--gold);font-size:12px}
.btn{display:inline-block;background:var(--gold);color:var(--navy);padding:9px 14px;border-radius:8px;border:0;cursor:pointer;font-weight:700;transition:.15s}
.btn:hover{background:var(--gold2);color:var(--navy)}
.btn.sec{background:#eee7d3;color:var(--navy)}.btn.sec:hover{background:#e0d6b8}
.btn.danger{background:#c0392b;color:#fff}.btn.danger:hover{background:#a02f24}
.btn.wa{background:#25D366;color:#fff}.btn.wa:hover{background:#1eb85a}
.btn.call{background:var(--navy);color:#fff}.btn.call:hover{background:var(--navy2)}
.btn.sm{padding:6px 10px;font-size:13px}
input,select,textarea{padding:9px;border:1px solid var(--line);border-radius:8px;width:100%;font:inherit;background:#fff}
input:focus,select:focus,textarea:focus{outline:2px solid var(--gold);outline-offset:0;border-color:var(--gold)}
label{display:block;margin:8px 0 4px;font-weight:600;font-size:13px;color:var(--navy)}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1;min-width:160px}
.flash{background:#f4ecd0;border:1px solid var(--gold);color:var(--navy);padding:10px;border-radius:8px;margin-bottom:12px}
.flash.err{background:#fde8e8;border-color:#f5b5b5;color:#7a1f1f}
.badge{display:inline-block;background:var(--navy);color:var(--gold);font-size:11px;padding:3px 9px;border-radius:99px;font-weight:700;letter-spacing:.3px}
.badge.pending{background:#fff4d6;color:#8a6200}.badge.rej{background:#fde8e8;color:#c0392b}
.badge.featured{background:#16a34a;color:#fff}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#faf7ee;color:var(--navy);font-weight:700}
.muted{color:var(--muted);font-size:13px}
h1,h2{margin:0 0 12px;color:var(--navy)}
h1{font-family:Georgia,"Times New Roman",serif;letter-spacing:.3px}
.hero{background:linear-gradient(135deg,var(--navy) 0%,var(--navy2) 55%,#3a2a00 100%);color:#fff;padding:40px 22px;border-radius:12px;margin-bottom:16px;border-bottom:3px solid var(--gold);position:relative;overflow:hidden}
.hero::after{content:"";position:absolute;top:0;right:-80px;width:280px;height:280px;background:radial-gradient(circle,rgba(212,175,55,.35) 0%,transparent 70%);pointer-events:none}
.hero h1{font-size:28px;margin-bottom:6px;color:#fff;font-family:Georgia,serif;letter-spacing:.4px}
.hero h1 em{color:var(--gold);font-style:normal}
.searchbar{background:#fff;padding:14px;border-radius:10px;margin-top:16px;display:flex;gap:10px;flex-wrap:wrap;box-shadow:0 4px 16px rgba(0,0,0,.15)}
.searchbar>*{flex:1;min-width:140px}
.modal-bg{display:none;position:fixed;inset:0;background:rgba(10,31,68,.65);z-index:99;align-items:center;justify-content:center;padding:20px}
.modal{background:#fff;padding:20px;border-radius:10px;max-width:520px;width:100%;max-height:90vh;overflow:auto;border-top:4px solid var(--gold)}
.chat-box{background:#fafbfc;border:1px solid var(--line);border-radius:10px;padding:14px;max-height:60vh;overflow:auto}
.msg{padding:8px 12px;border-radius:8px;margin-bottom:8px;max-width:80%;word-wrap:break-word}
.msg.tenant{background:#e7f0ff;margin-right:auto}.msg.agent{background:#f4ecd0;margin-left:auto;border-left:3px solid var(--gold)}
.msg .t{font-size:11px;color:var(--muted);display:block;margin-top:4px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--gold);margin-left:4px;box-shadow:0 0 0 2px rgba(212,175,55,.3)}
.warn{background:#fff8e1;border:1px solid var(--gold);border-left:4px solid var(--gold);padding:8px 12px;border-radius:6px;font-size:13px;margin-bottom:10px;color:#5a4500}
footer{text-align:center;padding:20px 14px;margin-top:28px;background:var(--navy);color:#d6d1c2;font-size:12px;border-top:3px solid var(--gold)}
footer em{color:var(--gold);font-style:italic;letter-spacing:.3px}
footer a{color:#d6d1c2;text-decoration:underline}footer a:hover{color:var(--gold)}
</style></head><body>
<!-- FORCE TERMS POPUP ON OPEN -->
<div id="forceTerms" style="position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:99999;display:flex;align-items:center;justify-content:center;padding:16px">
  <div style="background:white;max-width:520px;width:100%;max-height:85vh;overflow:auto;border-radius:16px;padding:22px">
    <h2 style="margin:0 0 6px">Welcome to Accommodation Link 🏠</h2>
    <p style="font-size:13px;color:#64748B;margin:0 0 12px">Please read and agree to continue</p>
    <div style="font-size:13px;line-height:1.7;color:#334155;background:#F8FAFC;border-radius:10px;padding:14px;max-height:280px;overflow:auto">
  <p><b>1. Acceptance:</b> By using Accommodation Link you agree to these Terms. If you do not agree, do not use the site.</p>
  <p><b>2. Who We Are:</b> Accommodation Link is a listing platform in Zimbabwe connecting landlords / agents with tenants. We do NOT own the houses listed and we are NOT a party to any rental agreement.</p>
  <p><b>3. Accounts:</b> You must provide true name, phone number and suburb. You are responsible for your account. One account per person. Fake accounts will be banned.</p>
  <p><b>4. Landlord / Agent Rules:</b> Only list properties you own or are authorised to list. Price, suburb, city, rooms, type, photos and availability must be true and up to date. Remove listing when taken. No fake photos, no bait pricing, no charging viewing fees through this site.</p>
  <p><b>5. Tenant Rules:</b> Only contact landlords about listings you are genuinely interested in. No spam, no harassment, no abusive language on WhatsApp / calls. Be on time for viewings or cancel in advance.</p>
  <p><b>6. Viewings & Payments - VERY IMPORTANT:</b> ALWAYS view the house in person before paying ANY money. Never send deposit, rent or viewing fee via EcoCash / bank before viewing. Meet at the property, check ID of landlord, ask for proof of ownership / authority. Accommodation Link does NOT handle rent, deposits or refunds.</p>
  <p><b>7. Scams & Safety:</b> If someone asks for money before viewing, asks you to pay to a different name, or price is too good to be true - STOP and report via WhatsApp support. We will ban scammers but you must verify before paying.</p>
  <p><b>8. Boost / Featured Listings:</b> Paid Boost / Featured only gives more visibility. It does NOT guarantee tenants and is non-refundable once activated, unless listing was removed by us in error.</p>
  <p><b>9. Prohibited Content:</b> No porn, no drugs, no violence, no hate speech, no discrimination, no posting other people's phone numbers or photos without permission.</p>
  <p><b>10. Privacy:</b> Your name, phone and listings are visible to other users so they can contact you about houses. We do NOT sell your data. We may use your contact to send booking / listing updates.</p>
  <p><b>10. Privacy - Personal Info:</b> Your name, phone and listings are visible to other users so they can contact you about houses. We do NOT sell your data. We may use your contact to send booking / listing updates.</p>
<p><b>10B. Privacy - Documents Uploaded:</b> If you upload documents for verification - e.g. National ID, passport, proof of ownership, title deed, lease agreement, or agent letter - those documents are PRIVATE and are NEVER shown to other users, tenants or landlords. Only Admin / Verification Team can see them, only to verify you are a real landlord / agent. We do NOT share, sell, post or forward your documents to anyone. Documents are stored securely and are only used for verification. You may request deletion of your documents after verification by contacting WhatsApp Support. Do NOT upload bank cards, PINs or passwords - we will never ask for them.</p>
  <p><b>11. Removal & Termination:</b> We may remove any listing or ban any account without notice for false info, scam, abuse or breaking these Terms. No refund for Boost if banned for breaking Terms.</p>
  <p><b>12. Liability:</b> You use the site at your own risk. We are not liable for loss, theft, damage, or disputes between landlord and tenant. Any agreement is between landlord and tenant only.</p>
  <p><b>13. Changes:</b> We may update these Terms at any time. Continued use means you accept the new Terms.</p>
  <p><b>14. Law:</b> These Terms are governed by the laws of Zimbabwe.</p>
  <p><b>15. Contact:</b> For help, scams or complaints, contact us via WhatsApp Support on the site.</p>
</div>
    <label style="display:flex;gap:8px;align-items:flex-start;font-size:13px;margin:14px 0">
      <input type="checkbox" id="forceAgreeCheck" onchange="document.getElementById('forceAgreeBtn').disabled=!this.checked">
      <span>I have read and agree to the Terms & Conditions</span>
    </label>
    <button id="forceAgreeBtn" disabled onclick="document.getElementById('forceTerms').style.display='none';document.body.style.overflow='auto';sessionStorage.setItem('al_agreed','1')" style="width:100%;background:#16A34A;color:white;border:none;padding:14px;border-radius:10px;font-weight:800;font-size:15px;cursor:pointer;opacity:0.5">I AGREE - ENTER SITE</button>
    <p style="font-size:11px;color:#94A3B8;text-align:center;margin:10px 0 0">You must agree to use Accommodation Link</p>
  </div>
</div>
<script>
// Show on every fresh open, hide if already agreed in this session
if(sessionStorage.getItem('al_agreed')==='1'){
  document.getElementById('forceTerms').style.display='none';
} else {
  document.body.style.overflow='hidden';
}
// Enable button colour when checked
document.getElementById('forceAgreeCheck').addEventListener('change', function(){
  var btn=document.getElementById('forceAgreeBtn');
  btn.style.opacity=this.checked?'1':'0.5';
});
</script>
<header>
  <a href="/" class="logo">Accommodation&nbsp;Link</a>
  <nav>
    <a href="/">Browse</a><a href="/saved">Saved</a>
    {% if current_user %}
      {% if current_user.role == 'admin' %}<a href="/admin">Admin</a>
      {% else %}<a href="/dashboard">Dashboard</a><a href="/dashboard/messages">Messages{% if unread_count %}<span class="dot"></span>{% endif %}</a>{% endif %}
      <a href="/logout">Logout ({{ current_user.full_name }})</a>
    {% else %}<a href="/login">Login</a><a href="/register">Register</a>{% endif %}
    <a href="https://wa.me/{{ get_setting('whatsapp_support_number') }}" target="_blank" class="btn wa sm">💬 Support</a>
  </nav>
</header>
<main>
{% with msgs = get_flashed_messages(with_categories=true) %}
  {% for cat, m in msgs %}<div class="flash {{ 'err' if cat=='error' }}">{{ m }}</div>{% endfor %}
{% endwith %}
{% block content %}{% endblock %}
</main>
<footer><em>{{ get_setting('site_credit') }}</em> &nbsp;&nbsp; <a href="/about-developer">About the developer</a><br><br><p style="font-size:12px; color:#d6d1c2;">DISCLAIMER: Accommodation Link is advertising only. We do NOT collect rent. VIEW Before You Pay.<br><a href="/terms">Terms & Conditions</a> | <a href="/privacy">Privacy Policy</a></p></footer>
<div id="termsPopup" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.7); z-index:10000; justify-content:center; align-items:center;">
  <div style="background:white; color:#111; max-width:500px; width:90%; padding:20px; border-radius:10px;">
    <h3 style="color:#111;">Welcome to Accommodation Link</h3>
    <ul style="color:#111; font-size:14px;">
      <li>We are advertising ONLY - we do NOT collect rent</li>
      <li><b>VIEW Before You Pay</b> - Never pay before viewing</li>
      <li>Title Deed / Licence is for verification ONLY, never shown to public</li>
      <li>We do NOT sell your data. You can ask for deletion anytime.</li>
    </ul>
    <p><a href="/terms" target="_blank">Read Full Terms & Privacy</a></p>
    <label style="color:#111;"><input type="checkbox" id="agreeTerms"> I Agree to Terms & Conditions</label><br><br>
    <button onclick="acceptTerms()" class="btn wa" style="width:100%;">I Agree - Continue</button>
  </div>
</div>
<script>
function acceptTerms(){
  if(!document.getElementById('agreeTerms').checked){ alert('Please tick I Agree first'); return; }
  localStorage.setItem('acclink_terms','accepted');
  document.getElementById('termsPopup').style.display='none';
}
if(!localStorage.getItem('acclink_terms')){
  document.getElementById('termsPopup').style.display='flex';
}
</script>
</body></html>
"""
@app.route('/terms')
def terms():
    content = """
    <h2>Terms & Conditions - Accommodation Link</h2>
    <p><b>Last updated:</b> 21 Nov 2026</p>
    <h3>1. DISCLAIMER</h3>
    <p>Accommodation Link is advertising only. We do NOT own properties, we do NOT collect rent. Any rent agreement is between Tenant and Landlord.</p>
    <p style="color:red; font-weight:bold;">VIEW BEFORE YOU PAY - Never send money before you have viewed the property. We are NOT liable if you pay without viewing.</p>
    <h3>2. Verification Documents</h3>
    <p>Title Deed / Agent Licence is for verification ONLY. It is NEVER shown to public. Tenants only see Verified Badge.</p>
    <p>By ticking I Agree, you give consent under Cyber and Data Protection Act [Chapter 12:07] for verification only. You can ask for deletion anytime via WhatsApp, we delete within 7 days. We do NOT sell your data.</p>
    <h3>3. Scam Reports</h3>
    <p>Tenants can Report Scam. Reported listings are hidden while Admin investigates.</p>
    <p><a href="/">Back to Home</a></p>
    """
    return render_template_string(BASE_HTML.replace("{% block content %}{% endblock %}", content))

@app.route('/privacy')
def privacy():
    return terms()
@app.context_processor
def inject():
    u = current_user()
    uc = 0
    if u and u["role"] in ("agent", "landlord"):
        uc = q("SELECT COUNT(*) c FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.agent_id=? AND m.sender='tenant' AND m.read_at IS NULL", (u["id"],), one=True)["c"]
    return dict(current_user=u, get_setting=get_setting, unread_count=uc, wa_number=wa_number, tel_number=tel_number)

# Helper to render pages cleanly without 'block defined twice'
def render_page(html, **kwargs):
    return render_template_string(BASE_HTML.replace("{% block content %}{% endblock %}", html), **kwargs)

# ================= PUBLIC =================
@app.route("/")
def home():
    a = request.args
    sql = """SELECT l.*, u.verified_badge FROM listings l JOIN users u ON u.id=l.user_id WHERE l.status='active' AND u.blacklisted=0"""
    args = []
    if a.get("city"): sql += " AND lower(l.city)=?"; args.append(a["city"].lower())
    if a.get("suburb"): sql += " AND lower(l.suburb) LIKE ?"; args.append(f"%{a['suburb'].lower()}%")
    if a.get("type"): sql += " AND l.type=?"; args.append(a["type"])
    if a.get("min_price", type=float) is not None: sql += " AND l.price>=?"; args.append(a["min_price"])
    if a.get("max_price", type=float) is not None: sql += " AND l.price<=?"; args.append(a["max_price"])
    if a.get("rooms", type=int) is not None: sql += " AND l.rooms>=?"; args.append(a["rooms"])
    sql += " ORDER BY (l.views + l.clicks*2) DESC, l.created_at DESC"
    listings = q(sql, args)
    cities = [r["city"] for r in q("SELECT DISTINCT city FROM listings WHERE status='active' ORDER BY city")]
    return render_page("""
    <div class="hero">
      <h1>Accommodation <em>Link</em> — Find your next home in Zimbabwe</h1>
      <div class="muted" style="color:#eae6da">Rentals · Houses for sale · Shops · Gweru &amp; beyond</div>
      <form class="searchbar" method="get">
        <input name="city" placeholder="City" value="{{ request.args.city or '' }}" list="cl">
        <datalist id="cl">{% for c in cities %}<option value="{{ c }}">{% endfor %}</datalist>
        <input name="suburb" placeholder="Suburb" value="{{ request.args.suburb or '' }}">
        <select name="type"><option value="">Any type</option>
          <option value="rent" {{ 'selected' if request.args.type=='rent' }}>For rent</option>
          <option value="sale" {{ 'selected' if request.args.type=='sale' }}>For sale</option>
          <option value="shop" {{ 'selected' if request.args.type=='shop' }}>Shop</option></select>
        <input name="min_price" type="number" placeholder="Min price" value="{{ request.args.min_price or '' }}">
        <input name="max_price" type="number" placeholder="Max price" value="{{ request.args.max_price or '' }}">
        <input name="rooms" type="number" placeholder="Min rooms" value="{{ request.args.rooms or '' }}">
        <button class="btn">Search</button>
      </form>
    </div>
    <h2>🔥 Trending / Latest</h2>
    <div class="grid">
      {% for l in listings %}
      <a class="listing" href="/listing/{{ l.id }}">
        <img src="/photo_of/{{ l.id }}" alt="">
        <div class="body">
          <div class="price">{{ l.currency }} {{ '%.2f'|format(l.price) }}</div>
          <div style="font-weight:700;margin:4px 0">{{ l.title }}</div>
          <div class="muted">📍 {{ l.suburb }}{% if l.suburb %}, {% endif %}{{ l.city }} · {{ l.rooms or '-' }} rooms</div>
          {% if l.verified_badge %}<span class="badge">✅ Verified</span>{% endif %}
          <span class="muted"> · {{ l.views }} views</span>
        </div></a>
      {% else %}<p class="muted">No listings yet.</p>{% endfor %}
    </div>
    """, listings=listings, cities=cities)

@app.route("/photo_of/<int:lid>")
def photo_of(lid):
    r = q("SELECT url FROM listing_photos WHERE listing_id=? ORDER BY sort_order LIMIT 1", (lid,), one=True)
    if not r: abort(404)
    return redirect(f"/uploads/{r['url']}")

@app.route("/listing/<int:lid>")
def listing_detail(lid):
    l = q("SELECT l.*, u.verified_badge, u.full_name AS agent_name, u.role AS agent_role, u.id AS aid FROM listings l JOIN users u ON u.id=l.user_id WHERE l.id=?", (lid,), one=True)
    if not l or l["status"] not in ("active", "expired"): abort(404)
    photos = q("SELECT * FROM listing_photos WHERE listing_id=? ORDER BY sort_order", (lid,))
    ex("UPDATE listings SET views=views+1 WHERE id=?", (lid,))
    return render_page("""
    <div class="card">
      <h1>{{ l.title }}</h1>
      <div class="muted">📍 {{ l.suburb }}{% if l.suburb %}, {% endif %}{{ l.city }} · {{ l.type|upper }} · {{ l.rooms or '-' }} rooms · by <a href="/agent/{{ l.aid }}">{{ l.agent_name }}</a></div>
      <div class="price" style="font-size:24px;margin:8px 0">{{ l.currency }} {{ '%.2f'|format(l.price) }}</div>
      {% if l.verified_badge %}<span class="badge">✅ Verified</span>{% endif %}
      <div class="grid" style="margin-top:14px">{% for p in photos %}<img src="/uploads/{{ p.url }}" style="width:100%;border-radius:8px;max-height:340px;object-fit:cover">{% endfor %}</div>
      <p style="margin-top:14px;white-space:pre-wrap">{{ l.description or '' }}</p>
      {% if l.amenities %}<p><b>Amenities:</b> {{ l.amenities }}</p>{% endif %}
      {% if l.lat and l.lng %}<p><b>Map:</b> <a target="_blank" href="https://www.google.com/maps?q={{ l.lat }},{{ l.lng }}">Open in Google Maps ({{ l.lat }}, {{ l.lng }})</a></p>{% endif %}
      <div class="row" style="margin-top:14px">
        <a class="btn wa" target="_blank" onclick="fetch('/listing/{{ l.id }}/track_click',{method:'POST'})" href="https://wa.me/{{ wa_number(l.contact_phone) }}?text=Hi%2C%20I%27m%20interested%20in%20{{ l.title|urlencode }}%20on%20Accommodation%20Link">💬 WhatsApp</a>
       <a class="btn wa" target="_blank" onclick="fetch('/listing/{{ l.id }}/track_click',{method:'POST'})" href="https://wa.me/{{ wa_number(l.contact_phone) }}?text=Hi%2C%20I%20saw%20your%20house%20in%20{{ l.suburb }}%2C%20{{ l.city }}%20on%20Accommodation%20Link.%20Details%3A%20{{ l.rooms }}%20Bedroom%20{{ l.type }}%20-%20{{ l.currency }}%20{{ '%.2f'|format(l.price) }}%2Fmonth.%20Is%20it%20still%20available%3F%20Can%20I%20view%3F">💬 WhatsApp</a>
        <a class="btn call" href="tel:{{ tel_number(l.contact_phone) }}" onclick="fetch('/listing/{{ l.id }}/track_click',{method:'POST'})">📞 Call</a>
        <button class="btn sec" onclick="saveFav({{ l.id }})">❤ Save</button>
        <button class="btn danger" onclick="document.getElementById('reportBox').style.display='block'">⚠ Report</button>
      </div>
      <div id="reportBox" style="display:none;margin-top:14px">
        <form method="post" action="/report/{{ l.id }}">
          <label>Reason</label><select name="reason"><option>Fake listing</option><option>Wrong price</option><option>Already taken</option><option>Scammer</option><option>Other</option></select>
          <label>Note (optional)</label><textarea name="note" rows="2"></textarea>
          <div style="margin-top:8px"><button class="btn">Submit report</button></div>
        </form>
      </div>
    </div>
    <div id="msgModal" class="modal-bg">
      <div class="modal">
        <h2>Send message to {{ l.agent_name }}</h2>
        <div class="warn">⚠️ Never pay before viewing. Accommodation Link does not collect rent or deposits.</div>
        <form method="post" action="/listing/{{ l.id }}/message">
          <label>Your name (optional)</label><input name="tenant_name">
          <label>Your phone (optional — enables WhatsApp nudges)</label><input name="tenant_phone">
          <label>Your email (optional)</label><input name="tenant_email" type="email">
          <label>Message</label><textarea name="body" rows="4" required></textarea>
          <input type="text" name="hp" style="display:none" tabindex="-1" autocomplete="off">
          <div style="margin-top:12px"><button class="btn">Send</button><button type="button" class="btn sec" onclick="document.getElementById('msgModal').style.display='none'">Cancel</button></div>
        </form>
      </div>
    </div>
    <script>
      function saveFav(id){ let s = JSON.parse(localStorage.getItem('al_saved')||'[]'); if(!s.includes(id)) s.push(id); localStorage.setItem('al_saved', JSON.stringify(s)); alert('Saved!'); }
    </script>
    """, l=l, photos=photos)

@app.route("/listing/<int:lid>/track_click", methods=["POST"])
def track_click(lid): ex("UPDATE listings SET clicks=clicks+1 WHERE id=?", (lid,)); return ("", 204)

@app.route("/listing/<int:lid>/message", methods=["POST"])
def listing_message(lid):
    if not rate_limit(f"msg:{request.remote_addr}", 3, 3600): flash("Too many messages. Try again in an hour.", "error"); return redirect(f"/listing/{lid}")
    if request.form.get("hp"): abort(400)
    l = q("SELECT * FROM listings WHERE id=? AND status='active'", (lid,), one=True)
    if not l: abort(404)
    body = sanitize_text(request.form.get("body", ""))
    if not body: flash("Message empty.", "error"); return redirect(f"/listing/{lid}")
    token = gen_token()
    cid = ex("INSERT INTO conversations(token,listing_id,agent_id,tenant_name,tenant_phone,tenant_email) VALUES(?,?,?,?,?,?)", (token, lid, l["user_id"], request.form.get("tenant_name", ""), request.form.get("tenant_phone", ""), request.form.get("tenant_email", "")))
    ex("INSERT INTO messages(conversation_id,sender,body) VALUES(?,?,?)", (cid, "tenant", body))
    flash("Message sent! Save this link to see replies."); return redirect(f"/chat/{token}")

@app.route("/chat/<token>", methods=["GET", "POST"])
def chat_guest(token):
    c = q("SELECT * FROM conversations WHERE token=?", (token,), one=True)
    if not c: abort(404)
    l = q("SELECT * FROM listings WHERE id=?", (c["listing_id"],), one=True)
    agent = q("SELECT * FROM users WHERE id=?", (c["agent_id"],), one=True)
    if request.method == "POST":
        if not rate_limit(f"chat:{request.remote_addr}", 30, 3600): flash("Slow down.", "error"); return redirect(f"/chat/{token}")
        body = sanitize_text(request.form.get("body", ""))
        if body:
            ex("INSERT INTO messages(conversation_id,sender,body) VALUES(?,?,?)", (c["id"], "tenant", body))
            ex("UPDATE conversations SET last_activity=datetime('now') WHERE id=?", (c["id"],))
        return redirect(f"/chat/{token}")
    msgs = q("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (c["id"],))
    ex("UPDATE messages SET read_at=datetime('now') WHERE conversation_id=? AND sender='agent' AND read_at IS NULL", (c["id"],))
    return render_page("""
    <div class="card">
      <h1>Conversation with {{ agent.full_name }}</h1>
      <div class="muted">About: <a href="/listing/{{ l.id }}">{{ l.title }}</a></div>
      <div class="warn" style="margin-top:10px">⚠️ Never pay before viewing. Accommodation Link does not collect rent or deposits.</div>
      <div class="chat-box" style="margin-top:10px">
        {% for m in msgs %}<div class="msg {{ m.sender }}">{{ m.body }}<span class="t">{{ m.sender|capitalize }} · {{ m.created_at }}</span></div>{% endfor %}
      </div>
      <form method="post" style="margin-top:10px"><textarea name="body" rows="3" placeholder="Type your reply…" required></textarea><div style="margin-top:8px"><button class="btn">Reply</button></div></form>
      <p class="muted" style="margin-top:10px">🔗 Bookmark this link to see replies. Or contact agent on <a href="https://wa.me/{{ wa_number(l.contact_phone) }}?text=Hi%2C%20re%3A%20{{ l.title|urlencode }}" target="_blank">WhatsApp</a>.</p>
    </div>
    <script>setTimeout(()=>location.reload(), 20000);</script>
    """, c=c, l=l, agent=agent, msgs=msgs)

@app.route("/agent/<int:aid>")
def agent_profile(aid):
    u = q("SELECT * FROM users WHERE id=? AND role!='admin' AND blacklisted=0", (aid,), one=True)
    if not u: abort(404)
    listings = q("SELECT * FROM listings WHERE user_id=? AND status='active' ORDER BY id DESC", (aid,))
    return render_page("""
    <div class="card">
      <h1>{{ u.full_name }} {% if u.verified_badge %}<span class="badge">✅ Verified</span>{% endif %}</h1>
      <div class="muted">{{ u.role|capitalize }}{% if u.bio %} · {{ u.bio }}{% endif %}</div>
      <div class="row" style="margin-top:10px"><a class="btn wa" target="_blank" href="https://wa.me/{{ wa_number(u.phone) }}">💬 WhatsApp</a><a class="btn call" href="tel:{{ tel_number(u.phone) }}">📞 Call</a></div>
    </div>
    <h2>Active listings ({{ listings|length }})</h2>
    <div class="grid">
      {% for l in listings %}<a class="listing" href="/listing/{{ l.id }}"><img src="/photo_of/{{ l.id }}"><div class="body"><div class="price">{{ l.currency }} {{ '%.2f'|format(l.price) }}</div><div style="font-weight:700">{{ l.title }}</div><div class="muted">📍 {{ l.suburb }}{% if l.suburb %}, {% endif %}{{ l.city }}</div></div></a>
      {% else %}<p class="muted">No active listings.</p>{% endfor %}
    </div>
    """, u=u, listings=listings)

@app.route("/about-developer")
def about_developer():
    return render_page("""
    <div class="card">
      <h1>About the developer</h1>
      <p><b>{{ get_setting('developer_name') }}</b></p>
      <p class="muted">Designer &amp; developer of Accommodation Link.</p>
      <p>Need a custom website, bug fix, or integration? Get in touch:</p>
      <div class="row" style="margin-top:10px"><a class="btn wa" target="_blank" href="https://wa.me/{{ get_setting('developer_whatsapp') }}">💬 WhatsApp</a><a class="btn call" href="tel:{{ tel_number(get_setting('developer_whatsapp')) }}">📞 Call</a><a class="btn sec" href="mailto:{{ get_setting('developer_email') }}">✉️ Email</a></div>
    </div>
    """)

@app.route("/saved")
def saved():
    return render_page("""
    <h1>Saved listings</h1><p class="muted">Saved locally on this device.</p><div id="out" class="grid"></div>
    <script>
    (async()=>{ let ids = JSON.parse(localStorage.getItem('al_saved')||'[]'); let out = document.getElementById('out'); if(ids.length===0){out.innerHTML='<p class="muted">No saved listings yet.</p>';return;}
      for(const id of ids){ try{ const r = await fetch('/api/listing/'+id); if(!r.ok) continue; const l = await r.json();
          out.innerHTML += `<a class="listing" href="/listing/${l.id}"><img src="/photo_of/${l.id}"><div class="body"><div class="price">${l.currency} ${l.price}</div><div style="font-weight:700">${l.title}</div><div class="muted">📍 ${l.suburb||''}, ${l.city}</div></div></a>`; }catch(e){} }
    })();
    </script>
    """)

@app.route("/api/listing/<int:lid>")
def api_listing(lid):
    l = q("SELECT * FROM listings WHERE id=? AND status IN ('active','expired')", (lid,), one=True)
    if not l: abort(404)
    return jsonify(dict(l))

@app.route("/report/<int:lid>", methods=["POST"])
def report_listing(lid):
    l = q("SELECT id FROM listings WHERE id=?", (lid,), one=True)
    if not l: abort(404)
    ip = request.remote_addr or ""
    if q("SELECT 1 FROM reports WHERE listing_id=? AND reporter_ip=? AND status='open'", (lid, ip), one=True): flash("Already reported.", "error"); return redirect(f"/listing/{lid}")
    ex("INSERT INTO reports(listing_id,reason,note,reporter_ip) VALUES(?,?,?,?)", (lid, request.form.get("reason", "Other"), request.form.get("note", ""), ip))
    flash("Thanks — our team will review."); return redirect(f"/listing/{lid}")

# ================= AUTH =================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        role = request.form.get("role")
        if role not in ("landlord", "agent"): flash("Choose Landlord or Agent.", "error"); return redirect("/register")
        name, email, phone, pw = request.form.get("full_name", "").strip(), request.form.get("email", "").strip().lower(), request.form.get("phone", "").strip(), request.form.get("password", "")
        if not (name and email and phone and pw): flash("All fields required.", "error"); return redirect("/register")
        if q("SELECT 1 FROM users WHERE email=? OR phone=?", (email, phone), one=True): flash("Email or phone already registered.", "error"); return redirect("/register")
        uid = ex("INSERT INTO users(role,full_name,email,phone,password_hash,verification_status) VALUES(?,?,?,?,?,'pending')", (role, name, email, phone, generate_password_hash(pw)))
        session["uid"] = uid; flash("Registered. Upload verification docs."); return redirect("/verify")
    return render_page("""
    <div class="card" style="max-width:520px;margin:0 auto">
      <h1>Create account</h1>
      <form method="post">
        <label>I am a</label><select name="role"><option value="landlord">Landlord</option><option value="agent">Agent</option></select>
        <label>Full name</label><input name="full_name" required><label>Email</label><input name="email" type="email" required>
        <label>Phone</label><input name="phone" required><label>Password</label><input name="password" type="password" required>
        <div style="margin-top:12px"><button class="btn">Register</button></div>
      </form>
      <p class="muted" style="margin-top:10px">Already registered? <a href="/login">Login</a></p>
    </div>
    """)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email, pw = request.form.get("email", "").strip().lower(), request.form.get("password", "")
        u = q("SELECT * FROM users WHERE email=?", (email,), one=True)
        if u and check_password_hash(u["password_hash"], pw):
            if u["blacklisted"]: flash("Account suspended.", "error"); return redirect("/login")
            session["uid"] = u["id"]; return redirect("/admin" if u["role"] == "admin" else "/dashboard")
        flash("Invalid credentials.", "error")
    return render_page("""
    <div class="card" style="max-width:420px;margin:0 auto">
      <h1>Login</h1>
      <form method="post"><label>Email</label><input name="email" type="email" required><label>Password</label><input name="password" type="password" required><div style="margin-top:12px"><button class="btn">Login</button></div></form>
    </div>
    """)

@app.route("/logout")
def logout(): session.clear(); return redirect("/")

# ================= AGENT =================
@app.route("/verify", methods=["GET", "POST"])
@login_required()
def verify():
    u = current_user()
    if u["role"] == "admin": return redirect("/admin")
    if request.method == "POST":
        id_doc, td, lic = save_upload(request.files.get("id_doc"), "id"), save_upload(request.files.get("title_deed"), "td"), save_upload(request.files.get("license"), "lic")
        if not id_doc: flash("National ID required.", "error"); return redirect("/verify")
        if u["role"] == "landlord" and not td: flash("Title deed required.", "error"); return redirect("/verify")
        if u["role"] == "agent" and not lic: flash("Agent license required.", "error"); return redirect("/verify")
        ex("INSERT INTO verifications(user_id,id_doc_url,title_deed_url,agent_license_url,status) VALUES(?,?,?,?,'pending')", (u["id"], id_doc, td, lic))
        ex("UPDATE users SET verification_status='pending' WHERE id=?", (u["id"],)); flash("Submitted."); return redirect("/dashboard")
    return render_page("""
    <div class="card">
      <h1>Verify your account</h1><p class="muted">Status: <b>{{ u.verification_status }}</b></p>
      <form method="post" enctype="multipart/form-data">
        <label>National ID</label><input type="file" name="id_doc" required>
        {% if u.role=='landlord' %}<label>Title deed</label><input type="file" name="title_deed" required>{% else %}<label>Agent license</label><input type="file" name="license" required>{% endif %}
        <div style="margin-top:12px"><button class="btn">Submit</button></div>
      </form>
    </div>
    """, u=u)

@app.route("/dashboard")
@login_required()
def dashboard():
    u = current_user()
    if u["role"] == "admin": return redirect("/admin")
    listings = q("SELECT * FROM listings WHERE user_id=? ORDER BY id DESC", (u["id"],))
    sub = active_subscription(u["id"])
    return render_page("""
    <div class="card">
      <h1>Dashboard — {{ u.full_name }}</h1>
      <p class="muted">Role: {{ u.role|capitalize }} · Verification:
        {% if u.verification_status=='verified' %}<span class="badge">✅ Verified</span>{% elif u.verification_status=='pending' %}<span class="badge pending">Pending</span>{% else %}<span class="badge rej">Unverified</span>{% endif %}
        {% if sub %} · Subscription until {{ sub.ends_at }}{% endif %}</p>
      {% if u.verification_status!='verified' %}<p><a class="btn" href="/verify">Complete verification</a></p>{% endif %}
      <p><a class="btn" href="/listing/new">+ New listing</a><a class="btn sec" href="/subscription">Buy subscription</a><a class="btn sec" href="/dashboard/messages">Messages{% if unread_count %} ({{ unread_count }} new){% endif %}</a></p>
    </div>
    <div class="card">
      <h2>My listings ({{ listings|length }})</h2>
      <table><tr><th>Title</th><th>Type</th><th>Price</th><th>Status</th><th>Views/Clicks</th><th>Expires</th><th></th></tr>
      {% for l in listings %}<tr><td>{{ l.title }}</td><td>{{ l.type }}</td><td>{{ l.currency }} {{ '%.2f'|format(l.price) }}</td><td>{{ l.status }}</td><td>{{ l.views }} / {{ l.clicks }}</td><td>{{ l.expires_at or '-' }}</td><td><a href="/listing/{{ l.id }}">View</a></td></tr>{% endfor %}
      </table>
    </div>
    """, u=u, listings=listings, sub=sub)

@app.route("/dashboard/messages")
@login_required()
def agent_inbox():
    u = current_user()
    if u["role"] == "admin": return redirect("/admin")
    convs = q("SELECT c.*, l.title, (SELECT COUNT(*) FROM messages WHERE conversation_id=c.id AND sender='tenant' AND read_at IS NULL) unread, (SELECT body FROM messages WHERE conversation_id=c.id ORDER BY id DESC LIMIT 1) last_msg FROM conversations c JOIN listings l ON l.id=c.listing_id WHERE c.agent_id=? ORDER BY c.last_activity DESC", (u["id"],))
    return render_page("""
    <div class="card">
      <h1>Messages</h1>
      <table><tr><th>Listing</th><th>Tenant</th><th>Last message</th><th>Unread</th><th></th></tr>
      {% for c in convs %}<tr><td>{{ c.title }}</td><td>{{ c.tenant_name or 'Anonymous' }}{% if c.tenant_phone %}<br><span class="muted">{{ c.tenant_phone }}</span>{% endif %}</td><td class="muted">{{ c.last_msg or '' }}</td><td>{% if c.unread %}<span class="badge rej">{{ c.unread }} new</span>{% endif %}</td><td><a href="/dashboard/messages/{{ c.id }}">Open</a></td></tr>
      {% else %}<tr><td colspan="5" class="muted">No conversations yet.</td></tr>{% endfor %}
      </table>
    </div>
    """, convs=convs)

@app.route("/dashboard/messages/<int:cid>", methods=["GET", "POST"])
@login_required()
def agent_thread(cid):
    u = current_user()
    c = q("SELECT * FROM conversations WHERE id=? AND agent_id=?", (cid, u["id"]), one=True)
    if not c: abort(404)
    l = q("SELECT * FROM listings WHERE id=?", (c["listing_id"],), one=True)
    if request.method == "POST":
        body = sanitize_text(request.form.get("body", ""), 2000)
        if body: ex("INSERT INTO messages(conversation_id,sender,body) VALUES(?,?,?)", (cid, "agent", body)); ex("UPDATE conversations SET last_activity=datetime('now') WHERE id=?", (cid,))
        return redirect(f"/dashboard/messages/{cid}")
    msgs = q("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (cid,))
    ex("UPDATE messages SET read_at=datetime('now') WHERE conversation_id=? AND sender='tenant' AND read_at IS NULL", (cid,))
    nudge = ""
    if c["tenant_phone"]:
        last_agent = next((m for m in reversed(msgs) if m["sender"] == "agent"), None)
        if last_agent: nudge = f"https://wa.me/{wa_number(c['tenant_phone'])}?text=Reply%20from%20agent%20about%20'{l['title']}':%20{last_agent['body'][:200]}"
    return render_page("""
    <div class="card">
      <h1>Conversation · {{ l.title }}</h1>
      <div class="muted">Tenant: {{ c.tenant_name or 'Anonymous' }}{% if c.tenant_phone %} · {{ c.tenant_phone }}{% endif %}{% if c.tenant_email %} · {{ c.tenant_email }}{% endif %}</div>
      <div class="chat-box" style="margin-top:10px">{% for m in msgs %}<div class="msg {{ m.sender }}">{{ m.body }}<span class="t">{{ m.sender|capitalize }} · {{ m.created_at }}</span></div>{% endfor %}</div>
      <form method="post" style="margin-top:10px">
        <textarea name="body" rows="3" required></textarea>
        <div style="margin-top:8px"><button class="btn">Send</button>{% if nudge %}<a class="btn wa" href="{{ nudge }}" target="_blank">💬 Notify tenant on WhatsApp</a>{% endif %}{% if c.tenant_phone %}<a class="btn call" href="tel:{{ tel_number(c.tenant_phone) }}">📞 Call tenant</a>{% endif %}</div>
      </form>
      <p class="muted">🔗 Guest link: <code>{{ request.url_root }}chat/{{ c.token }}</code></p>
    </div>
    """, c=c, l=l, msgs=msgs, nudge=nudge)

@app.route("/listing/new", methods=["GET", "POST"])
@login_required()
def listing_new():
    u = current_user()
    if u["role"] == "admin": abort(403)
    if u["verification_status"] != "verified": flash("Verify before listing.", "error"); return redirect("/verify")
    if request.method == "POST":
        data = request.form
        for k in ("title", "price", "city", "type"):
            if not data.get(k): flash(f"Missing {k}.", "error"); return redirect("/listing/new")
        lid = ex("INSERT INTO listings(user_id,type,title,description,price,currency,rooms,amenities,city,suburb,lat,lng,contact_name,contact_phone,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending_payment')", (u["id"], data["type"], data["title"], data.get("description", ""), float(data["price"]), data.get("currency", "USD"), int(data["rooms"]) if data.get("rooms") else None, data.get("amenities", ""), data["city"], data.get("suburb", ""), float(data["lat"]) if data.get("lat") else None, float(data["lng"]) if data.get("lng") else None, data.get("contact_name", u["full_name"]), data.get("contact_phone", u["phone"])))
        for i, f in enumerate(request.files.getlist("photos")):
            name = save_upload(f, f"l{lid}_{i}")
            if name: ex("INSERT INTO listing_photos(listing_id,url,watermarked,sort_order) VALUES(?,?,?,?)", (lid, name, 1, i))
        if active_subscription(u["id"]): ex("UPDATE listings SET status='pending_review' WHERE id=?", (lid,)); flash("Submitted for review."); return redirect("/dashboard")
        tier = q("SELECT * FROM pricing_tiers WHERE type='per_listing' AND active=1 ORDER BY price_usd ASC LIMIT 1", one=True)
        if not tier: flash("No per-listing tier configured.", "error"); return redirect("/dashboard")
        ref = gen_ref()
        pid = ex("INSERT INTO payments(user_id,listing_id,tier_id,type,amount,currency,ref_code,status) VALUES(?,?,?,'per_listing',?,?,?,'awaiting_verification')", (u["id"], lid, tier["id"], tier["price_usd"], "USD", ref))
        return redirect(f"/checkout/{pid}")
    return render_page("""
    <div class="card">
      <h1>New listing</h1>
      <form method="post" enctype="multipart/form-data">
        <div class="row"><div><label>Title</label><input name="title" required></div><div><label>Type</label><select name="type"><option value="rent">For rent</option><option value="sale">For sale</option><option value="shop">Shop</option></select></div></div>
        <div class="row"><div><label>Price</label><input name="price" type="number" step="0.01" required></div><div><label>Currency</label><select name="currency"><option>USD</option><option>ZWG</option></select></div><div><label>Rooms</label><input name="rooms" type="number"></div></div>
        <div class="row"><div><label>City</label><input name="city" required></div><div><label>Suburb</label><input name="suburb"></div></div>
        <div class="row"><div><label>Latitude</label><input name="lat" step="0.000001"></div><div><label>Longitude</label><input name="lng" step="0.000001"></div></div>
        <label>Amenities (comma-separated)</label><input name="amenities"><label>Description</label><textarea name="description" rows="4"></textarea>
        <label>Contact name</label><input name="contact_name" value="{{ u.full_name }}"><label>Contact phone (WhatsApp)</label><input name="contact_phone" value="{{ u.phone }}">
        <label>Photos</label><input type="file" name="photos" multiple accept="image/*">
        <div style="margin-top:14px"><button class="btn">Submit & continue</button></div>
      </form>
    </div>
    """, u=u)

@app.route("/checkout/<int:pid>", methods=["GET", "POST"])
@login_required()
def checkout(pid):
    u = current_user()
    p = q("SELECT * FROM payments WHERE id=? AND user_id=?", (pid, u["id"]), one=True)
    if not p: abort(404)
    if request.method == "POST":
        proof, txn = save_upload(request.files.get("proof"), f"pay{pid}"), request.form.get("txn_id", "").strip()
        if not proof or not txn: flash("Proof and Txn ID required.", "error"); return redirect(f"/checkout/{pid}")
        ex("UPDATE payments SET proof_url=?, txn_id=?, status='awaiting_verification' WHERE id=?", (proof, txn, pid))
        flash("Submitted for verification."); return redirect("/dashboard")
    return render_page("""
    <div class="card" style="max-width:600px;margin:0 auto">
      <h1>Complete EcoCash payment</h1><p>Amount: <b>{{ p.currency }} {{ '%.2f'|format(p.amount) }}</b></p>
      <p>Send to EcoCash:<br><b>{{ get_setting('ecocash_number') }}</b><br>Name: <b>{{ get_setting('ecocash_receiver_name') }}</b><br>Reference: <b>{{ p.ref_code }}</b></p>
      <hr>
      <form method="post" enctype="multipart/form-data"><label>Proof screenshot</label><input type="file" name="proof" accept="image/*" required><label>EcoCash transaction ID</label><input name="txn_id" required><div style="margin-top:12px"><button class="btn">I have paid</button></div></form>
    </div>
    """, p=p)

@app.route("/subscription")
@login_required()
def subscription():
    tiers = q("SELECT * FROM pricing_tiers WHERE type='subscription' AND active=1")
    return render_page("""
    <div class="card"><h1>Buy subscription</h1><form method="post" action="/subscription/buy"><select name="tier_id">{% for t in tiers %}<option value="{{ t.id }}">{{ t.name }} — {{ t.duration_days }} days — ${{ '%.2f'|format(t.price_usd) }}</option>{% endfor %}</select><div style="margin-top:12px"><button class="btn">Continue</button></div></form></div>
    """, tiers=tiers)

@app.route("/subscription/buy", methods=["POST"])
@login_required()
def subscription_buy():
    u = current_user()
    t = q("SELECT * FROM pricing_tiers WHERE id=? AND type='subscription'", (request.form["tier_id"],), one=True)
    if not t: abort(404)
    ref = gen_ref()
    pid = ex("INSERT INTO payments(user_id,tier_id,type,amount,currency,ref_code,status) VALUES(?,?,'subscription',?,'USD',?,'awaiting_verification')", (u["id"], t["id"], t["price_usd"], ref))
    return redirect(f"/checkout/{pid}")

# ================= ADMIN =================
@app.route("/admin")
@admin_required
def admin_home():
    pending_listings = q("SELECT l.*, u.full_name FROM listings l JOIN users u ON u.id=l.user_id WHERE l.status='pending_review' ORDER BY l.id DESC")
    pending_payments = q("SELECT p.*, u.full_name FROM payments p JOIN users u ON u.id=p.user_id WHERE p.status='awaiting_verification' ORDER BY p.id DESC")
    pending_verifs = q("SELECT v.*, u.full_name, u.role, u.email, u.phone FROM verifications v JOIN users u ON u.id=v.user_id WHERE v.status='pending' ORDER BY v.id DESC")
    open_reports = q("SELECT r.*, l.title FROM reports r JOIN listings l ON l.id=r.listing_id WHERE r.status='open' ORDER BY r.id DESC")
    stats = {"active_listings": q("SELECT COUNT(*) c FROM listings WHERE status='active'", one=True)["c"], "revenue": q("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='verified'", one=True)["s"], "users": q("SELECT COUNT(*) c FROM users WHERE role!='admin'", one=True)["c"]}
        all_users = q("SELECT *, (SELECT COUNT(*) FROM listings WHERE user_id=users.id) as listing_count FROM users WHERE role!='admin' ORDER BY id DESC")
    all_listings = q("SELECT l.*, u.full_name FROM listings l JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 100")
    all_payments = q("SELECT p.*, u.full_name FROM payments p JOIN users u ON u.id=p.user_id ORDER BY p.id DESC LIMIT 100")
    return render_page("""
    <h1>Admin</h1>
    <div class="grid"><div class="card"><div class="muted">Active listings</div><h2>{{ stats.active_listings }}</h2></div><div class="card"><div class="muted">Revenue</div><h2>${{ '%.2f'|format(stats.revenue) }}</h2></div><div class="card"><div class="muted">Users</div><h2>{{ stats.users }}</h2></div></div>
    <div class="card"><h2>Pending verifications ({{ pending_verifs|length }})</h2><table><tr><th>User</th><th>Role</th><th>Docs</th><th>Action</th></tr>{% for v in pending_verifs %}<tr><td>{{ v.full_name }}<br><span class="muted">{{ v.email }} · {{ v.phone }}</span></td><td>{{ v.role }}</td><td>{% if v.id_doc_url %}<a target="_blank" href="/uploads/{{ v.id_doc_url }}">ID</a>{% endif %}{% if v.title_deed_url %} · <a target="_blank" href="/uploads/{{ v.title_deed_url }}">Deed</a>{% endif %}{% if v.agent_license_url %} · <a target="_blank" href="/uploads/{{ v.agent_license_url }}">License</a>{% endif %}</td><td><form method="post" action="/admin/verify/{{ v.id }}" style="display:inline"><button class="btn sm" name="act" value="approve">Approve</button></form><form method="post" action="/admin/verify/{{ v.id }}" style="display:inline"><input name="reason" placeholder="reason" style="width:90px;display:inline"><button class="btn sm danger" name="act" value="reject">Reject</button></form></td></tr>{% endfor %}</table></div>
    <div class="card"><h2>Pending payments ({{ pending_payments|length }})</h2><table><tr><th>Ref</th><th>User</th><th>Type</th><th>Amount</th><th>Proof</th><th>Action</th></tr>{% for p in pending_payments %}<tr><td>{{ p.ref_code }}</td><td>{{ p.full_name }}</td><td>{{ p.type }}</td><td>${{ '%.2f'|format(p.amount) }}</td><td>{% if p.proof_url %}<a target="_blank" href="/uploads/{{ p.proof_url }}">view</a>{% endif %}{% if p.txn_id %}<br><span class="muted">{{ p.txn_id }}</span>{% endif %}</td><td><form method="post" action="/admin/payment/{{ p.id }}" style="display:inline"><button class="btn sm" name="act" value="approve">Verify</button></form><form method="post" action="/admin/payment/{{ p.id }}" style="display:inline"><input name="reason" placeholder="reason" style="width:90px;display:inline"><button class="btn sm danger" name="act" value="reject">Reject</button></form></td></tr>{% endfor %}</table></div>
    <div class="card"><h2>Pending listings ({{ pending_listings|length }})</h2><table><tr><th>Title</th><th>Agent</th><th>Type</th><th>Preview</th><th>Action</th></tr>{% for l in pending_listings %}<tr><td>{{ l.title }}<br><span class="muted">{{ l.suburb }}, {{ l.city }} — {{ l.currency }} {{ '%.2f'|format(l.price) }}</span></td><td>{{ l.full_name }}</td><td>{{ l.type }}</td><td><a target="_blank" href="/listing/{{ l.id }}">Open</a></td><td><form method="post" action="/admin/listing/{{ l.id }}" style="display:inline"><button class="btn sm" name="act" value="approve">Approve</button></form><form method="post" action="/admin/listing/{{ l.id }}" style="display:inline"><input name="reason" placeholder="reason" style="width:90px;display:inline"><button class="btn sm danger" name="act" value="reject">Reject</button></form></td></tr>{% endfor %}</table></div>
    <div class="card"><h2>Open reports ({{ open_reports|length }})</h2><table><tr><th>Listing</th><th>Reason</th><th>Note</th><th>Action</th></tr>{% for r in open_reports %}<tr><td><a href="/listing/{{ r.listing_id }}">{{ r.title }}</a></td><td>{{ r.reason }}</td><td>{{ r.note }}</td><td><form method="post" action="/admin/report/{{ r.id }}" style="display:inline"><button class="btn sm sec" name="act" value="dismiss">Dismiss</button></form><form method="post" action="/admin/report/{{ r.id }}" style="display:inline"><button class="btn sm" name="act" value="hide">Hide</button></form><form method="post" action="/admin/report/{{ r.id }}" style="display:inline"><button class="btn sm danger" name="act" value="remove">Remove</button></form></td></tr>{% endfor %}</table></div>
        <div class="card"><h2>Manage Users ({{ all_users|length }})</h2><table><tr><th>Name</th><th>Phone / Email</th><th>Listings</th><th>Status</th><th>Action</th></tr>{% for u in all_users %}<tr><td>{{ u.full_name }}<br><span class="muted">{{ u.role }}</span></td><td>{{ u.phone }}<br><span class="muted">{{ u.email }}</span></td><td>{{ u.listing_count }}</td><td>{% if u.blacklisted %}<span class="badge rej">BLOCKED</span>{% else %}<span class="badge featured">Active</span>{% endif %}</td><td><form method="post" action="/admin/user_toggle/{{ u.id }}" style="display:inline"><button class="btn sm {% if u.blacklisted %}sec{% else %}danger{% endif %}">{% if u.blacklisted %}Unblock{% else %}Block{% endif %}</button></form> <form method="post" action="/admin/user_delete/{{ u.id }}" style="display:inline" onsubmit="return confirm('Delete user + all listings?')"><button class="btn sm danger">Delete</button></form></td></tr>{% endfor %}</table></div>
    <div class="card"><h2>Manage Listings ({{ all_listings|length }})</h2><table><tr><th>Title</th><th>Agent</th><th>Price</th><th>Status</th><th>Action</th></tr>{% for l in all_listings %}<tr><td>{{ l.title }}<br><span class="muted">{{ l.suburb }}, {{ l.city }}</span> {% if l.is_featured %}<span class="badge featured">★ FEATURED</span>{% endif %}</td><td>{{ l.full_name }}</td><td>{{ l.currency }} {{ '%.2f'|format(l.price) }}</td><td>{{ l.status }}</td><td><form method="post" action="/admin/listing_feature/{{ l.id }}" style="display:inline"><button class="btn sm">{% if l.is_featured %}Unfeature{% else %}Make Featured{% endif %}</button></form> <form method="post" action="/admin/listing_delete/{{ l.id }}" style="display:inline" onsubmit="return confirm('Delete listing?')"><button class="btn sm danger">Delete</button></form></td></tr>{% endfor %}</table></div>
    <div class="card"><h2>Transaction History</h2><table><tr><th>Date</th><th>User</th><th>Type</th><th>Amount</th><th>Ref / Txn</th><th>Status</th></tr>{% for p in all_payments %}<tr><td>{{ p.created_at }}</td><td>{{ p.full_name }}</td><td>{{ p.type }}</td><td><b>${{ '%.2f'|format(p.amount) }}</b></td><td>{{ p.ref_code }}{% if p.txn_id %}<br><span class="muted">{{ p.txn_id }}</span>{% endif %}</td><td>{{ p.status }}</td></tr>{% endfor %}</table></div>
    """, pending_listings=pending_listings, pending_payments=pending_payments, pending_verifs=pending_verifs, open_reports=open_reports, stats=stats, all_users=all_users, all_listings=all_listings, all_payments=all_payments)


@app.route("/admin/users")
@admin_required
def admin_users():
    search = request.args.get("q","").strip()
    if search:
        users = q("SELECT * FROM users WHERE full_name LIKE ? OR phone LIKE ? OR email LIKE ? ORDER BY id DESC", (f"%{search}%", f"%{search}%", f"%{search}%"))
    else:
        users = q("SELECT * FROM users ORDER BY id DESC LIMIT 100")
    return render_page("""
    <h1>All Users</h1>
    <form method="get"><input name="q" placeholder="Search name / phone / email" value="{{ search }}">
    <button class="btn">Search</button></form>
    <table><tr><th>ID</th><th>Name</th><th>Phone</th><th>Role</th><th>Verified</th><th>Status</th><th>Action</th></tr>
    {% for u in users %}
    <tr>
      <td>{{ u.id }}</td>
      <td>{{ u.full_name }}</td>
      <td>{{ u.phone }}</td>
      <td>{{ u.role }}</td>
      <td>{% if u.verified_badge %}✅{% else %}❌{% endif %}</td>
      <td>{% if u.blacklisted %}🚫 BANNED{% else %}Active{% endif %}</td>
      <td>
        <form method="post" action="/admin/user_action" style="display:inline">
        <input type="hidden" name="user_id" value="{{ u.id }}">
        <button name="action" value="verify">Verify</button>
        <button name="action" value="ban">Ban</button>
        <button name="action" value="unban">Unban</button>
        <button name="action" value="delete" onclick="return confirm('Delete user?')">Delete</button>
        </form>
      </td>
    </tr>
    {% endfor %}
    </table>
    <p><a href="/admin">← Back to Admin</a></p>
    """, users=users, search=search)

@app.route("/admin/user_action", methods=["POST"])
@admin_required
def admin_user_action():
    uid = request.form.get("user_id")
    act = request.form.get("action")
    if act == "verify":
        ex("UPDATE users SET verified_badge=1, verification_status='verified' WHERE id=?", (uid,))
    elif act == "ban":
        ex("UPDATE users SET blacklisted=1 WHERE id=?", (uid,))
    elif act == "unban":
        ex("UPDATE users SET blacklisted=0 WHERE id=?", (uid,))
    elif act == "delete":
        ex("DELETE FROM users WHERE id=?", (uid,))
    return redirect("/admin/users")
@app.route("/admin/verify/<int:vid>", methods=["POST"])
@admin_required
def admin_verify(vid):
    v = q("SELECT * FROM verifications WHERE id=?", (vid,), one=True)
    if not v: abort(404)
    if request.form.get("act") == "approve": ex("UPDATE verifications SET status='verified' WHERE id=?", (vid,)); ex("UPDATE users SET verification_status='verified', verified_badge=1 WHERE id=?", (v["user_id"],)); flash("User verified.")
    else: ex("UPDATE verifications SET status='rejected', admin_note=? WHERE id=?", (request.form.get("reason", ""), vid)); ex("UPDATE users SET verification_status='rejected' WHERE id=?", (v["user_id"],)); flash("Rejected.")
    return redirect("/admin")

@app.route("/admin/payment/<int:pid>", methods=["POST"])
@admin_required
def admin_payment(pid):
    p = q("SELECT * FROM payments WHERE id=?", (pid,), one=True)
    if not p: abort(404)
    if request.form.get("act") == "approve":
        ex("UPDATE payments SET status='verified', verified_by=?, verified_at=datetime('now') WHERE id=?", (current_user()["id"], pid))
        if p["type"] == "per_listing" and p["listing_id"]:
            tier = q("SELECT * FROM pricing_tiers WHERE id=?", (p["tier_id"],), one=True)
            days = tier["duration_days"] if tier else 30
            ex("UPDATE listings SET status='pending_review', expires_at=datetime('now', ?) WHERE id=?", (f"+{days} days", p["listing_id"]))
        elif p["type"] == "subscription":
            tier = q("SELECT * FROM pricing_tiers WHERE id=?", (p["tier_id"],), one=True)
            days = tier["duration_days"] if tier else 30
            ex("INSERT INTO subscriptions(user_id,tier_id,payment_id,starts_at,ends_at) VALUES(?,?,?,datetime('now'),datetime('now',?))", (p["user_id"], p["tier_id"], pid, f"+{days} days"))
        flash("Payment verified.")
    else: ex("UPDATE payments SET status='rejected', admin_note=? WHERE id=?", (request.form.get("reason", ""), pid)); flash("Rejected.")
    return redirect("/admin")

@app.route("/admin/listing/<int:lid>", methods=["POST"])
@admin_required
def admin_listing(lid):
    if request.form.get("act") == "approve": ex("UPDATE listings SET status='active' WHERE id=?", (lid,)); flash("Live.")
    else: ex("UPDATE listings SET status='rejected' WHERE id=?", (lid,)); flash("Rejected.")
    return redirect("/admin")

@app.route("/admin/report/<int:rid>", methods=["POST"])
@admin_required
def admin_report(rid):
    r = q("SELECT * FROM reports WHERE id=?", (rid,), one=True)
    if not r: abort(404)
    act = request.form.get("act")
    if act == "dismiss": ex("UPDATE reports SET status='dismissed', actioned_by=? WHERE id=?", (current_user()["id"], rid))
    elif act == "hide": ex("UPDATE reports SET status='actioned', actioned_by=? WHERE id=?", (current_user()["id"], rid)); ex("UPDATE listings SET status='hidden' WHERE id=?", (r["listing_id"],))
    elif act == "remove": ex("UPDATE reports SET status='actioned', actioned_by=? WHERE id=?", (current_user()["id"], rid)); ex("UPDATE listings SET status='rejected' WHERE id=?", (r["listing_id"],))
    flash("Updated."); return redirect("/admin")

@app.route("/admin/messages")
@admin_required
def admin_messages():
    convs = q("SELECT c.*, l.title, u.full_name AS agent_name FROM conversations c JOIN listings l ON l.id=c.listing_id JOIN users u ON u.id=c.agent_id ORDER BY c.last_activity DESC LIMIT 200")
    return render_page("""
    <div class="card"><h1>All conversations</h1><table><tr><th>Listing</th><th>Agent</th><th>Tenant</th><th>Last activity</th><th></th></tr>{% for c in convs %}<tr><td>{{ c.title }}</td><td>{{ c.agent_name }}</td><td>{{ c.tenant_name or 'Anonymous' }}{% if c.tenant_phone %}<br><span class="muted">{{ c.tenant_phone }}</span>{% endif %}</td><td>{{ c.last_activity }}</td><td><a href="/admin/messages/{{ c.id }}">Read</a></td></tr>{% endfor %}</table></div>
    """, convs=convs)

@app.route("/admin/messages/<int:cid>")
@admin_required
def admin_thread(cid):
    c = q("SELECT * FROM conversations WHERE id=?", (cid,), one=True)
    if not c: abort(404)
    msgs = q("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (cid,))
    return render_page("""
    <div class="card"><h1>Conversation #{{ c.id }}</h1><div class="chat-box">{% for m in msgs %}<div class="msg {{ m.sender }}">{{ m.body }}<span class="t">{{ m.sender }} · {{ m.created_at }}</span></div>{% endfor %}</div></div>
    """, c=c, msgs=msgs)

@app.route("/admin/pricing", methods=["GET", "POST"])
@admin_required
def admin_pricing():
    if request.method == "POST":
        ex("INSERT INTO pricing_tiers(name,type,duration_days,listing_limit,price_usd) VALUES(?,?,?,?,?)", (request.form["name"], request.form["type"], int(request.form["duration_days"]), int(request.form["listing_limit"]) if request.form.get("listing_limit") else None, float(request.form["price_usd"])))
        flash("Tier added."); return redirect("/admin/pricing")
    tiers = q("SELECT * FROM pricing_tiers ORDER BY id DESC")
    return render_page("""
    <div class="card"><h1>Pricing tiers</h1><form method="post" class="row"><div><label>Name</label><input name="name" required></div><div><label>Type</label><select name="type"><option value="per_listing">per_listing</option><option value="subscription">subscription</option></select></div><div><label>Duration days</label><input name="duration_days" type="number" value="30" required></div><div><label>Limit</label><input name="listing_limit" type="number"></div><div><label>Price USD</label><input name="price_usd" type="number" step="0.01" required></div><div style="align-self:flex-end"><button class="btn">Add</button></div></form>
      <table style="margin-top:14px"><tr><th>ID</th><th>Name</th><th>Type</th><th>Days</th><th>Limit</th><th>Price</th></tr>{% for t in tiers %}<tr><td>{{ t.id }}</td><td>{{ t.name }}</td><td>{{ t.type }}</td><td>{{ t.duration_days }}</td><td>{{ t.listing_limit or '∞' }}</td><td>${{ '%.2f'|format(t.price_usd) }}</td></tr>{% endfor %}</table></div>
    """, tiers=tiers)

@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    keys = ["ecocash_number", "ecocash_receiver_name", "whatsapp_support_number", "whatsapp_support_display_name", "site_email", "default_currency", "watermark_text", "site_credit", "developer_name", "developer_whatsapp", "developer_email"]
    if request.method == "POST":
        for k in keys:
            if k in request.form: set_setting(k, request.form[k])
        flash("Saved."); return redirect("/admin/settings")
    return render_page("""
    <div class="card"><h1>Settings</h1><form method="post">{% for k in keys %}<label>{{ k }}</label><input name="{{ k }}" value="{{ get_setting(k) }}">{% endfor %}<div style="margin-top:12px"><button class="btn">Save</button></div></form></div>
    """, keys=keys)

# Auto-create database on Render
try:
    init_db()
except Exception as e:
    print(f"DB init: {e}")
if __name__ == "__main__":
    init_db()
    print("\n▶  http://127.0.0.1:5000")
    print("   Admin: admin@accommodationlink.co.zw / Admin@123\n")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
# --- FIX 5: Safe column check ---
def ensure_featured_column():
  try:
      with app.app_context():
          d = db()
          d.execute("ALTER TABLE products ADD COLUMN is_featured INTEGER DEFAULT 0")
          d.commit()
  except Exception:
      pass
ensure_featured_column()

