    async def track_statistics(self, event_type: str, user_id: int, url: str, success: bool = True):
        """Record statistics for analysis"""
        await MongoDB.stats.update_one(
            {'user_id': user_id, 'url': url},
            {'$inc': {f'stats.{event_type}.{"success" if success else "failure"}': 1}},
            upsert=True
        )

    async def get_statistics(self, user_id: int) -> Dict:
        """Get aggregated statistics for user"""
        pipeline = [
            {'$match': {'user_id': user_id}},
            {'$group': {
                '_id': None,
                'total_tracked': {'$sum': 1},
                'success_downloads': {'$sum': '$stats.downloads.success'},
                'failed_downloads': {'$sum': '$stats.downloads.failure'},
                'uptime_percentage': {
                    '$avg': {
                        '$cond': [
                            {'$eq': ['$stats.checks.success', 0]},
                            0,
                            {'$divide': ['$stats.checks.success', {'$add': ['$stats.checks.success', '$stats.checks.failure']}]}
                        ]
                    }
                }
            }}
        ]

        result = await MongoDB.stats.aggregate(pipeline).to_list(1)
        return result[0] if result else {}

    # Archives system
    async def create_archive(self, user_id: int, url: str, content: str):
        """Create historical archive of webpage content"""
        await MongoDB.archives.insert_one({
            'user_id': user_id,
            'url': url,
            'content': content,
            'timestamp': datetime.now()
        })

    async def get_archives(self, user_id: int, url: str) -> List[Dict]:
        """Retrieve archives for specific URL"""
        return await MongoDB.archives.find({
            'user_id': user_id,
            'url': url
        }).sort('timestamp', -1).to_list(None)

    # Content diff system
    async def generate_diff(self, old_content: str, new_content: str) -> str:
        """Generate human-readable diff between versions"""
        diff = difflib.unified_diff(
            old_content.splitlines(),
            new_content.splitlines(),
            fromfile='Previous',
            tofile='Current',
            lineterm=''
        )
        return '\n'.join(diff)[:MAX_MESSAGE_LENGTH]

    # Notification system
    async def send_notification(self, user_id: int, url: str, changes: str):
        """Send customized notification based on user preferences"""
        settings = await MongoDB.notifications.find_one({'user_id': user_id}) or {}

        message_format = settings.get('format', 'text')
        frequency = settings.get('frequency', 'immediate')

        if message_format == 'text':
            await self.app.send_message(user_id, f"🔔 Update detected for {url}:\n{changes}")
        elif message_format == 'html':
            await self.app.send_message(user_id, f"<b>Update detected</b> for {url}:\n<pre>{changes}</pre>", parse_mode=enums.ParseMode.HTML)

    # Maintenance jobs
    async def cleanup_old_archives(self):
        """Cleanup archives older than retention period"""
        cutoff = datetime.now() - timedelta(days=ARCHIVE_RETENTION_DAYS)
        await MongoDB.archives.delete_many({'timestamp': {'$lt': cutoff}})
        logger.info("Cleaned up old archives")

    async def aggregate_statistics(self):
        """Aggregate statistics for better performance"""
        # Implement your aggregation logic here
        logger.info("Statistics aggregation completed")

    # Updated tracking logic
    async def check_updates(self, user_id: int, url: str):
        try:
            tracked_data = await MongoDB.urls.find_one({'user_id': user_id, 'url': url})
            if not tracked_data:
                return

            # Night mode check 
            if tracked_data.get('night_mode'):
                tz = pytz.timezone(TIMEZONE)
                now = datetime.now(tz)
                if not (9 <= now.hour < 22):  # From 9 AM To 10 PM
                    logger.info(f"Due to night mode, {url} was skipped" )
                    return

            current_content, new_resources = await self.get_webpage_content(url)
            await self.create_archive(user_id, url, current_content)

            previous_hash = tracked_data.get('content_hash', '')
            current_hash = hashlib.md5(current_content.encode()).hexdigest()

            if current_hash != previous_hash:
                diff_content = await self.generate_diff(
                    tracked_data.get('content', ''),
                    current_content
                )
                await self.send_notification(user_id, url, diff_content)

                await self.track_statistics('content_changes', user_id, url)

            filtered_resources = []
            for resource in new_resources:
                if await self.apply_filters(resource, user_id):
                    filtered_resources.append(resource)

            if filtered_resources:
                sent_hashes = []
                for resource in filtered_resources:
                    if await self.send_media(user_id, resource, tracked_data):
                        sent_hashes.append(resource['hash'])
                        await self.track_statistics('downloads', user_id, url, success=True)
                    else:
                        await self.track_statistics('downloads', user_id, url, success=False)

                update_data = {
                    'content_hash': current_hash,
                    'last_checked': datetime.now()
                }

                if sent_hashes:
                    update_data['$push'] = {'sent_hashes': {'$each': sent_hashes}}

                await MongoDB.urls.update_one(
                    {'_id': tracked_data['_id']},
                    {'$set': update_data}
                )

        except Exception as e:
            logger.error(f"Update check failed for {url}: {str(e)}")
            await self.track_statistics('checks', user_id, url, success=False)

    # New command handlers
    async def filter_handler(self, client: Client, message: Message):
        """Handle filter configuration"""
        pass  # Implement filter configuration logic

    async def export_handler(self, client: Client, message: Message):
        """Handle export commands"""
        try:
            format = message.command[1].lower()
            if format not in ['json', 'csv']:
                return await message.reply("Invalid format. Use /export json|csv")

            filename = await self.export_data(message.chat.id, format)
            await message.reply_document(filename)
            await async_os.remove(filename)
        except Exception as e:
            await message.reply(f"Export failed: {str(e)}")

    async def stats_handler(self, client: Client, message: Message):
        """Show statistics dashboard"""
        try:
            stats = await self.get_statistics(message.chat.id)
            response = (
                "📊 Statistics Dashboard\n\n"
                f"Tracked URLs: {stats.get('total_tracked', 0)}\n"
                f"Success Downloads: {stats.get('success_downloads', 0)}\n"
                f"Failed Downloads: {stats.get('failed_downloads', 0)}\n"
                f"Uptime Percentage: {stats.get('uptime_percentage', 0)*100:.2f}%"
            )
            await message.reply(response)
        except Exception as e:
            await message.reply(f"Failed to get stats: {str(e)}")

    async def archive_handler(self, client: Client, message: Message):
        """Handle archive commands"""
        pass  # Implement archive listing/retrieval

    async def notification_handler(self, client: Client, message: Message):
        """Handle notification settings"""
        pass  # Implement notification configuration

    # Enhanced Web Monitoring
    async def get_webpage_content(self, url: str) -> Tuple[str, List[Dict]]:
        try:
            async with self.http.get(url, timeout=30) as resp:
                content = await resp.text()
                soup = BeautifulSoup(content, 'lxml')

                resources = []
                seen_hashes = set()

                for tag in soup.find_all(['a', 'img', 'audio', 'video', 'source']):
                    resource_url = None
                    if tag.name == 'a' and (href := tag.get('href')):
                        resource_url = unquote(urljoin(url, href))
                    elif (src := tag.get('src')):
                        resource_url = unquote(urljoin(url, src))

                    if resource_url:
                        ext = os.path.splitext(resource_url)[1].lower()
                        for file_type, extensions in SUPPORTED_EXTENSIONS.items():
                            if ext in extensions:
                                file_hash = hashlib.md5(resource_url.encode()).hexdigest()
                                resources.append({
                                    'url': resource_url,
                                    'type': file_type,
                                    'hash': file_hash
                                })
                                break

                return content, resources
        except Exception as e:
            logger.error(f"Web monitoring error: {str(e)}")
            return "", []

    async def check_updates(self, user_id: int, url: str):
        try:
            tracked_data = await MongoDB.urls.find_one({'user_id': user_id, 'url': url})
            if not tracked_data:
                return

            current_content, new_resources = await self.get_webpage_content(url)
            previous_hash = tracked_data.get('content_hash', '')
            current_hash = hashlib.md5(current_content.encode()).hexdigest()

            if current_hash != previous_hash or new_resources:
                text_changes = f"🔄 Website Updated: {url}\n" + \
                             f"📅 Change detected at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"

                await self.safe_send_message(user_id, text_changes)

                sent_hashes = []
                for resource in new_resources:
                    if resource['hash'] not in tracked_data.get('sent_hashes', []):
                        if await self.send_media(user_id, resource, tracked_data):
                            sent_hashes.append(resource['hash'])

                update_data = {
                    'content_hash': current_hash,
                    'last_checked': datetime.now()
                }

                if sent_hashes:
                    update_data['$push'] = {'sent_hashes': {'$each': sent_hashes}}

                await MongoDB.urls.update_one(
                    {'_id': tracked_data['_id']},
                    {'$set': update_data}
                )

        except Exception as e:
            logger.error(f"Update check failed for {url}: {str(e)}")
            await self.app.send_message(user_id, f"⚠️ Error checking updates for {url}")

    # Media Sending
    async def send_media(self, user_id: int, resource: Dict, tracked_data: Dict) -> bool:
        try:
            caption = (
                f"📁 {tracked_data.get('name', 'Unnamed')}\n"
                f"🔗 Source: {tracked_data['url']}\n"
                f"📥 Direct URL: {resource['url']}"
            )

            file_path = await self.ytdl_download(resource['url'])
            if not file_path:
                file_path = await self.direct_download(resource['url'])

            if not file_path:
                return False

            file_size = os.path.getsize(file_path)
            if file_size > MAX_FILE_SIZE:
                logger.warning(f"File too big: {file_size} bytes")
                return False

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

            await async_os.remove(file_path)
            return True

        except Exception as e:
            logger.error(f"Media send failed: {str(e)}")
            return False

Es code ko achha kijiye taki sb kuchh shi tarike se ho
