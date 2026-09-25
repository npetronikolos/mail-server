"""Mail server admin dashboard.

Runs as the unprivileged "mail-dashboard" user behind nginx at /admin/.
Every change goes through "sudo mailctl --json ...", which validates its
input and records an audit log; the dashboard itself never touches mail
configuration files.

Sign-in uses the mailbox's own email password (checked against Dovecot over
IMAP) plus an optional TOTP second factor. Mailboxes listed in
/etc/mail-server/admins get the admin pages; everyone else gets a personal
account page (password, 2FA, device setup, calendar/contacts links).
"""
import base64
import datetime
import hashlib
import hmac
import imaplib
import io
import json
import os
import re
import secrets
import ssl
import subprocess
import threading
import time
import urllib.request
import uuid
from functools import wraps

import pyotp
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)

CONF_FILE = "/etc/mail-server/mail-server.conf"
ADMINS_FILE = "/etc/mail-server/admins"
DATA_DIR = os.environ.get("DASHBOARD_DATA", "/var/lib/mail-dashboard")
AUTH_LOG = os.environ.get("DASHBOARD_AUTH_LOG", "/var/log/mail-dashboard/auth.log")
MAILCTL = ["sudo", "-n", "/usr/local/sbin/mailctl", "--json"]
RSPAMD = "http://127.0.0.1:11334"
IDLE_TIMEOUT = 30 * 60
SESSION_MAX = 8 * 3600

app = Flask(__name__)


def _secret_key():
    path = os.path.join(DATA_DIR, "secret_key")
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key


app.secret_key = _secret_key()
app.config.update(
    SESSION_COOKIE_NAME="mailadmin",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=SESSION_MAX,
    MAX_CONTENT_LENGTH=64 * 1024,
)

_lock = threading.Lock()
_failures = {}      # key -> [timestamps]
_totp_used = {}     # email -> last accepted time step
_cache = {}         # key -> (expires, value)
_dav_ok = {}        # sha256(user:pass) -> expires


# ------------------------------------------------------------------ helpers

def load_conf():
    conf = {}
    try:
        with open(CONF_FILE) as f:
            for line in f:
                m = re.match(r"^\s*([A-Z0-9_]+)=(.*)$", line)
                if not m:
                    continue
                v = m.group(2).strip()
                if v[:1] in "\"'":
                    end = v.find(v[0], 1)
                    v = v[1:end] if end > 0 else v[1:]
                else:
                    v = v.split("#", 1)[0].strip()
                conf[m.group(1)] = v
    except OSError:
        pass
    conf["DOMAIN"] = conf.get("DOMAIN", "").lower()
    conf["MAIL_HOSTNAME"] = conf.get("MAIL_HOSTNAME", "").lower()
    conf.setdefault("ENABLE_DAV", "yes")
    conf.setdefault("ENABLE_WEBMAIL", "yes")
    return conf


CONF = load_conf()


class MailctlError(Exception):
    pass


def mailctl(*args, input=None, timeout=180):
    cmd = list(MAILCTL)
    if session.get("email"):
        cmd += ["--actor", session["email"]]
    cmd += [str(a) for a in args]
    try:
        p = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MailctlError("the operation timed out")
    out = p.stdout.strip().splitlines()
    data = None
    if out:
        try:
            data = json.loads(out[-1])
        except ValueError:
            data = None
    if isinstance(data, dict) and "error" in data:
        raise MailctlError(data["error"])
    if p.returncode != 0:
        raise MailctlError((p.stderr or p.stdout or "mailctl failed").strip()[:300])
    return data


def cached(key, seconds, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (now + seconds, value)
    return value


def invalidate(*prefixes):
    with _lock:
        for k in list(_cache):
            if k.startswith(prefixes):
                del _cache[k]


def client_ip():
    return request.headers.get("X-Real-IP") or request.remote_addr or "?"


def safe(s):
    return re.sub(r"[^A-Za-z0-9@._+:-]", "?", s or "")[:120]


def log_auth(event, email):
    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(AUTH_LOG, "a") as f:
            f.write(f"{stamp} {event} ip={safe(client_ip())} user={safe(email)}\n")
    except OSError:
        pass


def too_many_failures(*keys, limit=5, window=600):
    now = time.time()
    with _lock:
        for k in keys:
            recent = [t for t in _failures.get(k, []) if now - t < window]
            _failures[k] = recent
            if len(recent) >= limit:
                return True
    return False


def record_failure(*keys):
    with _lock:
        for k in keys:
            _failures.setdefault(k, []).append(time.time())


def clear_failures(*keys):
    with _lock:
        for k in keys:
            _failures.pop(k, None)


def imap_login_ok(email, password):
    """Check a mailbox password against Dovecot."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # local connection to our own server
    try:
        with imaplib.IMAP4_SSL("127.0.0.1", 993, ssl_context=ctx, timeout=15) as m:
            m.login(email, password)
            return True
    except (imaplib.IMAP4.error, UnicodeEncodeError):
        return False


def read_admins():
    try:
        with open(ADMINS_FILE) as f:
            return {x.strip().lower() for x in f if x.strip() and not x.startswith("#")}
    except OSError:
        return set()


# ------------------------------------------------------------------ TOTP store

def _totp_path():
    return os.path.join(DATA_DIR, "totp.json")


def totp_load():
    try:
        with open(_totp_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def totp_save(data):
    tmp = _totp_path() + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, _totp_path())


def totp_verify(email, secret, code):
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return False
    totp = pyotp.TOTP(secret)
    now = time.time()
    for offset in (-1, 0, 1):
        step = int(now // 30) + offset
        if hmac.compare_digest(totp.at(step * 30), code):
            with _lock:
                if _totp_used.get(email, -1) >= step:
                    return False          # code already used: replay
                _totp_used[email] = step
            return True
    return False


def qr_svg(text):
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    svg = re.sub(r"<\?xml[^>]*\?>", "", svg)
    return re.sub(r'\s(width|height)="[^"]*"', "", svg, count=2)


# ------------------------------------------------------------------ session & guards

@app.before_request
def guard():
    g.conf = CONF
    g.nonce = None
    if request.endpoint in ("static", "dav_auth", "healthz"):
        return
    now = time.time()
    if session.get("email"):
        if now - session.get("seen", 0) > IDLE_TIMEOUT or now - session.get("at", 0) > SESSION_MAX:
            session.clear()
            flash("You were signed out after a period of inactivity.", "info")
            return redirect(url_for("login"))
        session["seen"] = now
        # Admin rights are re-checked on every request.
        session["admin"] = session["email"] in read_admins()
    if request.method == "POST":
        token = request.form.get("csrf", "")
        if not token or not hmac.compare_digest(token, session.get("csrf", "")):
            abort(400, "Security token expired - reload the page and try again.")


@app.after_request
def headers(resp):
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
    return resp


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return {"csrf": session["csrf"], "me": session.get("email"), "is_admin": session.get("admin"),
            "conf": CONF, "human": human_bytes, "ago": ago}


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("email"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a, **kw):
        if not session.get("admin"):
            abort(403)
        return fn(*a, **kw)
    return wrapper


def action(fn):
    """Run a mailctl-backed form action, flash the outcome, redirect back."""
    @wraps(fn)
    def wrapper(*a, **kw):
        try:
            msg = fn(*a, **kw)
            if msg:
                flash(msg, "ok")
        except MailctlError as e:
            flash(str(e), "error")
        invalidate("status", "users", "domains", "aliases", "queue", "bans", "backups", "check",
                   "shares", "junk", "dmarc", "delivery")
        return redirect(request.form.get("next") or request.referrer or url_for("index"))
    return wrapper


@app.errorhandler(MailctlError)
def mailctl_failed(e):
    return render_template("error.html", message=str(e)), 500


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def http_error(e):
    return render_template("error.html", message=e.description, code=e.code), e.code


# ------------------------------------------------------------------ formatting

def human_bytes(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def ago(ts):
    try:
        if isinstance(ts, str):
            ts = datetime.datetime.fromisoformat(ts).timestamp()
        s = int(time.time() - float(ts))
    except (TypeError, ValueError):
        return ""
    if s < 60:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if s >= size:
            n = s // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return ""


# ------------------------------------------------------------------ sign in / out

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("email"):
        return redirect(url_for("index"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if "@" not in email and CONF.get("DOMAIN"):
            email = f"{email}@{CONF['DOMAIN']}"
        ip = client_ip()
        if too_many_failures(f"ip:{ip}", limit=10) or too_many_failures(f"user:{email}", limit=10):
            log_auth("LOGIN BLOCKED", email)
            flash("Too many failed attempts. Wait 10 minutes and try again.", "error")
            return render_template("login.html", email=email), 429
        try:
            ok = imap_login_ok(email, password)
        except OSError:
            flash("The mail server is not responding (Dovecot). Try again shortly.", "error")
            return render_template("login.html", email=email), 503
        if not ok:
            record_failure(f"ip:{ip}", f"user:{email}")
            log_auth("LOGIN FAILED", email)
            time.sleep(1)
            flash("Wrong email address or password.", "error")
            return render_template("login.html", email=email), 401
        clear_failures(f"ip:{ip}", f"user:{email}")
        secret = totp_load().get(email)
        session.clear()
        if secret:
            session["pending"] = email
            session["pending_at"] = time.time()
            session["next"] = request.args.get("next", "")
            return redirect(url_for("login_2fa"))
        return finish_login(email, request.args.get("next"))
    return render_template("login.html", email="")


@app.route("/login/2fa", methods=["GET", "POST"])
def login_2fa():
    email = session.get("pending")
    if not email or time.time() - session.get("pending_at", 0) > 300:
        session.clear()
        return redirect(url_for("login"))
    if request.method == "POST":
        ip = client_ip()
        if too_many_failures(f"2fa:{email}", limit=5):
            session.clear()
            log_auth("2FA BLOCKED", email)
            flash("Too many wrong codes. Sign in again in 10 minutes.", "error")
            return redirect(url_for("login"))
        secret = totp_load().get(email)
        if secret and totp_verify(email, secret, request.form.get("code")):
            clear_failures(f"2fa:{email}")
            return finish_login(email, session.get("next"))
        record_failure(f"2fa:{email}", f"ip:{ip}")
        log_auth("2FA FAILED", email)
        flash("That code is not valid. Check your authenticator app's clock.", "error")
    return render_template("login_2fa.html", email=email)


def finish_login(email, nxt=None):
    session.clear()
    session.permanent = True
    session.update(email=email, at=time.time(), seen=time.time(),
                   csrf=secrets.token_urlsafe(32), admin=email in read_admins())
    log_auth("LOGIN OK", email)
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(request.script_root + nxt if not nxt.startswith(request.script_root) else nxt)
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "info")
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    return "ok"


# ------------------------------------------------------------------ overview

def rspamd_get(path):
    try:
        with urllib.request.urlopen(RSPAMD + path, timeout=5) as r:
            return json.loads(r.read().decode())
    except (OSError, ValueError):
        return None


@app.route("/")
@login_required
def index():
    if not session.get("admin"):
        return redirect(url_for("account"))
    st = cached("status", 20, lambda: mailctl("status"))
    stats = cached("rspamd:stat", 30, lambda: rspamd_get("/stat")) or {}
    hist = cached("rspamd:hist", 30, lambda: rspamd_get("/history")) or {}
    rows = hist.get("rows", []) if isinstance(hist, dict) else []
    actions = stats.get("actions", {}) if isinstance(stats, dict) else {}
    total = sum(actions.values()) or 0
    has_totp = session["email"] in totp_load()
    return render_template("overview.html", st=st, stats=stats, actions=actions, total=total,
                           history=rows[:15], has_totp=has_totp)


@app.route("/health")
@admin_required
def health():
    return render_template("health.html")


@app.route("/api/health")
@admin_required
def api_health():
    fresh = request.args.get("fresh") == "1"
    if fresh:
        invalidate("check")
    return jsonify(cached("check", 300, lambda: mailctl("check", timeout=300)))


# ------------------------------------------------------------------ domains

@app.route("/domains")
@admin_required
def domains():
    return render_template("domains.html", domains=cached("domains", 30, lambda: mailctl("domain", "list")))


@app.route("/domains/add", methods=["POST"])
@admin_required
@action
def domain_add():
    d = request.form.get("domain", "").strip().lower()
    mailctl("domain", "add", d)
    return f"{d} added. Create its DNS records next (shown on the domain page)."


@app.route("/domains/<domain>/delete", methods=["POST"])
@admin_required
@action
def domain_del(domain):
    mailctl("domain", "del", domain)
    return f"{domain} removed."


@app.route("/domains/<domain>")
@admin_required
def domain_view(domain):
    records = mailctl("dns", domain)
    return render_template("domain.html", domain=domain, records=records,
                           dnsp=cached("delivery:dnsp", 30, lambda: mailctl("dns", "provider")))


@app.route("/api/dns/<domain>")
@admin_required
def api_dns(domain):
    return jsonify(cached(f"check:dns:{domain}", 120, lambda: mailctl("dns", domain, "--check")))


# ------------------------------------------------------------------ mailboxes

@app.route("/mailboxes")
@admin_required
def mailboxes():
    users = cached("users", 20, lambda: mailctl("user", "list"))
    doms = cached("domains", 30, lambda: mailctl("domain", "list"))
    shares = cached("shares", 20, lambda: mailctl("share", "list"))
    totp = totp_load()
    for u in users:
        u["totp"] = u["email"] in totp
        u["shared_with"] = shares.get(u["email"], [])
        u["member_of"] = [mb for mb, members in shares.items() if u["email"] in members]
    return render_template("mailboxes.html", users=users, domains=doms,
                           default_quota=CONF.get("DEFAULT_QUOTA", ""))


@app.route("/mailboxes/<email>/share", methods=["POST"])
@admin_required
@action
def mailbox_share(email):
    member = request.form.get("member", "").strip().lower()
    if request.form.get("remove"):
        mailctl("share", "del", email, member)
        return f"{member} no longer has access to {email}."
    mailctl("share", "add", email, member)
    return f"{member} can now read {email} (folder Shared/{email}) and send as it."


def _new_password(form):
    pw = form.get("password") or ""
    if form.get("generate"):
        pw = secrets.token_urlsafe(15)
    return pw


@app.route("/mailboxes/add", methods=["POST"])
@admin_required
@action
def mailbox_add():
    local = request.form.get("local", "").strip().lower()
    domain = request.form.get("domain", "").strip().lower()
    email = f"{local}@{domain}" if local and "@" not in local else local
    pw = _new_password(request.form)
    args = ["user", "add", email, "--quota", request.form.get("quota", "").strip() or "none"]
    mailctl(*args, input=pw + "\n")
    if request.form.get("generate"):
        return f"Created {email}. Password: {pw} (shown once - copy it now)"
    return f"Created {email}."


@app.route("/mailboxes/<email>/password", methods=["POST"])
@admin_required
@action
def mailbox_password(email):
    pw = _new_password(request.form)
    mailctl("user", "passwd", email, input=pw + "\n")
    if request.form.get("generate"):
        return f"New password for {email}: {pw} (shown once - copy it now)"
    return f"Password changed for {email}."


@app.route("/mailboxes/<email>/quota", methods=["POST"])
@admin_required
@action
def mailbox_quota(email):
    q = request.form.get("quota", "").strip() or "none"
    r = mailctl("user", "quota", email, q)
    return f"Quota for {email}: {r['quota'] or 'unlimited'}."


@app.route("/mailboxes/<email>/admin", methods=["POST"])
@admin_required
@action
def mailbox_admin(email):
    if request.form.get("make") == "1":
        mailctl("admin", "add", email)
        return f"{email} is now an admin."
    if email == session["email"]:
        raise MailctlError("you cannot remove your own admin rights")
    mailctl("admin", "del", email)
    return f"{email} is no longer an admin."


@app.route("/mailboxes/<email>/reset-2fa", methods=["POST"])
@admin_required
@action
def mailbox_reset_2fa(email):
    data = totp_load()
    if data.pop(email.lower(), None) is None:
        raise MailctlError(f"{email} does not use two-factor sign-in")
    totp_save(data)
    return f"Two-factor sign-in turned off for {email}."


@app.route("/mailboxes/<email>/delete", methods=["POST"])
@admin_required
@action
def mailbox_delete(email):
    if email == session["email"]:
        raise MailctlError("you cannot delete the mailbox you are signed in with")
    args = ["user", "del", email]
    if request.form.get("purge"):
        args.append("--purge")
    mailctl(*args)
    data = totp_load()
    if data.pop(email.lower(), None) is not None:
        totp_save(data)
    return f"Deleted {email}."


# ------------------------------------------------------------------ aliases

@app.route("/aliases")
@admin_required
def aliases():
    return render_template("aliases.html",
                           aliases=cached("aliases", 20, lambda: mailctl("alias", "list")),
                           domains=cached("domains", 30, lambda: mailctl("domain", "list")))


@app.route("/aliases/add", methods=["POST"])
@admin_required
@action
def alias_add():
    local = request.form.get("local", "").strip().lower()
    domain = request.form.get("domain", "").strip().lower()
    alias = f"@{domain}" if local in ("", "*") else f"{local}@{domain}"
    dests = request.form.get("destinations", "")
    r = mailctl("alias", "add", alias, dests)
    return f"{r['alias']} now forwards to {', '.join(r['destinations'])}."


@app.route("/aliases/delete", methods=["POST"])
@admin_required
@action
def alias_delete():
    a = request.form.get("alias", "")
    mailctl("alias", "del", a)
    return f"Removed {a}."


# ------------------------------------------------------------------ queue, security, logs, backups

@app.route("/queue")
@admin_required
def queue():
    return render_template("queue.html", items=cached("queue", 10, lambda: mailctl("queue", "list")))


@app.route("/queue/action", methods=["POST"])
@admin_required
@action
def queue_action():
    act = request.form.get("action")
    if act == "flush":
        mailctl("queue", "flush")
        return "Retrying delivery of all queued mail."
    if act not in ("delete", "hold", "release"):
        abort(400)
    qid = request.form.get("id", "")
    mailctl("queue", act, qid)
    return f"Queue: {act} {qid}."


@app.route("/security")
@admin_required
def security():
    audit = mailctl("logs", "audit", "--lines", "100")
    auth = []
    try:
        with open(AUTH_LOG) as f:
            auth = f.readlines()[-100:]
    except OSError:
        pass
    return render_template("security.html", bans=cached("bans", 15, lambda: mailctl("bans")),
                           audit=list(reversed(audit)), auth=list(reversed(auth)))


@app.route("/security/unban", methods=["POST"])
@admin_required
@action
def unban():
    ip = request.form.get("ip", "")
    mailctl("unban", ip)
    return f"Unblocked {ip}."


LOG_KINDS = [("mail", "Mail (Postfix + Dovecot)"), ("postfix", "Postfix"), ("dovecot", "Dovecot"),
             ("spam", "Spam filter (Rspamd)"), ("security", "Fail2ban"), ("web", "Web"),
             ("audit", "Admin changes")]


@app.route("/logs")
@admin_required
def logs():
    kind = request.args.get("kind", "mail")
    if kind not in dict(LOG_KINDS):
        kind = "mail"
    q = request.args.get("q", "").strip()[:100]
    try:
        n = max(20, min(int(request.args.get("lines", 200)), 2000))
    except ValueError:
        n = 200
    args = ["logs", kind, "--lines", n]
    if q:
        args += ["--grep", q]
    lines = mailctl(*args)
    return render_template("logs.html", lines=lines, kind=kind, kinds=LOG_KINDS, q=q, n=n)


@app.route("/backups")
@admin_required
def backups():
    return render_template("backups.html", backups=cached("backups", 10, lambda: mailctl("backup", "list")))


@app.route("/backups/run", methods=["POST"])
@admin_required
@action
def backup_run():
    mailctl("backup", "--background")
    return "Backup started. Refresh this page in a minute to see it."


@app.route("/certificate/refresh", methods=["POST"])
@admin_required
@action
def cert_refresh():
    r = mailctl("cert", timeout=600)
    return ("Certificate updated: " if r.get("changed") else "Certificate already covers: ") + \
        ", ".join(r.get("names", []))


# ------------------------------------------------------------------ personal account

def dav_enabled():
    return CONF.get("ENABLE_DAV") == "yes"


@app.route("/account")
@login_required
def account():
    email = session["email"]
    try:
        me = mailctl("user", "info", email)
    except MailctlError:
        me = {"email": email}
    rules = mailctl("user", "rules", email)
    temps = mailctl("alias", "temp", "list", "--owner", email)
    junk = mailctl("junk", "--user", email, "--days", "30")
    shared = [mb for mb, members in mailctl("share", "list").items() if email in members]
    return render_template("account.html", info=me, has_totp=email in totp_load(),
                           dav=dav_enabled(), rules=rules, temps=temps, junk=junk[:50],
                           shared=shared, today=datetime.date.today().isoformat())


@app.route("/account/forward", methods=["POST"])
@login_required
@action
def account_forward():
    email = session["email"]
    if request.form.get("off"):
        mailctl("user", "forward", email, "--off")
        return "Forwarding is off."
    args = ["user", "forward", email, "--to", request.form.get("to", "")]
    if not request.form.get("keep"):
        args.append("--no-keep")
    r = mailctl(*args)
    return f"Mail is now forwarded to {', '.join(r['forward']['to'])}."


@app.route("/account/vacation", methods=["POST"])
@login_required
@action
def account_vacation():
    email = session["email"]
    if request.form.get("off"):
        mailctl("user", "vacation", email, "--off")
        return "Vacation reply is off."
    args = ["user", "vacation", email, "--subject", request.form.get("subject", "").strip() or "Out of office"]
    for k in ("start", "end"):
        if request.form.get(k):
            args += [f"--{k}", request.form[k]]
    mailctl(*args, input=request.form.get("message", ""))
    return "Vacation reply is on."


@app.route("/account/throwaway", methods=["POST"])
@login_required
@action
def account_throwaway():
    email = session["email"]
    if request.form.get("delete"):
        mailctl("alias", "temp", "del", request.form["delete"], "--owner", email)
        return f"Deleted {request.form['delete']}. Mail to it is now refused."
    args = ["alias", "temp", "add", email, "--note", request.form.get("note", "")[:100]]
    if request.form.get("days"):
        args += ["--days", request.form["days"]]
    r = mailctl(*args)
    return f"New address: {r['alias']} (delivers to you)"


@app.route("/account/junk", methods=["POST"])
@login_required
@action
def account_junk():
    act = request.form.get("action")
    if act not in ("release", "delete"):
        abort(400)
    mailctl("junk", act, session["email"], request.form.get("id", ""))
    return "Moved to your inbox; the spam filter learned it is not spam." if act == "release" else "Deleted."


# ------------------------------------------------------------------ quarantine, DMARC, delivery

@app.route("/quarantine")
@admin_required
def quarantine():
    days = request.args.get("days", "7")
    if days not in ("1", "7", "30"):
        days = "7"
    items = cached(f"junk:{days}", 30, lambda: mailctl("junk", "--days", days, timeout=300))
    return render_template("quarantine.html", items=items, days=days)


@app.route("/quarantine/action", methods=["POST"])
@admin_required
@action
def quarantine_action():
    act = request.form.get("action")
    if act not in ("release", "delete"):
        abort(400)
    mailctl("junk", act, request.form.get("user", ""), request.form.get("id", ""))
    return "Released to the inbox (and learned as not spam)." if act == "release" else "Deleted."


@app.route("/dmarc")
@admin_required
def dmarc():
    days = request.args.get("days", "30")
    if days not in ("7", "30", "90"):
        days = "30"
    return render_template("dmarc.html", r=cached(f"dmarc:{days}", 60, lambda: mailctl("dmarc", "--days", days)),
                           days=days)


@app.route("/delivery")
@admin_required
def delivery():
    return render_template("delivery.html", relay=mailctl("relay"), limit=mailctl("limit"),
                           alerts=mailctl("alerts"), dnsp=mailctl("dns", "provider"))


@app.route("/delivery/relay", methods=["POST"])
@admin_required
@action
def delivery_relay():
    act = request.form.get("action")
    if act == "off":
        mailctl("relay", "off")
        return "Outgoing mail is delivered directly again."
    if act == "test":
        mailctl("relay", "test", timeout=60)
        return "Connected to the relay service and logged in."
    args = ["relay", "set", request.form.get("host", ""), "--port", request.form.get("port", "587")]
    if request.form.get("user"):
        args += ["--user", request.form["user"]]
    if request.form.get("spf"):
        args += ["--spf", request.form["spf"]]
    r = mailctl(*args, input=request.form.get("password", "") + "\n")
    return (f"Outgoing mail now goes through {r['host']}:{r['port']}. "
            "Update your SPF record (Domains & DNS) so receivers accept it.")


@app.route("/delivery/limit", methods=["POST"])
@admin_required
@action
def delivery_limit():
    r = mailctl("limit", "set", request.form.get("per_hour", "0"))
    return f"Each user may now send {r['per_hour']} messages per hour." if r["per_hour"] else "Outgoing limit removed."


@app.route("/delivery/alerts", methods=["POST"])
@admin_required
@action
def delivery_alerts():
    act = request.form.get("action")
    if act == "test":
        r = mailctl("alerts", "test")
        return f"Test alert sent to {r['sent_to']}."
    if act == "report":
        mailctl("report", "--force", timeout=300)
        return "Weekly report sent."
    mailctl("alerts", "config", "--email", request.form.get("email", "").strip(),
            "--weekly", "yes" if request.form.get("weekly") else "no")
    return "Alert settings saved."


@app.route("/delivery/dns-provider", methods=["POST"])
@admin_required
@action
def delivery_dns_provider():
    if request.form.get("action") == "off":
        mailctl("dns", "provider", "off")
        return "DNS provider disconnected; the API token was deleted."
    r = mailctl("dns", "provider", "set", request.form.get("provider", ""),
                input=request.form.get("token", "").strip() + "\n", timeout=60)
    return f"Connected to {r['name']}. Use 'Create records automatically' on a domain's page."


@app.route("/domains/<domain>/dns-apply", methods=["POST"])
@admin_required
def domain_dns_apply(domain):
    confirm = request.form.get("confirm") == "1"
    try:
        args = ["dns", "apply", domain] + (["--yes"] if confirm else [])
        r = mailctl(*args, timeout=180)
    except MailctlError as e:
        flash(str(e), "error")
        return redirect(url_for("domain_view", domain=domain))
    if confirm:
        invalidate("check")
        changed = sum(1 for p in r["plan"] if p["action"] in ("create", "update", "delete"))
        flash(f"{changed} DNS change(s) made at {r['provider']}. They usually take effect within minutes.", "ok")
        return redirect(url_for("domain_view", domain=domain))
    return render_template("dns_plan.html", domain=domain, r=r)


@app.route("/account/password", methods=["POST"])
@login_required
@action
def account_password():
    email = session["email"]
    current = request.form.get("current", "")
    new = request.form.get("new", "")
    if new != request.form.get("repeat", ""):
        raise MailctlError("the new passwords do not match")
    if too_many_failures(f"pw:{email}", limit=5):
        raise MailctlError("too many wrong passwords; try again in 10 minutes")
    if not imap_login_ok(email, current):
        record_failure(f"pw:{email}")
        log_auth("PASSWORD CHANGE FAILED", email)
        raise MailctlError("your current password is not correct")
    mailctl("user", "passwd", email, input=new + "\n")
    return "Password changed. Update it in your mail apps too."


@app.route("/account/2fa", methods=["GET", "POST"])
@login_required
def account_2fa():
    email = session["email"]
    if request.method == "POST":
        secret = session.get("totp_setup")
        if secret and totp_verify(email, secret, request.form.get("code")):
            data = totp_load()
            data[email] = secret
            totp_save(data)
            session.pop("totp_setup", None)
            log_auth("2FA ENABLED", email)
            flash("Two-factor sign-in is on. You will need a code from your app to sign in here.", "ok")
            return redirect(url_for("account"))
        flash("That code is not valid. Scan the QR code again and enter the current code.", "error")
    if "totp_setup" not in session or request.method == "GET":
        session["totp_setup"] = pyotp.random_base32()
    secret = session["totp_setup"]
    uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=f"{CONF.get('MAIL_HOSTNAME')}")
    return render_template("totp_setup.html", secret=secret, qr=qr_svg(uri), uri=uri)


@app.route("/account/2fa/disable", methods=["POST"])
@login_required
@action
def account_2fa_disable():
    email = session["email"]
    data = totp_load()
    secret = data.get(email)
    if not secret:
        raise MailctlError("two-factor sign-in is not on")
    if not totp_verify(email, secret, request.form.get("code")):
        raise MailctlError("that code is not valid")
    del data[email]
    totp_save(data)
    log_auth("2FA DISABLED", email)
    return "Two-factor sign-in is off."


@app.route("/account/apple.mobileconfig")
@login_required
def apple_profile():
    """Configuration profile for iPhone, iPad and Mac: mail + calendar + contacts."""
    email = session["email"]
    host = CONF["MAIL_HOSTNAME"]
    ident = ".".join(reversed(host.split(".")))

    def payload(kind, body):
        u = str(uuid.uuid4()).upper()
        return (f"<dict><key>PayloadType</key><string>{kind}</string>"
                f"<key>PayloadVersion</key><integer>1</integer>"
                f"<key>PayloadIdentifier</key><string>{ident}.{u}</string>"
                f"<key>PayloadUUID</key><string>{u}</string>{body}</dict>")

    esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    e = esc(email)
    items = [payload("com.apple.mail.managed",
                     f"<key>PayloadDisplayName</key><string>{e}</string>"
                     f"<key>EmailAccountDescription</key><string>{e}</string>"
                     f"<key>EmailAccountType</key><string>EmailTypeIMAP</string>"
                     f"<key>EmailAddress</key><string>{e}</string>"
                     f"<key>IncomingMailServerAuthentication</key><string>EmailAuthPassword</string>"
                     f"<key>IncomingMailServerHostName</key><string>{host}</string>"
                     f"<key>IncomingMailServerPortNumber</key><integer>993</integer>"
                     f"<key>IncomingMailServerUseSSL</key><true/>"
                     f"<key>IncomingMailServerUsername</key><string>{e}</string>"
                     f"<key>OutgoingMailServerAuthentication</key><string>EmailAuthPassword</string>"
                     f"<key>OutgoingMailServerHostName</key><string>{host}</string>"
                     f"<key>OutgoingMailServerPortNumber</key><integer>465</integer>"
                     f"<key>OutgoingMailServerUseSSL</key><true/>"
                     f"<key>OutgoingMailServerUsername</key><string>{e}</string>"
                     f"<key>OutgoingPasswordSameAsIncomingPassword</key><true/>")]
    if dav_enabled():
        for kind, pre in (("com.apple.caldav.account", "CalDAV"), ("com.apple.carddav.account", "CardDAV")):
            items.append(payload(kind,
                                 f"<key>PayloadDisplayName</key><string>{pre} {e}</string>"
                                 f"<key>{pre}AccountDescription</key><string>{e}</string>"
                                 f"<key>{pre}HostName</key><string>{host}</string>"
                                 f"<key>{pre}Port</key><integer>443</integer>"
                                 f"<key>{pre}PrincipalURL</key><string>/dav/{e}/</string>"
                                 f"<key>{pre}UseSSL</key><true/>"
                                 f"<key>{pre}Username</key><string>{e}</string>"))
    u = str(uuid.uuid4()).upper()
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>'
            f"<key>PayloadContent</key><array>{''.join(items)}</array>"
            f"<key>PayloadDisplayName</key><string>{e} ({host})</string>"
            f"<key>PayloadIdentifier</key><string>{ident}.profile.{u}</string>"
            f"<key>PayloadType</key><string>Configuration</string>"
            f"<key>PayloadUUID</key><string>{u}</string>"
            f"<key>PayloadVersion</key><integer>1</integer>"
            "</dict></plist>\n")
    return Response(body, mimetype="application/x-apple-aspen-config",
                    headers={"Content-Disposition": f'attachment; filename="{host}.mobileconfig"'})


# ------------------------------------------------------------------ nginx auth for CalDAV/CardDAV

@app.route("/_auth/dav")
def dav_auth():
    """nginx auth_request endpoint: checks HTTP Basic credentials against Dovecot."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Basic "):
        return Response(status=401)
    try:
        user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return Response(status=401)
    user = user.strip().lower()
    if not re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", user):
        return Response(status=401)
    key = hashlib.sha256(f"{user}\0{pw}".encode()).hexdigest()
    now = time.time()
    with _lock:
        ok = _dav_ok.get(key, 0) > now
    if not ok:
        ip = client_ip()
        if too_many_failures(f"dav:{ip}", limit=10) or too_many_failures(f"user:{user}", limit=10):
            return Response(status=401)
        try:
            ok = imap_login_ok(user, pw)
        except OSError:
            return Response(status=503)
        if not ok:
            record_failure(f"dav:{ip}", f"user:{user}")
            log_auth("DAV FAILED", user)
            return Response(status=401)
        with _lock:
            _dav_ok[key] = now + 300
            if len(_dav_ok) > 10000:
                _dav_ok.clear()
    return Response(status=200, headers={"X-Remote-User": user})
