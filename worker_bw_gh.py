#!/usr/bin/env python3
import os, sys, json, time, shutil, subprocess, urllib.request, urllib.parse, re, threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))

STAGING_ROOT = f"/tmp/staging_bw_s{SHARD_INDEX}"
os.makedirs(STAGING_ROOT, exist_ok=True)
PAYLOAD_FILE = "bw_2026_payload.json"

log_lock = threading.Lock()
disk_sem = threading.Semaphore(2)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_lock:
        sys.stdout.write(f"[{ts}][BW-S{SHARD_INDEX}] {msg}\n")
        sys.stdout.flush()

def probe_video(fp):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", fp], timeout=45
        ).decode("utf-8", errors="ignore")
        d = json.loads(out)
        streams = d.get("streams", [])
        if not streams: return None
        return {
            "w": int(streams[0].get("width", 0)),
            "h": int(streams[0].get("height", 0)),
            "dur": float(d.get("format", {}).get("duration", 0.0))
        }
    except Exception:
        return None

def rclone_upload_to_folder_id(local_dir, folder_id):
    dest = f"gdrive2026_cases,root_folder_id={folder_id}:"
    for attempt in range(5):
        r = subprocess.run([
            "rclone", "copy", local_dir, dest,
            "--ignore-times", "--transfers", "4", "--checkers", "4", "--drive-chunk-size", "128M", "--fast-list", "--tpslimit", "6"
        ], capture_output=True, text=True)
        if r.returncode == 0:
            return True
        else:
            log(f"  [RCLONE RETRY {attempt+1}] {dest}: {r.stderr.strip()[:160]}")
            time.sleep(3 * (attempt + 1))
    return False

def download_and_upload_one_vid(vid, cid, vfolder_id):
    u = f"https://www.youtube.com/watch?v={vid}"
    with disk_sem:
        tmp_dir = f"/tmp/dl_bw_{cid}_{vid}"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(tmp_dir, exist_ok=True)
        out_tmpl = os.path.join(tmp_dir, f"%(title).90B [{vid}].%(ext)s")
        try:
            for attempt in range(4):
                cmd = [
                    "yt-dlp",
                    "-f", "bestvideo[height>=720][height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720][height<=2160]+bestaudio/best[height>=720]/137+140/299+140/136+140/298+140/best",
                    "--merge-output-format", "mp4", "--concurrent-fragments", "16",
                    "--socket-timeout", "20", "--retries", "6", "--fragment-retries", "6", "--no-part", "--no-playlist", "--no-mtime",
                    "-o", out_tmpl, u
                ]
                clients = ["ios,web", "mweb,web", "tv,web", "web,default"]
                extra_args = [
                    "--extractor-args", f"youtube:player_client={clients[attempt % len(clients)]};youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416"
                ]
                if attempt % 2 == 1:
                    extra_args = ["--proxy", "socks5://127.0.0.1:40020"] + extra_args
                cmd[1:1] = extra_args
                r_yt = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                dl = [x for x in os.listdir(tmp_dir) if x.endswith(".mp4") and not re.search(r'\.f\d+\.', x)]
                if dl:
                    fp = os.path.join(tmp_dir, dl[0])
                    info = probe_video(fp)
                    if info and info["dur"] > 0:
                        sz = os.path.getsize(fp)
                        log(f"  [{cid}] Downloaded {info['w']}x{info['h']} {dl[0]} ({sz/1e6:.1f} MB, {info['dur']/60:.1f}m) -> uploading to {vfolder_id}...")
                        up_ok = rclone_upload_to_folder_id(tmp_dir, vfolder_id)
                        return up_ok, info["dur"], sz, dl[0]
                else:
                    err_snip = (r_yt.stderr or "").strip().splitlines()[-1:]
                    log(f"  [{cid}] yt-dlp attempt {attempt+1} err: {err_snip}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return False, 0.0, 0, ""

def process_case(case):
    cid = case["case_id"]
    vfolder_id = case["drive_videos_folder_id"]
    r_ls = subprocess.run(["rclone", "lsf", f"gdrive2026_cases,root_folder_id={vfolder_id}:"], capture_output=True, text=True)
    existing = [x.strip() for x in r_ls.stdout.splitlines() if x.strip().endswith(".mp4")]
    if existing:
        log(f"*** SKIPPING {cid} (Already on Drive: {existing}) ***")
        return
    for vid in case.get("videos", []):
        ok, dur, sz, fn = download_and_upload_one_vid(vid, cid, vfolder_id)
        if ok:
            log(f"*** COMPLETED {cid}: {fn} ({sz/1e6:.1f} MB) ***")
        else:
            log(f"!!! FAILED {cid} ({vid}) !!!")

def main():
    cases = json.load(open(PAYLOAD_FILE, encoding="utf-8"))
    my_cases = [c for idx, c in enumerate(cases) if idx % SHARD_TOTAL == SHARD_INDEX]
    log(f"Starting BW 2026 Shard {SHARD_INDEX}/{SHARD_TOTAL} ({len(my_cases)} assigned cases)...")
    for c in my_cases:
        process_case(c)

if __name__ == "__main__":
    main()
