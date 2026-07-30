"""Shared fixtures and test helpers for the tune-bridge-bot test suite."""

import pytest

from config import SongInfo, MusicLinks
from http_client import HTTPClient


@pytest.fixture
def sample_song_info() -> SongInfo:
    """Return a standard song info for testing."""
    return SongInfo(
        title="Bohemian Rhapsody",
        artist="Queen",
        album="A Night at the Opera",
        platform="Spotify",
        original_url="https://open.spotify.com/track/abc123",
        confidence=100.0,
    )


@pytest.fixture
def sample_music_links() -> MusicLinks:
    """Return sample music links with all platforms populated."""
    return MusicLinks(
        spotify="https://open.spotify.com/track/abc123",
        youtube_music="https://music.youtube.com/watch?v=xyz789",
        apple_music="https://music.apple.com/us/album/song/12345",
    )
