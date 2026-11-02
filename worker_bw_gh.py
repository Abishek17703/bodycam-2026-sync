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
        # 1. Primary: InnerTube IOS over WARP SOCKS5 (which returns status=OK + hlsManifestUrl + adaptiveFormats)
        for use_proxy in (True, False):
            try:
                api = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
                ios_ua = "com.google.ios.youtube/20.03.02 (iPhone16,2; U; CPU iOS 18_2_1 like Mac OS X;)"
                client_obj = {
                    "clientName": "IOS", "clientVersion": "20.03.02", "hl": "en", "gl": "US",
                    "deviceMake": "Apple", "deviceModel": "iPhone16,2", "osName": "iPhone", "osVersion": "18.2.1.22C161"
                }
                payload = json.dumps({"videoId": vid, "context": {"client": client_obj}, "racyCheckOk": True, "contentCheckOk": True})
                curl_cmd = ["curl", "-sL", "--max-time", "15", "-H", "Content-Type: application/json", "-A", ios_ua, "-d", payload, api]
                if use_proxy:
                    curl_cmd[1:1] = ["-x", "socks5h://127.0.0.1:40020"]
                r_p = subprocess.run(curl_cmd, capture_output=True, text=True)
                if r_p.returncode != 0 or not r_p.stdout.strip():
                    continue
                d = json.loads(r_p.stdout)
                play_status = d.get("playabilityStatus", {}).get("status")
                sdata = d.get("streamingData", {})
                fmts = sdata.get("adaptiveFormats", [])
                hls_url = sdata.get("hlsManifestUrl")
                raw_title = d.get("videoDetails", {}).get("title") or f"{cid}"
                safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw_title)[:85].strip()
                final_fn = f"{safe_title} [{vid}].mp4"
                merged_fp = os.path.join(tmp_dir, final_fn)
                log(f"  [{cid}] InnerTube IOS proxy={use_proxy} status={play_status} fmts={len(fmts)} hls={bool(hls_url)}")
                push_diag()

                # 1a. Try adaptiveFormats (any video + audio with url)
                v_fmts = [f for f in fmts if "url" in f and "video" in f.get("mimeType", "")]
                a_fmts = [f for f in fmts if "url" in f and "audio" in f.get("mimeType", "")]
                if v_fmts and a_fmts:
                    v_fmts.sort(key=lambda f: (1 if "video/mp4" in f.get("mimeType", "") else 0, max(int(f.get("width", 0) or 0), int(f.get("height", 0) or 0)), int(f.get("bitrate", 0) or 0)), reverse=True)
                    a_fmts.sort(key=lambda f: (1 if "audio/mp4" in f.get("mimeType", "") else 0, int(f.get("bitrate", 0) or 0)), reverse=True)
                    best_v = v_fmts[0]
                    best_a = a_fmts[0]
                    v_ext = "mp4" if "mp4" in best_v.get("mimeType", "") else "webm"
                    a_ext = "m4a" if "mp4" in best_a.get("mimeType", "") else "webm"
                    v_tmp = os.path.join(tmp_dir, f"v.{v_ext}")
                    a_tmp = os.path.join(tmp_dir, f"a.{a_ext}")
                    dl_v_cmd = ["curl", "-sL", "--retry", "5", "-A", ios_ua, "-o", v_tmp, best_v["url"]]
                    dl_a_cmd = ["curl", "-sL", "--retry", "5", "-A", ios_ua, "-o", a_tmp, best_a["url"]]
                    if use_proxy:
                        dl_v_cmd[1:1] = ["-x", "socks5h://127.0.0.1:40020"]
                        dl_a_cmd[1:1] = ["-x", "socks5h://127.0.0.1:40020"]
                    subprocess.run(dl_v_cmd, timeout=1800)
                    subprocess.run(dl_a_cmd, timeout=900)
                    v_sz = os.path.getsize(v_tmp) if os.path.exists(v_tmp) else 0
                    a_sz = os.path.getsize(a_tmp) if os.path.exists(a_tmp) else 0
                    log(f"  [{cid}] InnerTube adaptive dl v_sz={v_sz} a_sz={a_sz}")
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
                                log(f"  [{cid}] Downloaded InnerTube-Adaptive {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                                push_diag()
                                up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                                return up_ok, info["dur"], sz, final_fn
                    for t in (v_tmp, a_tmp):
                        try:
                            if os.path.exists(t): os.remove(t)
                        except Exception: pass

                # 1b. Try hlsManifestUrl via yt-dlp over same proxy
                if hls_url:
                    hls_cmd = [
                        "yt-dlp", "-f", "bestvideo+bestaudio/best",
                        "--merge-output-format", "mp4", "--concurrent-fragments", "16",
                        "--user-agent", ios_ua, "--no-part", "-o", merged_fp, hls_url
                    ]
                    if use_proxy:
                        hls_cmd[1:1] = ["--proxy", "socks5://127.0.0.1:40020"]
                    r_hls = subprocess.run(hls_cmd, capture_output=True, text=True, timeout=2400)
                    if os.path.exists(merged_fp) and os.path.getsize(merged_fp) > 50000:
                        info = probe_video(merged_fp)
                        if info and info["dur"] > 0:
                            sz = os.path.getsize(merged_fp)
                            log(f"  [{cid}] Downloaded InnerTube-HLS {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                            push_diag()
                            up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                            return up_ok, info["dur"], sz, final_fn
                    else:
                        log(f"  [{cid}] InnerTube-HLS err: {(r_hls.stderr or '').strip().splitlines()[-2:]}")
                        push_diag()
            except Exception as e_in:
                log(f"  [{cid}] InnerTube attempt err: {e_in}")
                push_diag()

        # 2. Fallback: yt-dlp with pure non-SABR clients over WARP proxy
        clients = ["ios", "mweb", "tv", "android_vr", "ios,web"]
        for attempt, cl in enumerate(clients):
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
                    log(f"  [{cid}] Downloaded ({cl}) {info['w']}x{info['h']} {dl[0]} ({sz/1e6:.1f} MB) -> uploading...")
                    push_diag()
                    up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                    return up_ok, info["dur"], sz, dl[0]
            else:
                err_snip = (r_yt.stderr or "").strip().splitlines()[-2:]
                log(f"  [{cid}] yt-dlp ({cl}) err: {err_snip}")
                push_diag()
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
