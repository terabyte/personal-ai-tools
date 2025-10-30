"""
Shared test fixtures for personal-ai-tools tests.

Provides mocked dependencies and test helpers for controller testing.
"""

import pytest
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock

# Note: These imports will work once we create jira_view_core.py
# For now, they're documented for when we implement the controllers


@pytest.fixture
def fixture_dir():
    """Path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def mock_api_responses(fixture_dir):
    """
    Load mock API responses from JSON.

    Returns:
        dict: Mock responses with keys:
            - search_result: JQL search result with pagination
            - tickets: List of ticket objects
            - single_ticket: Single ticket detail response
    """
    with open(fixture_dir / "mock_api_responses.json") as f:
        return json.load(f)


@pytest.fixture
def mock_jira_utils(mock_api_responses):
    """
    Mock JiraUtils that returns canned responses.

    Thread Safety: Mocks are inherently thread-safe for reading.

    Returns:
        MagicMock: Mocked JiraUtils with configured responses
    """
    from unittest.mock import MagicMock

    # Import will work once jira_utils exists
    # from jira_utils import JiraUtils

    utils = MagicMock()  # spec=JiraUtils when available

    # Mock API calls to return test data
    utils.call_jira_api.return_value = mock_api_responses["search_result"]
    utils.fetch_all_jql_results.return_value = mock_api_responses["tickets"]

    # Mock cache (will be replaced with real cache in tests that need it)
    utils.cache = MagicMock()

    return utils


@pytest.fixture
def slow_mock_jira_utils(mock_api_responses):
    """
    Mock JiraUtils with intentionally slow API calls.

    Used for testing that locks aren't held during I/O operations.

    Thread Safety: Simulates network latency, safe for concurrent calls.

    Returns:
        MagicMock: Mocked JiraUtils with 0.5s delays
    """
    utils = MagicMock()

    def slow_fetch(*args, **kwargs):
        """Simulate slow network I/O"""
        time.sleep(0.5)
        return mock_api_responses["tickets"]

    utils.fetch_all_jql_results.side_effect = slow_fetch
    utils.call_jira_api.side_effect = lambda *args, **kwargs: (
        time.sleep(0.5), mock_api_responses["search_result"]
    )[1]

    utils.cache = MagicMock()

    return utils


@pytest.fixture
def temp_cache(tmp_path):
    """
    Temporary cache database for testing.

    Thread Safety: Each test gets isolated cache.

    Args:
        tmp_path: pytest's temporary directory fixture

    Returns:
        JiraCache: Temporary cache instance
    """
    # Import will work once jira_cache exists
    # from jira_cache import JiraCache

    # For now, return a mock
    cache = MagicMock()
    cache.get_cache_stats.return_value = {"users": {"count": 0}, "tickets": {"count": 0}}

    yield cache
    # Cleanup handled by tmp_path fixture


@pytest.fixture
def query_controller(mock_jira_utils, temp_cache):
    """
    QueryController with mocked dependencies.

    Thread Safety: Safe for concurrent test execution.

    Returns:
        QueryController: Controller instance with mocks
    """
    from jira_view_core import QueryController

    mock_jira_utils.cache = temp_cache
    return QueryController(mock_jira_utils)


@pytest.fixture
def ticket_controller(mock_jira_utils, temp_cache):
    """
    TicketController with mocked dependencies.

    Thread Safety: Safe for concurrent test execution.

    Returns:
        TicketController: Controller instance with mocks
    """
    from jira_view_core import TicketController

    mock_jira_utils.cache = temp_cache
    return TicketController(mock_jira_utils)


@pytest.fixture
def cache_controller(temp_cache):
    """
    CacheController with mocked dependencies.

    Thread Safety: Safe for concurrent test execution.

    Returns:
        CacheController: Controller instance with mocks
    """
    from jira_view_core import CacheController

    return CacheController(temp_cache)


@pytest.fixture
def thread_error_collector():
    """
    Collect errors from background threads.

    Usage:
        def test_something(thread_error_collector):
            collect_error, errors = thread_error_collector

            def background_work():
                try:
                    # do work
                except Exception as e:
                    collect_error(e)

            # If any errors collected, test fails at teardown

    Returns:
        tuple: (collect_function, errors_list)
    """
    errors = []

    def collect(error):
        errors.append(error)

    yield collect, errors

    # Assert no errors collected during test
    if errors:
        pytest.fail(f"Background thread errors: {errors}")


@pytest.fixture
def mock_slow_api_call():
    """
    Mock function that simulates slow API call.

    Returns:
        callable: Function that sleeps then returns data
    """
    def slow_call(delay=0.5):
        time.sleep(delay)
        return {"status": "success", "data": []}

    return slow_call


# Mark configurations for pytest
def pytest_configure(config):
    """Configure custom pytest marks."""
    config.addinivalue_line(
        "markers", "stress: mark test as stress test (deselect with '-m \"not stress\"')"
    )
    config.addinivalue_line(
        "markers", "performance: mark test as performance benchmark"
    )
    config.addinivalue_line(
        "markers", "threading: mark test as threading/concurrency test"
    )
