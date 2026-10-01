#!/usr/bin/env bash
# Uji end-to-end: setiap kategori dikerjakan bahasa berbeda.
#   html/md/txt -> PDF   (Python  -> Gotenberg/Go)
#   png -> webp          (Node.js -> Sharp)
#   mp3 -> wav           (Go      -> FFmpeg/C)
# Jalankan dari root proyek setelah `docker compose up -d`:  bash tests/smoke_test.sh
set -u
API="${API:-http://localhost:8000}"
TMP="tests/tmp"; mkdir -p "$TMP"
pass=0; fail=0

convert() {  # $1=file $2=target
  local resp id status i
  resp=$(curl -s -F "file=@$1" -F "target=$2" "$API/api/jobs")
  id=$(echo "$resp" | sed -n 's/.*"job_id":"\([^"]*\)".*/\1/p')
  if [ -z "$id" ]; then echo "FAIL  $1 -> $2 : $resp"; fail=$((fail+1)); return; fi
  for i in $(seq 1 90); do
    status=$(curl -s "$API/api/jobs/$id" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')
    [ "$status" = "done" ] || [ "$status" = "failed" ] && break
    sleep 1
  done
  if [ "$status" = "done" ]; then
    curl -s -o "$TMP/out_$(basename "$1").$2" "$API/api/jobs/$id/download"
    echo "PASS  $1 -> $2  ($(wc -c < "$TMP/out_$(basename "$1").$2") bytes) $(curl -s "$API/api/jobs/$id" | sed -n 's/.*"worker":"\([^"]*\)".*/\1/p')"
    pass=$((pass+1))
  else
    echo "FAIL  $1 -> $2 : status=$status $(curl -s "$API/api/jobs/$id")"; fail=$((fail+1))
  fi
}

echo "== Health =="; curl -s "$API/api/health"; echo; echo

printf '<h1>Halo</h1><p>Uji HTML ke PDF</p>' > "$TMP/sample.html"
printf '# Judul\n\nUji **Markdown** ke PDF\n' > "$TMP/sample.md"
printf 'Uji teks biasa ke PDF\n' > "$TMP/sample.txt"
# PNG 1x1 valid
echo 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==' | base64 -d > "$TMP/sample.png"

echo "== Konversi =="
convert "$TMP/sample.html" pdf
convert "$TMP/sample.md" pdf
convert "$TMP/sample.txt" pdf
convert "$TMP/sample.png" webp

# Buat mp3 2 detik memakai FFmpeg di dalam kontainer worker-media
if docker compose exec -T worker-media ffmpeg -loglevel error -f lavfi -i "sine=frequency=440:duration=2" -f mp3 pipe:1 > "$TMP/tone.mp3" 2>/dev/null && [ -s "$TMP/tone.mp3" ]; then
  convert "$TMP/tone.mp3" wav
else
  echo "SKIP  media (tidak bisa membuat sampel mp3)"
fi

echo; echo "Lulus: $pass  Gagal: $fail"
[ "$fail" -eq 0 ]
