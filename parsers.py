#!/usr/bin/env python3
"""Text normalization and platform-specific parsers for music links."""

import json
import logging
import re
import unicodedata
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup

from config import Platform, SongInfo
from http_client import HTTPClient

logger = logging.getLogger(__name__)


class TextNormalizer:
    """Handles all text normalization with multi-stage pipeline"""

    # Regex patterns (compiled once)
    RE_PARENS = re.compile(r'\s*[\(\[]([^\)\]]*?)[\)\]]')
    RE_FEAT = re.compile(r'(?i)\s*(feat\.?|featuring|ft\.?|with)\s+.+$')
    RE_PLATFORM_SUFFIX = re.compile(r'(?i)\s+(?:on|в|у|na|en|sur|su|auf|no|em|di|de|a)\s+(?:apple\s*music|spotify|youtube(?:\s*music)?)\b')
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
                                if isinstance(a0, dict):
                                    artist_name = (a0.get("name") or "").strip()

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

                clean_title = TextNormalizer.clean_for_display(title)
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
                og_desc = mirror_soup.find('meta', property='og:description')

                m_title = ''
                m_desc = ''
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
