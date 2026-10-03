// CloudConvert-X - Worker Gambar (Node.js + Sharp)
// Mengambil job dari Redis `queue:image`, memprosesnya dengan Sharp (libvips, C++).
const os = require('os');
const Redis = require('ioredis');
const sharp = require('sharp');
const { S3Client, GetObjectCommand, PutObjectCommand } = require('@aws-sdk/client-s3');

const QUEUE = 'queue:image';
const PROCESSING = 'processing:image'; // pesan yang sedang dikerjakan (tahan-gagal)
const LEASE_TTL = 30; // detik; diperbarui heartbeat selama job berjalan
const BUCKET = process.env.S3_BUCKET || 'ccx';
const HOST = os.hostname();
const MAX_WIDTH = parseInt(process.env.IMAGE_MAX_WIDTH || '2560', 10);
const QUALITY = parseInt(process.env.IMAGE_QUALITY || '80', 10);
const [redisHost, redisPort] = (process.env.REDIS_ADDR || 'redis:6379').split(':');

// Dua koneksi: satu khusus BRPOP (memblokir), satu untuk perintah lain.
const blocking = new Redis({ host: redisHost, port: +redisPort });
const cmd = new Redis({ host: redisHost, port: +redisPort });

const s3 = new S3Client({
  endpoint: process.env.S3_ENDPOINT || 'http://minio:9000',
  region: 'us-east-1',
  forcePathStyle: true,
  requestChecksumCalculation: 'WHEN_REQUIRED',
  responseChecksumValidation: 'WHEN_REQUIRED',
  credentials: {
    accessKeyId: process.env.S3_ACCESS_KEY,
    secretAccessKey: process.env.S3_SECRET_KEY,
  },
});

const SHARP_FORMAT = { jpg: 'jpeg', jpeg: 'jpeg', png: 'png', webp: 'webp', avif: 'avif', tiff: 'tiff', tif: 'tiff', gif: 'gif' };
const MIME = { jpg: 'image/jpeg', png: 'image/png', webp: 'image/webp', avif: 'image/avif', tiff: 'image/tiff', gif: 'image/gif' };

let running = true;
let currentJob = null;
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => { running = false; });

const log = (...a) => console.log(new Date().toISOString(), '[worker-image]', ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function heartbeat() {
  const info = { service: 'worker-image', language: 'Node.js', queue: QUEUE, host: HOST, ts: Date.now() / 1000 };
  try {
    await cmd.set(`worker:image:${HOST}`, JSON.stringify(info), 'EX', 15);
    if (currentJob) await cmd.set(`lease:${currentJob}`, HOST, 'EX', LEASE_TTL); // perpanjang lease
  } catch (_) { /* abaikan */ }
}

const update = (id, fields) => cmd.hset(`job:${id}`, fields);

async function convert(job) {
  const obj = await s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: job.input_key }));
  const input = Buffer.from(await obj.Body.transformToByteArray());
  await update(job.job_id, { progress: 30 });

  const target = job.target.toLowerCase();
  const fmt = SHARP_FORMAT[target];
  if (!fmt) throw new Error(`format tujuan .${target} tidak didukung`);

  const output = await sharp(input, { animated: target === 'webp' || target === 'gif' })
    .rotate()
    .resize({ width: MAX_WIDTH, withoutEnlargement: true })
    .toFormat(fmt, { quality: QUALITY })
    .toBuffer();
  await update(job.job_id, { progress: 85 });

  const stem = job.filename.replace(/\.[^.]+$/, '');
  const outKey = `outputs/${job.job_id}/${stem}.${target}`;
  await s3.send(new PutObjectCommand({ Bucket: BUCKET, Key: outKey, Body: output, ContentType: MIME[target] }));
  return { outKey, outName: `${stem}.${target}` };
}

async function handle(job) {
  const id = job.job_id;
  log('mulai job', id, job.filename);
  await update(id, { status: 'processing', progress: 5, worker: `worker-image (Node.js) @${HOST}` });
  try {
    const { outKey, outName } = await convert(job);
    await update(id, { status: 'done', progress: 100, output_key: outKey, output_name: outName, finished_at: Date.now() / 1000 });
    log('selesai job', id);
  } catch (e) {
    console.error(e);
    await update(id, { status: 'failed', error: String(e.message || e).slice(0, 300) });
  }
}

async function main() {
  heartbeat();
  const timer = setInterval(heartbeat, 5000);
  log('siap, menunggu job di', QUEUE);
  while (running) {
    let raw;
    try {
      // atomik: ambil dari antrean DAN catat di processing; pesan tidak hilang
      raw = await blocking.brpoplpush(QUEUE, PROCESSING, 5);
    } catch (e) {
      console.warn('redis error:', e.message);
      await sleep(2000);
      continue;
    }
    if (!raw) continue;
    let job;
    try {
      job = JSON.parse(raw);
      if (!job.job_id) throw new Error('job_id kosong');
    } catch (e) {
      console.warn('pesan tidak valid, dibuang:', raw.slice(0, 100));
      await cmd.lrem(PROCESSING, 1, raw).catch(() => {});
      continue;
    }
    currentJob = job.job_id;
    try {
      await cmd.set(`lease:${job.job_id}`, HOST, 'EX', LEASE_TTL);
      await handle(job);
      await cmd.lrem(PROCESSING, 1, raw); // selesai: hapus dari processing
      await cmd.del(`lease:${job.job_id}`);
    } catch (e) {
      // pesan tetap di processing; API akan memulihkannya
      console.warn('redis error saat job', job.job_id, e.message);
      await sleep(2000);
    } finally {
      currentJob = null;
    }
  }
  clearInterval(timer);
  log('berhenti dengan rapi');
  process.exit(0);
}

main();
