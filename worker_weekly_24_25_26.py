#!/usr/bin/env python3
import os, sys, json, time, shutil, subprocess, urllib.request, urllib.parse, re, ssl
from datetime import datetime

SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))
PAYLOAD_FILE = os.environ.get("PAYLOAD_FILE", "weekly_24_25_26_payload.json")
STAGING_ROOT = f"/tmp/staging_weekly_s{SHARD_INDEX}"
os.makedirs(STAGING_ROOT, exist_ok=True)

CLIENT_ID = os.environ["OWNER_CLIENT_ID"]
CLIENT_SECRET = os.environ["OWNER_CLIENT_SECRET"]
TOK_MAIN_REF = os.environ["OWNER_BW_REFRESH_TOKEN"]   # bodyacamfootages@gmail.com
TOK_948_REF  = os.environ["OWNER_2026_REFRESH_TOKEN"] # bodycamfoia948@gmail.com

YEAR_ROOTS_MAIN = {
    "2026": "19L2kkkiLok94TQhdMQf2v7OEpZrqNVM0",
    "2025": "17qdClzhGNmtyMIsMNoXPsfok4fDXbIQj",
    "2024": "1AKyVJrPvGJmYhtp5lXo30eJ26VIDnGVj",
}
ROOT_2026_948 = "11REjO5KBB5vWD8C6Xfid7HV1fkpWSBIB"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout.write(f"[{ts}][S{SHARD_INDEX}] {msg}\n")
    sys.stdout.flush()

def get_token(refresh_token):
    for _ in range(3):
        try:
            data = urllib.parse.urlencode({
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            }).encode()
            req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
            return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())["access_token"]
        except Exception:
            time.sleep(2)
    return None

def drive_req(url, token, method="GET", body=None, content_type="application/json"):
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None and isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))

def get_or_create_folder(token, parent_id, name):
    q = urllib.parse.quote(f"'{parent_id}' in parents and name='{name.replace(chr(39), '')}' and mimeType='application/vnd.google-apps.folder' and trashed=false")
    res = drive_req(f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id,name)", token)
    if res.get("files"):
        return res["files"][0]["id"]
    created = drive_req("https://www.googleapis.com/drive/v3/files?fields=id", token, method="POST", body={
        "name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]
    })
    return created["id"]

def upload_file_resumable(token, parent_id, local_path, remote_name=None):
    if not remote_name:
        remote_name = os.path.basename(local_path)
    q = urllib.parse.quote(f"'{parent_id}' in parents and name='{remote_name.replace(chr(39), '')}' and trashed=false")
    existing = drive_req(f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id,size)", token).get("files", [])
    fsize = os.path.getsize(local_path)
    if existing and int(existing[0].get("size", 0)) == fsize:
        return existing[0]["id"]
    mime = "video/mp4" if local_path.lower().endswith(".mp4") else ("application/pdf" if local_path.lower().endswith(".pdf") else "application/octet-stream")
    init_req = urllib.request.Request(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id",
        data=json.dumps({"name": remote_name, "parents": [parent_id]}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime,
            "X-Upload-Content-Length": str(fsize),
        }
    )
    init_resp = urllib.request.urlopen(init_req, timeout=30)
    upload_url = init_resp.headers["Location"]
    chunk_size = 16 * 1024 * 1024
    with open(local_path, "rb") as f:
        offset = 0
        while offset < fsize:
            chunk = f.read(chunk_size)
            end = offset + len(chunk) - 1
            up_req = urllib.request.Request(
                upload_url,
                data=chunk,
                method="PUT",
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{fsize}"
                }
            )
            try:
                resp = urllib.request.urlopen(up_req, timeout=120)
                return json.loads(resp.read().decode())["id"]
            except urllib.error.HTTPError as e:
                if e.code == 308:
                    offset = end + 1
                else:
                    raise
    return None

def download_box_folder(box_url, out_dir):
    from playwright.sync_api import sync_playwright
    ctx_ssl = ssl.create_default_context()
    ctx_ssl.check_hostname = False
    ctx_ssl.verify_mode = ssl.CERT_NONE
    html = urllib.request.urlopen(urllib.request.Request(box_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20, context=ctx_ssl).read().decode("utf-8", errors="ignore")
    fids = re.findall(r'"typedID":"f_(\d+)"[^}]*"name":"([^"]+)"', html)
    share = box_url.rstrip("/").split("/")[-1]
    log(f"  Box {share}: found {len(fids)} files")
    downloaded = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        try:
            page.goto(f"https://njoag.box.com/s/{share}", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
        except Exception:
            pass
        for fid, fname in fids:
            try:
                dl_url = f"https://njoag.app.box.com/index.php?rm=box_download_shared_file&shared_name={share}&file_id=f_{fid}"
                resp = ctx.request.get(dl_url, timeout=300000)
                if resp.ok and len(resp.body()) > 1024:
                    out_p = os.path.join(out_dir, fname)
                    with open(out_p, "wb") as fp:
                        fp.write(resp.body())
                    downloaded.append(out_p)
                    log(f"  Box Direct DL OK: {fname} ({len(resp.body())}b)")
                    continue
            except Exception as e:
                log(f"  Box direct {fname} err: {e}")
            try:
                furl = f"https://njoag.app.box.com/s/{share}/file/{fid}"
                page.goto(furl, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_selector('[data-resin-target="download"], button[aria-label*="Download"], button:has-text("Download")', timeout=25000)
                with page.expect_download(timeout=300000) as dl_info:
                    page.locator('[data-resin-target="download"], button[aria-label*="Download"], button:has-text("Download")').first.click(force=True)
                dl = dl_info.value
                out_p = os.path.join(out_dir, dl.suggested_filename or fname)
                dl.save_as(out_p)
                downloaded.append(out_p)
                log(f"  Box DL OK: {os.path.basename(out_p)}")
            except Exception as e:
                log(f"  Box file {fname} err: {e}")
        browser.close()
    return downloaded

def process_case(case):
    cid = case["case_id"]
    yr = case["year"]
    cat = case["category"]
    folder_name = case["folder"]
    case_dir = os.path.join(STAGING_ROOT, cid)
    os.makedirs(case_dir, exist_ok=True)

    if case["source_type"] == "copa_vimeo":
        for idx, vurl in enumerate(case["video_urls"], 1):
            out_tmpl = os.path.join(case_dir, f"Clip_{idx:02d}_%(id)s.%(ext)s")
            cmd = [
                "yt-dlp", "--referer", "https://www.chicagocopa.org/",
                "-f", "bv*[height>=720]+ba/b[height>=720]/bv*+ba/b",
                "--merge-output-format", "mp4",
                "--no-warnings", "-q", "-o", out_tmpl, vurl
            ]
            subprocess.run(cmd, check=False, timeout=600)
        for pidx, purl in enumerate(case.get("pdf_urls", []), 1):
            pname = os.path.basename(urllib.parse.urlparse(purl).path) or f"Report_{pidx:02d}.pdf"
            subprocess.run(["curl", "-sL", "-o", os.path.join(case_dir, pname), purl], check=False, timeout=60)
    elif case["source_type"] == "direct_urls":
        for idx, vurl in enumerate(case["video_urls"], 1):
            vname = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(vurl).path)) or f"Clip_{idx:02d}.mp4"
            subprocess.run(["curl", "-sL", "-o", os.path.join(case_dir, vname), vurl], check=False, timeout=600)
    elif case["source_type"] == "njoag_box":
        download_box_folder(case["portal_url"], case_dir)

    local_files = []
    for root, _, files in os.walk(case_dir):
        for fn in files:
            fp = os.path.join(root, fn)
            if os.path.getsize(fp) > 1024:
                local_files.append(fp)

    log(f"{cid}: downloaded {len(local_files)} files")
    if not local_files:
        shutil.rmtree(case_dir, ignore_errors=True)
        return

    tok_main = get_token(TOK_MAIN_REF)
    cat_id_main = get_or_create_folder(tok_main, YEAR_ROOTS_MAIN[yr], cat)
    case_fid_main = get_or_create_folder(tok_main, cat_id_main, folder_name)
    for fp in local_files:
        upload_file_resumable(tok_main, case_fid_main, fp)
    log(f"{cid}: uploaded to BODYCAM AUTOMATION/{yr}/{cat}")

    if yr == "2026":
        tok_948 = get_token(TOK_948_REF)
        cat_id_948 = get_or_create_folder(tok_948, ROOT_2026_948, cat)
        case_fid_948 = get_or_create_folder(tok_948, cat_id_948, folder_name)
        for fp in local_files:
            upload_file_resumable(tok_948, case_fid_948, fp)
        log(f"{cid}: ALSO uploaded to bodycamfoia948/2026 Cases/{cat}")

    shutil.rmtree(case_dir, ignore_errors=True)

def main():
    payload = json.load(open(PAYLOAD_FILE, encoding="utf-8"))
    my_cases = [c for i, c in enumerate(payload) if i % SHARD_TOTAL == SHARD_INDEX]
    log(f"Shard {SHARD_INDEX}/{SHARD_TOTAL} handling {len(my_cases)} cases from {PAYLOAD_FILE}")
    for c in my_cases:
        try:
            process_case(c)
        except Exception as e:
            log(f"ERROR on {c['case_id']}: {e}")

if __name__ == "__main__":
    main()
