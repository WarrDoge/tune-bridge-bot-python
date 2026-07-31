"""Text normalisation and parsing utilities."""

import re
import unicodedata
from typing import Optional, Tuple


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
