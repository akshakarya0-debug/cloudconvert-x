// CloudConvert-X - Worker Media (Go + FFmpeg)
// Mengambil job dari Redis `queue:media`, menjalankan FFmpeg (C) sebagai proses anak,
// dan melaporkan progres nyata ke Redis.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"github.com/redis/go-redis/v9"
)

const queueName = "queue:media"

type Job struct {
	JobID    string `json:"job_id"`
	Category string `json:"category"`
	InputKey string `json:"input_key"`
	Filename string `json:"filename"`
	Target   string `json:"target"`
}

var (
	bucket = env("S3_BUCKET", "ccx")
	host, _ = os.Hostname()
)

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func newMinio() *minio.Client {
	u, err := url.Parse(env("S3_ENDPOINT", "http://minio:9000"))
	if err != nil {
		log.Fatal(err)
	}
	mc, err := minio.New(u.Host, &minio.Options{
		Creds:  credentials.NewStaticV4(os.Getenv("S3_ACCESS_KEY"), os.Getenv("S3_SECRET_KEY"), ""),
		Secure: u.Scheme == "https",
	})
	if err != nil {
		log.Fatal(err)
	}
	return mc
}

func heartbeat(ctx context.Context, rdb *redis.Client) {
	for ctx.Err() == nil {
		b, _ := json.Marshal(map[string]any{
			"service": "worker-media", "language": "Go", "queue": queueName,
			"host": host, "ts": float64(time.Now().UnixNano()) / 1e9,
		})
		rdb.Set(context.Background(), "worker:media:"+host, b, 15*time.Second)
		select {
		case <-ctx.Done():
		case <-time.After(5 * time.Second):
		}
	}
}

// ffmpegArgs memilih codec sesuai format tujuan.
func ffmpegArgs(in, out, target string) ([]string, error) {
	base := []string{"-y", "-nostdin", "-loglevel", "error", "-i", in, "-progress", "pipe:1", "-nostats"}
	var codec []string
	switch target {
	case "mp3":
		codec = []string{"-vn", "-c:a", "libmp3lame", "-q:a", "2"}
	case "wav":
		codec = []string{"-vn", "-c:a", "pcm_s16le"}
	case "ogg":
		codec = []string{"-vn", "-c:a", "libvorbis", "-q:a", "5"}
	case "flac":
		codec = []string{"-vn", "-c:a", "flac"}
	case "m4a":
		codec = []string{"-vn", "-c:a", "aac", "-b:a", "192k"}
	case "mp4":
		codec = []string{"-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
			"-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"}
	case "mkv":
		codec = []string{"-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "128k"}
	case "webm":
		codec = []string{"-c:v", "libvpx-vp9", "-crf", "36", "-b:v", "0",
			"-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus"}
	default:
		return nil, fmt.Errorf("format tujuan .%s tidak didukung worker-media", target)
	}
	return append(append(base, codec...), out), nil
}

func probeDuration(ctx context.Context, path string) float64 {
	out, err := exec.CommandContext(ctx, "ffprobe", "-v", "error", "-show_entries",
		"format=duration", "-of", "default=nw=1:nk=1", path).Output()
	if err != nil {
		return 0
	}
	d, _ := strconv.ParseFloat(strings.TrimSpace(string(out)), 64)
	return d
}

func tail(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) > n {
		return s[len(s)-n:]
	}
	return s
}

func process(ctx context.Context, rdb *redis.Client, mc *minio.Client, job Job) (string, string, error) {
	key := "job:" + job.JobID
	dir, err := os.MkdirTemp("", "job-")
	if err != nil {
		return "", "", err
	}
	defer os.RemoveAll(dir)

	inPath := filepath.Join(dir, "input"+filepath.Ext(job.Filename))
	outPath := filepath.Join(dir, "output."+job.Target)
	if err := mc.FGetObject(ctx, bucket, job.InputKey, inPath, minio.GetObjectOptions{}); err != nil {
		return "", "", fmt.Errorf("unduh input: %w", err)
	}
	rdb.HSet(ctx, key, "progress", 3)

	args, err := ffmpegArgs(inPath, outPath, job.Target)
	if err != nil {
		return "", "", err
	}
	total := probeDuration(ctx, inPath)

	cmd := exec.CommandContext(ctx, "ffmpeg", args...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return "", "", err
	}
	if err := cmd.Start(); err != nil {
		return "", "", err
	}
	last := 0
	sc := bufio.NewScanner(stdout)
	for sc.Scan() {
		line := sc.Text()
		if strings.HasPrefix(line, "out_time_us=") && total > 0 {
			us, _ := strconv.ParseFloat(strings.TrimPrefix(line, "out_time_us="), 64)
			pct := int(us / 1e6 / total * 95)
			if pct > 95 {
				pct = 95
			}
			if pct > last {
				last = pct
				rdb.HSet(ctx, key, "progress", pct)
			}
		}
	}
	if err := cmd.Wait(); err != nil {
		return "", "", fmt.Errorf("ffmpeg gagal: %v: %s", err, tail(stderr.String(), 300))
	}

	stem := strings.TrimSuffix(job.Filename, filepath.Ext(job.Filename))
	outName := stem + "." + job.Target
	outKey := fmt.Sprintf("outputs/%s/%s", job.JobID, outName)
	if _, err := mc.FPutObject(ctx, bucket, outKey, outPath, minio.PutObjectOptions{}); err != nil {
		return "", "", fmt.Errorf("unggah hasil: %w", err)
	}
	return outKey, outName, nil
}

func handle(rdb *redis.Client, mc *minio.Client, job Job) {
	// Konteks terpisah: job yang sedang berjalan diselesaikan dulu meski ada SIGTERM.
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	key := "job:" + job.JobID
	log.Printf("mulai job %s (%s -> %s)", job.JobID, job.Filename, job.Target)
	rdb.HSet(ctx, key, "status", "processing", "progress", 1,
		"worker", "worker-media (Go) @"+host)

	outKey, outName, err := process(ctx, rdb, mc, job)
	if err != nil {
		log.Printf("gagal job %s: %v", job.JobID, err)
		rdb.HSet(ctx, key, "status", "failed", "error", tail(err.Error(), 300))
		return
	}
	rdb.HSet(ctx, key, "status", "done", "progress", 100,
		"output_key", outKey, "output_name", outName,
		"finished_at", float64(time.Now().UnixNano())/1e9)
	log.Printf("selesai job %s", job.JobID)
}

func main() {
	log.SetFlags(log.LstdFlags)
	log.SetPrefix("[worker-media] ")

	rdb := redis.NewClient(&redis.Options{Addr: env("REDIS_ADDR", "redis:6379")})
	mc := newMinio()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	go heartbeat(ctx, rdb)

	log.Printf("siap, menunggu job di %s", queueName)
	for ctx.Err() == nil {
		res, err := rdb.BRPop(ctx, 5*time.Second, queueName).Result()
		if err == redis.Nil || ctx.Err() != nil {
			continue
		}
		if err != nil {
			log.Printf("redis error: %v", err)
			time.Sleep(2 * time.Second)
			continue
		}
		var job Job
		if err := json.Unmarshal([]byte(res[1]), &job); err != nil {
			log.Printf("pesan tidak valid: %v", err)
			continue
		}
		handle(rdb, mc, job)
	}
	log.Println("berhenti dengan rapi")
}
