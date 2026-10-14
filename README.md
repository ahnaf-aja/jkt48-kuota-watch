# jkt48-kuota-watch

Watcher kuota **JKT48 Exclusive (EX5B99)** yang jalan di **GitHub Actions** — server GitHub,
jadi laptop kamu **tidak perlu nyala** untuk dapat notifikasi.

## Cara kerjanya

- GitHub memanggil workflow `.github/workflows/watch.yml` setiap 5 menit (`cron: */5 * * * *`)
  dan di dalamnya `watch.py` disiklus 4x dengan jeda 60 detik → cek ±1 menit sekali, tanpa laptop.
- `watch.py` hit endpoint publik `https://jkt48.com/api/v1/exclusives/<KODE>/bonus?lang=id`
  (butuh header browser; tanpa itu Cloudflare balas 403).
- Kalau ada perubahan apa pun — kuota naik/turun, sesi baru, jalur baru/hilang, member ganti,
  harga berubah — pesan dikirim ke Discord lewat secret `DISCORD_WEBHOOK_URL`.
- Snapshot terakhir disimpan di `state.json` dan di-commit otomatis oleh workflow, jadi
  tidak ada alert dobel. Kalau pengiriman gagal, state **tidak** dimajukan → dicoba lagi nanti.

## Yang tidak dikabari

Member di `config.json` → `ignore_members` (default: `Desy Natalia`). Perubahannya tetap
disimpan di state, cuma tidak dikirim.

## Nambah event lain

Edit `config.json` (bisa langsung dari web GitHub):

```json
"events": [
  { "code": "EX5B99", "label": "JKT48 Exclusive EX5B99" },
  { "code": "KODE_LAIN", "label": "Nama buat di notif" }
]
```

## Test manual

1. Buat file `test_alert.txt` isi apa saja → commit/push.
2. Tab **Actions** → `jkt48-quota-watch` → **Run workflow**.
3. Pesannya masuk ke Discord, lalu file `test_alert.txt` dihapus otomatis.

## Ubah channel Discord

Webhook-nya dibuat di channel Discord tujuan (Channel → Integrations → Webhooks). URL-nya
disimpan sebagai secret repo bernama `DISCORD_WEBHOOK_URL` (Settings → Secrets and variables →
Actions). Untuk pindah channel: bikin webhook baru di channel itu, lalu ganti secret-nya.

## Versi lokal (cadangan)

Masih ada watcher versi lokal di `D:\notif kuota` (cron Hermes tiap 2 menit, kirim ke Discord
yang sama). Kalau dua-duanya nyala bareng saat laptop kamu hidup, notifikasinya bisa dobel —
jadi kalau versi cloud ini jalan normal, biarkan job lokal di-pause (`hermes cron pause <id>`
atau lewat Hermes).
