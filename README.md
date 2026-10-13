# LocalPanel

Panel hosting sederhana ala cPanel untuk server Ubuntu/Debian: **Nginx + PHP-FPM + MariaDB**, dikelola lewat web (Flask), dengan integrasi **Cloudflare Tunnel**.

## Fitur
- **Website**: buat/hapus vhost, domain dipilih dari domain aktif di Cloudflare (subdomain + domain), publikasi otomatis lewat Cloudflare Tunnel (cek bentrok DNS sebelum dibuat).
- **Git Deploy**: tarik website dari GitHub (repo publik / privat dengan deploy key), webhook auto-deploy, database + `.env` dibuat otomatis, impor `database/*.sql` saat deploy pertama, dukungan repo berisi paket `.zip`, queue worker & scheduler Laravel.
- **Aplikasi (installer sekali klik)**: WordPress, OJS, Moodle, Joomla, Drupal, SLiMS, Laravel, phpMyAdmin — versi resmi terbaru, database & akun admin dibuat otomatis.
- **Backup & Restore**: satu berkas `.tar.gz` per website (file + database + pengaturan panel), jadwal harian/mingguan, restore ke server lain atau domain lain (konfigurasi & URL disesuaikan otomatis).
- Database (MariaDB), File Manager, Services, Logs.

## Instalasi di server baru
Ubuntu 24.04/26.04 atau Debian 12+, sebagai root:

```bash
git clone https://github.com/brakekek/localpanel.git /opt/localpanel
cd /opt/localpanel
bash install.sh --tunnel-token TOKEN_TUNNEL_CLOUDFLARE
```

Token tunnel: Cloudflare **Zero Trust → Networks → Tunnels → Create tunnel** (Cloudflared), salin token dari perintah instalasinya. Tanpa `--tunnel-token`, website hanya bisa diakses di jaringan lokal (bisa ditambahkan nanti dengan menjalankan ulang installer).

Di akhir instalasi ditampilkan alamat panel (port **2083**) dan password admin acak. Setelah login:
1. **Ganti Password**.
2. Menu **Cloudflare** → masukkan API token (izin: *Zone:Read, DNS:Edit, Cloudflare Tunnel:Edit*) agar domain muncul otomatis.

Opsi lain: `--port 8443`, `--no-node`. Installer aman dijalankan ulang.

## Pindah server
1. Server lama: menu **Backup** → *Backup sekarang* → unduh berkas `.tar.gz` (atau salin dari `/var/backups/localpanel/<domain>/`).
2. Server baru: pasang LocalPanel → menu **Backup** → *Restore* → unggah berkas (lebih dari 100 MB: salin ke `/var/backups/localpanel/<domain>/` lalu pilih dari daftar).

## Update panel
```bash
cd /opt/localpanel && git pull && systemctl restart localpanel
```

## Struktur
| Berkas | Isi |
|---|---|
| `app.py` | aplikasi web (route & halaman) |
| `apps.py` | resep installer aplikasi |
| `backup.py` | backup/restore (juga dipanggil cron: `backup.py auto`) |
| `gitdeploy.py` | deploy dari Git |
| `cloudflare.py` | API Cloudflare (tunnel & DNS) |
| `deploy/` | konfigurasi server yang dipasang `install.sh` |

`config.json` (password admin, token Cloudflare, kredensial database) dan `keys/` dibuat per server dan **tidak** disimpan di repo.
