#!/usr/bin/env python3
"""Configuration, constants, and data models for the Music Link Converter Bot."""

from dataclasses import dataclass
from datetime import timedelta, datetime
from enum import Enum
from typing import Optional


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
CACHE_TTL_FAILURE = timedelta(hours=12)  # Failed searches
CACHE_TTL_INFO = timedelta(days=3)  # Song info

# HTTP settings
REQUEST_TIMEOUT = 12.0
MAX_CONCURRENT_REQUESTS = 8
MAX_RETRIES = 2
BACKOFF_BASE = 0.3

# Rate limiting
MAX_REQUESTS_PER_PLATFORM = 3
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_TIMEOUT = 60


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
