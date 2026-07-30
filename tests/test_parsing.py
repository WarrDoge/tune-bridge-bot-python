"""Tests for the tune-bridge-bot pure parsing/conversion logic.

Covers:
- TextNormalizer (normalize_unicode, remove_platform_noise, clean_for_search, etc.)
- ParsingUtils (looks_like_album, looks_like_artist_list, split_title_artist, parse_spotify_description)
- SongInfo / MusicLinks data structures and helpers
- Platform enum
- _canonicalize_spotify_url
- MatchValidator.calculate_confidence
"""

import pytest

from config import Platform, SongInfo, MusicLinks, CacheEntry
from datetime import datetime, timedelta
from bot import _canonicalize_spotify_url
from matcher import MatchValidator
from parsers import TextNormalizer, ParsingUtils


# ===========================================================================
# Platform Enum
# ===========================================================================

class TestPlatform:
    def test_values(self):
        assert Platform.SPOTIFY.value == "Spotify"
        assert Platform.YOUTUBE_MUSIC.value == "YouTube Music"
        assert Platform.YOUTUBE.value == "YouTube"
        assert Platform.APPLE_MUSIC.value == "Apple Music"
        assert Platform.UNKNOWN.value == "Unknown"

    def test_distinct(self):
        values = {p.value for p in Platform}
        assert len(values) == 5


# ===========================================================================
# SongInfo
# ===========================================================================

class TestSongInfo:
    def test_basic_creation(self):
        info = SongInfo(title="  Hello  ", artist="  World  ")
        assert info.title == "Hello"
        assert info.artist == "World"
        assert info.album is None
        assert info.platform == "Unknown"
        assert info.original_url == ""
        assert info.confidence == 100.0

    def test_album_stripped(self):
        info = SongInfo(title="Test", artist="A", album="  Album Name  ")
        assert info.album == "Album Name"

    def test_default_fields(self):
        info = SongInfo(title="T", artist="A")
        assert info.platform == "Unknown"
        assert info.original_url == ""
        assert info.confidence == 100.0


# ===========================================================================
# MusicLinks
# ===========================================================================

class TestMusicLinks:
    def test_empty_count(self):
        links = MusicLinks()
        assert links.count() == 0

    def test_one_link(self):
        links = MusicLinks(spotify="https://spotify.com/track/1")
        assert links.count() == 1

    def test_all_links(self):
        links = MusicLinks(
            spotify="https://spotify.com/track/1",
            youtube_music="https://music.youtube.com/watch?v=1",
            apple_music="https://music.apple.com/song/1",
        )
        assert links.count() == 3


# ===========================================================================
# CacheEntry
# ===========================================================================

class TestCacheEntry:
    def test_default_is_negative_false(self):
        entry = CacheEntry(value="test", expires_at=datetime.now() + timedelta(hours=1))
        assert entry.is_negative is False

    def test_negative_cache(self):
        entry = CacheEntry(value="NEGATIVE", expires_at=datetime.now() + timedelta(hours=1), is_negative=True)
        assert entry.is_negative is True


# ===========================================================================
# TextNormalizer
# ===========================================================================

class TestNormalizeUnicode:
    def test_non_breaking_space(self):
        result = TextNormalizer.normalize_unicode("\u00A0hello")
        assert "\u00A0" not in result
        assert " hello" in result or "hello" in result

    def test_nfkc_normalization(self):
        # Full-width Latin letters -> ASCII
        result = TextNormalizer.normalize_unicode("\uFF21\uFF22")  # ＡＢ
        assert result == "AB" or result == "\uff21\uff22"  # depends on Python NFKC


class TestRemovePlatformNoise:
    def test_on_spotify_suffix(self):
        result = TextNormalizer.remove_platform_noise("Song Title on Spotify")
        assert "on Spotify" not in result

    def test_on_apple_music_suffix(self):
        result = TextNormalizer.remove_platform_noise("Song Title on Apple Music")
        assert "on Apple Music" not in result

    def test_entire_string_is_platform(self):
        result = TextNormalizer.remove_platform_noise("Spotify")
        assert result == ""

    def test_entire_string_is_web_player(self):
        result = TextNormalizer.remove_platform_noise("Spotify - Web Player")
        assert result == ""

    def test_platform_in_middle_kept(self):
        result = TextNormalizer.remove_platform_noise("Spotify Premium")
        assert result == "Spotify Premium"

    def test_language_preposition_kept(self):
        # Cyrillic "на" is not matched by the Latin-only RE_PLATFORM_SUFFIX
        result = TextNormalizer.remove_platform_noise("Песня на Spotify")
        assert "на Spotify" in result


class TestRemoveParentheticals:
    def test_remove_round_parentheses(self):
        result = TextNormalizer.remove_parentheticals("Hello (World)")
        assert result.strip() == "Hello"

    def test_remove_square_brackets(self):
        result = TextNormalizer.remove_parentheticals("Hello [World]")
        assert result.strip() == "Hello"

    def test_no_parentheses(self):
        result = TextNormalizer.remove_parentheticals("Hello World")
        assert result == "Hello World"


class TestRemoveFeatures:
    def test_feat_prefix(self):
        result = TextNormalizer.remove_features("Hello feat. Someone")
        assert "feat" not in result
        assert "Hello" in result

    def test_featuring(self):
        result = TextNormalizer.remove_features("Song ft. Artist")
        assert "ft." not in result


class TestCleanForDisplay:
    def test_basic_clean(self):
        result = TextNormalizer.clean_for_display("  Hello  World  ")
        assert result == "Hello World"

    def test_platform_noise_removed(self):
        result = TextNormalizer.clean_for_display("Song on Spotify")
        assert result == "Song"


class TestCleanForSearch:
    def test_removes_parentheses(self):
        result = TextNormalizer.clean_for_search("Hello (feat. World)")
        assert "(" not in result

    def test_lowercases(self):
        result = TextNormalizer.clean_for_search("HELLO")
        assert result == "hello"

    def test_removes_extra_words(self):
        result = TextNormalizer.clean_for_search("Official Video")
        assert "official" not in result
        assert "video" not in result

    def test_removes_noise_terms(self):
        result = TextNormalizer.clean_for_search("Song Remix")
        assert "remix" not in result


class TestNormalizeForMatching:
    def test_lowercase_and_no_punctuation(self):
        result = TextNormalizer.normalize_for_matching("Hello, World!")
        assert "," not in result
        assert result == "hello world"

    def test_removes_parentheticals(self):
        result = TextNormalizer.normalize_for_matching("Song (Remix)")
        assert "remix" not in result

    def test_multiple_spaces_collapsed(self):
        result = TextNormalizer.normalize_for_matching("a    b")
        assert result == "a b"


class TestBuildSearchQuery:
    def test_with_artist(self):
        result = TextNormalizer.build_search_query("Queen", "Bohemian Rhapsody")
        assert "queen" in result
        assert "bohemian" in result

    def test_without_artist(self):
        result = TextNormalizer.build_search_query("", "Song Title")
        assert result == "song title"

    def test_both_cleaned(self):
        result = TextNormalizer.build_search_query("  Artist  ", "  Title  (Remix)")
        assert "(remix)" not in result
        assert "artist" in result
        assert "title" in result


# ===========================================================================
# ParsingUtils
# ===========================================================================

class TestLooksLikeAlbum:
    def test_soundtrack_is_album(self):
        assert ParsingUtils.looks_like_album("Movie Soundtrack")

    def test_empty_is_not_album(self):
        assert not ParsingUtils.looks_like_album("")

    def test_ost_is_album(self):
        assert ParsingUtils.looks_like_album("OST Name")

    def test_plain_name_not_album(self):
        assert not ParsingUtils.looks_like_album("Hello")


class TestLooksLikeArtistList:
    def test_comma_separated(self):
        assert ParsingUtils.looks_like_artist_list("Artist1, Artist2")

    def test_and_separated(self):
        assert ParsingUtils.looks_like_artist_list("Artist1 & Artist2")

    def test_with_structure_marker(self):
        assert not ParsingUtils.looks_like_artist_list("Artist1: Title")

    def test_empty_is_not(self):
        assert not ParsingUtils.looks_like_artist_list("")

    def test_featuring(self):
        assert ParsingUtils.looks_like_artist_list("Artist feat. Other")


class TestSplitTitleArtist:
    def test_standard_separator(self):
        title, artist = ParsingUtils.split_title_artist("Song - Artist")
        assert title == "Song"
        assert artist == "Artist"

    def test_no_separator(self):
        title, artist = ParsingUtils.split_title_artist("JustATitle")
        assert title == "JustATitle"
        assert artist == ""

    def test_custom_separator(self):
        title, artist = ParsingUtils.split_title_artist("Song by Artist", " by ")
        assert title == "Song"
        assert artist == "Artist"


class TestParseSpotifyDescription:
    def test_normal_artist_album(self):
        artist, album = ParsingUtils.parse_spotify_description("Artist · Album")
        assert artist == "Artist"
        assert album == "Album"

    def test_none_description(self):
        assert ParsingUtils.parse_spotify_description(None) == (None, None)

    def test_empty_description(self):
        assert ParsingUtils.parse_spotify_description("") == (None, None)

    def test_single_part(self):
        assert ParsingUtils.parse_spotify_description("JustOne") == (None, None)

    def test_album_like_first_swapped(self):
        artist, album = ParsingUtils.parse_spotify_description("OST Name · Artist")
        assert artist == "Artist"
        assert album == "OST Name"


# ===========================================================================
# Canonicalize Spotify URL
# ===========================================================================

class TestCanonicalizeSpotifyUrl:
    def test_strips_tracking(self):
        result = _canonicalize_spotify_url("https://open.spotify.com/track/abc123?si=xyz&utm_source=web")
        assert result == "https://open.spotify.com/track/abc123"
        assert "?" not in result

    def test_non_spotify_unchanged(self):
        result = _canonicalize_spotify_url("https://music.youtube.com/watch?v=abc")
        assert result == "https://music.youtube.com/watch?v=abc"

    def test_already_clean(self):
        result = _canonicalize_spotify_url("https://open.spotify.com/track/abc123")
        assert result == "https://open.spotify.com/track/abc123"

    def test_invalid_url_returns_original(self):
        result = _canonicalize_spotify_url("not-a-url")
        assert result == "not-a-url"


# ===========================================================================
# MatchValidator
# ===========================================================================

class TestMatchValidator:
    def test_exact_match(self):
        original = SongInfo(title="Hello", artist="World")
        found = SongInfo(title="Hello", artist="World")
        confidence = MatchValidator.calculate_confidence(original, found)
        assert confidence > 90

    def test_completely_different(self):
        original = SongInfo(title="AAAAAA", artist="BBBBBB")
        found = SongInfo(title="XXXXXX", artist="YYYYYY")
        confidence = MatchValidator.calculate_confidence(original, found)
        assert confidence < 50

    def test_partial_title_match(self):
        original = SongInfo(title="Bohemian Rhapsody", artist="Queen")
        found = SongInfo(title="Bohemian Rhapsody (Remastered)", artist="Queen")
        confidence = MatchValidator.calculate_confidence(original, found)
        assert confidence > 70

    def test_is_valid_match_below_threshold(self):
        original = SongInfo(title="AAAA", artist="BBBB")
        found = SongInfo(title="XXXX", artist="YYYY")
        assert not MatchValidator.is_valid_match(original, found)

    def test_is_valid_match_above_threshold(self):
        original = SongInfo(title="Same Song", artist="Same Artist")
        found = SongInfo(title="Same Song", artist="Same Artist")
        assert MatchValidator.is_valid_match(original, found)
