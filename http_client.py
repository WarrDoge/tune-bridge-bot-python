#!/usr/bin/env python3
"""HTTP client with retry, backoff, and circuit breaker pattern."""

import asyncio
import logging
import time
from collections import defaultdict
from typing import Dict, List, Optional

import httpx

from config import REQUEST_TIMEOUT, MAX_CONCURRENT_REQUESTS, MAX_RETRIES, BACKOFF_BASE, CIRCUIT_BREAKER_THRESHOLD, CIRCUIT_BREAKER_TIMEOUT

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Circuit breaker pattern for failing services"""

    def __init__(self, threshold: int = CIRCUIT_BREAKER_THRESHOLD, timeout: int = CIRCUIT_BREAKER_TIMEOUT):
        self.threshold = threshold
        self.timeout = timeout
        self.failures: Dict[str, List[float]] = defaultdict(list)
        self.opened_at: Dict[str, float] = {}

    def record_failure(self, service: str):
        """Record a failure"""
        now = time.time()
        self.failures[service].append(now)

        # Keep only recent failures (last 5 minutes)
        self.failures[service] = [t for t in self.failures[service] if now - t < 300]

        # Open circuit if threshold exceeded
        if len(self.failures[service]) >= self.threshold:
            self.opened_at[service] = now
            logger.warning(f"Circuit breaker opened for {service}")

    def record_success(self, service: str):
        """Record a success (reset failures)"""
        if service in self.failures:
            self.failures[service].clear()
        if service in self.opened_at:
            del self.opened_at[service]
            logger.info(f"Circuit breaker closed for {service}")

    def is_open(self, service: str) -> bool:
        """Check if circuit is open"""
        if service not in self.opened_at:
            return False

        # Check if timeout has passed
        if time.time() - self.opened_at[service] > self.timeout:
            del self.opened_at[service]
            logger.info(f"Circuit breaker reset for {service}")
            return False

        return True


class HTTPClient:
    """HTTP client with retry, backoff, and circuit breaker"""

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            limits=httpx.Limits(max_connections=MAX_CONCURRENT_REQUESTS),
            follow_redirects=True
        )
        self.circuit_breaker = CircuitBreaker(
            threshold=CIRCUIT_BREAKER_THRESHOLD,
            timeout=CIRCUIT_BREAKER_TIMEOUT
        )

    async def get(self, url: str, service: str = "default") -> Optional[httpx.Response]:
        """GET request with retry and circuit breaker"""
        # Check circuit breaker
        if self.circuit_breaker.is_open(service):
            logger.warning(f"Circuit breaker open for {service}, skipping request")
            return None

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Referer': 'https://open.spotify.com/',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Dest': 'document',
        }

        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                if attempt > 0:
                    # Exponential backoff with jitter
                    delay = BACKOFF_BASE * (2 ** attempt) + (0.1 * attempt)
                    await asyncio.sleep(delay)

                response = await self.client.get(url, headers=headers)

                if response.status_code == 200:
                    self.circuit_breaker.record_success(service)
                    return response
                elif response.status_code == 429:
                    # Rate limited
                    logger.warning(f"Rate limited on {service}: {url}")
                    await asyncio.sleep(2 ** attempt)
                    continue
                elif response.status_code >= 500:
                    # Server error, retry
                    logger.warning(f"Server error {response.status_code} on {service}")
                    continue
                else:
                    # Client error, don't retry
                    logger.warning(f"Client error {response.status_code}: {url}")
                    self.circuit_breaker.record_failure(service)
                    return None

            except httpx.TimeoutException:
                logger.warning(f"Timeout on attempt {attempt + 1} for {url}")
                last_error = "timeout"
            except httpx.NetworkError as e:
                logger.warning(f"Network error on attempt {attempt + 1}: {e}")
                last_error = str(e)
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                last_error = str(e)
                break

        # All retries failed
        self.circuit_breaker.record_failure(service)
        logger.error(f"All retries failed for {url}: {last_error}")
        return None

    async def get_via_text_mirror(self, url: str, service: str = "text_mirror") -> Optional[httpx.Response]:
        """Fetch a prerendered text/HTML version via r.jina.ai mirror."""
        try:
            mirror = f"https://r.jina.ai/http://{url.replace('https://', '').replace('http://','')}"
            return await self.get(mirror, service=service)
        except Exception:
            return None

    async def close(self):
        """Close the client"""
        await self.client.aclose()
