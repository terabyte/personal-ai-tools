#!/usr/bin/env python3

"""
Jira TUI - Terminal User Interface for interactive ticket viewing
Provides a split-pane interface with vim keybindings for browsing tickets
"""

import json
import sys
import subprocess
import webbrowser
import textwrap
import threading
import signal
import atexit
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple
from pathlib import Path

# Try to import curses, gracefully handle if not available
try:
    import curses
    CURSES_AVAILABLE = True
except ImportError:
    CURSES_AVAILABLE = False
    print("⚠️  curses not available - falling back to basic mode", file=sys.stderr)


class JiraTUI:
    """Interactive Terminal UI for Jira ticket viewing with vim keybindings."""

    def __init__(self, viewer, use_colors: bool):
        """Initialize TUI with reference to JiraViewer instance."""
        self.viewer = viewer
        self.use_colors = use_colors
        self.show_full = True  # Toggle for full mode (all comments/history)
        self.ticket_cache = {}  # Cache for full ticket details
        self.transitions_cache = {}  # Cache for available transitions per ticket
        self.loading_complete = False  # Track if background loading is done
        self.loading_count = 0  # Track how many tickets loaded
        self.loading_total = 0  # Track total tickets to load
        self.loading_lock = threading.Lock()  # Thread-safe cache updates
        self.legend_lines = 0  # Track how many lines the legend occupies
        self.detail_scroll_offset = 0  # Track right pane scroll position
        self.detail_total_lines = 0  # Track total lines in right pane
        self.curses_initialized = False  # Track if curses is active
        self._original_sigint_handler = None  # Store original signal handler
        self._shutdown_flag = False  # Flag to signal background threads to stop
        self.stale_tickets = set()  # Track tickets that may no longer match the query

    def _cleanup_curses(self):
        """Ensure curses is properly cleaned up."""
        if CURSES_AVAILABLE and self.curses_initialized:
            try:
                curses.endwin()
                self.curses_initialized = False
            except:
                pass

    def _sigint_handler(self, signum, frame):
        """Handle Ctrl+C gracefully."""
        self._cleanup_curses()
        # Restore original handler and re-raise
        if self._original_sigint_handler:
            signal.signal(signal.SIGINT, self._original_sigint_handler)
        sys.exit(0)

    def run(self, query_or_ticket: str) -> int:
        """
        Run the interactive TUI or fallback to basic mode.

        Args:
            query_or_ticket: Either a ticket key or JQL query

        Returns:
            Exit code (0 for success)
        """
        if not CURSES_AVAILABLE:
            return self._run_fallback(query_or_ticket)

        # Register cleanup handlers
        atexit.register(self._cleanup_curses)
        self._original_sigint_handler = signal.signal(signal.SIGINT, self._sigint_handler)

        try:
            return curses.wrapper(self._curses_main, query_or_ticket)
        except KeyboardInterrupt:
            self._cleanup_curses()
            return 0
        except Exception as e:
            self._cleanup_curses()
            print(f"❌ Error in TUI: {e}", file=sys.stderr)
            return 1
        finally:
            # Restore original signal handler
            if self._original_sigint_handler:
                signal.signal(signal.SIGINT, self._original_sigint_handler)
            atexit.unregister(self._cleanup_curses)

    def _run_fallback(self, query_or_ticket: str) -> int:
        """Fallback mode when curses is not available - show first ticket."""
        print("Running in basic mode (curses not available)...")
        print()

        # Determine if it's a ticket or query
        if self.viewer.is_ticket_key(query_or_ticket):
            # Single ticket - just display it
            ticket = self.viewer.fetch_ticket_details(query_or_ticket)
            if not ticket:
                print(f'❌ Failed to fetch ticket: {query_or_ticket}', file=sys.stderr)
                return 1
            self.viewer.display_ticket(ticket, self.use_colors, show_full=False)
        else:
            # JQL query - fetch and show first result
            fields = ['key', 'summary', 'status', 'priority', 'assignee', 'updated',
                      'customfield_10061', 'customfield_10021', 'customfield_10023']

            issues = self.viewer.utils.fetch_all_jql_results(query_or_ticket, fields)

            if not issues:
                print('No results found.')
                return 0

            print(f'Found {len(issues)} tickets. Showing first result...')
            print()

            # Show first ticket
            first_key = issues[0].get('key')
            ticket = self.viewer.fetch_ticket_details(first_key)
            if ticket:
                self.viewer.display_ticket(ticket, self.use_colors, show_full=False)

        return 0

    def _curses_main(self, stdscr, query_or_ticket: str):
        """Main curses loop."""
        # Mark curses as initialized
        self.curses_initialized = True

        # Initialize curses
        curses.curs_set(0)  # Hide cursor
        stdscr.timeout(100)  # Non-blocking input with 100ms timeout

        # Setup colors if terminal supports it
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            # Define color pairs
            curses.init_pair(1, curses.COLOR_GREEN, -1)    # Green
            curses.init_pair(2, curses.COLOR_YELLOW, -1)   # Yellow
            curses.init_pair(3, curses.COLOR_BLUE, -1)     # Blue
            curses.init_pair(4, curses.COLOR_RED, -1)      # Red
            curses.init_pair(5, curses.COLOR_CYAN, -1)     # Cyan
            curses.init_pair(6, curses.COLOR_MAGENTA, -1)  # Magenta
            curses.init_pair(7, curses.COLOR_WHITE, -1)    # White/bright

        # Fetch tickets with progress display
        stdscr.addstr(0, 0, "Fetching tickets...")
        stdscr.refresh()

        def progress_callback(fetched, total):
            """Update screen with fetch progress."""
            stdscr.clear()
            stdscr.addstr(0, 0, f"Fetching tickets: {fetched}/{total}...")
            stdscr.refresh()

        tickets, single_ticket_mode = self._fetch_tickets(query_or_ticket, progress_callback)

        if not tickets:
            stdscr.clear()
            stdscr.addstr(0, 0, "No tickets found. Press any key to exit.")
            stdscr.refresh()
            stdscr.getch()
            return 0

        # Cache all tickets immediately (we already have full data from JQL query)
        stdscr.addstr(0, 0, "Loading tickets...")
        stdscr.refresh()

        with self.loading_lock:
            for ticket in tickets:
                ticket_key = ticket.get('key')
                if ticket_key:
                    # Store the full ticket data (already includes all fields from JQL query)
                    self.ticket_cache[ticket_key] = ticket
            self.loading_count = len(tickets)
            self.loading_total = len(tickets)
            self.loading_complete = True

        # Start background thread to cache transitions for all tickets (for T key feature)
        if tickets:
            thread = threading.Thread(target=self._load_transitions_background, args=(tickets,), daemon=True)
            thread.start()

        stdscr.clear()

        # State management
        current_query = query_or_ticket
        all_tickets = tickets  # Unfiltered list for search/restore
        selected_idx = 0
        scroll_offset = 0
        search_query = ""
        show_help = False

        while True:
            # Get terminal dimensions
            height, width = stdscr.getmaxyx()

            # Clear screen
            stdscr.clear()

            # Show help overlay if requested
            if show_help:
                self._draw_help(stdscr, height, width)
                key = stdscr.getch()
                if key != -1:  # Any key dismisses help
                    show_help = False
                continue

            # Calculate pane dimensions (1/5 to 1/4 of screen width)
            list_width = max(width // 5, min(width // 4, 70))
            detail_x = list_width + 1
            detail_width = width - detail_x

            # Draw vertical separator
            for y in range(height - 1):
                try:
                    stdscr.addch(y, list_width, curses.ACS_VLINE)
                except curses.error:
                    pass

            # Draw ticket list in left pane
            self._draw_ticket_list(stdscr, tickets, selected_idx, scroll_offset,
                                   height - 2, list_width, search_query)

            # Draw ticket details in right pane
            if tickets and selected_idx < len(tickets):
                current_ticket_key = tickets[selected_idx].get('key')
                self._draw_ticket_details(stdscr, current_ticket_key, detail_x,
                                         height - 2, detail_width)

            # Draw status bar at bottom
            self._draw_status_bar(stdscr, height - 1, width, selected_idx + 1,
                                 len(tickets), search_query)

            stdscr.refresh()

            # Handle input
            key = stdscr.getch()

            if key == -1:  # No input (timeout)
                continue
            elif key == ord('q'):  # Quit
                self._shutdown_flag = True
                break
            elif key == ord('j') or key == curses.KEY_DOWN:  # Down
                if selected_idx < len(tickets) - 1:
                    selected_idx += 1
                    # Auto-scroll if needed
                    visible_height = self._get_visible_height(height)
                    if selected_idx >= scroll_offset + visible_height:
                        scroll_offset = selected_idx - visible_height + 1
                    # Reset detail scroll when changing tickets
                    self.detail_scroll_offset = 0
            elif key == ord('k') or key == curses.KEY_UP:  # Up
                if selected_idx > 0:
                    selected_idx -= 1
                    # Auto-scroll if needed
                    if selected_idx < scroll_offset:
                        scroll_offset = selected_idx
                    # Reset detail scroll when changing tickets
                    self.detail_scroll_offset = 0
            elif key == 10:  # Ctrl+J or Enter - Scroll detail pane down
                detail_height = height - 2
                if self.detail_scroll_offset + detail_height < self.detail_total_lines:
                    self.detail_scroll_offset += 1
            elif key == 11 or key == ord('\\'):  # Ctrl+K or \ - Scroll detail pane up
                if self.detail_scroll_offset > 0:
                    self.detail_scroll_offset -= 1
            elif key == 2:  # Ctrl+B - Scroll detail pane down half-page
                detail_height = height - 2
                half_page = max(1, detail_height // 2)
                if self.detail_scroll_offset + detail_height < self.detail_total_lines:
                    self.detail_scroll_offset = min(
                        self.detail_scroll_offset + half_page,
                        max(0, self.detail_total_lines - detail_height)
                    )
            elif key == 21:  # Ctrl+U - Scroll detail pane up half-page
                detail_height = height - 2
                half_page = max(1, detail_height // 2)
                self.detail_scroll_offset = max(0, self.detail_scroll_offset - half_page)
            elif key == ord('g'):  # Go to top
                selected_idx = 0
                scroll_offset = 0
                self.detail_scroll_offset = 0
            elif key == ord('G'):  # Go to bottom
                selected_idx = len(tickets) - 1
                visible_height = self._get_visible_height(height)
                scroll_offset = max(0, len(tickets) - visible_height)
                self.detail_scroll_offset = 0
            elif key == ord('r'):  # Refresh
                # Remember currently selected ticket
                current_ticket_key = tickets[selected_idx].get('key') if tickets and selected_idx < len(tickets) else None

                all_tickets, _ = self._fetch_tickets(current_query)
                tickets = all_tickets

                # Try to find the previously selected ticket in refreshed results
                if current_ticket_key:
                    new_idx = None
                    for i, ticket in enumerate(tickets):
                        if ticket.get('key') == current_ticket_key:
                            new_idx = i
                            break

                    if new_idx is not None:
                        # Ticket still exists, select it
                        selected_idx = new_idx
                    else:
                        # Ticket no longer in results, select closest ticket
                        selected_idx = min(selected_idx, len(tickets) - 1) if tickets else 0
                else:
                    selected_idx = min(selected_idx, len(tickets) - 1) if tickets else 0

                # Adjust scroll position to keep selected item visible
                visible_height = self._get_visible_height(height)
                if selected_idx < scroll_offset:
                    scroll_offset = selected_idx
                elif selected_idx >= scroll_offset + visible_height:
                    scroll_offset = selected_idx - visible_height + 1

                # Clear cache and cache all tickets immediately
                stdscr.addstr(0, 0, "Refreshing tickets...")
                stdscr.refresh()

                self.ticket_cache.clear()
                with self.loading_lock:
                    for ticket in all_tickets:
                        ticket_key = ticket.get('key')
                        if ticket_key:
                            self.ticket_cache[ticket_key] = ticket
                    self.loading_count = len(all_tickets)
                    self.loading_total = len(all_tickets)
                    self.loading_complete = True

                # Clear stale tickets after refresh
                self.stale_tickets.clear()

                # Reload transitions in background
                if all_tickets:
                    thread = threading.Thread(target=self._load_transitions_background, args=(all_tickets,), daemon=True)
                    thread.start()

                # Re-apply search filter if active
                if search_query:
                    tickets = self._filter_tickets(all_tickets, search_query)
                    # Try to maintain selection after filter
                    if current_ticket_key:
                        new_idx = None
                        for i, ticket in enumerate(tickets):
                            if ticket.get('key') == current_ticket_key:
                                new_idx = i
                                break
                        if new_idx is not None:
                            selected_idx = new_idx
                        else:
                            selected_idx = min(selected_idx, len(tickets) - 1) if tickets else 0
                    else:
                        selected_idx = min(selected_idx, len(tickets) - 1) if tickets else 0
            elif key == ord('F'):  # Toggle full mode (capital F)
                self.show_full = not self.show_full
            elif key == ord('v'):  # Open in browser
                if tickets and selected_idx < len(tickets):
                    current_key = tickets[selected_idx].get('key')
                    self._open_in_browser(current_key)
            elif key == ord('/'):  # Search
                # Remember current ticket key before filtering
                current_ticket_key = tickets[selected_idx].get('key') if tickets and selected_idx < len(tickets) else None

                search_query = self._get_search_input(stdscr, height - 1, width)

                # Filter tickets by search query or restore full list if empty
                if search_query:
                    tickets = self._filter_tickets(all_tickets, search_query)
                    selected_idx = 0
                    scroll_offset = 0
                else:
                    # Restore full list and try to re-select the same ticket
                    tickets = all_tickets
                    if current_ticket_key:
                        # Find the ticket in the full list
                        for i, ticket in enumerate(tickets):
                            if ticket.get('key') == current_ticket_key:
                                selected_idx = i
                                break
                        else:
                            selected_idx = 0
                    else:
                        selected_idx = 0

                    # Adjust scroll to keep selected item visible
                    visible_height = self._get_visible_height(height)
                    if selected_idx < scroll_offset:
                        scroll_offset = selected_idx
                    elif selected_idx >= scroll_offset + visible_height:
                        scroll_offset = max(0, selected_idx - visible_height + 1)
            elif key == ord('t') or key == ord('T'):  # Transition
                if tickets and selected_idx < len(tickets):
                    current_key = tickets[selected_idx].get('key')
                    self._handle_transition(stdscr, current_key, height, width)
                    # Mark as stale since transition may affect query match
                    self.stale_tickets.add(current_key)
                    # Refresh current ticket after transition (both cache and list)
                    full_ticket = self.viewer.fetch_ticket_details(current_key)
                    if full_ticket:
                        with self.loading_lock:
                            self.ticket_cache[current_key] = full_ticket
                        # Update the tickets list entry so left pane shows updated data
                        tickets[selected_idx] = {'key': full_ticket.get('key'), 'fields': full_ticket.get('fields', {})}
                        if tickets is not all_tickets:
                            # Also update all_tickets if we're in filtered view
                            for i, t in enumerate(all_tickets):
                                if t.get('key') == current_key:
                                    all_tickets[i] = tickets[selected_idx]
                                    break
            elif key == ord('c') or key == ord('C'):  # Comment
                if tickets and selected_idx < len(tickets):
                    current_key = tickets[selected_idx].get('key')
                    self._handle_comment(stdscr, current_key, height, width)
                    # Refresh current ticket after comment (both cache and list)
                    full_ticket = self.viewer.fetch_ticket_details(current_key)
                    if full_ticket:
                        with self.loading_lock:
                            self.ticket_cache[current_key] = full_ticket
                        # Update the tickets list entry so left pane shows updated data
                        tickets[selected_idx] = {'key': full_ticket.get('key'), 'fields': full_ticket.get('fields', {})}
                        if tickets is not all_tickets:
                            # Also update all_tickets if we're in filtered view
                            for i, t in enumerate(all_tickets):
                                if t.get('key') == current_key:
                                    all_tickets[i] = tickets[selected_idx]
                                    break
            elif key == ord('f'):  # Flags (lowercase f)
                if tickets and selected_idx < len(tickets):
                    current_key = tickets[selected_idx].get('key')
                    self._handle_flags(stdscr, current_key, height, width)
                    # Mark as stale since flag change may affect query match
                    self.stale_tickets.add(current_key)
                    # Refresh current ticket after flag change (both cache and list)
                    full_ticket = self.viewer.fetch_ticket_details(current_key)
                    if full_ticket:
                        with self.loading_lock:
                            self.ticket_cache[current_key] = full_ticket
                        # Update the tickets list entry so left pane shows updated data
                        tickets[selected_idx] = {'key': full_ticket.get('key'), 'fields': full_ticket.get('fields', {})}
                        if tickets is not all_tickets:
                            # Also update all_tickets if we're in filtered view
                            for i, t in enumerate(all_tickets):
                                if t.get('key') == current_key:
                                    all_tickets[i] = tickets[selected_idx]
                                    break
            elif key == ord('e') or key == ord('E'):  # Edit issue
                if tickets and selected_idx < len(tickets):
                    current_key = tickets[selected_idx].get('key')
                    self._handle_edit_issue(stdscr, current_key, height, width)
                    # Mark as stale since edit may affect query match
                    self.stale_tickets.add(current_key)
                    # Refresh current ticket after edit (both cache and list)
                    full_ticket = self.viewer.fetch_ticket_details(current_key)
                    if full_ticket:
                        with self.loading_lock:
                            self.ticket_cache[current_key] = full_ticket
                        # Update the tickets list entry so left pane shows updated data
                        tickets[selected_idx] = {'key': full_ticket.get('key'), 'fields': full_ticket.get('fields', {})}
                        if tickets is not all_tickets:
                            # Also update all_tickets if we're in filtered view
                            for i, t in enumerate(all_tickets):
                                if t.get('key') == current_key:
                                    all_tickets[i] = tickets[selected_idx]
                                    break
            elif key == ord('n') or key == ord('N'):  # New issue
                new_ticket_key = self._handle_new_issue(stdscr, current_query, height, width)
                if new_ticket_key:
                    # Switch to viewing the new ticket
                    new_query = new_ticket_key
                    stdscr.addstr(0, 0, f"Loading {new_ticket_key}...")
                    stdscr.refresh()

                    try:
                        tickets, single_ticket_mode = self._fetch_tickets(new_query)
                        if tickets:
                            # Reset state
                            current_query = new_query
                            all_tickets = tickets
                            selected_idx = 0
                            scroll_offset = 0
                            search_query = ""

                            # Clear caches and stale tickets
                            self.ticket_cache.clear()
                            self.transitions_cache.clear()
                            self.stale_tickets.clear()

                            # Cache all tickets immediately
                            with self.loading_lock:
                                for ticket in tickets:
                                    ticket_key = ticket.get('key')
                                    if ticket_key:
                                        self.ticket_cache[ticket_key] = ticket
                                self.loading_count = len(tickets)
                                self.loading_total = len(tickets)
                                self.loading_complete = True

                            # Restart background transition loading
                            thread = threading.Thread(target=self._load_transitions_background, args=(tickets,), daemon=True)
                            thread.start()
                    except Exception as e:
                        self._show_message(stdscr, f"✗ Error loading new ticket: {str(e)}", height, width)
            elif key == ord('s'):  # New query
                is_edit_mode = False
                new_query = self._handle_query_change(stdscr, current_query, is_edit_mode, height, width)
                if new_query:
                    # Re-fetch tickets with new query
                    stdscr.addstr(0, 0, "Loading tickets...")
                    stdscr.refresh()

                    try:
                        tickets, single_ticket_mode = self._fetch_tickets(new_query)
                        if tickets:
                            # Reset state
                            current_query = new_query
                            all_tickets = tickets
                            selected_idx = 0
                            scroll_offset = 0
                            search_query = ""

                            # Clear caches and stale tickets
                            self.ticket_cache.clear()
                            self.transitions_cache.clear()
                            self.stale_tickets.clear()

                            # Cache all tickets immediately
                            with self.loading_lock:
                                for ticket in tickets:
                                    ticket_key = ticket.get('key')
                                    if ticket_key:
                                        self.ticket_cache[ticket_key] = ticket
                                self.loading_count = len(tickets)
                                self.loading_total = len(tickets)
                                self.loading_complete = True

                            # Restart background transition loading
                            thread = threading.Thread(target=self._load_transitions_background, args=(tickets,), daemon=True)
                            thread.start()

                            self._show_message(stdscr, f"✓ Loaded {len(tickets)} tickets", height, width)
                        else:
                            self._show_message(stdscr, "No tickets found", height, width)
                    except Exception as e:
                        self._show_message(stdscr, f"✗ Error: {str(e)}", height, width)
            elif key == ord('S'):  # Edit query
                is_edit_mode = True
                new_query = self._handle_query_change(stdscr, current_query, is_edit_mode, height, width)
                if new_query:
                    # Re-fetch tickets with new query
                    stdscr.addstr(0, 0, "Loading tickets...")
                    stdscr.refresh()

                    try:
                        tickets, single_ticket_mode = self._fetch_tickets(new_query)
                        if tickets:
                            # Reset state
                            current_query = new_query
                            all_tickets = tickets
                            selected_idx = 0
                            scroll_offset = 0
                            search_query = ""

                            # Clear caches and stale tickets
                            self.ticket_cache.clear()
                            self.transitions_cache.clear()
                            self.stale_tickets.clear()

                            # Cache all tickets immediately
                            with self.loading_lock:
                                for ticket in tickets:
                                    ticket_key = ticket.get('key')
                                    if ticket_key:
                                        self.ticket_cache[ticket_key] = ticket
                                self.loading_count = len(tickets)
                                self.loading_total = len(tickets)
                                self.loading_complete = True

                            # Restart background transition loading
                            thread = threading.Thread(target=self._load_transitions_background, args=(tickets,), daemon=True)
                            thread.start()

                            self._show_message(stdscr, f"✓ Loaded {len(tickets)} tickets", height, width)
                        else:
                            self._show_message(stdscr, "No tickets found", height, width)
                    except Exception as e:
                        self._show_message(stdscr, f"✗ Error: {str(e)}", height, width)
            elif key == ord('?'):  # Help
                show_help = True

        return 0

    def _sort_tickets(self, tickets: List[dict], query: str) -> List[dict]:
        """
        Sort tickets based on query.

        If query contains ORDER BY, preserve API order.
        Otherwise, sort by key ascending alphabetically.

        Args:
            tickets: List of ticket dictionaries
            query: The JQL query string

        Returns:
            Sorted list of tickets
        """
        # Check if query has ORDER BY clause (case insensitive)
        if 'order by' in query.lower():
            # Preserve order from API
            return tickets
        else:
            # Sort by key ascending alphabetically
            return sorted(tickets, key=lambda t: t.get('key', ''))

    def _fetch_tickets(self, query_or_ticket: str, progress_callback=None) -> tuple:
        """
        Fetch tickets from Jira.

        Args:
            query_or_ticket: Ticket key or JQL query
            progress_callback: Optional callback for progress updates

        Returns:
            Tuple of (ticket_list, is_single_ticket)
        """
        if self.viewer.is_ticket_key(query_or_ticket):
            # Single ticket - wrap in list
            ticket = self.viewer.fetch_ticket_details(query_or_ticket)
            if ticket:
                # Return just the key and summary for list view
                return [{'key': ticket.get('key'),
                        'fields': ticket.get('fields', {})}], True
            return [], True
        else:
            # JQL query - fetch all fields needed for detail view to avoid per-ticket API calls
            fields = [
                'key', 'summary', 'status', 'priority', 'assignee', 'updated',
                'customfield_10061',  # Story points
                'customfield_10021',  # Sprint
                'customfield_10023',  # Flags
                'description', 'reporter', 'created', 'issuetype', 'labels',
                'parent', 'issuelinks', 'comment', 'resolution'
            ]
            issues = self.viewer.utils.fetch_all_jql_results(
                query_or_ticket, fields, expand='changelog', progress_callback=progress_callback
            )

            # Apply consistent sorting
            issues = self._sort_tickets(issues, query_or_ticket)

            return issues, False

    def _fetch_single_ticket(self, ticket_key: str) -> Optional[dict]:
        """Fetch a single ticket's full details."""
        return self.viewer.fetch_ticket_details(ticket_key)

    def _fetch_transitions(self, ticket_key: str) -> List[dict]:
        """Fetch available transitions for a ticket."""
        try:
            endpoint = f"/issue/{ticket_key}/transitions"
            response = self.viewer.utils.call_jira_api(endpoint)
            if response and 'transitions' in response:
                return response['transitions']
        except Exception:
            pass
        return []

    def _load_tickets_background(self, tickets: List[dict]) -> None:
        """Background thread to load ticket details with parallel fetching."""
        max_workers = 5  # Fetch up to 5 tickets concurrently

        # Use thread pool to fetch tickets in parallel
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            # Submit all fetch tasks
            future_to_key = {
                executor.submit(self._fetch_single_ticket, ticket.get('key')): ticket.get('key')
                for ticket in tickets if ticket.get('key')
            }

            # Process results as they complete
            for future in as_completed(future_to_key):
                # Check if we should shutdown
                if self._shutdown_flag:
                    break

                ticket_key = future_to_key[future]
                try:
                    full_ticket = future.result()
                    if full_ticket:
                        with self.loading_lock:
                            self.ticket_cache[ticket_key] = full_ticket
                            self.loading_count += 1

                        # Only fetch transitions if not shutting down
                        if not self._shutdown_flag:
                            executor.submit(self._cache_transitions, ticket_key)
                except Exception:
                    # Skip failed tickets
                    pass
        finally:
            # Shutdown executor without waiting for pending tasks
            executor.shutdown(wait=False)

        # Mark loading as complete
        with self.loading_lock:
            self.loading_complete = True

    def _cache_transitions(self, ticket_key: str) -> None:
        """Cache transitions for a ticket."""
        # Don't fetch if shutting down
        if self._shutdown_flag:
            return
        transitions = self._fetch_transitions(ticket_key)
        with self.loading_lock:
            self.transitions_cache[ticket_key] = transitions

    def _load_transitions_background(self, tickets: List[dict]) -> None:
        """Background thread to load transitions for all tickets."""
        max_workers = 5  # Fetch up to 5 transitions concurrently

        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            # Submit all transition fetch tasks
            for ticket in tickets:
                # Check if we should shutdown
                if self._shutdown_flag:
                    break
                ticket_key = ticket.get('key')
                if ticket_key:
                    executor.submit(self._cache_transitions, ticket_key)
        finally:
            # Shutdown executor without waiting for pending tasks
            executor.shutdown(wait=False)

    def _handle_transition(self, stdscr, ticket_key: str, height: int, width: int):
        """Handle ticket transition (T key)."""
        # Get transitions with field info from API
        try:
            endpoint = f"/issue/{ticket_key}/transitions?expand=transitions.fields"
            response = self.viewer.utils.call_jira_api(endpoint)
            if not response or 'transitions' not in response:
                self._show_message(stdscr, "No transitions available", height, width)
                return
            transitions = response['transitions']
        except Exception:
            self._show_message(stdscr, "Failed to fetch transitions", height, width)
            return

        if not transitions:
            self._show_message(stdscr, "No transitions available", height, width)
            return

        # Draw transition selection overlay - ensure we have room for all transitions
        max_visible = min(len(transitions), height - 8)  # Leave room for box, title, input
        overlay_height = max_visible + 5  # +5 for box, title, input line, margins
        overlay_width = min(60, width - 4)
        start_y = (height - overlay_height) // 2
        start_x = (width - overlay_width) // 2

        # Create window for overlay
        try:
            overlay = curses.newwin(overlay_height, overlay_width, start_y, start_x)
            overlay.box()
            overlay.addstr(0, 2, " Select Transition ", curses.A_BOLD)

            # List transitions (show warning if truncated)
            for idx in range(max_visible):
                transition = transitions[idx]
                name = transition.get('name', 'Unknown')
                # Show target state name
                to_status = transition.get('to', {}).get('name', '')
                display = f"{idx + 1}. {name} -> {to_status}"
                overlay.addstr(idx + 2, 2, display[:overlay_width - 6])

            if len(transitions) > max_visible:
                overlay.addstr(max_visible + 2, 2, f"... and {len(transitions) - max_visible} more")

            overlay.addstr(overlay_height - 2, 2, "Enter number or q to cancel: ")
            overlay.refresh()

            # Get user input
            curses.echo()
            input_str = ""
            while True:
                ch = overlay.getch()
                if ch == ord('q') or ch == 27:  # q or ESC
                    curses.noecho()
                    return
                elif ch == ord('\n'):
                    break
                elif ch in [curses.KEY_BACKSPACE, 127, 8]:
                    input_str = input_str[:-1]
                elif chr(ch).isdigit():
                    input_str += chr(ch)

            curses.noecho()

            # Perform transition
            try:
                choice = int(input_str)
                if 1 <= choice <= len(transitions):
                    transition = transitions[choice - 1]
                    transition_id = transition.get('id')
                    transition_name = transition.get('name')

                    # Build payload with transition ID
                    payload = {"transition": {"id": transition_id}}

                    # Check for required fields
                    fields = transition.get('fields', {})
                    transition_fields = {}

                    # Check if resolution is required
                    is_closing = False
                    if 'resolution' in fields and fields['resolution'].get('required'):
                        # Prompt for resolution
                        resolutions = fields['resolution'].get('allowedValues', [])
                        resolution_id = self._prompt_for_resolution(stdscr, resolutions, height, width)
                        if resolution_id is None:
                            return  # User cancelled
                        transition_fields['resolution'] = {'id': resolution_id}
                        is_closing = True

                    # Check if Current Issue Owner is required (customfield_11684)
                    if 'customfield_11684' in fields and fields['customfield_11684'].get('required'):
                        # Get current user's account ID
                        try:
                            me_response = self.viewer.utils.call_jira_api("/myself")
                            if me_response and 'accountId' in me_response:
                                transition_fields['customfield_11684'] = {'accountId': me_response['accountId']}
                        except:
                            pass

                    # Add fields to payload if any were collected
                    if transition_fields:
                        payload['fields'] = transition_fields

                    # If closing, prompt for optional comment
                    comment_text = None
                    if is_closing:
                        add_comment = self._prompt_yes_no(stdscr, "Add a comment?", height, width)
                        if add_comment:
                            comment_text = self._prompt_for_comment_vim(stdscr, ticket_key, height, width)
                            if comment_text is None:
                                # User cancelled the comment, ask if they still want to proceed
                                proceed = self._prompt_yes_no(stdscr, "Continue without comment?", height, width)
                                if not proceed:
                                    return

                    # Call Jira API to perform transition
                    endpoint = f"/issue/{ticket_key}/transitions"

                    # Call jira-api directly so we can capture error messages
                    try:
                        cmd = [str(self.viewer.utils.jira_api), 'POST', endpoint, '-d', json.dumps(payload)]
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

                        # Check for errors in JSON response (jira-api returns exit 0 even on HTTP errors)
                        error_msg = None
                        if result.stdout.strip():
                            try:
                                response_json = json.loads(result.stdout)
                                if 'errorMessages' in response_json and response_json['errorMessages']:
                                    error_msg = response_json['errorMessages'][0]
                                elif 'errors' in response_json and response_json['errors']:
                                    # errors is a dict like {"field": "error message"}
                                    errors_dict = response_json['errors']
                                    error_msg = "; ".join([f"{k}: {v}" for k, v in errors_dict.items()])
                            except:
                                pass

                        if result.returncode == 0 and error_msg is None:
                            # Transition succeeded, now add comment if provided
                            if comment_text:
                                self._add_comment_to_ticket(ticket_key, comment_text)
                            self._show_message(stdscr, f"✓ Transitioned to {transition_name}", height, width)
                        else:
                            if error_msg is None:
                                error_msg = result.stderr.strip() or "Unknown error"
                            self._show_message(stdscr, f"✗ Transition failed: {error_msg[:80]}", height, width, duration=5000)
                    except subprocess.TimeoutExpired:
                        self._show_message(stdscr, "✗ Transition timed out", height, width)
                    except Exception as e:
                        self._show_message(stdscr, f"✗ Error: {str(e)[:80]}", height, width)
                else:
                    self._show_message(stdscr, "Invalid choice", height, width)
            except (ValueError, KeyError):
                self._show_message(stdscr, "Invalid input", height, width)

        except curses.error:
            pass

    def _prompt_for_resolution(self, stdscr, resolutions: list, height: int, width: int) -> Optional[str]:
        """Prompt user to select a resolution. Returns resolution ID or None if cancelled."""
        if not resolutions:
            return None

        # Draw resolution selection overlay
        max_visible = min(len(resolutions), height - 8)
        overlay_height = max_visible + 5
        overlay_width = min(60, width - 4)
        start_y = (height - overlay_height) // 2
        start_x = (width - overlay_width) // 2

        try:
            overlay = curses.newwin(overlay_height, overlay_width, start_y, start_x)
            overlay.box()
            overlay.addstr(0, 2, " Select Resolution ", curses.A_BOLD)

            # List resolutions
            for idx in range(max_visible):
                resolution = resolutions[idx]
                name = resolution.get('name', 'Unknown')
                overlay.addstr(idx + 2, 2, f"{idx + 1}. {name[:overlay_width - 6]}")

            if len(resolutions) > max_visible:
                overlay.addstr(max_visible + 2, 2, f"... and {len(resolutions) - max_visible} more")

            overlay.addstr(overlay_height - 2, 2, "Enter number or q to cancel: ")
            overlay.refresh()

            # Get user input
            curses.echo()
            input_str = ""
            while True:
                ch = overlay.getch()
                if ch == ord('q') or ch == 27:  # q or ESC
                    curses.noecho()
                    return None
                elif ch == ord('\n'):
                    break
                elif ch in [curses.KEY_BACKSPACE, 127, 8]:
                    input_str = input_str[:-1]
                elif chr(ch).isdigit():
                    input_str += chr(ch)

            curses.noecho()

            # Return selected resolution ID
            try:
                choice = int(input_str)
                if 1 <= choice <= len(resolutions):
                    return resolutions[choice - 1].get('id')
            except ValueError:
                pass

            return None

        except curses.error:
            return None

    def _prompt_yes_no(self, stdscr, question: str, height: int, width: int) -> bool:
        """Prompt user with yes/no question. Returns True for yes, False for no."""
        msg_width = min(len(question) + 14, width - 4)  # Extra space for " (y/n): "
        msg_height = 3
        start_y = (height - msg_height) // 2
        start_x = (width - msg_width) // 2

        try:
            overlay = curses.newwin(msg_height, msg_width, start_y, start_x)
            overlay.box()
            overlay.addstr(1, 2, f"{question} (y/n): "[:msg_width - 4])
            overlay.refresh()

            while True:
                ch = overlay.getch()
                if ch == ord('y') or ch == ord('Y'):
                    return True
                elif ch == ord('n') or ch == ord('N') or ch == ord('q') or ch == 27:
                    return False
        except curses.error:
            return False

    def _prompt_for_comment_vim(self, stdscr, ticket_key: str, height: int, width: int) -> Optional[str]:
        """Prompt for comment using vim editor. Returns comment text or None if cancelled."""
        import tempfile
        import os

        # Get ticket details for context
        with self.loading_lock:
            ticket = self.ticket_cache.get(ticket_key)

        if not ticket:
            ticket = self.viewer.fetch_ticket_details(ticket_key)

        if not ticket:
            return None

        # Create temp file with ticket info as comments
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            temp_path = f.name
            fields = ticket.get('fields', {})

            f.write(f"# Ticket: {ticket_key}\n")
            f.write(f"# Summary: {fields.get('summary', '')}\n")
            f.write(f"# Status: {(fields.get('status') or {}).get('name', '')}\n")
            f.write(f"# Assignee: {(fields.get('assignee') or {}).get('displayName', 'Unassigned')}\n")
            f.write(f"#\n")
            f.write(f"# Enter your comment below (lines starting with # will be ignored):\n")
            f.write(f"#\n")
            f.write("\n")

        # Open vim editor
        curses.def_prog_mode()
        curses.endwin()

        try:
            subprocess.call(['vim', temp_path])
        finally:
            curses.reset_prog_mode()
            stdscr.refresh()

        # Read comment from file
        try:
            with open(temp_path, 'r') as f:
                lines = f.readlines()

            # Remove comment lines and empty trailing lines
            comment_lines = [line.rstrip() for line in lines if not line.strip().startswith('#')]
            comment_text = '\n'.join(comment_lines).strip()

            # Clean up temp file
            os.unlink(temp_path)

            return comment_text if comment_text else None

        except Exception:
            return None

    def _add_comment_to_ticket(self, ticket_key: str, comment_text: str) -> bool:
        """Add a comment to a ticket. Returns True if successful."""
        try:
            # Convert plain text to Atlassian Document Format (ADF)
            adf_body = self._text_to_adf(comment_text)

            # Post comment via Jira API
            endpoint = f"/issue/{ticket_key}/comment"
            payload = {"body": adf_body}
            response = self.viewer.utils.call_jira_api(endpoint, method='POST', data=payload)

            return response is not None
        except Exception:
            return False

    def _handle_flags(self, stdscr, ticket_key: str, height: int, width: int):
        """Handle flag toggling (f key)."""
        # Get current ticket to see existing flags
        with self.loading_lock:
            ticket = self.ticket_cache.get(ticket_key)

        if not ticket:
            ticket = self.viewer.fetch_ticket_details(ticket_key)

        if not ticket:
            self._show_message(stdscr, "Failed to load ticket", height, width)
            return

        # Get current flags (handle None value after clearing flags)
        current_flags = ticket.get('fields', {}).get('customfield_10023') or []
        current_flag_ids = {flag.get('id') for flag in current_flags if isinstance(flag, dict)}

        # Available flag options (currently only Impediment is known)
        # Structure: [(id, value), ...]
        available_flags = [
            ("10019", "Impediment"),
        ]

        # Track which flags are selected (start with current state)
        selected_flags = set(current_flag_ids)

        # Draw flag selection overlay
        overlay_height = min(len(available_flags) + 6, height - 4)
        overlay_width = min(60, width - 4)
        start_y = (height - overlay_height) // 2
        start_x = (width - overlay_width) // 2

        cursor_pos = 0

        try:
            overlay = curses.newwin(overlay_height, overlay_width, start_y, start_x)

            while True:
                overlay.clear()
                overlay.box()
                overlay.addstr(0, 2, " Toggle Flags ", curses.A_BOLD)

                # List flags with checkboxes
                for idx, (flag_id, flag_value) in enumerate(available_flags):
                    is_checked = flag_id in selected_flags
                    checkbox = "[x]" if is_checked else "[ ]"

                    # Highlight cursor position
                    attr = curses.A_REVERSE if idx == cursor_pos else curses.A_NORMAL

                    flag_line = f" {checkbox} {flag_value}"
                    overlay.addstr(idx + 2, 2, flag_line[:overlay_width - 4], attr)

                # Instructions
                overlay.addstr(overlay_height - 3, 2, "Space: toggle  Enter: save  q: cancel")
                overlay.refresh()

                # Get user input
                ch = overlay.getch()

                if ch == ord('q') or ch == 27:  # q or ESC
                    return
                elif ch == ord(' '):  # Space to toggle
                    flag_id, _ = available_flags[cursor_pos]
                    if flag_id in selected_flags:
                        selected_flags.remove(flag_id)
                    else:
                        selected_flags.add(flag_id)
                elif ch in [curses.KEY_DOWN, ord('j')]:
                    cursor_pos = min(cursor_pos + 1, len(available_flags) - 1)
                elif ch in [curses.KEY_UP, ord('k')]:
                    cursor_pos = max(cursor_pos - 1, 0)
                elif ch == ord('\n'):  # Enter to save
                    # Build the new flags array
                    new_flags = [{"id": fid} for fid in selected_flags]

                    # Call Jira API to update flags
                    endpoint = f"/issue/{ticket_key}"
                    payload = {
                        "fields": {
                            "customfield_10023": new_flags
                        }
                    }

                    response = self.viewer.utils.call_jira_api(endpoint, method='PUT', data=payload)

                    # Check if the response contains errors
                    if response is None:
                        self._show_message(stdscr, "✗ Failed to update flags", height, width)
                    elif response.get('errors') or response.get('errorMessages'):
                        # Extract error message
                        errors = response.get('errors', {})
                        error_messages = response.get('errorMessages', [])
                        if errors:
                            # Get first error message from the errors dict
                            field_errors = list(errors.values())
                            error_text = field_errors[0] if field_errors else "Unknown error"
                        else:
                            error_text = error_messages[0] if error_messages else "Unknown error"
                        self._show_message(stdscr, f"✗ Failed: {error_text}", height, width)
                    else:
                        # Success
                        flag_names = [val for fid, val in available_flags if fid in selected_flags]
                        if flag_names:
                            self._show_message(stdscr, f"✓ Flags set: {', '.join(flag_names)}", height, width)
                        else:
                            self._show_message(stdscr, "✓ Flags cleared", height, width)

                    return

        except curses.error:
            pass

    def _handle_comment(self, stdscr, ticket_key: str, height: int, width: int):
        """Handle adding a comment (C key)."""
        import tempfile
        import subprocess

        # Get ticket details for context
        with self.loading_lock:
            ticket = self.ticket_cache.get(ticket_key)

        if not ticket:
            ticket = self.viewer.fetch_ticket_details(ticket_key)

        if not ticket:
            self._show_message(stdscr, "Failed to load ticket", height, width)
            return

        # Create temp file with ticket info as comments
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            temp_path = f.name
            fields = ticket.get('fields', {})

            f.write(f"# Ticket: {ticket_key}\n")
            f.write(f"# Summary: {fields.get('summary', '')}\n")
            f.write(f"# Status: {(fields.get('status') or {}).get('name', '')}\n")
            f.write(f"# Assignee: {(fields.get('assignee') or {}).get('displayName', 'Unassigned')}\n")
            f.write(f"#\n")
            f.write(f"# Enter your comment below (lines starting with # will be ignored):\n")
            f.write(f"#\n")
            f.write("\n")

        # Open vim editor
        curses.def_prog_mode()
        curses.endwin()

        try:
            subprocess.call(['vim', temp_path])
        finally:
            curses.reset_prog_mode()
            stdscr.refresh()

        # Read comment from file
        try:
            with open(temp_path, 'r') as f:
                lines = f.readlines()

            # Remove comment lines and empty trailing lines
            comment_lines = [line.rstrip() for line in lines if not line.strip().startswith('#')]
            comment_text = '\n'.join(comment_lines).strip()

            # Clean up temp file
            import os
            os.unlink(temp_path)

            if not comment_text:
                self._show_message(stdscr, "Comment cancelled (empty)", height, width)
                return

            # Convert plain text to Atlassian Document Format (ADF)
            adf_body = self._text_to_adf(comment_text)

            # Post comment via Jira API
            endpoint = f"/issue/{ticket_key}/comment"
            payload = {"body": adf_body}
            response = self.viewer.utils.call_jira_api(endpoint, method='POST', data=payload)

            if response is not None:
                self._show_message(stdscr, "✓ Comment added", height, width)
            else:
                self._show_message(stdscr, "✗ Failed to add comment", height, width)

        except Exception as e:
            self._show_message(stdscr, f"✗ Error: {str(e)}", height, width)

    def _handle_new_issue(self, stdscr, current_query: str, height: int, width: int) -> Optional[str]:
        """Handle creating a new issue (n key). Returns new ticket key or None."""
        import tempfile
        import subprocess
        import os

        # Extract project from current query or default to CIPLAT
        project = self._extract_project_from_query(current_query) or "CIPLAT"

        error_message = None
        previous_fields = None
        while True:
            # Create template (with previous fields if retrying after error)
            template = self._create_issue_template(project, error_message, previous_fields)

            # Write to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                temp_path = f.name
                f.write(template)

            # Open vim editor
            curses.def_prog_mode()
            curses.endwin()

            try:
                subprocess.call(['vim', temp_path])
            finally:
                curses.reset_prog_mode()
                stdscr.refresh()

            # Read and parse result
            try:
                with open(temp_path, 'r') as f:
                    template_text = f.read()

                os.unlink(temp_path)

                # Parse fields
                fields = self._parse_issue_template(template_text)
                if not fields:
                    self._show_message(stdscr, "Issue creation cancelled (empty)", height, width)
                    return None

                # Create issue
                success, result = self._create_jira_issue(fields)
                if success:
                    self._show_message(stdscr, f"✓ Created issue: {result}", height, width)
                    return result
                else:
                    # Save fields and show error, then loop to retry
                    previous_fields = fields
                    error_message = result
                    # Continue loop to re-open editor with error

            except Exception as e:
                # Save fields if we had them
                if 'fields' in locals():
                    previous_fields = fields
                error_message = f"Unexpected error: {str(e)}"
                # Continue loop to re-open editor with error

    def _handle_edit_issue(self, stdscr, ticket_key: str, height: int, width: int):
        """Handle editing an issue (e key)."""
        import tempfile
        import subprocess
        import os

        # Fetch full ticket details
        with self.loading_lock:
            ticket = self.ticket_cache.get(ticket_key)

        if not ticket:
            ticket = self.viewer.fetch_ticket_details(ticket_key)

        if not ticket:
            self._show_message(stdscr, "Failed to load ticket", height, width)
            return

        error_message = None
        while True:
            # Create template with current values
            template = self._create_edit_template(ticket, error_message)

            # Write to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                temp_path = f.name
                f.write(template)

            # Open vim editor
            curses.def_prog_mode()
            curses.endwin()

            try:
                subprocess.call(['vim', temp_path])
            finally:
                curses.reset_prog_mode()
                stdscr.refresh()

            # Read and parse result
            try:
                with open(temp_path, 'r') as f:
                    template_text = f.read()

                os.unlink(temp_path)

                # Parse fields
                parsed = self._parse_issue_template(template_text)
                if not parsed:
                    self._show_message(stdscr, "Edit cancelled (empty)", height, width)
                    return

                # Check if anything changed
                original_values = self._extract_current_values(ticket)
                comment_text = parsed.pop('comment', None)  # Extract comment

                changes = {}
                for key, new_value in parsed.items():
                    old_value = original_values.get(key, '')
                    if new_value != old_value:
                        changes[key] = new_value

                if not changes and not comment_text:
                    self._show_message(stdscr, "Edit cancelled (no changes)", height, width)
                    return

                # Update issue
                success, result = self._update_jira_issue(ticket_key, changes, comment_text)
                if success:
                    msg = f"✓ Updated {ticket_key}"
                    if comment_text:
                        msg += " (with comment)"
                    self._show_message(stdscr, msg, height, width)
                    return
                else:
                    # Show error and loop to retry
                    error_message = result
                    # Continue loop to re-open editor with error

            except Exception as e:
                error_message = f"Unexpected error: {str(e)}"
                # Continue loop to re-open editor with error

    def _handle_query_change(self, stdscr, current_query: str, is_edit_mode: bool, height: int, width: int) -> Optional[str]:
        """Handle changing the query (s/S key). Returns new query or None if cancelled."""
        import tempfile
        import subprocess

        # Create temp file with helpful comments
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            temp_path = f.name

            # Write helpful comments
            f.write("# Enter a JQL query or ticket key below\n")
            f.write("# Examples:\n")
            f.write("#   project=CIPLAT AND status='In Progress'\n")
            f.write("#   project=CIPLAT AND assignee=currentUser() ORDER BY updated DESC\n")
            f.write("#   CIPLAT-1234\n")
            f.write("#\n")
            f.write("# Lines starting with # will be ignored\n")
            f.write("#\n")

            # If edit mode, include current query
            if is_edit_mode and current_query:
                f.write(f"{current_query}\n")
            else:
                f.write("\n")

        # Open vim editor
        curses.def_prog_mode()
        curses.endwin()

        try:
            subprocess.call(['vim', temp_path])
        finally:
            curses.reset_prog_mode()
            stdscr.refresh()

        # Read query from file
        try:
            with open(temp_path, 'r') as f:
                lines = f.readlines()

            # Remove comment lines and empty trailing lines
            query_lines = [line.rstrip() for line in lines if not line.strip().startswith('#')]
            new_query = '\n'.join(query_lines).strip()

            # Clean up temp file
            import os
            os.unlink(temp_path)

            if not new_query:
                self._show_message(stdscr, "Query change cancelled (empty)", height, width)
                return None

            return new_query

        except Exception as e:
            self._show_message(stdscr, f"✗ Error: {str(e)}", height, width)
            return None

    def _search_user_by_display_name(self, display_name: str) -> Optional[str]:
        """Search for user by display name and return accountId."""
        import urllib.parse
        try:
            # Remove @ prefix if present
            name = display_name.lstrip('@').strip()

            # URL encode the name for the query
            encoded_name = urllib.parse.quote(name)

            # Search for users matching the display name
            result = self.viewer.utils.call_jira_api(f'/user/search?query={encoded_name}')

            if result and len(result) > 0:
                # Return first match's accountId
                return result[0].get('accountId')
        except:
            pass
        return None

    def _parse_inline_text(self, text: str) -> List[dict]:
        """Parse inline text for mentions and links, returning ADF content nodes.

        Supports:
        - @Name mentions (looked up via API)
        - Markdown links [text](url)
        - Bare URLs
        """
        import re

        result = []
        pos = 0

        # Pattern for markdown links [text](url)
        markdown_link_pattern = r'\[([^\]]+)\]\(([^\)]+)\)'
        # Pattern for @mentions (word characters, spaces, dots, hyphens)
        mention_pattern = r'@([\w\s\.\-]+?)(?=\s|$|[,\.](?:\s|$))'
        # Pattern for bare URLs
        url_pattern = r'https?://[^\s\)]+'

        # Combine patterns - order matters! Markdown links first, then mentions, then bare URLs
        combined = f'({markdown_link_pattern})|({mention_pattern})|({url_pattern})'

        for match in re.finditer(combined, text):
            # Add text before match
            if match.start() > pos:
                result.append({
                    "type": "text",
                    "text": text[pos:match.start()]
                })

            if match.group(2) and match.group(3):  # Markdown link [text](url)
                link_text = match.group(2)
                link_url = match.group(3)
                # For now, just use the URL (Jira inlineCard doesn't support custom text easily)
                result.append({
                    "type": "inlineCard",
                    "attrs": {
                        "url": link_url
                    }
                })
            elif match.group(5):  # Mention (@Name)
                name = match.group(5)
                account_id = self._search_user_by_display_name(name)
                if account_id:
                    result.append({
                        "type": "mention",
                        "attrs": {
                            "id": account_id,
                            "text": f"@{name}"
                        }
                    })
                else:
                    # Couldn't find user, keep as text
                    result.append({
                        "type": "text",
                        "text": f"@{name}"
                    })
            elif match.group(6):  # Bare URL
                url = match.group(6)
                result.append({
                    "type": "inlineCard",
                    "attrs": {
                        "url": url
                    }
                })

            pos = match.end()

        # Add remaining text
        if pos < len(text):
            result.append({
                "type": "text",
                "text": text[pos:]
            })

        return result if result else [{"type": "text", "text": text}]

    def _text_to_adf(self, text: str) -> dict:
        """Convert plain text to Atlassian Document Format (ADF).

        Supports:
        - Code blocks: ```language ... ```
        - Mentions: @Name (looked up via API)
        - Links: bare URLs
        """
        lines = text.split('\n')
        content = []
        i = 0

        while i < len(lines):
            line = lines[i]

            # Check for code block start (```)
            if line.strip().startswith('```'):
                # Extract language if specified
                language = line.strip()[3:].strip() or None

                # Collect code block lines
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith('```'):
                    code_lines.append(lines[i])
                    i += 1

                # Create code block node
                code_block = {
                    "type": "codeBlock",
                    "content": [
                        {
                            "type": "text",
                            "text": '\n'.join(code_lines)
                        }
                    ]
                }
                if language:
                    code_block["attrs"] = {"language": language}

                content.append(code_block)
                i += 1  # Skip closing ```
            elif line.strip():  # Non-empty line
                # Parse line for mentions and links
                inline_content = self._parse_inline_text(line)
                content.append({
                    "type": "paragraph",
                    "content": inline_content
                })
                i += 1
            else:  # Empty line - add empty paragraph for spacing
                content.append({
                    "type": "paragraph",
                    "content": []
                })
                i += 1

        # Handle case where text is empty or only whitespace
        if not content:
            content = [{
                "type": "paragraph",
                "content": [{"type": "text", "text": ""}]
            }]

        return {
            "type": "doc",
            "version": 1,
            "content": content
        }

    def _extract_project_from_query(self, query: str) -> Optional[str]:
        """Extract project key from JQL query or ticket key."""
        import re

        # Check if it's a ticket key (e.g., "CIPLAT-1234")
        ticket_match = re.match(r'^([A-Z]+)-\d+', query)
        if ticket_match:
            return ticket_match.group(1)

        # Check for "project = KEY" or "project=KEY"
        project_match = re.search(r'project\s*=\s*["\']?([A-Z]+)["\']?', query, re.IGNORECASE)
        if project_match:
            return project_match.group(1)

        # Check for "project IN (KEY1, KEY2)"
        project_in_match = re.search(r'project\s+IN\s*\(\s*([A-Z, ]+)\)', query, re.IGNORECASE)
        if project_in_match:
            projects = [p.strip().strip('"\'') for p in project_in_match.group(1).split(',')]
            if projects:
                return projects[0]  # Return first project

        return None

    def _create_issue_template(self, project: str, error_message: Optional[str] = None, previous_fields: Optional[dict] = None) -> str:
        """Create template for new issue creation.

        Args:
            project: Project key
            error_message: Optional error message to display at top
            previous_fields: Optional dict of previous field values to pre-populate
        """
        template = []

        if error_message:
            template.append(f"# ERROR: {error_message}")
            template.append("#")

        template.extend([
            "# Create new Jira issue",
            "# Lines starting with # are ignored",
            "# Required fields must have values",
            "# Multi-line fields end with __END_OF_FIELDNAME__",
            "#",
            "# Issue Types: Task, Bug, New Feature, Improvement",
            "# Priorities: Critical, High, Medium, Low",
            "#",
            ""
        ])

        # Use previous values if available, otherwise defaults
        if previous_fields:
            template.append(f"project: {previous_fields.get('project', project)}")
            template.append(f"summary: {previous_fields.get('summary', '')}")
            template.append(f"issuetype: {previous_fields.get('issuetype', 'Task')}")
            template.append("")
            template.append("description:")
            desc = previous_fields.get('description', '')
            if desc:
                template.append(desc)
            else:
                template.append("")
            template.append("__END_OF_DESCRIPTION__")
            template.append("")

            # Optional fields - show with values if they were set
            template.append("# Optional fields (uncomment to use):")
            assignee = previous_fields.get('assignee', '')
            if assignee:
                template.append(f"assignee: {assignee}")
            else:
                template.append("# assignee: currentUser()")

            priority = previous_fields.get('priority', '')
            if priority:
                template.append(f"priority: {priority}")
            else:
                template.append("# priority: Medium")

            labels = previous_fields.get('labels', '')
            if labels:
                template.append(f"labels: {labels}")
            else:
                template.append("# labels: ")

            story_points = previous_fields.get('story_points', '')
            if story_points:
                template.append(f"story_points: {story_points}")
            else:
                template.append("# story_points: ")

            epic_link = previous_fields.get('epic_link', '')
            if epic_link:
                template.append(f"epic_link: {epic_link}")
            else:
                template.append("# epic_link: ")
        else:
            # Default template
            template.extend([
                f"project: {project}",
                "summary: ",
                "issuetype: Task",
                "",
                "description:",
                "",
                "__END_OF_DESCRIPTION__",
                "",
                "# Optional fields (uncomment to use):",
                "# assignee: currentUser()",
                "# priority: Medium",
                "# labels: ",
                "# story_points: ",
                "# epic_link: ",
            ])

        template.append("")
        return '\n'.join(template)

    def _parse_issue_template(self, template_text: str) -> Optional[dict]:
        """Parse issue template into field dict. Returns None if empty/cancelled."""
        lines = template_text.split('\n')
        fields = {}
        current_field = None
        current_value = []

        for line in lines:
            # Skip comment lines
            if line.strip().startswith('#'):
                continue

            # Check for multi-line field end marker
            if line.strip().startswith('__END_OF_'):
                if current_field:
                    fields[current_field] = '\n'.join(current_value).strip()
                    current_field = None
                    current_value = []
                continue

            # Check for field: value line
            if ':' in line and not current_field:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()

                # Check if this is a multi-line field (ends with empty value or no value)
                if key in ['description', 'comment'] and not value:
                    current_field = key
                    current_value = []
                elif value:
                    fields[key] = value
            elif current_field:
                # Accumulate multi-line field content
                current_value.append(line)

        # Check if template is effectively empty (only has comments/whitespace)
        if not fields or all(not v for v in fields.values()):
            return None

        return fields

    def _create_jira_issue(self, fields: dict) -> tuple:
        """Create Jira issue from parsed fields. Returns (success, ticket_key_or_error_message)."""
        # Map issuetype names to IDs
        issuetype_map = {
            'task': '10009',
            'bug': '10017',
            'new feature': '11081',
            'improvement': '11078',
            'epic': '10000'
        }

        # Validate required fields
        required = ['project', 'summary', 'issuetype']
        for field in required:
            if field not in fields or not fields[field]:
                return (False, f"Missing required field: {field}")

        # Normalize and validate project key
        project_key = fields['project'].upper().strip()
        if not project_key.replace('-', '').replace('_', '').isalnum():
            return (False, f"Invalid project key: {fields['project']}. Project keys should contain only letters, numbers, hyphens, and underscores.")

        # Build API payload
        issuetype_name = fields['issuetype'].lower()
        issuetype_id = issuetype_map.get(issuetype_name)
        if not issuetype_id:
            return (False, f"Unknown issue type: {fields['issuetype']}. Options: Task, Bug, New Feature, Improvement")

        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": fields['summary'],
                "issuetype": {"id": issuetype_id}
            }
        }

        # Add description (convert to ADF if present)
        if 'description' in fields and fields['description']:
            payload['fields']['description'] = self._text_to_adf(fields['description'])

        # Add optional fields
        if 'assignee' in fields and fields['assignee']:
            # Handle currentUser() specially
            if fields['assignee'] == 'currentUser()':
                payload['fields']['assignee'] = {"accountId": None}  # Will use current user
            else:
                payload['fields']['assignee'] = {"accountId": fields['assignee']}

        if 'priority' in fields and fields['priority']:
            payload['fields']['priority'] = {"name": fields['priority']}

        if 'labels' in fields and fields['labels']:
            labels = [l.strip() for l in fields['labels'].split(',')]
            payload['fields']['labels'] = labels

        if 'story_points' in fields and fields['story_points']:
            try:
                payload['fields']['customfield_10061'] = int(fields['story_points'])
            except ValueError:
                return (False, f"Invalid story points value: {fields['story_points']}")

        if 'epic_link' in fields and fields['epic_link']:
            payload['fields']['customfield_10014'] = fields['epic_link']

        # Call API
        try:
            response = self.viewer.utils.call_jira_api('/issue', method='POST', data=payload)

            # Check for error messages in response (jira-api returns exit 0 even on errors)
            if response:
                if 'errorMessages' in response or 'errors' in response:
                    error_parts = []
                    if 'errorMessages' in response:
                        error_parts.extend(response['errorMessages'])
                    if 'errors' in response:
                        for field, msg in response['errors'].items():
                            error_parts.append(f"{field}: {msg}")
                    return (False, "; ".join(error_parts))

                if 'key' in response:
                    return (True, response['key'])

            return (False, "API call failed - no ticket key in response")
        except Exception as e:
            return (False, f"API error: {str(e)}")

    def _create_edit_template(self, ticket: dict, error_message: Optional[str] = None) -> str:
        """Create template for editing an existing issue."""
        template = []

        if error_message:
            template.append(f"# ERROR: {error_message}")
            template.append("#")

        fields = ticket.get('fields', {})
        key = ticket.get('key', 'Unknown')

        template.extend([
            f"# Edit Jira issue: {key}",
            "# Lines starting with # are ignored",
            "# Multi-line fields end with __END_OF_FIELDNAME__",
            "# Leave field unchanged to keep current value",
            "# Clear a field by leaving it empty",
            "#",
            "",
            "# Add a comment with this update (optional):",
            "comment:",
            "",
            "__END_OF_COMMENT__",
            "",
            "# Editable fields:",
            ""
        ])

        # Extract current values
        summary = fields.get('summary', '')
        description_adf = fields.get('description', {})
        description = self._adf_to_text(description_adf) if description_adf else ''

        assignee = fields.get('assignee')
        assignee_name = assignee.get('displayName', '') if assignee else ''

        priority = fields.get('priority')
        priority_name = priority.get('name', '') if priority else ''

        labels = fields.get('labels', [])
        labels_str = ', '.join(labels) if labels else ''

        story_points = fields.get('customfield_10061', '')

        epic_link = fields.get('customfield_10014', '')

        # Add fields to template
        template.extend([
            f"summary: {summary}",
            "",
            "description:",
            description,
            "__END_OF_DESCRIPTION__",
            "",
            f"assignee: {assignee_name}",
            f"priority: {priority_name}",
            f"labels: {labels_str}",
            f"story_points: {story_points}",
            f"epic_link: {epic_link}",
            ""
        ])

        return '\n'.join(template)

    def _adf_to_text(self, adf: dict) -> str:
        """Convert Atlassian Document Format to plain text with markdown formatting.

        Supports:
        - Code blocks: wrapped in triple backticks
        - Mentions: converted to @Name
        - Links: converted to [text](url) or bare URL
        """
        if not adf or not isinstance(adf, dict):
            return ''

        def process_inline_content(content_items):
            """Process inline content (text, mentions, links) within a paragraph."""
            result = []
            for item in content_items:
                item_type = item.get('type')
                if item_type == 'text':
                    result.append(item.get('text', ''))
                elif item_type == 'mention':
                    # Extract mention text (e.g., "@Tron N")
                    mention_text = item.get('attrs', {}).get('text', '@Unknown')
                    result.append(mention_text)
                elif item_type == 'inlineCard':
                    # Extract link URL
                    url = item.get('attrs', {}).get('url', '')
                    result.append(url)
            return ''.join(result)

        content = adf.get('content', [])
        lines = []

        for block in content:
            block_type = block.get('type')
            if block_type == 'paragraph':
                para_content = block.get('content', [])
                para_text = process_inline_content(para_content)
                lines.append(para_text)
            elif block_type == 'codeBlock':
                # Get language if specified
                language = block.get('attrs', {}).get('language', '')

                # Add opening backticks with language
                lines.append(f'```{language}')

                # Add code content
                code_content = block.get('content', [])
                for item in code_content:
                    if item.get('type') == 'text':
                        lines.append(item.get('text', ''))

                # Add closing backticks
                lines.append('```')

        return '\n'.join(lines)

    def _extract_current_values(self, ticket: dict) -> dict:
        """Extract current field values from ticket for comparison."""
        fields = ticket.get('fields', {})

        description_adf = fields.get('description', {})
        description = self._adf_to_text(description_adf) if description_adf else ''

        assignee = fields.get('assignee')
        assignee_name = assignee.get('displayName', '') if assignee else ''

        priority = fields.get('priority')
        priority_name = priority.get('name', '') if priority else ''

        labels = fields.get('labels', [])
        labels_str = ', '.join(labels) if labels else ''

        return {
            'summary': fields.get('summary', ''),
            'description': description,
            'assignee': assignee_name,
            'priority': priority_name,
            'labels': labels_str,
            'story_points': str(fields.get('customfield_10061', '')),
            'epic_link': fields.get('customfield_10014', '')
        }

    def _update_jira_issue(self, ticket_key: str, changes: dict, comment_text: Optional[str]) -> tuple:
        """Update Jira issue with changes. Returns (success, error_message)."""
        if not changes and not comment_text:
            return (True, "")

        # Build update payload
        update_payload = {"fields": {}}

        # Handle field updates
        if 'summary' in changes:
            update_payload['fields']['summary'] = changes['summary']

        if 'description' in changes:
            if changes['description']:
                update_payload['fields']['description'] = self._text_to_adf(changes['description'])
            else:
                update_payload['fields']['description'] = None

        if 'assignee' in changes:
            if changes['assignee']:
                # Try to find user by display name
                # For now, just set to null (unassigned) if empty, otherwise leave as-is
                # TODO: Implement user lookup by display name
                update_payload['fields']['assignee'] = {"accountId": changes['assignee']}
            else:
                update_payload['fields']['assignee'] = None

        if 'priority' in changes and changes['priority']:
            update_payload['fields']['priority'] = {"name": changes['priority']}

        if 'labels' in changes:
            if changes['labels']:
                labels = [l.strip() for l in changes['labels'].split(',')]
                update_payload['fields']['labels'] = labels
            else:
                update_payload['fields']['labels'] = []

        if 'story_points' in changes:
            if changes['story_points']:
                try:
                    update_payload['fields']['customfield_10061'] = int(changes['story_points'])
                except ValueError:
                    return (False, f"Invalid story points value: {changes['story_points']}")
            else:
                update_payload['fields']['customfield_10061'] = None

        if 'epic_link' in changes:
            if changes['epic_link']:
                update_payload['fields']['customfield_10014'] = changes['epic_link']
            else:
                update_payload['fields']['customfield_10014'] = None

        # Update the issue if there are changes
        if update_payload['fields']:
            try:
                response = self.viewer.utils.call_jira_api(f'/issue/{ticket_key}', method='PUT', data=update_payload)

                # Check for errors
                if response is None:
                    return (False, "API call failed")
                elif response.get('errors') or response.get('errorMessages'):
                    errors = response.get('errors', {})
                    error_messages = response.get('errorMessages', [])
                    if errors:
                        field_errors = list(errors.values())
                        error_text = field_errors[0] if field_errors else "Unknown error"
                    else:
                        error_text = error_messages[0] if error_messages else "Unknown error"
                    return (False, error_text)
            except Exception as e:
                return (False, f"API error: {str(e)}")

        # Add comment if provided
        if comment_text:
            try:
                comment_payload = {
                    "body": self._text_to_adf(comment_text)
                }
                response = self.viewer.utils.call_jira_api(f'/issue/{ticket_key}/comment', method='POST', data=comment_payload)
                if response is None:
                    return (False, "Failed to add comment")
            except Exception as e:
                return (False, f"Failed to add comment: {str(e)}")

        return (True, "")

    def _show_message(self, stdscr, message: str, height: int, width: int, duration: int = 1500):
        """Show a temporary message overlay.

        Args:
            stdscr: Curses screen object
            message: Message to display
            height: Screen height
            width: Screen width
            duration: Duration in milliseconds (default 1500)
        """
        msg_width = min(len(message) + 4, width - 4)
        msg_height = 3
        start_y = (height - msg_height) // 2
        start_x = (width - msg_width) // 2

        try:
            overlay = curses.newwin(msg_height, msg_width, start_y, start_x)
            overlay.box()
            overlay.addstr(1, 2, message[:msg_width - 4])
            overlay.refresh()
            curses.napms(duration)
        except curses.error:
            pass

    def _get_legend_items(self):
        """Get legend items with colors in the order they should appear."""
        # Order: Blue (backlog), Yellow (active), Green (done), Red (blocked)
        return [
            # Blue - Backlog states
            ('B', 'Backlog', 3),
            ('A', 'Accepted', 3),
            ('S', 'Scheduled', 3),
            ('W', 'Wish List', 3),
            # Yellow - Active states
            ('T', 'Triage', 2),
            ('P', 'In Progress', 2),
            ('R', 'In Review', 2),
            ('Q', 'Requirements', 2),
            # Green - Done states
            ('C', 'Done', 1),
            ('V', 'Verification', 1),
            ('Y', 'Deploy', 1),
            ('M', 'Merge', 1),
            ('Z', 'Closure', 1),
            # Red - Blocked/Deferred states
            ('D', 'Deferred', 4),
            ('_', 'Abandoned', 4),
            ('X', 'Blocked', 4),
        ]

    def _draw_legend(self, stdscr, start_y: int, max_width: int) -> int:
        """Draw status legend at the top of the left pane. Returns number of lines used."""
        legend_items = self._get_legend_items()

        # Build legend with proper spacing
        current_line = []
        current_width = 0
        lines = []

        for letter, name, color_pair in legend_items:
            # Format: [L] Name  (space between items)
            item_text = f"[{letter}] {name}"
            item_width = len(item_text) + 2  # +2 for spacing between items

            # Check if this item fits on the current line
            if current_width + item_width > max_width and current_line:
                # Start a new line
                lines.append(current_line)
                current_line = []
                current_width = 0

            current_line.append((letter, name, color_pair))
            current_width += item_width

        # Add the last line
        if current_line:
            lines.append(current_line)

        # Draw the lines
        for line_idx, line_items in enumerate(lines):
            y = start_y + line_idx
            x = 0

            for idx, (letter, name, color_pair) in enumerate(line_items):
                try:
                    # Draw [L] in color
                    stdscr.addstr(y, x, f"[{letter}]", curses.color_pair(color_pair))
                    x += len(f"[{letter}]")

                    # Draw name
                    stdscr.addstr(y, x, f" {name}", curses.A_NORMAL)
                    x += len(f" {name}")

                    # Add spacing between items (but not after the last one)
                    if idx < len(line_items) - 1:
                        stdscr.addstr(y, x, "  ", curses.A_NORMAL)
                        x += 2
                except curses.error:
                    pass

        # Store legend height for scroll calculations
        self.legend_lines = len(lines)
        return len(lines)

    def _get_visible_height(self, height: int) -> int:
        """Calculate visible height for ticket list accounting for legend and header."""
        # height - 2 (status bar) - 1 (header) - legend_lines - 1 (separator)
        return height - 4 - self.legend_lines

    def _format_date_with_relative(self, date_str: str) -> Tuple[str, str, int]:
        """Format date with relative time. Returns (date_str, relative_str, color_pair)."""
        if not date_str:
            return ('N/A', '', 0)

        try:
            # Parse the date
            date_only = date_str.split('T')[0] if 'T' in date_str else date_str

            # Calculate relative time
            days, days_text = self.viewer.utils.calculate_days_since_update(date_str)

            # Determine color based on days
            if days < 2:
                color_pair = 1  # Green
            elif days <= 4:
                color_pair = 2  # Yellow
            else:
                color_pair = 0  # Normal

            return (date_only, days_text, color_pair)
        except Exception:
            return (date_str, '', 0)

    def _draw_ticket_list(self, stdscr, tickets: List[dict], selected_idx: int,
                         scroll_offset: int, max_height: int, max_width: int,
                         search_query: str):
        """Draw the ticket list in the left pane."""
        # Header
        try:
            header = f" Tickets ({len(tickets)})"
            if search_query:
                header += f" [Filter: {search_query}]"
            stdscr.addstr(0, 0, header[:max_width], curses.A_BOLD)
        except curses.error:
            pass

        # Draw legend
        legend_lines = self._draw_legend(stdscr, 1, max_width)

        # Draw separator
        separator_y = 1 + legend_lines
        try:
            stdscr.addstr(separator_y, 0, "=" * max_width)
        except curses.error:
            pass

        # Draw tickets (start after header + legend + separator)
        ticket_start_y = separator_y + 1
        visible_height = max_height - ticket_start_y
        for i in range(scroll_offset, min(scroll_offset + visible_height, len(tickets))):
            y = i - scroll_offset + ticket_start_y

            issue = tickets[i]
            fields = issue.get('fields', {})
            key = issue.get('key', 'N/A')
            status = fields.get('status', {}).get('name', 'Unknown')
            status_letter = self.viewer.utils.get_status_letter(status)

            # Extract flags if present (handle None value)
            flags = fields.get('customfield_10023') or []
            flag_text = ''
            if flags:
                flag_values = []
                for flag in flags:
                    if isinstance(flag, dict):
                        flag_values.append(flag.get('value', str(flag)))
                    else:
                        flag_values.append(str(flag))
                if flag_values:
                    flag_text = f"[{', '.join(flag_values)}] "

            # Calculate available space for summary (key + status + separators + flags = variable)
            summary_max = max_width - len(key) - len(flag_text) - 6
            summary = fields.get('summary', 'No summary')[:summary_max]

            # Check if ticket is stale (may no longer match query)
            is_stale = key in self.stale_tickets

            # Highlight selection
            is_selected = i == selected_idx
            base_attr = curses.A_REVERSE if is_selected else curses.A_NORMAL

            # Apply dim attribute if stale
            if is_stale:
                base_attr |= curses.A_DIM

            # Determine status color (matching dashboard style)
            if status_letter in ['C', 'V', 'Z', 'Y', 'M']:
                status_color = curses.color_pair(1)  # Green for done
            elif status_letter in ['A', 'B', 'S', 'W']:
                status_color = curses.color_pair(3)  # Blue for backlog
            elif status_letter in ['P', 'R', 'Q', 'T']:
                status_color = curses.color_pair(2)  # Yellow for active
            elif status_letter in ['D', 'X', '_']:
                status_color = curses.color_pair(4)  # Red for blocked
            else:
                status_color = curses.A_NORMAL

            # Draw line in colored segments
            try:
                x_pos = 0
                # Draw status indicator with color
                stdscr.addstr(y, x_pos, f"[{status_letter}]", status_color | base_attr)
                x_pos += len(f"[{status_letter}]")

                # Draw space
                stdscr.addstr(y, x_pos, " ", base_attr)
                x_pos += 1

                # Draw key in green (or dim if stale)
                stdscr.addstr(y, x_pos, key, curses.color_pair(1) | base_attr)
                x_pos += len(key)

                # Draw colon
                stdscr.addstr(y, x_pos, ": ", base_attr)
                x_pos += 2

                # Draw flags in red if present
                if flag_text:
                    stdscr.addstr(y, x_pos, flag_text, curses.color_pair(4) | base_attr)
                    x_pos += len(flag_text)

                # Draw summary
                stdscr.addstr(y, x_pos, summary, base_attr)

                # Add stale indicator at the end if stale
                if is_stale:
                    stale_indicator = " [?]"
                    if x_pos + len(stale_indicator) < max_width:
                        stdscr.addstr(y, x_pos + len(summary), stale_indicator, base_attr)
            except curses.error:
                pass

    def _draw_ticket_details(self, stdscr, ticket_key: str, x_offset: int,
                            max_height: int, max_width: int):
        """Draw ticket details in the right pane."""
        # Get full ticket details from cache
        ticket = self.ticket_cache.get(ticket_key)
        if not ticket:
            # Check if still loading or actually failed
            with self.loading_lock:
                still_loading = not self.loading_complete

            try:
                if still_loading:
                    stdscr.addstr(1, x_offset + 2, "Loading ticket details, please wait...")
                    stdscr.addstr(3, x_offset + 2, "(If this doesn't load shortly, try 'r' to refresh)")
                else:
                    stdscr.addstr(1, x_offset + 2, "Failed to load ticket details")
                    stdscr.addstr(3, x_offset + 2, "(Try 'r' to refresh)")
            except curses.error:
                pass
            return

        # Use shared formatting logic from viewer
        lines = self.viewer.format_ticket_detail_lines(ticket, max_width)

        # Add issuelinks info
        fields = ticket.get('fields', {})
        issuelinks = fields.get('issuelinks', [])

        # Linked issues - group by relationship type
        if issuelinks:
            lines.append(("HEADER", (" Linked Issues:")[:max_width - 2]))

            # Group links by relationship and direction
            grouped_links = {}
            for link in issuelinks:
                link_type = link.get('type', {})

                if 'inwardIssue' in link:
                    linked_issue = link['inwardIssue']
                    direction = link_type.get('inward', 'related to')
                elif 'outwardIssue' in link:
                    linked_issue = link['outwardIssue']
                    direction = link_type.get('outward', 'relates to')
                else:
                    continue

                if direction not in grouped_links:
                    grouped_links[direction] = []
                grouped_links[direction].append(linked_issue)

            # Display grouped links
            for direction, issues in sorted(grouped_links.items()):
                lines.append(("", f"  {direction}:"[:max_width - 2]))

                for linked_issue in issues:
                    linked_key = linked_issue.get('key', 'Unknown')
                    linked_fields = linked_issue.get('fields', {})
                    linked_summary = linked_fields.get('summary', '')
                    linked_status = linked_fields.get('status', {})
                    status_name = linked_status.get('name', 'Unknown')
                    status_letter = self.viewer.utils.get_status_letter(status_name)

                    # Format: [P] CIPLAT-2116: Summary
                    link_text = f"    [{status_letter}] {linked_key}"
                    if linked_summary:
                        # Calculate remaining space for summary
                        remaining = max_width - len(link_text) - 6
                        if remaining > 20:
                            link_text += f": {linked_summary[:remaining]}"

                    lines.append((f"STATUS_{status_letter}", link_text[:max_width - 2]))

        lines.append(("", ""))

        # Description
        description = fields.get('description')
        if description:
            lines.append(("HEADER", (" Description:")[:max_width - 2]))
            desc_lines = self.viewer.format_description_lines(description, indent="")
            # Wrap description lines, preserving tags
            for tag, line in desc_lines:
                if tag == "CODE":
                    # Code lines: don't wrap, just add with CODE tag
                    lines.append((tag, f"  {line}"[:max_width - 2]))
                elif tag == "SEGMENTS":
                    # Segmented lines (with inline styling): prepend spaces to first segment
                    segments = line
                    if segments:
                        first_tag, first_text = segments[0]
                        segments[0] = (first_tag, f"  {first_text}")
                    lines.append((tag, segments))
                else:
                    # Normal text: wrap as before
                    wrapped = self._wrap_text(line, max_width - 4)
                    lines.extend([("", f"  {l}") for l in wrapped])

        lines.append(("", ""))

        # Comments
        comments_data = fields.get('comment', {})
        all_comments = comments_data.get('comments', [])

        if all_comments:
            if self.show_full:
                comments_to_show = all_comments
                lines.append(("HEADER", (f" ──── All Comments ({len(all_comments)}) ────")[:max_width - 2]))
            else:
                comments_to_show = self.viewer.filter_recent_comments(all_comments)
                lines.append(("HEADER", (f" ──── Recent Comments ({len(comments_to_show)}/{len(all_comments)}) ────")[:max_width - 2]))

            for comment in comments_to_show:
                comment_text = self.viewer.format_comment(comment, False)
                for line in comment_text.split('\n'):
                    wrapped = self._wrap_text(line, max_width - 4)
                    lines.extend([("", f"  {l}") for l in wrapped])
                lines.append(("", ""))
        else:
            lines.append(("HEADER", (" ──── Comments ────")[:max_width - 2]))
            lines.append(("", "  (No comments)"))
            lines.append(("", ""))

        # History (only if full mode)
        if self.show_full:
            changelog = ticket.get('changelog', {})
            histories = changelog.get('histories', [])

            if histories:
                lines.append(("HEADER", (f" ──── Change History ({len(histories)}) ────")[:max_width - 2]))
                for history in histories:
                    history_lines = self.viewer.format_history_entry(history, False)
                    for line in history_lines:
                        wrapped = self._wrap_text(line, max_width - 4)
                        lines.extend([("", f"  {l}") for l in wrapped])
                    lines.append(("", ""))

        # Store total lines for scroll tracking
        self.detail_total_lines = len(lines)

        # Draw visible lines with scrolling support
        visible_lines = lines[self.detail_scroll_offset:self.detail_scroll_offset + max_height - 1]

        for i, (tag, line) in enumerate(visible_lines):
            # Handle segmented lines (inline styling)
            if tag == "SEGMENTS":
                # line is actually a list of (tag, text) segments
                segments = line
                x_pos = 0
                for seg_tag, seg_text in segments:
                    if seg_tag == "LINK":
                        seg_attr = curses.color_pair(5)  # Cyan for links/mentions
                    else:
                        seg_attr = curses.A_NORMAL

                    try:
                        # Only render what fits
                        available = max_width - 2 - x_pos
                        if available > 0:
                            text_to_render = seg_text[:available]
                            stdscr.addstr(i, x_offset + 1 + x_pos, text_to_render, seg_attr)
                            x_pos += len(text_to_render)
                    except curses.error:
                        pass
                continue

            # Determine color based on tag
            if tag == "KEY":
                attr = curses.color_pair(1) | curses.A_BOLD  # Green bold
            elif tag == "SUMMARY":
                attr = curses.A_BOLD  # Bold
            elif tag == "HEADER":
                attr = curses.color_pair(3)  # Blue
            elif tag.startswith("STATUS_"):
                # Map status letter to color (matching dashboard style)
                status_letter = tag.split("_")[1]
                if status_letter in ['C', 'V', 'Z', 'Y', 'M']:
                    attr = curses.color_pair(1)  # Green for done
                elif status_letter in ['A', 'B', 'S', 'W']:
                    attr = curses.color_pair(3)  # Blue for backlog
                elif status_letter in ['P', 'R', 'Q', 'T']:
                    attr = curses.color_pair(2)  # Yellow for active
                elif status_letter in ['D', 'X', '_']:
                    attr = curses.color_pair(4)  # Red for blocked
                else:
                    attr = curses.A_NORMAL
            elif tag.startswith("PRIORITY_"):
                # Map priority to color
                priority = tag.split("_", 1)[1]
                if priority in ['Critical', 'Blocker', 'Highest']:
                    attr = curses.color_pair(4)  # Red
                elif priority in ['High']:
                    attr = curses.color_pair(2)  # Yellow
                elif priority in ['Low', 'Lowest']:
                    attr = curses.color_pair(3)  # Blue
                else:
                    attr = curses.A_NORMAL  # Medium/None
            elif tag.startswith("DATE_"):
                # Map relative date to color
                color_num = tag.split("_")[1]
                if color_num == '1':
                    attr = curses.color_pair(1)  # Green (< 2 days)
                elif color_num == '2':
                    attr = curses.color_pair(2)  # Yellow (2-4 days)
                else:
                    attr = curses.A_NORMAL  # Normal (> 4 days)
            elif tag == "WARN":
                attr = curses.color_pair(4)  # Red for warnings/flags
            elif tag == "CODE":
                attr = curses.A_DIM  # Dim/grey for code blocks
            else:
                attr = curses.A_NORMAL

            try:
                stdscr.addstr(i, x_offset + 1, line[:max_width - 2], attr)
            except curses.error:
                pass

        # Show scroll indicator if content is scrolled
        if self.detail_scroll_offset > 0 or self.detail_scroll_offset + max_height - 1 < self.detail_total_lines:
            scroll_indicator = f"[{self.detail_scroll_offset + 1}-{min(self.detail_scroll_offset + max_height - 1, self.detail_total_lines)}/{self.detail_total_lines}]"
            try:
                stdscr.addstr(max_height - 1, x_offset + 1, scroll_indicator, curses.A_REVERSE)
            except curses.error:
                pass

    def _draw_status_bar(self, stdscr, y: int, width: int, current: int,
                        total: int, search_query: str):
        """Draw status bar at bottom showing commands and position."""
        status_left = f" {current}/{total}"

        # Add loading indicator if still loading
        with self.loading_lock:
            if not self.loading_complete:
                status_left += f" [Loading {self.loading_count}/{self.loading_total}]"

        status_right = " q:quit j/k:move g/G:top/bot r:refresh e:edit t:transition f:flags c:comment v:browser ?:help "

        # Calculate spacing
        padding = width - len(status_left) - len(status_right)
        status = status_left + " " * max(0, padding) + status_right

        try:
            stdscr.addstr(y, 0, status[:width - 1], curses.A_REVERSE)
        except curses.error:
            pass

    def _draw_help(self, stdscr, height: int, width: int):
        """Draw help overlay."""
        help_text = [
            "JIRA-VIEW INTERACTIVE MODE - HELP",
            "",
            "Navigation:",
            "  j / ↓      Move down in list",
            "  k / ↑      Move up in list",
            "  g          Jump to top",
            "  G          Jump to bottom",
            "  Enter      Scroll detail pane down (1 line)",
            "  \\          Scroll detail pane up (1 line)",
            "  Ctrl+J     Scroll detail pane down (1 line)",
            "  Ctrl+K     Scroll detail pane up (1 line)",
            "  Ctrl+B     Scroll detail pane down (half-page)",
            "  Ctrl+U     Scroll detail pane up (half-page)",
            "",
            "Actions:",
            "  r          Refresh current view",
            "  e          Edit ticket",
            "  f          Toggle flags",
            "  F          Toggle full mode (all comments)",
            "  v          Open ticket in browser",
            "  t          Transition ticket",
            "  c          Add comment to ticket",
            "  n          Create new issue",
            "  s          New query (JQL or ticket key)",
            "  S          Edit current query",
            "  /          Search/filter tickets",
            "  ?          Show this help",
            "  q          Quit",
            "",
            "Press any key to close help"
        ]

        # Draw centered box
        box_width = max(len(line) for line in help_text) + 4
        box_height = len(help_text) + 2
        start_y = (height - box_height) // 2
        start_x = (width - box_width) // 2

        # Draw box
        try:
            for i, line in enumerate(help_text):
                y = start_y + i + 1
                x = start_x + 2
                if i == 0:  # Title
                    stdscr.addstr(y, x, line[:box_width - 4], curses.A_BOLD)
                else:
                    stdscr.addstr(y, x, line[:box_width - 4])
        except curses.error:
            pass

    def _get_search_input(self, stdscr, y: int, width: int) -> str:
        """Get search input from user."""
        curses.echo()
        curses.curs_set(1)

        try:
            # Clear the status line (use width - 1 to avoid curses boundary error)
            stdscr.addstr(y, 0, " " * (width - 1), curses.A_REVERSE)
            stdscr.addstr(y, 0, "Search: ", curses.A_REVERSE)
            stdscr.refresh()

            # Get input (simplified - just use getch loop)
            search = ""
            while True:
                ch = stdscr.getch()
                if ch == 10 or ch == 13:  # Enter
                    break
                elif ch == 27:  # Escape
                    search = ""
                    break
                elif ch == curses.KEY_BACKSPACE or ch == 127:
                    search = search[:-1]
                elif 32 <= ch <= 126:  # Printable characters
                    search += chr(ch)

                # Update display (truncate to fit width)
                stdscr.addstr(y, 0, " " * (width - 1), curses.A_REVERSE)
                display_text = f"Search: {search}"[:width - 1]
                stdscr.addstr(y, 0, display_text, curses.A_REVERSE)
                stdscr.refresh()
        finally:
            curses.noecho()
            curses.curs_set(0)

        return search.strip()

    def _filter_tickets(self, tickets: List[dict], query: str) -> List[dict]:
        """Filter tickets by search query (case-insensitive)."""
        query_lower = query.lower()
        filtered = []

        for ticket in tickets:
            key = ticket.get('key', '').lower()
            summary = ticket.get('fields', {}).get('summary', '').lower()

            if query_lower in key or query_lower in summary:
                filtered.append(ticket)

        return filtered if filtered else tickets

    def _strip_ansi(self, text: str) -> str:
        """Strip ANSI color codes from text."""
        import re
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

    def _wrap_text(self, text: str, width: int) -> List[str]:
        """Wrap text respecting word boundaries."""
        if not text:
            return ['']

        if len(text) <= width:
            return [text]

        # Use textwrap for proper word-boundary wrapping
        return textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=False)

    def _open_in_browser(self, ticket_key: str):
        """Open ticket in browser using xdg-open."""
        # Construct Jira URL
        url = f"https://indeed.atlassian.net/browse/{ticket_key}"

        try:
            # Try xdg-open (Linux), open (Mac), or start (Windows)
            if sys.platform.startswith('linux'):
                subprocess.run(['xdg-open', url], check=False)
            elif sys.platform == 'darwin':
                subprocess.run(['open', url], check=False)
            elif sys.platform == 'win32':
                subprocess.run(['start', url], shell=True, check=False)
        except Exception:
            # Silently fail - TUI will continue running
            pass
