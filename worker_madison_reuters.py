import os
import re
import json
import time
import socket
import subprocess
import urllib.request
import urllib.parse

TARGET_FOLDER_ID = "1TOgFL4pbKisYdJb7_Y1oyV0lWDQiQQoo"

VIDEOS = [
    {
        "name": "Reuters_LIVE_Scene_Abundant_Life_Christian_School_Shooting_Madison_WI_2h20m.mp4",
        "vid": "D_XqUk-zUvQ",
        "url": "https://www.youtube.com/watch?v=D_XqUk-zUvQ"
    },
    {
        "name": "AP_LIVE_Madison_Police_Scene_Briefing_Abundant_Life_Christian_School_1h16m.mp4",
        "vid": "mURB-nAXfE8",
        "url": "https://www.youtube.com/watch?v=mURB-nAXfE8"
    },
    {
        "name": "Madison_PD_Full_Response_Briefing_Abundant_Life_Christian_School_Clean_Pool_35m.mp4",
        "vid": "_bPRHwY1r4I",
        "url": "https://www.youtube.com/watch?v=_bPRHwY1r4I"
    },
    {
        "name": "Madison_PD_Jeffrey_Rupnow_Arrest_and_Investigation_Briefing_Clean_Pool_25m.mp4",
        "vid": "AqsAjW8TJog",
        "url": "https://www.youtube.com/watch?v=AqsAjW8TJog"
    }
]

def is_warp_open():
    try:
        with socket.create_connection(("127.0.0.1", 40020), timeout=1.5):
            return True
    except Exception:
        return False

def start_fresh_warp(seed=0):
    subprocess.run(["pkill", "-9", "wireproxy"], capture_output=True)
    time.sleep(0.5)
    wdir = f"/tmp/warp_{seed}_{int(time.time())}"
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

def main():
    idx = int(os.environ.get("SHARD_INDEX", "0"))
    if idx >= len(VIDEOS):
        return
    item = VIDEOS[idx]
    name = item["name"]
    vid = item["vid"]
    url = item["url"]

    token = get_owner_token()
    existing = check_exists(token, name)
    if existing and int(existing.get("size", 0)) > 5000000:
        print(f"Already exists: {name} ({existing.get('size')} bytes)")
        return

    local_path = f"/tmp/{name}"
    ensure_warp_alive()

    for att in range(1, 7):
        ensure_warp_alive()
        print(f"Downloading {name} (att {att}) via WARP SOCKS5...")
        r = subprocess.run([
            "yt-dlp",
            "--proxy", "socks5://127.0.0.1:40020",
            "-f", "137+140/299+140/136+140/298+140/bestvideo[height>=720][height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720][height<=1080]+bestaudio/best[height>=720]/best",
            "--merge-output-format", "mp4",
            "--concurrent-fragments", "12",
            "--no-part",
            "-o", local_path,
            url
        ])
        if os.path.exists(local_path) and os.path.getsize(local_path) > 5000000:
            break
        # Fallback: IOS hlsManifestUrl via WARP
        api = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
        ios_ua = "com.google.ios.youtube/20.03.02 (iPhone16,2; U; CPU iOS 18_2_1 like Mac OS X;)"
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
                    subprocess.run([
                        "yt-dlp", "--proxy", "socks5://127.0.0.1:40020",
                        "-f", "bestvideo[height>=720]+bestaudio/best",
                        "--merge-output-format", "mp4",
                        "--concurrent-fragments", "16",
                        "--no-part",
                        "-o", local_path,
                        hls_url
                    ])
                    if os.path.exists(local_path) and os.path.getsize(local_path) > 5000000:
                        break
            except Exception:
                pass
        start_fresh_warp(att)

    actual_size = os.path.getsize(local_path)
    print(f"Downloaded {name}: {actual_size / (1024**2):.2f} MB")

    print(f"Uploading {name} to {TARGET_FOLDER_ID} as bodyacamfootages@gmail.com...")
    t1 = time.time()
    token = get_owner_token()
    upload_resumable(token, local_path, name)
    print(f"Uploaded {name} in {time.time() - t1:.1f}s!")
    os.remove(local_path)

if __name__ == "__main__":
    main()
