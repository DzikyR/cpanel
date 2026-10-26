"""PostgreSQL untuk LocalPanel: database, user, dump & import.

Panel berjalan sebagai root dan memanggil psql sebagai user sistem postgres (peer auth).
Nama/password dikirim lewat variabel psql (:"nama" / :'nilai') agar selalu di-quote dengan benar.
Aplikasi terhubung lewat TCP 127.0.0.1:5432 dengan password (scram-sha-256).
"""
import shutil, subprocess

HOST, PORT = "127.0.0.1", "5432"
SYSTEM_DBS = {"postgres", "template0", "template1"}


class PGError(Exception):
    pass


def available():
    return bool(shutil.which("psql"))


def psql(sql, db="postgres", **params):
    """Jalankan SQL (stdin) sebagai postgres. Kembalikan baris hasil (kolom dipisah '|')."""
    cmd = ["runuser", "-u", "postgres", "--", "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", db]
    for k, v in params.items():
        cmd += ["-v", f"{k}={v}"]
    p = subprocess.run(cmd, input=sql, capture_output=True, text=True, cwd="/tmp")
    if p.returncode != 0:
        raise PGError(p.stderr.strip().replace("psql:<stdin>:", "").strip() or "psql gagal")
    return [l.split("|") for l in p.stdout.splitlines() if l]


def list_dbs():
    rows = psql("SELECT d.datname, pg_database_size(d.datname), r.rolname FROM pg_database d "
                "JOIN pg_roles r ON r.oid = d.datdba WHERE NOT d.datistemplate ORDER BY 1;")
    return [{"name": r[0], "size": int(r[1]), "owner": r[2]} for r in rows if r[0] not in SYSTEM_DBS]


def list_users():
    rows = psql("SELECT rolname FROM pg_roles WHERE rolcanlogin AND rolname <> 'postgres' ORDER BY 1;")
    return [r[0] for r in rows]


def user_exists(user):
    return bool(psql("SELECT 1 FROM pg_roles WHERE rolname = :'u';", u=user))


def db_exists(name):
    return bool(psql("SELECT 1 FROM pg_database WHERE datname = :'n';", n=name))


def ensure_user(user, password):
    verb = "ALTER" if user_exists(user) else "CREATE"
    psql(f"{verb} ROLE :\"u\" LOGIN PASSWORD :'p';", u=user, p=password)


def ensure_db(creds):
    """Buat user + database (pemiliknya user tsb), atau samakan password bila sudah ada."""
    ensure_user(creds["user"], creds["password"])
    if not db_exists(creds["name"]):
        psql("CREATE DATABASE :\"n\" OWNER :\"u\" ENCODING 'UTF8' TEMPLATE template0;",
             n=creds["name"], u=creds["user"])
    psql("REVOKE ALL ON DATABASE :\"n\" FROM PUBLIC; GRANT ALL ON DATABASE :\"n\" TO :\"u\";",
         n=creds["name"], u=creds["user"])


def create_db(name, owner=None):
    if owner:
        psql("CREATE DATABASE :\"n\" OWNER :\"u\" ENCODING 'UTF8' TEMPLATE template0;"
             "REVOKE ALL ON DATABASE :\"n\" FROM PUBLIC;", n=name, u=owner)
    else:
        psql("CREATE DATABASE :\"n\" ENCODING 'UTF8' TEMPLATE template0;", n=name)


def drop_db(name):
    psql("DROP DATABASE IF EXISTS :\"n\" WITH (FORCE);", n=name)


def drop_user(user):
    owned = [r[0] for r in psql("SELECT datname FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba "
                                "WHERE r.rolname = :'u';", u=user)]
    if owned:
        raise PGError(f"User {user} masih memiliki database: {', '.join(owned)}. Hapus database-nya dulu.")
    psql("DROP ROLE IF EXISTS :\"u\";", u=user)


def url(creds):
    return f"postgresql://{creds['user']}:{creds['password']}@{HOST}:{PORT}/{creds['name']}"


def dump(name, out):
    """pg_dump teks polos tanpa pemilik/izin (bisa dipulihkan ke user lain); objek lama ditimpa saat import."""
    with open(out, "w") as f:
        p = subprocess.run(["runuser", "-u", "postgres", "--", "pg_dump", "--no-owner", "--no-acl", "--clean",
                            "--if-exists", name],
                           stdout=f, stderr=subprocess.PIPE, text=True, cwd="/tmp")
    if p.returncode != 0:
        raise PGError(f"pg_dump {name} gagal: {p.stderr.strip()}")


def import_sql(creds, path):
    """Import dump ke database; objek dibuat atas nama user aplikasi (SET ROLE) agar menjadi miliknya."""
    with open(path) as f:
        sql = f'SET ROLE "{creds["user"]}";\n' + f.read()
    p = subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
                        "-d", creds["name"]], input=sql, capture_output=True, text=True, cwd="/tmp")
    if p.returncode != 0:
        raise PGError(f"Import {path.name} gagal: {p.stderr.strip()[-500:]}")
