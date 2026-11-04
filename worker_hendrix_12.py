import os
import re
import json
import time
import base64
import subprocess
import urllib.request
import urllib.parse
import http.cookiejar

TARGET_FOLDER_ID = "1dcQ6Mpo0DDmgafnMn_ZxXJJSZKDOYD-L"
DBX_URL = "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/AO8yE5cWgJcHk8OlnmuUTQk?rlkey=j2fc123749n8bfxgryi8g1mr7&st=3d7eyr5b&e=1&dl=0"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

def get_owner_token():
    data = urllib.parse.urlencode({
        "client_id": os.environ["OWNER_CLIENT_ID"],
        "client_secret": os.environ["OWNER_CLIENT_SECRET"],
        "refresh_token": os.environ["OWNER_REFRESH_TOKEN"],
        "grant_type": "refresh_token"
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())["access_token"]

def check_exists(token, name):
    q = f"'{TARGET_FOLDER_ID}' in parents and name = '{name}' and trashed = false"
    url = f"https://www.googleapis.com/drive/v3/files?q={urllib.parse.quote(q)}&fields=files(id,name,size)"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        files = json.loads(resp.read().decode()).get("files", [])
        return files[0] if files else None

def upload_resumable(token, local_path, name):
    file_size = os.path.getsize(local_path)
    meta = json.dumps({"name": name, "parents": [TARGET_FOLDER_ID]}).encode("utf-8")
    init_req = urllib.request.Request(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable",
        data=meta,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(file_size)
        },
        method="POST"
    )
    with urllib.request.urlopen(init_req) as resp:
        upload_url = resp.headers["Location"]

    subprocess.run([
        "curl", "-sS", "-X", "PUT",
        "-H", f"Content-Length: {file_size}",
        "-H", "Content-Type: video/mp4",
        "--upload-file", local_path,
        upload_url
    ], check=True)

def get_page2_entries():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    h = opener.open(urllib.request.Request(DBX_URL, headers={"User-Agent": UA}), timeout=20).read().decode("utf-8", "ignore")
    b64s = re.findall(r'registerStreamedPrefetch\s*\(\s*"[^"]+"\s*,\s*"([^"]+)"', h)
    raw = base64.b64decode(b64s[-1]).decode("latin1")
    voucher = re.search(r'(\{"prog":\s*".*?"sig":\s*"[^"]+"\})', raw).group(1)
    t_cookie = next((c.value for c in cj if c.name in ('t', '__Host-js_csrf')), "")
    data = urllib.parse.urlencode({
        "is_xhr": "true",
        "t": t_cookie,
        "link_key": "eliti6950htuavbhboca7",
        "link_type": "s",
        "rlkey": "j2fc123749n8bfxgryi8g1mr7",
        "secure_hash": "AO8yE5cWgJcHk8OlnmuUTQk",
        "sub_path": "",
        "voucher": voucher
    }).encode("utf-8")
    req2 = urllib.request.Request(
        "https://www.dropbox.com/list_shared_link_folder_entries",
        data=data,
        headers={
            "User-Agent": UA,
            "Referer": DBX_URL,
            "Origin": "https://www.dropbox.com",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
        }
    )
    res2 = json.loads(opener.open(req2, timeout=20).read().decode("utf-8"))
    return res2.get("entries", [])

def main():
    idx = int(os.environ.get("SHARD_INDEX", "0"))
    entries = get_page2_entries()
    if idx >= len(entries):
        print(f"Index {idx} out of range ({len(entries)} entries)")
        return
    entry = entries[idx]
    name = entry["filename"]
    m3u8_url = entry["preview"]["content"]["transcode_url"]

    token = get_owner_token()
    existing = check_exists(token, name)
    if existing and int(existing.get("size", 0)) > 5000000:
        print(f"Already exists: {name} ({existing.get('size')} bytes)")
        return

    local_path = f"/tmp/{name}"
    print(f"Downloading HLS stream for {name}...")
    t0 = time.time()
    subprocess.run([
        "yt-dlp",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "16",
        "--no-part",
        "-o", local_path,
        m3u8_url
    ], check=True)
    dt = time.time() - t0
    actual_size = os.path.getsize(local_path)
    print(f"Downloaded {name}: {actual_size / (1024**2):.2f} MB in {dt:.1f}s")

    if actual_size < 1000000:
        raise RuntimeError(f"File too small: {actual_size} bytes")

    print(f"Uploading {name} ({actual_size / (1024**2):.2f} MB) to {TARGET_FOLDER_ID} as bodyacamfootages@gmail.com...")
    t1 = time.time()
    token = get_owner_token()
    upload_resumable(token, local_path, name)
    print(f"Uploaded {name} in {time.time() - t1:.1f}s!")
    os.remove(local_path)

if __name__ == "__main__":
    main()
