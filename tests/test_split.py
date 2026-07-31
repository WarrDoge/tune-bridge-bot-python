"""Tests for the tune_bridge_bot package — pure logic, no network."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tune_bridge_bot.text import TextNormalizer, ParsingUtils
from tune_bridge_bot.models import Platform, SongInfo, MusicLinks
from tune_bridge_bot.parsers import _canonicalize_spotify_url
from tune_bridge_bot.matching import MatchValidator


# ── TextNormalizer tests ───────────────────────────────────────────────

def test_normalize_unicode_strips_nbsp():
    result = TextNormalizer.normalize_unicode("Song\u00A0Title")
    assert result == "Song Title", repr(result)


def test_remove_platform_noise_strips_suffix():
    result = TextNormalizer.remove_platform_noise("Blinding Lights on Spotify")
    assert "on Spotify" not in result


def test_remove_platform_noise_clears_generic():
    result = TextNormalizer.remove_platform_noise("spotify")
    assert result == ""


def test_remove_platform_noise_preserves_real_title():
    result = TextNormalizer.remove_platform_noise("Shape of You")
    assert result == "Shape of You"


def test_remove_parentheticals():
    result = TextNormalizer.remove_parentheticals("Hello (feat. Beyoncé)")
    assert "feat. Beyoncé" not in result
    assert "Hello" in result


def test_remove_features():
    result = TextNormalizer.remove_features("Hello feat. Beyoncé")
    assert result == "Hello"


def test_clean_for_search_removes_noise():
    result = TextNormalizer.clean_for_search("Hello (Official Video) ft. Artist")
    assert "official" not in result
    assert "video" not in result


def test_build_search_query_with_artist():
    query = TextNormalizer.build_search_query("Adele", "Hello")
    assert "adele" in query
    assert "hello" in query


# ── ParsingUtils tests ─────────────────────────────────────────────────

def test_looks_like_album_detects_soundtrack():
    assert ParsingUtils.looks_like_album("Original Soundtrack") is True


def test_looks_like_album_rejects_song():
    assert ParsingUtils.looks_like_album("My Heart Will Go On") is False


def test_looks_like_artist_list_detects_list():
    assert ParsingUtils.looks_like_artist_list("John, Paul & George") is True
    assert ParsingUtils.looks_like_artist_list("A Day in the Life") is False


def test_split_title_artist():
    title, artist = ParsingUtils.split_title_artist("Hello - Adele")
    assert title == "Hello"
    assert artist == "Adele"


def test_split_title_artist_no_separator():
    title, artist = ParsingUtils.split_title_artist("Hello")
    assert title == "Hello"
    assert artist == ""


def test_parse_spotify_description():
    artist, album = ParsingUtils.parse_spotify_description("Adele · 25")
    assert artist == "Adele"
    assert album == "25"


# ── URL canonicalization tests ─────────────────────────────────────────

def test_canonicalize_spotify_drops_tracking():
    raw = "https://open.spotify.com/track/123?si=abc123"
    cleaned = _canonicalize_spotify_url(raw)
    assert "?si=" not in cleaned
    assert cleaned.endswith("/track/123")


def test_canonicalize_spotify_keeps_non_track():
    raw = "https://open.spotify.com/album/456?si=def"
    cleaned = _canonicalize_spotify_url(raw)
    assert cleaned == raw  # only track URLs are canonicalized


# ── SongInfo model tests ───────────────────────────────────────────────

def test_songinfo_strips_whitespace():
    info = SongInfo(title="  Hello  ", artist="  Adele  ")
    assert info.title == "Hello"
    assert info.artist == "Adele"


# ── MusicLinks model tests ─────────────────────────────────────────────

def test_musiclinks_count():
    links = MusicLinks(spotify="http://spotify.com/track/1", youtube_music="http://music.youtube.com/watch?v=abc")
    assert links.count() == 2


def test_musiclinks_count_empty():
    links = MusicLinks()
    assert links.count() == 0


# ── MatchValidator tests (pure computation, no network) ────────────────

def test_calculate_confidence_perfect_match():
    original = SongInfo(title="Hello", artist="Adele", platform="Spotify")
    found = SongInfo(title="Hello", artist="Adele", platform="YouTube")
    score = MatchValidator.calculate_confidence(original, found)
    assert score >= 99.0


def test_calculate_confidence_partial_match():
    original = SongInfo(title="Hello", artist="Adele", platform="Spotify")
    found = SongInfo(title="Hello (Live)", artist="Adele", platform="YouTube")
    score = MatchValidator.calculate_confidence(original, found)
    assert 50.0 < score <= 100.0


# ── Platform enum tests ────────────────────────────────────────────────

def test_platform_values():
    assert Platform.SPOTIFY.value == "Spotify"
    assert Platform.YOUTUBE_MUSIC.value == "YouTube Music"
    assert Platform.UNKNOWN.value == "Unknown"


# ── Regression: string constant identity ───────────────────────────────
# This test would have caught the split silently altering string literals.
# It extracts every evaluated string constant from the package files via
# ast.Constant walking and compares them against origin/devel:__main__.py.

import ast
import pathlib


def _collect_string_constants(source: str, exclude_docstrings: bool = True) -> dict:
    """Return {repr(value): (lineno, value)} for every string constant."""
    tree = ast.parse(source)
    out = {}

    # Collect all Expr nodes whose value is a Constant (docstrings)
    docstring_nodes = set()
    if exclude_docstrings:
        for expr_node in ast.walk(tree):
            if isinstance(expr_node, ast.Expr):
                if isinstance(expr_node.value, ast.Constant) and isinstance(expr_node.value.value, str):
                    docstring_nodes.add(id(expr_node.value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if exclude_docstrings and id(node) in docstring_nodes:
                continue
            key = repr(node.value)
            if key not in out:
                out[key] = (node.lineno, node.value)
    return out


ORIGINAL_PATH = pathlib.Path(__file__).resolve().parent.parent / ".."
# Path to the original __main__.py from origin/devel
_ORIG = None


def _get_originals():
    global _ORIG
    if _ORIG is not None:
        return _ORIG
    try:
        import subprocess
        result = subprocess.run(
            ["git", "show", "origin/devel:__main__.py"],
            capture_output=True, text=True, check=True, cwd=ORIGINAL_PATH
        )
        _ORIG = _collect_string_constants(result.stdout)
    except Exception:
        _ORIG = {}
    return _ORIG


def test_string_constants_match_originals():
    """Every string constant in the package must match origin/devel.

    This is the mechanical audit that would have caught the 3 string-literal
    drifts introduced by o7/py-split:

      1. ``Found: …`` message: ``\\n\\n`` → literal ``\\n\\n`` text
      2. ``send_results``: ``🎧 Choose your platform:`` line dropped entirely
      3. ``_extract_url`` regex: ``\\}`` inserted before ``\\]``

    The test fails if any string value in the package differs from the
    corresponding value in the original monolithic __main__.py.
    """
    originals = _get_originals()
    if not originals:
        pytest.skip("origin/devel:__main__.py not available — not in a git repo?")

    pkg_dir = pathlib.Path(__file__).resolve().parent.parent / "tune_bridge_bot"
    package_constants = {}

    for pyfile in sorted(pkg_dir.glob("*.py")):
        src = pyfile.read_text(encoding="utf-8")
        consts = _collect_string_constants(src)
        for key, (lineno, val) in consts.items():
            rel = f"{pyfile.name}:{lineno}"
            package_constants.setdefault(key, []).append((rel, val))

    # For every string constant that exists in the original, assert it's
    # also present in the package.  If it's missing, the split changed it.
    missing = []
    for key, (lineno, val) in originals.items():
        if key not in package_constants:
            missing.append(f"  origin/devel:{lineno:>4}  {key}")

    assert not missing, (
        f"{len(missing)} string constant(s) from origin/devel are missing "
        f"or altered in the package:\n" + "\n".join(missing)
    )
