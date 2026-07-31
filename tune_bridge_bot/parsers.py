"""Platform-specific link parsers for Spotify, YouTube, and Apple Music."""

import json
import logging
import re
from typing import Optional, Tuple, Dict
from urllib.parse import quote, urlsplit, urlunsplit

from tune_bridge_bot.text import TextNormalizer, ParsingUtils
from tune_bridge_bot.models import Platform, SongInfo

# Third-party
try:
    from bs4 import BeautifulSoup
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install python-telegram-bot httpx beautifulsoup4 lxml fuzzywuzzy python-levenshtein")
    import sys
    sys.exit(1)

logger = logging.getLogger(__name__)


def _canonicalize_spotify_url(u: str) -> str:
    """Drop tracking queries from Spotify track URLs so cache hits are maximized."""
    try:
        s = urlsplit(u)
        if s.netloc.endswith('open.spotify.com') and s.path.startswith('/track/'):
            return urlunsplit((s.scheme, s.netloc, s.path, '', ''))
        return u
    except Exception:
        return u


class SpotifyParser:
    """Parse Spotify links and extract song info"""

    RE_TRACK = re.compile(r'(?:open\.spotify\.com|spotify:)(?:/intl-[a-z]{2})?(?:/|:)track[/:]([A-Za-z0-9]+)')
    RE_ALBUM = re.compile(r'(?:open\.spotify\.com)(?:/intl-[a-z]{2})?/album/([A-Za-z0-9]+)')

    @classmethod
    async def extract_info(cls, url: str, http_client) -> Optional[SongInfo]:
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
    async def _try_oembed(cls, url: str, http_client) -> Optional[SongInfo]:
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
    async def _scrape_page(cls, url: str, http_client) -> Optional[SongInfo]:
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

            # ---------- 0) NEXT_DATA (most reliable) ----------
            try:
                nd = soup.find("script", id="__NEXT_DATA__")
                if nd and nd.string:
                    ndj = json.loads(nd.string)
                    props = ndj.get("props", {})
                    page_props = props.get("pageProps", {}) if isinstance(props, dict) else {}
                    track = {}
                    if isinstance(page_props, dict):
                        track = page_props.get("track", {}) or page_props.get("pageProps", {}).get("track", {}) if isinstance(page_props.get("pageProps", {}), dict) else {}
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
                        if isinstance(artists_obj, list) and artists_obj:
                            a0 = artists_obj[0]
                            artist_name = (a0.get("name") if isinstance(a0, dict) else str(a0)).strip()
                        elif isinstance(artists_obj, dict):
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

            # ---------- 2) OPENGRAPH ----------
            og_title = soup.find('meta', property='og:title')
            og_desc = soup.find('meta', property='og:description')

            logger.info(f"📋 Found og:title={og_title is not None}, og:desc={og_desc is not None}")

            title_text = ""
            desc_text = ""

            if og_title and og_title.get('content'):
                title_text = og_title.get('content', '').replace(' | Spotify', '').strip()
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
    async def extract_info(cls, url: str, http_client) -> Optional[SongInfo]:
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
    async def extract_info(cls, url: str, http_client) -> Optional[SongInfo]:
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
