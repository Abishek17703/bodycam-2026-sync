#!/usr/bin/env python3
import os, sys, json, time, shutil, subprocess, re, socket
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

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
    subprocess.run(["wgcf", "register", "--accept-tos"], cwd=wdir, capture_output=True, timeout=25)
    subprocess.run(["wgcf", "generate"], cwd=wdir, capture_output=True, timeout=20)
    prof = os.path.join(wdir, "wgcf-profile.conf")
    if os.path.exists(prof):
        with open(prof, "a", encoding="utf-8") as f:
            f.write("\n[Socks5]\nBindAddress = 127.0.0.1:40020\n")
        subprocess.Popen(["wireproxy", "-c", prof], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
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

def ranged_download_stream(url, content_len, out_fp, ua):
    """Download YouTube adaptive stream using 9MB HTTP Range requests over WARP SOCKS5 (or direct if IP isn't bound) with 8 parallel workers."""
    total = int(content_len or 0)
    if total <= 0:
        # Probe total size via 0-0 range header
        r_h = subprocess.run([
            "curl", "-sI", "--max-time", "12", "-x", "socks5h://127.0.0.1:40020",
            "-A", ua, "-r", "0-0", url
        ], capture_output=True, text=True)
        m = re.search(r"Content-Range:\s*bytes\s+0-0/(\d+)", r_h.stdout, re.I)
        if m:
            total = int(m.group(1))
    if total <= 0:
        return False

    chunk_sz = 9 * 1024 * 1024  # 9 MB per chunk (bypasses YouTube's 10MB unthrottled cap)
    ranges = []
    pos = 0
    idx = 0
    while pos < total:
        end = min(pos + chunk_sz - 1, total - 1)
        ranges.append((idx, pos, end))
        pos = end + 1
        idx += 1

    part_dir = out_fp + "_parts"
    shutil.rmtree(part_dir, ignore_errors=True)
    os.makedirs(part_dir, exist_ok=True)

    # Check if direct (non-proxy) range request works for even faster speed
    r_direct = subprocess.run([
        "curl", "-sI", "--max-time", "8", "-A", ua, "-r", "0-1023", url
    ], capture_output=True, text=True)
    use_direct = "206 Partial Content" in r_direct.stdout

    def fetch_part(item):
        i, s, e = item
        pfp = os.path.join(part_dir, f"p_{i:05d}")
        expected = e - s + 1
        for att in range(5):
            cmd = ["curl", "-sL", "--max-time", "90", "-A", ua, "-r", f"{s}-{e}", "-o", pfp, url]
            if not use_direct or att >= 2:
                ensure_warp_alive()
                cmd[1:1] = ["-x", "socks5h://127.0.0.1:40020"]
            subprocess.run(cmd, capture_output=True)
            if os.path.exists(pfp) and os.path.getsize(pfp) == expected:
                return True
            time.sleep(1)
        return False

    workers = 12 if use_direct else 6
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(fetch_part, ranges))

    if not all(results):
        shutil.rmtree(part_dir, ignore_errors=True)
        return False

    with open(out_fp, "wb") as out_f:
        for i, _, _ in ranges:
            pfp = os.path.join(part_dir, f"p_{i:05d}")
            with open(pfp, "rb") as in_f:
                shutil.copyfileobj(in_f, out_f)
    shutil.rmtree(part_dir, ignore_errors=True)
    return os.path.exists(out_fp) and os.path.getsize(out_fp) == total

def download_and_upload_one_vid(vid, cid, case_folder_name):
    tmp_dir = f"/tmp/dl_bw_{cid}_{vid}"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        ensure_warp_alive()
        api = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
        ios_ua = "com.google.ios.youtube/20.03.02 (iPhone16,2; U; CPU iOS 18_2_1 like Mac OS X;)"

        for rot in range(1, 10):
            ensure_warp_alive()
            curl_cmd = [
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
            ]
            r_p = subprocess.run(curl_cmd, capture_output=True, text=True)
            d = {}
            if r_p.returncode == 0 and r_p.stdout.strip():
                try: d = json.loads(r_p.stdout)
                except Exception: d = {}
            play_status = d.get("playabilityStatus", {}).get("status")
            sdata = d.get("streamingData", {})
            fmts = sdata.get("adaptiveFormats", [])
            v_with_url = [f for f in fmts if "url" in f and "video" in f.get("mimeType", "")]
            a_with_url = [f for f in fmts if "url" in f and "audio" in f.get("mimeType", "")]
            log(f"  [{cid}] rot={rot} IOS status={play_status} v={len(v_with_url)} a={len(a_with_url)}")
            push_diag()

            if play_status == "OK" and v_with_url and a_with_url:
                raw_title = d.get("videoDetails", {}).get("title") or f"{cid}"
                safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw_title)[:85].strip()
                final_fn = f"{safe_title} [{vid}].mp4"
                merged_fp = os.path.join(tmp_dir, final_fn)

                # Prefer 1080p or 720p MP4 (avc1) so file sizes are reasonable (~300-800MB instead of 3GB 4K) and muxing is instant
                v_1080_720 = [f for f in v_with_url if 720 <= max(int(f.get("width", 0) or 0), int(f.get("height", 0) or 0)) <= 1920]
                if v_1080_720:
                    v_1080_720.sort(key=lambda f: (1 if "video/mp4" in f.get("mimeType", "") else 0, max(int(f.get("width", 0) or 0), int(f.get("height", 0) or 0)), int(f.get("bitrate", 0) or 0)), reverse=True)
                    best_v = v_1080_720[0]
                else:
                    v_with_url.sort(key=lambda f: (1 if "video/mp4" in f.get("mimeType", "") else 0, max(int(f.get("width", 0) or 0), int(f.get("height", 0) or 0)), int(f.get("bitrate", 0) or 0)), reverse=True)
                    best_v = v_with_url[0]

                a_with_url.sort(key=lambda f: (1 if "audio/mp4" in f.get("mimeType", "") else 0, int(f.get("bitrate", 0) or 0)), reverse=True)
                best_a = a_with_url[0]

                v_ext = "mp4" if "mp4" in best_v.get("mimeType", "") else "webm"
                a_ext = "m4a" if "mp4" in best_a.get("mimeType", "") else "webm"
                v_tmp = os.path.join(tmp_dir, f"v.{v_ext}")
                a_tmp = os.path.join(tmp_dir, f"a.{a_ext}")

                log(f"  [{cid}] Ranged-DL starting: v={best_v.get('width')}x{best_v.get('height')} ({int(best_v.get('contentLength', 0))/1e6:.1f} MB) + a ({int(best_a.get('contentLength', 0))/1e6:.1f} MB)")
                push_diag()

                v_ok = ranged_download_stream(best_v["url"], best_v.get("contentLength"), v_tmp, ios_ua)
                a_ok = ranged_download_stream(best_a["url"], best_a.get("contentLength"), a_tmp, ios_ua)
                log(f"  [{cid}] Ranged-DL finished: v_ok={v_ok} a_ok={a_ok}")
                push_diag()

                if v_ok and a_ok:
                    acodec = "copy" if a_ext == "m4a" else "aac"
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", v_tmp, "-i", a_tmp, "-c:v", "copy", "-c:a", acodec, merged_fp], timeout=600)
                    for t in (v_tmp, a_tmp):
                        try: os.remove(t)
                        except Exception: pass
                    if os.path.exists(merged_fp):
                        info = probe_video(merged_fp)
                        if info and info["dur"] > 0:
                            sz = os.path.getsize(merged_fp)
                            log(f"  [{cid}] SUCCESS {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
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
