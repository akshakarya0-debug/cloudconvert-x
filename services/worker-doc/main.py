"""CloudConvert-X - Worker Dokumen (Python).

Mengambil job dari Redis `queue:doc`, mengunduh file dari MinIO, lalu memanggil
Gotenberg (Go) lewat HTTP untuk mengubahnya menjadi PDF.
"""
import json
import logging
import os
import signal
import socket
import threading
import time

import boto3
import httpx
import redis
from botocore.client import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker-doc] %(message)s")
log = logging.getLogger("worker-doc")

QUEUE = "queue:doc"
BUCKET = os.environ.get("S3_BUCKET", "ccx")
GOTENBERG = os.environ.get("GOTENBERG_URL", "http://gotenberg:3000")
HOST = socket.gethostname()

h, p = os.environ.get("REDIS_ADDR", "redis:6379").split(":")
r = redis.Redis(host=h, port=int(p), decode_responses=True)
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio:9000"),
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name="us-east-1",
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                  request_checksum_calculation="when_required",
                  response_checksum_validation="when_required"),
)

OFFICE = {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf", "txt"}
MD_TEMPLATE = (
    '<!doctype html><html><head><meta charset="utf-8"><style>'
    "body{font-family:sans-serif;max-width:800px;margin:2em auto;line-height:1.5}"
    "pre,code{background:#f4f4f4;padding:2px 4px}</style></head>"
    '<body>{{ toHTML "file.md" }}</body></html>'
)

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def heartbeat():
    while True:
        info = {"service": "worker-doc", "language": "Python", "queue": QUEUE,
                "host": HOST, "ts": time.time()}
        try:
            r.set(f"worker:doc:{HOST}", json.dumps(info), ex=15)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(5)


def update(job_id, **fields):
    r.hset(f"job:{job_id}", mapping={k: str(v) for k, v in fields.items()})


def convert(job):
    name = job["filename"]
    ext = name.rsplit(".", 1)[-1].lower()
    data = s3.get_object(Bucket=BUCKET, Key=job["input_key"])["Body"].read()
    update(job["job_id"], progress=30)

    if ext in OFFICE:
        url = f"{GOTENBERG}/forms/libreoffice/convert"
        files = [("files", (name, data))]
    elif ext in ("html", "htm"):
        url = f"{GOTENBERG}/forms/chromium/convert/html"
        files = [("files", ("index.html", data, "text/html"))]
    elif ext == "md":
        url = f"{GOTENBERG}/forms/chromium/convert/markdown"
        files = [("files", ("index.html", MD_TEMPLATE.encode(), "text/html")),
                 ("files", ("file.md", data, "text/markdown"))]
    else:
        raise ValueError(f"format .{ext} tidak didukung worker-doc")

    resp = httpx.post(url, files=files, timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(f"Gotenberg HTTP {resp.status_code}: {resp.text[:200]}")
    update(job["job_id"], progress=85)

    stem = name.rsplit(".", 1)[0]
    out_key = f"outputs/{job['job_id']}/{stem}.pdf"
    s3.put_object(Bucket=BUCKET, Key=out_key, Body=resp.content,
                  ContentType="application/pdf")
    return out_key, f"{stem}.pdf"


def handle(job):
    jid = job["job_id"]
    log.info("mulai job %s (%s)", jid, job["filename"])
    update(jid, status="processing", progress=5, worker=f"worker-doc (Python) @{HOST}")
    try:
        out_key, out_name = convert(job)
        update(jid, status="done", progress=100, output_key=out_key,
               output_name=out_name, finished_at=time.time())
        log.info("selesai job %s", jid)
    except Exception as e:  # noqa: BLE001
        log.exception("gagal job %s", jid)
        update(jid, status="failed", error=str(e)[:300])


def main():
    threading.Thread(target=heartbeat, daemon=True).start()
    log.info("siap, menunggu job di %s", QUEUE)
    while running:
        try:
            item = r.brpop(QUEUE, timeout=5)
        except redis.RedisError as e:
            log.warning("redis error: %s", e)
            time.sleep(2)
            continue
        if item:
            handle(json.loads(item[1]))
    log.info("berhenti dengan rapi")


if __name__ == "__main__":
    main()
