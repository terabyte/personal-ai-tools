#!/bin/bash
#
# Test script for personal-ai-tools
#
# Usage:
#   ./test.sh                    # Deep check with coverage (fails if <80%)
#   ./test.sh quick              # Quick test without coverage
#   ./test.sh long               # Verify no deadlocks (run tests 10x)
#   COVERAGE_MIN=90 ./test.sh    # Set custom coverage threshold
#

set -e  # Exit on error

# Default coverage threshold (can be overridden with env var)
# Current coverage: 93%, so we set threshold at 80% to allow some wiggle room
COVERAGE_MIN="${COVERAGE_MIN:-80}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print colored message
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Get test mode from argument (default to deep)
MODE="${1:-deep}"

case "$MODE" in
    quick)
        log_info "Running quick tests (no coverage)..."
        echo
        python3 -m pytest tests/ -v --timeout=30

        if [ $? -eq 0 ]; then
            log_success "All tests passed!"
        else
            log_error "Tests failed!"
            exit 1
        fi
        ;;

    long)
        log_info "Running long test (verify no deadlocks - 10 iterations)..."
        echo

        FAILED=0
        for i in {1..10}; do
            log_info "Iteration $i/10..."

            if ! python3 -m pytest tests/test_threading.py -v --timeout=30 -q; then
                log_error "Tests failed on iteration $i"
                FAILED=1
                break
            fi

            echo
        done

        if [ $FAILED -eq 0 ]; then
            log_success "All 10 iterations passed! No deadlocks detected."
        else
            log_error "Tests failed during long run"
            exit 1
        fi
        ;;

    deep)
        log_info "Running deep check with coverage (minimum ${COVERAGE_MIN}%)..."
        echo

        # Run tests with coverage
        python3 -m pytest tests/ -v \
            --cov=jira_view_core \
            --cov-report=term \
            --cov-report=html \
            --cov-fail-under=${COVERAGE_MIN} \
            --timeout=30

        EXIT_CODE=$?

        if [ $EXIT_CODE -eq 0 ]; then
            log_success "All tests passed with sufficient coverage (>=${COVERAGE_MIN}%)!"
            log_info "HTML coverage report: htmlcov/index.html"
        else
            log_error "Tests failed or coverage below ${COVERAGE_MIN}%!"
            log_info "Check htmlcov/index.html for detailed coverage report"
            log_info "Tip: Set COVERAGE_MIN=<percent> to adjust threshold"
            exit 1
        fi
        ;;

    *)
        log_error "Unknown mode: $MODE"
        echo
        echo "Usage:"
        echo "  ./test.sh                    # Deep check with coverage (fails if <80%)"
        echo "  ./test.sh quick              # Quick test without coverage"
        echo "  ./test.sh long               # Verify no deadlocks (run tests 10x)"
        echo "  COVERAGE_MIN=90 ./test.sh    # Set custom coverage threshold"
        exit 1
        ;;
esac

echo
log_success "Testing complete!"
