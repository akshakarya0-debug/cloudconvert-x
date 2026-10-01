"""CloudConvert-X - API Gateway (Python / FastAPI).

Tugas: terima unggahan, simpan ke MinIO, buat job di Redis, dan laporkan status.
API TIDAK melakukan konversi; itu tugas worker (Python, Node.js, Go).
"""
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import boto3
import httpx
import redis
from botocore.client import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [api] %(message)s")
log = logging.getLogger("api")

FORMATS = json.loads((Path(__file__).parent / "formats.json").read_text())["categories"]
BUCKET = os.environ.get("S3_BUCKET", "ccx")
GOTENBERG_URL = os.environ.get("GOTENBERG_URL", "http://gotenberg:3000")
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "200")) * 1024 * 1024
JOB_TTL = 24 * 3600

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

app = FastAPI(title="CloudConvert-X API", version="0.1.0")


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
def formats():
    return {"categories": FORMATS}


@app.get("/api/health")
def health():
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
    try:
        for key in r.scan_iter("worker:*"):
            raw = r.get(key)
            if raw:
                workers.append(json.loads(raw))
        queues = {q: r.llen(f"queue:{q}") for q in ("doc", "image", "media")}
    except Exception:  # noqa: BLE001
        pass
    return {"api": {"language": "Python", "status": "ok"},
            "services": services, "workers": workers, "queue_length": queues}


@app.post("/api/jobs", status_code=202)
def create_job(file: UploadFile = File(...), target: str = Form(...)):
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
        "created_at": time.time(),
    })
    r.expire(f"job:{job_id}", JOB_TTL)
    r.lpush("jobs:recent", job_id)
    r.ltrim("jobs:recent", 0, 49)

    # ---- KONTRAK PESAN (dibaca worker Python, Node.js, dan Go) ----
    message = {"job_id": job_id, "category": category, "input_key": input_key,
               "filename": safe, "target": target}
    queue = FORMATS[category]["queue"]
    r.lpush(f"queue:{queue}", json.dumps(message))
    log.info("job %s (%s -> %s) masuk queue:%s", job_id, ext, target, queue)
    return {"job_id": job_id, "status": "queued", "queue": queue}


@app.get("/api/jobs")
def list_jobs():
    out = []
    for job_id in r.lrange("jobs:recent", 0, 19):
        h = r.hgetall(f"job:{job_id}")
        if h:
            out.append(public_job(job_id, h))
    return out


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    h = r.hgetall(f"job:{job_id}")
    if not h:
        raise HTTPException(404, "Job tidak ditemukan / sudah kedaluwarsa")
    return public_job(job_id, h)


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    h = r.hgetall(f"job:{job_id}")
    if not h:
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
