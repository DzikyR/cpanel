"""Integrasi Cloudflare Tunnel + DNS untuk LocalPanel."""
import base64, json, urllib.error, urllib.request

API = "https://api.cloudflare.com/client/v4"
TUNNEL_TOKEN_FILE = "/etc/cloudflared/token"
ORIGIN = "http://localhost:80"


class CFError(Exception):
    pass


def tunnel_ids():
    """Ambil (account_id, tunnel_id) dari token cloudflared lokal."""
    try:
        raw = open(TUNNEL_TOKEN_FILE).read().strip()
        d = json.loads(base64.b64decode(raw + "==="))
        return d["a"], d["t"]
    except (OSError, ValueError, KeyError):
        return None, None


class Cloudflare:
    def __init__(self, token, account_id=None, tunnel_id=None):
        if not token:
            raise CFError("API Token Cloudflare belum diisi")
        self.token = token
        a, t = tunnel_ids()
        self.account_id, self.tunnel_id = account_id or a, tunnel_id or t
        if not (self.account_id and self.tunnel_id):
            raise CFError("Account ID / Tunnel ID tidak ditemukan")
        self.target = f"{self.tunnel_id}.cfargotunnel.com"

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            API + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                data = json.load(e)
            except ValueError:
                raise CFError(f"HTTP {e.code} dari Cloudflare")
        except (urllib.error.URLError, TimeoutError) as e:
            raise CFError(f"Tidak bisa menghubungi Cloudflare: {e}")
        if not data.get("success"):
            errs = "; ".join(f"{x.get('code')}: {x.get('message')}" for x in data.get("errors", []))
            raise CFError(errs or "Permintaan ke Cloudflare gagal")
        return data["result"]

    # ---------- verifikasi ----------
    def verify(self):
        """Cek token bisa membaca konfigurasi tunnel dan daftar zone."""
        self.get_config()
        zones = self.call("GET", "/zones?per_page=50")
        return [z["name"] for z in zones]

    def zones(self):
        """Nama domain (zone) yang aktif di akun Cloudflare."""
        return sorted(z["name"] for z in self.call("GET", "/zones?per_page=50&status=active"))

    def conflict(self, domain):
        """Record DNS lain yang sudah memakai hostname website ini (akan menghalangi publikasi), atau None."""
        zone_id, zone_name = self.find_zone(domain)
        for h in self.hostnames_for(domain, zone_name):
            recs = [r for r in self.call("GET", f"/zones/{zone_id}/dns_records?name={h}")
                    if not (r["type"] == "CNAME" and r["content"] == self.target)]
            if recs:
                kinds = ", ".join(f"{r['type']} {r['content']}" for r in recs)
                return f"{h} sudah dipakai record DNS lain ({kinds})"
        return None

    # ---------- zone / DNS ----------
    def find_zone(self, hostname):
        parts = hostname.split(".")
        for i in range(len(parts) - 1):
            name = ".".join(parts[i:])
            res = self.call("GET", f"/zones?name={name}")
            if res:
                return res[0]["id"], res[0]["name"]
        raise CFError(f"Domain untuk {hostname} belum ditambahkan ke akun Cloudflare")

    def ensure_dns(self, zone_id, hostname):
        recs = self.call("GET", f"/zones/{zone_id}/dns_records?name={hostname}")
        for r in recs:
            if r["type"] == "CNAME" and r["content"] == self.target:
                return "sudah ada"
        if recs:
            kinds = ", ".join(f"{r['type']} {r['content']}" for r in recs)
            raise CFError(f"{hostname} sudah punya record DNS lain ({kinds}). Hapus dulu di Cloudflare.")
        self.call("POST", f"/zones/{zone_id}/dns_records", {
            "type": "CNAME", "name": hostname, "content": self.target, "proxied": True,
            "comment": "Dibuat oleh LocalPanel"})
        return "dibuat"

    def remove_dns(self, zone_id, hostname):
        for r in self.call("GET", f"/zones/{zone_id}/dns_records?name={hostname}"):
            if r["type"] == "CNAME" and r["content"] == self.target:
                self.call("DELETE", f"/zones/{zone_id}/dns_records/{r['id']}")

    # ---------- tunnel ingress ----------
    def _cfg_path(self):
        return f"/accounts/{self.account_id}/cfd_tunnel/{self.tunnel_id}/configurations"

    def get_config(self):
        cfg = (self.call("GET", self._cfg_path()) or {}).get("config") or {}
        cfg.setdefault("ingress", [])
        return cfg

    def public_hostnames(self):
        return {r["hostname"]: r["service"] for r in self.get_config()["ingress"] if r.get("hostname")}

    def add_ingress(self, hostnames):
        cfg = self.get_config()
        rules = cfg["ingress"]
        existing = {r.get("hostname") for r in rules}
        catch_all = [r for r in rules if not r.get("hostname")] or [{"service": "http_status:404"}]
        named = [r for r in rules if r.get("hostname")]
        # hostname spesifik harus di atas wildcard
        new = [{"hostname": h, "service": ORIGIN, "originRequest": {}} for h in hostnames if h not in existing]
        if not new:
            return
        plain = [r for r in named if "*" not in r["hostname"]]
        wild = [r for r in named if "*" in r["hostname"]]
        cfg["ingress"] = plain + new + wild + catch_all[-1:]
        self.call("PUT", self._cfg_path(), {"config": cfg})

    def remove_ingress(self, hostnames):
        cfg = self.get_config()
        before = len(cfg["ingress"])
        cfg["ingress"] = [r for r in cfg["ingress"] if r.get("hostname") not in hostnames]
        if len(cfg["ingress"]) != before:
            self.call("PUT", self._cfg_path(), {"config": cfg})

    # ---------- operasi tingkat tinggi ----------
    def hostnames_for(self, domain, zone_name):
        # www hanya untuk domain utama; www.sub.domain tidak tercakup SSL gratis Cloudflare
        return [domain, f"www.{domain}"] if domain == zone_name else [domain]

    def publish(self, domain):
        zone_id, zone_name = self.find_zone(domain)
        hosts = self.hostnames_for(domain, zone_name)
        for h in hosts:
            self.ensure_dns(zone_id, h)
        self.add_ingress(hosts)
        return hosts

    def unpublish(self, domain):
        zone_id, zone_name = self.find_zone(domain)
        hosts = self.hostnames_for(domain, zone_name)
        self.remove_ingress(hosts)
        for h in hosts:
            self.remove_dns(zone_id, h)
        return hosts

    # ---------- email routing (mail server LocalPanel) ----------
    def put_worker(self, name, js, bindings):
        """Unggah Worker modul (multipart) beserta binding plain_text / secret_text."""
        boundary = "lp" + base64.b16encode(json.dumps(bindings).encode()[:8]).decode()
        meta = json.dumps({"main_module": "worker.js", "compatibility_date": "2024-09-23", "bindings": bindings})
        parts = [("metadata", "metadata.json", "application/json", meta),
                 ("worker.js", "worker.js", "application/javascript+module", js)]
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"; filename="{f}"\r\nContent-Type: {t}\r\n\r\n'
            .encode() + c.encode() + b"\r\n" for n, f, t, c in parts) + f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"{API}/accounts/{self.account_id}/workers/scripts/{name}", data=body,
                                     method="PUT", headers={"Authorization": f"Bearer {self.token}",
                                                            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            data = json.load(e)
        if not data.get("success"):
            raise CFError("; ".join(f"{x.get('code')}: {x.get('message')}" for x in data.get("errors", [])) or "gagal")

    def email_enable(self, domain):
        """Aktifkan Email Routing untuk domain/subdomain (Cloudflare menambah MX + SPF sendiri)."""
        zone_id, zone_name = self.find_zone(domain)
        mx = [r for r in self.call("GET", f"/zones/{zone_id}/dns_records?type=MX&name={domain}")
              if not r["content"].endswith(".mx.cloudflare.net")]
        if mx:
            raise CFError(f"{domain} sudah punya MX lain ({', '.join(r['content'] for r in mx)}). Hapus dulu di Cloudflare.")
        self.call("POST", f"/zones/{zone_id}/email/routing/dns", {} if domain == zone_name else {"name": domain})
        return zone_id

    def email_status(self, domain):
        """Ringkasan: Email Routing aktif?, MX Cloudflare ada?"""
        zone_id, zone_name = self.find_zone(domain)
        s = self.call("GET", f"/zones/{zone_id}/email/routing")
        mx = self.call("GET", f"/zones/{zone_id}/dns_records?type=MX&name={domain}")
        return {"enabled": bool(s.get("enabled")), "status": s.get("status"),
                "mx": [r["content"] for r in mx]}

    def email_rules(self, zone_id):
        return self.call("GET", f"/zones/{zone_id}/email/routing/rules?per_page=50")

    def email_sync(self, domain, addresses, worker):
        """Samakan aturan Email Routing domain ini dengan daftar alamat (tiap alamat -> Worker)."""
        zone_id, zone_name = self.find_zone(domain)
        tag = "LocalPanel "
        have = {}
        for r in self.email_rules(zone_id):
            to = [m.get("value") for m in r.get("matchers", []) if m.get("field") == "to"]
            if r.get("name", "").startswith(tag) and to and to[0].endswith("@" + domain):
                have[to[0]] = r["id"]
        for addr in sorted(set(addresses) - set(have)):
            self.call("POST", f"/zones/{zone_id}/email/routing/rules", {
                "name": tag + addr, "enabled": True,
                "matchers": [{"type": "literal", "field": "to", "value": addr}],
                "actions": [{"type": "worker", "value": [worker]}]})
        for addr in set(have) - set(addresses):
            self.call("DELETE", f"/zones/{zone_id}/email/routing/rules/{have[addr]}")

    def email_catch_all(self, domain, worker):
        """Catch-all hanya per zone (domain utama), bukan per subdomain."""
        zone_id, zone_name = self.find_zone(domain)
        if domain != zone_name:
            raise CFError("Catch-all Cloudflare hanya bisa untuk domain utama, bukan subdomain")
        body = {"enabled": bool(worker), "matchers": [{"type": "all"}],
                "actions": [{"type": "worker", "value": [worker]}] if worker else [{"type": "drop"}]}
        self.call("PUT", f"/zones/{zone_id}/email/routing/rules/catch_all", body)

    def ensure_txt(self, name, value, prefix):
        """Buat/perbarui satu record TXT di `name` yang isinya diawali `prefix` (TXT lain tidak disentuh)."""
        zone_id, _ = self.find_zone(name)
        for r in self.call("GET", f"/zones/{zone_id}/dns_records?type=TXT&name={name}"):
            if r["content"].strip('"').startswith(prefix):
                if r["content"].strip('"') != value:
                    self.call("PATCH", f"/zones/{zone_id}/dns_records/{r['id']}", {"content": value})
                return "diperbarui"
        self.call("POST", f"/zones/{zone_id}/dns_records", {"type": "TXT", "name": name, "content": value,
                                                            "comment": "Dibuat oleh LocalPanel (email)"})
        return "dibuat"

    def ensure_cname_dns(self, name, target):
        zone_id, _ = self.find_zone(name)
        recs = self.call("GET", f"/zones/{zone_id}/dns_records?name={name}")
        if any(r["type"] == "CNAME" and r["content"] == target for r in recs):
            return
        if recs:
            raise CFError(f"{name} sudah punya record lain")
        self.call("POST", f"/zones/{zone_id}/dns_records", {"type": "CNAME", "name": name, "content": target,
                                                            "proxied": False, "comment": "Dibuat oleh LocalPanel (email)"})

    def txt(self, name):
        zone_id, _ = self.find_zone(name)
        return [r["content"].strip('"') for r in self.call("GET", f"/zones/{zone_id}/dns_records?type=TXT&name={name}")]

    def spf_include(self, domain, include):
        """Tambahkan include:<include> ke SPF domain (buat SPF bila belum ada)."""
        cur = next((t for t in self.txt(domain) if t.startswith("v=spf1")), None)
        if cur and f"include:{include}" in cur.split():
            return
        if cur:
            parts = cur.split()
            tail = [p for p in parts if p.endswith("all")]
            new = " ".join([p for p in parts if not p.endswith("all")] + [f"include:{include}"] + (tail or ["~all"]))
        else:
            new = f"v=spf1 include:_spf.mx.cloudflare.net include:{include} ~all"
        self.ensure_txt(domain, new, "v=spf1")
