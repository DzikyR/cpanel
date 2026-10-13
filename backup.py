#!/usr/bin/env python3
"""Backup & restore per website untuk LocalPanel.

Isi satu berkas backup (<domain>-<waktu>.tar.gz):
  manifest.json   pengaturan website (git, aplikasi, database, nginx, queue/cron)
  db/<nama>.sql   dump tiap database website
  <domain>/...    seluruh folder /var/www/<domain> (tanpa node_modules)

Dipakai juga dari cron:  python3 backup.py auto   (backup terjadwal + hapus backup lama)
"""
import json, re, shutil, subprocess, sys, tarfile, time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
BACKUPS = Path("/var/backups/localpanel")
WWW = Path("/var/www")
NGINX_AVAIL = Path("/etc/nginx/sites-available")
SITE_PREFIX = "lp-"
FORMAT = 1
CONFIG_FILES = {".env", "wp-config.php", "config.inc.php", "config.php", "configuration.php", "database.php",
                "settings.php"}
SKIP_DIRS = {"vendor", "node_modules", ".git", "storage", "cache", "lib", "plugins", "wp-content", "core",
             "modules", "themes", "files"}


def slug(domain):
    return re.sub(r"[^a-z0-9]", "_", domain)


def site_dbs(cfg, domain):
    """Semua database milik website: dari pengaturan Git, aplikasi, lalu .env Laravel."""
    out = {}
    for section in ("git", "apps"):
        d = (cfg.get(section, {}).get(domain) or {}).get("db")
        if d:
            out[d["name"]] = d
    for env in (WWW / domain / "repo" / ".env", WWW / domain / "app" / ".env"):
        if env.exists():
            v = dict(re.findall(r"(?m)^(DB_\w+)=\"?([^\"\n]*)\"?$", env.read_text(errors="replace")))
            if v.get("DB_CONNECTION", "mysql") in ("mysql", "mariadb") and v.get("DB_DATABASE") \
                    and v["DB_DATABASE"] not in out and v.get("DB_USERNAME") != "root":
                out[v["DB_DATABASE"]] = {"name": v["DB_DATABASE"], "user": v.get("DB_USERNAME", ""),
                                         "password": v.get("DB_PASSWORD", "")}
    return list(out.values())


def backup_dir(domain):
    return BACKUPS / domain


def list_backups(domain=None):
    items = []
    dirs = [backup_dir(domain)] if domain else (sorted(BACKUPS.iterdir()) if BACKUPS.exists() else [])
    for d in dirs:
        if d.is_dir() and not d.name.startswith("."):
            for f in d.glob("*.tar.gz"):
                st = f.stat()
                items.append({"domain": d.name, "name": f.name, "path": f, "size": st.st_size, "time": st.st_mtime})
    return sorted(items, key=lambda x: x["time"], reverse=True)


def create(cfg, domain, log=None):
    """Buat backup satu website. Kembalikan path berkas .tar.gz."""
    say = (lambda m: (log.write(m + "\n"), log.flush())) if log else (lambda m: None)
    site = WWW / domain
    conf = NGINX_AVAIL / f"{SITE_PREFIX}{domain}.conf"
    if not site.is_dir() or not conf.exists():
        raise RuntimeError(f"Website {domain} tidak ditemukan")
    out_dir = backup_dir(domain)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = BACKUPS / f".work-{domain}-{stamp}"
    (work / "db").mkdir(parents=True)
    try:
        dbs = site_dbs(cfg, domain)
        for d in dbs:
            say(f"dump database {d['name']}")
            with open(work / "db" / f"{d['name']}.sql", "w") as f:
                p = subprocess.run(["mysqldump", "--single-transaction", "--routines", "--triggers",
                                    "--default-character-set=utf8mb4", d["name"]], stdout=f,
                                   stderr=subprocess.PIPE, text=True)
            if p.returncode != 0:
                raise RuntimeError(f"mysqldump {d['name']} gagal: {p.stderr.strip()}")
        nginx = conf.read_text()
        root = re.search(r"(?m)^\s*root (.+);$", nginx).group(1)
        cron_app = Path("/etc/cron.d") / f"localpanel-app-{slug(domain)}"
        manifest = {
            "format": FORMAT, "domain": domain, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "nginx_root": root, "nginx_conf": nginx, "dbs": dbs,
            "git": cfg.get("git", {}).get(domain), "app": cfg.get("apps", {}).get(domain),
            "queue": subprocess.run(["systemctl", "is-enabled", f"localpanel-queue@{domain}.service"],
                                    capture_output=True, text=True).stdout.strip() == "enabled",
            "schedule": (Path("/etc/cron.d") / ("localpanel-" + domain.replace(".", "_"))).exists(),
            "app_cron": cron_app.read_text() if cron_app.exists() else None,
        }
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
        out = out_dir / f"{domain}-{stamp}.tar.gz"
        say(f"arsipkan file website ke {out.name}")
        p = subprocess.run(["tar", "czf", str(out), "-C", str(work), "manifest.json", "db",
                            "-C", str(WWW), "--exclude=node_modules", domain], stderr=subprocess.PIPE, text=True)
        if p.returncode not in (0, 1):  # 1 = ada file berubah saat diarsipkan (mis. log), tetap valid
            out.unlink(missing_ok=True)
            raise RuntimeError(f"tar gagal: {p.stderr.strip()}")
        out.chmod(0o600)
        say(f"selesai: {out} ({out.stat().st_size // 1024} KB)")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def prune(domain, keep):
    for item in list_backups(domain)[keep:]:
        item["path"].unlink(missing_ok=True)


def read_manifest(path):
    with tarfile.open(path, "r:gz") as t:
        m = t.extractfile("manifest.json")
        if not m:
            raise RuntimeError("manifest.json tidak ada; ini bukan backup LocalPanel")
        return json.loads(m.read())


def extract(path, dest):
    with tarfile.open(path, "r:gz") as t:
        t.extractall(dest, filter="tar")  # tolak path absolut / '..'; pemilik & izin dipertahankan


def config_files(site):
    """Berkas konfigurasi aplikasi di folder website (maks. 4 level, lewati folder besar)."""
    def walk(d, depth):
        for p in d.iterdir():
            if p.is_file() and p.name in CONFIG_FILES:
                yield p
            elif p.is_dir() and not p.is_symlink() and depth < 4 and p.name not in SKIP_DIRS:
                yield from walk(p, depth + 1)
    return list(walk(site, 0)) if site.is_dir() else []


def rewrite_configs(site, replacements):
    """Ganti teks (domain lama, nama DB lama, ...) di berkas konfigurasi. Kembalikan berkas yang diubah."""
    changed = []
    for f in config_files(site):
        text = f.read_text(errors="replace")
        new = text
        for old, rep in replacements:
            new = re.sub(rf"(?<![\w-]){re.escape(old)}(?![\w-])", rep, new)
        if new != text:
            f.write_text(new)
            changed.append(f)
    return changed


def due(domain, freq):
    last = list_backups(domain)
    if not last:
        return True
    age = time.time() - last[0]["time"]
    return age > (6.5 * 86400 if freq == "weekly" else 20 * 3600)


def auto():
    cfg = json.loads(CONFIG.read_text())
    BACKUPS.mkdir(parents=True, exist_ok=True)
    with open(BACKUPS / "auto.log", "a") as log:
        for domain, s in (cfg.get("backup_schedule") or {}).items():
            if s.get("freq") not in ("daily", "weekly") or not due(domain, s["freq"]):
                continue
            log.write(f"\n== {time.strftime('%Y-%m-%d %H:%M:%S')} backup otomatis {domain}\n")
            try:
                create(cfg, domain, log)
                prune(domain, int(s.get("keep", 7)))
            except Exception as e:
                log.write(f"❌ gagal: {e}\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["auto"]:
        auto()
    else:
        print("Pemakaian: backup.py auto")
