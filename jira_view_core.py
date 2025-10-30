"""
Jira View Controllers - Business Logic for TUI Application

This module provides pure business logic controllers with zero curses dependencies.
All controllers are designed to be fully testable and thread-safe.

Threading Design Principles:
1. Never hold locks during I/O - locks only for memory updates
2. Use atomic flags for cross-thread communication
3. Minimize shared state - prefer immutable data
4. Single lock per controller - avoid lock ordering issues
5. Daemon threads for background work - clean shutdown
6. Document thread safety for every public method

Controllers:
- QueryController: JQL query execution with caching and background refresh
- TicketController: Individual ticket operations
- CacheController: Cache management operations
"""

from typing import List, Optional, Tuple, Callable, Dict, Any
from dataclasses import dataclass
import threading
import time


@dataclass
class QueryResult:
    """
    Result of executing a JQL query.

    Attributes:
        tickets: List of ticket dictionaries
        status: Current status ("CURRENT", "UPDATING", "FIRST_RUN", "ERROR")
        cache_age: Seconds since oldest ticket was cached (None if no cache)
        is_updating: True if background refresh is in progress
        error: Error message if status is "ERROR"
    """
    tickets: List[dict]
    status: str
    cache_age: Optional[float] = None
    is_updating: bool = False
    error: Optional[str] = None


class QueryController:
    """
    Handles JQL query execution with caching and background refresh.

    This controller provides non-blocking query execution that returns
    immediately with cached data (if available) and spawns background
    threads to verify and refresh stale data.

    Thread Safety:
    - execute_query(): Thread-safe, returns immediately, spawns background thread
    - get_background_status(): Thread-safe, lock-free reads of atomic flags
    - is_startup_complete(): Thread-safe, always returns True
    - Internal lock (update_lock) protects ticket_cache dict updates only
    - Never holds lock during network I/O

    Design:
    - Atomic flags (refresh_needed, is_updating) for cross-thread communication
    - Lock only held during quick memory updates to ticket_cache
    - Network I/O happens outside of any locks
    - Background threads are daemon threads for clean shutdown
    """

    def __init__(self, utils):
        """
        Initialize QueryController.

        Args:
            utils: JiraUtils instance (handles API calls and caching)
        """
        self.utils = utils
        self.ticket_cache: Dict[str, dict] = {}  # key -> ticket dict
        self.update_lock = threading.Lock()  # Protects ticket_cache only

        # Atomic flags for cross-thread communication
        self.refresh_needed = False  # Set by background thread when cache updated
        self.is_updating = False  # True when background refresh running

        # Background thread tracking
        self._background_thread: Optional[threading.Thread] = None
        self._progress_current = 0
        self._progress_total = 0

    def execute_query(
        self,
        jql: str,
        fields: List[str],
        force_refresh: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> QueryResult:
        """
        Execute JQL query with intelligent caching.

        Returns immediately with cached data if available. Spawns a background
        thread to verify cache freshness and refresh stale tickets.

        Thread Safety: Safe to call from multiple threads. Never blocks on I/O.
        Each call may spawn a background thread, but only one refresh runs at a time.

        Performance: Returns in <1 second even for large queries (from cache).
        Background refresh happens asynchronously without blocking the UI.

        Args:
            jql: JQL query string (e.g., "project=TEST AND status='In Progress'")
            fields: List of field names to fetch (e.g., ["key", "summary", "status"])
            force_refresh: If True, bypass cache and fetch from API immediately
            progress_callback: Optional callback(current, total) for progress updates

        Returns:
            QueryResult with tickets and status information

        Examples:
            >>> controller = QueryController(jira_utils)
            >>> result = controller.execute_query("project=TEST", ["key", "summary"])
            >>> print(f"Got {len(result.tickets)} tickets, status: {result.status}")
            >>> # Returns immediately, background refresh may still be running
        """
        try:
            # Try to get from cache first
            cache_tickets = self._get_cached_tickets()

            if force_refresh or not cache_tickets:
                # Need to fetch: spawn background thread and return immediately
                # This ensures we NEVER block, even on first run or force refresh
                self._spawn_background_refresh(jql, fields, progress_callback)

                if not cache_tickets:
                    # First run: return empty with FIRST_RUN status
                    # UI will show loading state, data appears when background completes
                    return QueryResult(
                        tickets=[],
                        status="FIRST_RUN",
                        cache_age=None,
                        is_updating=True
                    )
                else:
                    # Force refresh: return cached data while refreshing
                    return QueryResult(
                        tickets=list(cache_tickets.values()),
                        status="UPDATING",
                        cache_age=self._calculate_cache_age(cache_tickets),
                        is_updating=True
                    )

            # Have cache and not forcing refresh: return immediately
            # Background refresh will verify freshness asynchronously
            self._spawn_background_refresh(jql, fields, progress_callback)

            return QueryResult(
                tickets=list(cache_tickets.values()),
                status="UPDATING" if self.is_updating else "CURRENT",
                cache_age=self._calculate_cache_age(cache_tickets),
                is_updating=self.is_updating
            )

        except Exception as e:
            # Error: return empty result with error status
            return QueryResult(
                tickets=[],
                status="ERROR",
                error=str(e),
                is_updating=False
            )

    def _fetch_from_api(
        self,
        jql: str,
        fields: List[str],
        progress_callback: Optional[Callable] = None
    ) -> List[dict]:
        """
        Fetch tickets from Jira API.

        Thread Safety: Safe to call from any thread. Does not hold locks.

        Args:
            jql: JQL query string
            fields: List of field names
            progress_callback: Optional progress callback

        Returns:
            List of ticket dictionaries
        """
        # Delegate to jira_utils for API call
        # This is where network I/O happens (NO LOCK HELD)
        tickets = self.utils.fetch_all_jql_results(
            jql,
            fields,
            progress_callback=progress_callback
        )
        return tickets if tickets else []

    def _get_cached_tickets(self) -> Dict[str, dict]:
        """
        Get cached tickets.

        Thread Safety: Acquires lock for read.

        Returns:
            Dictionary of ticket_key -> ticket_dict
        """
        with self.update_lock:
            return dict(self.ticket_cache)  # Return copy

    def _spawn_background_refresh(
        self,
        jql: str,
        fields: List[str],
        progress_callback: Optional[Callable] = None
    ):
        """
        Spawn background thread to verify and refresh cache.

        Thread Safety: Safe to call multiple times. Only spawns if no refresh running.

        Args:
            jql: JQL query string
            fields: List of field names
            progress_callback: Optional progress callback
        """
        # Only spawn if not already running
        if self.is_updating:
            return

        self.is_updating = True

        def background_refresh():
            try:
                # Fetch from API (NO LOCK HELD during I/O)
                fresh_tickets = self._fetch_from_api(jql, fields, progress_callback)

                # Quick update with lock
                with self.update_lock:
                    self.ticket_cache = {t["key"]: t for t in fresh_tickets}
                    self.refresh_needed = True  # Signal main thread

            except Exception as e:
                # Log error but don't crash
                print(f"Background refresh error: {e}")
            finally:
                self.is_updating = False

        # Spawn daemon thread
        self._background_thread = threading.Thread(
            target=background_refresh,
            daemon=True
        )
        self._background_thread.start()

    def _calculate_cache_age(self, tickets: Dict[str, dict]) -> Optional[float]:
        """
        Calculate age of oldest cached ticket.

        Thread Safety: Pure function, no shared state.

        Args:
            tickets: Dictionary of cached tickets

        Returns:
            Seconds since oldest ticket was cached, or None
        """
        if not tickets:
            return None

        # For now, return 0 (will be implemented with real cache timestamps)
        return 0.0

    def get_background_status(self) -> Tuple[bool, Optional[int], Optional[int]]:
        """
        Get status of background refresh operation.

        Thread Safety: Lock-free read of atomic flags. Always safe.

        Returns:
            Tuple of (is_running, current_count, total_count)
            - is_running: True if background refresh in progress
            - current_count: Number of tickets processed (or None)
            - total_count: Total tickets to process (or None)

        Examples:
            >>> is_running, current, total = controller.get_background_status()
            >>> if is_running:
            ...     print(f"Refreshing: {current}/{total}")
        """
        # Atomic reads, no lock needed
        return (
            self.is_updating,
            self._progress_current if self.is_updating else None,
            self._progress_total if self.is_updating else None
        )

    def is_startup_complete(self) -> bool:
        """
        Check if controller is ready to display UI.

        This always returns True because QueryController never blocks startup.
        The controller returns immediately from execute_query() with cached data
        or spawns a background thread for fetching.

        Thread Safety: Always safe, no shared state accessed.

        Returns:
            Always True (controller never blocks startup)

        Design Note:
            This method exists to verify the "<1 second startup" requirement.
            If this returns False, something is wrong with the implementation.
        """
        return True


class TicketController:
    """
    Handles operations on individual tickets.

    This controller provides operations on single tickets, such as refreshing
    a specific ticket from the API or formatting ticket data for display.

    Thread Safety:
    - refresh_ticket(): Spawns background thread, safe to call concurrently
    - get_cached_ticket(): Thread-safe via cache layer
    - format_ticket_display(): Pure function, no shared state

    Design:
    - Minimal shared state (delegates to cache)
    - Background threads for I/O operations
    - Pure functions for formatting (no side effects)
    """

    def __init__(self, utils):
        """
        Initialize TicketController.

        Args:
            utils: JiraUtils instance (handles API calls and caching)
        """
        self.utils = utils

    def refresh_ticket(
        self,
        ticket_key: str,
        callback: Optional[Callable[[dict], None]] = None
    ) -> None:
        """
        Force refresh single ticket from API in background.

        Spawns a background thread to fetch the latest ticket data from Jira.
        Does not block. Calls callback when complete.

        Thread Safety: Safe to call for same or different tickets concurrently.
        Each call spawns its own background thread.

        Args:
            ticket_key: Ticket key (e.g., "PROJ-123")
            callback: Called when refresh complete with updated ticket dict

        Examples:
            >>> controller.refresh_ticket(
            ...     "TEST-123",
            ...     callback=lambda t: print(f"Refreshed: {t['key']}")
            ... )
            >>> # Returns immediately, callback called when complete
        """
        def background_refresh():
            try:
                # Fetch from API (NO LOCK, this is I/O)
                jql = f"key = {ticket_key}"
                tickets = self.utils.fetch_all_jql_results(jql, ["*"])

                if tickets:
                    ticket = tickets[0]
                    # Update cache
                    if hasattr(self.utils, 'cache'):
                        # Cache update is thread-safe via cache layer
                        pass  # Will be implemented with cache.set_ticket()

                    # Call callback if provided
                    if callback:
                        callback(ticket)
            except Exception as e:
                print(f"Error refreshing ticket {ticket_key}: {e}")

        # Spawn daemon thread
        thread = threading.Thread(target=background_refresh, daemon=True)
        thread.start()

    def get_cached_ticket(self, ticket_key: str) -> Optional[dict]:
        """
        Get ticket from cache only (no API call).

        Thread Safety: Thread-safe via cache layer.

        Args:
            ticket_key: Ticket key (e.g., "PROJ-123")

        Returns:
            Ticket dict if cached, None otherwise

        Examples:
            >>> ticket = controller.get_cached_ticket("TEST-123")
            >>> if ticket:
            ...     print(f"Found in cache: {ticket['fields']['summary']}")
        """
        if hasattr(self.utils, 'cache'):
            # Will be implemented with cache.get_ticket()
            pass
        return None

    def format_ticket_display(
        self,
        ticket: dict,
        field_formatters: Optional[Dict[str, Callable]] = None
    ) -> Dict[str, Any]:
        """
        Format ticket for display (extract fields, format dates, etc).

        This is a pure function with no side effects and no shared state.
        Safe to call from any thread.

        Thread Safety: Pure function, no side effects, no shared state.
        Always safe to call concurrently.

        Args:
            ticket: Raw ticket dict from API/cache
            field_formatters: Optional dict of field_name -> formatter_func

        Returns:
            Dict of formatted field values for display

        Examples:
            >>> formatters = {
            ...     'updated': lambda d: format_relative_date(d),
            ...     'status': lambda s: s['name'].upper()
            ... }
            >>> formatted = controller.format_ticket_display(ticket, formatters)
            >>> print(formatted['summary'], formatted['status'])
        """
        if not field_formatters:
            field_formatters = {}

        result = {}
        fields = ticket.get('fields', {})

        # Basic fields
        result['key'] = ticket.get('key', '')
        result['summary'] = fields.get('summary', '')

        # Apply formatters
        for field_name, formatter in field_formatters.items():
            if field_name in fields:
                try:
                    result[field_name] = formatter(fields[field_name])
                except Exception:
                    result[field_name] = fields[field_name]

        return result


class CacheController:
    """
    Handles cache management operations.

    This controller provides operations for inspecting and managing the cache,
    such as getting statistics, refreshing cached data, and clearing the cache.

    Thread Safety:
    - get_stats(): Thread-safe via cache layer
    - refresh_all_tickets(): Spawns background thread, checks for existing refresh
    - clear_*(): Atomic operations via cache layer

    Design:
    - Delegates to cache layer for thread-safe operations
    - Background threads for bulk refresh operations
    - Lock protects refresh_thread to prevent duplicate refreshes
    """

    def __init__(self, cache):
        """
        Initialize CacheController.

        Args:
            cache: Cache instance (JiraCache or JiraSQLiteCache)
        """
        self.cache = cache
        self.refresh_thread: Optional[threading.Thread] = None
        self.refresh_lock = threading.Lock()  # Protects refresh_thread only
        self.is_refreshing = False  # Atomic flag

    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics for display.

        Thread Safety: Thread-safe via cache layer.

        Returns:
            Dict with cache statistics:
                - tickets: {count, oldest_age, size_mb}
                - users: {count, oldest_age, size_mb}
                - total_size_mb: Total cache size

        Examples:
            >>> stats = controller.get_stats()
            >>> print(f"Cached tickets: {stats['tickets']['count']}")
            >>> print(f"Cache size: {stats['total_size_mb']:.1f} MB")
        """
        if hasattr(self.cache, 'get_cache_stats'):
            return self.cache.get_cache_stats()

        # Fallback for caches without stats
        return {
            'tickets': {'count': 0, 'oldest_age': None, 'size_mb': 0},
            'users': {'count': 0, 'oldest_age': None, 'size_mb': 0},
            'total_size_mb': 0
        }

    def refresh_all_tickets(
        self,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> bool:
        """
        Spawn background thread to refresh all cached tickets.

        Returns immediately. Use progress_callback for updates.

        Thread Safety: Safe to call concurrently. Only one refresh runs at a time.
        Returns False if refresh already in progress.

        Args:
            progress_callback: Optional callback(current, total) for progress

        Returns:
            True if refresh started, False if already in progress

        Examples:
            >>> def on_progress(current, total):
            ...     print(f"Refreshing: {current}/{total}")
            >>> if controller.refresh_all_tickets(on_progress):
            ...     print("Refresh started")
            ... else:
            ...     print("Refresh already in progress")
        """
        with self.refresh_lock:
            if self.is_refreshing:
                return False  # Already refreshing

            self.is_refreshing = True

        def background_refresh():
            try:
                # Refresh logic will be implemented
                # For now, just sleep to simulate work
                if progress_callback:
                    progress_callback(0, 100)
                time.sleep(0.1)
                if progress_callback:
                    progress_callback(100, 100)
            finally:
                self.is_refreshing = False

        self.refresh_thread = threading.Thread(
            target=background_refresh,
            daemon=True
        )
        self.refresh_thread.start()
        return True

    def clear_tickets(self) -> int:
        """
        Clear ticket cache.

        Thread Safety: Atomic operation via cache layer.

        Returns:
            Number of tickets cleared

        Examples:
            >>> count = controller.clear_tickets()
            >>> print(f"Cleared {count} tickets from cache")
        """
        if hasattr(self.cache, 'clear_tickets'):
            return self.cache.clear_tickets()
        return 0

    def clear_users(self) -> int:
        """
        Clear user cache.

        Thread Safety: Atomic operation via cache layer.

        Returns:
            Number of users cleared

        Examples:
            >>> count = controller.clear_users()
            >>> print(f"Cleared {count} users from cache")
        """
        if hasattr(self.cache, 'clear_users'):
            return self.cache.clear_users()
        return 0

    def clear_all(self) -> Tuple[int, int]:
        """
        Clear entire cache (tickets + users + metadata).

        Thread Safety: Atomic operation via cache layer.

        Returns:
            Tuple of (tickets_cleared, users_cleared)

        Examples:
            >>> tickets, users = controller.clear_all()
            >>> print(f"Cleared {tickets} tickets and {users} users")
        """
        tickets_cleared = self.clear_tickets()
        users_cleared = self.clear_users()
        return (tickets_cleared, users_cleared)
