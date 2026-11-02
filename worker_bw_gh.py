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
        try:
            subprocess.run(
                ["rclone", "copyto", "--ignore-times", "--contimeout", "10s", "--timeout", "20s", "--retries", "1", DIAG_FILE, f"gdrive_bw2026:_diag_s{SHARD_INDEX}.txt"],
                capture_output=True, timeout=25
            )
        except Exception:
            pass

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
    try:
        subprocess.run(["/tmp/wgcf", "register", "--accept-tos"], cwd=wdir, capture_output=True, timeout=35)
        subprocess.run(["/tmp/wgcf", "generate"], cwd=wdir, capture_output=True, timeout=25)
    except Exception:
        return False
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
    for i in range(5):
        if start_fresh_warp(i):
            return True
        time.sleep(2)
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

def rclone_upload_dir(local_dir, vfolder_id):
    dest = f"gdrive_bw2026:--drive-root-folder-id"
    for attempt in range(5):
        r = subprocess.run([
            "rclone", "copy", local_dir, "gdrive_bw2026:",
            "--drive-root-folder-id", vfolder_id,
            "--ignore-times", "--transfers", "4", "--checkers", "4", "--drive-chunk-size", "128M", "--fast-list", "--tpslimit", "6"
        ], capture_output=True, text=True, timeout=600)
        if r.returncode == 0:
            return True
        else:
            log(f"  [RCLONE RETRY {attempt+1}] {vfolder_id}: {r.stderr.strip()[:200]}")
            push_diag()
            time.sleep(3 * (attempt + 1))
    return False

def ranged_download_stream(url, content_len, out_fp, ua):
    total = int(content_len or 0)
    if total <= 0:
        return False
    # Check 0-1023 with -L (follow redirects!) via WARP first, then direct
    p_test = out_fp + "_test"
    subprocess.run([
        "curl", "-sL", "--max-time", "15", "-x", "socks5h://127.0.0.1:40020",
        "-A", ua, "-r", "0-1023", "-o", p_test, url
    ], capture_output=True)
    test_sz = os.path.getsize(p_test) if os.path.exists(p_test) else 0
    try: os.remove(p_test)
    except Exception: pass
    if test_sz != 1024:
        # Also test &range=0-1023 query param
        sep = "&" if "?" in url else "?"
        subprocess.run([
            "curl", "-sL", "--max-time", "15", "-x", "socks5h://127.0.0.1:40020",
            "-A", ua, "-o", p_test, f"{url}{sep}range=0-1023"
        ], capture_output=True)
        q_sz = os.path.getsize(p_test) if os.path.exists(p_test) else 0
        try: os.remove(p_test)
        except Exception: pass
        log(f"  Range probe: header_sz={test_sz}, query_sz={q_sz}")
        push_diag()
        if q_sz != 1024:
            return False
        use_query_range = True
    else:
        log(f"  Range probe OK: header_sz={test_sz}")
        push_diag()
        use_query_range = False

    chunk_sz = 2 * 1024 * 1024
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
    sep = "&" if "?" in url else "?"

    def fetch_part(item):
        i, s, e = item
        pfp = os.path.join(part_dir, f"p_{i:05d}")
        expected = e - s + 1
        for att in range(6):
            ensure_warp_alive()
            if use_query_range:
                cmd = ["curl", "-sL", "--max-time", "45", "-x", "socks5h://127.0.0.1:40020", "-A", ua, "-o", pfp, f"{url}{sep}range={s}-{e}"]
            else:
                cmd = ["curl", "-sL", "--max-time", "45", "-x", "socks5h://127.0.0.1:40020", "-A", ua, "-r", f"{s}-{e}", "-o", pfp, url]
            subprocess.run(cmd, capture_output=True)
            if os.path.exists(pfp) and os.path.getsize(pfp) == expected:
                return True
            time.sleep(1)
        return False

    log(f"  [Ranged-DL] Starting {len(ranges)} chunks ({total/1e6:.1f} MB total)...")
    push_diag()
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(fetch_part, ranges))

    ok_cnt = sum(1 for x in results if x)
    log(f"  [Ranged-DL] Completed {ok_cnt}/{len(ranges)} chunks.")
    push_diag()
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

def generate_pot_for_vid(vid):
    """Fetch a fresh PO token from localhost:4416 directly (never via WARP proxy) or return None."""
    try:
        r = subprocess.run([
            "curl", "-s", "--max-time", "10", "-X", "POST",
            "-H", "Content-Type: application/json",
            "-d", json.dumps({"content_binding": vid}),
            "http://127.0.0.1:4416/get_pot"
        ], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            d = json.loads(r.stdout)
            return d.get("po_token"), d.get("content_binding")
    except Exception:
        pass
    return None, None

def download_and_upload_one_vid(vid, cid, case_folder_name):
    u = f"https://www.youtube.com/watch?v={vid}"
    tmp_dir = f"/tmp/dl_bw_{cid}_{vid}"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    out_tmpl = os.path.join(tmp_dir, f"%(title).90B [{vid}].%(ext)s")
    try:
        subprocess.run(["pip", "uninstall", "-y", "bgutil-ytdlp-pot-provider"], capture_output=True)
        subprocess.run(["sudo", "docker", "rm", "-f", "bgutil-provider"], capture_output=True)
        ensure_warp_alive()
        # 0. Try Direct (no proxy) and WARP SOCKS5 with 90s timeout per attempt so we never hang 15m
        for att, use_p in [(1, False), (2, True), (3, True)]:
            if use_p:
                ensure_warp_alive()
            cmd_fast = [
                "yt-dlp",
                "-f", "136+140/298+140/137+140/bestvideo[height>=720][height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height>=720]/best",
                "--merge-output-format", "mp4",
                "--concurrent-fragments", "8",
                "--socket-timeout", "15",
                "--retries", "3",
                "--fragment-retries", "3",
                "--no-part",
                "--no-playlist",
                "-o", out_tmpl,
                u
            ]
            if use_p:
                cmd_fast[1:1] = ["--proxy", "socks5://127.0.0.1:40020"]
            log(f"  [{cid}] Starting yt-dlp att={att} proxy={use_p}...")
            push_diag()
            try:
                r_fast = subprocess.run(cmd_fast, capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                log(f"  [{cid}] yt-dlp att={att} proxy={use_p} timed out (180s)")
                push_diag()
                if use_p:
                    start_fresh_warp(att + 1)
                continue
            dl = [x for x in os.listdir(tmp_dir) if x.endswith(".mp4") and not re.search(r'\.f\d+\.', x)]
            if dl:
                fp = os.path.join(tmp_dir, dl[0])
                info = probe_video(fp)
                if info and info["dur"] > 0:
                    sz = os.path.getsize(fp)
                    log(f"  [{cid}] SUCCESS yt-dlp att={att} proxy={use_p} {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                    push_diag()
                    up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                    return up_ok, info["dur"], sz, dl[0]
            else:
                err_lines = [ln for ln in (r_fast.stderr or "").splitlines() if "ERROR:" in ln]
                log(f"  [{cid}] yt-dlp att={att} proxy={use_p} err: {err_lines[-2:]}")
                push_diag()
                if use_p:
                    start_fresh_warp(att + 1)

        api = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
        ios_ua = "com.google.ios.youtube/20.03.02 (iPhone16,2; U; CPU iOS 18_2_1 like Mac OS X;)"

        for rot in range(1, 9):
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
            sdata = d.get("streamingData", {})
            fmts = sdata.get("adaptiveFormats", [])
            v_with_url = [f for f in fmts if "url" in f and "video" in f.get("mimeType", "")]
            a_with_url = [f for f in fmts if "url" in f and "audio" in f.get("mimeType", "")]
            log(f"  [{cid}] rot={rot} IOS status={play_status} v={len(v_with_url)} a={len(a_with_url)}")
            push_diag()

            if play_status == "OK":
                hls_url = sdata.get("hlsManifestUrl")
                raw_title = d.get("videoDetails", {}).get("title") or f"{cid}"
                safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw_title)[:85].strip()
                final_fn = f"{safe_title} [{vid}].mp4"
                merged_fp = os.path.join(tmp_dir, final_fn)
                if hls_url:
                    log(f"  [{cid}] Trying IOS hlsManifestUrl via yt-dlp...")
                    push_diag()
                    try:
                        subprocess.run([
                            "yt-dlp", "--proxy", "socks5://127.0.0.1:40020",
                            "--user-agent", ios_ua,
                            "-f", "bestvideo[height>=720][height<=1920]+bestaudio/best[height>=720][height<=1920]/best",
                            "--merge-output-format", "mp4", "--concurrent-fragments", "4",
                            "--socket-timeout", "20", "--retries", "6", "--fragment-retries", "6", "--no-part",
                            "-o", merged_fp, hls_url
                        ], capture_output=True, text=True, timeout=600)
                    except Exception:
                        pass
                    if os.path.exists(merged_fp):
                        info = probe_video(merged_fp)
                        if info and info["dur"] > 0:
                            sz = os.path.getsize(merged_fp)
                            log(f"  [{cid}] SUCCESS IOS-HLS {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                            push_diag()
                            up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                            return up_ok, info["dur"], sz, final_fn

                # 1. Try Ranged-DL with curl -L (follows 302 redirect!)
                if v_with_url and a_with_url:
                    raw_title = d.get("videoDetails", {}).get("title") or f"{cid}"
                    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw_title)[:85].strip()
                    final_fn = f"{safe_title} [{vid}].mp4"
                    merged_fp = os.path.join(tmp_dir, final_fn)
                    v_720 = [f for f in v_with_url if 720 <= max(int(f.get("width", 0) or 0), int(f.get("height", 0) or 0)) <= 1920]
                    cands = v_720 if v_720 else v_with_url
                    cands.sort(key=lambda f: (1 if "video/mp4" in f.get("mimeType", "") else 0, max(int(f.get("width", 0) or 0), int(f.get("height", 0) or 0)), int(f.get("bitrate", 0) or 0)), reverse=True)
                    best_v = cands[0]
                    a_with_url.sort(key=lambda f: (1 if "audio/mp4" in f.get("mimeType", "") else 0, int(f.get("bitrate", 0) or 0)), reverse=True)
                    best_a = a_with_url[0]
                    v_ext = "mp4" if "mp4" in best_v.get("mimeType", "") else "webm"
                    a_ext = "m4a" if "mp4" in best_a.get("mimeType", "") else "webm"
                    v_tmp = os.path.join(tmp_dir, f"v.{v_ext}")
                    a_tmp = os.path.join(tmp_dir, f"a.{a_ext}")
                    v_ok = ranged_download_stream(best_v["url"], best_v.get("contentLength"), v_tmp, ios_ua)
                    if v_ok:
                        a_ok = ranged_download_stream(best_a["url"], best_a.get("contentLength"), a_tmp, ios_ua)
                        if a_ok:
                            acodec = "copy" if a_ext == "m4a" else "aac"
                            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", v_tmp, "-i", a_tmp, "-c:v", "copy", "-c:a", acodec, merged_fp], timeout=600)
                            if os.path.exists(merged_fp):
                                info = probe_video(merged_fp)
                                if info and info["dur"] > 0:
                                    sz = os.path.getsize(merged_fp)
                                    log(f"  [{cid}] SUCCESS Ranged-DL {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                                    push_diag()
                                    up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                                    return up_ok, info["dur"], sz, final_fn

                # 2. Pre-fetch PO token directly from 127.0.0.1:4416 (no proxy!), then pass static po_token to yt-dlp WITH --proxy socks5://127.0.0.1:40020!
                pot, _ = generate_pot_for_vid(vid)
                log(f"  [{cid}] Pre-fetched PO-Token from localhost:4416: pot_len={len(pot or '')}")
                push_diag()
                for cl in ["mweb", "web", "tv"]:
                    ensure_warp_alive()
                    ext_arg = f"youtube:player_client={cl}"
                    if pot:
                        ext_arg += f"; po_token={cl}.gvs+{pot},{cl}.player+{pot}"
                    cmd = [
                        "yt-dlp",
                        "--proxy", "socks5://127.0.0.1:40020",
                        "--extractor-args", ext_arg,
                        "-f", "bestvideo[height>=720][height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720][height<=2160]+bestaudio/best[height>=720]/best",
                        "--merge-output-format", "mp4", "--concurrent-fragments", "16",
                        "--socket-timeout", "15", "--retries", "4", "--fragment-retries", "4", "--no-part", "--no-playlist", "--no-mtime",
                        "-o", out_tmpl, u
                    ]
                    try:
                        r_yt = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
                    except subprocess.TimeoutExpired:
                        log(f"  [{cid}] WARP+static_pot yt-dlp({cl}) timed out")
                        push_diag()
                        continue
                    dl = [x for x in os.listdir(tmp_dir) if x.endswith(".mp4") and not re.search(r'\.f\d+\.', x)]
                    if dl:
                        fp = os.path.join(tmp_dir, dl[0])
                        info = probe_video(fp)
                        if info and info["dur"] > 0:
                            sz = os.path.getsize(fp)
                            log(f"  [{cid}] SUCCESS WARP+static_pot yt-dlp({cl}) {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> uploading...")
                            push_diag()
                            up_ok = rclone_upload_dir(tmp_dir, case_folder_name)
                            return up_ok, info["dur"], sz, dl[0]
                    else:
                        err_lines = [ln for ln in (r_yt.stderr or "").splitlines() if "ERROR:" in ln or "WARNING:" in ln]
                        log(f"  [{cid}] WARP+static_pot yt-dlp({cl}) err: {err_lines[-2:]}")
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
    vfolder_id = case["drive_videos_folder_id"]
    try:
        r_ls = subprocess.run(["rclone", "lsf", "--drive-root-folder-id", vfolder_id, "--contimeout", "10s", "--timeout", "20s", "--retries", "1", "gdrive_bw2026:"], capture_output=True, text=True, timeout=30)
        existing = [x.strip() for x in r_ls.stdout.splitlines() if x.strip().endswith(".mp4")]
    except Exception:
        existing = []
    if existing:
        log(f"*** SKIPPING {cid} (Already on Drive: {existing}) ***")
        push_diag()
        return
    for vid in case.get("videos", []):
        ok, dur, sz, fn = download_and_upload_one_vid(vid, cid, vfolder_id)
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
