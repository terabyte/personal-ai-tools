"""
Dedicated threading and concurrency tests.

This module contains comprehensive tests for concurrent operations,
race conditions, deadlocks, and thread safety. These tests go beyond
the 7 critical tests and stress-test the system under heavy concurrent load.

Test Categories:
- Race condition detection
- Shared state corruption
- SQLite concurrency
- Stress tests (100+ concurrent operations)
- Property-based testing (optional)
"""

import pytest
import time
import threading
from unittest.mock import MagicMock


class TestRaceConditions:
    """Tests for detecting race conditions in concurrent operations."""

    def test_shared_state_corruption(self, query_controller):
        """
        Hammer controller with concurrent operations to detect state corruption.

        If there are race conditions in ticket_cache updates or flag updates,
        this test will catch them by exercising many concurrent operations.
        """
        errors = []

        def worker():
            """Worker that hammers the controller with operations"""
            try:
                for _ in range(20):
                    # Mix of different operations
                    query_controller.execute_query("project=TEST", ["key"])
                    query_controller.get_background_status()
                    query_controller.is_startup_complete()
            except Exception as e:
                errors.append(e)

        # Spawn 5 concurrent workers
        threads = [threading.Thread(target=worker) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Should not crash or corrupt state
        assert len(errors) == 0, f"Workers encountered errors: {errors}"

        # Controller should still work after hammering
        result = query_controller.execute_query("project=FINAL", ["key"])
        assert result is not None, "Controller corrupted after concurrent ops"

    def test_cache_update_during_read(self, query_controller):
        """
        Verify reading cache while background thread updates it.

        This tests that read operations don't see partially-written state
        or cause crashes when cache is being updated.
        """
        # Start initial query to populate some state
        query_controller.execute_query("project=TEST", ["key"])

        # Immediately start hammering with read operations
        read_errors = []

        def continuous_reader():
            """Continuously read from controller"""
            for _ in range(100):
                try:
                    result = query_controller.execute_query("project=TEST", ["key"])
                    assert result is not None
                    assert hasattr(result, 'tickets')
                except Exception as e:
                    read_errors.append(e)
                time.sleep(0.01)  # Brief pause between reads

        reader_thread = threading.Thread(target=continuous_reader)
        reader_thread.start()
        reader_thread.join(timeout=5)

        assert len(read_errors) == 0, f"Reads failed during updates: {read_errors}"

    def test_concurrent_cache_access(self, query_controller):
        """
        Multiple threads reading/writing cache simultaneously.

        Tests that the internal ticket_cache dict is properly synchronized.
        """
        errors = []

        def reader_writer(thread_id):
            """Alternate between reading and writing cache"""
            try:
                for i in range(10):
                    # Write
                    query_controller.execute_query(
                        f"project=TEST{thread_id}",
                        ["key"]
                    )
                    # Read
                    result = query_controller.execute_query(
                        f"project=TEST{thread_id}",
                        ["key"]
                    )
                    assert result is not None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader_writer, args=(i,)) for i in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Concurrent access errors: {errors}"


class TestCacheLayerConcurrency:
    """Tests for cache layer thread safety (when real cache is implemented)."""

    def test_sqlite_concurrent_access(self, temp_cache):
        """
        Verify cache handles concurrent reads/writes.

        Tests concurrent access to JiraCache (file-based) operations.
        Future SQLite cache will have more rigorous concurrent access patterns.
        """
        errors = []

        def cache_operations():
            """Perform cache operations"""
            try:
                for i in range(10):
                    # Test read/write operations
                    temp_cache.set('test_category', f'data_{i}', ttl=3600)
                    data = temp_cache.get('test_category')
                    assert data is not None or i == 0  # First may be None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=cache_operations) for _ in range(3)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Cache operations failed: {errors}"

    def test_cache_clear_during_read(self, cache_controller, query_controller):
        """
        Verify cache clear doesn't break ongoing reads.

        Tests that clearing cache while queries are running doesn't
        cause crashes or data corruption.
        """
        errors = []
        stop_flag = threading.Event()

        def continuous_queries():
            """Keep running queries"""
            while not stop_flag.is_set():
                try:
                    query_controller.execute_query("project=TEST", ["key"])
                    time.sleep(0.05)
                except Exception as e:
                    errors.append(e)

        query_thread = threading.Thread(target=continuous_queries, daemon=True)
        query_thread.start()

        # Clear cache multiple times while queries running
        for _ in range(5):
            time.sleep(0.1)
            try:
                cache_controller.clear_tickets()
            except Exception as e:
                errors.append(e)

        stop_flag.set()
        query_thread.join(timeout=2)

        assert len(errors) == 0, f"Errors during cache clear: {errors}"


class TestBackgroundThreadBehavior:
    """Tests for background thread management."""

    def test_multiple_background_threads_dont_interfere(self, query_controller):
        """
        Spawn multiple background threads and verify they don't interfere.

        Each query should spawn its own background thread (if needed).
        They should all complete without issues.
        """
        results = []
        errors = []

        def query_different_projects(project_id):
            """Query different project"""
            try:
                result = query_controller.execute_query(
                    f"project=PROJ{project_id}",
                    ["key", "summary"]
                )
                results.append(result)
            except Exception as e:
                errors.append(e)

        # Spawn many concurrent queries (each may spawn background thread)
        threads = [
            threading.Thread(target=query_different_projects, args=(i,))
            for i in range(10)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Queries failed: {errors}"
        assert len(results) == 10, f"Expected 10 results, got {len(results)}"

    def test_background_thread_cleanup(self, query_controller):
        """
        Verify background threads are daemon threads and clean up properly.

        Daemon threads should not prevent process exit.
        """
        # Spawn a query that will create a background thread
        result = query_controller.execute_query("project=TEST", ["key"])
        assert result is not None

        # Check if background thread is running
        is_running, _, _ = query_controller.get_background_status()

        if is_running:
            # Background thread should be daemon
            if query_controller._background_thread:
                assert query_controller._background_thread.daemon, \
                    "Background thread should be daemon for clean shutdown"


@pytest.mark.stress
class TestStressScenarios:
    """Stress tests with high concurrent load (marked for optional runs)."""

    def test_many_concurrent_queries(self, query_controller):
        """
        100 concurrent queries to stress test the system.

        Run with: pytest -m stress

        This tests scalability and ensures no resource leaks or crashes
        under heavy concurrent load.
        """
        results = []
        errors = []

        def worker(query_id):
            """Execute query"""
            try:
                result = query_controller.execute_query(
                    f"project=T{query_id}",
                    ["key"]
                )
                results.append(result)
            except Exception as e:
                errors.append(e)

        # Spawn 100 concurrent queries
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]

        start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        elapsed = time.time() - start

        assert len(errors) == 0, f"Queries failed: {errors}"
        assert len(results) == 100, f"Expected 100 results, got {len(results)}"

        # Should complete reasonably fast (even with 100 threads)
        assert elapsed < 15, f"Stress test took {elapsed:.1f}s (too slow)"

    def test_rapid_ticket_refresh_cycles(self, ticket_controller):
        """
        Rapid ticket refresh in parallel.

        Tests that ticket refresh operations don't cause resource leaks
        or corruption when hammered rapidly.
        """
        errors = []

        def rapid_refresh():
            """Rapidly refresh same ticket"""
            try:
                for _ in range(50):
                    ticket_controller.refresh_ticket("TEST-1")
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=rapid_refresh) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert len(errors) == 0, f"Refresh errors: {errors}"

    def test_cache_operations_under_load(self, cache_controller, query_controller):
        """
        Mix of cache operations while queries running.

        Tests that cache management doesn't break under load.
        """
        errors = []
        stop_flag = threading.Event()

        def continuous_queries():
            """Keep running queries"""
            while not stop_flag.is_set():
                try:
                    query_controller.execute_query("project=TEST", ["key"])
                    time.sleep(0.05)
                except Exception as e:
                    errors.append(e)

        def cache_operations():
            """Perform various cache operations"""
            try:
                for _ in range(10):
                    cache_controller.get_stats()
                    time.sleep(0.1)
                    cache_controller.clear_tickets()
                    time.sleep(0.1)
            except Exception as e:
                errors.append(e)

        # Start continuous queries
        query_thread = threading.Thread(target=continuous_queries, daemon=True)
        query_thread.start()

        # Start cache operations
        cache_thread = threading.Thread(target=cache_operations)
        cache_thread.start()
        cache_thread.join(timeout=10)

        stop_flag.set()
        query_thread.join(timeout=2)

        assert len(errors) == 0, f"Errors under load: {errors}"


class TestThreadSafetyProperties:
    """Property-based tests for thread safety (requires hypothesis)."""

    def test_any_query_sequence_is_safe(self, query_controller):
        """
        Any sequence of queries should be safe.

        This is a simplified version without hypothesis, but tests
        various query patterns.
        """
        errors = []

        # Test various query patterns
        patterns = [
            ["project=A", "project=B", "project=C"],  # Sequential different
            ["project=X"] * 5,  # Repeated same
            ["status=Open", "status=Closed", "status=Open"],  # Alternating
        ]

        def run_pattern(pattern):
            """Execute query pattern"""
            try:
                for jql in pattern:
                    result = query_controller.execute_query(jql, ["key"])
                    assert result is not None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run_pattern, args=(p,)) for p in patterns]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Pattern execution failed: {errors}"


# Test marks for organization
pytestmark = pytest.mark.threading


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--timeout=30'])
