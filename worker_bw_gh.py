#!/usr/bin/env python3
import os, sys, json, time, shutil, subprocess, re, socket
from datetime import datetime

SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))
PAYLOAD_FILE = "bw_2026_payload.json"
DIAG_FILE = f"/tmp/_diag_s{SHARD_INDEX}.txt"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}][BW-S{SHARD_INDEX}] {msg}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    if SHARD_INDEX in (0, 18):
        with open(DIAG_FILE, "a", encoding="utf-8") as f:
            f.write(line)

def push_diag():
    if SHARD_INDEX in (0, 18) and os.path.exists(DIAG_FILE):
        subprocess.run(["rclone", "copyto", DIAG_FILE, f"gdrive_bw2026:_diag_s{SHARD_INDEX}.txt"], capture_output=True)

def is_warp_open():
    try:
        with socket.create_connection(("127.0.0.1", 40020), timeout=1.5):
            return True
    except Exception:
        return False

def start_fresh_warp(seed=0):
    subprocess.run(["pkill", "-9", "wireproxy"], capture_output=True)
    time.sleep(0.5)
    wdir = f"/tmp/warp_{SHARD_INDEX}_{seed}_{int(time.time())}"
    os.makedirs(wdir, exist_ok=True)
    subprocess.run(["/tmp/wgcf", "register", "--accept-tos"], cwd=wdir, capture_output=True, timeout=25)
    subprocess.run(["/tmp/wgcf", "generate"], cwd=wdir, capture_output=True, timeout=20)
    prof = os.path.join(wdir, "wgcf-profile.conf")
    if os.path.exists(prof):
        with open(prof, "a", encoding="utf-8") as f:
            f.write("\n[Socks5]\nBindAddress = 127.0.0.1:40020\n")
        subprocess.Popen(["/tmp/wireproxy", "-c", prof], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(16):
            time.sleep(0.5)
            if is_warp_open():
                return True
    return False

def ensure_warp_alive():
    if is_warp_open():
        return True
    for i in range(4):
        if start_fresh_warp(i):
            return True
    return False

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

def rclone_upload_dir(local_dir, case_folder_name):
    dest = f"gdrive_bw2026:{case_folder_name}/Videos"
    for attempt in range(5):
        r = subprocess.run([
            "rclone", "copy", local_dir, dest,
            "--ignore-times", "--transfers", "4", "--checkers", "4", "--drive-chunk-size", "128M", "--fast-list", "--tpslimit", "6"
        ], capture_output=True, text=True)
        if r.returncode == 0:
            return True
        else:
            log(f"  [RCLONE RETRY {attempt+1}] {dest}: {r.stderr.strip()[:200]}")
            push_diag()
            time.sleep(3 * (attempt + 1))
    return False

def download_and_upload_one_vid(vid, cid, case_folder_name):
    u = f"https://www.youtube.com/watch?v={vid}"
    tmp_dir = f"/tmp/dl_bw_{cid}_{vid}"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    out_tmpl = os.path.join(tmp_dir, f"%(title).90B [{vid}].%(ext)s")
    try:
        ensure_warp_alive()
        api = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
        ios_ua = "com.google.ios.youtube/20.03.02 (iPhone16,2; U; CPU iOS 18_2_1 like Mac OS X;)"

        for rot in range(1, 8):
            # Step A: Try DIRECT (no proxy) with bgutil PO-Token server first!
            # When --proxy is NOT passed, yt-dlp reaches 127.0.0.1:4416 immediately with zero deadlock.
            for cl in ["mweb,web", "ios,web", "tv,web", "web,default"]:
                log(f"  [{cid}] rot={rot} Trying DIRECT yt-dlp({cl}) + bgutil...")
                push_diag()
                cmd = [
                    "yt-dlp",
                    "--extractor-args", f"youtube:player_client={cl};youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
                    "-f", "bestvideo[height>=720][height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720][height<=2160]+bestaudio/best[height>=720]/137+140/299+140/136+140/298+140/best",
                    "--merge-output-format", "mp4", "--concurrent-fragments", "16",
                    "--socket-timeout", "15", "--retries", "4", "--fragment-retries", "4", "--no-part", "--no-playlist", "--no-mtime",
                    "-o", out_tmpl, u
                ]
                try:
                    r_yt = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
                except subprocess.TimeoutExpired:
                    log(f"  [{cid}] DIRECT yt-dlp({cl}) timed out after 360s")
                    push_diag()
                    continue
                dl = [x for x in os.listdir(tmp_dir) if x.endswith(".mp4") and not re.search(r'\.f\d+\.', x)]
                if dl:
                    fp = os.path.join(tmp_dir, dl[0])
                    info = probe_video(fp)
                    if info and info["dur"] > 0:
                        sz = os.path.getsize(fp)
                        log(f"  [{cid}] SUCCESS DIRECT yt-dlp({cl}) {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                        push_diag()
                        up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                        return up_ok, info["dur"], sz, dl[0]
                else:
                    err_lines = [ln for ln in (r_yt.stderr or "").splitlines() if "ERROR:" in ln or "WARNING:" in ln]
                    log(f"  [{cid}] DIRECT yt-dlp({cl}) err: {err_lines[-2:]}")
                    push_diag()

            # Step B: Try WARP proxy + InnerTube IOS HLS manifest (or curl -L with -r 0-9437183)
            ensure_warp_alive()
            r_p = subprocess.run([
                "curl", "-sL", "--max-time", "15", "-x", "socks5h://127.0.0.1:40020",
                "-H", "Content-Type: application/json", "-A", ios_ua,
                "-d", json.dumps({
                    "videoId": vid,
                    "context": {"client": {
                        "clientName": "IOS", "clientVersion": "20.03.02", "hl": "en", "gl": "US",
                        "deviceMake": "Apple", "deviceModel": "iPhone16,2", "osName": "iPhone", "osVersion": "18.2.1.22C161"
                    }},
                    "racyCheckOk": True, "contentCheckOk": True
                }),
                api
            ], capture_output=True, text=True)
            d = {}
            if r_p.returncode == 0 and r_p.stdout.strip():
                try: d = json.loads(r_p.stdout)
                except Exception: d = {}
            play_status = d.get("playabilityStatus", {}).get("status")
            hls_url = d.get("streamingData", {}).get("hlsManifestUrl")
            log(f"  [{cid}] rot={rot} WARP IOS status={play_status} hls={bool(hls_url)}")
            push_diag()
            if play_status == "OK" and hls_url:
                raw_title = d.get("videoDetails", {}).get("title") or f"{cid}"
                safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw_title)[:85].strip()
                final_fn = f"{safe_title} [{vid}].mp4"
                merged_fp = os.path.join(tmp_dir, final_fn)
                r_hls = subprocess.run([
                    "yt-dlp", "--proxy", "socks5://127.0.0.1:40020",
                    "-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4",
                    "--concurrent-fragments", "16", "--user-agent", ios_ua, "--no-part", "-o", merged_fp, hls_url
                ], capture_output=True, text=True, timeout=600)
                if os.path.exists(merged_fp) and os.path.getsize(merged_fp) > 50000:
                    info = probe_video(merged_fp)
                    if info and info["dur"] > 0:
                        sz = os.path.getsize(merged_fp)
                        log(f"  [{cid}] SUCCESS WARP HLS {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                        push_diag()
                        up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                        return up_ok, info["dur"], sz, final_fn

            log(f"  [{cid}] Rotating WARP IP (attempt {rot+1})...")
            push_diag()
            start_fresh_warp(rot + 10)
            time.sleep(1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return False, 0.0, 0, ""

def process_case(case):
    cid = case["case_id"]
    cfolder_name = case["drive_case_folder_name"]
    r_ls = subprocess.run(["rclone", "lsf", f"gdrive_bw2026:{cfolder_name}/Videos"], capture_output=True, text=True)
    existing = [x.strip() for x in r_ls.stdout.splitlines() if x.strip().endswith(".mp4")]
    if existing:
        log(f"*** SKIPPING {cid} (Already on Drive: {existing}) ***")
        push_diag()
        return
    for vid in case.get("videos", []):
        ok, dur, sz, fn = download_and_upload_one_vid(vid, cid, cfolder_name)
        if ok:
            log(f"*** COMPLETED {cid}: {fn} ({sz/1e6:.1f} MB) ***")
        else:
            log(f"!!! FAILED {cid} ({vid}) !!!")
        push_diag()

def main():
    cases = json.load(open(PAYLOAD_FILE, encoding="utf-8"))
    my_cases = [c for idx, c in enumerate(cases) if idx % SHARD_TOTAL == SHARD_INDEX]
    log(f"Starting BW 2026 Shard {SHARD_INDEX}/{SHARD_TOTAL} ({len(my_cases)} assigned cases)...")
    push_diag()
    for c in my_cases:
        process_case(c)

if __name__ == "__main__":
    main()
