"""Brevo (relay SMTP keluar) untuk mail server LocalPanel: autentikasi domain pengirim lewat API."""
import json, urllib.error, urllib.request

API = "https://api.brevo.com/v3"


class BrevoError(Exception):
    pass


def call(key, method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"api-key": key, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("message")
        except ValueError:
            msg = None
        if e.code == 404:
            return None
        raise BrevoError(f"HTTP {e.code}: {msg or e.reason}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise BrevoError(f"Tidak bisa menghubungi Brevo: {e}")


def account(key):
    a = call(key, "GET", "/account")
    if a is None:
        raise BrevoError("API key tidak valid")
    return a.get("email") or a.get("companyName") or "?"


def domain(key, name):
    """Info domain di Brevo (None bila belum ditambahkan)."""
    return call(key, "GET", f"/senders/domains/{name}")


def add_domain(key, name):
    call(key, "POST", "/senders/domains", {"name": name})
    return domain(key, name)


def authenticate(key, name):
    call(key, "PUT", f"/senders/domains/{name}/authenticate")
    return domain(key, name)


def records(info, name):
    """Record DNS yang diminta Brevo -> [(type, fqdn, value)]."""
    out = []
    for r in ((info or {}).get("dns_records") or {}).values():
        if not isinstance(r, dict) or not r.get("value"):
            continue
        host = (r.get("host_name") or "@").rstrip(".")
        fqdn = name if host in ("@", "", name) else host if host.endswith("." + name) else f"{host}.{name}"
        out.append((r.get("type", "TXT").upper(), fqdn, r["value"]))
    return out
