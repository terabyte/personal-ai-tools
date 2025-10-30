# Testing & Refactoring Strategy for Jira TUI (Concurrency-Focused)

## Overview

The Jira TUI application has grown to over 6,000 lines of code without tests. This makes it difficult to:
- **Verify changes work** - Requires manual terminal testing every time
- **Catch regressions** - Fixing one thing breaks another
- **Debug issues** - "It hangs" requires extensive instrumentation
- **Implement safely** - Threading/caching bugs are invisible until runtime
- **Prevent concurrency bugs** - Race conditions, deadlocks only manifest in production

**Goal:** Refactor for testability to enable faster, safer iteration, WITH STRONG EMPHASIS ON THREADING CORRECTNESS.

**Philosophy:** Tests are a tool for implementation, not an end goal. We test what makes development easier. Given the history of concurrency bugs, threading tests are first-class citizens, not afterthoughts.

## Current State Analysis

**Code Size:**
```
jira_tui.py:          4,903 lines  (TUI + business logic mixed)
jira-view:              730 lines  (entry point/wrapper)
jira_utils.py:          960 lines  (API + formatting)
jira_cache.py:          228 lines  (simple JSON cache)
Total:                6,821 lines  (0 tests)
```

**Architecture Problems:**
1. **TUI tightly coupled to business logic** - Can't test without curses rendering
2. **No mocking layer** - Tests would hit real Jira API
3. **Mixed responsibilities** - API calls, caching, formatting, display all intertwined
4. **Threading complexity** - Race conditions invisible until production
5. **Known concurrency bugs** - Multiple hangs, race conditions, deadlocks in history
6. **Future concurrency needs** - Heavy use of background threads for caching planned
7. **Manual testing loop:**
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
- **A threading test would have caught the lock-during-I/O bug immediately**

## Refactoring Strategy

### Goal: Separate Concerns with Thread-Safety First

**Current (Monolithic):**
```
jira_tui.py (4903 lines)
├── Curses rendering
├── Business logic
├── API orchestration
├── Cache management
├── Threading coordination (UNTESTED, BUGGY)
└── Data formatting
```

**Proposed (Layered with Thread-Safety):**
```
jira_view_core.py          # Pure business logic (NEW)
├── QueryController        # Query execution, caching, filtering (THREAD-SAFE)
├── TicketController       # Single ticket operations (THREAD-SAFE)
└── CacheController        # Cache management operations (THREAD-SAFE)

jira_utils.py              # API wrapper (existing, minimal changes)
├── API call wrappers
├── Pagination handling
├── Response parsing

jira_cache.py              # Cache backend (existing)
├── File-based JSON cache
└── TTL management

jira_tui.py                # Thin presentation layer (refactored)
├── Curses rendering only
├── Input handling
├── Uses controllers for all logic
└── No business logic
└── Thread-safe screen updates
```

### New Component: `jira_view_core.py`

**Purpose:** Pure business logic with zero curses dependencies. Fully testable. Thread-safe by design.

**Threading Design Principles:**
1. **Never hold locks during I/O** - locks only for memory updates
2. **Atomic flags for communication** - simple booleans, no complex state
3. **Minimal shared state** - prefer immutable data structures
4. **Single lock per controller** - avoid lock ordering issues
5. **Daemon threads** - clean shutdown
6. **Document thread safety** - every method labeled

```python
"""
Jira View Controllers - Business logic for TUI application
Zero curses dependencies - fully testable
Thread-safe by design - all methods documented for concurrency safety
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

class QueryController:
    """
    Handles JQL query execution with caching and background refresh.

    Thread Safety:
    - execute_query(): Thread-safe, returns immediately
    - get_background_status(): Thread-safe, lock-free reads
    - is_startup_complete(): Thread-safe, always true
    - Internal lock protects ticket_cache dict updates only
    - Never holds lock during network I/O
    """

    def __init__(self, utils):
        """
        Args:
            utils: JiraUtils instance (handles API calls)
        """
        self.utils = utils
        self.ticket_cache = {}  # key -> ticket dict
        self.refresh_needed = False
        self.update_lock = threading.Lock()  # Protects ticket_cache only
        self._background_thread = None

    def execute_query(self, jql: str, fields: List[str],
                     force_refresh: bool = False,
                     progress_callback: Optional[Callable] = None) -> QueryResult:
        """
        Execute JQL query with intelligent caching.

        Returns immediately with cached data if available.
        Spawns background thread to verify/refresh stale data.

        Thread Safety: Safe to call from multiple threads. Never blocks on I/O.

        Args:
            jql: JQL query string
            fields: List of field names to fetch
            force_refresh: Skip cache, fetch from API
            progress_callback: Optional callback(current, total) for progress

        Returns:
            QueryResult with tickets and status
        """
        # Implementation
        pass

    def get_background_status(self) -> Tuple[bool, Optional[int], Optional[int]]:
        """
        Get status of background refresh operation.

        Thread Safety: Lock-free read of atomic flags.

        Returns:
            (is_running, current_count, total_count)
        """
        pass

    def is_startup_complete(self) -> bool:
        """
        Check if controller is ready to display UI.

        Thread Safety: Always safe, no shared state.

        Returns:
            Always True (controller never blocks startup)
        """
        return True

class TicketController:
    """
    Handles operations on individual tickets.

    Thread Safety:
    - refresh_ticket(): Spawns background thread, safe to call concurrently
    - get_cached_ticket(): Thread-safe via cache layer
    - format_ticket_display(): Pure function, no shared state
    """

    def __init__(self, utils):
        self.utils = utils

    def refresh_ticket(self, ticket_key: str,
                      callback: Optional[Callable] = None) -> None:
        """
        Force refresh single ticket from API in background.

        Thread Safety: Safe to call for same or different tickets concurrently.

        Args:
            ticket_key: Ticket key (e.g., "PROJ-123")
            callback: Called when refresh complete with ticket dict
        """
        pass

    def get_cached_ticket(self, ticket_key: str) -> Optional[dict]:
        """
        Get ticket from cache only (no API call).

        Thread Safety: Thread-safe via cache layer.

        Returns:
            Ticket dict if cached, None otherwise
        """
        pass

    def format_ticket_display(self, ticket: dict,
                             field_formatters: dict) -> dict:
        """
        Format ticket for display (extract fields, format dates, etc).

        Thread Safety: Pure function, no side effects, no shared state.

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

    Thread Safety:
    - get_stats(): Thread-safe via cache layer
    - refresh_all_tickets(): Spawns background thread
    - clear_*(): Atomic operations via cache layer
    """

    def __init__(self, cache):
        self.cache = cache
        self.refresh_thread = None
        self.refresh_lock = threading.Lock()

    def get_stats(self) -> dict:
        """
        Get cache statistics for display.

        Thread Safety: Thread-safe via cache layer.
        """
        pass

    def refresh_all_tickets(self, progress_callback: Optional[Callable] = None):
        """
        Spawn background thread to refresh all cached tickets.

        Thread Safety: Safe to call concurrently (checks for existing refresh).
        """
        pass

    def clear_tickets(self) -> int:
        """
        Clear ticket cache.

        Thread Safety: Atomic operation via cache layer.

        Returns:
            Number of tickets cleared
        """
        pass
```

### Refactored `jira_tui.py`

**Changes:**
- Replace inline logic with controller calls
- Keep only curses rendering code
- No direct API calls
- No cache management logic
- Thread-safe screen updates (noutrefresh + doupdate)

```python
class JiraTUI:
    def __init__(self):
        self.utils = JiraUtils()

        # Controllers handle all business logic (thread-safe)
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
            # Log warning: startup took too long
            pass

        # Render UI (curses code only, thread-safe updates)
        self._render_ticket_list(stdscr, result.tickets)
        self._render_status_bar(stdscr, result.status, result.cache_age)

        # Main loop - just handles input and rendering
        while True:
            key = stdscr.getch()

            if key == ord('r'):
                # Refresh current ticket - delegate to controller
                self.ticket_controller.refresh_ticket(
                    current_key,
                    callback=lambda t: self._update_ticket_display(stdscr, t)
                )

            elif key == ord('R'):
                # Refresh query - delegate to controller
                result = self.query_controller.execute_query(
                    initial_jql, fields, force_refresh=True
                )
                self._render_ticket_list(stdscr, result.tickets)
```

## Testing Infrastructure

### Directory Structure

```
tests/
├── __init__.py
├── conftest.py                    # Shared fixtures
├── test_jira_view_core.py         # Controller tests (NEW)
├── test_threading.py              # Dedicated concurrency tests (NEW)
├── test_jira_utils.py             # API wrapper tests
├── test_jira_cache.py             # Cache tests
├── test_integration.py            # End-to-end tests
└── fixtures/
    ├── mock_api_responses.json    # Sample Jira responses
    └── sample_tickets.json        # Test ticket data
```

### Test Dependencies

```bash
pip install pytest pytest-mock pytest-timeout pytest-cov hypothesis
```

- **pytest**: Test runner
- **pytest-mock**: Mocking utilities
- **pytest-timeout**: Prevent hanging tests (CRITICAL for threading)
- **pytest-cov**: Coverage reporting
- **hypothesis**: Property-based testing for concurrency

### conftest.py - Shared Fixtures

```python
import pytest
import json
import time
from pathlib import Path
from unittest.mock import MagicMock
from jira_utils import JiraUtils
from jira_cache import JiraCache
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
    utils.call_jira_api.return_value = mock_api_responses["search_result"]
    utils.fetch_all_jql_results.return_value = mock_api_responses["tickets"]
    return utils

@pytest.fixture
def slow_mock_jira_utils(mock_api_responses):
    """Mock JiraUtils with slow API calls (for timeout/threading tests)"""
    utils = MagicMock(spec=JiraUtils)

    def slow_fetch(*args, **kwargs):
        time.sleep(0.5)
        return mock_api_responses["tickets"]

    utils.fetch_all_jql_results.side_effect = slow_fetch
    return utils

@pytest.fixture
def temp_cache(tmp_path):
    """Temporary cache for testing"""
    cache = JiraCache("https://test.atlassian.net", cache_dir=tmp_path)
    yield cache
    # Cleanup handled by tmp_path

@pytest.fixture
def query_controller(mock_jira_utils, temp_cache):
    """QueryController with mocked dependencies"""
    mock_jira_utils.cache = temp_cache
    return QueryController(mock_jira_utils)

@pytest.fixture
def ticket_controller(mock_jira_utils, temp_cache):
    """TicketController with mocked dependencies"""
    mock_jira_utils.cache = temp_cache
    return TicketController(mock_jira_utils)

@pytest.fixture
def cache_controller(temp_cache):
    """CacheController with mocked dependencies"""
    return CacheController(temp_cache)

@pytest.fixture
def thread_error_collector():
    """Collect errors from background threads"""
    errors = []

    def collect(error):
        errors.append(error)

    yield collect, errors

    # Assert no errors collected
    if errors:
        pytest.fail(f"Background thread errors: {errors}")
```

## Critical Tests (7 Tests - Concurrency Emphasized)

### Original 5 Critical Tests

**test_startup_under_1_second**
```python
def test_startup_under_1_second(query_controller):
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
```

**test_background_refresh_doesnt_block**
```python
def test_background_refresh_doesnt_block(query_controller):
    """
    CRITICAL: Verify background refresh starts but doesn't block.
    """
    result = query_controller.execute_query("project=TEST", ["key", "summary"])

    # Should return immediately
    assert result is not None

    # Should be able to do more work
    result2 = query_controller.execute_query("project=OTHER", ["key"])
    assert result2 is not None
```

**test_no_upfront_user_caching**
```python
def test_no_upfront_user_caching(query_controller, temp_cache):
    """
    CRITICAL: Verify users are NOT cached upfront at startup.
    The bug was caching 728 users upfront, blocking startup.
    """
    query_controller.execute_query("project=TEST", ["key", "summary", "assignee"])
    stats = temp_cache.get_cache_stats()
    # Users should be 0 or very low (lazy caching)
    assert stats.get("users", {}).get("count", 0) < 10
```

**test_url_encoding_correct**
```python
def test_url_encoding_correct():
    """
    CRITICAL: Verify URL encoding doesn't break JQL keywords.
    Manual .replace() breaks keywords like "is empty".
    """
    from urllib.parse import quote, unquote
    jql = "project = TEST and sprint is empty"
    encoded = quote(jql, safe='')
    decoded = unquote(encoded)
    assert "is empty" in decoded  # Keywords preserved
```

**test_no_deadlock_timeout**
```python
@pytest.mark.timeout(5)
def test_no_deadlock_timeout(query_controller):
    """
    CRITICAL: Verify no threading deadlocks.
    Test will timeout after 5 seconds if deadlock occurs.
    """
    # Multiple queries shouldn't deadlock
    for i in range(5):
        result = query_controller.execute_query(f"project=TEST{i}", ["key"])
        assert result is not None
```

### NEW: 2 Additional Critical Concurrency Tests

**test_concurrent_query_execution**
```python
def test_concurrent_query_execution(query_controller):
    """
    CRITICAL: Execute 10 queries concurrently without race conditions.
    """
    results = []
    errors = []

    def run_query(jql):
        try:
            result = query_controller.execute_query(jql, ["key"])
            results.append(result)
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=run_query, args=(f"project=TEST{i}",))
        for i in range(10)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(errors) == 0, f"Concurrent queries failed: {errors}"
    assert len(results) == 10, f"Expected 10 results, got {len(results)}"
```

**test_no_lock_held_during_network_io**
```python
@pytest.mark.timeout(3)
def test_no_lock_held_during_network_io(query_controller, slow_mock_jira_utils):
    """
    CRITICAL: Verify locks are released during slow network operations.
    If locks are held during I/O, second query will block.
    """
    # Replace with slow mock
    query_controller.utils = slow_mock_jira_utils

    # Start slow query in background
    t1 = threading.Thread(
        target=lambda: query_controller.execute_query("project=TEST1", ["key"])
    )
    t1.start()

    # Let first query start
    time.sleep(0.1)

    # Second query should not wait for first query's I/O
    start = time.time()
    result = query_controller.execute_query("project=TEST2", ["key"])
    elapsed = time.time() - start

    assert elapsed < 0.3, f"Query blocked for {elapsed}s (lock held during I/O?)"
    assert result is not None

    t1.join(timeout=2)
```

## Dedicated Threading Tests (test_threading.py)

### Race Condition Tests

**test_shared_state_corruption**
```python
def test_shared_state_corruption(query_controller):
    """
    Hammer controller with concurrent operations to detect corruption.
    """
    def worker():
        for _ in range(20):
            query_controller.execute_query("project=TEST", ["key"])
            query_controller.get_background_status()
            query_controller.is_startup_complete()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # Should not crash or corrupt state
    result = query_controller.execute_query("project=FINAL", ["key"])
    assert result is not None
```

**test_cache_update_during_read**
```python
def test_cache_update_during_read(query_controller):
    """
    Verify reading cache while background thread updates it.
    """
    # Start background refresh
    query_controller.execute_query("project=TEST", ["key"])

    # Immediately start reading repeatedly
    read_errors = []
    def continuous_reader():
        for _ in range(100):
            try:
                result = query_controller.execute_query("project=TEST", ["key"])
                assert result is not None
            except Exception as e:
                read_errors.append(e)
            time.sleep(0.01)

    reader_thread = threading.Thread(target=continuous_reader)
    reader_thread.start()
    reader_thread.join(timeout=5)

    assert len(read_errors) == 0, f"Reads failed during updates: {read_errors}"
```

### SQLite Concurrency Tests

**test_sqlite_concurrent_access**
```python
def test_sqlite_concurrent_access(temp_cache):
    """
    Verify SQLite cache handles concurrent reads/writes.
    """
    def writer(ticket_id):
        for i in range(10):
            temp_cache.set_ticket(
                f"TEST-{ticket_id}-{i}",
                {"key": f"TEST-{ticket_id}-{i}"},
                "2025-01-15T10:00:00.000+0000"
            )

    def reader():
        for _ in range(50):
            temp_cache.get_ticket("TEST-1-5")

    threads = []
    # Multiple writers
    for i in range(3):
        threads.append(threading.Thread(target=writer, args=(i,)))
    # Multiple readers
    for _ in range(3):
        threads.append(threading.Thread(target=reader))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # Verify data integrity
    ticket = temp_cache.get_ticket("TEST-1-5")
    assert ticket is not None
```

### Stress Tests (Optional)

**test_many_concurrent_queries**
```python
@pytest.mark.stress
def test_many_concurrent_queries(query_controller):
    """
    100 concurrent queries - stress test for scalability.
    Run with: pytest -m stress
    """
    results = []

    def worker(i):
        result = query_controller.execute_query(f"project=T{i}", ["key"])
        results.append(result)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 100
```

**test_rapid_refresh_cycles**
```python
@pytest.mark.stress
def test_rapid_refresh_cycles(ticket_controller):
    """
    Rapid ticket refresh in parallel.
    """
    def rapid_refresh():
        for _ in range(50):
            ticket_controller.refresh_ticket("TEST-1")

    threads = [threading.Thread(target=rapid_refresh) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
```

**test_cache_clear_during_operations**
```python
@pytest.mark.stress
def test_cache_clear_during_operations(cache_controller, query_controller):
    """
    Clear cache while queries are running.
    """
    stop_flag = threading.Event()

    def continuous_queries():
        while not stop_flag.is_set():
            query_controller.execute_query("project=TEST", ["key"])
            time.sleep(0.1)

    query_thread = threading.Thread(target=continuous_queries)
    query_thread.start()

    # Clear cache repeatedly
    for _ in range(10):
        time.sleep(0.2)
        cache_controller.clear_tickets()

    stop_flag.set()
    query_thread.join(timeout=5)
```

### Property-Based Testing (Optional)

```python
from hypothesis import given, strategies as st

@given(st.lists(st.text(min_size=1, max_size=20), min_size=0, max_size=100))
def test_concurrent_cache_operations_any_order(temp_cache, ticket_keys):
    """
    Property-based test: cache operations in random order.
    Hypothesis will try many random combinations.
    """
    def random_operations(keys):
        for key in keys:
            temp_cache.set_ticket(key, {"key": key}, "2025-01-15T10:00:00.000+0000")
            temp_cache.get_ticket(key)

    threads = [
        threading.Thread(target=random_operations, args=(ticket_keys[:50],)),
        threading.Thread(target=random_operations, args=(ticket_keys[50:],))
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # Cache should be in valid state
    stats = temp_cache.get_cache_stats()
    assert stats is not None
```

## Implementation Tasks

### Week 1: Foundation + Critical Concurrency

**Day 1: Test Infrastructure**
- [ ] Create tests/ directory structure
- [ ] Create conftest.py with fixtures
- [ ] Create fixtures/mock_api_responses.json
- [ ] Install test dependencies
- [ ] Verify pytest runs: `pytest tests/ -v`

**Day 2: Extract QueryController**
- [ ] Create jira_view_core.py
- [ ] Implement QueryController with thread-safe design
- [ ] Document thread safety for each method
- [ ] Update jira_tui.py to use QueryController
- [ ] Verify app still works (manual test)

**Day 3: Write 7 Critical Tests**
- [ ] test_startup_under_1_second
- [ ] test_background_refresh_doesnt_block
- [ ] test_no_upfront_user_caching
- [ ] test_url_encoding_correct
- [ ] test_no_deadlock_timeout
- [ ] test_concurrent_query_execution (NEW)
- [ ] test_no_lock_held_during_network_io (NEW)
- [ ] Verify all pass: `pytest tests/test_jira_view_core.py -v`

**Day 4: Dedicated Threading Test Suite**
- [ ] Create test_threading.py
- [ ] test_shared_state_corruption
- [ ] test_cache_update_during_read
- [ ] test_sqlite_concurrent_access
- [ ] Verify all pass: `pytest tests/test_threading.py -v`

**Day 5: Verify and Fix**
- [ ] Run all tests 10x to catch flaky failures
- [ ] Fix any threading bugs discovered
- [ ] Manual testing with focus on concurrency
- [ ] Document any threading patterns discovered

### Week 2: Full Extraction + Stress Testing

**Day 1-2: Extract Remaining Controllers**
- [ ] Add TicketController to jira_view_core.py
- [ ] Add CacheController to jira_view_core.py
- [ ] Document thread safety for all methods
- [ ] Write controller tests

**Day 3: Refactor jira_tui.py**
- [ ] Move all business logic to controllers
- [ ] Keep only curses rendering
- [ ] Thread-safe screen updates
- [ ] Verify app functionality unchanged

**Day 4: Write Stress Tests**
- [ ] test_many_concurrent_queries
- [ ] test_rapid_refresh_cycles
- [ ] test_cache_clear_during_operations
- [ ] Mark with @pytest.mark.stress

**Day 5: Manual Testing**
- [ ] Test with realistic workloads
- [ ] Test with multiple "users" (windows)
- [ ] Test rapid user actions during loading
- [ ] Document any issues found

### Week 3: Comprehensive Testing

**Day 1-2: All Controller Tests**
- [ ] Complete test_jira_view_core.py
- [ ] Test all QueryController methods
- [ ] Test all TicketController methods
- [ ] Test all CacheController methods

**Day 3: Integration Tests**
- [ ] First run scenario (empty cache)
- [ ] Second run scenario (populated cache)
- [ ] Stale cache scenario
- [ ] Large query performance
- [ ] Network error handling
- [ ] Threading scenarios

**Day 4: API/Cache Tests**
- [ ] test_jira_utils.py (URL encoding, batching, pagination)
- [ ] test_jira_cache.py (operations, TTL, concurrency)

**Day 5: Coverage Review**
- [ ] Run coverage: `pytest --cov=jira_view_core --cov-report=html`
- [ ] Aim for >80% on controllers
- [ ] Fill coverage gaps

### Week 4: Polish & CI

**Day 1: Performance Benchmarks**
- [ ] test_query_cache_performance
- [ ] test_cache_hit_ratio
- [ ] Baseline performance measurements

**Day 2: Property-Based Tests**
- [ ] Install hypothesis
- [ ] test_concurrent_operations_any_order
- [ ] Other property-based tests

**Day 3: Documentation**
- [ ] Document test patterns
- [ ] Document threading design
- [ ] Document mocking strategy
- [ ] Update README with testing info

**Day 4: GitHub Actions CI**
- [ ] Create .github/workflows/test.yml
- [ ] Run tests on every commit
- [ ] Coverage reporting
- [ ] Run stress tests nightly

**Day 5: Final Review**
- [ ] Review all tests for clarity
- [ ] Optimize slow tests
- [ ] Document known limitations
- [ ] Celebrate! 🎉

## Running Tests

### Quick Smoke Test (7 critical tests)
```bash
pytest tests/test_jira_view_core.py::test_startup_under_1_second \
       tests/test_jira_view_core.py::test_background_refresh_doesnt_block \
       tests/test_jira_view_core.py::test_no_upfront_user_caching \
       tests/test_jira_view_core.py::test_url_encoding_correct \
       tests/test_jira_view_core.py::test_no_deadlock_timeout \
       tests/test_threading.py::test_concurrent_query_execution \
       tests/test_threading.py::test_no_lock_held_during_network_io \
       -v --timeout=10
```

### Full Threading Suite
```bash
pytest tests/test_threading.py -v --timeout=30
```

### Stress Tests (optional, slower)
```bash
pytest -m stress --timeout=60
```

### All Tests with Coverage
```bash
pytest tests/ --cov=jira_view_core --cov=jira_utils --cov=jira_cache \
       --cov-report=html --timeout=30
```

### Run Tests 10x (catch flaky failures)
```bash
for i in {1..10}; do
    echo "Run $i"
    pytest tests/test_threading.py -v --timeout=30 || break
done
```

## Success Criteria

### Must Have
- ✅ All business logic testable without curses
- ✅ **7 critical tests passing (5 original + 2 concurrency)**
- ✅ **Dedicated threading test suite (test_threading.py)**
- ✅ Mock API layer (no real Jira calls in tests)
- ✅ **No race conditions in basic operations**
- ✅ **No deadlocks under normal load**
- ✅ App functionality unchanged (backward compatible)
- ✅ Tests run in <10 seconds (excluding stress tests)

### Should Have
- ✅ >80% test coverage on controllers
- ✅ **Stress tests for 100+ concurrent operations**
- ✅ **SQLite concurrency tests**
- ✅ Integration tests with threading scenarios
- ✅ Performance tests for startup/caching
- ✅ **Lock ordering verification**
- ✅ **Thread safety documentation**

### Nice to Have
- ✅ Property-based testing (Hypothesis)
- ✅ CI integration (GitHub Actions)
- ✅ Pre-commit hooks
- ✅ Type hints + mypy checking
- ✅ Thread profiling/visualization
- ✅ Performance regression detection

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
3. Test fails: "Startup took 8.2s" or "Deadlock detected"
4. AI fixes blocking operation
5. Tests pass
6. User tests once to verify UI
7. Done

### For User

- **Faster iterations** - Less manual testing
- **Fewer regressions** - Tests catch when fixes break other things
- **Better error messages** - "Startup took 8.2s" vs "it hangs"
- **More confidence** - Tests document expected behavior
- **Threading bugs caught early** - Not in production

### For Maintenance

- **Safe refactoring** - Tests verify behavior unchanged
- **Documentation** - Tests show how code should work
- **Regression prevention** - Bug fixes include tests
- **Faster debugging** - Reproduce issues in tests
- **Threading confidence** - Concurrency bugs caught immediately

## Testing Philosophy

**Test what makes development easier:**
- ✅ Test business logic (controllers)
- ✅ **Test threading behavior (PRIMARY FOCUS)**
- ✅ Test complex algorithms (caching, pagination)
- ✅ Test performance requirements (<1s startup)
- ✅ **Test race conditions and deadlocks**
- ✅ **Test lock ordering and shared state**
- ❌ Don't test curses rendering (hard, low value)
- ❌ Don't test Jira API (mock it instead)
- ❌ Don't test for 100% coverage (test useful things)

**Tests are a tool, not a goal.**

The 7 critical tests would have saved 2+ hours of debugging on the caching implementation.

The threading tests will prevent future concurrency bugs that have plagued this codebase.

## Threading-Specific Design Guidelines

### Controller Implementation Rules

1. **Never hold locks during I/O** - acquire lock, update state, release, THEN do I/O
2. **Use atomic flags** - simple booleans for cross-thread communication
3. **Minimize shared state** - prefer message passing or immutable data
4. **Single lock per controller** - avoid complex lock hierarchies
5. **Document thread safety** - every method labeled thread-safe or not
6. **Test-driven** - write threading test BEFORE implementing feature

### Code Review Checklist

When reviewing controller code, verify:

- [ ] No locks held during network/disk I/O
- [ ] Shared state updates are atomic or locked
- [ ] Background threads are daemon threads
- [ ] Threading tests exist for new concurrent features
- [ ] No nested locks
- [ ] Lock acquisition order documented (if multiple locks)
- [ ] Timeout on all thread joins
- [ ] Thread safety documented in docstrings

### Common Threading Pitfalls to Avoid

**❌ Bad: Lock held during I/O**
```python
def execute_query(self, jql):
    with self.lock:
        tickets = self.utils.fetch_from_api(jql)  # BLOCKS!
        self.ticket_cache = tickets
```

**✅ Good: Lock only for state update**
```python
def execute_query(self, jql):
    tickets = self.utils.fetch_from_api(jql)  # No lock
    with self.lock:
        self.ticket_cache = tickets  # Quick update
```

**❌ Bad: Complex shared state**
```python
self.state = {
    'loading': False,
    'progress': 0,
    'tickets': [],
    'errors': []
}
# Hard to synchronize correctly
```

**✅ Good: Atomic flags**
```python
self.loading = False  # Simple boolean
self.refresh_needed = False  # Simple boolean
# Cache handles ticket storage
```

**❌ Bad: Nested locks**
```python
with self.cache_lock:
    with self.update_lock:
        # Complex ordering, easy to deadlock
```

**✅ Good: Single lock**
```python
with self.update_lock:
    # One lock, no ordering issues
```

## TUI Testing Strategy

**Explicitly OUT OF SCOPE for automated testing:**
- Curses rendering correctness
- Visual layout and formatting
- Keyboard input handling
- Screen refresh behavior

**Rationale:** TUI testing is hard and low ROI. Manual testing is acceptable for presentation layer.

**IN SCOPE for automated testing:**
- All business logic (controllers) - **WITH CONCURRENCY**
- All API interactions (mocked)
- All caching behavior - **WITH CONCURRENT ACCESS**
- All data transformations
- All threading behavior - **PRIMARY FOCUS**

This gives us 90% of the value with 10% of the effort, and catches the bugs that matter.

---

## Next Steps

1. **Immediate:** Create test infrastructure + extract QueryController
2. **Week 1:** Write 7 critical tests + threading suite
3. **Week 2:** Full controller extraction + stress tests
4. **Week 3:** Comprehensive testing + coverage
5. **Week 4:** Polish, CI, documentation

**Start small, iterate based on value. Thread safety is first-class, not an afterthought.**
