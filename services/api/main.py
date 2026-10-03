"""CloudConvert-X - API Gateway (Python / FastAPI).

Tugas: terima unggahan, simpan ke MinIO, buat job di Redis, dan laporkan status.
API TIDAK melakukan konversi; itu tugas worker (Python, Node.js, Go).
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import boto3
import httpx
import redis
from botocore.client import Config
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import auth

logging.basicConfig(level=logging.INFO, format="%(asctime)s [api] %(message)s")
log = logging.getLogger("api")

FORMATS = json.loads((Path(__file__).parent / "formats.json").read_text())["categories"]
BUCKET = os.environ.get("S3_BUCKET", "ccx")
GOTENBERG_URL = os.environ.get("GOTENBERG_URL", "http://gotenberg:3000")
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "200")) * 1024 * 1024
JOB_TTL = 24 * 3600

# ---- Antrean tahan-gagal ----
# Worker memindahkan pesan dari `queue:<x>` ke `processing:<x>` (atomik) lalu
# memperbarui `lease:<job_id>` (TTL 30 dtk) selama bekerja. Pemulih di bawah
# mengembalikan job yang pesannya tertinggal di `processing:<x>` tanpa lease.
QUEUES = ("doc", "image", "media")
LEASE_GRACE = 20      # detik tanpa lease sebelum job dianggap yatim
MAX_ATTEMPTS = 3      # batas percobaan ulang
RECOVER_EVERY = 10    # detik antar-pemeriksaan
_orphan_since: dict = {}

host, port = os.environ.get("REDIS_ADDR", "redis:6379").split(":")
r = redis.Redis(host=host, port=int(port), decode_responses=True)

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio:9000"),
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name="us-east-1",
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    ),
)

app = FastAPI(title="CloudConvert-X API", version="0.2.0")


# ------------------------------------------------------------------ autentikasi
def _hostname(value: str) -> str:
    try:
        return (urlparse(value if "//" in value else "//" + value).hostname or "").lower()
    except ValueError:
        return ""


def _session_user(request: Request):
    return auth.get_session(r, request.cookies.get(auth.COOKIE))


@app.middleware("http")
async def gerbang(request: Request, call_next):
    """Penolakan dini, SEBELUM isi permintaan dibaca:
    1) permintaan ubah-data dari situs lain (Origin tidak cocok dengan Host),
    2) unggahan tanpa sesi (agar orang asing tidak bisa menghabiskan bandwidth/disk)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and _hostname(origin) != _hostname(request.headers.get("host", "")):
            return JSONResponse({"detail": "Asal permintaan ditolak"}, status_code=403)
        if request.url.path == "/api/jobs" and not await run_in_threadpool(_session_user, request):
            return JSONResponse({"detail": "Belum masuk"}, status_code=401)
    return await call_next(request)


def current_user(request: Request) -> dict:
    user = _session_user(request)
    if not user:
        raise HTTPException(401, "Belum masuk")
    return user


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=auth.MAX_PASSWORD)


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return (proto or request.url.scheme) == "https"


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    email = auth.norm_email(body.email)
    wait = auth.throttle_wait(r, email, ip)
    if wait:
        raise HTTPException(429, f"Terlalu banyak percobaan. Coba lagi dalam {-(-wait // 60)} menit.",
                            headers={"Retry-After": str(wait)})
    user = auth.authenticate(email, body.password)
    if not user:
        auth.throttle_fail(r, email, ip)
        log.info("login gagal: %s dari %s", email, ip)
        raise HTTPException(401, "Email atau kata sandi salah")
    auth.throttle_clear(r, email)
    token = auth.create_session(r, user)
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_TTL, httponly=True,
                        samesite="lax", secure=_is_https(request), path="/")
    log.info("login: %s", email)
    return {"user": {"email": user["email"], "name": user["name"]}}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.destroy_session(r, request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    return {"user": {"email": user["email"], "name": user["name"]}}


@app.on_event("startup")
def init_auth():
    auth.init_db()


@app.on_event("startup")
def start_recovery():
    threading.Thread(target=recovery_loop, daemon=True).start()


@app.on_event("startup")
def ensure_bucket():
    for _ in range(30):
        try:
            try:
                s3.head_bucket(Bucket=BUCKET)
            except ClientError:
                s3.create_bucket(Bucket=BUCKET)
                log.info("bucket '%s' dibuat", BUCKET)
            try:  # file otomatis terhapus setelah 1 hari (privasi + hemat disk)
                s3.put_bucket_lifecycle_configuration(
                    Bucket=BUCKET,
                    LifecycleConfiguration={"Rules": [{
                        "ID": "expire-1d", "Status": "Enabled",
                        "Filter": {"Prefix": ""}, "Expiration": {"Days": 1},
                    }]},
                )
            except Exception as e:  # noqa: BLE001
                log.warning("lifecycle tidak terpasang: %s", e)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("menunggu MinIO (%s) ...", e)
            time.sleep(2)
    raise RuntimeError("MinIO tidak bisa dihubungi")


def recover_orphans():
    """Kembalikan ke antrean job yang worker-nya mati di tengah proses."""
    now = time.time()
    for q in QUEUES:
        pkey = f"processing:{q}"
        for raw in r.lrange(pkey, 0, -1):
            try:
                jid = json.loads(raw)["job_id"]
            except Exception:  # noqa: BLE001 - pesan rusak: buang
                r.lrem(pkey, 1, raw)
                continue
            job_key = f"job:{jid}"
            h = r.hgetall(job_key)
            if not h or h.get("status") in ("done", "failed"):
                # kedaluwarsa, atau selesai tetapi worker mati sebelum membersihkan
                r.lrem(pkey, 1, raw)
                r.delete(f"lease:{jid}")
                _orphan_since.pop(jid, None)
                continue
            if r.exists(f"lease:{jid}"):
                _orphan_since.pop(jid, None)
                continue
            first = _orphan_since.setdefault(jid, now)
            if now - first < LEASE_GRACE:
                continue
            _orphan_since.pop(jid, None)
            attempts = int(h.get("attempts", 0)) + 1
            if attempts > MAX_ATTEMPTS:
                r.hset(job_key, mapping={
                    "status": "failed",
                    "error": f"Worker berhenti saat memproses (sudah dicoba {MAX_ATTEMPTS} kali)",
                })
                r.lrem(pkey, 1, raw)
                log.warning("job %s gagal permanen setelah %d percobaan", jid, MAX_ATTEMPTS)
            else:
                pipe = r.pipeline()
                pipe.lrem(pkey, 1, raw)
                pipe.rpush(f"queue:{q}", raw)  # RPUSH = didahulukan (worker ambil dari ekor)
                pipe.hset(job_key, mapping={"status": "queued", "progress": 0,
                                            "attempts": attempts, "error": ""})
                pipe.execute()
                log.warning("job %s dikembalikan ke queue:%s (percobaan %d)", jid, q, attempts)


def recovery_loop():
    while True:
        try:
            recover_orphans()
        except Exception as e:  # noqa: BLE001
            log.warning("pemulih error: %s", e)
        time.sleep(RECOVER_EVERY)


def detect_category(ext: str):
    for cat, spec in FORMATS.items():
        if ext in spec["input"]:
            return cat
    return None


def public_job(job_id: str, h: dict) -> dict:
    return {
        "job_id": job_id,
        "status": h.get("status"),
        "progress": int(float(h.get("progress", 0))),
        "category": h.get("category"),
        "filename": h.get("filename"),
        "target": h.get("target"),
        "output_name": h.get("output_name"),
        "error": h.get("error") or None,
        "worker": h.get("worker") or None,
        "created_at": float(h.get("created_at", 0)),
    }


@app.get("/api/ping")
def ping():
    return {"ok": True}


@app.get("/api/formats")
def formats(user: dict = Depends(current_user)):
    return {"categories": FORMATS}


@app.get("/api/health")
def health(user: dict = Depends(current_user)):
    """Cek semua layanan lintas-bahasa + worker yang aktif (heartbeat Redis)."""
    services = {}
    try:
        r.ping()
        services["redis"] = "ok"
    except Exception as e:  # noqa: BLE001
        services["redis"] = f"error: {e}"
    try:
        s3.head_bucket(Bucket=BUCKET)
        services["minio"] = "ok"
    except Exception as e:  # noqa: BLE001
        services["minio"] = f"error: {e}"
    try:
        resp = httpx.get(f"{GOTENBERG_URL}/health", timeout=3)
        services["gotenberg"] = "ok" if resp.status_code == 200 else f"http {resp.status_code}"
    except Exception as e:  # noqa: BLE001
        services["gotenberg"] = f"error: {e}"

    workers = []
    queues = {}
    processing = {}
    try:
        for key in r.scan_iter("worker:*"):
            raw = r.get(key)
            if raw:
                workers.append(json.loads(raw))
        queues = {q: r.llen(f"queue:{q}") for q in QUEUES}
        processing = {q: r.llen(f"processing:{q}") for q in QUEUES}
    except Exception:  # noqa: BLE001
        pass
    return {"api": {"language": "Python", "status": "ok"},
            "services": services, "workers": workers, "queue_length": queues,
            "processing": processing}


@app.post("/api/jobs", status_code=202)
def create_job(file: UploadFile = File(...), target: str = Form(...),
               user: dict = Depends(current_user)):
    name = Path(file.filename or "file").name
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    target = target.lower().lstrip(".")

    category = detect_category(ext)
    if not category:
        raise HTTPException(415, f"Format sumber '.{ext}' belum didukung")
    if target not in FORMATS[category]["output"]:
        raise HTTPException(400, f"'{category}' tidak bisa dikonversi ke '{target}'")
    if ext == target:
        raise HTTPException(400, "Format sumber dan tujuan sama")

    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size == 0:
        raise HTTPException(400, "File kosong")
    if size > MAX_UPLOAD:
        raise HTTPException(413, f"File melebihi {MAX_UPLOAD // 1024 // 1024} MB")

    job_id = uuid.uuid4().hex
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    input_key = f"inputs/{job_id}/{safe}"
    s3.upload_fileobj(file.file, BUCKET, input_key)

    r.hset(f"job:{job_id}", mapping={
        "status": "queued", "progress": 0, "category": category,
        "filename": safe, "target": target, "input_key": input_key,
        "created_at": time.time(), "owner": user["id"],
    })
    r.expire(f"job:{job_id}", JOB_TTL)
    r.lpush(f"jobs:user:{user['id']}", job_id)
    r.ltrim(f"jobs:user:{user['id']}", 0, 49)

    # ---- KONTRAK PESAN (dibaca worker Python, Node.js, dan Go) ----
    message = {"job_id": job_id, "category": category, "input_key": input_key,
               "filename": safe, "target": target}
    queue = FORMATS[category]["queue"]
    r.lpush(f"queue:{queue}", json.dumps(message))
    log.info("job %s (%s -> %s) masuk queue:%s", job_id, ext, target, queue)
    return {"job_id": job_id, "status": "queued", "queue": queue}


@app.get("/api/jobs")
def list_jobs(user: dict = Depends(current_user)):
    out = []
    for job_id in r.lrange(f"jobs:user:{user['id']}", 0, 19):
        h = r.hgetall(f"job:{job_id}")
        if h:
            out.append(public_job(job_id, h))
    return out


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: dict = Depends(current_user)):
    h = r.hgetall(f"job:{job_id}")
    if not h or h.get("owner") != str(user["id"]):
        raise HTTPException(404, "Job tidak ditemukan / sudah kedaluwarsa")
    return public_job(job_id, h)


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, user: dict = Depends(current_user)):
    h = r.hgetall(f"job:{job_id}")
    if not h or h.get("owner") != str(user["id"]):
        raise HTTPException(404, "Job tidak ditemukan")
    if h.get("status") != "done":
        raise HTTPException(409, f"Job belum selesai (status: {h.get('status')})")
    obj = s3.get_object(Bucket=BUCKET, Key=h["output_key"])
    name = h.get("output_name", "hasil")
    return StreamingResponse(
        obj["Body"].iter_chunks(64 * 1024),
        media_type=obj.get("ContentType", "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
