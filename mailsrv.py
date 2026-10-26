"""Mail server LocalPanel: Postfix + Dovecot dengan kotak surat virtual.

Alur:
  masuk   Cloudflare Email Routing -> Email Worker -> POST /mail/inbound (panel, lewat tunnel)
          -> sendmail -> Postfix -> Dovecot LMTP -> /var/vmail/<domain>/<user>/Maildir
  keluar  webmail / aplikasi -> Postfix 127.0.0.1:587 (login kotak surat) -> relay Brevo :587
Semua layanan hanya mendengarkan localhost; dari luar hanya webmail (HTTPS lewat tunnel).
Sumber kebenaran ada di config.json bagian "mail"; berkas peta Postfix/Dovecot dibuat ulang oleh render().
"""
import hashlib, hmac, os, re, shutil, subprocess, time
from pathlib import Path

VMAIL = Path("/var/vmail")
VMAIL_UID = 5000
DOVECOT_CONF = Path("/etc/dovecot/dovecot.conf")
USERS = Path("/etc/dovecot/lp-users")
PF = Path("/etc/postfix")
MAPS = {k: PF / f"lp-{k}" for k in ("domains", "mailboxes", "aliases", "senders", "relay-sasl")}
LOCAL_RE = re.compile(r"^[a-z0-9]([a-z0-9._+-]{0,62}[a-z0-9])?$")
QUOTA_RE = re.compile(r"^\d{1,5}[MG]$")
BREVO_RELAY = "[smtp-relay.brevo.com]:587"

DOVECOT = r"""# Dikelola oleh LocalPanel: kotak surat virtual (hanya localhost; akses luar lewat webmail)
dovecot_config_version = 2.4.0
dovecot_storage_version = 2.4.0

protocols = imap lmtp
listen = 127.0.0.1
ssl = no
auth_allow_cleartext = yes
auth_mechanisms = plain login
auth_username_format = %{user | lower}

mail_driver = maildir
mail_home = /var/vmail/%{user | domain}/%{user | username}
mail_path = ~/Maildir
mail_uid = vmail
mail_gid = vmail
first_valid_uid = 5000
last_valid_uid = 5000

# kolom tambahan per user: userdb_quota/user/storage_size=1G
passdb passwd-file {
  passwd_file_path = /etc/dovecot/lp-users
}
userdb passwd-file {
  passwd_file_path = /etc/dovecot/lp-users
}

mail_plugins {
  quota = yes
}
protocol imap {
  mail_plugins {
    imap_quota = yes
  }
}
quota user {
  storage_size = 1G
}

namespace inbox {
  inbox = yes
  separator = /
  mailbox Drafts {
    special_use = \Drafts
    auto = subscribe
  }
  mailbox Sent {
    special_use = \Sent
    auto = subscribe
  }
  mailbox Junk {
    special_use = \Junk
    auto = subscribe
  }
  mailbox Trash {
    special_use = \Trash
    auto = subscribe
  }
}

service lmtp {
  unix_listener /var/spool/postfix/private/dovecot-lmtp {
    mode = 0600
    user = postfix
    group = postfix
  }
}
service auth {
  unix_listener /var/spool/postfix/private/auth {
    mode = 0660
    user = postfix
    group = postfix
  }
}
log_path = syslog
"""

SUBMISSION = """# LocalPanel: kirim email dari webmail/aplikasi (login kotak surat), hanya localhost
127.0.0.1:submission inet n - y - - smtpd
  -o syslog_name=postfix/submission
  -o smtpd_tls_security_level=none
  -o smtpd_sasl_auth_enable=yes
  -o smtpd_sasl_type=dovecot
  -o smtpd_sasl_path=private/auth
  -o smtpd_sender_login_maps=texthash:/etc/postfix/lp-senders
  -o smtpd_client_restrictions=permit_sasl_authenticated,reject
  -o smtpd_sender_restrictions=reject_sender_login_mismatch
  -o smtpd_recipient_restrictions=permit_sasl_authenticated,reject
"""


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return p.returncode, (p.stdout + p.stderr).strip()


def installed():
    return bool(shutil.which("postfix") and shutil.which("doveadm"))


def setup(hostname):
    """Konfigurasi Postfix & Dovecot untuk LocalPanel (aman dipanggil berulang)."""
    if not installed():
        raise RuntimeError("Paket postfix/dovecot belum terpasang (jalankan ulang install.sh)")
    if run(["id", "vmail"])[0] != 0:
        run(["useradd", "-r", "-u", str(VMAIL_UID), "-U", "-d", str(VMAIL), "-s", "/usr/sbin/nologin", "vmail"])
    VMAIL.mkdir(exist_ok=True)
    shutil.chown(VMAIL, "vmail", "vmail")
    VMAIL.chmod(0o750)
    if DOVECOT_CONF.read_text() != DOVECOT:
        DOVECOT_CONF.write_text(DOVECOT)
    for f in MAPS.values():
        f.touch()
    MAPS["relay-sasl"].chmod(0o600)
    if not USERS.exists():
        USERS.touch()
    shutil.chown(USERS, "root", "dovecot")
    USERS.chmod(0o640)
    settings = {
        "myhostname": hostname, "mydestination": "localhost", "inet_interfaces": "loopback-only",
        "inet_protocols": "ipv4", "mynetworks": "127.0.0.0/8",
        "virtual_mailbox_domains": f"texthash:{MAPS['domains']}",
        "virtual_mailbox_maps": f"texthash:{MAPS['mailboxes']}",
        "virtual_alias_maps": f"texthash:{MAPS['aliases']}",
        "virtual_transport": "lmtp:unix:private/dovecot-lmtp",
        "smtp_sasl_auth_enable": "yes", "smtp_sasl_password_maps": f"texthash:{MAPS['relay-sasl']}",
        "smtp_sasl_security_options": "noanonymous", "smtp_tls_security_level": "encrypt",
        "message_size_limit": str(26 * 1024 * 1024), "mailbox_size_limit": "0",
    }
    run(["postconf", "-e", *(f"{k}={v}" for k, v in settings.items())])
    master = PF / "master.cf"
    text = master.read_text()
    if "# LocalPanel: kirim email" not in text:
        master.write_text(text.rstrip("\n") + "\n" + SUBMISSION)
    run(["systemctl", "enable", "postfix", "dovecot"])
    run(["systemctl", "restart", "dovecot"])
    run(["systemctl", "restart", "postfix"])


def hash_password(pw):
    p = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=pw, capture_output=True, text=True, check=True)
    return "{SHA512-CRYPT}" + p.stdout.strip()


def render(mail):
    """Tulis ulang semua peta Postfix/Dovecot dari pengaturan panel, lalu reload Postfix."""
    domains = mail.get("domains", {})
    users, boxes, aliases, senders = [], [], [], []
    for d, dm in sorted(domains.items()):
        for local, b in sorted(dm.get("mailboxes", {}).items()):
            addr = f"{local}@{d}"
            users.append(f"{addr}:{b['hash']}::::::userdb_quota/user/storage_size={b.get('quota', '1G')}")
            boxes.append(f"{addr} {d}/{local}/")
            senders.append(f"{addr} {addr}")
        if "*" in dm.get("forwards", {}):
            # catch-all menimpa semua alamat; kotak surat dipetakan ke dirinya sendiri agar tetap menerima
            aliases += [f"{l}@{d} {l}@{d}" for l in sorted(dm.get("mailboxes", {})) if l not in dm["forwards"]]
        for local, dests in sorted(dm.get("forwards", {}).items()):
            src = f"{local}@{d}" if local != "*" else f"@{d}"
            # kotak surat dengan nama sama tetap menerima salinannya
            keep = [f"{local}@{d}"] if local in dm.get("mailboxes", {}) else []
            aliases.append(f"{src} {', '.join(keep + dests)}")
            owners = [x for x in dests if x.endswith("@" + d) and x.split("@")[0] in dm.get("mailboxes", {})]
            if local != "*" and local not in dm.get("mailboxes", {}) and owners:
                senders.append(f"{local}@{d} {', '.join(owners)}")  # boleh kirim sebagai alias miliknya
    MAPS["domains"].write_text("".join(f"{d} OK\n" for d in sorted(domains)))
    MAPS["mailboxes"].write_text("\n".join(boxes) + "\n")
    MAPS["aliases"].write_text("\n".join(aliases) + "\n")
    MAPS["senders"].write_text("\n".join(senders) + "\n")
    relay = mail.get("relay") or {}
    if relay.get("user") and relay.get("password"):
        MAPS["relay-sasl"].write_text(f"{BREVO_RELAY} {relay['user']}:{relay['password']}\n")
        run(["postconf", "-e", f"relayhost={BREVO_RELAY}"])
    else:
        MAPS["relay-sasl"].write_text("")
        run(["postconf", "-e", "relayhost="])
    MAPS["relay-sasl"].chmod(0o600)
    tmp = USERS.with_suffix(".tmp")
    tmp.write_text("\n".join(users) + "\n")
    shutil.chown(tmp, "root", "dovecot")
    tmp.chmod(0o640)
    tmp.replace(USERS)  # Dovecot membaca ulang passwd-file otomatis
    run(["postfix", "reload"])


def recipients(mail):
    """Semua alamat yang boleh menerima email (kotak surat + forwarder + catch-all)."""
    out = set()
    for d, dm in mail.get("domains", {}).items():
        out |= {f"{l}@{d}" for l in dm.get("mailboxes", {})}
        out |= {f"{l}@{d}" if l != "*" else f"@{d}" for l in dm.get("forwards", {})}
    return out


def accepts(mail, rcpt):
    rcpt = rcpt.lower()
    r = recipients(mail)
    return rcpt in r or "@" + rcpt.rsplit("@", 1)[-1] in r


def deliver(raw, sender, rcpt):
    """Serahkan email mentah ke Postfix (sendmail). Kembalikan (ok, pesan)."""
    p = subprocess.run(["/usr/sbin/sendmail", "-G", "-i", "-f", sender or "<>", "--", rcpt], input=raw,
                       capture_output=True, timeout=60)
    return p.returncode == 0, p.stderr.decode(errors="replace").strip()


def verify(secret, ts, sender, rcpt, body, sig):
    """Tanda tangan dari Email Worker: HMAC-SHA256(secret, ts\\nfrom\\nto\\n + body), hex."""
    try:
        if abs(time.time() - int(ts) / 1000) > 600:
            return False
    except (TypeError, ValueError):
        return False
    msg = f"{ts}\n{sender}\n{rcpt}\n".encode() + body
    return hmac.compare_digest(hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest(), sig or "")


def maildir(domain, local=None):
    return VMAIL / domain / local if local else VMAIL / domain


def usage(domain, local):
    d = maildir(domain, local)
    if not d.exists():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def remove_mailbox(domain, local):
    shutil.rmtree(maildir(domain, local), ignore_errors=True)


def queue():
    rc, out = run(["postqueue", "-p"])
    return out if rc == 0 else ""


def flush():
    return run(["postqueue", "-f"])


def log(domain=None, lines=60):
    rc, out = run(["journalctl", "-u", "postfix@-", "-u", "postfix", "-u", "dovecot", "-n", "400", "--no-pager",
                   "-o", "short-iso"])
    rows = out.splitlines()
    if domain:
        rows = [l for l in rows if "@" + domain in l or "relay=" in l and domain in l]
    return "\n".join(rows[-lines:])


def send_test(sender, to):
    body = (f"From: {sender}\nTo: {to}\nSubject: Tes email LocalPanel\n"
            f"Date: {time.strftime('%a, %d %b %Y %H:%M:%S %z')}\nMessage-ID: <{os.urandom(8).hex()}@{sender.split('@')[1]}>\n\n"
            "Email ini dikirim dari LocalPanel untuk menguji pengiriman.\n").encode()
    return deliver(body, sender, to)
