import os
import json
import time
import subprocess
import urllib.request
import urllib.parse

TARGET_FOLDER_ID = "1dcQ6Mpo0DDmgafnMn_ZxXJJSZKDOYD-L"

FILES = [
    {"name": "UNIT-02_Front_15179_04252024233441.mp4", "bytes": 1883472113, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/AI1p2odPRC2xVg6v4K-wkn4/UNIT-02_Front_15179_04252024233441.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
    {"name": "UNIT-10_Backseat_15185_04252024233203.mp4", "bytes": 561160098, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/ANfYAtDXDdaYWgCdPDW0X0E/UNIT-10_Backseat_15185_04252024233203.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
    {"name": "UNIT-10_Backseat_15185_04262024003205.mp4", "bytes": 558913630, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/AHHAUdE9Rm352SHGOElQRp4/UNIT-10_Backseat_15185_04262024003205.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
    {"name": "UNIT-10_Backseat_15185_04262024013206.mp4", "bytes": 565722233, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/AP9y6_52EAWj4MTj5BIhCvg/UNIT-10_Backseat_15185_04262024013206.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
    {"name": "UNIT-10_Backseat_15185_04262024023207.mp4", "bytes": 528451084, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/AJrDcOZ_jR6ppYJL_qoSOsA/UNIT-10_Backseat_15185_04262024023207.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
    {"name": "UNIT-10_Backseat_15185_04262024034633.mp4", "bytes": 321279948, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/AI0Wmp4AB0xPeQQXJpJ1GBc/UNIT-10_Backseat_15185_04262024034633.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
    {"name": "UNIT-10_Front_15185_04252024233203.mp4", "bytes": 1885225479, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/AH2pjEaSkoVVC7qHlhrnk2A/UNIT-10_Front_15185_04252024233203.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
    {"name": "UNIT-10_Front_15185_04262024003204.mp4", "bytes": 1870024086, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/ABrmPRbD2taAI_xoSGoPE_A/UNIT-10_Front_15185_04262024003204.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
    {"name": "UNIT-10_Front_15185_04262024013205.mp4", "bytes": 1869825620, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/AGqOSmG2Q-_m4JN0KvgEpB4/UNIT-10_Front_15185_04262024013205.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
    {"name": "UNIT-10_Front_15185_04262024023206.mp4", "bytes": 1745149249, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/ANT2TsK0Qf1uTEFnJyjitI0/UNIT-10_Front_15185_04262024023206.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
    {"name": "UNIT-10_Front_15185_04262024034633.mp4", "bytes": 1067152409, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/AHK_zrx8c6enDELdPnMn174/UNIT-10_Front_15185_04262024034633.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
    {"name": "UNIT-K9-01_Front_15187_04252024233424.mp4", "bytes": 234319640, "url": "https://www.dropbox.com/scl/fo/eliti6950htuavbhboca7/AA5Sx4OlWerGwGxkfEVBsKs/UNIT-K9-01_Front_15187_04252024233424.mp4?rlkey=j2fc123749n8bfxgryi8g1mr7&dl=1"},
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
    item = FILES[idx]
    name = item["name"]
    url = item["url"]
    token = get_owner_token()

    existing = check_exists(token, name)
    if existing and int(existing.get("size", 0)) > 1000000:
        print(f"Already exists: {name} ({existing.get('size')} bytes)")
        return

    local_path = f"/tmp/{name}"
    print(f"Downloading {name} ({item['bytes'] / (1024**3):.2f} GB)...")
    t0 = time.time()
    subprocess.run(["curl", "-sSL", "-o", local_path, url], check=True)
    dt = time.time() - t0
    actual_size = os.path.getsize(local_path)
    print(f"Downloaded {actual_size} bytes in {dt:.1f}s")

    print(f"Uploading {name} to {TARGET_FOLDER_ID} as owner bodyacamfootages@gmail.com...")
    t1 = time.time()
    token = get_owner_token()
    upload_resumable(token, local_path, name)
    print(f"Uploaded {name} in {time.time() - t1:.1f}s!")
    os.remove(local_path)

if __name__ == "__main__":
    main()
