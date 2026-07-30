#!/usr/bin/env python3
"""Caching layer with TTL support and negative caching."""

from datetime import datetime, timedelta
from typing import Dict, Optional

from config import CacheEntry, CACHE_TTL_SUCCESS, CACHE_TTL_FAILURE, CACHE_TTL_INFO


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

    def stats(self) -> Dict[str, float]:
        """Get cache statistics"""
        return {
            'size': len(self._cache),
            'hits': self._hits,
            'misses': self._misses,
            'hit_rate': self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0
        }
