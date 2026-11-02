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

def download_direct(url, out_name):
    fp = os.path.join(VIDS_DIR, out_name)
    log(f"Downloading direct URL -> {out_name}")
    subprocess.run(["curl", "-sL", "--retry", "5", "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "-o", fp, url], timeout=600)
    if os.path.exists(fp) and os.path.getsize(fp) > 10000:
        info = probe_video(fp)
        log(f"  OK {out_name}: {info}")
        return info
    log(f"  FAILED {out_name}")
    return None

def download_yt_hd(vid, out_name):
    fp = os.path.join(VIDS_DIR, out_name)
    log(f"Downloading YouTube HD [{vid}] -> {out_name}")
    api = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
    ios_ua = "com.google.ios.youtube/20.03.02 (iPhone16,2; U; CPU iOS 18_2_1 like Mac OS X;)"
    # IMPORTANT: Both player API call AND stream downloads must use the exact same IP (either both direct or both via WARP proxy)
    for use_proxy in (True, False):
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
                    dl_v = ["curl", "-sL", "--retry", "4", "-A", ua, "-o", v_tmp, v_best["url"]]
                    dl_a = ["curl", "-sL", "--retry", "4", "-A", ua, "-o", a_tmp, a_best["url"]]
                    if use_proxy:
                        dl_v[1:1] = ["-x", "socks5h://127.0.0.1:40020"]
                        dl_a[1:1] = ["-x", "socks5h://127.0.0.1:40020"]
                    subprocess.run(dl_v, timeout=300)
                    subprocess.run(dl_a, timeout=300)
                    if os.path.exists(v_tmp) and os.path.exists(a_tmp) and os.path.getsize(v_tmp) > 10000 and os.path.getsize(a_tmp) > 5000:
                        acodec = "copy" if a_ext == "m4a" else "aac"
                        subprocess.run(["ffmpeg", "-y", "-i", v_tmp, "-i", a_tmp, "-c:v", "copy", "-c:a", acodec, fp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
                        for t in (v_tmp, a_tmp):
                            try: os.remove(t)
                            except Exception: pass
                        if os.path.exists(fp) and os.path.getsize(fp) > 10000:
                            info = probe_video(fp)
                            log(f"  OK YouTube InnerTube ({cname}/proxy={use_proxy}) {out_name}: {info}")
                            return info
                    for t in (v_tmp, a_tmp):
                        try: os.remove(t)
                        except Exception: pass
            except Exception as e:
                log(f"  InnerTube err {vid} ({cname}/proxy={use_proxy}): {e}")
    log(f"  FAILED YouTube {vid}")
    return None

def main():
    results = {}

    # 1. Angle 1B: Extended 1080p (15.6s) Aki Kuusisto Raw Dashcam (Unwatermarked, Full HD)
    results["01B_Angle1_Aki_Kuusisto_Raw_Dashcam_Extended_1920x1080_15.6s.mp4"] = download_direct(
        "https://video.twimg.com/amplify_video/2116998718767435777/vid/avc1/1920x1080/_WBbGz1xQDG6bFiH.mp4?tag=29",
        "01B_Angle1_Aki_Kuusisto_Raw_Dashcam_Extended_1920x1080_15.6s.mp4"
    )

    # 2. Angle 4: Eduin Cruz Full 73s Unwatermarked 1080p Raw Witness Video
    results["04_Angle4_Eduin_Cruz_Full_Raw_Witness_Video_1920x1080_73s_Unwatermarked.mp4"] = download_yt_hd(
        "D3ihj8FUTkg",
        "04_Angle4_Eduin_Cruz_Full_Raw_Witness_Video_1920x1080_73s_Unwatermarked.mp4"
    )

    # 3. Angle 9: VASAviation Full 6m23s LiveATC + Radar & ADS-B Sync (1920x1080)
    results["09_Angle9_VASAviation_Full_LiveATC_Radar_Sync_383s_1080p.mp4"] = download_yt_hd(
        "v5dfFoZ_fuQ",
        "09_Angle9_VASAviation_Full_LiveATC_Radar_Sync_383s_1080p.mp4"
    )

    # 4. Angle 11: Flightradar24 Official 4K UHD (3022x2160) ADS-B + Weather Radar Playback
    results["11_Angle11_Flightradar24_Official_ADSB_Weather_Radar_Playback_3022x2160_4K.mp4"] = download_direct(
        "https://video.twimg.com/amplify_video/2117036062056886273/vid/avc1/3022x2160/rkUiTIyoYSo2PZ9q.mp4?tag=29",
        "11_Angle11_Flightradar24_Official_ADSB_Weather_Radar_Playback_3022x2160_4K.mp4"
    )

    # 5. Angle 12: AirNav Radar 116.4s Final Moments ATC + 32,000 fpm Descent Telemetry
    results["12_Angle12_AirNavRadar_Miami_Center_ATC_And_Descent_Telemetry_116s.mp4"] = download_direct(
        "https://video.twimg.com/amplify_video/2117026848886439936/vid/avc1/1280x720/DwxRK5hkILOdz1Ip.mp4?tag=29",
        "12_Angle12_AirNavRadar_Miami_Center_ATC_And_Descent_Telemetry_116s.mp4"
    )

    # 6. Angle 13: TurbineTraveler Greenacres Track & Descent Replay (1080x1772)
    results["13_Angle13_TurbineTraveler_Greenacres_Flight_Track_1080x1772.mp4"] = download_direct(
        "https://video.twimg.com/amplify_video/2116986321084329984/vid/avc1/1080x1772/Z80sjCO8tAK9WdSA.mp4?tag=29",
        "13_Angle13_TurbineTraveler_Greenacres_Flight_Track_1080x1772.mp4"
    )

    # 7. Angle 14: WPLG Local 10 New Dashcam Video Release (JAPGRehkckI)
    results["14_Angle14_WPLG_New_Dashcam_Video_Release_1080p.mp4"] = download_yt_hd(
        "JAPGRehkckI",
        "14_Angle14_WPLG_New_Dashcam_Video_Release_1080p.mp4"
    )

    # 8. Angle 15: WPLG Local 10 Extended Dashcam & Scene Report (k2s3UIKkV1Q, 152s)
    results["15_Angle15_WPLG_Extended_Dashcam_And_Witness_Aftermath_152s_1080p.mp4"] = download_yt_hd(
        "k2s3UIKkV1Q",
        "15_Angle15_WPLG_Extended_Dashcam_And_Witness_Aftermath_152s_1080p.mp4"
    )

    # 9. Angle 16: WPLG Local 10 Witnesses Describe Aftermath on Military Trail (O9iHZRcplg4, 155s)
    results["16_Angle16_WPLG_Witnesses_Describe_Aftermath_Military_Trail_155s_1080p.mp4"] = download_yt_hd(
        "O9iHZRcplg4",
        "16_Angle16_WPLG_Witnesses_Describe_Aftermath_Military_Trail_155s_1080p.mp4"
    )

    # 10. Angle 17: WPTV Neighbors & Witnesses on Scene Near Greenacres (wbn0PEDoyw8, 104s)
    results["17_Angle17_WPTV_Greenacres_Neighbors_And_Scene_Footage_104s_1080p.mp4"] = download_yt_hd(
        "wbn0PEDoyw8",
        "17_Angle17_WPTV_Greenacres_Neighbors_And_Scene_Footage_104s_1080p.mp4"
    )

    # 11. Angle 18: WPTV Full On-Scene Press Conference (Gov. DeSantis, PBSO, PBCFR, 542s)
    results["18_Angle18_Official_PBSO_PBCFR_DeSantis_On_Scene_Press_Briefing_542s_1080p.mp4"] = download_yt_hd(
        "nvQ7wwPnZz0",
        "18_Angle18_Official_PBSO_PBCFR_DeSantis_On_Scene_Press_Briefing_542s_1080p.mp4"
    )

    # 12. Angle 19: Fox 13 / WTVT Syndication Raw Scene & Dashcam Package
    results["19_Angle19_Fox_WTVT_Syndication_Scene_Package.mp4"] = download_direct(
        "https://static-clips.fox.com/proxy/fts_clip/svc_hf_fts_clip_prod/WM_WTVT_49375249614_49375249708/49375249708/hf-uuid-8b5ad380-834c-4276-b373-467a88a018ef/49375249708_syndication.mp4",
        "19_Angle19_Fox_WTVT_Syndication_Scene_Package.mp4"
    )

    log("Uploading new angles to Google Drive via rclone...")
    for attempt in range(5):
        r = subprocess.run(["rclone", "copy", VIDS_DIR, f"{REMOTE_BASE}/Videos", "--transfers", "3", "--drive-chunk-size", "128M", "--tpslimit", "4", "-v"])
        if r.returncode == 0:
            break
        time.sleep(5)

    r_files = subprocess.run(["rclone", "lsjson", f"{REMOTE_BASE}/Videos"], capture_output=True, text=True)
    drive_files = sorted(json.loads(r_files.stdout), key=lambda x: x.get("Name", "")) if r_files.returncode == 0 else []

    summary_text = f"""================================================================================
INCIDENT CASE REPORT & MULTI-ANGLE RAW FOOTAGE MANIFEST (EXPANDED EDITION)
================================================================================
Incident: Greenacres / Palm Beach County, FL Learjet 60 Crash (N722BD)
Date & Time: November 1, 2026, ~12:49:32 PM EST (17:49:32 UTC)
Location: South Military Trail between Cresthaven Blvd & 10th Ave North, Greenacres / Lake Worth Beach, Palm Beach County, FL (26°37'58.4"N 80°06'47.2"W)
Aircraft: 2004 Bombardier Learjet 60 (Registration: N722BD, Serial: 60-271, Operator: LR60271 LLC)
Crew Identified: Pilot Nathalia Filgueiras (20, Boca Raton, FL; Upper Jets) + 1 co-pilot + 7 passengers
Route: Austin-Bergstrom International Airport (KAUS) -> North Eleuthera Airport (MYEH), Bahamas
Fatalities: 9 (All 9 aboard killed; ~600 residents evacuated from nearby Cresthaven mobile home park)
Responding Agencies: Palm Beach County Sheriff's Office (PBSO), Palm Beach County Fire Rescue (PBCFR), FAA, NTSB

TARGET INSTAGRAM REELS IDENTIFIED:
1. https://www.instagram.com/reels/DfOaWkuDdwg/ (@moneycontrolcom)
   - Shows Aki Kuusisto's dashcam video overlaid with LiveATC audio and Moneycontrol news banner.
2. https://www.instagram.com/reels/DfNvDblAlkh/ (@carolinaresch)
   - Shows bystander cell phone footage at the smoking impact crater on South Military Trail (Eduin Cruz / Sisley Parra @SISLY21 angle).

COMPLETE STAGED MULTI-ANGLE PACKAGE ON GOOGLE DRIVE ({len(drive_files)} VIDEOS):
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
        "new_results": results,
        "total_drive_files": len(drive_files),
        "drive_files": [{"Name": x.get("Name"), "Size": x.get("Size"), "ID": x.get("ID")} for x in drive_files]
    }, indent=2))
    print("=== FINAL_REPORT_JSON_END ===")

if __name__ == "__main__":
    main()
