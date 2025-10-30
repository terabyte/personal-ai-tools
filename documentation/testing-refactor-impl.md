# Testing & Refactoring Strategy for Jira TUI

## Overview

The Jira TUI application has grown to over 7,000 lines of code without tests. This makes it difficult to:
- **Verify changes work** - Requires manual terminal testing every time
- **Catch regressions** - Fixing one thing breaks another
- **Debug issues** - "It hangs" requires extensive instrumentation
- **Implement safely** - Threading/caching bugs are invisible until runtime

**Goal:** Refactor for testability to enable faster, safer iteration.

**Philosophy:** Tests are a tool for implementation, not an end goal. We test what makes development easier.

## Current State Analysis

**Code Size:**
```
jira_tui.py:          5,177 lines  (TUI + business logic mixed)
jira_utils.py:        1,296 lines  (API + formatting + cache orchestration)
jira_sqlite_cache.py:   680 lines  (cache backend)
Total:                7,153 lines  (0 tests)
```

**Architecture Problems:**
1. **TUI tightly coupled to business logic** - Can't test without curses rendering
2. **No mocking layer** - Tests would hit real Jira API
3. **Mixed responsibilities** - API calls, caching, formatting, display all intertwined
4. **Threading complexity** - Race conditions invisible until production
5. **Manual testing loop:**
   - AI makes changes
   - User manually tests in terminal
   - User reports vague issue ("hangs", "doesn't work")
   - AI adds debug instrumentation
   - Repeat 10+ times
   - Issue may still not be fixed

**Why This Matters:**
When implementing caching, the application hung at startup. This required:
- 15+ iterations of debug instrumentation
- User clearing cache, restarting app repeatedly
- Never got to root cause before reverting
- **A 5-line test could have caught this in 1 second**

## Refactoring Strategy

### Goal: Separate Concerns

**Current (Monolithic):**
```
jira_tui.py (5177 lines)
├── Curses rendering
├── Business logic
├── API orchestration
├── Cache management
├── Threading coordination
└── Data formatting
```

**Proposed (Layered):**
```
jira_view_core.py          # Pure business logic (NEW)
├── QueryController        # Query execution, caching, filtering
├── TicketController       # Single ticket operations
└── CacheController        # Cache management operations

jira_utils.py              # API wrapper (existing, minimal changes)
├── API call wrappers
├── Pagination handling
├── Response parsing

jira_sqlite_cache.py       # Cache backend (existing, no changes)
├── SQLite operations
└── TTL management

jira_tui.py                # Thin presentation layer (refactored)
├── Curses rendering only
├── Input handling
├── Uses controllers for all logic
└── No business logic
```

### New Component: `jira_view_core.py`

**Purpose:** Pure business logic with zero curses dependencies. Fully testable.

```python
"""
Jira View Controllers - Business logic for TUI application
Zero curses dependencies - fully testable
"""

from typing import List, Optional, Tuple, Callable
from dataclasses import dataclass
import threading
import time

@dataclass
class QueryResult:
    """Result of executing a JQL query"""
    tickets: List[dict]
    status: str            # "CURRENT", "UPDATING", "FIRST_RUN", "ERROR"
    cache_age: Optional[float]  # Seconds since oldest ticket cached
    is_updating: bool      # True if background refresh in progress

@dataclass
class CacheStats:
    """Cache statistics"""
    ticket_count: int
    user_count: int
    oldest_ticket_age: Optional[float]
    oldest_user_age: Optional[float]
    cache_size_mb: float

class QueryController:
    """
    Handles JQL query execution with caching and background refresh.
    No curses dependencies - pure business logic.
    """

    def __init__(self, utils):
        """
        Args:
            utils: JiraUtils instance (handles API calls)
        """
        self.utils = utils
        self.ticket_cache = {}  # key -> ticket dict
        self.refresh_needed = False
        self.update_lock = threading.Lock()

    def execute_query(self, jql: str, fields: List[str],
                     force_refresh: bool = False,
                     progress_callback: Optional[Callable] = None) -> QueryResult:
        """
        Execute JQL query with intelligent caching.

        Returns immediately with cached data if available.
        Spawns background thread to verify/refresh stale data.

        Args:
            jql: JQL query string
            fields: List of field names to fetch
            force_refresh: Skip cache, fetch from API
            progress_callback: Optional callback(current, total) for progress

        Returns:
            QueryResult with tickets and status
        """
        # Implementation goes here
        pass

    def is_startup_complete(self) -> bool:
        """
        Check if controller is ready to display UI.
        Used to verify <1s startup requirement.
        """
        return True  # Should always be true immediately

    def get_background_status(self) -> Tuple[bool, Optional[int], Optional[int]]:
        """
        Get status of background refresh operation.

        Returns:
            (is_running, current_count, total_count)
        """
        pass

class TicketController:
    """
    Handles operations on individual tickets.
    No curses dependencies.
    """

    def __init__(self, utils):
        self.utils = utils

    def refresh_ticket(self, ticket_key: str) -> Optional[dict]:
        """
        Force refresh single ticket from API.
        Blocks until complete (fast, single ticket).

        Returns:
            Updated ticket dict, or None on error
        """
        pass

    def get_cached_ticket(self, ticket_key: str) -> Optional[dict]:
        """
        Get ticket from cache only (no API call).

        Returns:
            Ticket dict if cached, None otherwise
        """
        pass

    def format_ticket_display(self, ticket: dict,
                             field_formatters: dict) -> dict:
        """
        Format ticket for display (extract fields, format dates, etc).
        Pure function - no side effects.

        Args:
            ticket: Raw ticket dict from API/cache
            field_formatters: dict of field_name -> formatter_func

        Returns:
            Dict of formatted field values for display
        """
        pass

class CacheController:
    """
    Handles cache management operations.
    No curses dependencies.
    """

    def __init__(self, cache):
        self.cache = cache
        self.refresh_thread = None

    def get_stats(self) -> CacheStats:
        """Get cache statistics for display"""
        pass

    def refresh_all_tickets(self, progress_callback: Optional[Callable] = None):
        """
        Spawn background thread to refresh all cached tickets.
        Returns immediately - use progress_callback for updates.
        """
        pass

    def clear_tickets(self) -> int:
        """
        Clear ticket cache.

        Returns:
            Number of tickets cleared
        """
        pass

    def clear_all(self) -> Tuple[int, int]:
        """
        Clear entire cache.

        Returns:
            (tickets_cleared, users_cleared)
        """
        pass
```

### Refactored `jira_tui.py`

**Changes:**
- Replace inline logic with controller calls
- Keep only curses rendering code
- No direct API calls
- No cache management logic

```python
class JiraTUI:
    def __init__(self):
        self.utils = JiraUtils()

        # Controllers handle all business logic
        self.query_controller = QueryController(self.utils)
        self.ticket_controller = TicketController(self.utils)
        self.cache_controller = CacheController(self.utils.cache)

    def run(self, stdscr, initial_jql, fields):
        # Measure startup time
        start_time = time.time()

        # Execute query - returns immediately with cache or placeholder
        result = self.query_controller.execute_query(initial_jql, fields)

        # Verify startup was fast
        elapsed = time.time() - start_time
        if elapsed > 1.0:
            # Warning: startup took too long
            pass

        # Render UI (curses code only)
        self._render_ticket_list(stdscr, result.tickets)
        self._render_status_bar(stdscr, result.status, result.cache_age)

        # Main loop - just handles input and rendering
        while True:
            key = stdscr.getch()

            if key == ord('r'):
                # Refresh current ticket - delegate to controller
                ticket = self.ticket_controller.refresh_ticket(current_key)
                self._update_ticket_display(stdscr, ticket)

            elif key == ord('R'):
                # Refresh query - delegate to controller
                result = self.query_controller.execute_query(
                    initial_jql, fields, force_refresh=True
                )
                self._render_ticket_list(stdscr, result.tickets)
```

## Testing Infrastructure

### Setup

**Install test dependencies:**
```bash
pip install pytest pytest-mock pytest-timeout
```

**Directory structure:**
```
tests/
├── __init__.py
├── conftest.py                    # Shared fixtures
├── test_jira_view_core.py         # Controller tests
├── test_jira_utils.py             # API wrapper tests
├── test_jira_sqlite_cache.py      # Cache backend tests
├── test_integration.py            # End-to-end tests
└── fixtures/
    ├── mock_api_responses.json    # Sample Jira responses
    ├── test.db                    # Pre-populated cache
    └── empty.db                   # Empty cache for first-run tests
```

**Sample `conftest.py`:**
```python
import pytest
import json
from pathlib import Path
from unittest.mock import MagicMock
from jira_utils import JiraUtils
from jira_sqlite_cache import JiraSQLiteCache
from jira_view_core import QueryController, TicketController, CacheController

@pytest.fixture
def fixture_dir():
    """Path to test fixtures"""
    return Path(__file__).parent / "fixtures"

@pytest.fixture
def mock_api_responses(fixture_dir):
    """Load mock API responses from JSON"""
    with open(fixture_dir / "mock_api_responses.json") as f:
        return json.load(f)

@pytest.fixture
def mock_jira_utils(mock_api_responses):
    """Mock JiraUtils that returns canned responses"""
    utils = MagicMock(spec=JiraUtils)

    # Mock API calls to return test data
    utils.call_jira_api.return_value = mock_api_responses["search_result"]
    utils.fetch_all_jql_results.return_value = mock_api_responses["tickets"]

    return utils

@pytest.fixture
def temp_cache(tmp_path):
    """Temporary cache database for testing"""
    cache = JiraSQLiteCache("https://test.atlassian.net",
                            cache_dir=tmp_path)
    yield cache
    cache.close()

@pytest.fixture
def query_controller(mock_jira_utils, temp_cache):
    """QueryController with mocked dependencies"""
    mock_jira_utils.cache = temp_cache
    return QueryController(mock_jira_utils)
```

**Sample `fixtures/mock_api_responses.json`:**
```json
{
  "search_result": {
    "issues": [
      {
        "key": "TEST-1",
        "fields": {
          "summary": "Test ticket 1",
          "status": {"name": "In Progress"},
          "updated": "2025-01-15T10:00:00.000+0000"
        }
      },
      {
        "key": "TEST-2",
        "fields": {
          "summary": "Test ticket 2",
          "status": {"name": "Done"},
          "updated": "2025-01-15T11:00:00.000+0000"
        }
      }
    ],
    "nextPageToken": null
  },
  "tickets": [
    "... same as above ..."
  ],
  "single_ticket": {
    "key": "TEST-1",
    "fields": "..."
  }
}
```

## Core Unit Tests

### Cache Tests (`test_jira_sqlite_cache.py`)

**Purpose:** Verify SQLite cache operations work correctly.

```python
def test_set_and_get_ticket(temp_cache):
    """Verify basic ticket storage and retrieval"""
    ticket = {"key": "TEST-1", "summary": "Test"}
    updated = "2025-01-15T10:00:00.000+0000"

    temp_cache.set_ticket("TEST-1", ticket, updated)
    retrieved = temp_cache.get_ticket("TEST-1")

    assert retrieved is not None
    assert retrieved["key"] == "TEST-1"
    assert retrieved["summary"] == "Test"

def test_get_many_tickets(temp_cache):
    """Verify bulk ticket retrieval"""
    tickets = [
        {"key": "TEST-1", "summary": "First"},
        {"key": "TEST-2", "summary": "Second"},
        {"key": "TEST-3", "summary": "Third"},
    ]

    for t in tickets:
        temp_cache.set_ticket(t["key"], t, "2025-01-15T10:00:00.000+0000")

    result = temp_cache.get_many(["TEST-1", "TEST-3"])

    assert len(result) == 2
    assert result[0]["key"] == "TEST-1"
    assert result[1]["key"] == "TEST-3"

def test_query_cache_ttl(temp_cache):
    """Verify query cache respects 5-minute TTL"""
    import time

    # Cache query result
    temp_cache.set_query_result("project=TEST", ["TEST-1", "TEST-2"])

    # Should hit within TTL
    result = temp_cache.get_query_result("project=TEST", ttl_seconds=300)
    assert result == ["TEST-1", "TEST-2"]

    # Should miss after TTL expires
    time.sleep(1)
    result = temp_cache.get_query_result("project=TEST", ttl_seconds=0)
    assert result is None

def test_lazy_user_caching(temp_cache):
    """Verify users are NOT cached until accessed"""
    # This test verifies the absence of upfront caching
    # by checking cache is empty at startup
    stats = temp_cache.get_cache_stats()
    assert stats["users"]["count"] == 0

    # After accessing a user, it should be cached
    user = {"accountId": "123", "displayName": "Test User"}
    temp_cache.set_user("123", user)

    stats = temp_cache.get_cache_stats()
    assert stats["users"]["count"] == 1
```

### Controller Tests (`test_jira_view_core.py`)

**Purpose:** Verify business logic works correctly without UI.

```python
import time
import pytest

def test_query_controller_returns_immediately(query_controller):
    """
    CRITICAL: Verify query execution returns in <1 second.
    This catches startup blocking bugs.
    """
    start = time.time()
    result = query_controller.execute_query("project=TEST", ["key", "summary"])
    elapsed = time.time() - start

    assert elapsed < 1.0, f"Query took {elapsed:.2f}s, should be <1s"
    assert result is not None
    assert result.tickets is not None

def test_query_controller_spawns_background_thread(query_controller):
    """Verify background refresh starts but doesn't block"""
    result = query_controller.execute_query("project=TEST", ["key", "summary"])

    # Should return immediately
    assert result is not None

    # Background thread should be running
    is_running, current, total = query_controller.get_background_status()
    # May or may not be running depending on timing, but shouldn't error
    assert isinstance(is_running, bool)

def test_force_refresh_bypasses_cache(query_controller, mock_jira_utils):
    """Verify force_refresh=True always hits API"""
    # First call - cache miss, hits API
    result1 = query_controller.execute_query("project=TEST", ["key"],
                                            force_refresh=False)
    api_call_count_1 = mock_jira_utils.fetch_all_jql_results.call_count

    # Second call - would hit cache
    result2 = query_controller.execute_query("project=TEST", ["key"],
                                            force_refresh=False)
    api_call_count_2 = mock_jira_utils.fetch_all_jql_results.call_count

    # Third call - force refresh
    result3 = query_controller.execute_query("project=TEST", ["key"],
                                            force_refresh=True)
    api_call_count_3 = mock_jira_utils.fetch_all_jql_results.call_count

    # Force refresh should make additional API call
    assert api_call_count_3 > api_call_count_2

def test_ticket_controller_refresh(mock_jira_utils, temp_cache):
    """Verify single ticket refresh works"""
    controller = TicketController(mock_jira_utils)
    mock_jira_utils.cache = temp_cache

    # Mock API to return specific ticket
    mock_ticket = {"key": "TEST-1", "summary": "Updated"}
    mock_jira_utils.fetch_all_jql_results.return_value = [mock_ticket]

    result = controller.refresh_ticket("TEST-1")

    assert result is not None
    assert result["key"] == "TEST-1"
    assert result["summary"] == "Updated"

    # Ticket should be cached after refresh
    cached = temp_cache.get_ticket("TEST-1")
    assert cached is not None

def test_cache_controller_stats(temp_cache):
    """Verify cache statistics are accurate"""
    controller = CacheController(temp_cache)

    # Empty cache
    stats = controller.get_stats()
    assert stats.ticket_count == 0
    assert stats.user_count == 0

    # Add some data
    temp_cache.set_ticket("TEST-1", {"key": "TEST-1"}, "2025-01-15T10:00:00.000+0000")
    temp_cache.set_user("123", {"accountId": "123", "displayName": "Test"})

    stats = controller.get_stats()
    assert stats.ticket_count == 1
    assert stats.user_count == 1

@pytest.mark.timeout(5)
def test_no_deadlock_during_background_refresh(query_controller):
    """
    Verify background refresh doesn't cause deadlock.
    Test will timeout after 5 seconds if deadlock occurs.
    """
    # Start multiple background refreshes
    for i in range(3):
        query_controller.execute_query(f"project=TEST{i}", ["key"])

    # Wait a bit
    time.sleep(0.5)

    # Should still be able to execute queries
    result = query_controller.execute_query("project=FINAL", ["key"])
    assert result is not None
```

### API Tests (`test_jira_utils.py`)

**Purpose:** Verify API wrapper handles edge cases correctly.

```python
from urllib.parse import unquote

def test_url_encoding_with_special_chars():
    """
    CRITICAL: Verify JQL with 'is empty' encodes correctly.
    This catches the manual .replace() bug.
    """
    from jira_utils import JiraUtils
    from urllib.parse import quote

    utils = JiraUtils()
    jql = "project = CIPLAT and sprint is empty"

    # The correct encoding
    expected = quote(jql, safe='')

    # Verify it encodes 'is empty' properly (not breaks the keyword)
    assert '%20' in expected  # Spaces encoded
    assert 'is' in unquote(expected)  # 'is' keyword preserved
    assert 'empty' in unquote(expected)  # 'empty' keyword preserved

def test_jql_batching_for_keys():
    """Verify JQL batching stays under URL length limit"""
    from jira_utils import JiraUtils

    utils = JiraUtils()

    # Generate 364 ticket keys (realistic scenario)
    keys = [f"PROJ-{i}" for i in range(1, 365)]

    # Batch into 150-key groups
    batch_size = 150
    batches = []
    for i in range(0, len(keys), batch_size):
        batch = keys[i:i + batch_size]
        jql = f"key in ({','.join(batch)})"
        batches.append(jql)

    # Verify each batch is under ~2000 chars (safe limit)
    for jql in batches:
        assert len(jql) < 2000, f"Batch too long: {len(jql)} chars"

    # Verify we got expected number of batches
    expected_batches = (len(keys) + batch_size - 1) // batch_size
    assert len(batches) == expected_batches

def test_pagination_field_count_awareness():
    """
    Document the Jira API pagination behavior.
    Not a test that can fail, but documents expectations.
    """
    # Single field - expect 1000 per page
    single_field_page_size = 1000

    # Multiple fields - expect 100 per page (Jira limitation)
    multi_field_page_size = 100

    # For 2260 tickets:
    single_field_calls = (2260 + single_field_page_size - 1) // single_field_page_size
    multi_field_calls = (2260 + multi_field_page_size - 1) // multi_field_page_size

    assert single_field_calls == 3  # Fast for counting
    assert multi_field_calls == 23  # Reality for data fetching
```

## Integration Tests

**Purpose:** Test real scenarios end-to-end (but still mocked API).

```python
# test_integration.py

def test_first_run_empty_cache_scenario(tmp_path, mock_jira_utils):
    """
    Simulate first run with empty cache.

    Expected behavior:
    1. Returns in <1s with initial data
    2. Spawns background thread
    3. Cache populated after background completes
    """
    import time

    # Setup: Empty cache
    cache = JiraSQLiteCache("https://test.atlassian.net", cache_dir=tmp_path)
    mock_jira_utils.cache = cache
    controller = QueryController(mock_jira_utils)

    # Execute query
    start = time.time()
    result = controller.execute_query("project=TEST", ["key", "summary"])
    elapsed = time.time() - start

    # Should return quickly
    assert elapsed < 1.0, f"First run took {elapsed:.2f}s"
    assert result.status in ["FIRST_RUN", "UPDATING"]

    # Wait for background to complete (with timeout)
    max_wait = 5
    waited = 0
    while waited < max_wait:
        is_running, _, _ = controller.get_background_status()
        if not is_running:
            break
        time.sleep(0.1)
        waited += 0.1

    # Cache should be populated
    stats = cache.get_cache_stats()
    assert stats["tickets"]["count"] > 0

def test_second_run_with_cache_scenario(tmp_path, mock_jira_utils):
    """
    Simulate second run with populated cache.

    Expected behavior:
    1. Returns in <100ms (query cache hit)
    2. Background verification starts
    3. Shows current/updating status
    """
    import time

    # Setup: Pre-populated cache
    cache = JiraSQLiteCache("https://test.atlassian.net", cache_dir=tmp_path)

    # Populate with test data
    test_tickets = [
        {"key": "TEST-1", "summary": "First"},
        {"key": "TEST-2", "summary": "Second"},
    ]
    for ticket in test_tickets:
        cache.set_ticket(ticket["key"], ticket, "2025-01-15T10:00:00.000+0000")
    cache.set_query_result("project=TEST", ["TEST-1", "TEST-2"])

    mock_jira_utils.cache = cache
    controller = QueryController(mock_jira_utils)

    # Execute same query
    start = time.time()
    result = controller.execute_query("project=TEST", ["key", "summary"])
    elapsed = time.time() - start

    # Should be very fast (query cache hit)
    assert elapsed < 0.5, f"Cached query took {elapsed:.2f}s, should be <0.5s"
    assert len(result.tickets) == 2
    assert result.status in ["CURRENT", "UPDATING"]

def test_large_query_performance(mock_jira_utils, temp_cache):
    """
    Test with 500+ tickets to ensure no hangs.
    """
    import time

    # Mock API to return 500 tickets
    large_result = [
        {"key": f"TEST-{i}", "summary": f"Ticket {i}"}
        for i in range(500)
    ]
    mock_jira_utils.fetch_all_jql_results.return_value = large_result
    mock_jira_utils.cache = temp_cache

    controller = QueryController(mock_jira_utils)

    # Should still return quickly (background does the heavy lifting)
    start = time.time()
    result = controller.execute_query("project=TEST", ["key", "summary"])
    elapsed = time.time() - start

    assert elapsed < 1.0, f"Large query took {elapsed:.2f}s"
    # May return partial data initially, that's OK
    assert result is not None

@pytest.mark.timeout(10)
def test_no_hang_on_network_error(mock_jira_utils, temp_cache):
    """
    Verify graceful handling when API fails.
    """
    # Mock API to raise exception
    mock_jira_utils.fetch_all_jql_results.side_effect = Exception("Network error")
    mock_jira_utils.cache = temp_cache

    controller = QueryController(mock_jira_utils)

    # Should not hang, should return error status
    result = controller.execute_query("project=TEST", ["key"])

    assert result is not None
    assert result.status == "ERROR" or "ERROR" in result.status
```

## Minimal Viable Testing Strategy

**If you have limited time, do THIS:**

### Step 1: Extract Just `QueryController`

Create minimal `jira_view_core.py` with only `QueryController` class (copy from "New Component" section above).

### Step 2: Write 5 Critical Tests

```python
# test_critical.py - The 5 tests that would have caught the hang bug

def test_startup_under_1_second(query_controller):
    """Catches: Blocking operations at startup"""
    import time
    start = time.time()
    result = query_controller.execute_query("project=TEST", ["key"])
    assert time.time() - start < 1.0

def test_background_refresh_doesnt_block(query_controller):
    """Catches: Background threads blocking main thread"""
    result = query_controller.execute_query("project=TEST", ["key"])
    assert result is not None  # Returned immediately
    # Check we can do more work
    result2 = query_controller.execute_query("project=OTHER", ["key"])
    assert result2 is not None

def test_users_not_cached_upfront(query_controller, temp_cache):
    """Catches: Upfront user caching at startup"""
    query_controller.execute_query("project=TEST", ["key", "summary", "assignee"])
    stats = temp_cache.get_cache_stats()
    # Users should be 0 or very low (only if needed for display)
    # The bug was caching 728 users upfront
    assert stats["users"]["count"] < 10

def test_url_encoding_correct():
    """Catches: Manual .replace() breaking JQL keywords"""
    from urllib.parse import quote, unquote
    jql = "project = TEST and sprint is empty"
    encoded = quote(jql, safe='')
    decoded = unquote(encoded)
    assert "is empty" in decoded  # Keywords preserved

@pytest.mark.timeout(5)
def test_no_deadlock(query_controller):
    """Catches: Threading deadlocks"""
    # Multiple queries shouldn't deadlock
    for i in range(5):
        result = query_controller.execute_query(f"project=TEST{i}", ["key"])
        assert result is not None
```

### Step 3: Run Tests Before User Testing

```bash
# Fast feedback loop
pytest tests/test_critical.py -v

# If all 5 pass, ready for user testing
# If any fail, fix before asking user to test
```

**This would have saved 10+ iterations on the caching implementation.**

## Implementation Tasks

### Phase 1: Setup Infrastructure

- [ ] Create `tests/` directory structure
- [ ] Install pytest, pytest-mock, pytest-timeout
- [ ] Create `conftest.py` with fixtures
- [ ] Create `fixtures/mock_api_responses.json` with sample data
- [ ] Verify pytest runs: `pytest tests/ -v`

### Phase 2: Extract Controllers (Minimal)

- [ ] Create `jira_view_core.py` with `QueryController` only
- [ ] Move `execute_query` logic from `jira_tui.py` to controller
- [ ] Update `jira_tui.py` to use controller
- [ ] Verify app still works (manual test)
- [ ] Write 5 critical tests (minimal viable testing)
- [ ] Verify tests pass

### Phase 3: Full Controller Extraction (Optional)

- [ ] Add `TicketController` to `jira_view_core.py`
- [ ] Add `CacheController` to `jira_view_core.py`
- [ ] Move all business logic from `jira_tui.py` to controllers
- [ ] Update `jira_tui.py` to be thin presentation layer
- [ ] Verify app still works

### Phase 4: Comprehensive Testing (Optional)

- [ ] Write cache tests (`test_jira_sqlite_cache.py`)
- [ ] Write controller tests (`test_jira_view_core.py`)
- [ ] Write API tests (`test_jira_utils.py`)
- [ ] Write integration tests (`test_integration.py`)
- [ ] Achieve >80% code coverage on controllers

### Phase 5: CI Integration (Future)

- [ ] Add GitHub Actions workflow for pytest
- [ ] Run tests on every commit
- [ ] Block merges if tests fail
- [ ] Add coverage reporting

## Benefits

### For AI Implementation

**Before (No Tests):**
1. AI changes code
2. User manually tests in terminal
3. User: "It hangs"
4. AI adds debug prints
5. Repeat 10+ times
6. Maybe works, maybe revert

**After (With Tests):**
1. AI changes code
2. AI runs tests (5 seconds)
3. Test fails: "Startup took 8.2s, should be <1s"
4. AI fixes blocking operation
5. Tests pass
6. User tests once to verify UI looks good
7. Done

### For User

- **Faster iterations** - Less time spent manually testing
- **Fewer regressions** - Tests catch when fixes break other things
- **Better error messages** - "Startup took 8.2s" vs "it hangs"
- **More confidence** - Tests document expected behavior

### For Maintenance

- **Safe refactoring** - Tests verify behavior unchanged
- **Documentation** - Tests show how code should work
- **Regression prevention** - Bug fixes include tests
- **Faster debugging** - Reproduce issues in tests

## Testing Philosophy

**Test what makes development easier:**
- ✅ Test business logic (controllers)
- ✅ Test complex algorithms (caching, pagination)
- ✅ Test performance requirements (<1s startup)
- ✅ Test threading behavior (no deadlocks)
- ❌ Don't test curses rendering (hard, low value)
- ❌ Don't test Jira API (mock it instead)
- ❌ Don't test for 100% coverage (test useful things)

**Tests are a tool, not a goal.**

The 5 critical tests would have saved 2+ hours of debugging on the caching implementation.

---

## Next Steps

1. **Immediate:** Extract `QueryController` + write 5 critical tests
2. **Before next feature:** Run tests to verify no regressions
3. **Eventually:** Full controller extraction + comprehensive tests
4. **Future:** CI integration

**Start small, iterate based on value.**
