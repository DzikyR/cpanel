"""Application Manager: installer aplikasi sekali klik untuk LocalPanel.

Setiap aplikasi adalah satu "resep" (fungsi) yang menerima Ctx dan mengembalikan dict hasil:
  root      folder yang dijadikan document root nginx
  fallback  (opsional) target try_files nginx, mis. /r.php untuk Moodle
  cron      (opsional) baris cron (tanpa jadwal & user) yang dijalankan tiap menit sebagai www-data
  admin_url URL halaman admin
Untuk menambah aplikasi: tulis fungsi resep baru lalu daftarkan di CATALOG.
"""
import json, re, secrets, shutil, subprocess, threading, time, urllib.request
from pathlib import Path

import gitdeploy

BASE = Path(__file__).resolve().parent
LOGS = BASE / "deploy-logs"
BIN = BASE / "bin"
CACHE = BASE / "cache"  # paket unduhan berversi disimpan agar instalasi berikutnya cepat
WP_CLI = BIN / "wp-cli.phar"
PHP = "/usr/bin/php"

_locks = {}


class Ctx:
    def __init__(self, www, domain, url, db, admin, log):
        self.www, self.domain, self.url, self.db, self.admin, self.log = www, domain, url, db, admin, log
        self.site = www / domain
        self.version = None

    def step(self, title, cmd, cwd=None, timeout=900, input=None):
        self.log.write(f"\n$ {title}\n")
        self.log.flush()
        p = subprocess.run(cmd, cwd=cwd, input=input, text=input is not None, stdout=self.log,
                           stderr=subprocess.STDOUT, timeout=timeout,
                           env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root",
                                "COMPOSER_ALLOW_SUPERUSER": "1", "COMPOSER_NO_INTERACTION": "1"})
        if p.returncode != 0:
            raise RuntimeError(f"{title} gagal (exit {p.returncode})")

    def say(self, msg):
        self.log.write(msg + "\n")
        self.log.flush()

    def download(self, url, dest, cache=True):
        cached = CACHE / url.rsplit("/", 1)[-1]
        if cache and cached.exists():
            self.say(f"\n$ pakai cache {cached.name}")
        else:
            CACHE.mkdir(exist_ok=True)
            part = cached.with_suffix(".part")
            self.step(f"unduh {url}", ["curl", "-fSL", "--retry", "3", "-sS", "-o", str(part), url], timeout=1800)
            part.rename(cached)
        shutil.copy(cached, dest)
        if not cache:
            cached.unlink()

    def extract(self, archive, dest, strip=1):
        """Ekstrak tar.gz / zip ke dest, membuang `strip` level folder teratas."""
        dest.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".tmp-extract-{secrets.token_hex(4)}"
        tmp.mkdir()
        try:
            if str(archive).endswith(".zip"):
                self.step("ekstrak", ["unzip", "-q", str(archive), "-d", str(tmp)])
            else:
                self.step("ekstrak", ["tar", "xzf", str(archive), "-C", str(tmp)])
            src = tmp
            for _ in range(strip):
                kids = [k for k in src.iterdir()]
                if len(kids) == 1 and kids[0].is_dir():
                    src = kids[0]
            for item in src.iterdir():
                shutil.move(str(item), str(dest / item.name))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "LocalPanel"}), timeout=30) as r:
        return r.read().decode()


def ensure_wp_cli(ctx=None):
    if not WP_CLI.exists():
        BIN.mkdir(exist_ok=True)
        cmd = ["curl", "-fsSL", "-o", str(WP_CLI),
               "https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar"]
        ctx.step("unduh wp-cli", cmd) if ctx else subprocess.run(cmd, check=True)
    return [PHP, str(WP_CLI), "--allow-root"]


def empty_root(ctx):
    """public_html bawaan (hanya index.php buatan panel) dikosongkan sebelum dipasang aplikasi."""
    root = ctx.site / "public_html"
    root.mkdir(parents=True, exist_ok=True)
    for f in root.iterdir():
        if f.name == "index.php" and "Dibuat oleh LocalPanel" in f.read_text(errors="replace"):
            f.unlink()
    if any(root.iterdir()):
        raise RuntimeError(f"{root} tidak kosong")
    return root


# ---------- resep ----------
def install_wordpress(ctx):
    offer = json.loads(fetch("https://api.wordpress.org/core/version-check/1.7/"))["offers"][0]
    ctx.version = offer["version"]
    root = empty_root(ctx)
    pkg = ctx.site / ".wordpress.zip"
    ctx.download(offer["download"], pkg)
    ctx.extract(pkg, root)
    pkg.unlink()
    wp = ensure_wp_cli(ctx) + [f"--path={root}"]
    d = ctx.db
    ctx.step("wp config create", wp + ["config", "create", f"--dbname={d['name']}", f"--dbuser={d['user']}",
                                       f"--dbpass={d['password']}", "--dbhost=127.0.0.1", "--skip-check"])
    a = ctx.admin
    ctx.step("wp core install", wp + ["core", "install", f"--url={ctx.url}", f"--title={a['title']}",
                                      f"--admin_user={a['user']}", f"--admin_password={a['password']}",
                                      f"--admin_email={a['email']}", "--skip-email"])
    if a.get("lang") == "id":
        try:
            ctx.step("bahasa Indonesia", wp + ["language", "core", "install", "id_ID", "--activate"])
        except RuntimeError:
            ctx.say("(bahasa Indonesia gagal dipasang, lanjut dengan bahasa Inggris)")
    ctx.step("permalink", wp + ["rewrite", "structure", "/%postname%/"])
    return {"root": root, "admin_url": f"{ctx.url}/wp-admin/"}


def install_ojs(ctx):
    xml = fetch("https://pkp.sfu.ca/ojs/xml/ojs-version.xml")
    ctx.version = re.search(r"<release>([^<]+)</release>", xml).group(1)
    package = re.search(r"<package>([^<]+)</package>", xml).group(1)
    root = empty_root(ctx)
    files_dir = ctx.site / "ojs-files"
    files_dir.mkdir(exist_ok=True)
    pkg = ctx.site / ".ojs.tar.gz"
    ctx.download(package, pkg)
    ctx.extract(pkg, root)
    pkg.unlink()
    shutil.copy(root / "config.TEMPLATE.inc.php", root / "config.inc.php")
    subprocess.run(["chown", "-R", "www-data:www-data", str(ctx.site)])
    d, a = ctx.db, ctx.admin
    lang = "id" if a.get("lang") == "id" else "en"
    answers = [lang, "en" if lang == "id" else "", str(files_dir), a["user"], a["password"], a["password"],
               a["email"], "mysqli", "127.0.0.1", d["user"], d["password"], d["name"],
               re.sub(r"[^a-z0-9.]", "", ctx.domain), "n", "y"]
    ctx.step("instal OJS", [PHP, "tools/install.php"], cwd=root, input="\n".join(answers) + "\n", timeout=1800)
    conf = root / "config.inc.php"
    text = conf.read_text()
    if not re.search(r"(?m)^installed = On", text):
        raise RuntimeError("Instalasi OJS tidak selesai, lihat log di atas")
    text = re.sub(r'(?m)^base_url = .*$', f'base_url = "{ctx.url}"', text, count=1)
    text = re.sub(r"(?m)^restful_urls = .*$", "restful_urls = On", text, count=1)
    hosts = json.dumps([ctx.domain, f"www.{ctx.domain}"])
    text = re.sub(r"(?m)^allowed_hosts = .*$", lambda _: f"allowed_hosts = '{hosts}'", text, count=1)
    conf.write_text(text)
    # restful_urls butuh PATH_INFO: /jurnal/id/... diteruskan sebagai /index.php/jurnal/id/...
    return {"root": root, "fallback": "/index.php$uri", "admin_url": f"{ctx.url}/index/login"}


def install_moodle(ctx):
    page = fetch("https://download.moodle.org/releases/latest/")
    branch = max(int(b) for b in re.findall(r"stable(\d{3})", page))
    ctx.version = re.search(r"Moodle (\d+\.\d+(?:\.\d+)?)", page).group(1)
    code = ctx.site / "moodle"
    data = ctx.site / "moodledata"
    if code.exists() and any(code.iterdir()):
        raise RuntimeError(f"{code} tidak kosong")
    data.mkdir(exist_ok=True)
    pkg = ctx.site / ".moodle.tgz"
    ctx.download(f"https://download.moodle.org/download.php/direct/stable{branch}/moodle-latest-{branch}.tgz", pkg,
                 cache=False)  # nama berkas tidak berversi
    ctx.extract(pkg, code)
    pkg.unlink()
    subprocess.run(["chown", "-R", "www-data:www-data", str(ctx.site)])
    d, a = ctx.db, ctx.admin
    short = re.sub(r"[^A-Za-z0-9]", "", a["title"])[:20] or "moodle"
    ctx.step("instal Moodle", ["sudo", "-u", "www-data", PHP, "admin/cli/install.php", "--non-interactive",
                               "--agree-license", "--allow-unstable", f"--lang={'id' if a.get('lang') == 'id' else 'en'}",
                               f"--wwwroot={ctx.url}", f"--dataroot={data}", "--dbtype=mariadb",
                               "--dbhost=127.0.0.1", f"--dbname={d['name']}", f"--dbuser={d['user']}",
                               f"--dbpass={d['password']}", f"--fullname={a['title']}", f"--shortname={short}",
                               f"--adminuser={a['user']}", f"--adminpass={a['password']}",
                               f"--adminemail={a['email']}"], cwd=code, timeout=3600)
    public = code / "public"
    return {"root": public if (public / "index.php").exists() else code,
            "fallback": "/r.php" if (public / "r.php").exists() else None,
            "cron": f"{PHP} {code}/admin/cli/cron.php",
            "admin_url": f"{ctx.url}/admin/"}


def install_laravel(ctx):
    app = ctx.site / "app"
    if app.exists():
        raise RuntimeError(f"{app} sudah ada")
    ctx.step("composer create-project", ["composer", "create-project", "--no-dev", "--prefer-dist",
                                         "laravel/laravel", str(app)], timeout=1800)
    (app / ".env").unlink(missing_ok=True)
    gitdeploy.write_env(app, ctx.db)
    env = app / ".env"
    env.write_text(re.sub(r"(?m)^APP_URL=.*$", f"APP_URL={ctx.url}", env.read_text()))
    (app / "database" / "database.sqlite").unlink(missing_ok=True)
    ctx.step("migrate", [PHP, "artisan", "migrate", "--force"], cwd=app)
    ctx.step("storage:link", [PHP, "artisan", "storage:link"], cwd=app)
    ctx.version = subprocess.run([PHP, "artisan", "--version"], cwd=app, capture_output=True,
                                 text=True).stdout.strip().replace("Laravel Framework ", "")
    return {"root": app / "public", "admin_url": ctx.url}


def install_phpmyadmin(ctx):
    info = json.loads(fetch("https://www.phpmyadmin.net/home_page/version.json"))
    ctx.version = info["version"]
    root = empty_root(ctx)
    pkg = ctx.site / ".pma.tar.gz"
    ctx.download(f"https://files.phpmyadmin.net/phpMyAdmin/{ctx.version}/phpMyAdmin-{ctx.version}-all-languages.tar.gz", pkg)
    ctx.extract(pkg, root)
    pkg.unlink()
    (root / "tmp").mkdir(exist_ok=True)
    (root / "config.inc.php").write_text(
        "<?php\n// Dibuat oleh LocalPanel\n"
        f"$cfg['blowfish_secret'] = sodium_hex2bin('{secrets.token_hex(32)}');\n"
        "$i = 1;\n$cfg['Servers'][$i]['auth_type'] = 'cookie';\n"
        "$cfg['Servers'][$i]['host'] = 'localhost';\n$cfg['Servers'][$i]['AllowNoPassword'] = false;\n"
        "$cfg['TempDir'] = __DIR__ . '/tmp';\n")
    shutil.rmtree(root / "setup", ignore_errors=True)
    return {"root": root, "admin_url": ctx.url}


SLIMS_INSTALLER = r"""<?php
// Dibuat oleh LocalPanel: jalankan installer SLiMS (install/api.php do-install) tanpa browser
define('INDEX_AUTH', '1');
define('ACTION', 'install');
chdir(__DIR__);
require 'SLiMS.inc.php';
$_SESSION = [];
$_POST = ['engine' => 'InnoDB'];
$o = ['db_host' => '127.0.0.1', 'db_port' => '3306', 'db_name' => getenv('DB_NAME'),
      'db_user' => getenv('DB_USER'), 'db_pass' => getenv('DB_PASS')];
$s = new Install\SLiMS();
$s->createConnection($o['db_host'], $o['db_port'], $o['db_user'], $o['db_pass'], $o['db_name']);
require 'install.sql.php';
$types = ['create', 'insert', 'alter', 'update', 'delete', 'truncate', 'drop'];
$err = array_merge($s->query($sql, $types), $s->queryTrigger($query_trigger));
if (getenv('SAMPLE') === '1') {
    require 'install_sample_data.sql.php';
    $err = array_merge($err, $s->query($sample_sql, $types));
}
if (!$s->updateAdmin(getenv('ADMIN_USER'), getenv('ADMIN_PASS'))) $err[] = ['priority_error' => $s->getDb()->error];
$fatal = array_values(array_filter(array_column($err, 'priority_error')));
if ($fatal) { echo implode("\n", $fatal), "\n"; exit(1); }
$s->createConfigFile($o);
$s->createEnvFile();
echo "SLiMS terpasang\n";
"""


def install_slims(ctx):
    rel = json.loads(fetch("https://api.github.com/repos/slims/slims9_bulian/releases/latest"))
    ctx.version = rel["tag_name"].lstrip("v")
    url = next(a["browser_download_url"] for a in rel["assets"] if a["name"].endswith(f"{ctx.version}.tar.gz"))
    root = empty_root(ctx)
    pkg = ctx.site / ".slims.tar.gz"
    ctx.download(url, pkg)
    ctx.extract(pkg, root)
    pkg.unlink()
    script = root / "install" / "lp-install.php"
    script.write_text(SLIMS_INSTALLER)
    d, a = ctx.db, ctx.admin
    ctx.step("instal SLiMS", ["env", f"DB_NAME={d['name']}", f"DB_USER={d['user']}", f"DB_PASS={d['password']}",
                              f"ADMIN_USER={a['user']}", f"ADMIN_PASS={a['password']}",
                              f"SAMPLE={'1' if a.get('sample') else '0'}", PHP, str(script)], cwd=script.parent)
    # installer & berkas docker tidak dibutuhkan lagi (installer yang terbuka bisa dipakai menimpa instalasi)
    shutil.rmtree(root / "install", ignore_errors=True)
    for f in ("Dockerfile", "docker-compose.yml", "entrypoint.sh"):
        (root / f).unlink(missing_ok=True)
    return {"root": root, "admin_url": f"{ctx.url}/admin/"}


def install_joomla(ctx):
    rel = json.loads(fetch("https://api.github.com/repos/joomla/joomla-cms/releases/latest"))
    ctx.version = rel["tag_name"]
    url = next(a["browser_download_url"] for a in rel["assets"] if a["name"].endswith("Stable-Full_Package.tar.gz"))
    root = empty_root(ctx)
    pkg = ctx.site / ".joomla.tar.gz"
    ctx.download(url, pkg)
    ctx.extract(pkg, root, strip=0)
    pkg.unlink()
    d, a = ctx.db, ctx.admin
    ctx.step("instal Joomla", [PHP, "installation/joomla.php", "install", "--no-interaction",
                               f"--site-name={a['title']}", f"--admin-user={a['user']}",
                               f"--admin-username={a['user']}", f"--admin-password={a['password']}",
                               f"--admin-email={a['email']}", "--db-type=mysqli", "--db-host=127.0.0.1",
                               f"--db-user={d['user']}", f"--db-pass={d['password']}", f"--db-name={d['name']}",
                               f"--db-prefix={secrets.token_hex(3)[:5]}_"], cwd=root)
    shutil.rmtree(root / "installation", ignore_errors=True)
    return {"root": root, "admin_url": f"{ctx.url}/administrator/"}


def install_drupal(ctx):
    app = ctx.site / "drupal"
    if app.exists():
        raise RuntimeError(f"{app} sudah ada")
    ctx.step("composer create-project", ["composer", "create-project", "--no-dev", "--prefer-dist",
                                         "drupal/recommended-project", str(app)], timeout=1800)
    ctx.step("composer require drush", ["composer", "require", "--no-interaction", "drush/drush"],
             cwd=app, timeout=1800)
    d, a = ctx.db, ctx.admin
    drush = [str(app / "vendor/bin/drush"), f"--root={app}", "-y"]  # skrip proxy shell, jalankan langsung
    ctx.step("drush site:install", drush + ["site:install", "standard",
                                            f"--db-url=mysql://{d['user']}:{d['password']}@127.0.0.1/{d['name']}",
                                            f"--site-name={a['title']}", f"--site-mail={a['email']}",
                                            f"--account-name={a['user']}", f"--account-pass={a['password']}",
                                            f"--account-mail={a['email']}"], timeout=1800)
    settings = app / "web/sites/default/settings.php"
    settings.chmod(0o644)
    # hanya terima request untuk domain ini (cegah HOST header injection); ditulis polos agar restore bisa ganti domain
    with open(settings, "a") as f:
        f.write("\n// Dikelola oleh LocalPanel\n$settings['trusted_host_patterns'] = ["
                f"'^' . preg_quote('{ctx.domain}') . '$', '^' . preg_quote('www.{ctx.domain}') . '$'];\n")
    settings.chmod(0o444)
    if a.get("lang") == "id":
        try:
            ctx.step("bahasa Indonesia", drush + ["pm:install", "language", "locale"], timeout=600)
            ctx.step("tambah bahasa id", drush + ["language:add", "id"], timeout=900)
            ctx.step("jadikan default", drush + ["config:set", "system.site", "default_langcode", "id"])
        except RuntimeError:
            ctx.say("(bahasa Indonesia gagal dipasang, lanjut dengan bahasa Inggris)")
    ctx.version = subprocess.run(drush + ["status", "--field=drupal-version"], capture_output=True,
                                 text=True).stdout.strip()
    return {"root": app / "web", "admin_url": f"{ctx.url}/user/login"}


CATALOG = {
    "wordpress": {"name": "WordPress", "icon": "📝", "db": True, "admin": True, "fn": install_wordpress,
                  "desc": "Website, blog, dan company profile. CMS paling populer."},
    "ojs": {"name": "OJS", "icon": "📚", "db": True, "admin": True, "fn": install_ojs,
            "desc": "Open Journal Systems: pengelolaan & penerbitan jurnal ilmiah."},
    "moodle": {"name": "Moodle", "icon": "🎓", "db": True, "admin": True, "fn": install_moodle,
               "desc": "LMS / e-learning. Butuh RAM cukup; cron dipasang otomatis."},
    "laravel": {"name": "Laravel", "icon": "🔺", "db": True, "admin": False, "fn": install_laravel,
                "desc": "Proyek Laravel baru (versi terbaru) dengan database MySQL siap pakai."},
    "joomla": {"name": "Joomla", "icon": "🧱", "db": True, "admin": True, "min_pw": 12, "fn": install_joomla,
               "desc": "CMS untuk portal, website instansi & organisasi."},
    "drupal": {"name": "Drupal", "icon": "💧", "db": True, "admin": True, "fn": install_drupal,
               "desc": "CMS enterprise untuk portal besar, kampus & pemerintahan."},
    "slims": {"name": "SLiMS", "icon": "📖", "db": True, "admin": True, "fn": install_slims, "sample": True,
              "desc": "Senayan Library Management System: otomasi perpustakaan & OPAC."},
    "phpmyadmin": {"name": "phpMyAdmin", "icon": "🗄️", "db": False, "admin": False, "fn": install_phpmyadmin,
                   "desc": "Kelola database lewat browser. Login dengan user database website."},
}


def log_path(domain):
    return LOGS / f"app-{domain}.log"


def read_log(domain):
    p = log_path(domain)
    return p.read_text(errors="replace")[-30000:] if p.exists() else ""


def is_running(domain):
    lock = _locks.get(domain)
    return bool(lock and lock.locked())


def install(www, domain, app_id, url, admin, make_db, on_done):
    """Pasang aplikasi di background. on_done(ok, result) dipanggil di akhir."""
    lock = _locks.setdefault(domain, threading.Lock())
    if not lock.acquire(blocking=False):
        return False
    recipe = CATALOG[app_id]

    def job():
        LOGS.mkdir(exist_ok=True)
        ok, result = True, {}
        with open(log_path(domain), "w") as log:
            log.write(f"Instal {recipe['name']} di {domain} — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            try:
                db = None
                if recipe["db"]:
                    db = make_db()
                    log.write(f"Database: {db['name']}  User: {db['user']}\n")
                ctx = Ctx(www, domain, url, db, admin, log)
                result = recipe["fn"](ctx)
                result["version"] = ctx.version
                subprocess.run(["chown", "-R", "www-data:www-data", str(www / domain)])
                log.write(f"\n✅ {recipe['name']} {ctx.version or ''} terpasang: {url}\n")
            except Exception as e:
                ok = False
                log.write(f"\n❌ Instalasi gagal: {e}\n")
            log.flush()
            try:
                on_done(ok, result)
            except Exception as e:
                log.write(f"\n❌ Gagal menyimpan pengaturan: {e}\n")
        lock.release()

    threading.Thread(target=job, daemon=True).start()
    return True
