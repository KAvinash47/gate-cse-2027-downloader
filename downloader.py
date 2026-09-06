import os
import re
import sys
import time
import json
import base64
import asyncio
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.tl.types import MessageService
from telethon.errors import FloodWaitError, RpcCallFailError, RPCError

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

API_ID = int(os.environ.get('TG_API_ID', 38656404))
API_HASH = os.environ.get('TG_API_HASH', "f2cd910275c392039b0864c1dadd47f2")
SESSION_STRING = os.environ.get('TG_SESSION_STRING')
if not SESSION_STRING:
    raise ValueError("TG_SESSION_STRING environment variable / secret is required.")
GROUP_ID = -1003610973355
GDRIVE_REFRESH_TOKEN = os.environ.get('GDRIVE_REFRESH_TOKEN')
GDRIVE_CLIENT_ID = os.environ.get('GDRIVE_CLIENT_ID', '')
GDRIVE_CLIENT_SECRET = os.environ.get('GDRIVE_CLIENT_SECRET', '')

if not GDRIVE_REFRESH_TOKEN:
    raise ValueError("GDRIVE_REFRESH_TOKEN environment variable / secret is required.")
ROOT_FOLDER_ID = "1LjiY-Y-68Jvcp8Bs62RuNjJDJwD90OzC"

TEMP_DOWNLOAD_DIR = '/tmp/tg_downloads' if os.name != 'nt' else 'C:\\temp\\tg_downloads'
MAX_JOB_DURATION_SEC = 5 * 3600 + 20 * 60  # 5 hours 20 mins

# Priority ranking for subjects (DBMS, Algo, TOC first!)
PRIORITY_KEYWORDS = [
    ["dbms", "database"],
    ["algo", "algorithm"],
    ["toc", "theory of computation", "automata"],
    ["data structure", "ds"],
    ["operating system", "os"],
    ["compiler"],
    ["coa", "architecture"],
    ["engineering mathematics", "math"],
    ["discrete", "dm"],
    ["deep learning", "dl"],
    ["cn", "network"],
    ["aptitude", "reasoning"],
]

def get_topic_priority(topic_title):
    title_lower = str(topic_title).lower()
    for rank, keywords in enumerate(PRIORITY_KEYWORDS):
        if any(k in title_lower for k in keywords):
            return rank
    return 999

class GoogleDriveManager:
    def __init__(self, refresh_token, root_folder_id, client_id=None, client_secret=None):
        self.refresh_token = refresh_token
        self.root_folder_id = root_folder_id
        self.client_id = client_id or GDRIVE_CLIENT_ID
        self.client_secret = client_secret or GDRIVE_CLIENT_SECRET
        self.access_token = None
        self.token_expiry = 0
        self.folders_cache = {}
        self.files_cache = {}  # folder_id -> {filename: size}
        
        # Robust HTTP session with auto retries
        self.session = requests.Session()
        retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504, 429])
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        
        self._refresh_token()
        self._load_folders()

    def _refresh_token(self):
        print("🔄 Refreshing Google Drive Access Token...", flush=True)
        if self.client_id and self.client_secret:
            res = self.session.post("https://oauth2.googleapis.com/token", data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token"
            }, timeout=30)
            data = res.json()
        else:
            res = self.session.post("https://developers.google.com/oauthplayground/refreshAccessToken", json={
                "token_uri": "https://oauth2.googleapis.com/token",
                "refresh_token": self.refresh_token
            }, timeout=30)
            data = res.json()

        if "access_token" not in data:
            raise RuntimeError(f"Failed to refresh Google Drive token: {data}")
        self.access_token = data["access_token"]
        self.token_expiry = time.time() + 3000
        print("✅ Google Drive Access Token Refreshed!", flush=True)

    def get_auth_header(self):
        if time.time() >= self.token_expiry:
            self._refresh_token()
        return {"Authorization": f"Bearer {self.access_token}"}

    def _load_folders(self):
        print("📂 Scanning existing folders in Google Drive...", flush=True)
        headers = self.get_auth_header()
        query = f"'{self.root_folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        url = f"https://www.googleapis.com/drive/v3/files?q={query}&fields=files(id,name)&pageSize=100"
        res = self.session.get(url, headers=headers, timeout=30).json()
        for item in res.get("files", []):
            self.folders_cache[item["name"]] = item["id"]
        print(f"✅ Found {len(self.folders_cache)} subject folders in Google Drive!", flush=True)

    def get_or_create_folder(self, folder_name):
        if folder_name in self.folders_cache:
            return self.folders_cache[folder_name]
        headers = self.get_auth_header()
        meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [self.root_folder_id]
        }
        res = self.session.post("https://www.googleapis.com/drive/v3/files", headers=headers, json=meta, timeout=30).json()
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
            res = self.session.get(url, headers=headers, timeout=30).json()
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
        init_res = self.session.post(init_url, headers=init_headers, json=metadata, timeout=45)
        
        # Auto refresh if expired
        if init_res.status_code == 401:
            self._refresh_token()
            headers = self.get_auth_header()
            init_headers["Authorization"] = headers["Authorization"]
            init_res = self.session.post(init_url, headers=init_headers, json=metadata, timeout=45)
            
        if init_res.status_code != 200:
            raise RuntimeError(f"Failed to initiate resumable upload: {init_res.text}")
        
        upload_location = init_res.headers["Location"]
        chunk_size = 10 * 1024 * 1024  # 10 MB fast upload chunk
        
        with open(file_path, "rb") as f:
            offset = 0
            while offset < file_size:
                chunk = f.read(chunk_size)
                cur_len = len(chunk)
                headers = {
                    "Content-Range": f"bytes {offset}-{offset + cur_len - 1}/{file_size}",
                    "Content-Length": str(cur_len)
                }
                for attempt in range(5):
                    try:
                        res = self.session.put(upload_location, headers=headers, data=chunk, timeout=60)
                        if res.status_code in [200, 201, 308]:
                            break
                    except Exception as e:
                        if attempt == 4:
                            raise e
                        time.sleep(2 ** attempt)
                offset += cur_len
                
        # Register in cache
        if folder_id in self.files_cache:
            self.files_cache[folder_id][file_name] = file_size
        return True

try:
    import FastTelethon
except ImportError:
    FastTelethon = None

def clean_name(name):
    return re.sub(r'[\\/*?:"<>|]', '_', str(name))

async def download_media_resumable(client, msg, temp_path, expected_size):
    """Download Telegram media file using native MTProto downloader with progress callback."""
    if not client.is_connected():
        print("🔄 Reconnecting Telegram MTProto socket...", flush=True)
        await client.connect()

    if os.path.exists(temp_path) and os.path.getsize(temp_path) == expected_size and expected_size > 0:
        return True

    last_log_time = [time.time()]

    def progress_callback(current, total):
        now = time.time()
        if now - last_log_time[0] >= 4 or current >= total:
            pct = (current / total * 100) if total > 0 else 0
            cur_mb = current / (1024 * 1024)
            tot_mb = total / (1024 * 1024)
            print(f"  ⏳ Progress: {cur_mb:.1f}/{tot_mb:.1f} MB ({pct:.1f}%)", flush=True)
            last_log_time[0] = now

    for attempt in range(5):
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            res = await client.download_media(msg.media, file=temp_path, progress_callback=progress_callback)
            if res and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                return True
        except FloodWaitError as e:
            print(f"  ⏳ Telegram FloodWait: sleeping {e.seconds}s...", flush=True)
            await asyncio.sleep(e.seconds + 2)
        except RPCError as e:
            print(f"  ⚠️ Telegram RPC error (attempt {attempt+1}): {e}", flush=True)
            await asyncio.sleep(5)
        except Exception as e:
            print(f"  ⚠️ Download error (attempt {attempt+1}): {e}", flush=True)
            await asyncio.sleep(3 * (attempt + 1))

    return os.path.exists(temp_path) and os.path.getsize(temp_path) > 0

async def run():
    start_time = time.time()
    os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)
    
    gdrive = GoogleDriveManager(GDRIVE_REFRESH_TOKEN, ROOT_FOLDER_ID)
    
    # Initialize Telethon with automatic 300s flood sleep handling
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, flood_sleep_threshold=300)
    await client.connect()
    
    entity = await client.get_entity(GROUP_ID)
    print(f"🔗 Connected to Telegram Group: {entity.title}", flush=True)
    
    try:
        topics = await client(functions.channels.GetForumTopicsRequest(channel=entity, offset_date=None, offset_id=0, offset_topic=0, limit=50))
        all_targets = [(t.id, t.title) for t in topics.topics] + [(None, "General")]
    except Exception as e:
        print(f"⚠️ Fallback to general scan ({e})", flush=True)
        all_targets = [(None, "General")]

    # Sort topics by priority (Database/DBMS, Algo, TOC first!)
    all_targets.sort(key=lambda t: (get_topic_priority(t[1]), t[1]))
    
    print("\n📋 Topic Execution Order (Priority Weighted):", flush=True)
    for idx, (tid, title) in enumerate(all_targets, 1):
        p = get_topic_priority(title)
        badge = "🔥 [PRIORITY 1]" if p in [0, 1, 2] else "📌 [PRIORITY 2]" if p < 10 else "📁"
        print(f"  {idx}. {badge} {title}", flush=True)
    
    print("\n🔍 Scanning messages across all topics in priority order...", flush=True)
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
        print(f"  📁 {clean_topic}: {count} media queued", flush=True)
        
    print(f"\n🔥 TOTAL QUEUED IN COURSE: {len(all_files)} files", flush=True)
    print("⚡ Starting Resilient Telegram ➔ Google Drive Pipeline...\n", flush=True)
    
    stats = {"saved": 0, "skipped": 0, "bytes": 0}
    
    for item_idx, (msg, topic_name, folder_id) in enumerate(all_files, 1):
        if (time.time() - start_time) > MAX_JOB_DURATION_SEC:
            print("\n⏰ Reached maximum 5-hour batch window, ending gracefully...", flush=True)
            break
            
        fname = getattr(msg.file, 'name', None)
        if not fname:
            ext = getattr(msg.file, 'ext', '.bin')
            fname = f"msg_{msg.id}{ext}"
        fname = clean_name(fname)
        file_size = getattr(msg.file, 'size', 0)
        
        if gdrive.file_exists(folder_id, fname, file_size):
            stats["skipped"] += 1
            print(f"⏩ [{item_idx}/{len(all_files)}] [EXISTS]: {topic_name}/{fname}", flush=True)
            continue
            
        mb = file_size / (1024 * 1024)
        print(f"\n⬇️ [{item_idx}/{len(all_files)}] [DOWNLOADING ({mb:.1f} MB)]: {topic_name}/{fname}", flush=True)
        t0 = time.time()
        
        temp_path = os.path.join(TEMP_DOWNLOAD_DIR, f"temp_{clean_name(fname)}")
        
        ok = await download_media_resumable(client, msg, temp_path, file_size)
        if ok:
            t_down = time.time() - t0
            spd_down = mb / t_down if t_down > 0 else 0
            print(f"  ⚡ Download complete ({mb:.1f} MB in {t_down:.1f}s @ {spd_down:.2f} MB/s)", flush=True)
            
            # Resumable Stream to Google Drive
            t_up0 = time.time()
            print(f"  ☁️ Uploading to Google Drive ({topic_name})...", flush=True)
            gdrive.upload_file_resumable(temp_path, fname, folder_id, file_size)
            t_up = time.time() - t_up0
            spd_up = mb / t_up if t_up > 0 else 0
            
            stats["saved"] += 1
            stats["bytes"] += file_size
            print(f"✅ [SAVED TO GDRIVE]: {topic_name}/{fname} (Up: {t_up:.1f}s @ {spd_up:.2f} MB/s)", flush=True)
            
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
        else:
            print(f"❌ Failed transfer for {fname}, moving to next file.", flush=True)
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                        
        # Gentle inter-file rest to keep Telegram MTProto happy
        await asyncio.sleep(1.0)
        
    total_gb = stats["bytes"] / (1024 ** 3)
    print(f"\n🎉 BATCH SUMMARY: {stats['saved']} new files saved ({total_gb:.2f} GB transferred), {stats['skipped']} existing skipped.", flush=True)
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
