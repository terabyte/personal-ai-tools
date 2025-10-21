#!/usr/bin/env python3

"""
Jira Cache - Persistent file-based cache for Jira metadata
Caches data that rarely changes (link types, users, issue types) to improve performance
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class JiraCache:
    """Manages persistent cache for Jira metadata."""

    def __init__(self, jira_url: str):
        """
        Initialize cache for a specific Jira instance.

        Args:
            jira_url: Base URL of Jira instance (e.g., "https://jira.atlassian.com")
        """
        self.jira_url = jira_url
        self.cache_dir = Path.home() / '.cache' / 'jira-view'
        self.cache_file = self.cache_dir / 'cache.json'
        self.cache = self._load_cache()

    def _load_cache(self) -> Dict:
        """Load cache from disk or return empty dict."""
        if not self.cache_file.exists():
            return {}

        try:
            with open(self.cache_file, 'r') as f:
                all_cache = json.load(f)
                return all_cache.get(self.jira_url, {})
        except (json.JSONDecodeError, IOError):
            # Cache file corrupted or unreadable - delete and start fresh
            try:
                self.cache_file.unlink()
            except:
                pass
            return {}

    def _save_cache(self):
        """Save cache to disk."""
        try:
            # Create cache directory if it doesn't exist
            self.cache_dir.mkdir(parents=True, exist_ok=True)

            # Load full cache file (all Jira instances)
            all_cache = {}
            if self.cache_file.exists():
                try:
                    with open(self.cache_file, 'r') as f:
                        all_cache = json.load(f)
                except:
                    pass

            # Update cache for this Jira instance
            all_cache[self.jira_url] = self.cache

            # Write back to disk
            with open(self.cache_file, 'w') as f:
                json.dump(all_cache, f, indent=2)
        except (IOError, OSError):
            # Can't write cache - degrade gracefully
            pass

    def _is_expired(self, entry: Dict) -> bool:
        """
        Check if cache entry is expired based on timestamp + ttl.

        Args:
            entry: Cache entry with 'timestamp' and 'ttl' keys

        Returns:
            True if expired, False otherwise
        """
        if 'timestamp' not in entry or 'ttl' not in entry:
            return True

        now = time.time()
        age = now - entry['timestamp']
        return age > entry['ttl']

    def _get_entry(self, category: str, key: Optional[str] = None) -> Optional[Dict]:
        """
        Get cache entry by category and optional key.

        Args:
            category: Top-level cache category (e.g., 'link_types', 'users')
            key: Optional subcategory key (e.g., project key for issue_types)

        Returns:
            Cache entry dict or None if not found
        """
        if category not in self.cache:
            return None

        if key is None:
            return self.cache[category]

        # Navigate to subcategory
        if key not in self.cache[category]:
            return None

        return self.cache[category][key]

    def get(self, category: str, key: Optional[str] = None,
            force_refresh: bool = False) -> Optional[Any]:
        """
        Get cached data if not expired.

        Args:
            category: Cache category (e.g., 'link_types', 'users')
            key: Optional subcategory key
            force_refresh: If True, skip cache and return None

        Returns:
            Cached data or None if expired/not found/force_refresh
        """
        if force_refresh:
            return None

        entry = self._get_entry(category, key)
        if entry is None:
            return None

        if self._is_expired(entry):
            return None

        return entry.get('data')

    def set(self, category: str, data: Any, ttl: int, key: Optional[str] = None):
        """
        Set cache entry with TTL.

        Args:
            category: Cache category
            data: Data to cache
            ttl: Time-to-live in seconds
            key: Optional subcategory key
        """
        entry = {
            'timestamp': time.time(),
            'ttl': ttl,
            'data': data
        }

        if key is None:
            self.cache[category] = entry
        else:
            if category not in self.cache:
                self.cache[category] = {}
            self.cache[category][key] = entry

        self._save_cache()

    def get_age(self, category: str, key: Optional[str] = None) -> str:
        """
        Get human-readable age of cache entry.

        Args:
            category: Cache category
            key: Optional subcategory key

        Returns:
            Human-readable age string (e.g., "2h ago", "5m ago", "just now")
        """
        entry = self._get_entry(category, key)
        if entry is None or 'timestamp' not in entry:
            return "never cached"

        now = time.time()
        age_seconds = now - entry['timestamp']

        if age_seconds < 60:
            return "just now"
        elif age_seconds < 3600:
            minutes = int(age_seconds / 60)
            return f"{minutes}m ago"
        elif age_seconds < 86400:
            hours = int(age_seconds / 3600)
            return f"{hours}h ago"
        else:
            days = int(age_seconds / 86400)
            return f"{days}d ago"

    def invalidate(self, category: str, key: Optional[str] = None):
        """
        Manually invalidate specific cache entry.

        Args:
            category: Cache category to invalidate
            key: Optional subcategory key
        """
        if key is None:
            if category in self.cache:
                del self.cache[category]
        else:
            if category in self.cache and key in self.cache[category]:
                del self.cache[category][key]

        self._save_cache()

    def clear_all(self):
        """Clear entire cache for this Jira instance."""
        self.cache = {}
        self._save_cache()

    def is_cached(self, category: str, key: Optional[str] = None) -> bool:
        """
        Check if data is cached (not expired).

        Args:
            category: Cache category
            key: Optional subcategory key

        Returns:
            True if cached and not expired, False otherwise
        """
        entry = self._get_entry(category, key)
        if entry is None:
            return False
        return not self._is_expired(entry)
