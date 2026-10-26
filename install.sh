#!/usr/bin/env bash
# =============================================================================
#  LocalPanel installer — panel hosting ala cPanel (Nginx + PHP-FPM + MariaDB)
#
#  Pemakaian (sebagai root, di Ubuntu/Debian):
#    git clone https://github.com/DzikyR/cpanel.git /opt/localpanel
#    cd /opt/localpanel && bash install.sh [--tunnel-token TOKEN] [--port 2083] [--no-node]
#
#  --tunnel-token TOKEN  pasang Cloudflare Tunnel (token dari Zero Trust → Networks → Tunnels)
#  --port PORT           port panel (bawaan 2083)
#  --no-node             lewati Node.js sistem (untuk npm build Laravel/Vite & aplikasi Node.js;
#                        versi lain tetap bisa diunduh dari menu Node.js)
#
#  Aman dijalankan ulang: pengaturan & password yang sudah ada tidak ditimpa.
# =============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TUNNEL_TOKEN=""; PORT=2083; NODE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --tunnel-token) TUNNEL_TOKEN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-node) NODE=0; shift ;;
    -h|--help) sed -n 2,15p "$0"; exit 0 ;;
    *) echo "Opsi tidak dikenal: $1"; exit 1 ;;
  esac
done

say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Jalankan sebagai root (sudo bash install.sh)"; exit 1; }
command -v apt-get >/dev/null || { echo "Installer ini untuk Ubuntu/Debian (apt)."; exit 1; }
export DEBIAN_FRONTEND=noninteractive
# Postfix dikonfigurasi oleh panel (menu Email), bukan oleh wizard paket
echo "postfix postfix/main_mailer_type select No configuration" | debconf-set-selections

say "Paket dasar: nginx, MariaDB, PostgreSQL, PHP-FPM, Python, Composer"
apt-get update -q
apt-get install -y -q nginx mariadb-server php-fpm php-cli php-mysql php-curl php-gd php-intl php-mbstring \
  php-xml php-zip php-bcmath php-sqlite3 python3 python3-flask python3-psutil python3-pymysql \
  composer git curl unzip zip sudo cron ca-certificates gnupg postgresql php-pgsql \
  postfix dovecot-imapd dovecot-lmtpd openssl
PHP_VER="$(php -r 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;')"
echo "PHP $PHP_VER"

if [ "$NODE" = 1 ] && ! command -v node >/dev/null; then
  say "Node.js 24 (NodeSource)"
  curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
  apt-get install -y -q nodejs
fi

if ! command -v cloudflared >/dev/null; then
  say "cloudflared (Cloudflare Tunnel)"
  mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    > /etc/apt/sources.list.d/cloudflared.list
  apt-get update -q && apt-get install -y -q cloudflared
fi
if [ -n "$TUNNEL_TOKEN" ]; then
  say "Menghubungkan Cloudflare Tunnel"
  mkdir -p /etc/cloudflared
  printf '%s' "$TUNNEL_TOKEN" > /etc/cloudflared/token
  chmod 600 /etc/cloudflared/token
  cp "$DIR/deploy/cloudflared.service" /etc/systemd/system/cloudflared.service
  systemctl daemon-reload
  systemctl enable --now cloudflared
  systemctl restart cloudflared
fi

say "Konfigurasi PHP (batas upload 512 MB, max_input_vars 5000)"
cp "$DIR/deploy/php-localpanel.ini" "/etc/php/$PHP_VER/fpm/conf.d/99-localpanel.ini"
sed 's/^memory_limit = .*/memory_limit = -1/' "$DIR/deploy/php-localpanel.ini" \
  > "/etc/php/$PHP_VER/cli/conf.d/99-localpanel.ini"
[ -e /run/php/php-fpm.sock ] || update-alternatives --auto php-fpm.sock 2>/dev/null || true

say "Konfigurasi Nginx"
cp "$DIR/deploy/nginx-cloudflare-tunnel.conf" /etc/nginx/conf.d/cloudflare-tunnel.conf
cp "$DIR/deploy/nginx-localpanel-node.conf" /etc/nginx/conf.d/localpanel-node.conf
mkdir -p /var/www
nginx -t

say "Layanan systemd"
sed "s|__DIR__|$DIR|g" "$DIR/deploy/localpanel.service" > /etc/systemd/system/localpanel.service
cp "$DIR/deploy/localpanel-queue@.service" /etc/systemd/system/
mkdir -p "$DIR/keys" "$DIR/deploy-logs" && chmod 700 "$DIR/keys"

ADMIN_PASS=""
if [ ! -f "$DIR/config.json" ]; then
  say "Membuat akun admin panel"
  ADMIN_PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  ADMIN_PASS="$ADMIN_PASS" PORT="$PORT" python3 - "$DIR/config.json" <<'PY'
import json, os, secrets, sys
from werkzeug.security import generate_password_hash
cfg = {"username": "admin", "password_hash": generate_password_hash(os.environ["ADMIN_PASS"]),
       "secret_key": secrets.token_hex(32), "port": int(os.environ["PORT"])}
with open(sys.argv[1], "w") as f:
    json.dump(cfg, f, indent=2)
os.chmod(sys.argv[1], 0o600)
PY
fi

systemctl daemon-reload
systemctl enable --now mariadb postgresql nginx "php$PHP_VER-fpm" cron
systemctl reload nginx "php$PHP_VER-fpm"
systemctl enable localpanel
systemctl restart localpanel
sleep 2
systemctl is-active --quiet localpanel || { journalctl -u localpanel -n 30 --no-pager; exit 1; }

IP="$(hostname -I | awk '{print $1}')"
PORT_NOW="$(python3 -c "import json;print(json.load(open('$DIR/config.json')).get('port', 2083))")"
say "LocalPanel siap"
echo "  Alamat   : http://$IP:$PORT_NOW"
if [ -n "$ADMIN_PASS" ]; then
  echo "  Username : admin"
  echo "  Password : $ADMIN_PASS   (segera ganti di menu Ganti Password)"
else
  echo "  Login    : akun yang sudah ada (config.json tidak diubah)"
fi
[ -n "$TUNNEL_TOKEN" ] || echo "  Cloudflare: jalankan ulang dengan --tunnel-token TOKEN untuk mempublikasikan website."
echo "  Lalu buka menu Cloudflare di panel dan masukkan API token agar domain muncul otomatis."
