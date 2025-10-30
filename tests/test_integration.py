"""
Integration tests for jira_view_core controllers.

These tests verify realistic end-to-end scenarios with mocked API,
testing how controllers interact with each other and the cache layer.

Test Scenarios:
- First run (empty cache)
- Second run (populated cache)
- Concurrent operations across controllers
- Error handling and recovery
"""

import pytest
import time
import threading


class TestFirstRunScenario:
    """Test behavior on first run with empty cache."""

    def test_first_run_query_returns_quickly(self, query_controller):
        """
        First run should return immediately even with empty cache.

        Expected behavior:
        1. Returns in <1s with FIRST_RUN status
        2. Returns empty tickets initially
        3. Background thread populates cache
        """
        start = time.time()
        result = query_controller.execute_query("project=TEST", ["key", "summary"])
        elapsed = time.time() - start

        # Should return quickly
        assert elapsed < 1.0, f"First run took {elapsed:.2f}s, should be <1s"

        # Should indicate first run
        assert result.status == "FIRST_RUN", f"Expected FIRST_RUN, got {result.status}"

        # Should have spawned background thread
        assert result.is_updating is True, "Should be updating in background"

    def test_first_run_ticket_fetch(self, ticket_controller, mock_jira_utils):
        """
        First ticket fetch should work without cache.
        """
        mock_jira_utils.fetch_all_jql_results.return_value = [
            {"key": "TEST-123", "fields": {"summary": "Test"}}
        ]

        ticket = ticket_controller.fetch_ticket("TEST-123")

        assert ticket is not None
        assert ticket['key'] == "TEST-123"


class TestSecondRunScenario:
    """Test behavior on second run with populated cache."""

    def test_second_run_uses_cache(self, query_controller, mock_jira_utils):
        """
        Second run should use cached data from first run.

        Expected behavior:
        1. First query populates cache (via background thread)
        2. Second query returns cached data immediately
        3. Background verification may still run
        """
        # First run - populates cache
        result1 = query_controller.execute_query("project=TEST", ["key"])

        # Give background thread time to populate cache
        time.sleep(0.2)

        # Second run - should use cache
        start = time.time()
        result2 = query_controller.execute_query("project=TEST", ["key"])
        elapsed = time.time() - start

        # Should be very fast (cache hit)
        assert elapsed < 0.5, f"Cached query took {elapsed:.2f}s"

        # Should have tickets from cache
        assert result2.tickets is not None


class TestConcurrentControllers:
    """Test concurrent operations across multiple controllers."""

    def test_query_and_ticket_controllers_concurrent(
        self,
        query_controller,
        ticket_controller,
        mock_jira_utils
    ):
        """
        QueryController and TicketController can operate concurrently.
        """
        errors = []
        results = {'query': None, 'ticket': None}

        def run_query():
            try:
                results['query'] = query_controller.execute_query(
                    "project=TEST",
                    ["key", "summary"]
                )
            except Exception as e:
                errors.append(e)

        def refresh_ticket():
            try:
                # Set up mock for ticket refresh
                mock_jira_utils.fetch_all_jql_results.return_value = [
                    {"key": "TEST-123", "fields": {"summary": "Refreshed"}}
                ]
                results['ticket'] = ticket_controller.fetch_ticket("TEST-123")
            except Exception as e:
                errors.append(e)

        # Run both concurrently
        t1 = threading.Thread(target=run_query)
        t2 = threading.Thread(target=refresh_ticket)

        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(errors) == 0, f"Concurrent operations failed: {errors}"
        assert results['query'] is not None
        assert results['ticket'] is not None

    def test_all_three_controllers_concurrent(
        self,
        query_controller,
        ticket_controller,
        cache_controller
    ):
        """
        All three controllers can operate concurrently.
        """
        errors = []

        def use_query_controller():
            try:
                for _ in range(5):
                    query_controller.execute_query("project=TEST", ["key"])
            except Exception as e:
                errors.append(e)

        def use_ticket_controller():
            try:
                for _ in range(5):
                    ticket_controller.fetch_transitions("TEST-123")
            except Exception as e:
                errors.append(e)

        def use_cache_controller():
            try:
                for _ in range(5):
                    cache_controller.get_stats()
                    cache_controller.get_cache_ages()
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=use_query_controller),
            threading.Thread(target=use_ticket_controller),
            threading.Thread(target=use_cache_controller)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Concurrent controller use failed: {errors}"


class TestErrorRecovery:
    """Test error handling and recovery scenarios."""

    def test_query_error_with_no_cache(self, query_controller, mock_jira_utils):
        """
        API errors with no cache should return FIRST_RUN and spawn background thread.

        The background thread will fail and log the error, but the controller
        returns immediately with FIRST_RUN status. This is more resilient than
        returning ERROR immediately, as the background thread might retry or
        the user can take action.
        """
        # Mock raises exception
        mock_jira_utils.fetch_all_jql_results.side_effect = Exception("Network error")

        result = query_controller.execute_query("project=TEST", ["key"])

        assert result is not None, "Should return result even on error"
        # Returns FIRST_RUN because no cache, background thread will log error
        assert result.status == "FIRST_RUN", f"Expected FIRST_RUN, got {result.status}"
        assert result.is_updating is True, "Background thread should be spawned"

    def test_ticket_fetch_error_returns_none(self, ticket_controller, mock_jira_utils):
        """
        Ticket fetch errors should return None, not crash.
        """
        mock_jira_utils.fetch_all_jql_results.side_effect = Exception("API Error")

        ticket = ticket_controller.fetch_ticket("TEST-123")

        assert ticket is None, "Should return None on error"

    def test_transitions_error_returns_none(self, ticket_controller, mock_jira_utils):
        """
        Transition fetch errors should return None, not crash.
        """
        mock_jira_utils.call_jira_api.side_effect = Exception("API Error")

        transitions = ticket_controller.fetch_transitions("TEST-123")

        assert transitions is None, "Should return None on error"


class TestRealWorldPatterns:
    """Test realistic usage patterns from the TUI."""

    def test_typical_tui_startup_sequence(
        self,
        query_controller,
        ticket_controller,
        cache_controller
    ):
        """
        Simulate typical TUI startup sequence.

        1. Execute initial query
        2. Fetch transitions for first ticket
        3. Get cache stats for display
        """
        # Execute query (like TUI startup)
        result = query_controller.execute_query(
            "project=TEST AND status='In Progress'",
            ["key", "summary", "status", "assignee"]
        )

        assert result is not None
        assert result.status in ["FIRST_RUN", "UPDATING", "CURRENT"]

        # User selects first ticket - fetch transitions
        if result.tickets:
            first_key = result.tickets[0]['key']
            transitions = ticket_controller.fetch_transitions(first_key)
            # May be None if mock not configured, that's OK

        # Display cache stats in status bar
        stats = cache_controller.get_stats()
        assert stats is not None

        ages = cache_controller.get_cache_ages()
        assert ages is not None

    def test_refresh_workflow(self, query_controller, ticket_controller, mock_jira_utils):
        """
        Simulate user refresh workflow.

        1. Initial query
        2. User presses 'R' - force refresh query
        3. User presses 'r' - refresh single ticket
        """
        # Initial query
        result1 = query_controller.execute_query("project=TEST", ["key"])
        assert result1 is not None

        # User presses 'R' - force refresh
        result2 = query_controller.execute_query(
            "project=TEST",
            ["key"],
            force_refresh=True
        )
        assert result2 is not None
        assert result2.is_updating is True  # Background refresh started

        # User presses 'r' - refresh single ticket (non-blocking)
        callback_called = threading.Event()

        def on_refresh(ticket):
            callback_called.set()

        mock_jira_utils.fetch_all_jql_results.return_value = [
            {"key": "TEST-1", "fields": {"summary": "Updated"}}
        ]

        ticket_controller.refresh_ticket("TEST-1", callback=on_refresh)

        # Should complete quickly
        assert callback_called.wait(timeout=2), "Refresh didn't complete"


# Test marks
pytestmark = pytest.mark.threading


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
