# Kebutuhan (Requirements)

## 1. Yang wajib di laptop

| Kebutuhan | Versi | Cek |
|---|---|---|
| Docker Desktop (Windows/Mac) atau Docker Engine (Linux) | Compose v2 (`docker compose`) | `docker compose version` |
| Git | apa saja | `git --version` |
| Koneksi internet | hanya saat build pertama (pull image, unduh dependensi) | - |
| RAM | minimal 8 GB di laptop, **alokasikan 4 GB ke Docker** | Docker Desktop → Settings → Resources |
| Disk kosong | sekitar 6 GB (image Gotenberg + LibreOffice cukup besar) | - |
| Port bebas | 8080, 8000, 9001 | `ss -tlnp` / `netstat -ano` |

Windows: aktifkan **WSL2** dan pilih backend WSL2 di Docker Desktop.

**Tidak perlu** meng-install Python, Node.js, Go, atau FFmpeg di laptop. Semuanya ada di dalam kontainer.

## 2. Yang opsional (untuk mengembangkan kode)

| Alat | Untuk apa |
|---|---|
| VS Code + ekstensi Docker | mengelola kontainer dan melihat log |
| Python 3.12 | menjalankan/cek `services/api` tanpa Docker |
| Node.js 20 | mengedit `worker-image` |
| Go 1.22+ | mengedit `worker-media` |
| curl | menjalankan `tests/smoke_test.sh` (bash; di Windows pakai Git Bash atau WSL) |

## 3. Dependensi per layanan (otomatis terpasang saat `docker compose build`)

| Layanan | Bahasa | Berkas dependensi | Pustaka utama |
|---|---|---|---|
| `api` | Python 3.12 | `services/api/requirements.txt` | FastAPI, uvicorn, redis-py, boto3, httpx |
| `worker-doc` | Python 3.12 | `services/worker-doc/requirements.txt` | redis-py, boto3, httpx |
| `worker-image` | Node.js 20 | `services/worker-image/package.json` | sharp, ioredis, @aws-sdk/client-s3 |
| `worker-media` | Go 1.23 (build), Alpine + FFmpeg | `services/worker-media/go.mod` | go-redis v9, minio-go v7, ffmpeg (apk) |
| `frontend` | HTML/JS + Nginx | - | tanpa framework |

## 4. Image pihak ketiga (tanpa build, langsung pull)

| Image | Bahasa asal | Fungsi |
|---|---|---|
| `redis:7-alpine` | C | antrean job + status + heartbeat |
| `minio/minio` (versi ditandai) | Go | penyimpanan file (kompatibel S3) |
| `gotenberg/gotenberg:8` | Go | Office/HTML/Markdown → PDF (Chromium + LibreOffice) |
