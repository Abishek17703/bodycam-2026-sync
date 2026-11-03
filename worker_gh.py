#!/usr/bin/env python3
import os, sys, json, time, shutil, subprocess, urllib.request, urllib.parse, re, socket
from datetime import datetime

SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))

STAGING_ROOT = f"/tmp/staging_portal_a_s{SHARD_INDEX}"
os.makedirs(STAGING_ROOT, exist_ok=True)
PAYLOAD_FILE = "portal_both_payload.json"
ROOT_2026_FOLDER_ID = "11REjO5KBB5vWD8C6Xfid7HV1fkpWSBIB"

# Strictly use bodycamfoia948@gmail.com (owner of 2026 Cases 11REjO5KBB5vWD8C6Xfid7HV1fkpWSBIB)
# so ZERO bytes ever touch abishek17703@gmail.com!
CLIENT_ID = os.environ.get("OWNER_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("OWNER_CLIENT_SECRET", "")
REFRESH_TOKEN_2026_OWNER = os.environ.get("OWNER_REFRESH_TOKEN", "")

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout.write(f"[{ts}][PORTAL-A-S{SHARD_INDEX}] {msg}\n")
    sys.stdout.flush()

def get_drive_token():
    for _ in range(3):
        try:
            data = urllib.parse.urlencode({
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": REFRESH_TOKEN_2026_OWNER,
                "grant_type": "refresh_token"
            }).encode("utf-8")
            req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
            return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))["access_token"]
        except Exception as e:
            time.sleep(2)
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
    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
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

def get_case_subfolders(case):
    cat_id = resolve_or_create_folder(ROOT_2026_FOLDER_ID, case["category"])
    if not cat_id:
        return None, None
    case_id = resolve_or_create_folder(cat_id, case["folder"])
    if not case_id:
        return None, None
    v_id = resolve_or_create_folder(case_id, "Videos")
    d_id = resolve_or_create_folder(case_id, "Documents")
    return v_id, d_id

def list_drive_folder(folder_id):
    tok = get_drive_token()
    if not tok or not folder_id:
        return []
    q = urllib.parse.quote(f"'{folder_id}' in parents and trashed = false")
    req = urllib.request.Request(f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id,name,size)&pageSize=300", headers={"Authorization": f"Bearer {tok}"})
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

def sa_upload_file_to_folder(fp, fn, folder_id, mime_type="video/mp4"):
    tok = get_drive_token()
    fsize = os.path.getsize(fp)
    if not tok or not folder_id or fsize <= 0:
        return False
    # Check if already uploaded with non-zero size
    existing = list_drive_folder(folder_id)
    for ef in existing:
        if ef["name"] == fn and int(ef.get("size", 0)) >= fsize * 0.95:
            log(f"  [SKIP EXISTING] {fn} ({fsize/1e6:.1f} MB)")
            return True
    for attempt in range(5):
        try:
            sa_tok = get_sa_token(attempt)
            if sa_tok:
                # 1. Create 0-byte placeholder owned by bodycamfoia948@gmail.com
                meta = json.dumps({"name": fn, "parents": [folder_id], "mimeType": mime_type}).encode("utf-8")
                req_create = urllib.request.Request(
                    "https://www.googleapis.com/drive/v3/files?fields=id",
                    data=meta, method="POST",
                    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8"}
                )
                file_id = json.loads(urllib.request.urlopen(req_create, timeout=30).read().decode("utf-8"))["id"]
                # 2. Stream media via Service Account Pool PATCH uploadType=resumable
                req_patch = urllib.request.Request(
                    f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=resumable",
                    data=b"{}", method="PATCH",
                    headers={
                        "Authorization": f"Bearer {sa_tok}",
                        "Content-Type": "application/json; charset=UTF-8",
                        "X-Upload-Content-Type": mime_type,
                        "X-Upload-Content-Length": str(fsize)
                    }
                )
                resp = urllib.request.urlopen(req_patch, timeout=30)
                upload_url = resp.headers.get("Location")
                if upload_url:
                    up_tmp = f"/tmp/up_pa_{SHARD_INDEX}_{attempt}.bin"
                    try:
                        os.link(fp, up_tmp)
                    except Exception:
                        shutil.copyfile(fp, up_tmp)
                    r_up = subprocess.run([
                        "curl", "-s", "-g", "--max-time", "1800", "-X", "PUT",
                        "-H", f"Content-Type: {mime_type}", "-T", up_tmp, upload_url
                    ], capture_output=True, text=True)
                    try: os.remove(up_tmp)
                    except Exception: pass
                    if r_up.returncode == 0 and '"id"' in (r_up.stdout or ""):
                        return True
                delete_drive_file_id(file_id)
        except Exception as e:
            log(f"  [SA UPLOAD RETRY {attempt+1}] {fn}: {e}")
            time.sleep(3)
    return False

def download_and_upload_pdf(p_obj, dfolder_id, cid):
    fn = p_obj["filename"]
    url = p_obj["url"].replace(" ", "%20")
    existing = list_drive_folder(dfolder_id)
    for ef in existing:
        if ef["name"] == fn and int(ef.get("size", 0)) > 1000:
            return True
    tmp_fp = os.path.join(STAGING_ROOT, f"pdf_{SHARD_INDEX}_{re.sub(r'[^A-Za-z0-9_.-]', '_', fn)}")
    try:
        r = subprocess.run(["curl", "-sL", "--max-time", "180", "-A", "Mozilla/5.0", "-o", tmp_fp, url], capture_output=True)
        if os.path.exists(tmp_fp) and os.path.getsize(tmp_fp) > 500:
            ok = sa_upload_file_to_folder(tmp_fp, fn, dfolder_id, mime_type="application/pdf")
            if ok:
                log(f"  [{cid}] Uploaded PDF: {fn} ({os.path.getsize(tmp_fp)/1e3:.1f} KB)")
            return ok
    finally:
        try: os.remove(tmp_fp)
        except Exception: pass
    return False

def download_and_upload_portal_video(v_obj, idx, vfolder_id, cid):
    fn = v_obj["filename"]
    url = v_obj["url"]
    stype = v_obj.get("source_type", "direct_mp4")
    existing = list_drive_folder(vfolder_id)
    for ef in existing:
        if ef["name"] == fn and int(ef.get("size", 0)) > 1000000:
            log(f"  [{cid}] Video already on Drive: {fn} ({int(ef['size'])/1e6:.1f} MB)")
            return True

    tmp_dir = os.path.join(STAGING_ROOT, f"v_{cid}_{idx}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    out_fp = os.path.join(tmp_dir, fn)
    try:
        if stype == "vimeo_copa" or "vimeo.com" in url:
            cmd = [
                "yt-dlp",
                "--referer", v_obj.get("referer", "https://www.chicagocopa.org/"),
                "-f", "bestvideo[height>=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720]+bestaudio/best[height>=720]/best",
                "--merge-output-format", "mp4",
                "--concurrent-fragments", "16",
                "--socket-timeout", "20",
                "--retries", "5",
                "--fragment-retries", "5",
                "--no-part",
                "-o", out_fp,
                url
            ]
            log(f"  [{cid}] Downloading COPA Vimeo HD: {fn} ({url})...")
            subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        elif stype == "njoag_box" or "box.com" in url:
            box_u = v_obj.get("box_file_url") or url
            cmd = [
                "yt-dlp",
                "-f", "bestvideo[height>=720]+bestaudio/best",
                "--merge-output-format", "mp4",
                "--concurrent-fragments", "10",
                "-o", out_fp,
                box_u
            ]
            log(f"  [{cid}] Downloading NJOAG Box HD: {fn} ({box_u})...")
            subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
            if not os.path.exists(out_fp) or os.path.getsize(out_fp) < 100000:
                subprocess.run(["curl", "-sL", "--max-time", "1200", "-A", "Mozilla/5.0", "-o", out_fp, url], capture_output=True)
        else:
            clean_u = url.replace(" ", "%20")
            log(f"  [{cid}] Downloading Direct Portal MP4: {fn} ({clean_u[:80]})...")
            r_aria = subprocess.run([
                "aria2c", "-x", "8", "-s", "8", "-k", "1M",
                "--max-connection-per-server=8", "--allow-overwrite=true",
                "--auto-file-renaming=false", "-d", tmp_dir, "-o", fn, clean_u
            ], capture_output=True, text=True, timeout=1800)
            if not os.path.exists(out_fp) or os.path.getsize(out_fp) < 100000:
                subprocess.run(["curl", "-sL", "--max-time", "1800", "-A", "Mozilla/5.0", "-o", out_fp, clean_u], capture_output=True)

        if os.path.exists(out_fp) and os.path.getsize(out_fp) > 100000:
            info = probe_video(out_fp)
            sz = os.path.getsize(out_fp)
            if info and (max(info["w"], info["h"]) >= 1200 or min(info["w"], info["h"]) >= 720):
                log(f"  [{cid}] Probed HD {info['w']}x{info['h']} ({info['dur']:.1f}s, {sz/1e6:.1f} MB) -> SA uploading {fn}...")
                up_ok = sa_upload_file_to_folder(out_fp, fn, vfolder_id, mime_type="video/mp4")
                if up_ok:
                    log(f"  [PORTAL-A UPLOAD COMPLETE] {cid} -> {fn} ({sz/1e6:.1f} MB)")
                return up_ok
            else:
                log(f"  [{cid}] Rejecting non-HD or corrupt file {fn}: {info}")
    except Exception as e:
        log(f"  [{cid}] Error downloading {fn}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return False

def main():
    payload = json.load(open(PAYLOAD_FILE, encoding="utf-8"))
    cases = payload.get("target_a_2026_cases", [])
    log(f"Loaded {len(cases)} Target A (2026 Cases) pure portal cases.")

    # Step 1: Pre-create case folders & upload PDFs assigned to this shard
    pdf_tasks = []
    for c in cases:
        for p in c.get("pdfs", []):
            pdf_tasks.append((c, p))
    my_pdfs = [t for i, t in enumerate(pdf_tasks) if i % SHARD_TOTAL == SHARD_INDEX]
    log(f"Shard {SHARD_INDEX}/{SHARD_TOTAL} assigned {len(my_pdfs)}/{len(pdf_tasks)} PDFs...")
    for c, p in my_pdfs:
        _, dfolder_id = get_case_subfolders(c)
        if dfolder_id:
            download_and_upload_pdf(p, dfolder_id, c["case_id"])

    # Step 2: Flatten all 217 portal videos across all 20 shards so multi-camera cases download in parallel!
    vid_tasks = []
    for c in cases:
        for idx, v in enumerate(c.get("videos", []), 1):
            vid_tasks.append((c, idx, v))
    my_vids = [t for i, t in enumerate(vid_tasks) if i % SHARD_TOTAL == SHARD_INDEX]
    log(f"Shard {SHARD_INDEX}/{SHARD_TOTAL} assigned {len(my_vids)}/{len(vid_tasks)} portal >=720p BWC clips...")

    for c, idx, v in my_vids:
        vfolder_id, _ = get_case_subfolders(c)
        if vfolder_id:
            download_and_upload_portal_video(v, idx, vfolder_id, c["case_id"])

    log(f"Shard {SHARD_INDEX}/{SHARD_TOTAL} completed all Target A (2026 Cases) pure portal uploads.")

if __name__ == "__main__":
    main()
