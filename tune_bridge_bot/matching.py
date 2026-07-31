"""Search engine and match validation."""

import logging
import re
from typing import Optional, Tuple
from urllib.parse import quote, unquote

from tune_bridge_bot.text import TextNormalizer
from tune_bridge_bot.models import SongInfo

# Third-party
try:
    from bs4 import BeautifulSoup
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install python-telegram-bot httpx beautifulsoup4 lxml fuzzywuzzy python-levenshtein")
    import sys
    sys.exit(1)

try:
    from fuzzywuzzy import fuzz
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install python-telegram-bot httpx beautifulsoup4 lxml fuzzywuzzy python-levenshtein")
    import sys
    sys.exit(1)

from tune_bridge_bot.infra import MATCH_THRESHOLD_LOW

logger = logging.getLogger(__name__)


class SearchEngine:
    """Search for songs across platforms using DuckDuckGo"""

    @staticmethod
    async def search(query: str, site: str, http_client) -> Optional[str]:
        """Search using DuckDuckGo HTML"""
        search_query = f"site:{site} {query}"
        search_url = f"https://duckduckgo.com/html/?q={quote(search_query)}"

        response = await http_client.get(search_url, service=f"search_{site}")
        if not response:
            return None

        try:
            soup = BeautifulSoup(response.text, 'lxml')

            # Find first result
            result_link = soup.find('a', class_='result__a')
            if not result_link:
                return None

            href = result_link.get('href', '')

            # Extract actual URL from DuckDuckGo redirect
            match = re.search(r'uddg=([^&]+)', href)
            if match:
                return unquote(match.group(1))

            return href if site in href else None
        except Exception as e:
            logger.error(f"Search error for {site}: {e}")

        return None


class MatchValidator:
    """Validate search results match original song"""

    @staticmethod
    def calculate_confidence(original: SongInfo, found: SongInfo) -> float:
        """Calculate match confidence score (0-100)"""
        # Normalize for comparison
        orig_title = TextNormalizer.normalize_for_matching(original.title)
        orig_artist = TextNormalizer.normalize_for_matching(original.artist)
        found_title = TextNormalizer.normalize_for_matching(found.title)
        found_artist = TextNormalizer.normalize_for_matching(found.artist)

        # Calculate fuzzy match scores
        title_score = fuzz.ratio(orig_title, found_title)
        artist_score = fuzz.ratio(orig_artist, found_artist) if orig_artist and found_artist else 0

        # Weighted average (title more important)
        if orig_artist and found_artist:
            confidence = (title_score * 0.6) + (artist_score * 0.4)
        else:
            confidence = title_score

        return confidence

    @staticmethod
    def is_valid_match(original: SongInfo, found: SongInfo, threshold: float = MATCH_THRESHOLD_LOW) -> bool:
        """Check if found song is valid match"""
        confidence = MatchValidator.calculate_confidence(original, found)
        return confidence >= threshold
