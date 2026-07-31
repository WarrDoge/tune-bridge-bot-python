"""tune_bridge_bot — Music link converter bot package."""

import logging

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

from tune_bridge_bot.models import Platform, SongInfo, MusicLinks, CacheEntry
from tune_bridge_bot.text import TextNormalizer, ParsingUtils
from tune_bridge_bot.infra import (
    SmartCache, CircuitBreaker, HTTPClient,
    MATCH_THRESHOLD_HIGH, MATCH_THRESHOLD_MEDIUM, MATCH_THRESHOLD_LOW,
    CACHE_TTL_SUCCESS, CACHE_TTL_FAILURE, CACHE_TTL_INFO,
    REQUEST_TIMEOUT, MAX_CONCURRENT_REQUESTS, MAX_RETRIES, BACKOFF_BASE,
    MAX_REQUESTS_PER_PLATFORM, CIRCUIT_BREAKER_THRESHOLD, CIRCUIT_BREAKER_TIMEOUT,
)
from tune_bridge_bot.parsers import SpotifyParser, YouTubeParser, AppleMusicParser, _canonicalize_spotify_url
from tune_bridge_bot.matching import SearchEngine, MatchValidator
from tune_bridge_bot.bot import MusicBot, main

__all__ = [
    # Models
    "Platform", "SongInfo", "MusicLinks", "CacheEntry",
    # Text
    "TextNormalizer", "ParsingUtils",
    # Infrastructure
    "SmartCache", "CircuitBreaker", "HTTPClient",
    "MATCH_THRESHOLD_HIGH", "MATCH_THRESHOLD_MEDIUM", "MATCH_THRESHOLD_LOW",
    "CACHE_TTL_SUCCESS", "CACHE_TTL_FAILURE", "CACHE_TTL_INFO",
    "REQUEST_TIMEOUT", "MAX_CONCURRENT_REQUESTS", "MAX_RETRIES", "BACKOFF_BASE",
    "MAX_REQUESTS_PER_PLATFORM", "CIRCUIT_BREAKER_THRESHOLD", "CIRCUIT_BREAKER_TIMEOUT",
    # Parsers
    "SpotifyParser", "YouTubeParser", "AppleMusicParser", "_canonicalize_spotify_url",
    # Matching
    "SearchEngine", "MatchValidator",
    # Bot
    "MusicBot", "main",
]
