import os
import asyncio
import time
import threading
import mimetypes
from typing import Dict, Optional
from pyrogram import Client, enums
from pyrogram.types import Message
import yt_dlp
import async_os
from urllib.parse import urlparse

class DownloadHandler:
    def __init__(self):
        self.active_tasks: Dict[int, bool] = {}
        self.progress_data: Dict[int, dict] = {}
        self.lock = threading.Lock()
        self.ydl_opts = {
            'format': 'best',
            'outtmpl': 'downloads/%(title)s.%(ext)s',
            'progress_hooks': [self.progress_hook],
        }

    async def is_authorized(self, message: Message) -> bool:
        # Implement your authorization logic
        return True

    def progress_hook(self, d):
        chat_id = d.get('info_dict', {}).get('__original_chat_id')
        if chat_id and d['status'] == 'downloading':
            with self.lock:
                self.progress_data[chat_id] = {
                    'status': 'downloading',
                    'percent': d.get('_percent_str', '0%'),
                    'speed': d.get('_speed_str', 'N/A'),
                    'downloaded': d.get('_downloaded_bytes_str', '0MB'),
                    'total': d.get('_total_bytes_str', '?MB')
                }

    def format_speed(self, speed_bps: float) -> str:
        if speed_bps >= 1024 * 1024:
            return f"{speed_bps / 1024 / 1024:.2f} MB/s"
        elif speed_bps >= 1024:
            return f"{speed_bps / 1024:.2f} KB/s"
        return f"{speed_bps:.2f} B/s"

    def format_size(self, size_bytes: int) -> str:
        if size_bytes >= 1024 * 1024 * 1024:
            return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"
        elif size_bytes >= 1024 * 1024:
            return f"{size_bytes / 1024 / 1024:.2f} MB"
        elif size_bytes >= 1024:
            return f"{size_bytes / 1024:.2f} KB"
        return f"{size_bytes} B"

    def upload_progress(self, current: int, total: int, chat_id: int):
        now = time.time()
        with self.lock:
            data = self.progress_data.get(chat_id, {})
            last_time = data.get('upload_last_time', now)
            last_bytes = data.get('upload_last_bytes', 0)
            elapsed = now - last_time

            speed_bps = (current - last_bytes) / elapsed if elapsed > 0 else 0
            percent = (current / total) * 100 if total > 0 else 0

            update_data = {
                'status': 'uploading',
                'percent': f"{percent:.1f}%",
                'upload_speed': self.format_speed(speed_bps),
                'uploaded': self.format_size(current),
                'upload_total': self.format_size(total) if total > 0 else "?",
                'upload_last_time': now,
                'upload_last_bytes': current
            }
            data.update(update_data)
            self.progress_data[chat_id] = data

    async def split_file(self, file_path: str, chunk_size: int = 2000 * 1024 * 1024) -> list:
        """Split files larger than 2GB into chunks"""
        part_paths = []
        part_num = 0
        
        with open(file_path, 'rb') as f:
            while True:
                part_path = f"{file_path}.part{part_num:03d}"
                with open(part_path, 'wb') as part_file:
                    remaining = chunk_size
                    while remaining > 0:
                        data = f.read(min(remaining, 100 * 1024 * 1024))
                        if not data:
                            break
                        part_file.write(data)
                        remaining -= len(data)
                    
                    if os.path.getsize(part_path) == 0:
                        os.remove(part_path)
                        break
                    
                    part_paths.append(part_path)
                    part_num += 1
                    
                if remaining > 0:
                    break
        
        return part_paths

    async def update_progress(self, client: Client, chat_id: int, msg_id: int):
        """Update progress every 5 seconds"""
        while True:
            await asyncio.sleep(5)
            with self.lock:
                data = self.progress_data.get(chat_id, {})
            
            status = data.get('status', 'idle')
            message = ""
            
            if status == 'downloading':
                message = (
                    f"📥 Downloading...\n"
                    f"▰ {data.get('percent', '0%')}\n"
                    f"⚡ Speed: {data.get('speed', 'N/A')}\n"
                    f"📦 Size: {data.get('downloaded', '0MB')} / {data.get('total', '?MB')}"
                )
            elif status == 'uploading':
                message = (
                    f"📤 Uploading...\n"
                    f"▰ {data.get('percent', '0%')}\n"
                    f"⚡ Speed: {data.get('upload_speed', 'N/A')}\n"
                    f"📦 Progress: {data.get('uploaded', '0B')} / {data.get('upload_total', '?B')}"
                )
            else:
                continue

            try:
                await client.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=message
                )
            except Exception as e:
                logger.error(f"Progress update error: {str(e)}")

    async def ytdl_download(self, url: str, chat_id: int) -> Optional[str]:
        """Download with progress tracking"""
        try:
            info_dict = None
            def ydl_progress(d):
                nonlocal info_dict
                if d['status'] == 'finished':
                    info_dict = d['info_dict']

            ydl_opts = self.ydl_opts.copy()
            ydl_opts['progress_hooks'] = [ydl_progress, self.progress_hook]

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
                filename = ydl.prepare_filename(info)
                return filename

        except Exception as e:
            logger.error(f"Download Error: {str(e)}")
            return None

    async def ytdl_handler(self, client: Client, message: Message):
        """Handle /dl command with full progress tracking"""
        chat_id = message.chat.id
        
        if self.active_tasks.get(chat_id, False):
            return await message.reply("⏳ A download is already in progress. Please wait...")

        if not await self.is_authorized(message):
            return await message.reply("❌ Authorization failed!")

        url = ' '.join(message.command[1:]).strip()
        if not url:
            return await message.reply("❌ Please provide a URL to download")

        try:
            self.active_tasks[chat_id] = True
            status_msg = await message.reply("📥 Starting download...")
            progress_task = asyncio.create_task(
                self.update_progress(client, chat_id, status_msg.id)
            )

            file_path = await self.ytdl_download(url, chat_id)
            if not file_path:
                await status_msg.edit("❌ Download failed")
                return

            # Prepare for upload phase
            with self.lock:
                self.progress_data[chat_id] = {
                    'status': 'uploading',
                    'percent': '0%',
                    'upload_speed': 'Calculating...',
                    'uploaded': '0B',
                    'upload_total': '?B'
                }

            # Handle large files
            file_size = os.path.getsize(file_path)
            if file_size > 2 * 1024**3:
                split_parts = await self.split_file(file_path)
                for part in split_parts:
                    await client.send_document(
                        chat_id=chat_id,
                        document=part,
                        caption=f"📥 Downloaded from {url}\n💳 Name: {os.path.basename(file_path)}\n🔗 Part: {os.path.basename(part)}",
                        progress=self.upload_progress,
                        progress_args=(chat_id,)
                    )
                    await async_os.remove(part)
                await async_os.remove(file_path)
                return

            # Determine file type and send
            mime_type, _ = mimetypes.guess_type(file_path)
            file_extension = os.path.splitext(file_path)[1].lower()

            file_type = 'document'
            if mime_type:
                if mime_type.startswith('image'):
                    file_type = 'image'
                elif mime_type.startswith('audio'):
                    file_type = 'audio'
                elif mime_type.startswith('video'):
                    file_type = 'video'
                elif mime_type == 'application/pdf':
                    file_type = 'pdf'
            else:
                if file_extension == '.pdf':
                    file_type = 'pdf'
                elif file_extension in ('.jpg', '.jpeg', '.png', '.gif'):
                    file_type = 'image'
                elif file_extension in ('.mp3', '.wav', '.ogg'):
                    file_type = 'audio'
                elif file_extension in ('.mp4', '.mkv', '.avi', '.mov'):
                    file_type = 'video'

            send_methods = {
                'pdf': client.send_document,
                'image': client.send_photo,
                'audio': client.send_audio,
                'video': client.send_video
            }
            method = send_methods.get(file_type, client.send_document)

            caption = f"📥 Downloaded from {url}\n💳 Name: {os.path.basename(file_path)}"

            await method(
                chat_id,
                file_path,
                caption=caption[:1024],
                parse_mode=enums.ParseMode.HTML,
                progress=self.upload_progress,
                progress_args=(chat_id,)
            )
            await async_os.remove(file_path)

        except Exception as e:
            logger.error(f"Error: {str(e)}")
            await message.reply("❌ Error processing the request")
        finally:
            self.active_tasks.pop(chat_id, None)
            if 'progress_task' in locals():
                progress_task.cancel()
            with self.lock:
                self.progress_data.pop(chat_id, None)
