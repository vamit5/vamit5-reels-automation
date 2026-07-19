import os
import io
import json
import time
import subprocess
import tempfile

import requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account

IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
IG_ACCOUNT_ID = os.environ["IG_ACCOUNT_ID"]
GDRIVE_FOLDER_ID = os.environ["GDRIVE_FOLDER_ID"]
GDRIVE_SERVICE_ACCOUNT_JSON = os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"]

CLOUDINARY_CLOUD_NAME = "dnbjvccgy"
CLOUDINARY_UPLOAD_PRESET = "vamit5_reels"

STATE_FILE = "state.json"
MIN_CLIP_SECONDS = 9
MIN_TOTAL_SECONDS = 15
VIDEO_MIME_PREFIX = "video/"

CAPTION = (
    'Komentariši "VAMIT" i dobijaš link ka 7-dana besplatnom testu VAMIT-5 App. #joinvamit5\n\n'
    '@vamit5.athletes\n'
    '@vamit5.uniform'
)


def get_drive_service():
    info = json.loads(GDRIVE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)


def list_videos(drive):
    files = []
    page_token = None
    while True:
        response = drive.files().list(
            q=f"'{GDRIVE_FOLDER_ID}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, createdTime, videoMediaMetadata)",
            pageToken=page_token,
            pageSize=1000,
        ).execute()
        for f in response.get("files", []):
            if f.get("mimeType", "").startswith(VIDEO_MIME_PREFIX):
                files.append(f)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    files.sort(key=lambda f: (f["createdTime"], f["id"]))
    return files


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {"last_file_id": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def next_start_index(files, last_file_id):
    if last_file_id is None:
        return 0
    for i, f in enumerate(files):
        if f["id"] == last_file_id:
            return (i + 1) % len(files)
    return 0


def download_file(drive, file_id, dest_path):
    request = drive.files().get_media(fileId=file_id)
    with io.FileIO(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def ffprobe_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def get_duration(drive_file, local_path):
    meta = drive_file.get("videoMediaMetadata") or {}
    duration_millis = meta.get("durationMillis")
    if duration_millis:
        return int(duration_millis) / 1000.0
    return ffprobe_duration(local_path)


def merge_clips(paths, output_path):
    inputs = []
    filter_parts = []
    for i, p in enumerate(paths):
        inputs += ["-i", p]
        filter_parts.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(len(paths)))
    filter_complex = ";".join(filter_parts) + f";{concat_inputs}concat=n={len(paths)}:v=1:a=0[outv]"
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
        "-maxrate", "3500k", "-bufsize", "7000k",
        output_path,
    ]
    subprocess.run(cmd, check=True)


def compress_for_upload(input_path, output_path, keep_audio):
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
        "-maxrate", "3500k", "-bufsize", "7000k",
    ]
    if keep_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd.append(output_path)
    subprocess.run(cmd, check=True)


def upload_to_cloudinary(path):
    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload"
    with open(path, "rb") as fh:
        response = requests.post(
            url,
            files={"file": fh},
            data={"upload_preset": CLOUDINARY_UPLOAD_PRESET},
            timeout=600,
        )
    response.raise_for_status()
    return response.json()["secure_url"]


def publish_to_instagram(video_url):
    create_url = f"https://graph.instagram.com/v21.0/{IG_ACCOUNT_ID}/media"
    resp = requests.post(create_url, data={
        "media_type": "REELS",
        "video_url": video_url,
        "caption": CAPTION,
        "access_token": IG_ACCESS_TOKEN,
    })
    resp.raise_for_status()
    creation_id = resp.json()["id"]

    status_url = f"https://graph.instagram.com/v21.0/{creation_id}"
    for _ in range(60):
        time.sleep(10)
        status_resp = requests.get(status_url, params={
            "fields": "status_code",
            "access_token": IG_ACCESS_TOKEN,
        })
        status_resp.raise_for_status()
        status_code = status_resp.json().get("status_code")
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            raise RuntimeError("Instagram container processing failed")
    else:
        raise RuntimeError("Timed out waiting for Instagram to process the video")

    publish_url = f"https://graph.instagram.com/v21.0/{IG_ACCOUNT_ID}/media_publish"
    publish_resp = requests.post(publish_url, data={
        "creation_id": creation_id,
        "access_token": IG_ACCESS_TOKEN,
    })
    publish_resp.raise_for_status()
    return publish_resp.json()


def main():
    drive = get_drive_service()
    files = list_videos(drive)
    if not files:
        raise RuntimeError("Nema video fajlova u Google Drive folderu")

    state = load_state()
    start = next_start_index(files, state.get("last_file_id"))

    with tempfile.TemporaryDirectory() as tmp:
        chosen_paths = []
        chosen_files = []
        total_duration = 0.0
        index = start
        for _ in range(len(files)):
            f = files[index]
            local_path = os.path.join(tmp, f["id"])
            download_file(drive, f["id"], local_path)
            duration = get_duration(f, local_path)
            chosen_paths.append(local_path)
            chosen_files.append(f)
            total_duration += duration
            index = (index + 1) % len(files)

            if len(chosen_paths) == 1 and duration >= MIN_CLIP_SECONDS:
                break
            if len(chosen_paths) > 1 and total_duration >= MIN_TOTAL_SECONDS:
                break

        if len(chosen_paths) == 1:
            upload_path = os.path.join(tmp, "solo.mp4")
            compress_for_upload(chosen_paths[0], upload_path, keep_audio=True)
        else:
            upload_path = os.path.join(tmp, "merged.mp4")
            merge_clips(chosen_paths, upload_path)

        video_url = upload_to_cloudinary(upload_path)
        result = publish_to_instagram(video_url)
        print("Objavljeno:", result)

    state["last_file_id"] = chosen_files[-1]["id"]
    save_state(state)


if __name__ == "__main__":
    main()
