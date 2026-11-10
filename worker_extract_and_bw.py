#!/usr/bin/env python3
import os, sys, json, time, shutil, subprocess, urllib.request, urllib.parse, re, base64, http.cookiejar
from datetime import datetime

SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))

STAGING_ROOT = f"/tmp/staging_extract_wc_s{SHARD_INDEX}"
os.makedirs(STAGING_ROOT, exist_ok=True)
PAYLOAD_FILE = "extract_and_bw_payload.json"

CLIENT_ID = os.environ.get("OWNER_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("OWNER_CLIENT_SECRET", "")
REFRESH_TOKEN_BW_OWNER = os.environ.get("OWNER_REFRESH_TOKEN", "")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout.write(f"[{ts}][EXTRACT-BW-S{SHARD_INDEX}] {msg}\n")
    sys.stdout.flush()

def get_drive_token():
    for _ in range(3):
        try:
            data = urllib.parse.urlencode({
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": REFRESH_TOKEN_BW_OWNER,
                "grant_type": "refresh_token"
            }).encode("utf-8")
            req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
            return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))["access_token"]
        except Exception:
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

def list_drive_folder(folder_id):
    tok = get_drive_token()
    if not tok or not folder_id:
        return []
    q = urllib.parse.quote(f"'{folder_id}' in parents and trashed = false")
    req = urllib.request.Request(f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id,name,size,mimeType)&pageSize=300", headers={"Authorization": f"Bearer {tok}"})
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

def cleanup_shortcuts_if_matched(folder_id, fn):
    existing = list_drive_folder(folder_id)
    for ef in existing:
        if ef.get("mimeType") == "application/vnd.google-apps.shortcut" and (ef.get("name") == fn or os.path.splitext(ef.get("name", ""))[0] == os.path.splitext(fn)[0]):
            log(f"  [CLEANUP SHORTCUT] Deleting shortcut {ef.get('name')} ({ef['id']}) in {folder_id}")
            delete_drive_file_id(ef["id"])

def sa_upload_file_to_folder(fp, fn, folder_id, mime_type="video/mp4"):
    tok = get_drive_token()
    fsize = os.path.getsize(fp)
    if not tok or not folder_id or fsize <= 0:
        return False
    existing = list_drive_folder(folder_id)
    for ef in existing:
        if ef.get("mimeType") != "application/vnd.google-apps.shortcut" and ef["name"].lower() == fn.lower() and int(ef.get("size", 0)) >= max(10000, fsize * 0.85):
            log(f"  [SKIP EXISTING] {fn} ({int(ef.get('size',0))/1e6:.1f} MB)")
            cleanup_shortcuts_if_matched(folder_id, fn)
            return True

    for attempt in range(5):
        try:
            sa_tok = get_sa_token(attempt)
            if sa_tok:
                meta = json.dumps({"name": fn, "parents": [folder_id], "mimeType": mime_type}).encode("utf-8")
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
                        "X-Upload-Content-Type": mime_type,
                        "X-Upload-Content-Length": str(fsize)
                    }
                )
                resp = urllib.request.urlopen(req_patch, timeout=30)
                upload_url = resp.headers.get("Location")
                if upload_url:
                    up_tmp = f"/tmp/up_ex_{SHARD_INDEX}_{attempt}.bin"
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
                        cleanup_shortcuts_if_matched(folder_id, fn)
                        return True
                delete_drive_file_id(file_id)
        except Exception as e:
            log(f"  [SA UPLOAD RETRY {attempt+1}] {fn}: {e}")
            time.sleep(3)
    return False

def extract_fresh_dropbox_hls(page_url):
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        h = opener.open(urllib.request.Request(page_url, headers={"User-Agent": UA}), timeout=25).read().decode("utf-8", "ignore")
        m_direct = re.findall(r'https://[^\s"\'\\<>]+hls_master_playlist[^\s"\'\\<>]+', h)
        candidates = list(m_direct)
        b64s = re.findall(r'registerStreamedPrefetch\s*\(\s*"[^"]+"\s*,\s*"([^"]+)"', h)
        for b in b64s:
            try:
                raw = base64.b64decode(b).decode("latin1")
                urls = re.findall(r'https://[A-Za-z0-9._/-]+hls_master_playlist/[A-Za-z0-9._/-]+/p\.m3u8', raw)
                candidates.extend(urls)
            except Exception:
                pass
        for u in candidates:
            try:
                resp = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=10)
                body = resp.read().decode("utf-8", "ignore")
                if body.startswith("#EXTM3U"):
                    return u
            except Exception:
                pass
        if candidates:
            return candidates[0]
    except Exception as e:
        log(f"  [DBX HLS EXTRACT ERR] {page_url}: {e}")
    return None

def download_gdrive_authenticated_gvi(fid, out_fp):
    tok = get_drive_token()
    if not tok:
        return False
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        ep = f"https://drive.google.com/u/0/get_video_info?docid={fid}&drive_originator_app=303"
        headers = {"User-Agent": UA, "Authorization": f"Bearer {tok}"}
        body = opener.open(urllib.request.Request(ep, headers=headers), timeout=20).read().decode("utf-8", "ignore")
        parsed = urllib.parse.parse_qs(body)
        if parsed.get("status", [""])[0] != "ok":
            log(f"    [GVI-303] status={parsed.get('status')} reason={parsed.get('reason')}")
            return False
        stream_url = None
        if "fmt_stream_map" in parsed:
            by_itag = {}
            for p in parsed["fmt_stream_map"][0].split(","):
                if "|" in p:
                    it, u = p.split("|", 1)
                    by_itag[it] = u
            for pref in ("37", "22", "18", "59", "43"):
                if pref in by_itag:
                    stream_url = by_itag[pref]
                    break
            if not stream_url and by_itag:
                stream_url = list(by_itag.values())[0]
        if not stream_url and "player_response" in parsed:
            pr = json.loads(parsed["player_response"][0])
            sd = pr.get("streamingData", {})
            all_fmts = sd.get("formats", []) + sd.get("adaptiveFormats", [])
            for fmt in all_fmts:
                if fmt.get("itag") == 140 and fmt.get("url"):
                    stream_url = fmt["url"]
                    break
            if not stream_url and all_fmts:
                stream_url = all_fmts[-1].get("url")
        if not stream_url:
            return False
        log(f"    [GVI-303] Streaming authenticated playback URL for {fid} via 16-conn aria2c...")
        cookie_str = "; ".join([f"{c.name}={c.value}" for c in cj])
        cmd_aria = [
            "aria2c",
            "-x", "16",
            "-s", "16",
            "-k", "1M",
            "--header", f"User-Agent: {UA}",
            "--header", f"Authorization: Bearer {tok}",
            "--header", f"Cookie: {cookie_str}",
            "-d", os.path.dirname(out_fp),
            "-o", os.path.basename(out_fp),
            stream_url
        ]
        subprocess.run(cmd_aria, capture_output=True, text=True, timeout=600)
        return os.path.exists(out_fp) and os.path.getsize(out_fp) > 5000
    except Exception as e:
        log(f"    [GVI-303 ERR] {fid}: {e}")
        return False

def process_wc_gdrive(t, idx):
    folder_id = t["target_folder_id"]
    fn = t["filename"]
    fid = t["source_file_id"]
    mime = t.get("mimeType", "video/mp4")

    existing = list_drive_folder(folder_id)
    for ef in existing:
        if ef.get("mimeType") != "application/vnd.google-apps.shortcut" and ef["name"].lower() == fn.lower() and int(ef.get("size", 0)) > 10000:
            log(f"  [Row {t['row']}] Already uploaded: {fn} ({int(ef['size'])/1e6:.1f} MB)")
            cleanup_shortcuts_if_matched(folder_id, fn)
            return True

    tmp_dir = os.path.join(STAGING_ROOT, f"gd_{idx}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    out_fp = os.path.join(tmp_dir, fn)

    try:
        log(f"  [Row {t['row']}] Downloading GDrive view-only via authenticated GVI-303: {fn} ({fid})...")
        ok_gvi = download_gdrive_authenticated_gvi(fid, out_fp)
        if not ok_gvi:
            view_url = f"https://drive.google.com/file/d/{fid}/view"
            cmd = [
                "yt-dlp",
                "--concurrent-fragments", "16",
                "--socket-timeout", "30",
                "--retries", "5",
                "--no-part",
                "-o", out_fp,
                view_url
            ]
            if mime.startswith("video/"):
                cmd[1:1] = ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]
            subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

        if not os.path.exists(out_fp):
            cand = [os.path.join(tmp_dir, x) for x in os.listdir(tmp_dir) if os.path.isfile(os.path.join(tmp_dir, x))]
            if cand:
                out_fp = cand[0]

        if os.path.exists(out_fp) and os.path.getsize(out_fp) >= 5000:
            sz = os.path.getsize(out_fp)
            log(f"  [Row {t['row']}] Downloaded {fn} ({sz/1e6:.1f} MB) -> SA uploading to {t['folder_name']}...")
            up_ok = sa_upload_file_to_folder(out_fp, fn, folder_id, mime_type=mime)
            if up_ok:
                log(f"  [WC-GDRIVE COMPLETE] Row {t['row']} -> {fn} ({sz/1e6:.1f} MB)")
            return up_ok
        else:
            log(f"  [Row {t['row']}] Failed to download {fn} ({fid})")
    except Exception as e:
        log(f"  [Row {t['row']}] Error processing {fn}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return False

def process_wc_dropbox(t, idx):
    folder_id = t["target_folder_id"]
    fn = t["filename"]
    page_url = t["page_url"]
    raw_url = t.get("raw_url", "")

    existing = list_drive_folder(folder_id)
    for ef in existing:
        if ef.get("mimeType") != "application/vnd.google-apps.shortcut" and ef["name"].lower() == fn.lower() and int(ef.get("size", 0)) > 1000:
            log(f"  [Row {t['row']}] Already uploaded: {fn} ({int(ef['size'])/1e6:.1f} MB)")
            return True

    tmp_dir = os.path.join(STAGING_ROOT, f"dbx_{idx}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    out_fp = os.path.join(tmp_dir, fn)

    try:
        hls_url = extract_fresh_dropbox_hls(page_url)
        if hls_url:
            log(f"  [Row {t['row']}] Downloading Dropbox HLS via yt-dlp: {fn}...")
            cmd = [
                "yt-dlp",
                "-f", "bestvideo+bestaudio/best",
                "--merge-output-format", "mp4",
                "--concurrent-fragments", "16",
                "--socket-timeout", "30",
                "--retries", "5",
                "--no-part",
                "-o", out_fp,
                hls_url
            ]
            subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

        if not os.path.exists(out_fp) and raw_url:
            subprocess.run(["curl", "-sL", "--max-time", "300", "-A", UA, "-o", out_fp, raw_url], capture_output=True)

        if not os.path.exists(out_fp):
            cand = [os.path.join(tmp_dir, x) for x in os.listdir(tmp_dir) if os.path.isfile(os.path.join(tmp_dir, x))]
            if cand:
                out_fp = cand[0]

        if os.path.exists(out_fp) and os.path.getsize(out_fp) >= 5000:
            with open(out_fp, "rb") as fchk:
                head_bytes = fchk.read(64)
            if not head_bytes.lstrip().startswith((b"<!DOCTYPE", b"<html")):
                sz = os.path.getsize(out_fp)
                log(f"  [Row {t['row']}] Downloaded Dropbox file {fn} ({sz/1e6:.1f} MB) -> SA uploading to {t['folder_name']}...")
                up_ok = sa_upload_file_to_folder(out_fp, fn, folder_id, mime_type="video/mp4")
                if up_ok:
                    log(f"  [WC-DROPBOX COMPLETE] Row {t['row']} -> {fn} ({sz/1e6:.1f} MB)")
                return up_ok
        log(f"  [Row {t['row']}] Could not extract stream for {fn} ({page_url})")
    except Exception as e:
        log(f"  [Row {t['row']}] Error processing Dropbox {fn}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return False

def process_copa_vimeo(t, idx):
    folder_id = t["target_folder_id"]
    fn = t["filename"]
    vid = t["vimeo_id"]

    existing = list_drive_folder(folder_id)
    for ef in existing:
        if ef.get("mimeType") != "application/vnd.google-apps.shortcut" and ef["name"].lower() == fn.lower() and int(ef.get("size", 0)) > 10000:
            log(f"  [COPA-VIMEO] Already uploaded: {fn} ({int(ef['size'])/1e6:.1f} MB)")
            return True

    tmp_dir = os.path.join(STAGING_ROOT, f"vimeo_{idx}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    out_fp = os.path.join(tmp_dir, fn)

    try:
        v_url = f"https://player.vimeo.com/video/{vid}"
        log(f"  [COPA-VIMEO] Downloading {v_url} -> {fn}...")
        cmd = [
            "yt-dlp",
            "--referer", "https://www.chicagocopa.org/",
            "-f", "bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "--concurrent-fragments", "16",
            "--socket-timeout", "30",
            "--retries", "5",
            "--no-part",
            "-o", out_fp,
            v_url
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if not os.path.exists(out_fp):
            cand = [os.path.join(tmp_dir, x) for x in os.listdir(tmp_dir) if os.path.isfile(os.path.join(tmp_dir, x))]
            if cand:
                out_fp = cand[0]
        if os.path.exists(out_fp) and os.path.getsize(out_fp) >= 10000:
            sz = os.path.getsize(out_fp)
            log(f"  [COPA-VIMEO] Downloaded {fn} ({sz/1e6:.1f} MB) -> SA uploading...")
            up_ok = sa_upload_file_to_folder(out_fp, fn, folder_id, mime_type="video/mp4")
            if up_ok:
                log(f"  [COPA-VIMEO COMPLETE] {fn} ({sz/1e6:.1f} MB)")
            return up_ok
        log(f"  [COPA-VIMEO ERR] Failed to download {v_url}")
    except Exception as e:
        log(f"  [COPA-VIMEO EXC] {fn}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return False

def process_copa_pdf(t, idx):
    folder_id = t["target_folder_id"]
    fn = t["filename"]
    purl = t["pdf_url"]

    existing = list_drive_folder(folder_id)
    for ef in existing:
        if ef.get("mimeType") != "application/vnd.google-apps.shortcut" and ef["name"].lower() == fn.lower() and int(ef.get("size", 0)) > 500:
            log(f"  [COPA-PDF] Already uploaded: {fn}")
            return True

    tmp_dir = os.path.join(STAGING_ROOT, f"pdf_{idx}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    out_fp = os.path.join(tmp_dir, fn)

    try:
        subprocess.run(["curl", "-sL", "--max-time", "60", "-A", UA, "-o", out_fp, purl], capture_output=True)
        if os.path.exists(out_fp) and os.path.getsize(out_fp) > 500:
            return sa_upload_file_to_folder(out_fp, fn, folder_id, mime_type="application/pdf")
    except Exception as e:
        log(f"  [COPA-PDF EXC] {fn}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return False

def main():
    payload = json.load(open(PAYLOAD_FILE, encoding="utf-8"))
    wc_tasks = payload.get("wc_tasks", [])
    my_vids = [(i, t) for i, t in enumerate(wc_tasks) if i % SHARD_TOTAL == SHARD_INDEX]
    log(f"Shard {SHARD_INDEX}/{SHARD_TOTAL} assigned {len(my_vids)}/{len(wc_tasks)} tasks...")

    done = 0
    for idx, t in my_vids:
        ttype = t.get("task_type")
        ok = False
        if ttype == "wc_gdrive":
            ok = process_wc_gdrive(t, idx)
        elif ttype == "wc_dropbox":
            ok = process_wc_dropbox(t, idx)
        elif ttype == "copa_vimeo":
            ok = process_copa_vimeo(t, idx)
        elif ttype == "copa_pdf":
            ok = process_copa_pdf(t, idx)
        if ok:
            done += 1

    log(f"Shard {SHARD_INDEX}/{SHARD_TOTAL} finished: {done}/{len(my_vids)} tasks succeeded.")

if __name__ == "__main__":
    main()
