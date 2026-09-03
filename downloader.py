import os
import re
import sys
import time
import json
import asyncio
import requests
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.tl.types import MessageService
from telethon.errors import FloodWaitError

API_ID = int(os.environ.get('TG_API_ID', '38656404'))
API_HASH = os.environ.get('TG_API_HASH', 'f2cd910275c392039b0864c1dadd47f2')
SESSION_STRING = os.environ.get('TG_SESSION_STRING')
GROUP_ID = int(os.environ.get('TG_GROUP_ID', '-1003610973355'))
GDRIVE_REFRESH_TOKEN = os.environ.get('GDRIVE_REFRESH_TOKEN')
ROOT_FOLDER_ID = os.environ.get('GDRIVE_ROOT_FOLDER_ID', '1LjiY-Y-68Jvcp8Bs62RuNjJDJwD90OzC')

TEMP_DOWNLOAD_DIR = '/tmp/tg_downloads' if os.name != 'nt' else 'C:\\temp\\tg_downloads'
MAX_JOB_DURATION_SEC = 5 * 3600 + 15 * 60  # 5 hours 15 mins (safely under 6 hr limit)

class GoogleDriveManager:
    def __init__(self, refresh_token, root_folder_id):
        self.refresh_token = refresh_token
        self.root_folder_id = root_folder_id
        self.access_token = None
        self.token_expiry = 0
        self.folders_cache = {}
        self.files_cache = {}  # folder_id -> {filename: size}
        self._refresh_token()
        self._load_folders()

    def _refresh_token(self):
        print("🔄 Refreshing Google Drive Access Token...")
        res = requests.post("https://developers.google.com/oauthplayground/refreshAccessToken", json={
            "token_uri": "https://oauth2.googleapis.com/token",
            "refresh_token": self.refresh_token
        }, timeout=30)
        data = res.json()
        if "access_token" not in data:
            raise RuntimeError(f"Failed to refresh Google Drive token: {data}")
        self.access_token = data["access_token"]
        self.token_expiry = time.time() + 3000
        print("✅ Google Drive Access Token Refreshed!")

    def get_auth_header(self):
        if time.time() >= self.token_expiry:
            self._refresh_token()
        return {"Authorization": f"Bearer {self.access_token}"}

    def _load_folders(self):
        print("📂 Scanning existing folders in Google Drive...")
        headers = self.get_auth_header()
        query = f"'{self.root_folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        url = f"https://www.googleapis.com/drive/v3/files?q={query}&fields=files(id,name)&pageSize=100"
        res = requests.get(url, headers=headers, timeout=30).json()
        for item in res.get("files", []):
            self.folders_cache[item["name"]] = item["id"]
        print(f"✅ Found {len(self.folders_cache)} subject folders in Google Drive!")

    def get_or_create_folder(self, folder_name):
        if folder_name in self.folders_cache:
            return self.folders_cache[folder_name]
        headers = self.get_auth_header()
        meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [self.root_folder_id]
        }
        res = requests.post("https://www.googleapis.com/drive/v3/files", headers=headers, json=meta, timeout=30).json()
        folder_id = res["id"]
        self.folders_cache[folder_name] = folder_id
        return folder_id

    def load_files_in_folder(self, folder_id):
        if folder_id in self.files_cache:
            return self.files_cache[folder_id]
        headers = self.get_auth_header()
        files_map = {}
        page_token = None
        while True:
            query = f"'{folder_id}' in parents and trashed = false"
            url = f"https://www.googleapis.com/drive/v3/files?q={query}&fields=nextPageToken,files(id,name,size)&pageSize=1000"
            if page_token:
                url += f"&pageToken={page_token}"
            res = requests.get(url, headers=headers, timeout=30).json()
            for f in res.get("files", []):
                files_map[f["name"]] = int(f.get("size", 0))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        self.files_cache[folder_id] = files_map
        return files_map

    def file_exists(self, folder_id, file_name, file_size):
        files_map = self.load_files_in_folder(folder_id)
        if file_name in files_map:
            existing_size = files_map[file_name]
            if existing_size == file_size or existing_size > 0:
                return True
        return False

    def upload_file_resumable(self, file_path, file_name, folder_id, file_size):
        headers = self.get_auth_header()
        init_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
        init_headers = {
            **headers,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "application/octet-stream",
            "X-Upload-Content-Length": str(file_size)
        }
        metadata = {"name": file_name, "parents": [folder_id]}
        init_res = requests.post(init_url, headers=init_headers, json=metadata, timeout=30)
        if init_res.status_code != 200:
            raise RuntimeError(f"Failed to initiate resumable upload: {init_res.text}")
        
        upload_location = init_res.headers["Location"]
        chunk_size = 10 * 1024 * 1024  # 10 MB upload chunk
        
        with open(file_path, "rb") as f:
            offset = 0
            while offset < file_size:
                chunk = f.read(chunk_size)
                cur_len = len(chunk)
                headers = {
                    "Content-Range": f"bytes {offset}-{offset + cur_len - 1}/{file_size}",
                    "Content-Length": str(cur_len)
                }
                res = requests.put(upload_location, headers=headers, data=chunk, timeout=120)
                offset += cur_len
                
        # Register in cache
        if folder_id in self.files_cache:
            self.files_cache[folder_id][file_name] = file_size
        return True

def clean_name(name):
    return re.sub(r'[\\/*?:"<>|]', '_', str(name))

async def run():
    start_time = time.time()
    os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)
    
    if not SESSION_STRING or not GDRIVE_REFRESH_TOKEN:
        print("❌ Missing TG_SESSION_STRING or GDRIVE_REFRESH_TOKEN environment variable!")
        sys.exit(1)
        
    gdrive = GoogleDriveManager(GDRIVE_REFRESH_TOKEN, ROOT_FOLDER_ID)
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()
    
    entity = await client.get_entity(GROUP_ID)
    print(f"🔗 Connected to Telegram Group: {entity.title}")
    
    try:
        topics = await client(functions.channels.GetForumTopicsRequest(channel=entity, offset_date=None, offset_id=0, offset_topic=0, limit=50))
        all_targets = [(t.id, t.title) for t in topics.topics] + [(None, "General")]
    except Exception as e:
        print(f"⚠️ Could not fetch forum topics directly ({e}), fallback to full stream scan...")
        all_targets = [(None, "General")]
    
    print("\n🔍 Scanning messages across all topics...")
    all_files = []
    for topic_id, topic_title in all_targets:
        clean_topic = clean_name(topic_title)
        folder_id = gdrive.get_or_create_folder(clean_topic)
        gdrive.load_files_in_folder(folder_id)
        
        count = 0
        async for msg in client.iter_messages(entity, reply_to=topic_id, reverse=True):
            if msg.media and not isinstance(msg, MessageService):
                all_files.append((msg, clean_topic, folder_id))
                count += 1
        print(f"  📁 {clean_topic}: {count} media queued")
        
    print(f"\n🔥 TOTAL QUEUED IN COURSE: {len(all_files)} files")
    print("⚡ Starting 24x7 Telegram -> Google Drive Transfer Pipeline...\n")
    
    saved_count = 0
    skipped_count = 0
    total_downloaded_bytes = 0
    
    for i, (msg, topic_name, folder_id) in enumerate(all_files, 1):
        if (time.time() - start_time) > MAX_JOB_DURATION_SEC:
            print("\n⏰ Approaching job time limit (5h 15m). Triggering seamless continuation...")
            break
            
        fname = getattr(msg.file, 'name', None)
        if not fname:
            ext = getattr(msg.file, 'ext', '.bin')
            fname = f"msg_{msg.id}{ext}"
        fname = clean_name(fname)
        file_size = getattr(msg.file, 'size', 0)
        
        if gdrive.file_exists(folder_id, fname, file_size):
            skipped_count += 1
            print(f"⏩ [{i}/{len(all_files)}] [ALREADY SAVED IN GDRIVE]: {topic_name}/{fname}")
            continue
            
        mb = file_size / (1024 * 1024)
        print(f"\n⬇️ [{i}/{len(all_files)}] [DOWNLOADING ({mb:.1f} MB)]: {topic_name}/{fname}")
        t0 = time.time()
        
        temp_path = os.path.join(TEMP_DOWNLOAD_DIR, fname)
        if os.path.exists(temp_path):
            os.remove(temp_path)
            
        try:
            await msg.download_media(file=temp_path)
            if not os.path.exists(temp_path) or os.path.getsize(temp_path) != file_size:
                print(f"⚠️ Download size mismatch on {fname}, retrying...")
                continue
                
            t_down = time.time() - t0
            spd_down = mb / t_down if t_down > 0 else 0
            print(f"  ⚡ Telegram Download Finished ({mb:.1f} MB in {t_down:.1f}s @ {spd_down:.2f} MB/s)")
            
            # Resumable Stream to Google Drive
            t_up0 = time.time()
            print(f"  ☁️ Uploading to Google Drive ({topic_name})...")
            gdrive.upload_file_resumable(temp_path, fname, folder_id, file_size)
            t_up = time.time() - t_up0
            spd_up = mb / t_up if t_up > 0 else 0
            
            saved_count += 1
            total_downloaded_bytes += file_size
            print(f"✅ [COMPLETED IN GDRIVE]: {topic_name}/{fname} (Upload: {t_up:.1f}s @ {spd_up:.2f} MB/s)")
        except FloodWaitError as e:
            print(f"⏳ Telegram FloodWait: waiting {e.seconds}s...")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"⚠️ Error transferring {fname}: {e}")
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                    
    total_gb = total_downloaded_bytes / (1024 ** 3)
    print(f"\n🎉 BATCH SUMMARY: {saved_count} new files saved ({total_gb:.2f} GB transferred), {skipped_count} existing skipped.")
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
