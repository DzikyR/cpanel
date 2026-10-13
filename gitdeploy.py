"""Deploy website dari repository Git (GitHub/GitLab/dll) untuk LocalPanel."""
import base64, hashlib, hmac, os, re, secrets, shutil, subprocess, threading, time, zipfile
from pathlib import Path

KEYS = Path(__file__).resolve().parent / "keys"
LOGS = Path(__file__).resolve().parent / "deploy-logs"
REPO_RE = re.compile(r"^(https://[\w.-]+/[\w./-]+?(\.git)?|git@[\w.-]+:[\w./-]+?(\.git)?)$")
BRANCH_RE = re.compile(r"^[\w./-]{1,100}$")
SUBDIR_RE = re.compile(r"^([\w.-]+(/[\w.-]+)*)?$")

_locks = {}


def key_path(domain):
    return KEYS / domain


def ensure_key(domain):
    """Buat SSH deploy key per website (untuk repo privat). Kembalikan public key."""
    KEYS.mkdir(mode=0o700, exist_ok=True)
    k = key_path(domain)
    if not k.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"localpanel-{domain}",
                        "-f", str(k)], capture_output=True, check=True)
    return Path(str(k) + ".pub").read_text().strip()


def remove_key(domain):
    for p in (key_path(domain), Path(str(key_path(domain)) + ".pub")):
        p.unlink(missing_ok=True)


def repo_dir(www, domain):
    return www / domain / "repo"


def src_dir(www, domain):
    """Clone Git untuk repo berisi paket ZIP; isi ZIP diekstrak ke repo_dir."""
    return www / domain / ".src"


# berkas paket yang tidak ikut dipasang: installer web (panel sudah mengurus database & .env)
ZIP_SKIP = {".env", "public/pasang.php", "PASANG-KODE.txt"}


def _zip_prefix(z):
    """Folder dalam ZIP yang berisi aplikasi (punya artisan atau index.php), atau None."""
    names = set(z.namelist())
    for marker in ("artisan", "index.php"):
        hits = sorted((n for n in names if n == marker or n.endswith("/" + marker)), key=lambda n: n.count("/"))
        if hits and hits[0].count("/") <= 1:
            return hits[0][:-len(marker)]
    return None


def find_app_zip(rd):
    """Repo tanpa kode aplikasi di root, tapi berisi ZIP aplikasi -> path ZIP terbesar."""
    if (rd / "artisan").exists() or (rd / "index.php").exists() or (rd / "composer.json").exists():
        return None
    for f in sorted(rd.glob("*.zip"), key=lambda f: f.stat().st_size, reverse=True):
        try:
            with zipfile.ZipFile(f) as z:
                if _zip_prefix(z) is not None:
                    return f
        except zipfile.BadZipFile:
            continue
    return None


def extract_app_zip(path, dest):
    """Ekstrak ZIP ke dest. .env dan berkas storage/ yang sudah ada tidak ditimpa."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    n = 0
    with zipfile.ZipFile(path) as z:
        prefix = _zip_prefix(z)
        for info in z.infolist():
            if not info.filename.startswith(prefix):
                continue
            rel = info.filename[len(prefix):]
            if not rel or rel in ZIP_SKIP:
                continue
            target = (dest / rel).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"Path tidak aman di ZIP: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if rel.startswith("storage/") and target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
    return n


def log_path(domain):
    return LOGS / f"{domain}.log"


def read_log(domain):
    p = log_path(domain)
    return p.read_text(errors="replace")[-20000:] if p.exists() else ""


def is_running(domain):
    lock = _locks.get(domain)
    return bool(lock and lock.locked())


def verify_signature(secret, body, header):
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + digest, header)


def _env(domain):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", HOME="/root", COMPOSER_ALLOW_SUPERUSER="1",
               COMPOSER_NO_INTERACTION="1", NPM_CONFIG_UPDATE_NOTIFIER="false")
    env["GIT_SSH_COMMAND"] = (f"ssh -i {key_path(domain)} -o IdentitiesOnly=yes "
                              f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={KEYS}/known_hosts")
    return env


def write_env(rd, creds):
    """Buat .env dari .env.example dan isi koneksi database + APP_KEY."""
    text = (rd / ".env.example").read_text()
    values = {"DB_CONNECTION": "mysql", "DB_HOST": "127.0.0.1", "DB_PORT": "3306",
              "DB_DATABASE": creds["name"], "DB_USERNAME": creds["user"], "DB_PASSWORD": creds["password"]}
    if not re.search(r"(?m)^APP_KEY=\S", text):
        values["APP_KEY"] = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()
    for k, v in values.items():
        line = f"{k}={v}"
        text, n = re.subn(rf"(?m)^#?\s*{k}=.*$", lambda _: line, text, count=1)
        if not n:
            text = text.rstrip("\n") + f"\n{line}\n"
    env = rd / ".env"
    env.write_text(text)
    env.chmod(0o640)


def find_sql_dump(rd):
    """Cari file dump SQL bawaan repo: database/*.sql, lalu *.sql di root repo. Pilih yang terbesar."""
    for pattern in ("database/*.sql", "*.sql"):
        files = sorted(rd.glob(pattern), key=lambda f: f.stat().st_size, reverse=True)
        if files:
            return files[0]
    return None


def import_sql(dbname, path):
    """Import dump ke database website (sebagai root lewat unix_socket)."""
    sql = path.read_text(errors="replace")
    sql = re.sub(r"(?mi)^\s*(CREATE DATABASE|USE)\b.*$", "", sql)  # paksa masuk ke DB website
    sql = sql.replace("utf8mb4_0900_ai_ci", "utf8mb4_unicode_ci")  # dump MySQL 8 -> MariaDB
    p = subprocess.run(["mysql", dbname], input=sql, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Import {path.name} gagal: {p.stderr.strip()}")


def deploy(www, domain, gcfg, on_done=None, make_db=None):
    """Clone / pull repo lalu jalankan perintah setelah deploy. Berjalan di background."""
    lock = _locks.setdefault(domain, threading.Lock())
    if not lock.acquire(blocking=False):
        return False

    def job():
        LOGS.mkdir(exist_ok=True)
        ok = True
        with open(log_path(domain), "w") as log:
            def step(title, cmd, cwd=None, timeout=300, extra_env=None):
                log.write(f"\n$ {title}\n")
                log.flush()
                p = subprocess.run(cmd, cwd=cwd, env=dict(_env(domain), **(extra_env or {})), stdout=log,
                                   stderr=subprocess.STDOUT, timeout=timeout)
                if p.returncode != 0:
                    raise RuntimeError(f"Gagal (exit {p.returncode})")

            log.write(f"Deploy {domain} — {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                      f"Repo: {gcfg['repo']}  Branch: {gcfg['branch']}\n")
            rd = repo_dir(www, domain)
            src = src_dir(www, domain)
            gd = src if (src / ".git").exists() else rd  # folder clone Git
            git = ["git", "-c", "safe.directory=*"]
            try:
                if not (gd / ".git").exists():
                    if rd.exists() and any(rd.iterdir()):
                        raise RuntimeError(f"Folder {rd} tidak kosong")
                    step("git clone", git + ["clone", "--branch", gcfg["branch"], "--depth", "1",
                                             gcfg["repo"], str(rd)])
                else:
                    step("git remote set-url", git + ["remote", "set-url", "origin", gcfg["repo"]], cwd=gd)
                    step("git fetch", git + ["fetch", "--depth", "1", "origin", gcfg["branch"]], cwd=gd)
                    step("git reset --hard", git + ["reset", "--hard", "FETCH_HEAD"], cwd=gd)
                step("commit", git + ["log", "-1", "--format=%h %s (%an, %ar)"], cwd=gd)
                if gd == rd and find_app_zip(rd):
                    # repo hanya berisi paket ZIP: pindahkan clone ke .src, aplikasi diekstrak ke repo/
                    log.write(f"\nRepo berisi paket ZIP -> clone dipindah ke {src}\n")
                    rd.rename(src)
                    rd.mkdir()
                    gd = src
                if gd == src:
                    pkg = find_app_zip(src)
                    if not pkg:
                        raise RuntimeError("Paket ZIP aplikasi tidak ditemukan di repo")
                    log.write(f"\n$ ekstrak {pkg.name} ke {rd}\n")
                    log.write(f"{extract_app_zip(pkg, rd)} berkas diekstrak\n")
                new_db = False
                if make_db and not (rd / ".env").exists() and (rd / ".env.example").exists():
                    log.write("\n$ buat database & .env\n")
                    creds = make_db()
                    write_env(rd, creds)
                    new_db = True
                    log.write(f"Database: {creds['name']}  User: {creds['user']}  "
                              "(password ada di halaman Git Deploy)\n")
                    dump = find_sql_dump(rd)
                    if dump:
                        log.write(f"\n$ import {dump.relative_to(rd)} ke database\n")
                        log.flush()
                        import_sql(creds["name"], dump)
                        new_db = False  # data sudah dari dump, seeder tidak perlu dijalankan
                        log.write("Import selesai\n")
                cmd = (gcfg.get("post_cmd") or "").strip()
                if cmd:
                    step("perintah setelah deploy", ["bash", "-xc", cmd], cwd=rd, timeout=900,
                         extra_env={"LP_NEW_DB": "1" if new_db else "0"})
                subprocess.run(["chown", "-R", "www-data:www-data", str(www / domain)])
                log.write("\n✅ Deploy berhasil\n")
            except Exception as e:
                ok = False
                log.write(f"\n❌ Deploy gagal: {e}\n")
        if on_done:
            on_done(ok)
        lock.release()

    threading.Thread(target=job, daemon=True).start()
    return True
