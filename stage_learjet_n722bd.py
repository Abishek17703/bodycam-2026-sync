#!/usr/bin/env python3
import os, sys, json, time, shutil, subprocess, urllib.request, re

CASE_FOLDER = "2026-11-01_Greenacres_Palm_Beach_FL_Learjet_60_N722BD_Crash_Raw_Multi_Angle"
REMOTE_BASE = f"gdrive2026_cases:{CASE_FOLDER}"

STAGE_DIR = "/tmp/learjet_n722bd_stage"
VIDS_DIR = os.path.join(STAGE_DIR, "Videos")
DOCS_DIR = os.path.join(STAGE_DIR, "Documents")
os.makedirs(VIDS_DIR, exist_ok=True)
os.makedirs(DOCS_DIR, exist_ok=True)

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def probe_video(fp):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name:format=duration,size",
             "-of", "json", fp], timeout=45
        ).decode("utf-8", errors="ignore")
        d = json.loads(out)
        s = d.get("streams", [{}])[0]
        f = d.get("format", {})
        return {
            "w": int(s.get("width", 0) or 0),
            "h": int(s.get("height", 0) or 0),
            "codec": s.get("codec_name", ""),
            "dur": float(f.get("duration", 0.0) or 0.0),
            "size_mb": round(int(f.get("size", 0) or os.path.getsize(fp)) / (1024 * 1024), 2)
        }
    except Exception as e:
        return {"error": str(e)}

def download_yt_hd(vid, out_name):
    fp = os.path.join(VIDS_DIR, out_name)
    log(f"Downloading YouTube HD [{vid}] -> {out_name}")
    api = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
    ios_ua = "com.google.ios.youtube/20.03.02 (iPhone16,2; U; CPU iOS 18_2_1 like Mac OS X;)"
    for use_proxy in (False, True):
        for cname, cver, ua, dev_extra in [
            ("IOS", "20.03.02", ios_ua, {"deviceMake": "Apple", "deviceModel": "iPhone16,2", "osName": "iPhone", "osVersion": "18.2.1.22C161"}),
            ("ANDROID_VR", "1.60.19", "Mozilla/5.0", {})
        ]:
            client_obj = {"clientName": cname, "clientVersion": cver, "hl": "en", "gl": "US"}
            client_obj.update(dev_extra)
            payload = json.dumps({"videoId": vid, "context": {"client": client_obj}, "racyCheckOk": True, "contentCheckOk": True})
            cmd = ["curl", "-sL", "--max-time", "20", "-H", "Content-Type: application/json", "-A", ua, "-d", payload, api]
            if use_proxy:
                cmd[1:1] = ["-x", "socks5h://127.0.0.1:40020"]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0 or not r.stdout.strip():
                continue
            try:
                d = json.loads(r.stdout)
                fmts = d.get("streamingData", {}).get("adaptiveFormats", [])
                v_fmts = [f for f in fmts if "url" in f and "video" in f.get("mimeType", "")]
                a_fmts = [f for f in fmts if "url" in f and "audio" in f.get("mimeType", "")]
                if v_fmts and a_fmts:
                    v_best = sorted(v_fmts, key=lambda x: (1 if "video/mp4" in x.get("mimeType", "") else 0, max(int(x.get("width", 0) or 0), int(x.get("height", 0) or 0)), int(x.get("bitrate", 0) or 0)), reverse=True)[0]
                    a_best = sorted(a_fmts, key=lambda x: (1 if "audio/mp4" in x.get("mimeType", "") else 0, int(x.get("bitrate", 0) or 0)), reverse=True)[0]
                    v_ext = "mp4" if "mp4" in v_best.get("mimeType", "") else "webm"
                    a_ext = "m4a" if "mp4" in a_best.get("mimeType", "") else "webm"
                    v_tmp = f"/tmp/v_{vid}.{v_ext}"
                    a_tmp = f"/tmp/a_{vid}.{a_ext}"
                    dl_v = ["curl", "-sL", "-A", ua, "-o", v_tmp, v_best["url"]]
                    dl_a = ["curl", "-sL", "-A", ua, "-o", a_tmp, a_best["url"]]
                    if use_proxy:
                        dl_v[1:1] = ["-x", "socks5h://127.0.0.1:40020"]
                        dl_a[1:1] = ["-x", "socks5h://127.0.0.1:40020"]
                    subprocess.run(dl_v, timeout=600)
                    subprocess.run(dl_a, timeout=600)
                    if os.path.exists(v_tmp) and os.path.exists(a_tmp) and os.path.getsize(v_tmp) > 10000:
                        acodec = "copy" if a_ext == "m4a" else "aac"
                        subprocess.run(["ffmpeg", "-y", "-i", v_tmp, "-i", a_tmp, "-c:v", "copy", "-c:a", acodec, fp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
                        for t in (v_tmp, a_tmp):
                            try: os.remove(t)
                            except Exception: pass
                        if os.path.exists(fp) and os.path.getsize(fp) > 10000:
                            info = probe_video(fp)
                            log(f"  OK YouTube InnerTube ({cname}/proxy={use_proxy}) {out_name}: {info}")
                            return info
            except Exception as e:
                log(f"  InnerTube err {vid} ({cname}): {e}")
    # Fallback yt-dlp with bgutil PO-Token server
    clients = ["ios,web", "mweb,web", "tv,web", "web,default"]
    for attempt in range(4):
        extra = ["--extractor-args", f"youtube:player_client={clients[attempt % len(clients)]};youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416"]
        if attempt % 2 == 1:
            extra = ["--proxy", "socks5://127.0.0.1:40020"] + extra
        cmd = ["yt-dlp"] + extra + ["-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best", "--merge-output-format", "mp4", "-o", fp, f"https://www.youtube.com/watch?v={vid}"]
        subprocess.run(cmd, timeout=600)
        if os.path.exists(fp) and os.path.getsize(fp) > 10000:
            info = probe_video(fp)
            log(f"  OK yt-dlp+bgutil {out_name}: {info}")
            return info
    return None

def main():
    results = {}
    results["04_Angle4_Eduin_Cruz_Full_Raw_Witness_Video_1920x1080_73s_Unwatermarked.mp4"] = download_yt_hd(
        "D3ihj8FUTkg",
        "04_Angle4_Eduin_Cruz_Full_Raw_Witness_Video_1920x1080_73s_Unwatermarked.mp4"
    )
    results["09_Angle9_VASAviation_Full_LiveATC_Radar_Sync_383s_1080p.mp4"] = download_yt_hd(
        "v5dfFoZ_fuQ",
        "09_Angle9_VASAviation_Full_LiveATC_Radar_Sync_383s_1080p.mp4"
    )

    log("Uploading Angle 4 and Angle 9 to Google Drive via rclone...")
    for attempt in range(5):
        r = subprocess.run(["rclone", "copy", VIDS_DIR, f"{REMOTE_BASE}/Videos", "--transfers", "2", "--drive-chunk-size", "128M", "--tpslimit", "4", "-v"])
        if r.returncode == 0:
            break
        time.sleep(5)

    r_files = subprocess.run(["rclone", "lsjson", f"{REMOTE_BASE}/Videos"], capture_output=True, text=True)
    drive_files = json.loads(r_files.stdout) if r_files.returncode == 0 else []

    # Update Official_Case_Documentation_Summary.txt & manifest.json with all 9 staged files
    summary_text = f"""================================================================================
INCIDENT CASE REPORT & MULTI-ANGLE RAW FOOTAGE MANIFEST
================================================================================
Incident: Greenacres / Palm Beach County, FL Learjet 60 Crash (N722BD)
Date & Time: November 1, 2026, ~12:49:32 PM EDT (17:49:32 UTC)
Location: South Military Trail between Cresthaven Blvd & 10th Ave North, Greenacres / Lake Worth Beach, Palm Beach County, FL
Aircraft: Bombardier Learjet 60 (Registration: N722BD, Serial: 60-271, Operator: LR60271 LLC)
Route: Austin-Bergstrom International Airport (KAUS) -> North Eleuthera Airport (MYEH), Bahamas
Fatalities: 9 (All 9 aboard killed; ~600 residents evacuated from nearby Cresthaven mobile home park)
Responding Agencies: Palm Beach County Sheriff's Office (PBSO), Palm Beach County Fire Rescue (PBCFR), FAA, NTSB

TARGET INSTAGRAM REELS IDENTIFIED:
1. https://www.instagram.com/reels/DfOaWkuDdwg/ (@moneycontrolcom)
   - Shows Aki Kuusisto's dashcam video overlaid with LiveATC audio and Moneycontrol news banner.
2. https://www.instagram.com/reels/DfNvDblAlkh/ (@carolinaresch)
   - Shows bystander cell phone footage at the smoking impact crater on South Military Trail (Eduin Cruz / Sisley Parra @SISLY21 angle).

STAGED RAW UNWATERMARKED MULTI-ANGLE PACKAGE ON GOOGLE DRIVE:
{json.dumps(drive_files, indent=2)}
"""
    with open(os.path.join(DOCS_DIR, "Official_Case_Documentation_Summary.txt"), "w", encoding="utf-8") as f:
        f.write(summary_text)
    with open(os.path.join(DOCS_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(drive_files, f, indent=2)

    subprocess.run(["rclone", "copy", DOCS_DIR, f"{REMOTE_BASE}/Documents", "--transfers", "2", "-v"], check=True)

    print("=== FINAL_REPORT_JSON_START ===")
    print(json.dumps({
        "case_folder": CASE_FOLDER,
        "folder_id": "1BYb4BAdezz_b5G9SRqgrfg6BEZrCBoUS",
        "folder_url": "https://drive.google.com/drive/folders/1BYb4BAdezz_b5G9SRqgrfg6BEZrCBoUS",
        "new_yt_results": results,
        "drive_files": [{"Name": x.get("Name"), "Size": x.get("Size"), "ID": x.get("ID")} for x in drive_files]
    }, indent=2))
    print("=== FINAL_REPORT_JSON_END ===")

if __name__ == "__main__":
    main()
