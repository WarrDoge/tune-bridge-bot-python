"""Main bot class — orchestrates parsing, searching, and UI."""

import asyncio
import json
import logging
import os
import re
import signal
import sys
from dataclasses import asdict
from typing import Optional, Tuple

from tune_bridge_bot.models import Platform, SongInfo, MusicLinks
from tune_bridge_bot.text import TextNormalizer
from tune_bridge_bot.infra import (
    SmartCache, HTTPClient,
    CACHE_TTL_SUCCESS, CACHE_TTL_FAILURE, CACHE_TTL_INFO,
)
from tune_bridge_bot.parsers import SpotifyParser, YouTubeParser, AppleMusicParser, _canonicalize_spotify_url
from tune_bridge_bot.matching import SearchEngine, MatchValidator

# Third-party
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
    from telegram.constants import ParseMode, ChatAction
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install python-telegram-bot httpx beautifulsoup4 lxml fuzzywuzzy python-levenshtein")
    sys.exit(1)

logger = logging.getLogger(__name__)


class MusicBot:
    """Main bot class with all functionality"""

    def __init__(self, token: str):
        self.token = token
        self.http_client = HTTPClient()

        # Caches
        self.query_cache = SmartCache()  # Query -> URL
        self.info_cache = SmartCache()   # URL -> SongInfo

        # Statistics
        self.stats = {
            'messages_processed': 0,
            'successful_conversions': 0,
            'failed_conversions': 0,
            'cache_hits': 0
        }

    async def start(self):
        """Start the bot"""
        try:
            # Build application
            self.application = (
                Application.builder()
                .token(self.token)
                .concurrent_updates(True)
                .build()
            )

            # Add handlers
            self.application.add_handler(CommandHandler("start", self.cmd_start))
            self.application.add_handler(CommandHandler("help", self.cmd_help))
            self.application.add_handler(CommandHandler("stats", self.cmd_stats))
            self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

            # Start bot
            logger.info("Starting bot...")
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling()

            # Keep running
            logger.info("Bot is running! Press Ctrl+C to stop.")

            # Run cache cleanup task
            asyncio.create_task(self._cache_cleanup_loop())

            # Wait until stopped
            stop_event = asyncio.Event()

            def signal_handler(signum, frame):
                logger.info("Received shutdown signal")
                stop_event.set()

            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)

            await stop_event.wait()

        finally:
            await self.stop()

    async def stop(self):
        """Stop the bot"""
        logger.info("Stopping bot...")
        if hasattr(self, 'application'):
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
        await self.http_client.close()
        logger.info("Bot stopped")

    async def _cache_cleanup_loop(self):
        """Periodically clean up expired cache entries"""
        while True:
            await asyncio.sleep(300)  # Every 5 minutes
            self.query_cache.cleanup()
            self.info_cache.cleanup()
            logger.debug("Cache cleanup completed")

    # ========================================================================
    # Command Handlers
    # ========================================================================

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        await update.message.reply_text(
            "🎵 *Welcome to Music Link Converter Bot\\\\!*\\n\\n"
            "Send me a music link from Spotify, YouTube Music, or Apple Music, "
            "and I'll find it on all platforms\\\\.\\n\\n"
            "_Powered by advanced parsing and fuzzy matching\\\\._",
            parse_mode=ParseMode.MARKDOWN_V2
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        await update.message.reply_text(
            "📖 *How to use:*\\n\\n"
            "1\\\\. Send a music link\\n"
            "2\\\\. I'll search for it on all platforms\\n"
            "3\\\\. Get links with confidence scores\\\\!\\n\\n"
            "*Supported platforms:*\\n"
            "• Spotify\\n"
            "• YouTube Music\\n"
            "• Apple Music\\n\\n"
            "*Commands:*\\n"
            "/start \\\\- Start the bot\\n"
            "/help \\\\- Show this help\\n"
            "/stats \\\\- Show bot statistics",
            parse_mode=ParseMode.MARKDOWN_V2
        )

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stats command"""
        cache_stats = self.query_cache.stats()

        text = (
            f"📊 *Bot Statistics*\\n\\n"
            f"Messages processed: {self.stats['messages_processed']}\\n"
            f"Successful conversions: {self.stats['successful_conversions']}\\n"
            f"Failed conversions: {self.stats['failed_conversions']}\\n"
            f"Success rate: {self.stats['successful_conversions'] / max(self.stats['messages_processed'], 1) * 100:.1f}%\\n\\n"
            f"*Cache Stats:*\\n"
            f"Size: {cache_stats['size']}\\n"
            f"Hit rate: {cache_stats['hit_rate'] * 100:.1f}%"
        )

        await update.message.reply_text(
            self._escape_markdown(text),
            parse_mode=ParseMode.MARKDOWN_V2
        )

    # ========================================================================
    # Message Handler
    # ========================================================================

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming messages with music links"""
        self.stats['messages_processed'] += 1

        message = update.message
        text = message.text

        # Extract URL
        url = self._extract_url(text)
        if not url:
            await message.reply_text(
                "❌ Please send a valid music link from Spotify, YouTube Music, or Apple Music\\\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        # Canonicalize Spotify URLs (drop ?si= tracking etc.)
        if 'open.spotify.com/track/' in url:
            url = _canonicalize_spotify_url(url)

        # Show typing indicator
        await message.chat.send_action(ChatAction.TYPING)

        # Extract song info
        info = await self.extract_song_info(url)
        if not info:
            await message.reply_text(
                "❌ Couldn't extract song information from that link\\\\. Please try another\\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            self.stats['failed_conversions'] += 1
            return

        # Show what we found
        artist_display = info.artist if info.artist else "Unknown Artist"
        await message.reply_text(
            f"🔍 Found: *{self._escape_markdown(info.title)}* by *{self._escape_markdown(artist_display)}*\\n\\n"
            f"Searching other platforms…",
            parse_mode=ParseMode.MARKDOWN_V2
        )

        # Search on all platforms
        links = await self.find_on_all_platforms(info)

        # Try to enrich artist if missing
        if not info.artist:
            info.artist = await self._enrich_artist(links)

        # Check if we found anything
        if links.count() == 0:
            await message.reply_text(
                "😕 Couldn't find this song on other platforms\\\\. "
                "It might be a regional exclusive or rare release\\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            self.stats['failed_conversions'] += 1
            return

        # Send results
        await self.send_results(message, info, links)
        self.stats['successful_conversions'] += 1

    # ========================================================================
    # Core Functionality
    # ========================================================================

    async def extract_song_info(self, url: str) -> Optional[SongInfo]:
        """Extract song information from URL"""
        # Canonicalize for cache key
        key_url = _canonicalize_spotify_url(url)
        cache_key = f"info:{key_url}"
        cached = self.info_cache.get(cache_key)
        if cached:
            self.stats['cache_hits'] += 1
            try:
                return SongInfo(**json.loads(cached))
            except Exception:
                pass

        # Determine platform and parse
        url_lower = url.lower()
        info = None

        if 'spotify.com' in url_lower:
            info = await SpotifyParser.extract_info(url, self.http_client)
        elif 'music.youtube.com' in url_lower or 'youtube.com' in url_lower or 'youtu.be' in url_lower:
            info = await YouTubeParser.extract_info(url, self.http_client)
        elif 'music.apple.com' in url_lower:
            info = await AppleMusicParser.extract_info(url, self.http_client)

        # Cache result
        if info:
            self.info_cache.set(cache_key, json.dumps(asdict(info)), CACHE_TTL_INFO)

        return info

    async def find_on_all_platforms(self, info: SongInfo) -> MusicLinks:
        """Search for song on all platforms"""
        query = TextNormalizer.build_search_query(info.artist, info.title)
        links = MusicLinks()

        # Create search tasks
        tasks = []

        if info.platform != Platform.SPOTIFY.value:
            tasks.append(self._search_platform("spotify", query, "open.spotify.com/track"))
        else:
            links.spotify = _canonicalize_spotify_url(info.original_url) if info.original_url else None

        if info.platform not in [Platform.YOUTUBE.value, Platform.YOUTUBE_MUSIC.value]:
            tasks.append(self._search_platform("youtube", query, "music.youtube.com/watch"))
        else:
            links.youtube_music = info.original_url

        if info.platform != Platform.APPLE_MUSIC.value:
            tasks.append(self._search_platform("apple", query, "music.apple.com"))
        else:
            links.apple_music = info.original_url

        # Execute searches concurrently with timeout
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=18.0
            )

            # Process results
            for result in results:
                if isinstance(result, tuple) and len(result) == 2:
                    platform, url = result
                    if url:
                        # Verify the match
                        found_info = await self.extract_song_info(url)
                        if found_info and MatchValidator.is_valid_match(info, found_info):
                            if platform == "spotify":
                                links.spotify = _canonicalize_spotify_url(url)
                            elif platform == "youtube":
                                links.youtube_music = url
                            elif platform == "apple":
                                links.apple_music = url
                        else:
                            logger.warning(f"Match validation failed for {platform}")

        except asyncio.TimeoutError:
            logger.warning("Search timeout reached")

        return links

    async def _search_platform(self, platform: str, query: str, site: str) -> Tuple[str, Optional[str]]:
        """Search for song on specific platform"""
        cache_key = f"search:{platform}:{query}"

        # Check cache
        cached = self.query_cache.get(cache_key)
        if cached:
            if cached == "NEGATIVE":
                return (platform, None)
            return (platform, cached)

        # Search
        result = await SearchEngine.search(query, site, self.http_client)

        # Cache result
        if result:
            self.query_cache.set(cache_key, result, CACHE_TTL_SUCCESS)
        else:
            self.query_cache.set(cache_key, "NEGATIVE", CACHE_TTL_FAILURE, is_negative=True)

        return (platform, result)

    async def _enrich_artist(self, links: MusicLinks) -> str:
        """Try to get artist name from found links"""
        for url in [links.apple_music, links.youtube_music, links.spotify]:
            if url:
                info = await self.extract_song_info(url)
                if info and info.artist:
                    return info.artist
        return ""

    async def send_results(self, message, info: SongInfo, links: MusicLinks):
        """Send results to user"""
        artist = info.artist if info.artist else "Unknown Artist"

        text = (
            f"✅ *{self._escape_markdown(info.title)}* by *{self._escape_markdown(artist)}*\\n"
            f"🎧 Choose your platform:"
        )

        # Build keyboard
        buttons = []
        if links.spotify:
            buttons.append(InlineKeyboardButton("🎵 Spotify", url=links.spotify))
        if links.youtube_music:
            buttons.append(InlineKeyboardButton("▶️ YouTube Music", url=links.youtube_music))
        if links.apple_music:
            buttons.append(InlineKeyboardButton("🍎 Apple Music", url=links.apple_music))

        keyboard = []
        if buttons:
            keyboard.append(buttons)

        # Add source link
        if info.original_url:
            source_text = f"📎 Original ({info.platform})"
            keyboard.append([InlineKeyboardButton(source_text, url=info.original_url)])

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        await message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup
        )

    # ========================================================================
    # Utilities
    # ========================================================================

    @staticmethod
    def _extract_url(text: str) -> Optional[str]:
        """Extract first URL from text"""
        match = re.search(r'https?://[^\s]+', text)
        if match:
            url = match.group(0)
            # Clean trailing punctuation
            url = re.sub(r'[.,;!?)\}\]>"\']$', '', url)
            return url
        return None

    @staticmethod
    def _escape_markdown(text: str) -> str:
        """Escape text for MarkdownV2"""
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text


def main():
    """Main entry point"""
    # Get token from environment
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set")
        sys.exit(1)

    # Create and run bot
    bot = MusicBot(token)

    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
