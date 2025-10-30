#!/usr/bin/env python3
"""
SQLite-based persistent cache for Jira data.

Replaces jira_cache.py with a thread-safe SQLite implementation that caches:
- Metadata (link_types, issue_types) with TTL
- User information (persistent between runs)
- Ticket data (full ticket objects)
- Query results (JQL → ticket keys mapping)

Thread Safety:
- Uses connection-per-thread pattern
- All operations are atomic via SQLite transactions
- Safe for concurrent access from multiple threads
"""

import sqlite3
import json
import pickle
import time
import hashlib
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class JiraSQLiteCache:
    """
    SQLite-based cache for Jira data with TTL support and thread safety.

    Drop-in replacement for JiraCache with same API plus additional methods
    for tickets, users, and query results.
    """

    def __init__(self, jira_url: str, cache_dir: Optional[Path] = None):
        """
        Initialize SQLite cache for a specific Jira instance.

        Args:
            jira_url: Base URL of Jira instance (e.g., 'https://company.atlassian.net')
            cache_dir: Optional custom cache directory (default: ~/.cache/jira-view)
        """
        self.jira_url = jira_url

        # Setup cache directory
        if cache_dir is None:
            cache_dir = Path.home() / '.cache' / 'jira-view'
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Database file
        self.db_path = self.cache_dir / 'jira_cache.db'

        # Thread-local storage for connections
        self._local = threading.local()

        # Initialize database schema
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'conn'):
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=10.0
            )
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        """Initialize database schema if not exists."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Tickets table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                key TEXT PRIMARY KEY,
                updated TEXT NOT NULL,
                data BLOB NOT NULL,
                cached_at REAL NOT NULL
            )
        ''')

        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                account_id TEXT PRIMARY KEY,
                email TEXT,
                display_name TEXT,
                data BLOB NOT NULL,
                cached_at REAL NOT NULL
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_display ON users(display_name COLLATE NOCASE)')

        # Metadata table (link_types, issue_types, etc.)
        # Note: key defaults to empty string for NULL values
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS metadata (
                category TEXT NOT NULL,
                key TEXT NOT NULL DEFAULT '',
                data BLOB NOT NULL,
                ttl INTEGER NOT NULL,
                cached_at REAL NOT NULL,
                PRIMARY KEY (category, key)
            )
        ''')

        # Query results table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS query_results (
                query_hash TEXT PRIMARY KEY,
                jql TEXT NOT NULL,
                ticket_keys TEXT NOT NULL,
                cached_at REAL NOT NULL
            )
        ''')

        conn.commit()

    # ===== API-compatible methods (same as JiraCache) =====

    def get(self, category: str, key: Optional[str] = None, force_refresh: bool = False) -> Optional[Any]:
        """
        Get cached data for a category with TTL checking.

        API-compatible with JiraCache.get()

        Args:
            category: Cache category (e.g., 'link_types', 'issue_types')
            key: Optional subcategory key (e.g., project key for issue_types)
            force_refresh: If True, return None to force fresh fetch

        Returns:
            Cached data if valid, None if expired or not found
        """
        if force_refresh:
            return None

        conn = self._get_connection()
        cursor = conn.cursor()

        # Convert None to empty string for key
        key_value = key if key is not None else ''

        cursor.execute('''
            SELECT data, ttl, cached_at FROM metadata
            WHERE category = ? AND key = ?
        ''', (category, key_value))

        row = cursor.fetchone()
        if not row:
            return None

        # Check TTL
        age = time.time() - row['cached_at']
        if age > row['ttl']:
            return None

        # Unpickle and return data
        return pickle.loads(row['data'])

    def set(self, category: str, data: Any, ttl: int, key: Optional[str] = None):
        """
        Store data in cache with TTL.

        API-compatible with JiraCache.set()

        Args:
            category: Cache category
            data: Data to cache (will be pickled)
            ttl: Time-to-live in seconds
            key: Optional subcategory key
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Convert None to empty string for key
        key_value = key if key is not None else ''

        data_blob = pickle.dumps(data)
        cached_at = time.time()

        cursor.execute('''
            INSERT OR REPLACE INTO metadata (category, key, data, ttl, cached_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (category, key_value, data_blob, ttl, cached_at))

        conn.commit()

    def is_cached(self, category: str, key: Optional[str] = None) -> bool:
        """
        Check if data is cached and not expired.

        API-compatible with JiraCache.is_cached()
        """
        return self.get(category, key) is not None

    def get_age(self, category: str, key: Optional[str] = None) -> Optional[str]:
        """
        Get human-readable age of cached data.

        API-compatible with JiraCache.get_age()

        Returns:
            Age string like "2h ago", "5m ago", or None if not cached
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Convert None to empty string for key
        key_value = key if key is not None else ''

        cursor.execute('''
            SELECT cached_at FROM metadata
            WHERE category = ? AND key = ?
        ''', (category, key_value))

        row = cursor.fetchone()
        if not row:
            return None

        age_seconds = time.time() - row['cached_at']
        return self._format_age(age_seconds)

    def invalidate(self, category: str, key: Optional[str] = None):
        """
        Invalidate (delete) cached data.

        API-compatible with JiraCache.invalidate()
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Convert None to empty string for key
        key_value = key if key is not None else ''

        cursor.execute('''
            DELETE FROM metadata
            WHERE category = ? AND key = ?
        ''', (category, key_value))

        conn.commit()

    def clear_all(self):
        """
        Clear all cached data for this Jira instance.

        API-compatible with JiraCache.clear_all()
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('DELETE FROM tickets')
        cursor.execute('DELETE FROM users')
        cursor.execute('DELETE FROM metadata')
        cursor.execute('DELETE FROM query_results')

        conn.commit()

    # ===== New methods for ticket caching =====

    def get_ticket(self, key: str) -> Optional[dict]:
        """Get cached ticket by key."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT data FROM tickets WHERE key = ?', (key,))
        row = cursor.fetchone()

        if row:
            return pickle.loads(row['data'])
        return None

    def set_ticket(self, key: str, ticket: dict, updated: str):
        """
        Store ticket in cache.

        Args:
            key: Ticket key (e.g., 'PROJ-123')
            ticket: Full ticket dict from API
            updated: ISO timestamp from Jira (for freshness checking)
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        data_blob = pickle.dumps(ticket)
        cached_at = time.time()

        cursor.execute('''
            INSERT OR REPLACE INTO tickets (key, updated, data, cached_at)
            VALUES (?, ?, ?, ?)
        ''', (key, updated, data_blob, cached_at))

        conn.commit()

    def get_many_tickets(self, keys: List[str]) -> Dict[str, dict]:
        """
        Bulk fetch tickets by keys.

        Returns:
            Dict mapping key → ticket for found tickets
        """
        if not keys:
            return {}

        conn = self._get_connection()
        cursor = conn.cursor()

        placeholders = ','.join('?' * len(keys))
        cursor.execute(f'SELECT key, data FROM tickets WHERE key IN ({placeholders})', keys)

        result = {}
        for row in cursor.fetchall():
            result[row['key']] = pickle.loads(row['data'])

        return result

    def set_many_tickets(self, tickets: List[dict]):
        """
        Bulk store tickets.

        Args:
            tickets: List of ticket dicts (must have 'key' and 'fields.updated')
        """
        if not tickets:
            return

        conn = self._get_connection()
        cursor = conn.cursor()
        cached_at = time.time()

        values = []
        for ticket in tickets:
            key = ticket['key']
            updated = ticket.get('fields', {}).get('updated', '')
            data_blob = pickle.dumps(ticket)
            values.append((key, updated, data_blob, cached_at))

        cursor.executemany('''
            INSERT OR REPLACE INTO tickets (key, updated, data, cached_at)
            VALUES (?, ?, ?, ?)
        ''', values)

        conn.commit()

    # ===== New methods for user caching =====

    def get_user_by_account_id(self, account_id: str) -> Optional[dict]:
        """Get cached user by account ID."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT data FROM users WHERE account_id = ?', (account_id,))
        row = cursor.fetchone()

        if row:
            return pickle.loads(row['data'])
        return None

    def get_user_by_email(self, email: str) -> Optional[dict]:
        """Get cached user by email."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT data FROM users WHERE email = ?', (email,))
        row = cursor.fetchone()

        if row:
            return pickle.loads(row['data'])
        return None

    def get_user_by_display_name(self, display_name: str) -> Optional[dict]:
        """Get cached user by display name (case-insensitive)."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT data FROM users WHERE display_name = ? COLLATE NOCASE', (display_name,))
        row = cursor.fetchone()

        if row:
            return pickle.loads(row['data'])
        return None

    def set_user(self, account_id: str, user: dict):
        """
        Store user in cache.

        Args:
            account_id: User's account ID
            user: User dict from API (must have displayName, emailAddress)
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        data_blob = pickle.dumps(user)
        cached_at = time.time()
        email = user.get('emailAddress', '')
        display_name = user.get('displayName', '')

        cursor.execute('''
            INSERT OR REPLACE INTO users (account_id, email, display_name, data, cached_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (account_id, email, display_name, data_blob, cached_at))

        conn.commit()

    # ===== New methods for query result caching =====

    def get_query_result(self, jql: str, ttl_seconds: int = 300) -> Optional[List[str]]:
        """
        Get cached query result (JQL → ticket keys).

        Args:
            jql: JQL query string
            ttl_seconds: TTL in seconds (default: 5 minutes)

        Returns:
            List of ticket keys if cached and fresh, None otherwise
        """
        query_hash = hashlib.sha256(jql.encode()).hexdigest()

        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT ticket_keys, cached_at FROM query_results WHERE query_hash = ?
        ''', (query_hash,))

        row = cursor.fetchone()
        if not row:
            return None

        # Check TTL
        age = time.time() - row['cached_at']
        if age > ttl_seconds:
            return None

        return json.loads(row['ticket_keys'])

    def set_query_result(self, jql: str, ticket_keys: List[str]):
        """
        Store query result (JQL → ticket keys).

        Args:
            jql: JQL query string
            ticket_keys: List of ticket keys returned by query
        """
        query_hash = hashlib.sha256(jql.encode()).hexdigest()

        conn = self._get_connection()
        cursor = conn.cursor()
        cached_at = time.time()

        cursor.execute('''
            INSERT OR REPLACE INTO query_results (query_hash, jql, ticket_keys, cached_at)
            VALUES (?, ?, ?, ?)
        ''', (query_hash, jql, json.dumps(ticket_keys), cached_at))

        conn.commit()

    # ===== Statistics and management =====

    def get_cache_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics for display in cache menu.

        Returns:
            Dict with counts, sizes, and ages for all cache types
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        stats = {}

        # Ticket stats
        cursor.execute('SELECT COUNT(*) as count FROM tickets')
        ticket_count = cursor.fetchone()['count']

        cursor.execute('SELECT MIN(cached_at) as oldest, MAX(cached_at) as newest FROM tickets')
        row = cursor.fetchone()

        stats['tickets'] = {
            'count': ticket_count,
            'oldest_age': self._format_age(time.time() - row['oldest']) if row['oldest'] else None,
            'newest_age': self._format_age(time.time() - row['newest']) if row['newest'] else None,
        }

        # User stats
        cursor.execute('SELECT COUNT(*) as count FROM users')
        user_count = cursor.fetchone()['count']

        cursor.execute('SELECT MIN(cached_at) as oldest FROM users')
        row = cursor.fetchone()

        stats['users'] = {
            'count': user_count,
            'oldest_age': self._format_age(time.time() - row['oldest']) if row['oldest'] else None,
        }

        # Metadata stats
        metadata_info = {}
        cursor.execute('SELECT category, cached_at FROM metadata')
        for row in cursor.fetchall():
            age = time.time() - row['cached_at']
            metadata_info[row['category']] = self._format_age(age)

        stats['metadata'] = metadata_info

        # Database size
        stats['db_size_bytes'] = self.db_path.stat().st_size if self.db_path.exists() else 0
        stats['db_size_mb'] = round(stats['db_size_bytes'] / (1024 * 1024), 1)

        return stats

    def clear_tickets(self) -> int:
        """Clear ticket cache. Returns number of tickets cleared."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) as count FROM tickets')
        count = cursor.fetchone()['count']

        cursor.execute('DELETE FROM tickets')
        cursor.execute('DELETE FROM query_results')  # Also clear query cache
        conn.commit()

        return count

    def clear_users(self) -> int:
        """Clear user cache. Returns number of users cleared."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) as count FROM users')
        count = cursor.fetchone()['count']

        cursor.execute('DELETE FROM users')
        conn.commit()

        return count

    def get_all_ticket_keys(self) -> List[str]:
        """Get list of all cached ticket keys."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT key FROM tickets')
        return [row['key'] for row in cursor.fetchall()]

    def get_stale_ticket_keys(self, fresh_tickets: Dict[str, str]) -> List[str]:
        """
        Find tickets that need refresh based on 'updated' timestamps.

        Args:
            fresh_tickets: Dict mapping ticket_key → updated timestamp from API

        Returns:
            List of ticket keys that are stale (cached updated < API updated)
        """
        if not fresh_tickets:
            return []

        conn = self._get_connection()
        cursor = conn.cursor()

        stale_keys = []

        for key, api_updated in fresh_tickets.items():
            cursor.execute('SELECT updated FROM tickets WHERE key = ?', (key,))
            row = cursor.fetchone()

            if not row or row['updated'] < api_updated:
                stale_keys.append(key)

        return stale_keys

    def get_oldest_cached_time(self, keys: List[str]) -> Optional[float]:
        """
        Get oldest cached_at timestamp for given ticket keys.

        Returns:
            Oldest timestamp, or None if no tickets cached
        """
        if not keys:
            return None

        conn = self._get_connection()
        cursor = conn.cursor()

        placeholders = ','.join('?' * len(keys))
        cursor.execute(f'SELECT MIN(cached_at) as oldest FROM tickets WHERE key IN ({placeholders})', keys)

        row = cursor.fetchone()
        return row['oldest'] if row['oldest'] else None

    # ===== Helper methods =====

    def _format_age(self, seconds: float) -> str:
        """Format age in seconds as human-readable string."""
        if seconds < 60:
            return f"{int(seconds)}s ago"
        elif seconds < 3600:
            return f"{int(seconds / 60)}m ago"
        elif seconds < 86400:
            hours = int(seconds / 3600)
            mins = int((seconds % 3600) / 60)
            if mins > 0:
                return f"{hours}h {mins}m ago"
            return f"{hours}h ago"
        else:
            days = int(seconds / 86400)
            hours = int((seconds % 86400) / 3600)
            if hours > 0:
                return f"{days}d {hours}h ago"
            return f"{days}d ago"
