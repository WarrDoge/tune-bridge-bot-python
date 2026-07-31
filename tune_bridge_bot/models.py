"""Data models for the music link converter bot."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict


class Platform(Enum):
    SPOTIFY = "Spotify"
    YOUTUBE_MUSIC = "YouTube Music"
    YOUTUBE = "YouTube"
    APPLE_MUSIC = "Apple Music"
    UNKNOWN = "Unknown"


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
