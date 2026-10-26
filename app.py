#!/usr/bin/env python3
"""LocalPanel - panel hosting sederhana ala cPanel (Nginx + PHP-FPM + MariaDB)."""
import functools, glob, json, os, re, secrets, shutil, subprocess, threading, time
from pathlib import Path

import psutil, pymysql
from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import apps as appmgr
import backup as bk
import cloudflare as cfapi
import gitdeploy
import nodeapp
import pgsql

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
WWW = Path("/var/www")
NGINX_AVAIL = Path("/etc/nginx/sites-available")
NGINX_ENABLED = Path("/etc/nginx/sites-enabled")
SITE_PREFIX = "lp-"  # nama file vhost yang dikelola panel
# versi PHP-FPM terpasang (mis. 8.5 di Ubuntu 26.04, 8.3 di Ubuntu 24.04)
PHP_VER = (sorted(Path(p).parent.name for p in glob.glob("/etc/php/*/fpm")) or ["8.3"])[-1]
SERVICES = ["nginx", "mariadb", f"php{PHP_VER}-fpm", "ssh", "cron"] + (["postgresql"] if pgsql.available() else [])
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
DBNAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,64}$")


def load_config():
    if not CONFIG.exists():
        cfg = {"username": "admin", "password_hash": generate_password_hash("admin123"),
               "secret_key": secrets.token_hex(32), "port": 2083}
        CONFIG.write_text(json.dumps(cfg, indent=2))
        CONFIG.chmod(0o600)
    return json.loads(CONFIG.read_text())


cfg = load_config()
app = Flask(__name__)
app.secret_key = cfg["secret_key"]
app.config.update(MAX_CONTENT_LENGTH=512 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax")


# ---------- helper ----------
def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def login_required(f):
    @functools.wraps(f)
    def wrapper(*a, **kw):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


@app.before_request
def csrf_protect():
    if request.method == "POST" and request.endpoint not in ("login", "git_hook"):
        if request.form.get("csrf") != session.get("csrf"):
            abort(400, "CSRF token tidak valid")


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf": session["csrf"], "user": session.get("user"), "php_ver": PHP_VER}


def db():
    # panel berjalan sebagai root -> login MariaDB lewat unix_socket
    return pymysql.connect(unix_socket="/run/mysqld/mysqld.sock", user="root", autocommit=True)


def fmt_size(n):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


app.jinja_env.filters["size"] = fmt_size
app.jinja_env.filters["datetime"] = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def safe_path(rel):
    p = (WWW / (rel or "")).resolve()
    if p != WWW and WWW not in p.parents:
        abort(403)
    return p


# ---------- auth ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        time.sleep(0.5)
        if (request.form.get("username") == cfg["username"]
                and check_password_hash(cfg["password_hash"], request.form.get("password", ""))):
            session.clear()
            session["user"] = cfg["username"]
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        flash("Username atau password salah", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required
def password():
    if request.method == "POST":
        if not check_password_hash(cfg["password_hash"], request.form.get("old", "")):
            flash("Password lama salah", "error")
        elif len(request.form.get("new", "")) < 8:
            flash("Password baru minimal 8 karakter", "error")
        else:
            cfg["password_hash"] = generate_password_hash(request.form["new"])
            CONFIG.write_text(json.dumps(cfg, indent=2))
            flash("Password berhasil diganti", "ok")
    return render_template("password.html")


# ---------- dashboard ----------
@app.route("/")
@login_required
def dashboard():
    vm, du = psutil.virtual_memory(), psutil.disk_usage("/")
    stats = {
        "cpu": psutil.cpu_percent(interval=0.3), "cores": psutil.cpu_count(),
        "load": os.getloadavg(), "mem": vm, "disk": du,
        "uptime": int(time.time() - psutil.boot_time()),
        "hostname": os.uname().nodename,
        "ips": [a.address for addrs in psutil.net_if_addrs().values() for a in addrs
                if a.family == 2 and not a.address.startswith("127.")],
    }
    svc = {s: run(["systemctl", "is-active", s])[1] for s in SERVICES}
    return render_template("dashboard.html", s=stats, svc=svc, sites=len(list_sites()))


# ---------- cloudflare ----------
def cf():
    return cfapi.Cloudflare(cfg.get("cf_token"))


def cf_enabled():
    return bool(cfg.get("cf_token"))


def save_config():
    CONFIG.write_text(json.dumps(cfg, indent=2))
    CONFIG.chmod(0o600)


@app.route("/cloudflare", methods=["GET", "POST"])
@login_required
def cloudflare():
    if request.method == "POST":
        act = request.form.get("action")
        if act == "save":
            token = request.form.get("token", "").strip()
            if token:
                try:
                    zones = cfapi.Cloudflare(token).verify()
                    cfg["cf_token"] = token
                    flash("Token valid. Zone terdeteksi: " + (", ".join(zones) or "(tidak ada)"), "ok")
                except cfapi.CFError as e:
                    flash(f"Token ditolak: {e}", "error")
            cfg["cf_auto"] = request.form.get("auto") == "1"
            save_config()
        elif act == "remove":
            cfg.pop("cf_token", None)
            save_config()
            flash("Token Cloudflare dihapus", "ok")
        return redirect(url_for("cloudflare"))
    account_id, tunnel_id = cfapi.tunnel_ids()
    hosts, zones, err = {}, [], None
    if cf_enabled():
        try:
            c = cf()
            hosts, zones = c.public_hostnames(), c.verify()
        except cfapi.CFError as e:
            err = str(e)
    return render_template("cloudflare.html", account_id=account_id, tunnel_id=tunnel_id,
                           enabled=cf_enabled(), auto=cfg.get("cf_auto", True),
                           hosts=hosts, zones=zones, err=err)


# ---------- websites ----------
def list_sites():
    out = []
    for f in sorted(NGINX_AVAIL.glob(SITE_PREFIX + "*.conf")):
        d = f.stem[len(SITE_PREFIX):]
        out.append({"domain": d, "enabled": (NGINX_ENABLED / f.name).exists(),
                    "root": WWW / d / "public_html"})
    return out


VHOST = """# Dikelola oleh LocalPanel
server {{
    listen 80;
    listen [::]:80;
    server_name {domain} www.{domain};
    root {root};
    index index.php index.html index.htm;
    client_max_body_size 520M;
    fastcgi_read_timeout 300;
    access_log /var/log/nginx/{domain}.access.log;
    error_log /var/log/nginx/{domain}.error.log;

    location / {{
        try_files $uri $uri/ /index.php?$query_string;
    }}
    location ~ \\.php(/|$) {{
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php-fpm.sock;
        fastcgi_param HTTPS $lp_https;
    }}
    location ~ /\\.(?!well-known) {{ deny all; }}
}}
"""


def hosts_entry(domain, add):
    lines = Path("/etc/hosts").read_text().splitlines()
    tag = f"# localpanel:{domain}"
    lines = [l for l in lines if not l.endswith(tag)]
    if add:
        lines.append(f"127.0.0.1 {domain} www.{domain} {tag}")
    Path("/etc/hosts").write_text("\n".join(lines) + "\n")


_zones_cache = {"t": 0, "zones": []}


def cf_zones():
    """Domain aktif di Cloudflare (di-cache 5 menit agar halaman tetap cepat)."""
    if not cf_enabled():
        return []
    if time.time() - _zones_cache["t"] > 300:
        try:
            _zones_cache.update(t=time.time(), zones=cf().zones())
        except cfapi.CFError:
            return _zones_cache["zones"]
    return _zones_cache["zones"]


def form_domain():
    """Domain dari form: subdomain + domain Cloudflare yang dipilih, atau domain yang diketik manual."""
    zone = request.form.get("zone", "").strip().lower()
    if zone:
        sub = request.form.get("sub", "").strip().lower().strip(".")
        return f"{sub}.{zone}" if sub else zone
    return request.form.get("domain", "").strip().lower()


def in_cf_zone(domain):
    return any(domain == z or domain.endswith("." + z) for z in cf_zones())


def publish_conflict(domain):
    """Cek bentrok DNS sebelum website dibuat, supaya domain yang sudah dipakai tidak setengah jadi."""
    try:
        return cf().conflict(domain)
    except cfapi.CFError as e:
        return str(e)


def site_exists(domain):
    return (NGINX_AVAIL / f"{SITE_PREFIX}{domain}.conf").exists()


def create_site(domain, root=None, index=True):
    """Buat vhost nginx + folder website. Kembalikan pesan error, atau None bila berhasil."""
    if not DOMAIN_RE.match(domain):
        return "Nama domain tidak valid (contoh: tokoku.test)"
    conf = NGINX_AVAIL / f"{SITE_PREFIX}{domain}.conf"
    if conf.exists():
        return "Website sudah ada"
    root = Path(root) if root else WWW / domain / "public_html"
    root.mkdir(parents=True, exist_ok=True)
    idx = root / "index.php"
    if index and not any(root.iterdir()):
        idx.write_text(f"<h1>{domain}</h1>\n<p>Website aktif. Dibuat oleh LocalPanel.</p>\n"
                       "<?php echo '<p>PHP ' . PHP_VERSION . '</p>'; ?>\n")
    run(["chown", "-R", "www-data:www-data", str(WWW / domain)])
    conf.write_text(VHOST.format(domain=domain, root=root))
    (NGINX_ENABLED / conf.name).symlink_to(conf)
    rc, out = run(["nginx", "-t"])
    if rc != 0:
        (NGINX_ENABLED / conf.name).unlink()
        conf.unlink()
        return "Konfigurasi nginx gagal: " + out
    run(["systemctl", "reload", "nginx"])
    hosts_entry(domain, True)
    return None


def publish_site(domain):
    """Publikasikan via Cloudflare bila aktif. Kembalikan (kategori, pesan) atau None."""
    if not cf_enabled():
        return None
    try:
        hosts = cf().publish(domain)
        return "ok", "Dipublikasikan via Cloudflare Tunnel: " + ", ".join(f"https://{h}" for h in hosts)
    except cfapi.CFError as e:
        return "error", f"Website dibuat, tapi gagal publikasi Cloudflare: {e}"


def site_url(domain):
    """URL utama website: https bila dipublikasikan lewat Cloudflare, selain itu http."""
    try:
        if cf_enabled() and domain in cf().public_hostnames():
            return f"https://{domain}"
    except cfapi.CFError:
        pass
    return f"http://{domain}"


@app.route("/sites", methods=["GET", "POST"])
@login_required
def sites():
    if request.method == "POST":
        domain = form_domain()
        publish = request.form.get("publish") == "1" and in_cf_zone(domain)
        err = (publish and DOMAIN_RE.match(domain) and not site_exists(domain) and publish_conflict(domain)) \
            or create_site(domain)
        if err:
            flash(err, "error")
        else:
            flash(f"Website {domain} berhasil dibuat", "ok")
            if publish and (msg := publish_site(domain)):
                flash(msg[1], msg[0])
        return redirect(url_for("sites"))
    public, zones, cf_err = {}, [], None
    if cf_enabled():
        try:
            c = cf()
            public, zones = c.public_hostnames(), cf_zones()
        except cfapi.CFError as e:
            cf_err = str(e)
    sites_ = list_sites()
    for x in sites_:
        x["git"] = cfg.get("git", {}).get(x["domain"])
        x["node"] = cfg.get("node", {}).get(x["domain"])
        x["publishable"] = any(x["domain"] == z or x["domain"].endswith("." + z) for z in zones)
    return render_template("sites.html", sites=sites_, public=public, zones=zones, cf_on=cf_enabled(),
                           cf_auto=cfg.get("cf_auto", True), cf_err=cf_err)


@app.post("/sites/<domain>/publish")
@login_required
def site_publish(domain):
    if not DOMAIN_RE.match(domain) or not (NGINX_AVAIL / f"{SITE_PREFIX}{domain}.conf").exists():
        abort(404)
    try:
        if request.form.get("undo") == "1":
            cf().unpublish(domain)
            flash(f"{domain} tidak lagi dipublikasikan", "ok")
        else:
            hosts = cf().publish(domain)
            flash("Dipublikasikan: " + ", ".join(f"https://{h}" for h in hosts), "ok")
    except cfapi.CFError as e:
        flash(f"Cloudflare: {e}", "error")
    return redirect(url_for("sites"))


@app.post("/sites/<domain>/toggle")
@login_required
def site_toggle(domain):
    conf = NGINX_AVAIL / f"{SITE_PREFIX}{domain}.conf"
    if not DOMAIN_RE.match(domain) or not conf.exists():
        abort(404)
    link = NGINX_ENABLED / conf.name
    link.unlink() if link.exists() else link.symlink_to(conf)
    run(["systemctl", "reload", "nginx"])
    flash(f"Status {domain} diubah", "ok")
    return redirect(url_for("sites"))


@app.post("/sites/<domain>/delete")
@login_required
def site_delete(domain):
    conf = NGINX_AVAIL / f"{SITE_PREFIX}{domain}.conf"
    if not DOMAIN_RE.match(domain) or not conf.exists():
        abort(404)
    (NGINX_ENABLED / conf.name).unlink(missing_ok=True)
    conf.unlink()
    run(["systemctl", "reload", "nginx"])
    hosts_entry(domain, False)
    if cf_enabled():
        try:
            c = cf()
            if domain in c.public_hostnames():
                c.unpublish(domain)
        except cfapi.CFError as e:
            flash(f"Rute Cloudflare untuk {domain} gagal dihapus: {e}", "error")
    wipe = request.form.get("files") == "1"
    remove_site_data(domain, wipe)
    if wipe:
        shutil.rmtree(WWW / domain, ignore_errors=True)
    flash(f"Website {domain} dihapus", "ok")
    return redirect(url_for("sites"))


def remove_site_data(domain, drop_db):
    """Matikan queue/cron, hapus pengaturan panel, dan (opsional) database milik website."""
    dbs = bk.site_dbs(cfg, domain)
    pg_dbs = bk.site_pg_dbs(cfg, domain)
    laravel_set(domain, "queue", False)
    laravel_set(domain, "schedule", False)
    app_cron_file(domain).unlink(missing_ok=True)
    if cfg.get("node", {}).pop(domain, None) is not None:
        nodeapp.remove(domain)
    cfg.get("git", {}).pop(domain, None)
    cfg.get("apps", {}).pop(domain, None)
    cfg.get("backup_schedule", {}).pop(domain, None)
    save_config()
    gitdeploy.remove_key(domain)
    if drop_db:
        conn = db()
        for d in dbs:
            conn.cursor().execute(f"DROP DATABASE IF EXISTS `{d['name']}`")
            if d.get("user") and d["user"] != "root":
                conn.cursor().execute("DROP USER IF EXISTS %s@'localhost'", (d["user"],))
        conn.close()
        for d in pg_dbs:
            try:
                pgsql.drop_db(d["name"])
                pgsql.drop_user(d["user"])
            except pgsql.PGError:
                pass


# ---------- git deploy ----------
def set_site_fallback(domain, target):
    """Ubah target try_files di vhost (mis. /r.php untuk router Moodle)."""
    conf = NGINX_AVAIL / f"{SITE_PREFIX}{domain}.conf"
    old = conf.read_text()
    conf.write_text(re.sub(r"try_files \$uri \$uri/ \S+;", f"try_files $uri $uri/ {target}?$query_string;", old, count=1))
    rc, out = run(["nginx", "-t"])
    if rc != 0:
        conf.write_text(old)
        raise RuntimeError(out)
    run(["systemctl", "reload", "nginx"])


def set_site_root(domain, root):
    conf = NGINX_AVAIL / f"{SITE_PREFIX}{domain}.conf"
    old = conf.read_text()
    conf.write_text(re.sub(r"(?m)^    root .*;$", f"    root {root};", old, count=1))
    rc, out = run(["nginx", "-t"])
    if rc != 0:
        conf.write_text(old)
        raise RuntimeError(out)
    run(["systemctl", "reload", "nginx"])


def git_done(domain):
    def cb(ok):
        g = cfg.get("git", {}).get(domain)
        if g is not None:
            g["last"] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": ok}
            save_config()
        # worker harus dimuat ulang agar memakai kode terbaru
        if ok and laravel_status(domain)["queue"]:
            run(["systemctl", "restart", queue_unit(domain)])
        n = cfg.get("node", {}).get(domain)
        if ok and n and nodeapp.status(domain)["enabled"]:
            if n.get("auto_install", True):
                nodeapp.task(WWW, domain, n, ["install", "build"])
            else:
                nodeapp.control(domain, "restart")
    return cb


def ensure_db(creds):
    """Buat database + user (atau samakan password bila sudah ada) dan beri hak penuh."""
    conn = db()
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{creds['name']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    cur.execute("CREATE USER IF NOT EXISTS %s@'localhost' IDENTIFIED BY %s", (creds["user"], creds["password"]))
    cur.execute("ALTER USER %s@'localhost' IDENTIFIED BY %s", (creds["user"], creds["password"]))
    cur.execute(f"GRANT ALL PRIVILEGES ON `{creds['name']}`.* TO %s@'localhost'", (creds["user"],))
    conn.close()


def site_db(domain, section="git"):
    """Buat (atau pakai ulang) database + user khusus untuk website. Dipanggil saat deploy/instal pertama."""
    def make():
        g = cfg[section][domain]
        creds = g.get("db") or {"name": bk.slug(domain)[:64], "user": bk.slug(domain)[:32],
                                "password": secrets.token_urlsafe(18)}
        ensure_db(creds)
        g["db"] = creds
        save_config()
        return creds
    return make


def site_pg(domain, section):
    """Seperti site_db, tetapi database PostgreSQL (disimpan di bagian "pg")."""
    def make():
        g = cfg[section][domain]
        creds = g.get("pg") or {"name": bk.slug(domain)[:63], "user": bk.slug(domain)[:63],
                                "password": secrets.token_urlsafe(18)}
        pgsql.ensure_db(creds)
        g["pg"] = creds
        save_config()
        return creds
    return make


# ---------- laravel: queue & scheduler ----------
def queue_unit(domain):
    return f"localpanel-queue@{domain}.service"


def cron_file(domain):
    return Path("/etc/cron.d") / ("localpanel-" + domain.replace(".", "_"))


def app_cron_file(domain):
    return Path("/etc/cron.d") / f"localpanel-app-{bk.slug(domain)}"


def laravel_status(domain):
    unit = queue_unit(domain)
    return {"artisan": (WWW / domain / "repo" / "artisan").exists(),
            "queue": run(["systemctl", "is-enabled", unit])[1] == "enabled",
            "queue_state": run(["systemctl", "is-active", unit])[1],
            "schedule": cron_file(domain).exists()}


def laravel_set(domain, what, on):
    if what == "queue":
        run(["systemctl", "enable" if on else "disable", "--now", queue_unit(domain)])
    elif what == "schedule":
        if on:
            cron_file(domain).write_text(
                "# Dikelola oleh LocalPanel: Laravel scheduler\n"
                f"* * * * * www-data cd {WWW / domain / 'repo'} && /usr/bin/php artisan schedule:run >> /dev/null 2>&1\n")
            cron_file(domain).chmod(0o644)
        else:
            cron_file(domain).unlink(missing_ok=True)


@app.post("/sites/<domain>/laravel")
@login_required
def site_laravel(domain):
    if not DOMAIN_RE.match(domain) or not (NGINX_AVAIL / f"{SITE_PREFIX}{domain}.conf").exists():
        abort(404)
    what, act = request.form.get("what"), request.form.get("action")
    if what in ("queue", "schedule") and act in ("on", "off"):
        if act == "on" and not laravel_status(domain)["artisan"]:
            flash("File artisan tidak ditemukan. Deploy aplikasi Laravel dulu.", "error")
        else:
            laravel_set(domain, what, act == "on")
            name = "Queue worker" if what == "queue" else "Scheduler"
            flash(f"{name} {'diaktifkan' if act == 'on' else 'dimatikan'}", "ok")
    elif what == "queue" and act == "restart":
        run(["systemctl", "restart", queue_unit(domain)])
        flash("Queue worker di-restart", "ok")
    return redirect(url_for("site_git", domain=domain) + "#laravel")


@app.route("/sites/<domain>/git", methods=["GET", "POST"])
@login_required
def site_git(domain):
    if not DOMAIN_RE.match(domain) or not (NGINX_AVAIL / f"{SITE_PREFIX}{domain}.conf").exists():
        abort(404)
    allg = cfg.setdefault("git", {})
    g = allg.get(domain)
    if request.method == "POST":
        act = request.form.get("action")
        if act == "save":
            repo = request.form.get("repo", "").strip()
            branch = request.form.get("branch", "").strip() or "main"
            subdir = request.form.get("subdir", "").strip().strip("/")
            if not gitdeploy.REPO_RE.match(repo):
                flash("URL repo tidak valid. Contoh: https://github.com/user/repo.git atau git@github.com:user/repo.git", "error")
            elif not gitdeploy.BRANCH_RE.match(branch) or not gitdeploy.SUBDIR_RE.match(subdir) or ".." in subdir:
                flash("Branch atau folder publik tidak valid", "error")
            else:
                first = g is None
                g = allg.setdefault(domain, {"secret": secrets.token_hex(20)})
                g.update(repo=repo, branch=branch, subdir=subdir,
                         post_cmd=request.form.get("post_cmd", "").replace("\r\n", "\n").strip())
                save_config()
                try:
                    root = gitdeploy.repo_dir(WWW, domain) / subdir if subdir else gitdeploy.repo_dir(WWW, domain)
                    set_site_root(domain, root)
                except RuntimeError as e:
                    flash(f"Gagal mengubah root nginx: {e}", "error")
                if first or request.form.get("deploy_now") == "1":
                    gitdeploy.deploy(WWW, domain, g, git_done(domain), site_db(domain))
                    flash("Pengaturan disimpan, deploy dimulai…", "ok")
                else:
                    flash("Pengaturan disimpan", "ok")
        elif act == "deploy" and g:
            if gitdeploy.deploy(WWW, domain, g, git_done(domain), site_db(domain)):
                flash("Deploy dimulai…", "ok")
            else:
                flash("Deploy masih berjalan", "error")
        elif act == "import_sql" and g and g.get("db"):
            dump = gitdeploy.find_sql_dump(gitdeploy.repo_dir(WWW, domain))
            if not dump:
                flash("Tidak ada file .sql di folder database/ atau root repo", "error")
            else:
                bdir = BASE / "db-backups"
                bdir.mkdir(mode=0o700, exist_ok=True)
                backup = bdir / f"{g['db']['name']}-{time.strftime('%Y%m%d-%H%M%S')}.sql"
                with open(backup, "w") as f:
                    subprocess.run(["mysqldump", g["db"]["name"]], stdout=f)
                try:
                    gitdeploy.import_sql(g["db"]["name"], dump)
                    flash(f"{dump.name} berhasil diimport. Backup sebelumnya: {backup}", "ok")
                except RuntimeError as e:
                    flash(str(e), "error")
        elif act == "secret" and g:
            g["secret"] = secrets.token_hex(20)
            save_config()
            flash("Secret webhook dibuat ulang. Perbarui juga di GitHub.", "ok")
        elif act == "disconnect" and g:
            laravel_set(domain, "queue", False)
            laravel_set(domain, "schedule", False)
            allg.pop(domain)
            save_config()
            try:
                set_site_root(domain, WWW / domain / "public_html")
            except RuntimeError as e:
                flash(f"Gagal mengubah root nginx: {e}", "error")
            if request.form.get("delete_repo") == "1":
                shutil.rmtree(gitdeploy.repo_dir(WWW, domain), ignore_errors=True)
                shutil.rmtree(gitdeploy.src_dir(WWW, domain), ignore_errors=True)
            flash("Git diputus. Website kembali memakai public_html.", "ok")
        return redirect(url_for("site_git", domain=domain))
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return render_template("git.html", domain=domain, g=g, pubkey=gitdeploy.ensure_key(domain),
                           hook=f"{proto}://{request.host}/hook/{domain}",
                           running=gitdeploy.is_running(domain), log=gitdeploy.read_log(domain),
                           dump=gitdeploy.find_sql_dump(gitdeploy.repo_dir(WWW, domain)) if g and g.get("db") else None,
                           lv=laravel_status(domain) if g else None,
                           qlog=run(["journalctl", "-u", queue_unit(domain), "-n", "30", "--no-pager", "-o", "short-iso"])[1]
                           if g and laravel_status(domain)["queue"] else "")


@app.post("/hook/<domain>")
def git_hook(domain):
    g = cfg.get("git", {}).get(domain)
    if not g:
        abort(404)
    if not gitdeploy.verify_signature(g["secret"], request.get_data(), request.headers.get("X-Hub-Signature-256")):
        abort(403)
    event = request.headers.get("X-GitHub-Event", "push")
    if event == "ping":
        return {"ok": True, "msg": "pong"}
    payload = request.get_json(silent=True)
    if payload is None and request.form.get("payload"):
        payload = json.loads(request.form["payload"])
    ref = (payload or {}).get("ref", "")
    if event != "push" or ref != f"refs/heads/{g['branch']}":
        return {"ok": True, "msg": f"diabaikan ({event} {ref})"}
    started = gitdeploy.deploy(WWW, domain, g, git_done(domain), site_db(domain))
    return {"ok": True, "msg": "deploy dimulai" if started else "deploy sedang berjalan"}, 202


# ---------- databases ----------
SYSTEM_DBS = {"information_schema", "mysql", "performance_schema", "sys"}


@app.route("/databases", methods=["GET", "POST"])
@login_required
def databases():
    conn = db()
    cur = conn.cursor()
    if request.method == "POST":
        name, user, pw = (request.form.get(k, "").strip() for k in ("name", "user", "password"))
        if not DBNAME_RE.match(name) or (user and not DBNAME_RE.match(user)):
            flash("Nama database/user hanya boleh huruf, angka, underscore", "error")
        elif request.form.get("engine") == "pgsql":
            try:
                if user:
                    pw = pw or secrets.token_urlsafe(12)
                    pgsql.ensure_user(user, pw)
                pgsql.create_db(name, user or None)
                flash(f"Database PostgreSQL {name} dibuat" + (f". User: {user} / Password: {pw}" if user else ""), "ok")
            except pgsql.PGError as e:
                flash(f"Gagal: {e}", "error")
        else:
            try:
                cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                if user:
                    pw = pw or secrets.token_urlsafe(12)
                    cur.execute("CREATE USER IF NOT EXISTS %s@'localhost' IDENTIFIED BY %s", (user, pw))
                    cur.execute(f"GRANT ALL PRIVILEGES ON `{name}`.* TO %s@'localhost'", (user,))
                    flash(f"Database {name} dibuat. User: {user} / Password: {pw}", "ok")
                else:
                    flash(f"Database {name} dibuat", "ok")
            except pymysql.MySQLError as e:
                flash(f"Gagal: {e.args[-1]}", "error")
        return redirect(url_for("databases"))
    cur.execute("SELECT s.schema_name, COALESCE(SUM(t.data_length+t.index_length),0), COUNT(t.table_name) "
                "FROM information_schema.schemata s LEFT JOIN information_schema.tables t "
                "ON t.table_schema=s.schema_name GROUP BY s.schema_name ORDER BY s.schema_name")
    dbs = [{"name": r[0], "size": int(r[1]), "tables": r[2]} for r in cur.fetchall() if r[0] not in SYSTEM_DBS]
    cur.execute("SELECT User, Host FROM mysql.user WHERE User NOT IN ('root','mysql','mariadb.sys','') ORDER BY User")
    users = cur.fetchall()
    conn.close()
    pg = None
    if pgsql.available():
        try:
            pg = {"dbs": pgsql.list_dbs(), "users": pgsql.list_users()}
        except pgsql.PGError as e:
            flash(f"PostgreSQL: {e}", "error")
    return render_template("databases.html", dbs=dbs, users=users, pg=pg)


@app.post("/pg/<name>/delete")
@login_required
def pg_delete(name):
    if not DBNAME_RE.match(name) or name in pgsql.SYSTEM_DBS:
        abort(400)
    try:
        pgsql.drop_db(name)
        flash(f"Database PostgreSQL {name} dihapus", "ok")
    except pgsql.PGError as e:
        flash(f"Gagal: {e}", "error")
    return redirect(url_for("databases") + "#pg")


@app.post("/pgusers/<user>/delete")
@login_required
def pguser_delete(user):
    if not DBNAME_RE.match(user) or user == "postgres":
        abort(400)
    try:
        pgsql.drop_user(user)
        flash(f"User PostgreSQL {user} dihapus", "ok")
    except pgsql.PGError as e:
        flash(f"Gagal: {e}", "error")
    return redirect(url_for("databases") + "#pg")


@app.post("/databases/<name>/delete")
@login_required
def db_delete(name):
    if not DBNAME_RE.match(name) or name in SYSTEM_DBS:
        abort(400)
    conn = db()
    conn.cursor().execute(f"DROP DATABASE IF EXISTS `{name}`")
    conn.close()
    flash(f"Database {name} dihapus", "ok")
    return redirect(url_for("databases"))


@app.post("/dbusers/<user>/delete")
@login_required
def dbuser_delete(user):
    if not DBNAME_RE.match(user) or user in ("root", "mysql"):
        abort(400)
    conn = db()
    conn.cursor().execute("DROP USER IF EXISTS %s@'localhost'", (user,))
    conn.close()
    flash(f"User {user} dihapus", "ok")
    return redirect(url_for("databases"))


# ---------- file manager ----------
@app.route("/files")
@login_required
def files():
    rel = request.args.get("p", "")
    p = safe_path(rel)
    if p.is_file():
        return send_file(p, as_attachment=True)
    if not p.exists():
        abort(404)
    items = []
    for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        st = c.lstat()
        items.append({"name": c.name, "dir": c.is_dir(), "size": st.st_size,
                      "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                      "rel": str(c.relative_to(WWW))})
    rel = "" if p == WWW else str(p.relative_to(WWW))
    crumbs, acc = [], ""
    for part in Path(rel).parts:
        acc = f"{acc}/{part}" if acc else part
        crumbs.append((part, acc))
    parent = str(p.parent.relative_to(WWW)) if p != WWW else None
    return render_template("files.html", items=items, rel=rel, crumbs=crumbs,
                           parent="" if parent == "." else parent)


def fix_owner(path):
    run(["chown", "-R", "www-data:www-data", str(path)])


@app.post("/files/action")
@login_required
def files_action():
    rel = request.form.get("p", "")
    d = safe_path(rel)
    act = request.form.get("action")
    name = secure_filename(request.form.get("name", "")) if request.form.get("name") else ""
    try:
        if act == "upload":
            for f in request.files.getlist("files"):
                fn = secure_filename(f.filename or "")
                if fn:
                    f.save(d / fn)
                    fix_owner(d / fn)
            flash("Upload selesai", "ok")
        elif act == "mkdir" and name:
            (d / name).mkdir()
            fix_owner(d / name)
        elif act == "mkfile" and name:
            (d / name).touch()
            fix_owner(d / name)
        elif act == "delete":
            t = safe_path(request.form.get("target"))
            if t == WWW:
                abort(403)
            shutil.rmtree(t) if t.is_dir() and not t.is_symlink() else t.unlink()
            flash(f"{t.name} dihapus", "ok")
        elif act == "rename" and name:
            t = safe_path(request.form.get("target"))
            t.rename(t.parent / name)
        elif act == "unzip":
            t = safe_path(request.form.get("target"))
            rc, out = run(["unzip", "-o", "-q", str(t), "-d", str(t.parent)])
            fix_owner(t.parent)
            flash("Ekstrak selesai" if rc == 0 else "Gagal ekstrak: " + out, "ok" if rc == 0 else "error")
    except OSError as e:
        flash(f"Gagal: {e.strerror or e}", "error")
    return redirect(url_for("files", p=rel))


@app.route("/files/edit", methods=["GET", "POST"])
@login_required
def files_edit():
    rel = request.values.get("p", "")
    p = safe_path(rel)
    if not p.is_file():
        abort(404)
    if request.method == "POST":
        p.write_text(request.form.get("content", "").replace("\r\n", "\n"))
        flash("File disimpan", "ok")
        return redirect(url_for("files_edit", p=rel))
    if p.stat().st_size > 2 * 1024 * 1024:
        flash("File terlalu besar untuk diedit", "error")
        return redirect(url_for("files", p=str(p.parent.relative_to(WWW))))
    try:
        content = p.read_text()
    except UnicodeDecodeError:
        flash("File biner tidak bisa diedit", "error")
        return redirect(url_for("files", p=str(p.parent.relative_to(WWW))))
    return render_template("edit.html", rel=rel, content=content,
                           parent="" if p.parent == WWW else str(p.parent.relative_to(WWW)))


# ---------- services & logs ----------
@app.route("/services", methods=["GET", "POST"])
@login_required
def services():
    if request.method == "POST":
        s, act = request.form.get("service"), request.form.get("action")
        if s in SERVICES and act in ("start", "stop", "restart") and not (s == "ssh" and act == "stop"):
            rc, out = run(["systemctl", act, s])
            flash(f"{act} {s}: " + ("berhasil" if rc == 0 else out), "ok" if rc == 0 else "error")
        return redirect(url_for("services"))
    svc = []
    for s in SERVICES:
        _, state = run(["systemctl", "is-active", s])
        _, enabled = run(["systemctl", "is-enabled", s])
        svc.append({"name": s, "state": state, "enabled": enabled})
    return render_template("services.html", svc=svc)


@app.route("/logs")
@login_required
def logs():
    logfiles = sorted(glob.glob("/var/log/nginx/*.log")) + [f"/var/log/php{PHP_VER}-fpm.log"]
    logfiles = [l for l in logfiles if os.path.exists(l)]
    sel = request.args.get("f", "/var/log/nginx/error.log")
    if sel not in logfiles:
        sel = logfiles[0] if logfiles else None
    content = run(["tail", "-n", "200", sel])[1] if sel else ""
    return render_template("logs.html", logfiles=logfiles, sel=sel, content=content)


# ---------- application manager ----------
def free_sites():
    """Website yang belum dipakai Git/aplikasi (bisa dipasangi aplikasi)."""
    return [x["domain"] for x in list_sites()
            if x["domain"] not in cfg.get("git", {}) and x["domain"] not in cfg.get("apps", {})
            and x["domain"] not in cfg.get("node", {})]


def app_done(domain):
    def cb(ok, result):
        a = cfg.setdefault("apps", {}).get(domain)
        if a is None:
            return
        a["status"] = "ok" if ok else "failed"
        a["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if not ok:
            save_config()
            return
        a.update(version=result.get("version"), admin_url=result.get("admin_url"),
                 root=str(result["root"]), fallback=result.get("fallback"))
        set_site_root(domain, result["root"])
        if result.get("fallback"):
            set_site_fallback(domain, result["fallback"])
        if result.get("cron"):
            f = app_cron_file(domain)
            f.write_text(f"# Dikelola oleh LocalPanel: cron aplikasi\n* * * * * www-data {result['cron']} >/dev/null 2>&1\n")
            f.chmod(0o644)
        save_config()
    return cb


@app.route("/apps", methods=["GET", "POST"])
@login_required
def apps_page():
    if request.method == "POST":
        app_id = request.form.get("app")
        domain = form_domain()
        recipe = appmgr.CATALOG.get(app_id)
        admin = {"title": request.form.get("title", "").strip() or domain,
                 "user": request.form.get("admin_user", "").strip() or "admin",
                 "email": request.form.get("admin_email", "").strip(),
                 "password": request.form.get("admin_password", "") or secrets.token_urlsafe(12),
                 "lang": request.form.get("lang", "id"), "sample": request.form.get("sample") == "1"}
        if not recipe:
            flash("Pilih aplikasi", "error")
        elif not DOMAIN_RE.match(domain):
            flash("Nama domain tidak valid", "error")
        elif domain in cfg.get("git", {}) or domain in cfg.get("apps", {}) or domain in cfg.get("node", {}):
            flash(f"{domain} sudah dipakai Git deploy / aplikasi lain / Node.js", "error")
        elif recipe["admin"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", admin["email"]):
            flash("Email admin tidak valid", "error")
        elif recipe["admin"] and not re.match(r"^[A-Za-z0-9_.-]{3,32}$", admin["user"]):
            flash("Username admin 3-32 karakter: huruf, angka, _ . -", "error")
        elif recipe["admin"] and len(admin["password"]) < recipe.get("min_pw", 10):
            flash(f"Password admin {recipe['name']} minimal {recipe.get('min_pw', 10)} karakter", "error")
        else:
            if not site_exists(domain):
                publish = request.form.get("publish") == "1" and in_cf_zone(domain)
                err = (publish and publish_conflict(domain)) or create_site(domain)
                if err:
                    flash(err, "error")
                    return redirect(url_for("apps_page"))
                if publish and (msg := publish_site(domain)):
                    flash(msg[1], msg[0])
            cfg.setdefault("apps", {})[domain] = {
                "app": app_id, "status": "running", "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                "admin": {k: admin[k] for k in ("user", "email", "password")} if recipe["admin"] else None}
            save_config()
            appmgr.install(WWW, domain, app_id, site_url(domain), admin, site_db(domain, "apps"), app_done(domain))
            flash(f"Instalasi {recipe['name']} di {domain} dimulai…", "ok")
            return redirect(url_for("app_detail", domain=domain))
        return redirect(url_for("apps_page"))
    installed = [{"domain": d, **a, "running": appmgr.is_running(d)} for d, a in sorted(cfg.get("apps", {}).items())]
    return render_template("apps.html", catalog=appmgr.CATALOG, installed=installed, free=free_sites(), zones=cf_zones(),
                           cf_on=cf_enabled(), cf_auto=cfg.get("cf_auto", True))


@app.route("/apps/<domain>", methods=["GET", "POST"])
@login_required
def app_detail(domain):
    a = cfg.get("apps", {}).get(domain)
    if not a:
        abort(404)
    if request.method == "POST" and request.form.get("action") == "forget":
        cfg["apps"].pop(domain)
        app_cron_file(domain).unlink(missing_ok=True)
        save_config()
        flash(f"Aplikasi dilepas dari {domain}. File & database tetap ada (hapus lewat menu Website).", "ok")
        return redirect(url_for("apps_page"))
    running = appmgr.is_running(domain)
    if a.get("status") == "running" and not running:
        a["status"] = "failed"  # panel di-restart saat instalasi berjalan
    return render_template("app_detail.html", domain=domain, a=a, recipe=appmgr.CATALOG[a["app"]],
                           running=running, log=appmgr.read_log(domain), url=site_url(domain))


# ---------- node.js ----------
def set_vhost(domain, text):
    conf = NGINX_AVAIL / f"{SITE_PREFIX}{domain}.conf"
    old = conf.read_text()
    conf.write_text(text)
    rc, out = run(["nginx", "-t"])
    if rc != 0:
        conf.write_text(old)
        raise RuntimeError(out)
    run(["systemctl", "reload", "nginx"])


def php_vhost(domain):
    """Vhost PHP biasa (dipakai lagi saat Node.js dilepas): root repo Git bila ada, selain itu public_html."""
    g = cfg.get("git", {}).get(domain)
    root = WWW / domain / "public_html"
    if g:
        rd = gitdeploy.repo_dir(WWW, domain)
        root = rd / g["subdir"] if g.get("subdir") else rd
    return VHOST.format(domain=domain, root=root)


def node_apply(domain, n):
    """Tulis service systemd + vhost proxy nginx sesuai pengaturan."""
    nodeapp.write_service(WWW, domain, n)
    nodeapp.ensure_nginx_map()
    set_vhost(domain, nodeapp.vhost(domain, n))


def node_form(n, domain):
    """Baca form pengaturan Node.js ke n. Kembalikan pesan error atau None."""
    version = request.form.get("version", "system")
    rel = request.form.get("dir", "").strip().strip("/")
    start = request.form.get("start", "").strip()
    env, err = nodeapp.parse_env(request.form.get("env", ""))
    if version not in dict(nodeapp.runtimes()):
        return "Versi Node.js belum terpasang"
    if not rel or not gitdeploy.SUBDIR_RE.match(rel) or ".." in rel.split("/"):
        return "Folder aplikasi tidak valid (contoh: nodeapp atau repo)"
    if "\n" in start or "\r" in start or len(start) > 300:
        return "Perintah start harus satu baris"
    if err:
        return err
    n.update(version=version, dir=str(WWW / domain / rel), start=start, env=env,
             auto_install=request.form.get("auto_install") == "1")
    return None


@app.route("/node", methods=["GET", "POST"])
@login_required
def node_page():
    nodes = cfg.setdefault("node", {})
    if request.method == "POST":
        domain = form_domain()
        n = {"port": None, "db": None}
        err = node_form(n, domain) if DOMAIN_RE.match(domain) else "Nama domain tidak valid"
        if not err and (domain in nodes or domain in cfg.get("apps", {})):
            err = f"{domain} sudah dipakai aplikasi Node.js / aplikasi lain"
        if not err and not site_exists(domain):
            publish = request.form.get("publish") == "1" and in_cf_zone(domain)
            err = (publish and publish_conflict(domain)) or create_site(domain)
            if not err and publish and (msg := publish_site(domain)):
                flash(msg[1], msg[0])
        if err:
            flash(err, "error")
            return redirect(url_for("node_page"))
        app_dir = Path(n["dir"])
        if request.form.get("starter") == "1" and nodeapp.starter(app_dir):
            flash("Contoh aplikasi (server.js) dibuat. Ganti dengan aplikasi Anda lewat File Manager atau Git.", "ok")
        app_dir.mkdir(parents=True, exist_ok=True)
        fix_owner(WWW / domain)
        n["start"] = n["start"] or nodeapp.guess_start(app_dir)
        n["port"] = nodeapp.free_port({x["port"] for x in nodes.values()})
        nodes[domain] = n
        save_config()
        try:
            if request.form.get("make_db") in ("mysql", "both"):
                site_db(domain, "node")()
            if request.form.get("make_db") in ("pgsql", "both"):
                site_pg(domain, "node")()
        except (pymysql.MySQLError, pgsql.PGError) as e:
            flash(f"Database gagal dibuat: {e}", "error")
        try:
            node_apply(domain, n)
        except RuntimeError as e:
            flash(f"Konfigurasi nginx gagal: {e}", "error")
        if (app_dir / "package.json").exists():
            nodeapp.task(WWW, domain, n, ["install", "build"])
            flash(f"Aplikasi Node.js {domain} dibuat. npm install & start berjalan…", "ok")
        else:
            flash(f"Aplikasi Node.js {domain} dibuat. Unggah aplikasi (package.json) ke {app_dir}, lalu klik npm install.", "ok")
        return redirect(url_for("node_detail", domain=domain))
    rows = [{"domain": d, **n, "st": nodeapp.status(d), "running": nodeapp.is_running(d)} for d, n in sorted(nodes.items())]
    runtimes = nodeapp.runtimes()
    free = [x["domain"] for x in list_sites() if x["domain"] not in nodes and x["domain"] not in cfg.get("apps", {})]
    return render_template("node.html", pg_on=pgsql.available(), rows=rows, runtimes=runtimes, majors=nodeapp.MAJORS,
                           installed={k for k, _ in runtimes}, used={n["version"] for n in nodes.values()},
                           busy=nodeapp.runtime_busy(), rlog=nodeapp.runtime_log(), free=free, zones=cf_zones(),
                           cf_on=cf_enabled(), cf_auto=cfg.get("cf_auto", True))


@app.post("/node/runtime")
@login_required
def node_runtime():
    major = request.form.get("major", "")
    if request.form.get("action") == "remove":
        if any(n["version"] == major for n in cfg.get("node", {}).values()):
            flash(f"Node.js {major} masih dipakai aplikasi", "error")
        else:
            nodeapp.remove_runtime(major)
            flash(f"Node.js {major} dihapus", "ok")
    elif major.isdigit() and nodeapp.install_runtime(major):
        flash(f"Mengunduh Node.js {major}…", "ok")
    else:
        flash("Unduhan lain masih berjalan", "error")
    return redirect(url_for("node_page") + "#versi")


@app.route("/node/<domain>", methods=["GET", "POST"])
@login_required
def node_detail(domain):
    n = cfg.get("node", {}).get(domain)
    if not n:
        abort(404)
    if request.method == "POST":
        act = request.form.get("action")
        if act == "save":
            new = dict(n)
            err = node_form(new, domain) or (None if new["start"] else "Perintah start wajib diisi")
            if err:
                flash(err, "error")
            else:
                n.update(new)
                save_config()
                Path(n["dir"]).mkdir(parents=True, exist_ok=True)
                fix_owner(Path(n["dir"]))
                try:
                    node_apply(domain, n)
                    on = nodeapp.status(domain)["enabled"]
                    if on:
                        nodeapp.control(domain, "restart")
                    flash("Pengaturan disimpan" + (" dan aplikasi di-restart" if on else ""), "ok")
                except RuntimeError as e:
                    flash(f"Konfigurasi nginx gagal: {e}", "error")
        elif act in ("start", "stop", "restart"):
            rc, out = nodeapp.control(domain, act)
            flash(f"{act}: " + ("berhasil" if rc == 0 else out), "ok" if rc == 0 else "error")
        elif act in ("install", "build", "script"):
            steps = {"install": ["install"], "build": ["install", "build"]}.get(act)
            if act == "script":
                name = request.form.get("script", "")
                steps = [f"run:{name}"] if name in nodeapp.scripts(Path(n["dir"])) else None
            if not steps:
                flash("Skrip tidak ditemukan di package.json", "error")
            elif nodeapp.task(WWW, domain, n, steps, restart=act != "script"):
                flash("Dijalankan…", "ok")
            else:
                flash("Proses lain masih berjalan", "error")
        elif act in ("db", "pg") and not n.get(act):
            try:
                (site_db if act == "db" else site_pg)(domain, "node")()
            except (pymysql.MySQLError, pgsql.PGError) as e:
                flash(f"Database gagal dibuat: {e}", "error")
                return redirect(url_for("node_detail", domain=domain))
            nodeapp.write_service(WWW, domain, n)
            if nodeapp.status(domain)["enabled"]:
                nodeapp.control(domain, "restart")
            flash("Database dibuat dan dimasukkan ke environment aplikasi", "ok")
        elif act == "remove":
            cfg["node"].pop(domain)
            save_config()
            try:
                set_vhost(domain, php_vhost(domain))
            except RuntimeError as e:
                flash(f"Konfigurasi nginx gagal: {e}", "error")
            time.sleep(1)  # beri waktu worker nginx lama selesai sebelum aplikasi dimatikan
            nodeapp.remove(domain)
            flash(f"Node.js dilepas dari {domain}. Website kembali memakai PHP; folder aplikasi & database tetap ada.", "ok")
            return redirect(url_for("node_page"))
        return redirect(url_for("node_detail", domain=domain))
    app_dir = Path(n["dir"])
    site = WWW / domain
    return render_template("node_detail.html", domain=domain, n=n, st=nodeapp.status(domain),
                           runtimes=nodeapp.runtimes(), scripts=nodeapp.scripts(app_dir),
                           has_pkg=(app_dir / "package.json").exists(), running=nodeapp.is_running(domain),
                           log=nodeapp.read_log(domain), journal=nodeapp.journal(domain),
                           env_text=nodeapp.env_text(n.get("env", {})), db_env=nodeapp.db_env(n),
                           pg_on=pgsql.available(), pg_url=pgsql.url(n["pg"]) if n.get("pg") else None,
                           url=site_url(domain), git=cfg.get("git", {}).get(domain),
                           rel_dir=str(app_dir.relative_to(site)) if site in app_dir.parents else n["dir"])


# ---------- backup & restore ----------
_bk_running = set()
RESTORE_LOG = BASE / "deploy-logs" / "restore.log"
_restore_lock = threading.Lock()


def backup_async(domains):
    def job():
        with open(BASE / "deploy-logs" / "backup.log", "a") as log:
            for d in domains:
                log.write(f"\n== {time.strftime('%Y-%m-%d %H:%M:%S')} backup {d}\n")
                try:
                    bk.create(cfg, d, log)
                    s = cfg.get("backup_schedule", {}).get(d)
                    if s:
                        bk.prune(d, int(s.get("keep", 7)))
                except Exception as e:
                    log.write(f"❌ gagal: {e}\n")
                _bk_running.discard(d)
    _bk_running.update(domains)
    threading.Thread(target=job, daemon=True).start()


def backup_cron(enable):
    f = Path("/etc/cron.d/localpanel-backup")
    if enable:
        f.write_text("# Dikelola oleh LocalPanel: backup otomatis website\n"
                     f"30 2 * * * root /usr/bin/python3 {BASE / 'backup.py'} auto\n")
        f.chmod(0o644)
    else:
        f.unlink(missing_ok=True)


def backup_file(domain, name):
    if not DOMAIN_RE.match(domain) or not re.match(r"^[\w.-]+\.tar\.gz$", name):
        abort(404)
    f = bk.backup_dir(domain) / name
    if not f.is_file():
        abort(404)
    return f


@app.route("/backups", methods=["GET", "POST"])
@login_required
def backups():
    if request.method == "POST":
        act = request.form.get("action")
        domain = request.form.get("domain", "")
        if act == "backup" and site_exists(domain):
            backup_async([domain])
            flash(f"Backup {domain} dimulai…", "ok")
        elif act == "backup_all":
            backup_async([x["domain"] for x in list_sites()])
            flash("Backup semua website dimulai…", "ok")
        elif act == "schedule" and site_exists(domain):
            freq = request.form.get("freq")
            keep = max(1, min(60, int(request.form.get("keep") or 7)))
            sch = cfg.setdefault("backup_schedule", {})
            if freq in ("daily", "weekly"):
                sch[domain] = {"freq": freq, "keep": keep}
            else:
                sch.pop(domain, None)
            backup_cron(bool(sch))
            save_config()
            flash(f"Jadwal backup {domain} disimpan", "ok")
        elif act == "delete":
            backup_file(domain, request.form.get("name", "")).unlink()
            flash("Backup dihapus", "ok")
        return redirect(url_for("backups"))
    sch = cfg.get("backup_schedule", {})
    rows = []
    for x in list_sites():
        files = bk.list_backups(x["domain"])
        rows.append({"domain": x["domain"], "files": files, "schedule": sch.get(x["domain"]),
                     "running": x["domain"] in _bk_running, "dbs": [d["name"] for d in bk.site_dbs(cfg, x["domain"])]})
    orphans = [b for b in bk.list_backups() if not site_exists(b["domain"])]
    return render_template("backups.html", rows=rows, orphans=orphans, any_running=bool(_bk_running),
                           restoring=_restore_lock.locked(), cf_on=cf_enabled(),
                           restore_log=RESTORE_LOG.read_text(errors="replace")[-20000:] if RESTORE_LOG.exists() else "")


@app.route("/backups/<domain>/<name>")
@login_required
def backup_download(domain, name):
    return send_file(backup_file(domain, name), as_attachment=True)


def restore_job(path, target, overwrite, publish, cleanup):
    """Pulihkan website dari berkas backup (dijalankan di background)."""
    log = open(RESTORE_LOG, "w")
    say = lambda m: (log.write(m + "\n"), log.flush())
    work = bk.BACKUPS / f".restore-{secrets.token_hex(4)}"
    try:
        say(f"Restore {path.name} — {time.strftime('%Y-%m-%d %H:%M:%S')}")
        m = bk.read_manifest(path)
        old = m["domain"]
        new = target or old
        say(f"Domain asal: {old}  →  tujuan: {new}")
        if site_exists(new):
            if not overwrite:
                raise RuntimeError(f"Website {new} sudah ada. Centang 'Timpa' untuk menggantinya.")
            say(f"menimpa website {new} yang ada (file & database lama dihapus)")
            keep_schedule = cfg.get("backup_schedule", {}).get(new)
            remove_site_data(new, True)
            if keep_schedule:
                cfg["backup_schedule"][new] = keep_schedule
                save_config()
            conf = NGINX_AVAIL / f"{SITE_PREFIX}{new}.conf"
            (NGINX_ENABLED / conf.name).unlink(missing_ok=True)
            conf.unlink(missing_ok=True)
            shutil.rmtree(WWW / new, ignore_errors=True)
        elif (WWW / new).exists():
            raise RuntimeError(f"Folder {WWW / new} sudah ada tapi bukan website panel. Hapus dulu.")
        say("ekstrak backup…")
        work.mkdir(parents=True)
        bk.extract(path, work)
        shutil.move(str(work / old), str(WWW / new))

        # database: nama tetap bila domain sama, selain itu diturunkan dari domain baru
        repl, dbmap = [], {}
        for i, d in enumerate(m.get("dbs", [])):
            nd = dict(d)
            if new != old:
                base = bk.slug(new)
                nd["name"] = (d["name"].replace(bk.slug(old), base, 1) if d["name"].startswith(bk.slug(old))
                              else f"{base}_{i + 1}")[:64]
                nd["user"] = (d["user"].replace(bk.slug(old)[:32], base[:32], 1)
                              if d["user"].startswith(bk.slug(old)[:32]) else f"{base[:28]}_{i + 1}")[:32]
                repl += [(d["name"], nd["name"]), (d["user"], nd["user"])]
            dbmap[d["name"]] = nd
            say(f"database {nd['name']} (user {nd['user']})")
            ensure_db(nd)
            gitdeploy.import_sql(nd["name"], work / "db" / f"{d['name']}.sql")
        pgmap = {}
        for i, d in enumerate(m.get("pg_dbs", [])):
            nd = dict(d)
            if new != old:
                base = bk.slug(new)
                nd["name"] = (d["name"].replace(bk.slug(old), base, 1) if d["name"].startswith(bk.slug(old))
                              else f"{base}_pg{i + 1}")[:63]
                nd["user"] = (d["user"].replace(bk.slug(old), base, 1) if d["user"].startswith(bk.slug(old))
                              else f"{base}_pg{i + 1}")[:63]
                repl += [(d["name"], nd["name"]), (d["user"], nd["user"])]
            pgmap[d["name"]] = nd
            say(f"database PostgreSQL {nd['name']} (user {nd['user']})")
            pgsql.ensure_db(nd)
            pgsql.import_sql(nd, work / "db" / f"pg-{d['name']}.sql")
        if new != old:
            repl.append((old, new))
            for f in bk.rewrite_configs(WWW / new, repl):
                say(f"konfigurasi disesuaikan: {f.relative_to(WWW)}")

        # vhost nginx
        root = m["nginx_root"].replace(f"/var/www/{old}/", f"/var/www/{new}/").replace(f"/var/www/{old}", f"/var/www/{new}")
        err = create_site(new, root=root, index=False)
        if err:
            raise RuntimeError(err)
        fb = re.search(r"try_files \$uri \$uri/ (\S+?)(\?\$query_string)?;", m.get("nginx_conf", ""))
        if fb and fb.group(1) != "/index.php":
            set_site_fallback(new, fb.group(1))

        # pengaturan panel
        fix = lambda x: json.loads(json.dumps(x).replace(f"/var/www/{old}", f"/var/www/{new}")) if x else x
        if m.get("git"):
            g = fix(m["git"])
            if g.get("db"):
                g["db"] = dbmap.get(g["db"]["name"], g["db"])
            cfg.setdefault("git", {})[new] = g
        if m.get("app"):
            a = fix(m["app"])
            if a.get("db"):
                a["db"] = dbmap.get(a["db"]["name"], a["db"])
            if a.get("admin_url"):
                a["admin_url"] = a["admin_url"].replace(old, new)
            cfg.setdefault("apps", {})[new] = a
        save_config()
        if m.get("app_cron"):
            app_cron_file(new).write_text(m["app_cron"].replace(f"/var/www/{old}", f"/var/www/{new}"))
        run(["chown", "-R", "www-data:www-data", str(WWW / new)])
        if m.get("node"):
            n = fix(m["node"])
            if n.get("db"):
                n["db"] = dbmap.get(n["db"]["name"], n["db"])
            if n.get("pg"):
                n["pg"] = pgmap.get(n["pg"]["name"], n["pg"])
            if not nodeapp.node_version(n["version"]):
                say(f"Node.js versi {n['version']} belum terpasang di server ini, memakai versi sistem")
                n["version"] = "system"
            used = {x["port"] for d, x in cfg.get("node", {}).items() if d != new}
            if n["port"] in used:
                n["port"] = nodeapp.free_port(used)
            cfg.setdefault("node", {})[new] = n
            save_config()
            node_apply(new, n)
            say(f"Node.js: npm install & start di port {n['port']} (lihat menu Node.js)")
            nodeapp.task(WWW, new, n, ["install", "build"])

        # penyesuaian per aplikasi
        app_id = (m.get("app") or {}).get("app")
        site = WWW / new
        if new != old and app_id == "wordpress":
            wp = appmgr.ensure_wp_cli() + [f"--path={(cfg['apps'][new]['root'])}"]
            say("wp search-replace " + run(wp + ["search-replace", f"//{old}", f"//{new}", "--all-tables",
                                                 "--skip-plugins", "--skip-themes"])[1].splitlines()[-1])
        if new != old and app_id == "moodle":
            say("moodle replace " + run(["sudo", "-u", "www-data", "php", str(site / "moodle/admin/tool/replace/cli/replace.php"),
                                         f"--search=//{old}", f"--replace=//{new}", "--non-interactive"])[1][-300:])
        for artisan in (site / "repo" / "artisan", site / "app" / "artisan"):
            if artisan.exists():
                say("laravel: " + run(["sudo", "-u", "www-data", "php", str(artisan), "optimize:clear"])[1][-200:])
                run(["sudo", "-u", "www-data", "php", str(artisan), "optimize"])
                if m.get("queue") and artisan.parent.name == "repo":
                    laravel_set(new, "queue", True)
                if m.get("schedule") and artisan.parent.name == "repo":
                    laravel_set(new, "schedule", True)
        if publish and (msg := publish_site(new)):
            say(msg[1])
        say(f"\n✅ Restore selesai: http://{new}")
    except Exception as e:
        say(f"\n❌ Restore gagal: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if cleanup:
            path.unlink(missing_ok=True)
        log.close()
        _restore_lock.release()


@app.post("/backups/restore")
@login_required
def backup_restore():
    if not _restore_lock.acquire(blocking=False):
        flash("Restore lain masih berjalan", "error")
        return redirect(url_for("backups"))
    try:
        target = request.form.get("target", "").strip().lower()
        if target and not DOMAIN_RE.match(target):
            raise ValueError("Domain tujuan tidak valid")
        up = request.files.get("file")
        cleanup = False
        if up and up.filename:
            if not up.filename.endswith(".tar.gz"):
                raise ValueError("Berkas harus .tar.gz hasil backup LocalPanel")
            inbox = bk.BACKUPS / ".upload"
            inbox.mkdir(parents=True, exist_ok=True)
            path = inbox / f"{secrets.token_hex(4)}-{secure_filename(up.filename)}"
            up.save(path)
            cleanup = True
        else:
            path = backup_file(request.form.get("domain", ""), request.form.get("name", ""))
        try:
            m = bk.read_manifest(path)
        except Exception as e:
            if cleanup:
                path.unlink(missing_ok=True)
            raise ValueError(f"Bukan berkas backup LocalPanel yang valid: {e}")
        if cleanup and not target:
            target = m["domain"]
    except ValueError as e:
        _restore_lock.release()
        flash(str(e), "error")
        return redirect(url_for("backups"))
    except BaseException:
        _restore_lock.release()
        raise
    threading.Thread(target=restore_job, args=(path, target, request.form.get("overwrite") == "1",
                                                request.form.get("publish") == "1", cleanup), daemon=True).start()
    flash(f"Restore {m['domain']} → {target or m['domain']} dimulai…", "ok")
    return redirect(url_for("backups") + "#restore")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=cfg.get("port", 2083))
