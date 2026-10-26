"""Aplikasi Node.js untuk LocalPanel (mirip "Setup Node.js App" di cPanel).

Setiap website Node.js:
  - berjalan sebagai service systemd  localpanel-node-<slug>.service  (user www-data, restart otomatis)
  - mendengarkan 127.0.0.1:<port>; nginx meneruskan domain ke port itu (WebSocket didukung)
  - memakai versi Node pilihan: "system" (/usr/bin/node) atau versi resmi di node/<major>/
Pengaturan disimpan di config.json bagian "node":
  {domain: {"dir", "start", "port", "version", "env": {KEY: VALUE}, "db", "pg", "auto_install"}}
"""
import json, os, re, shutil, subprocess, threading, time, urllib.request
from pathlib import Path

import psutil

BASE = Path(__file__).resolve().parent
LOGS = BASE / "deploy-logs"
RUNTIMES = BASE / "node"  # unduhan Node.js resmi: node/<major>/bin/node
ETC = Path("/etc/localpanel/node")
UNITS = Path("/etc/systemd/system")
NGINX_MAP = Path("/etc/nginx/conf.d/localpanel-node.conf")
PORTS = range(3001, 4000)
MAJORS = (18, 20, 22, 24)  # versi LTS yang ditawarkan
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_ENV = {"PORT"}  # diatur panel; nginx meneruskan ke port ini

_locks = {}
_runtime_lock = threading.Lock()

VHOST = """# Dikelola oleh LocalPanel: aplikasi Node.js
server {{
    listen 80;
    listen [::]:80;
    server_name {domain} www.{domain};
    root {dir};
    client_max_body_size 520M;
    access_log /var/log/nginx/{domain}.access.log;
    error_log /var/log/nginx/{domain}.error.log;

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $lp_forwarded_proto;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $lp_connection_upgrade;
        proxy_read_timeout 300;
        proxy_intercept_errors off;
    }}
    error_page 502 504 @lp_node_down;
    location @lp_node_down {{
        default_type text/html;
        return 502 "<h1>{domain}</h1><p>Aplikasi Node.js sedang tidak berjalan. Cek status &amp; log di LocalPanel.</p>";
    }}
}}
"""

UNIT = """# Dikelola oleh LocalPanel
[Unit]
Description=Node.js {domain} (LocalPanel)
After=network.target mariadb.service

[Service]
User=www-data
Group=www-data
WorkingDirectory={dir}
Environment=NODE_ENV=production
Environment=HOST=127.0.0.1
Environment=HOME={site}
Environment=npm_config_cache={site}/.npm
EnvironmentFile=-{envfile}
Environment=PORT={port}
ExecStart=/bin/sh {script}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

STARTER_PKG = {"name": "app", "version": "1.0.0", "private": True, "main": "server.js",
               "scripts": {"start": "node server.js"}}
STARTER_JS = """// Contoh aplikasi Node.js dari LocalPanel. Ganti dengan aplikasi Anda.
const http = require('http');
const port = process.env.PORT || 3000;
http.createServer((req, res) => {
  res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
  res.end(`<h1>Node.js aktif</h1><p>Node ${process.version} · ${req.headers.host}</p>`);
}).listen(port, process.env.HOST || '127.0.0.1', () => console.log(`listening on ${port}`));
"""


def slug(domain):
    return re.sub(r"[^a-z0-9]", "_", domain)


def unit_name(domain):
    return f"localpanel-node-{slug(domain)}.service"


def env_file(domain):
    return ETC / f"{slug(domain)}.env"


def script_file(domain):
    return ETC / f"{slug(domain)}.sh"


def log_path(domain):
    return LOGS / f"node-{domain}.log"


def read_log(domain):
    p = log_path(domain)
    return p.read_text(errors="replace")[-20000:] if p.exists() else ""


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


# ---------- versi Node.js ----------
def runtime_bin(version):
    return Path("/usr/bin") if version == "system" else RUNTIMES / str(version) / "bin"


def node_version(version):
    node = runtime_bin(version) / "node"
    if not node.exists():
        return None
    return run([str(node), "-v"])[1] or None


def runtimes():
    """Versi Node yang terpasang: [(kunci, label)], "system" lebih dulu."""
    out = []
    if (v := node_version("system")):
        out.append(("system", f"{v} (bawaan sistem)"))
    if RUNTIMES.is_dir():
        for d in sorted(RUNTIMES.iterdir(), key=lambda d: int(d.name) if d.name.isdigit() else 0):
            if d.name.isdigit() and (v := node_version(d.name)):
                out.append((d.name, v))
    return out


def runtime_log():
    p = LOGS / "node-runtime.log"
    return p.read_text(errors="replace")[-8000:] if p.exists() else ""


def runtime_busy():
    return _runtime_lock.locked()


def install_runtime(major):
    """Unduh rilis resmi terbaru dari major tsb ke node/<major>/ (background). False bila sedang berjalan."""
    if int(major) not in MAJORS or not _runtime_lock.acquire(blocking=False):
        return False

    def job():
        LOGS.mkdir(exist_ok=True)
        with open(LOGS / "node-runtime.log", "w") as log:
            tmp = RUNTIMES / f".tmp-{major}"
            try:
                arch = {"x86_64": "x64", "aarch64": "arm64"}.get(os.uname().machine)
                if not arch:
                    raise RuntimeError(f"Arsitektur {os.uname().machine} tidak didukung")
                req = urllib.request.Request("https://nodejs.org/dist/index.json", headers={"User-Agent": "LocalPanel"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    rel = next(x for x in json.load(r) if x["version"].startswith(f"v{major}."))
                ver = rel["version"]
                url = f"https://nodejs.org/dist/{ver}/node-{ver}-linux-{arch}.tar.xz"
                log.write(f"Unduh Node.js {ver}\n{url}\n")
                log.flush()
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir(parents=True)
                p = subprocess.run(f"curl -fsSL --retry 3 '{url}' | tar xJ -C '{tmp}' --strip-components=1",
                                   shell=True, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
                if p.returncode != 0 or not (tmp / "bin" / "node").exists():
                    raise RuntimeError(f"unduh/ekstrak gagal (exit {p.returncode})")
                dest = RUNTIMES / str(major)
                shutil.rmtree(dest, ignore_errors=True)
                tmp.rename(dest)
                log.write(f"\n✅ Node.js {ver} terpasang di {dest}\n")
            except Exception as e:
                log.write(f"\n❌ Gagal: {e}\n")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        _runtime_lock.release()

    threading.Thread(target=job, daemon=True).start()
    return True


def remove_runtime(major):
    if str(major).isdigit():
        shutil.rmtree(RUNTIMES / str(major), ignore_errors=True)


# ---------- port & konfigurasi ----------
def free_port(used):
    busy = {c.laddr.port for c in psutil.net_connections("inet") if c.status == psutil.CONN_LISTEN}
    for p in PORTS:
        if p not in used and p not in busy:
            return p
    raise RuntimeError("Tidak ada port kosong di 3001-3999")


def parse_env(text):
    """Teks KEY=VALUE per baris -> dict. Kembalikan (dict, error)."""
    out = {}
    for n, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.partition("=")
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        if not sep or not ENV_KEY_RE.match(k):
            return None, f"Baris {n} tidak valid: {line[:60]}"
        if k in RESERVED_ENV:
            return None, f"{k} diatur otomatis oleh panel"
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out, None


def env_text(env):
    return "\n".join(f"{k}={v}" for k, v in env.items())


def db_env(n):
    """Variabel koneksi database aplikasi. Bila ada PostgreSQL, DATABASE_URL menunjuk ke PostgreSQL."""
    out = {}
    db, pg = n.get("db"), n.get("pg")
    if db:
        out.update(DB_HOST="127.0.0.1", DB_PORT="3306", DB_NAME=db["name"], DB_USER=db["user"],
                   DB_PASSWORD=db["password"],
                   DATABASE_URL=f"mysql://{db['user']}:{db['password']}@127.0.0.1:3306/{db['name']}")
    if pg:
        out.update(PGHOST="127.0.0.1", PGPORT="5432", PGDATABASE=pg["name"], PGUSER=pg["user"],
                   PGPASSWORD=pg["password"],
                   DATABASE_URL=f"postgresql://{pg['user']}:{pg['password']}@127.0.0.1:5432/{pg['name']}")
    return out


def write_service(www, domain, n):
    """Tulis skrip start, berkas env, dan unit systemd sesuai pengaturan n."""
    ETC.mkdir(parents=True, exist_ok=True)
    ETC.chmod(0o755)
    env = {**db_env(n), **n.get("env", {})}
    ef = env_file(domain)
    # systemd membaca berkas ini sebagai root sebelum turun ke www-data
    ef.write_text("# Dikelola oleh LocalPanel\n" + "".join(
        f'{k}="{v.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"\n' for k, v in env.items()))
    ef.chmod(0o600)
    sf = script_file(domain)
    sf.write_text("#!/bin/sh\n# Dikelola oleh LocalPanel\n"
                  f'export PATH="{runtime_bin(n["version"])}:/usr/local/bin:/usr/bin:/bin"\n'
                  f'cd "{n["dir"]}" || exit 1\n'
                  f"exec {n['start']}\n")
    sf.chmod(0o755)
    (UNITS / unit_name(domain)).write_text(UNIT.format(domain=domain, dir=n["dir"], site=www / domain,
                                                       envfile=ef, port=n["port"], script=sf))
    run(["systemctl", "daemon-reload"])


def ensure_nginx_map():
    """Variabel nginx untuk proxy WebSocket & https (sekali per server)."""
    if not NGINX_MAP.exists():
        shutil.copy(BASE / "deploy" / "nginx-localpanel-node.conf", NGINX_MAP)


def vhost(domain, n):
    return VHOST.format(domain=domain, dir=n["dir"], port=n["port"])


def starter(app_dir):
    """Isi folder kosong dengan contoh aplikasi tanpa dependensi."""
    app_dir.mkdir(parents=True, exist_ok=True)
    if any(app_dir.iterdir()):
        return False
    (app_dir / "package.json").write_text(json.dumps(STARTER_PKG, indent=2) + "\n")
    (app_dir / "server.js").write_text(STARTER_JS)
    return True


def guess_start(app_dir):
    try:
        pkg = json.loads((app_dir / "package.json").read_text())
    except (OSError, ValueError):
        pkg = {}
    if (pkg.get("scripts") or {}).get("start"):
        return "npm start"
    for f in (pkg.get("main"), "server.js", "app.js", "index.js", "main.js"):
        if f and (app_dir / f).is_file():
            return f"node {f}"
    return "npm start"


def scripts(app_dir):
    try:
        return list((json.loads((app_dir / "package.json").read_text()).get("scripts") or {}).keys())
    except (OSError, ValueError, AttributeError):
        return []


# ---------- service ----------
def status(domain):
    unit = unit_name(domain)
    _, out = run(["systemctl", "show", unit, "-p", "ActiveState,SubState,MainPID,MemoryCurrent,"
                  "ActiveEnterTimestamp,NRestarts,UnitFileState"])
    s = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    mem = s.get("MemoryCurrent", "")
    return {"state": s.get("ActiveState", "unknown"), "sub": s.get("SubState", ""),
            "pid": s.get("MainPID", "0"), "since": s.get("ActiveEnterTimestamp", ""),
            "restarts": s.get("NRestarts", "0"), "enabled": s.get("UnitFileState") == "enabled",
            "mem": int(mem) if mem.isdigit() else None}


def control(domain, action):
    unit = unit_name(domain)
    if action == "start":
        return run(["systemctl", "enable", "--now", unit])
    if action == "stop":
        return run(["systemctl", "disable", "--now", unit])
    if action == "restart":
        run(["systemctl", "enable", unit])
        return run(["systemctl", "restart", unit])
    raise ValueError(action)


def journal(domain, lines=100):
    return run(["journalctl", "-u", unit_name(domain), "-n", str(lines), "--no-pager", "-o", "short-iso"])[1]


def remove(domain):
    """Hentikan & hapus service (folder aplikasi tidak disentuh)."""
    unit = unit_name(domain)
    run(["systemctl", "disable", "--now", unit])
    for f in (UNITS / unit, env_file(domain), script_file(domain)):
        f.unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "reset-failed", unit])


# ---------- npm ----------
def is_running(domain):
    lock = _locks.get(domain)
    return bool(lock and lock.locked())


def task(www, domain, n, steps, restart=True, on_done=None):
    """Jalankan langkah npm di background sebagai www-data.
    steps: daftar "install", "build" (bila ada skrip build) atau "run:<skrip>"."""
    lock = _locks.setdefault(domain, threading.Lock())
    if not lock.acquire(blocking=False):
        return False

    def job():
        LOGS.mkdir(exist_ok=True)
        ok = True
        app_dir = Path(n["dir"])
        site = www / domain
        env = {"PATH": f"{runtime_bin(n['version'])}:/usr/local/bin:/usr/bin:/bin", "HOME": str(site),
               "npm_config_cache": str(site / ".npm"), "NPM_CONFIG_UPDATE_NOTIFIER": "false",
               "NPM_CONFIG_FUND": "false", "NPM_CONFIG_AUDIT": "false",
               **db_env(n), **n.get("env", {})}
        with open(log_path(domain), "w") as log:
            log.write(f"{domain} — {time.strftime('%Y-%m-%d %H:%M:%S')} — Node {node_version(n['version'])}\n")

            def step(title, cmd, timeout=1800, extra=None):
                log.write(f"\n$ {title}\n")
                log.flush()
                p = subprocess.run(cmd, cwd=app_dir, env=dict(env, **(extra or {})), user="www-data",
                                   group="www-data", extra_groups=[], stdout=log, stderr=subprocess.STDOUT,
                                   timeout=timeout)
                if p.returncode != 0:
                    raise RuntimeError(f"{title} gagal (exit {p.returncode})")

            try:
                if not (app_dir / "package.json").exists():
                    raise RuntimeError(f"package.json tidak ada di {app_dir}")
                subprocess.run(["chown", "-R", "www-data:www-data", str(site)])
                for s in steps:
                    if s == "install":
                        # devDependencies tetap dipasang: biasanya dibutuhkan untuk build
                        dev = {"NODE_ENV": "development"}
                        try:
                            if not (app_dir / "package-lock.json").exists():
                                raise RuntimeError
                            step("npm ci", ["npm", "ci"], extra=dev)
                        except RuntimeError:
                            step("npm install", ["npm", "install"], extra=dev)
                    elif s == "build":
                        if "build" in scripts(app_dir):
                            step("npm run build", ["npm", "run", "build"])
                    elif s.startswith("run:"):
                        step(f"npm run {s[4:]}", ["npm", "run", s[4:]])
                if restart:
                    log.write("\n$ restart aplikasi\n")
                    rc, out = control(domain, "restart")
                    log.write(out + "\n" if out else "")
                    time.sleep(2)
                    st = status(domain)
                    if st["state"] != "active":
                        raise RuntimeError(f"aplikasi tidak berjalan ({st['state']}/{st['sub']}), lihat log aplikasi")
                log.write("\n✅ Selesai\n")
            except Exception as e:
                ok = False
                log.write(f"\n❌ {e}\n")
        if on_done:
            on_done(ok)
        lock.release()

    threading.Thread(target=job, daemon=True).start()
    return True
