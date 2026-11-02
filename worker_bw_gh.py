#!/usr/bin/env python3
import os, sys, json, time, shutil, subprocess, urllib.request, urllib.parse, re, threading
from datetime import datetime

SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))

STAGING_ROOT = f"/tmp/staging_bw_s{SHARD_INDEX}"
os.makedirs(STAGING_ROOT, exist_ok=True)
PAYLOAD_FILE = "bw_2026_payload.json"
LOG_LINES = []

log_lock = threading.Lock()

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}][BW-S{SHARD_INDEX}] {msg}"
    with log_lock:
        LOG_LINES.append(line)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

def push_diag():
    if SHARD_INDEX in (0, 18):
        try:
            with open(f"/tmp/diag_s{SHARD_INDEX}.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(LOG_LINES) + "\n")
            subprocess.run(["rclone", "copyto", f"/tmp/diag_s{SHARD_INDEX}.txt", f"gdrive_bw2026:_diag_s{SHARD_INDEX}.txt"], capture_output=True, timeout=30)
        except Exception:
            pass

def start_fresh_warp(attempt=1):
    subprocess.run("pkill -9 wireproxy 2>/dev/null || true", shell=True)
    wdir = f"/tmp/w_rot_{attempt}"
    shutil.rmtree(wdir, ignore_errors=True)
    os.makedirs(wdir, exist_ok=True)
    wconf = os.path.join(wdir, "wgcf-profile.conf")
    subprocess.run(["/tmp/wgcf", "register", "--accept-tos"], cwd=wdir, capture_output=True)
    subprocess.run(["/tmp/wgcf", "generate"], cwd=wdir, capture_output=True)
    if os.path.exists(wconf):
        with open(wconf, "a") as f:
            f.write("\n[Socks5]\nBindAddress = 127.0.0.1:40020\n")
        subprocess.Popen(["/tmp/wireproxy", "-c", wconf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(4)
        r2 = subprocess.run(["curl", "-sI", "--max-time", "8", "-x", "socks5h://127.0.0.1:40020", "https://www.youtube.com"], capture_output=True)
        if r2.returncode == 0:
            return True
    return False

def ensure_warp_alive():
    r = subprocess.run(["curl", "-sI", "--max-time", "6", "-x", "socks5h://127.0.0.1:40020", "https://www.youtube.com"], capture_output=True)
    if r.returncode == 0:
        return True
    for a in range(1, 6):
        if start_fresh_warp(a):
            return True
        time.sleep(2 * a)
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
        client_obj = {
            "clientName": "IOS", "clientVersion": "20.03.02", "hl": "en", "gl": "US",
            "deviceMake": "Apple", "deviceModel": "iPhone16,2", "osName": "iPhone", "osVersion": "18.2.1.22C161"
        }
        payload = json.dumps({"videoId": vid, "context": {"client": client_obj}, "racyCheckOk": True, "contentCheckOk": True})

        # Rotate WARP exit IP up to 8 times until YouTube InnerTube IOS returns status=OK
        for rot in range(1, 9):
            ensure_warp_alive()
            curl_cmd = ["curl", "-sL", "--max-time", "15", "-x", "socks5h://127.0.0.1:40020", "-H", "Content-Type: application/json", "-A", ios_ua, "-d", payload, api]
            r_p = subprocess.run(curl_cmd, capture_output=True, text=True)
            d = {}
            if r_p.returncode == 0 and r_p.stdout.strip():
                try:
                    d = json.loads(r_p.stdout)
                except Exception:
                    d = {}
            play_status = d.get("playabilityStatus", {}).get("status")
            sdata = d.get("streamingData", {})
            fmts = sdata.get("adaptiveFormats", [])
            muxed = sdata.get("formats", [])
            hls_url = sdata.get("hlsManifestUrl")
            v_with_url = [f for f in fmts if "url" in f and "video" in f.get("mimeType", "")]
            a_with_url = [f for f in fmts if "url" in f and "audio" in f.get("mimeType", "")]
            log(f"  [{cid}] WARP rot={rot} IOS status={play_status} fmts={len(fmts)}(v_url={len(v_with_url)},a_url={len(a_with_url)},mux={len(muxed)}) hls={bool(hls_url)}")
            push_diag()

            if play_status == "OK":
                raw_title = d.get("videoDetails", {}).get("title") or f"{cid}"
                safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw_title)[:85].strip()
                final_fn = f"{safe_title} [{vid}].mp4"
                merged_fp = os.path.join(tmp_dir, final_fn)

                # 1a. Adaptive video + audio URLs via curl over same WARP proxy
                if v_with_url and a_with_url:
                    v_with_url.sort(key=lambda f: (1 if "video/mp4" in f.get("mimeType", "") else 0, max(int(f.get("width", 0) or 0), int(f.get("height", 0) or 0)), int(f.get("bitrate", 0) or 0)), reverse=True)
                    a_with_url.sort(key=lambda f: (1 if "audio/mp4" in f.get("mimeType", "") else 0, int(f.get("bitrate", 0) or 0)), reverse=True)
                    best_v = v_with_url[0]
                    best_a = a_with_url[0]
                    v_ext = "mp4" if "mp4" in best_v.get("mimeType", "") else "webm"
                    a_ext = "m4a" if "mp4" in best_a.get("mimeType", "") else "webm"
                    v_tmp = os.path.join(tmp_dir, f"v.{v_ext}")
                    a_tmp = os.path.join(tmp_dir, f"a.{a_ext}")
                    subprocess.run(["curl", "-sL", "--retry", "5", "-x", "socks5h://127.0.0.1:40020", "-A", ios_ua, "-o", v_tmp, best_v["url"]], timeout=1800)
                    subprocess.run(["curl", "-sL", "--retry", "5", "-x", "socks5h://127.0.0.1:40020", "-A", ios_ua, "-o", a_tmp, best_a["url"]], timeout=900)
                    v_sz = os.path.getsize(v_tmp) if os.path.exists(v_tmp) else 0
                    a_sz = os.path.getsize(a_tmp) if os.path.exists(a_tmp) else 0
                    log(f"  [{cid}] Adaptive curl dl v_sz={v_sz} a_sz={a_sz}")
                    push_diag()
                    if v_sz > 50000 and a_sz > 10000:
                        acodec = "copy" if a_ext == "m4a" else "aac"
                        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", v_tmp, "-i", a_tmp, "-c:v", "copy", "-c:a", acodec, merged_fp], timeout=900)
                        for t in (v_tmp, a_tmp):
                            try: os.remove(t)
                            except Exception: pass
                        if os.path.exists(merged_fp):
                            info = probe_video(merged_fp)
                            if info and info["dur"] > 0:
                                sz = os.path.getsize(merged_fp)
                                log(f"  [{cid}] Downloaded Adaptive {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                                push_diag()
                                up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                                return up_ok, info["dur"], sz, final_fn

                # 1b. HLS manifest URL via yt-dlp or ffmpeg over same WARP proxy
                if hls_url:
                    ensure_warp_alive()
                    hls_cmd = [
                        "yt-dlp", "--proxy", "socks5://127.0.0.1:40020",
                        "-f", "bestvideo+bestaudio/best",
                        "--merge-output-format", "mp4", "--concurrent-fragments", "16",
                        "--user-agent", ios_ua, "--no-part", "-o", merged_fp, hls_url
                    ]
                    r_hls = subprocess.run(hls_cmd, capture_output=True, text=True, timeout=2400)
                    if os.path.exists(merged_fp) and os.path.getsize(merged_fp) > 50000:
                        info = probe_video(merged_fp)
                        if info and info["dur"] > 0:
                            sz = os.path.getsize(merged_fp)
                            log(f"  [{cid}] Downloaded HLS {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                            push_diag()
                            up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                            return up_ok, info["dur"], sz, final_fn
                    else:
                        log(f"  [{cid}] HLS err: {(r_hls.stderr or '').strip().splitlines()[-2:]}")
                        push_diag()

                # 1c. Since this WARP IP has play_status == OK, run yt-dlp immediately on this clean WARP IP!
                for cl in ["ios", "mweb,web", "tv,web"]:
                    ensure_warp_alive()
                    cmd = [
                        "yt-dlp",
                        "--proxy", "socks5://127.0.0.1:40020",
                        "--extractor-args", f"youtube:player_client={cl};youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
                        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                        "--merge-output-format", "mp4", "--concurrent-fragments", "16",
                        "--socket-timeout", "20", "--retries", "6", "--fragment-retries", "6", "--no-part", "--no-playlist", "--no-mtime",
                        "-o", out_tmpl, u
                    ]
                    r_yt = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                    dl = [x for x in os.listdir(tmp_dir) if x.endswith(".mp4") and not re.search(r'\.f\d+\.', x)]
                    if dl:
                        fp = os.path.join(tmp_dir, dl[0])
                        info = probe_video(fp)
                        if info and info["dur"] > 0:
                            sz = os.path.getsize(fp)
                            log(f"  [{cid}] Downloaded yt-dlp({cl}) {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                            push_diag()
                            up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                            return up_ok, info["dur"], sz, dl[0]
                    else:
                        log(f"  [{cid}] yt-dlp({cl}) err: {(r_yt.stderr or '').strip().splitlines()[-2:]}")
                        push_diag()

            # Rotate WARP IP because this exit IP got LOGIN_REQUIRED
            log(f"  [{cid}] Rotating WARP IP (attempt {rot+1})...")
            push_diag()
            start_fresh_warp(rot + 10)
            time.sleep(2)
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
