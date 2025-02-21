# Added necessary imports (assuming at top)
import asyncio
from typing import Dict, List, Tuple
from datetime import datetime, timedelta
import difflib
import hashlib
import os
from urllib.parse import unquote
from tempfile import TemporaryDirectory

async def track_statistics(self, event_type: str, user_id: int, url: str, success: bool = True):
    """Record statistics for analysis with validation"""
    # Validate event type to prevent injection
    valid_events = {'downloads', 'checks', 'content_changes'}
    if event_type not in valid_events:
        raise ValueError(f"Invalid event type: {event_type}")
    
    # Use bulk writes for better performance if tracking multiple stats
    await MongoDB.stats.update_one(
        {'user_id': user_id, 'url': url},
        {'$inc': {f'stats.{event_type}.{"success" if success else "failure"}': 1},
        upsert=True
    )

async def get_statistics(self, user_id: int) -> Dict:
    """Get accurate aggregated statistics for user"""
    pipeline = [
        {'$match': {'user_id': user_id}},
        {'$group': {
            '_id': None,
            'total_tracked': {'$sum': 1},
            'total_checks': {
                '$sum': {
                    '$add': [
                        '$stats.checks.success',
                        '$stats.checks.failure'
                    ]
                }
            },
            'success_checks': {'$sum': '$stats.checks.success'},
            'success_downloads': {'$sum': '$stats.downloads.success'},
            'failed_downloads': {'$sum': '$stats.downloads.failure'},
        }},
        {'$project': {
            'total_tracked': 1,
            'success_downloads': 1,
            'failed_downloads': 1,
            'uptime_percentage': {
                '$cond': [
                    {'$eq': ['$total_checks', 0]},
                    0,
                    {'$divide': ['$success_checks', '$total_checks']}
                ]
            }
        }}
    ]

    result = await MongoDB.stats.aggregate(pipeline).to_list(1)
    return result[0] if result else {}

async def check_updates(self, user_id: int, url: str):
    """Consolidated update checking logic"""
    try:
        tracked_data = await MongoDB.urls.find_one({'user_id': user_id, 'url': url})
        if not tracked_data:
            return

        # Night mode check
        if tracked_data.get('night_mode'):
            tz = pytz.timezone(TIMEZONE)
            now = datetime.now(tz)
            if not (9 <= now.hour < 22):
                logger.info(f"Night mode active, skipping {url}")
                await self.track_statistics('checks', user_id, url, success=False)
                return

        current_content, new_resources = await self.get_webpage_content(url)
        await self.create_archive(user_id, url, current_content)

        previous_hash = tracked_data.get('content_hash', '')
        current_hash = hashlib.sha256(current_content.encode()).hexdigest()  # Better hash

        changes_detected = False
        text_changes = ""

        if current_hash != previous_hash:
            old_content = tracked_data.get('content', '')
            if old_content:
                diff_content = await self.generate_diff(old_content, current_content)
                text_changes = f"🔄 Content Updated: {url}\n{diff_content}"
            else:
                text_changes = f"🔍 Initial Content Saved: {url}"
            
            changes_detected = True
            await self.track_statistics('content_changes', user_id, url)

        filtered_resources = [
            resource for resource in new_resources
            if await self.apply_filters(resource, user_id)
        ]

        sent_hashes = []
        for resource in filtered_resources:
            if resource['hash'] not in tracked_data.get('sent_hashes', []):
                if await self.send_media(user_id, resource, tracked_data):
                    sent_hashes.append(resource['hash'])
                    await self.track_statistics('downloads', user_id, url, success=True)
                else:
                    await self.track_statistics('downloads', user_id, url, success=False)

        if changes_detected or sent_hashes:
            if text_changes:
                await self.safe_send_message(user_id, text_changes)

            update_data = {
                'content_hash': current_hash,
                'last_checked': datetime.now(),
                '$push': {'sent_hashes': {'$each': sent_hashes}}
            }
            await MongoDB.urls.update_one(
                {'_id': tracked_data['_id']},
                {'$set': update_data}
            )

        await self.track_statistics('checks', user_id, url, success=True)

    except Exception as e:
        logger.error(f"Update check failed for {url}: {str(e)}")
        await self.track_statistics('checks', user_id, url, success=False)
        await self.app.send_message(user_id, f"⚠️ Error checking {url}: {str(e)}")

async def send_media(self, user_id: int, resource: Dict, tracked_data: Dict) -> bool:
    """Improved media sending with temp files and retries"""
    try:
        caption = (
            f"📁 {tracked_data.get('name', 'Unnamed')}\n"
            f"🔗 Source: {tracked_data['url']}\n"
            f"📥 Direct URL: {resource['url']}"
        )

        with TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, os.path.basename(resource['url']))
            
            # Try multiple download methods
            downloaders = [self.ytdl_download, self.direct_download]
            for downloader in downloaders:
                if await downloader(resource['url'], file_path):
                    break
            else:
                return False

            file_size = os.path.getsize(file_path)
            if file_size > MAX_FILE_SIZE:
                logger.warning(f"File too big: {file_size} bytes")
                return False

            # Use appropriate send method
            send_methods = {
                'pdf': self.app.send_document,
                'image': self.app.send_photo,
                'audio': self.app.send_audio,
                'video': self.app.send_video
            }

            method = send_methods.get(resource['type'], self.app.send_document)
            await method(
                user_id,
                file_path,
                caption=caption[:1024],
                parse_mode=enums.ParseMode.HTML
            )

        return True
    except Exception as e:
        logger.error(f"Media send failed: {str(e)}")
        return False

async def safe_send_message(self, user_id: int, text: str):
    """Handle message splitting and formatting"""
    MAX_LENGTH = 4096  # Telegram message limit
    while text:
        chunk, text = text[:MAX_LENGTH], text[MAX_LENGTH:]
        await self.app.send_message(user_id, chunk)
        await asyncio.sleep(0.5)  # Prevent flooding

# Suggested database indexes (add during initialization)
# MongoDB.stats.create_index([('user_id', 1), ('url', 1)])
# MongoDB.archives.create_index([('user_id', 1), ('url', 1), ('timestamp', -1)])
# MongoDB.archives.create_index([('timestamp', 1)], expireAfterSeconds=ARCHIVE_RETENTION_DAYS*86400)
