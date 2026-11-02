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

        for rot in range(1, 9):
            ensure_warp_alive()
            for use_proxy in (True, False):
                for cname, cver, ua, dev_extra in [
                    ("ANDROID_VR", "1.60.19", "Mozilla/5.0", {}),
                    ("IOS", "20.03.02", ios_ua, {"deviceMake": "Apple", "deviceModel": "iPhone16,2", "osName": "iPhone", "osVersion": "18.2.1.22C161"})
                ]:
                    client_obj = {"clientName": cname, "clientVersion": cver, "hl": "en", "gl": "US"}
                    client_obj.update(dev_extra)
                    payload = json.dumps({"videoId": vid, "context": {"client": client_obj}, "racyCheckOk": True, "contentCheckOk": True})
                    curl_cmd = ["curl", "-sL", "--max-time", "15", "-H", "Content-Type: application/json", "-A", ua, "-d", payload, api]
                    if use_proxy:
                        curl_cmd[1:1] = ["-x", "socks5h://127.0.0.1:40020"]
                    r_p = subprocess.run(curl_cmd, capture_output=True, text=True)
                    d = {}
                    if r_p.returncode == 0 and r_p.stdout.strip():
                        try: d = json.loads(r_p.stdout)
                        except Exception: d = {}
                    play_status = d.get("playabilityStatus", {}).get("status")
                    sdata = d.get("streamingData", {})
                    fmts = sdata.get("adaptiveFormats", [])
                    hls_url = sdata.get("hlsManifestUrl")
                    v_with_url = [f for f in fmts if "url" in f and "video" in f.get("mimeType", "")]
                    a_with_url = [f for f in fmts if "url" in f and "audio" in f.get("mimeType", "")]
                    log(f"  [{cid}] rot={rot} {cname}(proxy={use_proxy}) status={play_status} v={len(v_with_url)} a={len(a_with_url)} hls={bool(hls_url)}")
                    push_diag()

                    if play_status == "OK":
                        raw_title = d.get("videoDetails", {}).get("title") or f"{cid}"
                        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw_title)[:85].strip()
                        final_fn = f"{safe_title} [{vid}].mp4"
                        merged_fp = os.path.join(tmp_dir, final_fn)

                        if v_with_url and a_with_url:
                            v_with_url.sort(key=lambda f: (1 if "video/mp4" in f.get("mimeType", "") else 0, max(int(f.get("width", 0) or 0), int(f.get("height", 0) or 0)), int(f.get("bitrate", 0) or 0)), reverse=True)
                            a_with_url.sort(key=lambda f: (1 if "audio/mp4" in f.get("mimeType", "") else 0, int(f.get("bitrate", 0) or 0)), reverse=True)
                            best_v = v_with_url[0]
                            best_a = a_with_url[0]
                            v_ext = "mp4" if "mp4" in best_v.get("mimeType", "") else "webm"
                            a_ext = "m4a" if "mp4" in best_a.get("mimeType", "") else "webm"
                            v_tmp = os.path.join(tmp_dir, f"v.{v_ext}")
                            a_tmp = os.path.join(tmp_dir, f"a.{a_ext}")

                            # Try aria2c direct first (fast), then curl via proxy if needed
                            for dl_mode in (["proxy", "direct"] if use_proxy else ["direct"]):
                                for t in (v_tmp, a_tmp):
                                    try: os.remove(t)
                                    except Exception: pass
                                if dl_mode == "direct":
                                    subprocess.run(["aria2c", "-x", "16", "-s", "16", "-k", "1M", "--file-allocation=none", "--user-agent", ua, "-d", tmp_dir, "-o", f"v.{v_ext}", best_v["url"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
                                    subprocess.run(["aria2c", "-x", "16", "-s", "16", "-k", "1M", "--file-allocation=none", "--user-agent", ua, "-d", tmp_dir, "-o", f"a.{a_ext}", best_a["url"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                                else:
                                    subprocess.run(["curl", "-sL", "--max-time", "180", "-x", "socks5h://127.0.0.1:40020", "-A", ua, "-o", v_tmp, best_v["url"]], timeout=200)
                                    subprocess.run(["curl", "-sL", "--max-time", "120", "-x", "socks5h://127.0.0.1:40020", "-A", ua, "-o", a_tmp, best_a["url"]], timeout=140)
                                v_sz = os.path.getsize(v_tmp) if os.path.exists(v_tmp) else 0
                                a_sz = os.path.getsize(a_tmp) if os.path.exists(a_tmp) else 0
                                log(f"  [{cid}] {cname} {dl_mode} dl v_sz={v_sz} a_sz={a_sz}")
                                push_diag()
                                if v_sz > 50000 and a_sz > 10000:
                                    acodec = "copy" if a_ext == "m4a" else "aac"
                                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", v_tmp, "-i", a_tmp, "-c:v", "copy", "-c:a", acodec, merged_fp], timeout=600)
                                    for t in (v_tmp, a_tmp):
                                        try: os.remove(t)
                                        except Exception: pass
                                    if os.path.exists(merged_fp):
                                        info = probe_video(merged_fp)
                                        if info and info["dur"] > 0:
                                            sz = os.path.getsize(merged_fp)
                                            log(f"  [{cid}] SUCCESS {cname} {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                                            push_diag()
                                            up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                                            return up_ok, info["dur"], sz, final_fn

                        if hls_url:
                            ensure_warp_alive()
                            hls_cmd = [
                                "yt-dlp",
                                "-f", "bestvideo+bestaudio/best",
                                "--merge-output-format", "mp4", "--concurrent-fragments", "16",
                                "--user-agent", ua, "--no-part", "-o", merged_fp, hls_url
                            ]
                            if use_proxy:
                                hls_cmd[1:1] = ["--proxy", "socks5://127.0.0.1:40020"]
                            r_hls = subprocess.run(hls_cmd, capture_output=True, text=True, timeout=600)
                            if os.path.exists(merged_fp) and os.path.getsize(merged_fp) > 50000:
                                info = probe_video(merged_fp)
                                if info and info["dur"] > 0:
                                    sz = os.path.getsize(merged_fp)
                                    log(f"  [{cid}] SUCCESS HLS {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                                    push_diag()
                                    up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                                    return up_ok, info["dur"], sz, final_fn

            # Try yt-dlp with bounded timeout (240s) + bgutil PO token over clean WARP proxy and direct
            for use_p in (True, False):
                for cl in ["mweb,web", "ios,web", "tv,web", "android_vr"]:
                    ensure_warp_alive()
                    cmd = [
                        "yt-dlp",
                        "--extractor-args", f"youtube:player_client={cl};youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
                        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                        "--merge-output-format", "mp4", "--concurrent-fragments", "16",
                        "--socket-timeout", "15", "--retries", "3", "--fragment-retries", "3", "--no-part", "--no-playlist", "--no-mtime",
                        "-o", out_tmpl, u
                    ]
                    if use_p:
                        cmd[1:1] = ["--proxy", "socks5://127.0.0.1:40020"]
                    try:
                        r_yt = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
                    except subprocess.TimeoutExpired:
                        log(f"  [{cid}] yt-dlp({cl},proxy={use_p}) timed out after 240s")
                        push_diag()
                        continue
                    dl = [x for x in os.listdir(tmp_dir) if x.endswith(".mp4") and not re.search(r'\.f\d+\.', x)]
                    if dl:
                        fp = os.path.join(tmp_dir, dl[0])
                        info = probe_video(fp)
                        if info and info["dur"] > 0:
                            sz = os.path.getsize(fp)
                            log(f"  [{cid}] SUCCESS yt-dlp({cl},proxy={use_p}) {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                            push_diag()
                            up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                            return up_ok, info["dur"], sz, dl[0]
                    else:
                        log(f"  [{cid}] yt-dlp({cl},proxy={use_p}) err: {(r_yt.stderr or '').strip().splitlines()[-1:]}")
                        push_diag()

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
