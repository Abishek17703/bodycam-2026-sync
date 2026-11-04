import os
import json
import time
import subprocess
import urllib.request
import urllib.parse

TARGET_FOLDER_ID = "1TOgFL4pbKisYdJb7_Y1oyV0lWDQiQQoo"

VIDEOS = [
    {
        "name": "Reuters_LIVE_Scene_Abundant_Life_Christian_School_Shooting_Madison_WI_2h20m.mp4",
        "url": "https://www.youtube.com/watch?v=D_XqUk-zUvQ"
    },
    {
        "name": "AP_LIVE_Madison_Police_Scene_Briefing_Abundant_Life_Christian_School_1h16m.mp4",
        "url": "https://www.youtube.com/watch?v=mURB-nAXfE8"
    },
    {
        "name": "ABC_LIVE_Madison_Police_Response_Update_Abundant_Life_Christian_School_52m.mp4",
        "url": "https://www.youtube.com/watch?v=7NPE3dzJn68"
    },
    {
        "name": "WISN12_Jeffrey_Rupnow_Father_of_Madison_School_Shooter_Arrest_and_Briefing_27m.mp4",
        "url": "https://www.youtube.com/watch?v=FMjbEyYq1Oo"
    }
]

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
    url = item["url"]

    token = get_owner_token()
    existing = check_exists(token, name)
    if existing and int(existing.get("size", 0)) > 5000000:
        print(f"Already exists: {name} ({existing.get('size')} bytes)")
        return

    local_path = f"/tmp/{name}"
    print(f"Downloading {name} from {url}...")
    t0 = time.time()
    subprocess.run([
        "yt-dlp",
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "16",
        "--no-part",
        "-o", local_path,
        url
    ], check=True)
    actual_size = os.path.getsize(local_path)
    print(f"Downloaded {name}: {actual_size / (1024**2):.2f} MB in {time.time() - t0:.1f}s")

    print(f"Uploading {name} to {TARGET_FOLDER_ID} as bodyacamfootages@gmail.com...")
    t1 = time.time()
    token = get_owner_token()
    upload_resumable(token, local_path, name)
    print(f"Uploaded {name} in {time.time() - t1:.1f}s!")
    os.remove(local_path)

if __name__ == "__main__":
    main()
