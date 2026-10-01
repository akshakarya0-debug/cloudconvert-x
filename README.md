# CloudConvert-X

Konverter dokumen, gambar, audio, dan video berbasis **microservices lintas bahasa**.
Semua berjalan di laptop lewat Docker Compose. Tujuan utama proyek ini: membuktikan bahwa
layanan yang ditulis dengan bahasa berbeda bisa bekerja sama lewat protokol jaringan standar.

| Layanan | Bahasa | Tugas |
|---|---|---|
| `api` | **Python** (FastAPI) | Menerima unggahan, membuat job, melaporkan status, mengirim hasil |
| `worker-doc` | **Python** | Mengubah dokumen ke PDF dengan memanggil Gotenberg |
| `gotenberg` | **Go** | Chromium + LibreOffice untuk render PDF |
| `worker-image` | **Node.js** + Sharp (C++) | Konversi dan kompresi gambar |
| `worker-media` | **Go** + FFmpeg (C) | Konversi audio dan video dengan progres nyata |
| `redis` | **C** | Antrean job, status job, heartbeat worker |
| `minio` | **Go** | Penyimpanan file (S3) |
| `frontend` | HTML/JS + Nginx | Antarmuka web |

## Cara kerja

```
Browser ──► Nginx ──/api──► API (Python)
                              │ 1. simpan file          ──► MinIO (S3 / HTTP)
                              │ 2. catat status job     ──► Redis (hash job:<id>)
                              │ 3. kirim pesan JSON     ──► Redis (list queue:<jenis>)
                              ▼
           ┌──────────────────┼───────────────────┐
     queue:doc           queue:image          queue:media
   worker-doc (Python)  worker-image (Node)  worker-media (Go)
        │ HTTP                │ Sharp              │ proses anak
        ▼                     ▼                    ▼
   Gotenberg (Go)        libvips (C++)         FFmpeg (C)
        └──── hasil ditulis ke MinIO, status "done" ditulis ke Redis ────┘
Browser polling GET /api/jobs/<id> ──► progres ──► tombol Unduh
```

### Kenapa beda bahasa tidak jadi masalah

Tidak ada layanan yang memanggil fungsi layanan lain. Semuanya hanya memakai tiga
"bahasa umum" jaringan:

1. **HTTP + JSON**: browser ke API, worker-doc ke Gotenberg.
2. **Protokol Redis (RESP)**: antrean, status, heartbeat. Klien Redis tersedia di semua bahasa.
3. **Protokol S3 (HTTP)**: baca/tulis file di MinIO dari boto3 (Python), AWS SDK (Node.js), dan minio-go (Go).

Kontrak yang wajib dipatuhi setiap worker:

**Pesan job** (JSON, di-`LPUSH` API ke `queue:<doc|image|media>`, di-`BRPOP` worker):
```json
{"job_id":"a1b2...","category":"image","input_key":"inputs/a1b2.../foto.png","filename":"foto.png","target":"webp"}
```

**Status job** (Redis hash `job:<job_id>`, ditulis worker):

| Field | Nilai |
|---|---|
| `status` | `queued` → `processing` → `done` atau `failed` |
| `progress` | 0-100 |
| `output_key` | lokasi hasil di MinIO (`outputs/<job_id>/<nama>.<ext>`) |
| `output_name` | nama file untuk diunduh |
| `error` | pesan galat bila `failed` |
| `worker` | siapa yang mengerjakan (bahasa + hostname) |

**Heartbeat**: setiap 5 detik worker menulis `worker:<jenis>:<host>` (TTL 15 detik) berisi
nama layanan dan bahasanya. Panel "Status layanan" di UI membacanya, jadi worker yang mati
otomatis hilang dari daftar.

## Struktur

```
cloudconvert-x/
├── docker-compose.yml
├── .env.example
├── REQUIREMENTS.md
├── services/
│   ├── api/            # Python  - main.py, formats.json
│   ├── worker-doc/     # Python  - main.py
│   ├── worker-image/   # Node.js - index.js
│   ├── worker-media/   # Go      - main.go
│   └── frontend/       # HTML + Nginx
├── tests/smoke_test.sh
└── .github/workflows/ci.yml
```

## Menjalankan

Prasyarat ada di [REQUIREMENTS.md](REQUIREMENTS.md).

```bash
git clone https://github.com/USERNAME/cloudconvert-x.git
cd cloudconvert-x
cp .env.example .env          # opsional; tanpa .env dipakai nilai bawaan
docker compose up -d --build  # build pertama 5-10 menit
docker compose ps             # semua harus Up / healthy
```

| Alamat | Isi |
|---|---|
| http://localhost:8080 | Aplikasi web |
| http://localhost:8000/docs | Dokumentasi API (Swagger) |
| http://localhost:9001 | Console MinIO (login sesuai `.env`) |

### Uji lintas bahasa

```bash
bash tests/smoke_test.sh
```
Contoh keluaran yang diharapkan:
```
PASS  tests/tmp/sample.html -> pdf  (..) worker-doc (Python) @abc123
PASS  tests/tmp/sample.png  -> webp (..) worker-image (Node.js) @def456
PASS  tests/tmp/tone.mp3    -> wav  (..) worker-media (Go) @789xyz
```
Kolom terakhir membuktikan bahasa mana yang mengerjakan tiap job.

Uji manual dengan curl:
```bash
curl http://localhost:8000/api/health
curl -F "file=@laporan.docx" -F "target=pdf" http://localhost:8000/api/jobs
curl http://localhost:8000/api/jobs/<job_id>
curl -OJ http://localhost:8000/api/jobs/<job_id>/download
```

### Format yang didukung

| Jenis | Masuk | Keluar | Dikerjakan oleh |
|---|---|---|---|
| Dokumen | doc, docx, xls, xlsx, ppt, pptx, odt, ods, odp, rtf, txt, html, md | pdf | worker-doc |
| Gambar | jpg, png, webp, avif, gif, tiff | jpg, png, webp, avif, tiff, gif | worker-image |
| Audio | mp3, wav, flac, m4a, ogg, aac, opus, wma | mp3, wav, ogg, flac, m4a | worker-media |
| Video | mp4, mkv, avi, mov, webm, flv, wmv, m4v | mp4, webm, mkv + ekstrak audio | worker-media |

Daftar ini ada di satu tempat: `services/api/formats.json`.

## Endpoint API

| Metode | Path | Fungsi |
|---|---|---|
| GET | `/api/health` | Status Redis, MinIO, Gotenberg, worker aktif, panjang antrean |
| GET | `/api/formats` | Matriks format |
| POST | `/api/jobs` | Form: `file`, `target`. Balasan 202 + `job_id` |
| GET | `/api/jobs` | 20 job terakhir |
| GET | `/api/jobs/{id}` | Status dan progres |
| GET | `/api/jobs/{id}/download` | Unduh hasil (setelah `done`) |

## Perintah harian

```bash
docker compose logs -f worker-media        # log satu layanan
docker compose up -d --scale worker-image=3  # tambah worker (paralel)
docker compose restart worker-doc
docker compose down                        # matikan (data tetap)
docker compose down -v                     # matikan + hapus data
```

## Eksperimen yang layak dicoba

1. **Bunuh worker saat idle**: `docker compose stop worker-image`. Panel status menandai worker hilang dalam ±15 detik. Kirim gambar, job menunggu di antrean. Jalankan lagi (`start`), job otomatis diproses. Ini bukti antrean memisahkan layanan.
2. **Scale**: kirim banyak gambar sambil `--scale worker-image=3`, lihat kolom `worker` berganti-ganti host.
3. **Tambah bahasa baru**: worker apa saja (Rust, Java, PHP, ...) cukup: `BRPOP queue:<nama>`, baca JSON, ambil file dari S3, tulis hasil, dan `HSET job:<id> ...` sesuai kontrak di atas. Tambahkan kategori di `formats.json` dan satu blok di `docker-compose.yml`.

## Keterbatasan versi ini (sengaja dibuat sederhana)

- **Job hilang jika worker mati saat memproses.** `BRPOP` mengeluarkan pesan dari antrean; bila kontainer mati di tengah, job tetap `processing`. Perbaikan nanti: `BLMOVE` ke daftar "processing" + pemulih, atau pindah ke BullMQ/RabbitMQ/NATS JetStream.
- **Tipe file hanya dicek lewat ekstensi.** Untuk produksi tambahkan pengecekan magic bytes dan pemindaian ClamAV.
- **Tanpa autentikasi dan kuota.** Jangan buka ke internet apa adanya.
- **Status via polling 1 detik**, belum SSE.
- **Upload lewat API** (bukan presigned URL), cukup untuk laptop.
- **Status job di Redis** (kedaluwarsa 24 jam); belum ada PostgreSQL/riwayat permanen.
- File di MinIO dihapus otomatis setelah 1 hari lewat lifecycle rule.
- Dokumen yang dirender Gotenberg tidak diberi sandbox jaringan khusus; jangan konversi HTML dari sumber tak tepercaya di lingkungan bersama.
- Lisensi: FFmpeg dengan libx264 berlisensi GPL; pahami implikasinya bila dikomersialkan.

## Pemecahan masalah

| Gejala | Penyebab / solusi |
|---|---|
| `minio` gagal pull | Ganti tag di `docker-compose.yml` menjadi `minio/minio:latest` |
| Build `worker-media` gagal di `go mod tidy` | Butuh internet; cek koneksi/proxy lalu ulangi `docker compose build worker-media` |
| Konversi dokumen pertama lambat/timeout | LibreOffice baru menyala; ulangi. Naikkan RAM Docker ke 4 GB+ |
| Panel menampilkan "belum ada worker aktif" | `docker compose logs worker-doc worker-image worker-media` |
| Job `failed` dengan pesan `Gotenberg HTTP 4xx/5xx` | Cek `docker compose logs gotenberg`; biasanya file rusak atau format tidak cocok |
| Port bentrok | Ubah pemetaan port kiri di `docker-compose.yml` (mis. `"8081:80"`) |
| Windows: `smoke_test.sh` gagal | Jalankan dari Git Bash atau WSL |

## Alur GitHub

```bash
git init && git branch -M main
git add . && git commit -m "feat: poc cloudconvert-x polyglot"
git remote add origin git@github.com:USERNAME/cloudconvert-x.git
git push -u origin main
```
`.env` sudah ada di `.gitignore`. Workflow `.github/workflows/ci.yml` memvalidasi compose dan membangun semua image di setiap push.

## Peta jalan

1. Presigned upload langsung ke MinIO + unggah bertahap
2. Antrean tahan-gagal (BLMOVE / BullMQ / NATS) dan retry otomatis
3. PostgreSQL untuk pengguna dan riwayat, autentikasi, kuota
4. SSE menggantikan polling
5. Validasi magic bytes + ClamAV
6. Bungkus frontend dengan Tauri (desktop)
7. Pindah ke k3s + HA di server sendiri (Patroni, Redis Sentinel, MinIO terdistribusi)
