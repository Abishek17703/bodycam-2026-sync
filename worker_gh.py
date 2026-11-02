#!/usr/bin/env python3
import os, sys, json, time, shutil, subprocess, urllib.request, urllib.parse, re, threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))

STAGING_ROOT = f"/tmp/staging_171_s{SHARD_INDEX}"
os.makedirs(STAGING_ROOT, exist_ok=True)
PAYLOAD_FILE = "pure_171_cases_payload.json"

log_lock = threading.Lock()
disk_sem = threading.Semaphore(2)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_lock:
        sys.stdout.write(f"[{ts}][S{SHARD_INDEX}] {msg}\n")
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

def rclone_upload_dir(local_dir, remote_subpath):
    dest = f"gdrive2026_cases:{remote_subpath}"
    for attempt in range(3):
        r = subprocess.run([
            "rclone", "copy", local_dir, dest,
            "--transfers", "8", "--checkers", "8", "--drive-chunk-size", "128M", "--fast-list"
        ], capture_output=True, text=True)
        if r.returncode == 0:
            return True
        else:
            log(f"  [RCLONE RETRY {attempt+1}] {dest}: {r.stderr.strip()[:160]}")
            time.sleep(2)
    return False

def download_and_upload_one_url(u, idx, vids_dir, rel_case_path, cid):
    m_yt = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', u)
    if not m_yt and "vimeo.com" not in u:
        clean_u = u.replace("?dl=0", "?dl=1")
        parsed = urllib.parse.urlsplit(clean_u)
        safe_path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/+-.,_()")
        safe_u = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, safe_path, parsed.query, ""))
        fn = urllib.parse.unquote(parsed.path.split("/")[-1]).replace("+", " ")
        fn = re.sub(r'[<>:"/\\|?*]', '_', fn)
        if not fn.lower().endswith(".mp4"):
            fn = f"{cid}_clip_{idx+1}.mp4"
        else:
            fn = f"{idx+1:02d}_{fn}"

        with disk_sem:
            tmp_dir = f"/tmp/dl_{cid}_{idx}"
            shutil.rmtree(tmp_dir, ignore_errors=True)
            os.makedirs(tmp_dir, exist_ok=True)
            fp = os.path.join(tmp_dir, fn)
            try:
                subprocess.run(["curl", "-sL", "--retry", "5", "-A", "Mozilla/5.0", "-o", fp, safe_u], timeout=2400)
                if os.path.exists(fp) and os.path.getsize(fp) > 10000:
                    info = probe_video(fp)
                    dur = info["dur"] if info else 0.0
                    sz = os.path.getsize(fp)
                    dst_fp = os.path.join(vids_dir, fn)
                    shutil.move(fp, dst_fp)
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    log(f"  [{cid}] Downloaded Direct MP4 {fn} ({sz/1e6:.1f} MB, {dur/60:.1f}m)")
                    up_ok = rclone_upload_dir(vids_dir, f"{rel_case_path}/Videos")
                    try: os.remove(dst_fp)
                    except Exception: pass
                    return up_ok, dur, sz
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        log(f"  [{cid}] FAILED Direct URL: {safe_u[:90]}")
        return False, 0.0, 0

    vid = m_yt.group(1) if m_yt else f"vimeo_{idx}"
    with disk_sem:
        tmp_dir = f"/tmp/dl_{cid}_{idx}"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(tmp_dir, exist_ok=True)
        out_tmpl = os.path.join(tmp_dir, f"%(title).90B [{vid}].%(ext)s")
        try:
            for attempt in range(4):
                cmd = [
                    "yt-dlp",
                    "-f", "136+140/298+140/18/bestvideo[height>=720][height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height>=720]/best",
                    "--merge-output-format", "mp4", "--concurrent-fragments", "16",
                    "--retries", "10", "--fragment-retries", "10", "--no-part", "--no-playlist",
                    "-o", out_tmpl, u
                ]
                if "vimeo.com" in u:
                    cmd[1:1] = ["--referer", "https://www.chicagocopa.org/"]
                if m_yt:
                    clients = ["ios,web", "android,web", "tv,web", "mweb,web"]
                    cmd[1:1] = ["--extractor-args", f"youtube:player_client={clients[attempt % len(clients)]}"]
                r_yt = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                dl = [x for x in os.listdir(tmp_dir) if x.endswith(".mp4") and not re.search(r'\.f\d+\.', x)]
                if dl:
                    fp = os.path.join(tmp_dir, dl[0])
                    info = probe_video(fp)
                    if info and info["dur"] > 0:
                        sz = os.path.getsize(fp)
                        dst_fp = os.path.join(vids_dir, dl[0])
                        shutil.move(fp, dst_fp)
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                        log(f"  [{cid}] Downloaded {dl[0]} ({sz/1e6:.1f} MB, {info['dur']/60:.1f}m)")
                        up_ok = rclone_upload_dir(vids_dir, f"{rel_case_path}/Videos")
                        try: os.remove(dst_fp)
                        except Exception: pass
                        return up_ok, info["dur"], sz
                else:
                    err_snip = (r_yt.stderr or "").strip().splitlines()[-1:]
                    log(f"  [{cid}] yt-dlp attempt {attempt+1} err: {err_snip}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    log(f"  [{cid}] FAILED URL: {u[:80]}")
    return False, 0.0, 0

def process_case(case):
    cid = case["case_id"]
    rel_case_path = f"{case['category']}/{case['folder']}"
    r_ls = subprocess.run(["rclone", "lsf", f"gdrive2026_cases:{rel_case_path}/Videos"], capture_output=True, text=True)
    existing_drive_files = [x.strip() for x in r_ls.stdout.splitlines() if x.strip().endswith(".mp4")]
    if len(existing_drive_files) >= len(case["video_urls"]):
        log(f"*** SKIPPING {cid} (Already 100% on Drive: {len(existing_drive_files)} clips) ***")
        return

    case_dir = os.path.join(STAGING_ROOT, f"{cid}_stage")
    vids_dir = os.path.join(case_dir, "Videos")
    docs_dir = os.path.join(case_dir, "Documents")
    os.makedirs(vids_dir, exist_ok=True)
    os.makedirs(docs_dir, exist_ok=True)

    summary_fp = os.path.join(docs_dir, "Official_Case_Documentation_Summary.txt")
    with open(summary_fp, "w", encoding="utf-8") as f:
        f.write(case["summary"])

    for p_idx, p_url in enumerate(case.get("pdf_urls", [])):
        if p_url.lower().endswith(".pdf") or "dl=1" in p_url.lower():
            parsed_p = urllib.parse.urlsplit(p_url)
            safe_p_path = urllib.parse.quote(urllib.parse.unquote(parsed_p.path), safe="/+-.,_()")
            safe_p_url = urllib.parse.urlunsplit((parsed_p.scheme, parsed_p.netloc, safe_p_path, parsed_p.query, ""))
            p_fn = urllib.parse.unquote(parsed_p.path.split("/")[-1]).replace("+", " ")
            p_fn = re.sub(r'[<>:"/\\|?*]', '_', p_fn)
            if not p_fn.lower().endswith(".pdf"):
                p_fn = f"Case_Document_{p_idx+1}.pdf"
            dst_p = os.path.join(docs_dir, p_fn)
            if not os.path.exists(dst_p):
                subprocess.run(["curl", "-sL", "-A", "Mozilla/5.0", "-o", dst_p, safe_p_url], timeout=180)

    rclone_upload_dir(docs_dir, f"{rel_case_path}/Documents")
    shutil.rmtree(docs_dir, ignore_errors=True)
    log(f"=== Started {cid}: {case['folder']} (Existing clips on Drive={len(existing_drive_files)}/{len(case['video_urls'])}) ===")

    v_cnt = len(existing_drive_files)
    for j, u in enumerate(case["video_urls"]):
        m_yt = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', u)
        if m_yt and any(f"[{m_yt.group(1)}]" in ef for ef in existing_drive_files):
            continue
        if not m_yt and "vimeo.com" not in u:
            clean_u = u.replace("?dl=0", "?dl=1")
            parsed = urllib.parse.urlsplit(clean_u)
            raw_fn = urllib.parse.unquote(parsed.path.split("/")[-1]).replace("+", " ")
            raw_fn = re.sub(r'[<>:"/\\|?*]', '_', raw_fn)
            if any(raw_fn in ef for ef in existing_drive_files):
                continue
        ok, dur, sz = download_and_upload_one_url(u, j, vids_dir, rel_case_path, cid)
        if ok:
            v_cnt += 1
            log(f"  [DRIVE UPLOADED] {cid} clip {v_cnt}/{len(case['video_urls'])} ({sz/1e9:.2f} GB)")

    shutil.rmtree(case_dir, ignore_errors=True)
    if v_cnt >= len(case["video_urls"]):
        log(f"*** COMPLETED & UPLOADED {cid}: {v_cnt}/{len(case['video_urls'])} clips ***")

def main():
    cases = json.load(open(PAYLOAD_FILE, encoding="utf-8"))
    cases.sort(key=lambda c: (0 if any("sandiego.gov" in u or "dropbox.com" in u or "archive.org" in u for u in c["video_urls"]) else (2 if any("vimeo.com" in u for u in c["video_urls"]) else 1), c["case_id"]))
    my_cases = [c for idx, c in enumerate(cases) if idx % SHARD_TOTAL == SHARD_INDEX]
    log(f"Starting Clean GitHub Actions Worker Shard {SHARD_INDEX}/{SHARD_TOTAL} ({len(my_cases)} assigned cases)...")
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(process_case, c) for c in my_cases]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                log(f"Worker exception: {e}")

if __name__ == "__main__":
    main()
