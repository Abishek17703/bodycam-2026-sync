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
        # 1. Fast Direct InnerTube iOS / ANDROID_VR (both direct and via WARP proxy)
        for use_proxy in (False, True):
            try:
                api = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
                ios_ua = "com.google.ios.youtube/20.03.02 (iPhone16,2; U; CPU iOS 18_2_1 like Mac OS X;)"
                for cname, cver, ua, dev_extra in [
                    ("IOS", "20.03.02", ios_ua, {"deviceMake": "Apple", "deviceModel": "iPhone16,2", "osName": "iPhone", "osVersion": "18.2.1.22C161"}),
                    ("ANDROID_VR", "1.60.19", "Mozilla/5.0", {})
                ]:
                    client_obj = {"clientName": cname, "clientVersion": cver, "hl": "en", "gl": "US"}
                    client_obj.update(dev_extra)
                    payload = json.dumps({"videoId": vid, "context": {"client": client_obj}, "racyCheckOk": True, "contentCheckOk": True})
                    curl_cmd = ["curl", "-sL", "--max-time", "15", "-H", "Content-Type: application/json", "-A", ua, "-d", payload, api]
                    if use_proxy:
                        curl_cmd[1:1] = ["-x", "socks5h://127.0.0.1:40020"]
                    r_p = subprocess.run(curl_cmd, capture_output=True, text=True)
                    if r_p.returncode != 0 or not r_p.stdout.strip():
                        log(f"  [{cid}] InnerTube {cname} proxy={use_proxy} empty/rc={r_p.returncode}")
                        continue
                    d = json.loads(r_p.stdout)
                    play_status = d.get("playabilityStatus", {}).get("status")
                    fmts = d.get("streamingData", {}).get("adaptiveFormats", [])
                    log(f"  [{cid}] InnerTube {cname} proxy={use_proxy} status={play_status} fmts={len(fmts)}")
                    push_diag()
                    v_fmts = [f for f in fmts if "url" in f and "video" in f.get("mimeType", "") and max(int(f.get("width", 0) or 0), int(f.get("height", 0) or 0)) >= 1024 and int(f.get("height", 0) or 0) <= 1080]
                    if not v_fmts:
                        v_fmts = [f for f in fmts if "url" in f and "video" in f.get("mimeType", "") and (int(f.get("height", 0) or 0) >= 720 or int(f.get("width", 0) or 0) >= 720)]
                    a_fmts = [f for f in fmts if "url" in f and "audio/mp4" in f.get("mimeType", "")]
                    if not a_fmts:
                        a_fmts = [f for f in fmts if "url" in f and "audio" in f.get("mimeType", "")]
                    if v_fmts and a_fmts:
                        v_fmts.sort(key=lambda f: (1 if "video/mp4" in f.get("mimeType", "") else 0, int(f.get("height", 0) or 0), int(f.get("bitrate", 0) or 0)), reverse=True)
                        a_fmts.sort(key=lambda f: int(f.get("bitrate", 0) or 0), reverse=True)
                        best_v = v_fmts[0]
                        best_a = a_fmts[0]
                        raw_title = d.get("videoDetails", {}).get("title") or f"{cid}"
                        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw_title)[:85].strip()
                        final_fn = f"{safe_title} [{vid}].mp4"
                        v_ext = "mp4" if "mp4" in best_v.get("mimeType", "") else "webm"
                        a_ext = "m4a" if "mp4" in best_a.get("mimeType", "") else "webm"
                        v_tmp = os.path.join(tmp_dir, f"v.{v_ext}")
                        a_tmp = os.path.join(tmp_dir, f"a.{a_ext}")
                        dl_v_cmd = ["aria2c", "-x", "16", "-s", "16", "-k", "1M", "--file-allocation=none", "--user-agent", ua, "-d", tmp_dir, "-o", f"v.{v_ext}", best_v["url"]]
                        dl_a_cmd = ["aria2c", "-x", "16", "-s", "16", "-k", "1M", "--file-allocation=none", "--user-agent", ua, "-d", tmp_dir, "-o", f"a.{a_ext}", best_a["url"]]
                        if use_proxy:
                            dl_v_cmd = ["curl", "-sL", "--retry", "5", "-x", "socks5h://127.0.0.1:40020", "-A", ua, "-o", v_tmp, best_v["url"]]
                            dl_a_cmd = ["curl", "-sL", "--retry", "5", "-x", "socks5h://127.0.0.1:40020", "-A", ua, "-o", a_tmp, best_a["url"]]
                        subprocess.run(dl_v_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
                        subprocess.run(dl_a_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
                        if os.path.exists(v_tmp) and os.path.exists(a_tmp) and os.path.getsize(v_tmp) > 50000:
                            merged_fp = os.path.join(tmp_dir, final_fn)
                            acodec = "copy" if a_ext == "m4a" else "aac"
                            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", v_tmp, "-i", a_tmp, "-c:v", "copy", "-c:a", acodec, merged_fp], timeout=900)
                            try:
                                os.remove(v_tmp)
                                os.remove(a_tmp)
                            except Exception:
                                pass
                            if os.path.exists(merged_fp):
                                info = probe_video(merged_fp)
                                if info and info["dur"] > 0:
                                    sz = os.path.getsize(merged_fp)
                                    log(f"  [{cid}] Downloaded Direct-InnerTube HD {info['w']}x{info['h']} {final_fn} ({sz/1e6:.1f} MB) -> uploading...")
                                    push_diag()
                                    up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                                    return up_ok, info["dur"], sz, final_fn
                        for t in (v_tmp, a_tmp):
                            try:
                                if os.path.exists(t): os.remove(t)
                            except Exception: pass
            except Exception as e_in:
                log(f"  [{cid}] InnerTube attempt err: {e_in}")

        # 2. Fallback: yt-dlp with bgutil PO-Token server
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
                    log(f"  [{cid}] Downloaded {info['w']}x{info['h']} {dl[0]} ({sz/1e6:.1f} MB) -> uploading...")
                    push_diag()
                    up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                    return up_ok, info["dur"], sz, dl[0]
            else:
                err_snip = (r_yt.stderr or "").strip().splitlines()[-2:]
                log(f"  [{cid}] yt-dlp attempt {attempt+1} err: {err_snip}")
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
