#!/usr/bin/env python3
"""
Music Link Converter Bot
Converts music links between Spotify, YouTube Music, and Apple Music
With advanced parsing, caching, validation, and error recovery
"""

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, List, Tuple, Set
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit
import signal
import sys

# Third-party imports
try:
    import httpx
    from bs4 import BeautifulSoup
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
    from telegram.constants import ParseMode, ChatAction
    from fuzzywuzzy import fuzz
    import unicodedata
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install python-telegram-bot httpx beautifulsoup4 lxml fuzzywuzzy python-levenshtein")
    sys.exit(1)

# ============================================================================
# Configuration & Constants
# ============================================================================

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Platform identifiers
class Platform(Enum):
    SPOTIFY = "Spotify"
    YOUTUBE_MUSIC = "YouTube Music"
    YOUTUBE = "YouTube"
    APPLE_MUSIC = "Apple Music"
    UNKNOWN = "Unknown"

# Matching thresholds
MATCH_THRESHOLD_HIGH = 90  # 90%+ = excellent match
MATCH_THRESHOLD_MEDIUM = 80  # 80%+ = good match
MATCH_THRESHOLD_LOW = 70  # 70%+ = acceptable match

# Cache TTLs
CACHE_TTL_SUCCESS = timedelta(days=7)  # Successful matches
CACHE_TTL_FAILURE = timedelta(hours=12)  # Failed searches (slightly longer; fewer re-queries)
CACHE_TTL_INFO = timedelta(days=3)  # Song info (longer; stable once fetched)

# HTTP settings
REQUEST_TIMEOUT = 12.0
MAX_CONCURRENT_REQUESTS = 8
MAX_RETRIES = 2
BACKOFF_BASE = 0.3

# Rate limiting
MAX_REQUESTS_PER_PLATFORM = 3  # Per search attempt
CIRCUIT_BREAKER_THRESHOLD = 5  # Failures before circuit opens
CIRCUIT_BREAKER_TIMEOUT = 60  # Seconds to wait before retry

# ============================================================================
# Data Models
# ============================================================================

@dataclass
class SongInfo:
    """Song metadata"""
    title: str
    artist: str
    album: Optional[str] = None
    platform: str = "Unknown"
    original_url: str = ""
    confidence: float = 100.0  # Match confidence 0-100
    
    def __post_init__(self):
        """Normalize fields"""
        self.title = self.title.strip()
        self.artist = self.artist.strip()
        if self.album:
            self.album = self.album.strip()

@dataclass
class MusicLinks:
    """Links to song on different platforms"""
    spotify: Optional[str] = None
    youtube_music: Optional[str] = None
    apple_music: Optional[str] = None
    
    def count(self) -> int:
        """Count non-None links"""
        return sum(1 for link in [self.spotify, self.youtube_music, self.apple_music] if link)

@dataclass
class CacheEntry:
    """Cache entry with expiry"""
    value: str
    expires_at: datetime
    is_negative: bool = False  # True for failed searches

# ============================================================================
# Text Processing & Normalization
# ============================================================================

class TextNormalizer:
    """Handles all text normalization with multi-stage pipeline"""
    
    # Regex patterns (compiled once)
    RE_PARENS = re.compile(r'\s*[\(\[]([^\)\]]*?)[\)\]]')
    RE_FEAT = re.compile(r'(?i)\s*(feat\.?|featuring|ft\.?|with)\s+.+$')
    RE_PLATFORM_SUFFIX = re.compile(r'(?i)\s+(?:on|в|у|na|en|sur|su|auf|no|em|di|de|a)\s+(?:apple\s*music|spotify|youtube(?:\s*music)?)\b')
    # NOTE: We removed broad RE_PLATFORM_NAMES substitution in favor of a stricter whole-string check (see remove_platform_noise)
    RE_EXTRA_WORDS = re.compile(r'(?i)\b(official|video|audio|lyric|lyrics|music|hd|hq|vevo)\b')
    RE_DASH_VARIANTS = re.compile(r'\s*[-–—]\s*')
    RE_MIDDOT = re.compile(r'\s*[·•]\s*')
    RE_WHITESPACE = re.compile(r'\s+')
    
    # Unwanted terms for search queries
    NOISE_TERMS = {
        'remaster', 'remastered', 'remix', 'live', 'acoustic', 'radio edit',
        'single', 'ep', 'album version', 'deluxe', 'explicit', 'clean'
    }

    _GENERIC_PLATFORM_STRINGS = {
        "spotify – web player",
        "spotify - web player",
        "spotify web player",
        "spotify",
        "apple music",
        "youtube music",
        "youtube",
    }
    
    @classmethod
    def normalize_unicode(cls, text: str) -> str:
        """Normalize Unicode (NFD then NFC)"""
        # Replace non-breaking spaces
        text = text.replace('\u00A0', ' ')
        # Normalize Unicode
        text = unicodedata.normalize('NFKC', text)
        return text
    
    @classmethod
    def remove_platform_noise(cls, text: str) -> str:
        """
        Remove platform-specific suffixes and names, but only strip platform names
        when the ENTIRE field is just the platform (bonus hardening).
        """
        # 1) Remove "... on Spotify/Apple Music/YouTube" style suffixes anywhere
        text = cls.RE_PLATFORM_SUFFIX.sub('', text)

        # 2) If the entire string is just a platform-y generic title, clear it
        norm = cls.normalize_unicode(text).strip().lower()
        if norm in cls._GENERIC_PLATFORM_STRINGS:
            return ""

        return text
    
    @classmethod
    def remove_parentheticals(cls, text: str) -> str:
        """Remove content in parentheses/brackets"""
        return cls.RE_PARENS.sub('', text)
    
    @classmethod
    def remove_features(cls, text: str) -> str:
        """Remove featuring artists"""
        return cls.RE_FEAT.sub('', text)
    
    @classmethod
    def clean_for_display(cls, text: str) -> str:
        """Clean text for display to user"""
        text = cls.normalize_unicode(text)
        text = cls.remove_platform_noise(text)
        text = cls.RE_WHITESPACE.sub(' ', text)
        return text.strip()
    
    @classmethod
    def clean_for_search(cls, text: str) -> str:
        """Clean text for search queries"""
        text = cls.normalize_unicode(text)
        text = cls.remove_platform_noise(text)
        text = cls.remove_parentheticals(text)
        text = cls.remove_features(text)
        text = cls.RE_EXTRA_WORDS.sub('', text)
        
        # Remove noise terms
        text_lower = text.lower()
        for term in cls.NOISE_TERMS:
            text_lower = text_lower.replace(term, ' ')
        
        text = cls.RE_WHITESPACE.sub(' ', text_lower)
        return text.strip()
    
    @classmethod
    def normalize_for_matching(cls, text: str) -> str:
        """Normalize for fuzzy matching"""
        text = cls.normalize_unicode(text).lower()
        text = cls.remove_parentheticals(text)
        
        # Remove all punctuation
        text = re.sub(r'[^\w\s]', ' ', text)
        text = cls.RE_WHITESPACE.sub(' ', text)
        return text.strip()
    
    @classmethod
    def build_search_query(cls, artist: str, title: str) -> str:
        """Build optimized search query"""
        artist_clean = cls.clean_for_search(artist) if artist else ""
        title_clean = cls.clean_for_search(title)
        
        if artist_clean:
            return f"{artist_clean} {title_clean}".strip()
        return title_clean

# ============================================================================
# Parsing Utilities
# ============================================================================

class ParsingUtils:
    """Utilities for parsing song metadata"""
    
    @staticmethod
    def looks_like_album(text: str) -> bool:
        """Heuristic: detect if text looks like album name"""
        text_lower = text.lower().strip()
        if not text_lower:
            return False
        
        album_keywords = [
            'soundtrack', 'ost', 'original score', 'music from',
            'season ', 'volume', ' vol.', ' vol ', ':'
        ]
        return any(kw in text_lower for kw in album_keywords)
    
    @staticmethod
    def looks_like_artist_list(text: str) -> bool:
        """Heuristic: detect if text looks like artist list"""
        text_lower = text.lower().strip()
        if not text_lower:
            return False
        
        # Has list markers but not structure markers
        has_list_markers = any(marker in text_lower for marker in [',', ' & ', ' and ', ' feat'])
        has_structure = any(marker in text_lower for marker in [':', ' - '])
        
        return has_list_markers and not has_structure
    
    @staticmethod
    def split_title_artist(text: str, separator: str = ' - ') -> Tuple[str, str]:
        """Split text into title and artist"""
        parts = text.split(separator, 1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        return text.strip(), ""
    
    @staticmethod
    def parse_spotify_description(desc: str) -> Tuple[Optional[str], Optional[str]]:
        """Parse Spotify description 'Artist · Album' or 'Song · Artist'"""
        if not desc:
            return None, None
        
        parts = TextNormalizer.RE_MIDDOT.split(desc)
        if len(parts) < 2:
            return None, None
        
        p0, p1 = parts[0].strip(), parts[1].strip()
        
        # Heuristic: album-like goes second
        if ParsingUtils.looks_like_album(p0) and not ParsingUtils.looks_like_album(p1):
            return p1, p0  # (artist, album)
        elif ParsingUtils.looks_like_album(p1) and not ParsingUtils.looks_like_album(p0):
            return p0, p1  # (artist, album)
        
        # Default: first is artist, second is album
        return p0, p1

# ============================================================================
# Caching Layer
# ============================================================================

class SmartCache:
    """Cache with TTL support and negative caching"""
    
    def __init__(self):
        self._cache: Dict[str, CacheEntry] = {}
        self._hits = 0
        self._misses = 0
    
    def get(self, key: str) -> Optional[str]:
        """Get value from cache"""
        if key not in self._cache:
            self._misses += 1
            return None
        
        entry = self._cache[key]
        if datetime.now() > entry.expires_at:
            del self._cache[key]
            self._misses += 1
            return None
        
        self._hits += 1
        return entry.value
    
    def set(self, key: str, value: str, ttl: timedelta, is_negative: bool = False):
        """Set value in cache"""
        self._cache[key] = CacheEntry(
            value=value,
            expires_at=datetime.now() + ttl,
            is_negative=is_negative
        )
    
    def cleanup(self):
        """Remove expired entries"""
        now = datetime.now()
        expired = [k for k, v in self._cache.items() if now > v.expires_at]
        for key in expired:
            del self._cache[key]
    
    def stats(self) -> Dict[str, int]:
        """Get cache statistics"""
        return {
            'size': len(self._cache),
            'hits': self._hits,
            'misses': self._misses,
            'hit_rate': self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0
        }

# ============================================================================
# Circuit Breaker
# ============================================================================

class CircuitBreaker:
    """Circuit breaker pattern for failing services"""
    
    def __init__(self, threshold: int = 5, timeout: int = 60):
        self.threshold = threshold
        self.timeout = timeout
        self.failures: Dict[str, List[float]] = defaultdict(list)
        self.opened_at: Dict[str, float] = {}
    
    def record_failure(self, service: str):
        """Record a failure"""
        now = time.time()
        self.failures[service].append(now)
        
        # Keep only recent failures (last 5 minutes)
        self.failures[service] = [t for t in self.failures[service] if now - t < 300]
        
        # Open circuit if threshold exceeded
        if len(self.failures[service]) >= self.threshold:
            self.opened_at[service] = now
            logger.warning(f"Circuit breaker opened for {service}")
    
    def record_success(self, service: str):
        """Record a success (reset failures)"""
        if service in self.failures:
            self.failures[service].clear()
        if service in self.opened_at:
            del self.opened_at[service]
            logger.info(f"Circuit breaker closed for {service}")
    
    def is_open(self, service: str) -> bool:
        """Check if circuit is open"""
        if service not in self.opened_at:
            return False
        
        # Check if timeout has passed
        if time.time() - self.opened_at[service] > self.timeout:
            del self.opened_at[service]
            logger.info(f"Circuit breaker reset for {service}")
            return False
        
        return True

# ============================================================================
# HTTP Client with Retry Logic
# ============================================================================

class HTTPClient:
    """HTTP client with retry, backoff, and circuit breaker"""
    
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            limits=httpx.Limits(max_connections=MAX_CONCURRENT_REQUESTS),
            follow_redirects=True
        )
        self.circuit_breaker = CircuitBreaker(
            threshold=CIRCUIT_BREAKER_THRESHOLD,
            timeout=CIRCUIT_BREAKER_TIMEOUT
        )
    
    async def get(self, url: str, service: str = "default") -> Optional[httpx.Response]:
        """GET request with retry and circuit breaker"""
        # Check circuit breaker
        if self.circuit_breaker.is_open(service):
            logger.warning(f"Circuit breaker open for {service}, skipping request")
            return None
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Referer': 'https://open.spotify.com/',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Dest': 'document',
        }
        
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                if attempt > 0:
                    # Exponential backoff with jitter
                    delay = BACKOFF_BASE * (2 ** attempt) + (0.1 * attempt)
                    await asyncio.sleep(delay)
                
                response = await self.client.get(url, headers=headers)
                
                if response.status_code == 200:
                    self.circuit_breaker.record_success(service)
                    return response
                elif response.status_code == 429:
                    # Rate limited
                    logger.warning(f"Rate limited on {service}: {url}")
                    await asyncio.sleep(2 ** attempt)
                    continue
                elif response.status_code >= 500:
                    # Server error, retry
                    logger.warning(f"Server error {response.status_code} on {service}")
                    continue
                else:
                    # Client error, don't retry
                    logger.warning(f"Client error {response.status_code}: {url}")
                    self.circuit_breaker.record_failure(service)
                    return None
                    
            except httpx.TimeoutException:
                logger.warning(f"Timeout on attempt {attempt + 1} for {url}")
                last_error = "timeout"
            except httpx.NetworkError as e:
                logger.warning(f"Network error on attempt {attempt + 1}: {e}")
                last_error = str(e)
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                last_error = str(e)
                break
        
        # All retries failed
        self.circuit_breaker.record_failure(service)
        logger.error(f"All retries failed for {url}: {last_error}")
        return None
    
    async def get_via_text_mirror(self, url: str, service: str = "text_mirror") -> Optional[httpx.Response]:
        """Fetch a prerendered text/HTML version via r.jina.ai mirror."""
        try:
            mirror = f"https://r.jina.ai/http://{url.replace('https://', '').replace('http://','')}"
            return await self.get(mirror, service=service)
        except Exception:
            return None

    async def close(self):
        """Close the client"""
        await self.client.aclose()

# ============================================================================
# Platform Parsers
# ============================================================================

class SpotifyParser:
    """Parse Spotify links and extract song info"""
    
    RE_TRACK = re.compile(r'(?:open\.spotify\.com|spotify:)(?:/intl-[a-z]{2})?(?:/|:)track[/:]([A-Za-z0-9]+)')
    RE_ALBUM = re.compile(r'(?:open\.spotify\.com)(?:/intl-[a-z]{2})?/album/([A-Za-z0-9]+)')
    
    @classmethod
    async def extract_info(cls, url: str, http_client: HTTPClient) -> Optional[SongInfo]:
        """Extract song info from Spotify URL"""
        # Try oEmbed API first
        track_match = cls.RE_TRACK.search(url)
        if not track_match:
            return None
        
        track_id = track_match.group(1)
        track_url = f"https://open.spotify.com/track/{track_id}"
        
        # Try oEmbed (primary)
        info = await cls._try_oembed(track_url, http_client)
        if info and info.title and info.artist:
            info.original_url = track_url
            return info
        
        # Fallback to HTML scraping
        return await cls._scrape_page(track_url, http_client)
    
    @classmethod
    async def _try_oembed(cls, url: str, http_client: HTTPClient) -> Optional[SongInfo]:
        """Try Spotify oEmbed API with a legacy alias fallback"""
        oembed_url = f"https://open.spotify.com/oembed?url={quote(url)}"
        
        def _parse_oembed_payload(data: Dict) -> Optional[SongInfo]:
            title = data.get('title', '').replace(' | Spotify', '').strip()
            artist = data.get('author_name', '').strip()
            # Try to parse description for better data
            desc = data.get('description', '')
            if desc and (not artist or not title):
                desc_artist, _ = ParsingUtils.parse_spotify_description(desc)
                if not artist and desc_artist:
                    artist = desc_artist
            # Parse title if it contains artist
            lower = title.lower()
            if ' by ' in lower:
                t, a = ParsingUtils.split_title_artist(title, ' by ')
                title, artist = t, (artist or a)
            elif ' - ' in title and not artist:
                parts = TextNormalizer.RE_DASH_VARIANTS.split(title, 1)
                if len(parts) == 2:
                    title, artist = parts[0].strip(), parts[1].strip()
            if title:
                return SongInfo(
                    title=TextNormalizer.clean_for_display(title),
                    artist=TextNormalizer.clean_for_display(artist) if artist else "",
                    platform=Platform.SPOTIFY.value,
                    original_url=url
                )
            return None

        # Primary endpoint
        try:
            response = await http_client.get(oembed_url, service="spotify_oembed")
            if response:
                data = response.json()
                parsed = _parse_oembed_payload(data)
                if parsed:
                    return parsed
        except Exception as e:
            logger.debug(f"oEmbed primary failed: {e}")

        # Legacy alias fallback
        legacy = f"https://embed.spotify.com/oembed?url={quote(url)}"
        try:
            response = await http_client.get(legacy, service="spotify_oembed_legacy")
            if response:
                data = response.json()
                parsed = _parse_oembed_payload(data)
                if parsed:
                    return parsed
        except Exception as e:
            logger.debug(f"oEmbed legacy failed: {e}")
        
        return None
    
    @classmethod
    async def _scrape_page(cls, url: str, http_client: HTTPClient) -> Optional[SongInfo]:
        """Scrape Spotify page for metadata with multiple fallbacks"""
        logger.info(f"🔍 Scraping Spotify page: {url}")
        response = await http_client.get(url, service="spotify_scrape")
        if not response:
            logger.warning(f"❌ No response from Spotify page")
            # Try text mirror immediately if primary fetch failed
            response = await http_client.get_via_text_mirror(url, service="spotify_mirror_initial")
            if not response:
                return None
        
        try:
            soup = BeautifulSoup(response.text, 'lxml')

            # ---------- 0) NEXT_DATA (most reliable; bonus #3) ----------
            try:
                nd = soup.find("script", id="__NEXT_DATA__")
                if nd and nd.string:
                    ndj = json.loads(nd.string)
                    # Typical shapes: props.pageProps.track or props.__APOLLO_STATE__ graph
                    props = ndj.get("props", {})
                    page_props = props.get("pageProps", {}) if isinstance(props, dict) else {}
                    track = {}
                    # Direct track structure
                    if isinstance(page_props, dict):
                        track = page_props.get("track", {}) or page_props.get("pageProps", {}).get("track", {}) if isinstance(page_props.get("pageProps", {}), dict) else {}
                    # Fallback: Apollo cache graph (keys like "Track:xxxxxxxx")
                    if not track and isinstance(props.get("__APOLLO_STATE__"), dict):
                        apollo = props["__APOLLO_STATE__"]
                        for k, v in apollo.items():
                            if isinstance(v, dict) and v.get("__typename") in ("Track", "MusicTrack") and v.get("name"):
                                track = v
                                break

                    if isinstance(track, dict) and track:
                        name = (track.get("name") or "").strip()
                        artist_name = ""
                        artists_obj = track.get("artists") or track.get("artists", [])
                        # artists may be list of dicts or dict with items
                        if isinstance(artists_obj, list) and artists_obj:
                            a0 = artists_obj[0]
                            artist_name = (a0.get("name") if isinstance(a0, dict) else str(a0)).strip()
                        elif isinstance(artists_obj, dict):
                            # Some shapes like {"items": [{name: ...}]}
                            items = artists_obj.get("items")
                            if isinstance(items, list) and items:
                                a0 = items[0]
                                artist_name = (a0.get("name") if isinstance(a0, dict) else str(a0)).strip()

                        if name:
                            result = SongInfo(
                                title=TextNormalizer.clean_for_display(name),
                                artist=TextNormalizer.clean_for_display(artist_name),
                                platform=Platform.SPOTIFY.value,
                                original_url=url
                            )
                            logger.info(f"✅ Spotify NEXT_DATA SUCCESS: title='{result.title}', artist='{result.artist}'")
                            return result
            except Exception as e:
                logger.debug(f"NEXT_DATA parse failed: {e}")

            # ---------- 1) JSON-LD ----------
            ld = soup.find('script', type='application/ld+json')
            if ld and ld.string:
                try:
                    ldj = json.loads(ld.string)
                    items = ldj if isinstance(ldj, list) else [ldj]
                    for item in items:
                        if isinstance(item, dict) and item.get('@type') in ('MusicRecording', 'MusicAlbum'):
                            name = item.get('name') or ''
                            by_artist = item.get('byArtist')
                            artist_name = ''
                            if isinstance(by_artist, dict):
                                artist_name = by_artist.get('name', '') or ''
                            elif isinstance(by_artist, list) and by_artist:
                                # Take the first artist if list
                                if isinstance(by_artist[0], dict):
                                    artist_name = by_artist[0].get('name', '') or ''
                            if name:
                                result = SongInfo(
                                    title=TextNormalizer.clean_for_display(name),
                                    artist=TextNormalizer.clean_for_display(artist_name) if artist_name else "",
                                    platform=Platform.SPOTIFY.value,
                                    original_url=url
                                )
                                logger.info(f"✅ Spotify JSON-LD SUCCESS: title='{result.title}', artist='{result.artist}'")
                                return result
                except Exception as e:
                    logger.debug(f"JSON-LD parse failed: {e}")

            # ---------- 2) OPENGRAPH (if available) ----------
            og_title = soup.find('meta', property='og:title')
            og_desc = soup.find('meta', property='og:description')

            logger.info(f"📋 Found og:title={og_title is not None}, og:desc={og_desc is not None}")

            title_text = ""
            desc_text = ""

            if og_title and og_title.get('content'):
                title_text = og_title.get('content', '').replace(' | Spotify', '').strip()
            # Try <title> as well
            if not title_text:
                t = soup.find('title')
                if t and t.text:
                    title_text = t.text.replace(' | Spotify', '').strip()

            if og_desc and og_desc.get('content'):
                desc_text = og_desc.get('content', '')

            # Parse title/artist from text collected so far
            def _parse_text_fields(_title_text: str, _desc_text: str) -> Optional[SongInfo]:
                if not _title_text:
                    return None
                if ' by ' in _title_text.lower():
                    title, artist = ParsingUtils.split_title_artist(_title_text, ' by ')
                elif TextNormalizer.RE_DASH_VARIANTS.search(_title_text):
                    parts = TextNormalizer.RE_DASH_VARIANTS.split(_title_text, 1)
                    if len(parts) == 2:
                        left, right = parts[0].strip(), parts[1].strip()
                        # Use description as hint
                        desc_artist, _ = ParsingUtils.parse_spotify_description(_desc_text)
                        if desc_artist:
                            left_norm = TextNormalizer.normalize_for_matching(left)
                            right_norm = TextNormalizer.normalize_for_matching(right)
                            artist_norm = TextNormalizer.normalize_for_matching(desc_artist)
                            if left_norm == artist_norm:
                                title, artist = right, left
                            elif right_norm == artist_norm:
                                title, artist = left, right
                            else:
                                title, artist = left, right
                        else:
                            title, artist = left, right
                    else:
                        title, artist = _title_text, ""
                else:
                    title, artist = _title_text, ""

                if not artist and _desc_text:
                    desc_artist, _ = ParsingUtils.parse_spotify_description(_desc_text)
                    if desc_artist:
                        artist = desc_artist

                clean_title  = TextNormalizer.clean_for_display(title)
                clean_artist = TextNormalizer.clean_for_display(artist) if artist else ""

                if not clean_title:
                    return None

                return SongInfo(
                    title=clean_title,
                    artist=clean_artist,
                    platform=Platform.SPOTIFY.value,
                    original_url=url
                )

            parsed_from_html = _parse_text_fields(title_text, desc_text)
            if parsed_from_html:
                logger.info(f"✅ Spotify scraping SUCCESS: title='{parsed_from_html.title}', artist='{parsed_from_html.artist}'")
                return parsed_from_html

            # ---------- 3) TEXT MIRROR FINAL FALLBACK ----------
            logger.info("🔁 Falling back to prerendered text mirror for Spotify")
            mirror_resp = await http_client.get_via_text_mirror(url, service="spotify_mirror")
            if mirror_resp:
                mirror_soup = BeautifulSoup(mirror_resp.text, 'lxml')
                og_title = mirror_soup.find('meta', property='og:title')
                og_desc  = mirror_soup.find('meta', property='og:description')

                m_title = ''
                m_desc  = ''
                if og_title and og_title.get('content'):
                    m_title = og_title['content'].replace(' | Spotify', '').strip()
                else:
                    mt = mirror_soup.find('title')
                    if mt and mt.text:
                        m_title = mt.text.replace(' | Spotify', '').strip()

                if og_desc and og_desc.get('content'):
                    m_desc = og_desc['content']

                parsed_from_mirror = _parse_text_fields(m_title, m_desc)
                if parsed_from_mirror:
                    logger.info(f"✅ Spotify mirror SUCCESS: title='{parsed_from_mirror.title}', artist='{parsed_from_mirror.artist}'")
                    return parsed_from_mirror

            logger.warning(f"❌ Spotify scraping FAIL: no title found anywhere")
        except Exception as e:
            logger.error(f"Spotify scraping error: {e}")
        
        return None

class YouTubeParser:
    """Parse YouTube/YouTube Music links"""
    
    RE_VIDEO_ID = re.compile(r'(?:v=|youtu\.be/|music\.youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})')
    
    @classmethod
    async def extract_info(cls, url: str, http_client: HTTPClient) -> Optional[SongInfo]:
        """Extract song info from YouTube URL"""
        response = await http_client.get(url, service="youtube")
        if not response:
            return None
        
        try:
            soup = BeautifulSoup(response.text, 'lxml')
            
            # Get title
            title_tag = soup.find('meta', attrs={'name': 'title'})
            if not title_tag:
                title_tag = soup.find('meta', property='og:title')
            if not title_tag:
                # Fallback to <title>
                t = soup.find('title')
                raw_title = t.text.strip() if t else ''
            else:
                raw_title = title_tag.get('content', '').strip()
            
            if not raw_title:
                return None
            
            # Get channel/author
            author_tag = soup.find('link', itemprop='name')
            if not author_tag:
                author_tag = soup.find('meta', attrs={'name': 'author'})
            
            channel_name = ""
            if author_tag:
                channel_name = author_tag.get('content', '').strip()
            
            # Remove "- Topic" suffix
            channel_name = re.sub(r'\s*-?\s*Topic$', '', channel_name, flags=re.IGNORECASE)
            
            # Parse title
            title, artist = cls._parse_youtube_title(raw_title, channel_name)
            
            platform = Platform.YOUTUBE_MUSIC if 'music.youtube.com' in url else Platform.YOUTUBE
            
            return SongInfo(
                title=TextNormalizer.clean_for_display(title),
                artist=TextNormalizer.clean_for_display(artist) if artist else "",
                platform=platform.value,
                original_url=url
            )
        except Exception as e:
            logger.error(f"YouTube parsing error: {e}")
        
        return None
    
    @staticmethod
    def _parse_youtube_title(raw_title: str, channel_name: str) -> Tuple[str, str]:
        """Parse YouTube title to extract song and artist"""
        # Common pattern: "Artist - Title" or "Title - Artist"
        if ' - ' in raw_title:
            parts = raw_title.split(' - ', 1)
            left, right = parts[0].strip(), parts[1].strip()
            
            # Remove common video suffixes
            right = re.sub(r'(?i)\s*\((Official|Lyric|Music)?\s*(Video|Audio)\)', '', right)
            
            # If channel name matches one side, that's the artist
            left_norm = TextNormalizer.normalize_for_matching(left)
            right_norm = TextNormalizer.normalize_for_matching(right)
            channel_norm = TextNormalizer.normalize_for_matching(channel_name)
            
            if channel_norm and left_norm == channel_norm:
                return right, left
            elif channel_norm and right_norm == channel_norm:
                return left, right
            
            # Heuristic: artist lists usually come first
            if ParsingUtils.looks_like_artist_list(left):
                return right, left
            
            # Default: Artist - Title
            return right, left
        
        # No separator, use channel as artist
        return raw_title, channel_name

class AppleMusicParser:
    """Parse Apple Music links"""
    
    RE_SONG = re.compile(r'music\.apple\.com/[a-z]{2}/(?:album|song)/[^/]+/(\d+)')
    
    @classmethod
    async def extract_info(cls, url: str, http_client: HTTPClient) -> Optional[SongInfo]:
        """Extract song info from Apple Music URL"""
        response = await http_client.get(url, service="apple_music")
        if not response:
            return None
        
        try:
            soup = BeautifulSoup(response.text, 'lxml')
            
            # Get og:title
            og_title = soup.find('meta', property='og:title')
            title_text = ""
            if og_title:
                title_text = og_title.get('content', '').strip()
            if not title_text:
                t = soup.find('title')
                if t and t.text:
                    title_text = t.text.strip()
            
            if not title_text:
                return None
            
            # Apple Music format: "Song Name - Artist Name"
            if ' - ' in title_text:
                title, artist = ParsingUtils.split_title_artist(title_text)
            else:
                title, artist = title_text, ""
            
            return SongInfo(
                title=TextNormalizer.clean_for_display(title),
                artist=TextNormalizer.clean_for_display(artist) if artist else "",
                platform=Platform.APPLE_MUSIC.value,
                original_url=url
            )
        except Exception as e:
            logger.error(f"Apple Music parsing error: {e}")
        
        return None

# ============================================================================
# Search Engine
# ============================================================================

class SearchEngine:
    """Search for songs across platforms using DuckDuckGo"""
    
    @staticmethod
    async def search(query: str, site: str, http_client: HTTPClient) -> Optional[str]:
        """Search using DuckDuckGo HTML"""
        search_query = f"site:{site} {query}"
        search_url = f"https://duckduckgo.com/html/?q={quote(search_query)}"
        
        response = await http_client.get(search_url, service=f"search_{site}")
        if not response:
            return None
        
        try:
            soup = BeautifulSoup(response.text, 'lxml')
            
            # Find first result
            result_link = soup.find('a', class_='result__a')
            if not result_link:
                return None
            
            href = result_link.get('href', '')
            
            # Extract actual URL from DuckDuckGo redirect
            match = re.search(r'uddg=([^&]+)', href)
            if match:
                return unquote(match.group(1))
            
            return href if site in href else None
        except Exception as e:
            logger.error(f"Search error for {site}: {e}")
        
        return None

# ============================================================================
# Match Validator
# ============================================================================

class MatchValidator:
    """Validate search results match original song"""
    
    @staticmethod
    def calculate_confidence(original: SongInfo, found: SongInfo) -> float:
        """Calculate match confidence score (0-100)"""
        # Normalize for comparison
        orig_title = TextNormalizer.normalize_for_matching(original.title)
        orig_artist = TextNormalizer.normalize_for_matching(original.artist)
        found_title = TextNormalizer.normalize_for_matching(found.title)
        found_artist = TextNormalizer.normalize_for_matching(found.artist)
        
        # Calculate fuzzy match scores
        title_score = fuzz.ratio(orig_title, found_title)
        artist_score = fuzz.ratio(orig_artist, found_artist) if orig_artist and found_artist else 0
        
        # Weighted average (title more important)
        if orig_artist and found_artist:
            confidence = (title_score * 0.6) + (artist_score * 0.4)
        else:
            confidence = title_score
        
        return confidence
    
    @staticmethod
    def is_valid_match(original: SongInfo, found: SongInfo, threshold: float = MATCH_THRESHOLD_LOW) -> bool:
        """Check if found song is valid match"""
        confidence = MatchValidator.calculate_confidence(original, found)
        return confidence >= threshold

# ============================================================================
# Music Bot Core
# ============================================================================

def _canonicalize_spotify_url(u: str) -> str:
    """Drop tracking queries from Spotify track URLs so cache hits are maximized."""
    try:
        s = urlsplit(u)
        if s.netloc.endswith('open.spotify.com') and s.path.startswith('/track/'):
            return urlunsplit((s.scheme, s.netloc, s.path, '', ''))
        return u
    except Exception:
        return u

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
            "🎵 *Welcome to Music Link Converter Bot\\!*\n\n"
            "Send me a music link from Spotify, YouTube Music, or Apple Music, "
            "and I'll find it on all platforms\\.\n\n"
            "_Powered by advanced parsing and fuzzy matching\\._",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        await update.message.reply_text(
            "📖 *How to use:*\n\n"
            "1\\. Send a music link\n"
            "2\\. I'll search for it on all platforms\n"
            "3\\. Get links with confidence scores\\!\n\n"
            "*Supported platforms:*\n"
            "• Spotify\n"
            "• YouTube Music\n"
            "• Apple Music\n\n"
            "*Commands:*\n"
            "/start \\- Start the bot\n"
            "/help \\- Show this help\n"
            "/stats \\- Show bot statistics",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stats command"""
        cache_stats = self.query_cache.stats()
        
        text = (
            f"📊 *Bot Statistics*\n\n"
            f"Messages processed: {self.stats['messages_processed']}\n"
            f"Successful conversions: {self.stats['successful_conversions']}\n"
            f"Failed conversions: {self.stats['failed_conversions']}\n"
            f"Success rate: {self.stats['successful_conversions'] / max(self.stats['messages_processed'], 1) * 100:.1f}%\n\n"
            f"*Cache Stats:*\n"
            f"Size: {cache_stats['size']}\n"
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
                "❌ Please send a valid music link from Spotify, YouTube Music, or Apple Music\\.",
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
                "❌ Couldn't extract song information from that link\\. Please try another\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            self.stats['failed_conversions'] += 1
            return
        
        # Show what we found
        artist_display = info.artist if info.artist else "Unknown Artist"
        await message.reply_text(
            f"🔍 Found: *{self._escape_markdown(info.title)}* by *{self._escape_markdown(artist_display)}*\n\n"
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
                "😕 Couldn't find this song on other platforms\\. "
                "It might be a regional exclusive or rare release\\.",
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
            f"✅ *{self._escape_markdown(info.title)}* by *{self._escape_markdown(artist)}*\n"
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
            url = re.sub(r'[.,;!?)\]}>"\']$', '', url)
            return url
        return None
    
    @staticmethod
    def _escape_markdown(text: str) -> str:
        """Escape text for MarkdownV2"""
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text

# ============================================================================
# Main Entry Point
# ============================================================================

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
