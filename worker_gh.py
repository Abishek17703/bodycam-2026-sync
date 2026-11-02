#!/usr/bin/env python3
import os, sys, json, time, shutil, subprocess, urllib.request, urllib.parse, re, socket
from datetime import datetime

SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))

STAGING_ROOT = f"/tmp/staging_171_s{SHARD_INDEX}"
os.makedirs(STAGING_ROOT, exist_ok=True)
PAYLOAD_FILE = "pure_171_cases_payload.json"
ROOT_2026_FOLDER_ID = "11REjO5KBB5vWD8C6Xfid7HV1fkpWSBIB"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout.write(f"[{ts}][S{SHARD_INDEX}] {msg}\n")
    sys.stdout.flush()

def get_drive_token():
    try:
        rc = open(os.path.expanduser("~/.config/rclone/rclone.conf"), encoding="utf-8").read()
        m_cid = re.search(r'client_id\s*=\s*(\S+)', rc)
        m_sec = re.search(r'client_secret\s*=\s*(\S+)', rc)
        m_tok = re.search(r'"refresh_token"\s*:\s*"([^"]+)"', rc)
        if m_cid and m_sec and m_tok:
            data = urllib.parse.urlencode({
                "client_id": m_cid.group(1),
                "client_secret": m_sec.group(1),
                "refresh_token": m_tok.group(1),
                "grant_type": "refresh_token"
            }).encode("utf-8")
            req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
            return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))["access_token"]
    except Exception:
        pass
    return None

def get_sa_token(idx=0):
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests
        pool_path = os.path.expanduser("~/.config/rclone/sa_pool.json")
        if os.path.exists(pool_path):
            pool = json.load(open(pool_path, encoding="utf-8"))
            if pool:
                info = pool[(SHARD_INDEX + idx) % len(pool)]
                creds = service_account.Credentials.from_service_account_info(
                    info, scopes=["https://www.googleapis.com/auth/drive"]
                )
                creds.refresh(google.auth.transport.requests.Request())
                return creds.token
    except Exception as e:
        log(f"  [SA TOKEN ERR] {e}")
    return None

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

FOLDER_CACHE = {}

def resolve_or_create_folder(parent_id, name):
    key = (parent_id, name)
    if key in FOLDER_CACHE:
        return FOLDER_CACHE[key]
    tok = get_drive_token()
    if not tok:
        return None
    safe_name = name.replace("'", "\\'")
    q = urllib.parse.quote(f"'{parent_id}' in parents and name = '{safe_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    req = urllib.request.Request(f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id,name)", headers={"Authorization": f"Bearer {tok}"})
    files = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8")).get("files", [])
    if files:
        FOLDER_CACHE[key] = files[0]["id"]
        return files[0]["id"]
    meta = json.dumps({"name": name, "parents": [parent_id], "mimeType": "application/vnd.google-apps.folder"}).encode("utf-8")
    req_c = urllib.request.Request("https://www.googleapis.com/drive/v3/files?fields=id", data=meta, method="POST", headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    fid = json.loads(urllib.request.urlopen(req_c, timeout=20).read().decode("utf-8"))["id"]
    FOLDER_CACHE[key] = fid
    return fid

def get_case_videos_folder_id(rel_case_path):
    parts = [p for p in rel_case_path.split("/") if p]
    curr_id = ROOT_2026_FOLDER_ID
    for p in parts:
        curr_id = resolve_or_create_folder(curr_id, p)
        if not curr_id:
            return None
    return resolve_or_create_folder(curr_id, "Videos")

def list_drive_videos(vfolder_id):
    tok = get_drive_token()
    if not tok or not vfolder_id:
        return []
    q = urllib.parse.quote(f"'{vfolder_id}' in parents and trashed = false")
    req = urllib.request.Request(f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id,name,size)&pageSize=200", headers={"Authorization": f"Bearer {tok}"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8")).get("files", [])

def delete_drive_file_id(fid):
    tok = get_drive_token()
    if not tok or not fid:
        return
    try:
        req = urllib.request.Request(f"https://www.googleapis.com/drive/v3/files/{fid}", headers={"Authorization": f"Bearer {tok}"}, method="DELETE")
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass

def sa_upload_file_to_folder(fp, fn, vfolder_id):
    tok = get_drive_token()
    fsize = os.path.getsize(fp)
    if not tok or not vfolder_id:
        return False
    for attempt in range(5):
        try:
            sa_tok = get_sa_token(attempt)
            if sa_tok:
                meta = json.dumps({"name": fn, "parents": [vfolder_id], "mimeType": "video/mp4"}).encode("utf-8")
                req_create = urllib.request.Request(
                    "https://www.googleapis.com/drive/v3/files?fields=id",
                    data=meta, method="POST",
                    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8"}
                )
                file_id = json.loads(urllib.request.urlopen(req_create, timeout=30).read().decode("utf-8"))["id"]
                req_patch = urllib.request.Request(
                    f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=resumable",
                    data=b"{}", method="PATCH",
                    headers={
                        "Authorization": f"Bearer {sa_tok}",
                        "Content-Type": "application/json; charset=UTF-8",
                        "X-Upload-Content-Type": "video/mp4",
                        "X-Upload-Content-Length": str(fsize)
                    }
                )
                resp = urllib.request.urlopen(req_patch, timeout=30)
                upload_url = resp.headers.get("Location")
                if upload_url:
                    up_tmp = f"/tmp/up_payload_{SHARD_INDEX}.mp4"
                    try:
                        os.link(fp, up_tmp)
                    except Exception:
                        shutil.copyfile(fp, up_tmp)
                    r_up = subprocess.run(["curl", "-s", "-g", "--max-time", "900", "-X", "PUT", "-H", "Content-Type: video/mp4", "-T", up_tmp, upload_url], capture_output=True, text=True)
                    try: os.remove(up_tmp)
                    except Exception: pass
                    if r_up.returncode == 0 and '"id"' in (r_up.stdout or ""):
                        return True
                delete_drive_file_id(file_id)
        except Exception as e:
            log(f"  [SA UPLOAD RETRY {attempt+1}] {fn}: {e}")
            time.sleep(3)
    return False

def download_and_upload_one_url(u, idx, vfolder_id, cid):
    m_yt = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', u)
    vid = m_yt.group(1) if m_yt else f"vimeo_{idx}"
    tmp_dir = f"/tmp/dl_{cid}_{idx}"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    out_tmpl = os.path.join(tmp_dir, f"%(title).90B [{vid}].%(ext)s")
    try:
        subprocess.run(["pip", "uninstall", "-y", "bgutil-ytdlp-pot-provider"], capture_output=True)
        ensure_warp_alive()
        # 1. Fast yt-dlp via WARP SOCKS5 (and direct fallback) requiring >=720p HD
        for att, use_p in [(1, True), (2, False), (3, True), (4, True)]:
            if use_p:
                ensure_warp_alive()
            cmd_fast = [
                "yt-dlp",
                "-f", "137+140/299+140/136+140/298+140/bestvideo[height>=720][height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720][height<=2160]+bestaudio/best[height>=720]",
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
            if "vimeo.com" in u:
                cmd_fast[1:1] = ["--referer", "https://www.chicagocopa.org/"]
            if use_p:
                cmd_fast[1:1] = ["--proxy", "socks5://127.0.0.1:40020"]
            log(f"  [{cid}] Starting yt-dlp HD att={att} proxy={use_p} for {vid}...")
            try:
                r_fast = subprocess.run(cmd_fast, capture_output=True, text=True, timeout=240)
            except subprocess.TimeoutExpired:
                log(f"  [{cid}] yt-dlp att={att} timed out (240s)")
                if use_p:
                    start_fresh_warp(att + 1)
                continue
            dl = [x for x in os.listdir(tmp_dir) if x.endswith(".mp4") and not re.search(r'\.f\d+\.', x)]
            if dl:
                fp = os.path.join(tmp_dir, dl[0])
                info = probe_video(fp)
                if info and info["dur"] > 0 and (max(info["w"], info["h"]) >= 1024 or min(info["w"], info["h"]) >= 720):
                    sz = os.path.getsize(fp)
                    log(f"  [{cid}] Downloaded HD {info['w']}x{info['h']} {dl[0]} ({sz/1e6:.1f} MB) -> SA uploading...")
                    up_ok = sa_upload_file_to_folder(fp, dl[0], vfolder_id)
                    return up_ok, info["dur"], sz, dl[0]
                else:
                    try: os.remove(fp)
                    except Exception: pass
            else:
                err_lines = [ln for ln in (r_fast.stderr or "").splitlines() if "ERROR:" in ln]
                log(f"  [{cid}] yt-dlp att={att} err: {err_lines[-1:]}")
                if use_p:
                    start_fresh_warp(att + 1)

        # 2. Fallback: IOS hlsManifestUrl via WARP
        if m_yt:
            api = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
            ios_ua = "com.google.ios.youtube/20.03.02 (iPhone16,2; U; CPU iOS 18_2_1 like Mac OS X;)"
            for rot in range(1, 5):
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
                if r_p.returncode == 0 and r_p.stdout.strip():
                    try:
                        d = json.loads(r_p.stdout)
                        hls_url = d.get("streamingData", {}).get("hlsManifestUrl")
                        if hls_url:
                            raw_title = d.get("videoDetails", {}).get("title") or f"{cid}_clip_{idx+1}"
                            safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw_title)[:85].strip()
                            final_fn = f"{safe_title} [{vid}].mp4"
                            merged_fp = os.path.join(tmp_dir, final_fn)
                            subprocess.run([
                                "yt-dlp", "--proxy", "socks5://127.0.0.1:40020",
                                "--user-agent", ios_ua,
                                "-f", "bestvideo[height>=720][height<=1920]+bestaudio/best[height>=720][height<=1920]",
                                "--merge-output-format", "mp4", "--concurrent-fragments", "4",
                                "--socket-timeout", "20", "--retries", "5", "--fragment-retries", "5", "--no-part",
                                "-o", merged_fp, hls_url
                            ], capture_output=True, text=True, timeout=600)
                            if os.path.exists(merged_fp):
                                info = probe_video(merged_fp)
                                if info and info["dur"] > 0 and (max(info["w"], info["h"]) >= 1024 or min(info["w"], info["h"]) >= 720):
                                    sz = os.path.getsize(merged_fp)
                                    log(f"  [{cid}] Downloaded IOS-HLS HD {info['w']}x{info['h']} ({sz/1e6:.1f} MB) -> SA uploading...")
                                    up_ok = sa_upload_file_to_folder(merged_fp, final_fn, vfolder_id)
                                    return up_ok, info["dur"], sz, final_fn
                    except Exception:
                        pass
                start_fresh_warp(rot + 10)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    log(f"  [{cid}] FAILED URL: {u[:80]}")
    return False, 0.0, 0, ""

LOW_RES_VIDS = set()
if os.path.exists("low_res_clips.json"):
    try:
        for item in json.load(open("low_res_clips.json", encoding="utf-8")):
            m = re.search(r'\[([A-Za-z0-9_-]{11})\]\.mp4$', item.get("file_name", ""))
            if m:
                LOW_RES_VIDS.add(m.group(1))
    except Exception:
        pass

def process_case(case):
    cid = case["case_id"]
    # Fast check: does this case have any LOW_RES_VIDS before making Drive API calls?
    needs_upgrade = []
    for u in case["video_urls"]:
        m_yt = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', u)
        if m_yt and m_yt.group(1) in LOW_RES_VIDS:
            needs_upgrade.append(m_yt.group(1))

    if not needs_upgrade:
        return

    rel_case_path = f"{case['category']}/{case['folder']}"
    vfolder_id = get_case_videos_folder_id(rel_case_path)
    if not vfolder_id:
        log(f"  [{cid}] Could not resolve Videos folder for {rel_case_path}")
        return

    drive_files = list_drive_videos(vfolder_id)
    existing_drive_files = [x["name"] for x in drive_files if x["name"].endswith(".mp4")]
    log(f"=== Upgrading {cid}: {case['folder']} (Upgrading {len(needs_upgrade)} clips: {needs_upgrade}) ===")

    for j, u in enumerate(case["video_urls"]):
        m_yt = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', u)
        if not m_yt or m_yt.group(1) not in LOW_RES_VIDS:
            continue
        vid = m_yt.group(1)
        old_files = [x for x in drive_files if f"[{vid}]" in x["name"]]
        ok, dur, sz, uploaded_fn = download_and_upload_one_url(u, j, vfolder_id, cid)
        if ok:
            for of in old_files:
                delete_drive_file_id(of["id"])
            log(f"  [HD UPGRADE COMPLETE] {cid} [{vid}] -> {uploaded_fn} ({sz/1e6:.1f} MB)")

def main():
    cases = json.load(open(PAYLOAD_FILE, encoding="utf-8"))
    # Filter only cases that have a clip in LOW_RES_VIDS and distribute evenly across shards!
    upgrade_cases = []
    for c in cases:
        if any(re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', u) and re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', u).group(1) in LOW_RES_VIDS for u in c["video_urls"]):
            upgrade_cases.append(c)
    my_cases = [c for idx, c in enumerate(upgrade_cases) if idx % SHARD_TOTAL == SHARD_INDEX]
    log(f"Starting 2026 HD Upgrade Shard {SHARD_INDEX}/{SHARD_TOTAL} ({len(my_cases)}/{len(upgrade_cases)} upgrade cases assigned)...")
    for c in my_cases:
        process_case(c)
    log(f"Shard {SHARD_INDEX} finished all assigned 2026 HD upgrades.")

if __name__ == "__main__":
    main()
