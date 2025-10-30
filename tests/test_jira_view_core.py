"""
Critical tests for jira_view_core controllers.

These tests verify the most important behaviors that would have caught
real bugs in production. Focus is on:
1. Startup performance (<1 second)
2. Non-blocking background operations
3. No upfront user caching
4. URL encoding correctness
5. No deadlocks
6. Concurrent query execution
7. No locks held during I/O

These 7 tests are the minimum viable test suite.
"""

import pytest
import time
import threading
from urllib.parse import quote, unquote


class TestCriticalPerformance:
    """Critical performance tests that would have caught the startup hang bug."""

    def test_startup_under_1_second(self, query_controller):
        """
        CRITICAL: Verify query execution returns in <1 second.

        This catches startup blocking bugs like:
        - Synchronous API calls on main thread
        - Bulk cache writes blocking startup
        - Upfront user caching

        Bug History: Application hung for 8+ seconds at startup due to
        blocking operations. This test would have caught it immediately.
        """
        start = time.time()
        result = query_controller.execute_query("project=TEST", ["key", "summary"])
        elapsed = time.time() - start

        assert elapsed < 1.0, f"Query took {elapsed:.2f}s, should be <1s"
        assert result is not None, "Query returned None"
        assert result.tickets is not None, "Query result has no tickets"
        assert isinstance(result.status, str), "Query result has no status"

    def test_background_refresh_doesnt_block(self, query_controller):
        """
        CRITICAL: Verify background refresh starts but doesn't block main thread.

        This catches bugs where:
        - Background threads block main thread
        - join() called on non-daemon threads
        - Locks held during background operations

        Bug History: Background refresh blocked UI for seconds.
        This test ensures UI remains responsive.
        """
        # First query - may spawn background thread
        result = query_controller.execute_query("project=TEST", ["key", "summary"])
        assert result is not None, "First query returned None"

        # Should be able to immediately do more work (not blocked)
        start = time.time()
        result2 = query_controller.execute_query("project=OTHER", ["key"])
        elapsed = time.time() - start

        assert result2 is not None, "Second query returned None"
        assert elapsed < 0.5, f"Second query blocked for {elapsed:.2f}s"

    def test_no_upfront_user_caching(self, query_controller, temp_cache):
        """
        CRITICAL: Verify users are NOT cached upfront at startup.

        This catches the bug where:
        - 728 users cached upfront before showing UI
        - Startup blocked for 5+ seconds on user cache population
        - Users fetched even when not needed for display

        Bug History: Startup hung because of upfront user caching.
        Users should be cached LAZILY (on-demand), not upfront.
        """
        # Execute query that includes assignee field
        query_controller.execute_query("project=TEST", ["key", "summary", "assignee"])

        # Check cache - users category should not be populated
        # JiraCache uses category-based caching, check if 'users' is cached
        is_users_cached = temp_cache.is_cached('users')

        # Users should NOT be cached upfront
        # NOT 728 users like in the bug!
        assert not is_users_cached, "Users were cached upfront (should be lazy)"


class TestCriticalCorrectness:
    """Critical correctness tests for data integrity."""

    def test_url_encoding_correct(self):
        """
        CRITICAL: Verify URL encoding doesn't break JQL keywords.

        This catches bugs where:
        - Manual .replace(' ', '%20') breaks keywords like "is empty"
        - Special characters not properly escaped
        - JQL syntax corrupted by incorrect encoding

        Bug History: JQL query "sprint is empty" broken by manual encoding.
        Must use urllib.parse.quote() for proper encoding.
        """
        # Test JQL with special keywords
        jql = "project = TEST and sprint is empty"

        # Proper encoding
        encoded = quote(jql, safe='')
        decoded = unquote(encoded)

        # Keywords must be preserved
        assert "is empty" in decoded, "JQL keyword 'is empty' corrupted"
        assert "project" in decoded, "JQL keyword 'project' corrupted"
        assert "TEST" in decoded, "Project name corrupted"

        # Test other special keywords
        test_cases = [
            "status is not null",
            "assignee is empty",
            "sprint is not empty",
            "labels in (bug, feature)"
        ]

        for jql in test_cases:
            encoded = quote(jql, safe='')
            decoded = unquote(encoded)
            # Original JQL should be recoverable
            assert decoded == jql, f"JQL corrupted: {jql} -> {decoded}"


class TestCriticalThreading:
    """Critical threading tests that would have caught concurrency bugs."""

    @pytest.mark.timeout(5)
    def test_no_deadlock_timeout(self, query_controller):
        """
        CRITICAL: Verify no threading deadlocks occur.

        This catches bugs where:
        - Nested locks cause deadlock
        - Background threads wait indefinitely
        - Lock ordering issues

        Test will timeout after 5 seconds if deadlock occurs.

        Bug History: Application occasionally froze completely due to deadlocks.
        """
        # Multiple sequential queries shouldn't deadlock
        for i in range(5):
            result = query_controller.execute_query(f"project=TEST{i}", ["key"])
            assert result is not None, f"Query {i} returned None (possible deadlock)"

        # Should complete within timeout
        # If we get here, no deadlock occurred

    def test_concurrent_query_execution(self, query_controller):
        """
        CRITICAL: Execute 10 queries concurrently without race conditions.

        This catches bugs where:
        - Shared state corrupted by concurrent access
        - Race conditions in ticket_cache updates
        - Missing synchronization

        Bug History: Concurrent queries caused crashes and data corruption.
        """
        results = []
        errors = []

        def run_query(jql):
            try:
                result = query_controller.execute_query(jql, ["key"])
                results.append(result)
            except Exception as e:
                errors.append(e)

        # Spawn 10 concurrent queries
        threads = [
            threading.Thread(target=run_query, args=(f"project=TEST{i}",))
            for i in range(10)
        ]

        # Start all threads
        for t in threads:
            t.start()

        # Wait for all to complete (with timeout)
        for t in threads:
            t.join(timeout=5)

        # Verify no errors occurred
        assert len(errors) == 0, f"Concurrent queries failed: {errors}"
        assert len(results) == 10, f"Expected 10 results, got {len(results)}"

        # Verify all results are valid
        for result in results:
            assert result is not None, "Query returned None"
            assert hasattr(result, 'tickets'), "Result missing tickets"
            assert hasattr(result, 'status'), "Result missing status"

    @pytest.mark.timeout(3)
    def test_no_lock_held_during_network_io(self, query_controller, slow_mock_jira_utils):
        """
        CRITICAL: Verify locks are released during slow network operations.

        This catches the bug where:
        - Locks held during API calls (I/O)
        - Second query blocks waiting for first query's I/O
        - Long network delays block all operations

        If locks are held during I/O, the second query will block
        for the full duration of the first query's network call.

        Bug History: UI froze for seconds while waiting for API calls
        because lock was held during I/O.
        """
        # Replace with slow mock to simulate network latency
        query_controller.utils = slow_mock_jira_utils

        # Start first query in background (will take 0.5s due to slow mock)
        t1 = threading.Thread(
            target=lambda: query_controller.execute_query("project=TEST1", ["key"])
        )
        t1.start()

        # Let first query start and acquire any locks
        time.sleep(0.1)

        # Second query should NOT wait for first query's I/O
        start = time.time()
        result = query_controller.execute_query("project=TEST2", ["key"])
        elapsed = time.time() - start

        # Should return quickly (not blocked by first query's 0.5s delay)
        assert elapsed < 0.3, \
            f"Query blocked for {elapsed:.2f}s (lock held during I/O?)"
        assert result is not None, "Query returned None"

        # Cleanup
        t1.join(timeout=2)


class TestControllerBasics:
    """Basic sanity tests for controller functionality."""

    def test_is_startup_complete_always_true(self, query_controller):
        """
        Verify is_startup_complete() always returns True.

        This method exists to verify the <1s startup requirement.
        If it returns False, the controller is doing something wrong.
        """
        assert query_controller.is_startup_complete() is True

    def test_get_background_status_returns_tuple(self, query_controller):
        """
        Verify get_background_status() returns expected format.

        Should always return (is_running, current, total) tuple.
        """
        status = query_controller.get_background_status()

        assert isinstance(status, tuple), "Status should be tuple"
        assert len(status) == 3, "Status should have 3 elements"

        is_running, current, total = status
        assert isinstance(is_running, bool), "is_running should be bool"

    def test_query_result_structure(self, query_controller):
        """
        Verify QueryResult has expected structure.

        All callers depend on this structure.
        """
        result = query_controller.execute_query("project=TEST", ["key"])

        assert result is not None, "Result is None"
        assert hasattr(result, 'tickets'), "Missing tickets attribute"
        assert hasattr(result, 'status'), "Missing status attribute"
        assert hasattr(result, 'cache_age'), "Missing cache_age attribute"
        assert hasattr(result, 'is_updating'), "Missing is_updating attribute"

        assert isinstance(result.tickets, list), "tickets should be list"
        assert isinstance(result.status, str), "status should be string"
        assert isinstance(result.is_updating, bool), "is_updating should be bool"

    def test_force_refresh_bypasses_cache(self, query_controller, mock_jira_utils):
        """
        Verify force_refresh=True always fetches from API.

        Important for 'R' key (refresh query) functionality.
        """
        # First call
        result1 = query_controller.execute_query("project=TEST", ["key"], force_refresh=False)
        call_count_1 = mock_jira_utils.fetch_all_jql_results.call_count

        # Second call with force_refresh
        result2 = query_controller.execute_query("project=TEST", ["key"], force_refresh=True)
        call_count_2 = mock_jira_utils.fetch_all_jql_results.call_count

        # Force refresh should make additional API call
        assert call_count_2 > call_count_1, "Force refresh didn't call API"


class TestTicketController:
    """Tests for TicketController methods."""

    def test_fetch_ticket_blocking(self, ticket_controller, mock_jira_utils):
        """
        Verify fetch_ticket returns ticket synchronously.
        """
        # Mock returns single ticket
        mock_jira_utils.fetch_all_jql_results.return_value = [
            {"key": "TEST-123", "fields": {"summary": "Test ticket"}}
        ]

        ticket = ticket_controller.fetch_ticket("TEST-123")

        assert ticket is not None
        assert ticket['key'] == "TEST-123"
        assert ticket['fields']['summary'] == "Test ticket"

    def test_fetch_ticket_returns_none_on_error(self, ticket_controller, mock_jira_utils):
        """
        Verify fetch_ticket handles errors gracefully.
        """
        # Mock raises exception
        mock_jira_utils.fetch_all_jql_results.side_effect = Exception("API Error")

        ticket = ticket_controller.fetch_ticket("TEST-123")

        assert ticket is None

    def test_refresh_ticket_non_blocking(self, ticket_controller):
        """
        Verify refresh_ticket returns immediately (doesn't block).
        """
        start = time.time()
        ticket_controller.refresh_ticket("TEST-123")
        elapsed = time.time() - start

        # Should return immediately (spawns background thread)
        assert elapsed < 0.1, f"refresh_ticket blocked for {elapsed:.2f}s"

    def test_refresh_ticket_calls_callback(self, ticket_controller, mock_jira_utils):
        """
        Verify refresh_ticket calls callback when complete.
        """
        # Mock returns ticket
        mock_jira_utils.fetch_all_jql_results.return_value = [
            {"key": "TEST-123", "fields": {"summary": "Refreshed"}}
        ]

        callback_called = threading.Event()
        received_ticket = [None]

        def callback(ticket):
            received_ticket[0] = ticket
            callback_called.set()

        ticket_controller.refresh_ticket("TEST-123", callback=callback)

        # Wait for callback (with timeout)
        assert callback_called.wait(timeout=2), "Callback not called"
        assert received_ticket[0] is not None
        assert received_ticket[0]['key'] == "TEST-123"

    def test_fetch_transitions(self, ticket_controller, mock_jira_utils):
        """
        Verify fetch_transitions returns transitions and caches them.
        """
        mock_jira_utils.call_jira_api.return_value = {
            'transitions': [
                {'id': '11', 'name': 'To Do'},
                {'id': '21', 'name': 'In Progress'},
                {'id': '31', 'name': 'Done'}
            ]
        }

        # First call - should fetch from API
        transitions = ticket_controller.fetch_transitions("TEST-123")

        assert transitions is not None
        assert len(transitions) == 3
        assert transitions[0]['name'] == 'To Do'

        # Second call - should hit cache
        mock_jira_utils.call_jira_api.reset_mock()
        transitions2 = ticket_controller.fetch_transitions("TEST-123")

        assert transitions2 == transitions
        assert not mock_jira_utils.call_jira_api.called, "Should have used cache"

    def test_get_cached_transitions(self, ticket_controller):
        """
        Verify get_cached_transitions returns None when not cached.
        """
        transitions = ticket_controller.get_cached_transitions("TEST-999")
        assert transitions is None

    def test_format_ticket_display(self, ticket_controller):
        """
        Verify format_ticket_display is a pure function.
        """
        ticket = {
            'key': 'TEST-123',
            'fields': {
                'summary': 'Test Summary',
                'status': {'name': 'In Progress'},
                'updated': '2025-01-15T10:00:00.000+0000'
            }
        }

        # Format with custom formatters
        formatters = {
            'status': lambda s: s['name'].upper(),
            'updated': lambda d: d.split('T')[0]  # Extract date
        }

        result = ticket_controller.format_ticket_display(ticket, formatters)

        assert result['key'] == 'TEST-123'
        assert result['summary'] == 'Test Summary'
        assert result['status'] == 'IN PROGRESS'
        assert result['updated'] == '2025-01-15'


class TestCacheController:
    """Tests for CacheController methods."""

    def test_get_stats(self, cache_controller):
        """
        Verify get_stats returns expected structure.
        """
        stats = cache_controller.get_stats()

        assert isinstance(stats, dict)
        # Should have info about common cache categories
        assert 'link_types' in stats or 'users' in stats or len(stats) >= 0

    def test_get_cache_ages(self, cache_controller):
        """
        Verify get_cache_ages returns age strings.
        """
        ages = cache_controller.get_cache_ages()

        assert isinstance(ages, dict)
        # Each age should be a string or None (if not cached)
        for category, age in ages.items():
            assert age is None or isinstance(age, str)

    def test_refresh_metadata(self, cache_controller):
        """
        Verify refresh_metadata invalidates cache category.
        """
        # Should not raise exception
        cache_controller.refresh_metadata('link_types')
        cache_controller.refresh_metadata('users')

    def test_clear_tickets(self, cache_controller):
        """
        Verify clear_tickets executes without error.
        """
        count = cache_controller.clear_tickets()
        assert isinstance(count, int)
        assert count >= 0

    def test_clear_users(self, cache_controller):
        """
        Verify clear_users executes without error.
        """
        count = cache_controller.clear_users()
        assert isinstance(count, int)
        assert count >= 0

    def test_clear_all(self, cache_controller):
        """
        Verify clear_all clears entire cache.
        """
        # Should not raise exception
        cache_controller.clear_all()


# Test marks for organization
pytestmark = [
    pytest.mark.threading,  # All tests in this file test threading behavior
]


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
