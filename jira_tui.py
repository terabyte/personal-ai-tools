#!/usr/bin/env python3

"""
Jira TUI - Terminal User Interface for interactive ticket viewing
Provides a split-pane interface with vim keybindings for browsing tickets
"""

import json
import os
import sys
import subprocess
import webbrowser
import textwrap
import threading
import signal
import atexit
import re
import time
from urllib.parse import quote
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
        self.query_lines = 0  # Track how many lines the query occupies
        self.original_query = None  # Store original query before backlog mode modifies it
        self.detail_scroll_offset = 0  # Track right pane scroll position
        self.detail_total_lines = 0  # Track total lines in right pane
        self.curses_initialized = False  # Track if curses is active
        self._original_sigint_handler = None  # Store original signal handler
        self._shutdown_flag = False  # Flag to signal background threads to stop
        self.stale_tickets = set()  # Track tickets that may no longer match the query
        self.backlog_mode = False  # Track if in backlog mode (for rank reordering)

    @staticmethod
    def normalize_jql_input(input_str: str) -> str:
        """
        Normalize JQL input - convert plain issue keys to JQL and upcase them.

        Args:
            input_str: User input (issue key or JQL query)

        Returns:
            Normalized JQL query string
        """
        stripped = input_str.strip()

        # Check if input looks like a plain issue key (case-insensitive)
        issue_key_pattern = r'^[A-Za-z][A-Za-z0-9]+-\d+$'
        if re.match(issue_key_pattern, stripped):
            # Convert to uppercase and wrap in key= JQL
            return f'key={stripped.upper()}'

        # Otherwise return as-is (already JQL)
        return stripped

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
        stdscr.addstr(0, 0, "Counting tickets...")
        stdscr.refresh()

        def progress_callback(fetched, total):
            """Update screen with fetch progress."""
            stdscr.clear()
            stdscr.addstr(0, 0, f"Fetching tickets: {fetched}/{total}...")
            stdscr.refresh()

        tickets, single_ticket_mode = self._fetch_tickets(query_or_ticket, progress_callback, stdscr)

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

                    # Populate user cache from assignee and reporter
                    fields = ticket.get('fields', {})
                    assignee = fields.get('assignee')
                    reporter = fields.get('reporter')
                    if assignee:
                        self.viewer.utils.cache_user(assignee)
                    if reporter:
                        self.viewer.utils.cache_user(reporter)

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
        input_buffer = ""  # Shows what user is typing (for number prefixes)

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
            # Show original query if in backlog mode (current_query has ORDER BY Rank appended)
            display_query = self.original_query if self.backlog_mode and self.original_query else current_query
            self._draw_ticket_list(stdscr, tickets, selected_idx, scroll_offset,
                                   height - 2, list_width, search_query, display_query)

            # Draw ticket details in right pane
            if tickets and selected_idx < len(tickets):
                current_ticket_key = tickets[selected_idx].get('key')
                self._draw_ticket_details(stdscr, current_ticket_key, detail_x,
                                         height - 2, detail_width)

            # Draw status bar at bottom
            self._draw_status_bar(stdscr, height - 1, width, selected_idx + 1,
                                 len(tickets), search_query, input_buffer)

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
            elif ord('0') <= key <= ord('9'):  # Number prefix for vim-style movement
                # Define callback to update input_buffer and redraw ONLY status bar
                def update_display(digits):
                    nonlocal input_buffer
                    input_buffer = digits
                    # Only redraw status bar for speed (avoid flashing)
                    self._draw_status_bar(stdscr, height - 1, width, selected_idx + 1,
                                         len(tickets), search_query, input_buffer)
                    stdscr.noutrefresh()
                    curses.doupdate()

                # Read the full number with display callback
                count = self._read_number_from_key(stdscr, key, update_display)

                # Clear input buffer after reading
                input_buffer = ""

                # Wait for next command: j, k, g, or G
                stdscr.nodelay(False)
                next_key = stdscr.getch()
                stdscr.nodelay(True)

                if next_key == ord('j'):  # <count>j - move down
                    selected_idx, scroll_offset = self._handle_vim_navigation(
                        stdscr, tickets, selected_idx, scroll_offset, height, count, 'j')
                elif next_key == ord('k'):  # <count>k - move up
                    selected_idx, scroll_offset = self._handle_vim_navigation(
                        stdscr, tickets, selected_idx, scroll_offset, height, count, 'k')
                elif next_key == ord('g'):  # <count>g - wait for second char (j/k/g)
                    stdscr.nodelay(False)
                    second_key = stdscr.getch()
                    stdscr.nodelay(True)
                    if second_key == ord('g'):  # <count>gg - go to line number
                        selected_idx, scroll_offset = self._handle_vim_navigation(
                            stdscr, tickets, selected_idx, scroll_offset, height, count, 'gg')
                    elif second_key == ord('j'):  # <count>gj - move down (same as j)
                        selected_idx, scroll_offset = self._handle_vim_navigation(
                            stdscr, tickets, selected_idx, scroll_offset, height, count, 'j')
                    elif second_key == ord('k'):  # <count>gk - move up (same as k)
                        selected_idx, scroll_offset = self._handle_vim_navigation(
                            stdscr, tickets, selected_idx, scroll_offset, height, count, 'k')
                elif next_key == ord('G'):  # <count>G - go to line (immediate, no second key)
                    selected_idx, scroll_offset = self._handle_vim_navigation(
                        stdscr, tickets, selected_idx, scroll_offset, height, count, 'G')
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
            elif key == ord('g'):  # gg - Go to top (wait for second g)
                stdscr.nodelay(False)
                next_key = stdscr.getch()
                stdscr.nodelay(True)
                if next_key == ord('g'):  # gg - go to top
                    selected_idx, scroll_offset = self._handle_vim_navigation(
                        stdscr, tickets, selected_idx, scroll_offset, height, 0, 'gg')
            elif key == ord('G'):  # G - Go to bottom
                selected_idx, scroll_offset = self._handle_vim_navigation(
                    stdscr, tickets, selected_idx, scroll_offset, height, 0, 'G')
            elif key == ord('r'):  # Refresh
                # Remember currently selected ticket
                current_ticket_key = tickets[selected_idx].get('key') if tickets and selected_idx < len(tickets) else None

                # current_query is already modified in backlog mode (has ORDER BY Rank)
                all_tickets, _ = self._fetch_tickets(current_query, stdscr=stdscr)

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
            elif key == ord('R'):  # Cache refresh menu (Shift+R)
                self._handle_cache_refresh(stdscr, height, width)
            elif key == ord('F'):  # Toggle full mode (capital F)
                self.show_full = not self.show_full
            elif key == ord('b'):  # Toggle backlog mode
                self.backlog_mode = not self.backlog_mode

                # Remember the currently selected ticket key
                current_key = tickets[selected_idx].get('key') if tickets and selected_idx < len(tickets) else None

                if self.backlog_mode:
                    # Entering backlog mode - save original query and modify to use Rank ordering
                    self.original_query = current_query
                    current_query = self._add_rank_order_to_query(current_query)

                    # Re-fetch with rank ordering
                    all_tickets, _ = self._fetch_tickets(current_query, stdscr=stdscr)
                    tickets = all_tickets
                    # Find the same ticket in the new order
                    if current_key:
                        new_idx = next((i for i, t in enumerate(tickets) if t.get('key') == current_key), 0)
                        selected_idx = new_idx
                    elif selected_idx >= len(tickets):
                        selected_idx = 0
                    # Adjust scroll to keep selection visible
                    visible_height = self._get_visible_height(height)
                    if selected_idx < scroll_offset:
                        scroll_offset = selected_idx
                    elif selected_idx >= scroll_offset + visible_height:
                        scroll_offset = selected_idx - visible_height + 1
                else:
                    # Exiting backlog mode - restore original query
                    if self.original_query:
                        current_query = self.original_query
                        self.original_query = None

                    # Re-fetch with original query (restores original order)
                    all_tickets, _ = self._fetch_tickets(current_query, stdscr=stdscr)
                    # Filter all_tickets to match current tickets (in case of search)
                    ticket_keys = {t.get('key') for t in tickets}
                    tickets = [t for t in all_tickets if t.get('key') in ticket_keys]
                    # Find the same ticket in the restored order
                    if current_key:
                        new_idx = next((i for i, t in enumerate(tickets) if t.get('key') == current_key), 0)
                        selected_idx = new_idx
                    # Adjust scroll to keep selection visible
                    visible_height = self._get_visible_height(height)
                    if selected_idx < scroll_offset:
                        scroll_offset = selected_idx
                    elif selected_idx >= scroll_offset + visible_height:
                        scroll_offset = selected_idx - visible_height + 1
            elif key == ord('m') and self.backlog_mode:  # Move up in backlog
                # Wait for next key
                stdscr.nodelay(False)
                next_key = stdscr.getch()
                stdscr.nodelay(True)

                if next_key == ord('m') or next_key == ord('0'):  # mm or m0 = top
                    tickets, selected_idx, scroll_offset = self._handle_backlog_move(
                        stdscr, tickets, all_tickets, selected_idx, scroll_offset,
                        current_query, 'top', 0, height, width
                    )
                elif ord('1') <= next_key <= ord('9'):  # mN = up N
                    count = self._read_number_from_key(stdscr, next_key)
                    tickets, selected_idx, scroll_offset = self._handle_backlog_move(
                        stdscr, tickets, all_tickets, selected_idx, scroll_offset,
                        current_query, 'up', count, height, width
                    )
            elif key == ord('M') and self.backlog_mode:  # Move down in backlog
                # Wait for next key
                stdscr.nodelay(False)
                next_key = stdscr.getch()
                stdscr.nodelay(True)

                if next_key == ord('M'):  # MM = bottom
                    tickets, selected_idx, scroll_offset = self._handle_backlog_move(
                        stdscr, tickets, all_tickets, selected_idx, scroll_offset,
                        current_query, 'bottom', 0, height, width
                    )
                elif ord('1') <= next_key <= ord('9'):  # MN = down N
                    count = self._read_number_from_key(stdscr, next_key)
                    tickets, selected_idx, scroll_offset = self._handle_backlog_move(
                        stdscr, tickets, all_tickets, selected_idx, scroll_offset,
                        current_query, 'down', count, height, width
                    )
            elif key == ord('v'):  # Open in browser
                if tickets and selected_idx < len(tickets):
                    current_key = tickets[selected_idx].get('key')
                    self._open_in_browser(current_key)
            elif key == ord('y'):  # Copy URL to clipboard (yank)
                if tickets and selected_idx < len(tickets):
                    current_key = tickets[selected_idx].get('key')
                    success, error_msg = self._copy_url_to_clipboard(current_key)
                    if success:
                        self._show_message(stdscr, f"✓ URL copied to clipboard", height, width, duration=1500)
                    else:
                        self._show_message(stdscr, f"✗ Copy failed: {error_msg}", height, width, duration=3000)
            elif key == ord('/'):  # Search
                # Remember current ticket key before filtering
                current_ticket_key = tickets[selected_idx].get('key') if tickets and selected_idx < len(tickets) else None

                search_query = self._get_search_input(stdscr, height - 1, width)

                # Filter tickets by search query or restore full list if empty
                if search_query:
                    tickets = self._filter_tickets(all_tickets, search_query)
                    # Tickets should already be in rank order if in backlog mode
                    # (since all_tickets was fetched with ORDER BY Rank)
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

                # Adjust scroll to keep selected item visible (both cases)
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
            elif key == ord('l') or key == ord('L'):  # Issue links
                if tickets and selected_idx < len(tickets):
                    current_key = tickets[selected_idx].get('key')
                    self._handle_issue_links(stdscr, current_key, tickets, height, width)
                    # Refresh current ticket after link change (both cache and list)
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
                        tickets, single_ticket_mode = self._fetch_tickets(new_query, stdscr=stdscr)
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
            elif key == ord('w') or key == ord('W'):  # Weight (story points)
                if tickets and selected_idx < len(tickets):
                    current_key = tickets[selected_idx].get('key')
                    self._handle_weight_edit(stdscr, current_key, height, width)
                    # Mark as stale since weight change may affect query match
                    self.stale_tickets.add(current_key)
                    # Refresh current ticket after weight edit (both cache and list)
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
            elif key == ord('s'):  # New query
                is_edit_mode = False
                new_query = self._handle_query_change(stdscr, current_query, is_edit_mode, height, width)
                if new_query:
                    # Re-fetch tickets with new query
                    stdscr.addstr(0, 0, "Loading tickets...")
                    stdscr.refresh()

                    try:
                        tickets, single_ticket_mode = self._fetch_tickets(new_query, stdscr=stdscr)
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
                        tickets, single_ticket_mode = self._fetch_tickets(new_query, stdscr=stdscr)
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

    def _add_rank_order_to_query(self, query: str) -> str:
        """Replace any existing ORDER BY with ORDER BY Rank ASC."""
        query_upper = query.upper()
        if 'ORDER BY' in query_upper:
            # Query already has ORDER BY - remove it and add our own
            order_pos = query_upper.find('ORDER BY')
            before_order = query[:order_pos].rstrip()
            return f"{before_order} ORDER BY Rank ASC"
        else:
            # No ORDER BY - add it
            return f"{query} ORDER BY Rank ASC"

    def _fetch_tickets(self, query_or_ticket: str, progress_callback=None, stdscr=None) -> tuple:
        """
        Fetch tickets from Jira.

        Args:
            query_or_ticket: Ticket key or JQL query
            progress_callback: Optional callback for progress updates
            stdscr: Optional curses screen for count progress and interruption

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
                query_or_ticket, fields, expand='changelog', progress_callback=progress_callback, stdscr=stdscr
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

    def _find_exact_user_matches(self, users: List[dict], query: str) -> List[dict]:
        """Find users with exact matches on displayName, email, or email prefix.

        Note: Jira API sometimes returns empty emailAddress fields, so we also
        check if displayName looks like an email and extract prefix from that.

        Args:
            users: List of user dicts from Jira
            query: Search query from user input

        Returns:
            List of users that exactly match the query
        """
        query_lower = query.lower().strip()
        exact_matches = []

        for user in users:
            display_name = user.get('displayName', '').lower()
            email = user.get('emailAddress', '').lower()

            # Extract email prefix from emailAddress field if available
            email_prefix = email.split('@')[0] if '@' in email and email else ''

            # Also check if displayName is email-like (contains @) and extract prefix
            display_email_prefix = ''
            if '@' in display_name:
                display_email_prefix = display_name.split('@')[0].strip()

            # Exact match on any of: displayName, full email, email prefix, or display name prefix
            if query_lower in [display_name, email, email_prefix, display_email_prefix]:
                exact_matches.append(user)

        return exact_matches

    def _prompt_for_user_selection(self, users: List[dict], query: str,
                                   field_name: str, allow_none: bool,
                                   stdscr, height: int, width: int):
        """Show interactive user picker. Returns accountId, None (unassigned), or False (cancelled)."""
        menu_height = min(len(users) + 8, height - 4)
        menu_width = min(70, width - 4)
        start_y = (height - menu_height) // 2
        start_x = (width - menu_width) // 2

        cursor_pos = 0
        search_filter = query or ""
        type_buffer = ""

        try:
            overlay = curses.newwin(menu_height, menu_width, start_y, start_x)

            while True:
                # Filter users by search
                filtered = users
                if search_filter:
                    search_lower = search_filter.lower()
                    filtered = [u for u in users if
                               search_lower in u.get('displayName', '').lower() or
                               search_lower in u.get('emailAddress', '').lower()]

                # Build display list
                options = []
                if allow_none:
                    options.append(("__NONE__", "(Unassigned)"))

                for user in filtered:
                    # Use consistent formatting: "Real Name (username)"
                    label = self.viewer.utils.format_user(user)
                    options.append((user.get('accountId'), label))

                cursor_pos = min(cursor_pos, len(options) - 1) if options else 0

                overlay.clear()
                overlay.box()
                overlay.addstr(0, 2, f" Select {field_name} ", curses.A_BOLD)

                # Show search filter or type buffer
                if search_filter:
                    overlay.addstr(1, 2, f"Filter: {search_filter}", curses.A_DIM)
                elif type_buffer:
                    overlay.addstr(1, 2, f"Search: {type_buffer}", curses.A_DIM)

                # List options with scrolling
                visible_height = menu_height - 5
                start_idx = max(0, cursor_pos - visible_height + 1) if cursor_pos >= visible_height else 0

                for idx in range(start_idx, min(start_idx + visible_height, len(options))):
                    _, label = options[idx]
                    attr = curses.A_REVERSE if idx == cursor_pos else curses.A_NORMAL
                    overlay.addstr(idx - start_idx + 2, 2, label[:menu_width - 4], attr)

                # Footer
                overlay.addstr(menu_height - 2, 2, f"Showing {len(options)} users")
                overlay.addstr(menu_height - 1, 2, "Enter: select  /: filter  q: cancel")
                overlay.refresh()

                # Handle input
                ch = overlay.getch()

                if ch == ord('q') or ch == 27:  # q or ESC
                    return False  # Cancelled
                elif ch in [curses.KEY_DOWN, ord('j')]:
                    cursor_pos = min(cursor_pos + 1, len(options) - 1)
                    type_buffer = ""  # Clear type buffer on navigation
                elif ch in [curses.KEY_UP, ord('k')]:
                    cursor_pos = max(cursor_pos - 1, 0)
                    type_buffer = ""  # Clear type buffer on navigation
                elif ch == ord('/'):  # Start filtering
                    curses.echo()
                    curses.curs_set(1)
                    overlay.addstr(menu_height - 3, 2, "Filter: ")
                    overlay.clrtoeol()
                    overlay.refresh()
                    filter_input = overlay.getstr(menu_height - 3, 10, menu_width - 14).decode('utf-8', errors='ignore')
                    curses.noecho()
                    curses.curs_set(0)
                    search_filter = filter_input.strip()
                    type_buffer = ""
                    cursor_pos = 0
                elif ch == ord('\n'):  # Enter to select
                    if options:
                        account_id, _ = options[cursor_pos]
                        return None if account_id == "__NONE__" else account_id
                elif ch == curses.KEY_BACKSPACE or ch == 127:  # Backspace
                    if type_buffer:
                        type_buffer = type_buffer[:-1]
                        # Apply filter with shortened buffer
                        if type_buffer:
                            search_filter = type_buffer
                            cursor_pos = 0
                elif 32 <= ch <= 126:  # Printable ASCII - type-to-search
                    char = chr(ch)
                    type_buffer += char
                    search_filter = type_buffer
                    cursor_pos = 0

        except curses.error:
            return False

    def _resolve_user_field(self, field_name: str, input_value: str,
                            stdscr, height: int, width: int) -> tuple:
        """Resolve user input to accountId. Returns (success, accountId_or_None, error_msg).

        Args:
            field_name: Field name ('assignee' or 'reporter')
            input_value: User's text input from template
            stdscr: Curses screen object
            height: Terminal height
            width: Terminal width

        Returns:
            Tuple of (success: bool, accountId or None, error_msg: str or None)
        """
        input_value = input_value.strip()

        # Handle empty or "None"
        if not input_value or input_value.lower() == 'none':
            # Only assignee can be None, reporter is required
            if field_name == 'reporter':
                return (False, None, "Reporter cannot be empty")
            return (True, None, None)

        # Handle currentUser() specially - resolve to actual accountId
        if input_value.lower() == 'currentuser()':
            current_user_id = self.viewer.utils.get_current_user_id()
            if not current_user_id:
                return (False, None, "Unable to get current user from API")
            return (True, current_user_id, None)

        # Check cache first for exact match on formatted user string
        # This handles cases where user doesn't change the pre-filled value
        cache = self.viewer.utils._user_cache
        for account_id, user in cache.items():
            formatted = self.viewer.utils.format_user(user)
            if formatted == input_value:
                return (True, account_id, None)

        # Search users with the input as query (real-time API search)
        # Note: Jira API requires at least 2 characters for meaningful results
        if len(input_value) < 2:
            return (False, None, f"Search query too short - need at least 2 characters")

        all_users = self.viewer.utils.get_users(query=input_value)

        if not all_users:
            return (False, None, f"No users found matching '{input_value}'. Try full name, email, or username (e.g., 'cmyers')")

        # Find exact matches (displayName, email, email prefix)
        exact_matches = self._find_exact_user_matches(all_users, input_value)

        if len(exact_matches) == 1:
            # Perfect match - auto-select
            return (True, exact_matches[0].get('accountId'), None)
        elif len(exact_matches) > 1:
            # Multiple exact matches - show picker with exact matches only
            allow_none = (field_name == 'assignee')
            result = self._prompt_for_user_selection(
                exact_matches, input_value, field_name, allow_none,
                stdscr, height, width)

            if result is False:
                return (False, None, None)  # User cancelled

            return (True, result, None)

        # Find substring matches for picker (including email prefix)
        # Note: Jira API sometimes returns empty emailAddress, so check displayName too
        query_lower = input_value.lower()
        substring_matches = []
        for u in all_users:
            display_name = u.get('displayName', '').lower()
            email = u.get('emailAddress', '').lower()

            # Extract email prefix from emailAddress if available
            email_prefix = email.split('@')[0] if '@' in email and email else ''

            # Also check displayName if it looks like an email
            display_email_prefix = ''
            if '@' in display_name:
                display_email_prefix = display_name.split('@')[0].strip()

            if (query_lower in display_name or
                query_lower in email or
                query_lower in email_prefix or
                query_lower in display_email_prefix):
                substring_matches.append(u)

        if len(substring_matches) == 0:
            return (False, None, f"No users found matching '{input_value}' (searched {len(all_users)} users)")

        # Multiple matches - show picker
        allow_none = (field_name == 'assignee')
        result = self._prompt_for_user_selection(
            substring_matches, input_value, field_name, allow_none,
            stdscr, height, width)

        if result is False:
            return (False, None, None)  # User cancelled

        return (True, result, None)  # result is accountId or None

    def _resolve_all_user_fields(self, fields: dict, stdscr, height: int, width: int) -> tuple:
        """Resolve all user fields in dict. Returns (success, resolved_fields_or_error_msg).

        Args:
            fields: Dictionary of field names to values
            stdscr: Curses screen object
            height: Terminal height
            width: Terminal width

        Returns:
            Tuple of (success: bool, resolved_fields or error_message)
        """
        resolved = fields.copy()

        for field_name in ['assignee', 'reporter']:
            if field_name not in fields:
                continue

            raw_value = fields[field_name]
            success, account_id, error_msg = self._resolve_user_field(
                field_name, raw_value, stdscr, height, width)

            if not success:
                if error_msg:
                    return (False, f"{field_name}: {error_msg}")
                else:
                    return (False, None)  # User cancelled

            resolved[field_name] = account_id

        return (True, resolved)

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

            # List transitions with underlined first letters
            for idx in range(max_visible):
                transition = transitions[idx]
                name = transition.get('name', 'Unknown')
                # Show target state name
                to_status = transition.get('to', {}).get('name', '')

                # Underline first letter of transition name
                prefix = f"{idx + 1}. "
                overlay.addstr(idx + 2, 2, prefix)
                if name:
                    overlay.addstr(idx + 2, 2 + len(prefix), name[0], curses.A_UNDERLINE)
                    rest = f"{name[1:]} -> {to_status}"
                    overlay.addstr(idx + 2, 2 + len(prefix) + 1, rest[:overlay_width - 6 - len(prefix) - 1])

            if len(transitions) > max_visible:
                overlay.addstr(max_visible + 2, 2, f"... and {len(transitions) - max_visible} more")

            overlay.addstr(overlay_height - 2, 2, "Enter number, first letter, or q to cancel: ")
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
                elif chr(ch).isalpha() and not input_str:
                    # First letter shortcut - find matching transition
                    letter = chr(ch).lower()
                    for idx, transition in enumerate(transitions):
                        name = transition.get('name', '')
                        if name and name[0].lower() == letter:
                            input_str = str(idx + 1)
                            break

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

            # List resolutions with underlined first letters
            for idx in range(max_visible):
                resolution = resolutions[idx]
                name = resolution.get('name', 'Unknown')

                # Underline first letter of resolution name
                prefix = f"{idx + 1}. "
                overlay.addstr(idx + 2, 2, prefix)
                if name:
                    overlay.addstr(idx + 2, 2 + len(prefix), name[0], curses.A_UNDERLINE)
                    overlay.addstr(idx + 2, 2 + len(prefix) + 1, name[1:overlay_width - 6 - len(prefix) - 1])

            if len(resolutions) > max_visible:
                overlay.addstr(max_visible + 2, 2, f"... and {len(resolutions) - max_visible} more")

            overlay.addstr(overlay_height - 2, 2, "Enter number, first letter, or q to cancel: ")
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
                elif chr(ch).isalpha() and not input_str:
                    # First letter shortcut - find matching resolution
                    letter = chr(ch).lower()
                    for idx, resolution in enumerate(resolutions):
                        name = resolution.get('name', '')
                        if name and name[0].lower() == letter:
                            input_str = str(idx + 1)
                            break

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
            assignee = fields.get('assignee')
            assignee_display = self.viewer.utils.format_user(assignee) if assignee else 'Unassigned'
            f.write(f"# Assignee: {assignee_display}\n")
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
            assignee = fields.get('assignee')
            assignee_display = self.viewer.utils.format_user(assignee) if assignee else 'Unassigned'
            f.write(f"# Assignee: {assignee_display}\n")
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

                # Resolve user fields (assignee, reporter)
                success, resolved = self._resolve_all_user_fields(fields, stdscr, height, width)
                if not success:
                    if resolved:  # Error message - retry
                        previous_fields = fields
                        error_message = resolved
                        continue
                    else:  # User cancelled
                        self._show_message(stdscr, "Issue creation cancelled", height, width)
                        return None

                # Create issue with resolved accountIds
                success, result = self._create_jira_issue(resolved)
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

                # Resolve user fields in changes (assignee, reporter)
                if changes:
                    success, resolved = self._resolve_all_user_fields(changes, stdscr, height, width)
                    if not success:
                        if resolved:  # Error message - retry
                            error_message = resolved
                            continue
                        else:  # User cancelled
                            self._show_message(stdscr, "Edit cancelled", height, width)
                            return
                    changes = resolved

                # Update issue with resolved accountIds
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

    def _handle_weight_edit(self, stdscr, ticket_key: str, height: int, width: int):
        """Handle quick weight/story points edit (w key)."""
        import tempfile
        import subprocess
        import os

        # Get current ticket
        with self.loading_lock:
            ticket = self.ticket_cache.get(ticket_key)

        if not ticket:
            ticket = self.viewer.fetch_ticket_details(ticket_key)

        if not ticket:
            self._show_message(stdscr, "Failed to load ticket", height, width)
            return

        error_message = None
        while True:
            # Create template
            fields = ticket.get('fields', {})
            current_points = fields.get('customfield_10061', '')

            template = []
            if error_message:
                template.append(f"# ERROR: {error_message}")
                template.append("#")

            template.extend([
                f"# Edit Story Points for {ticket_key}",
                "# Set empty to clear story points",
                "# Lines starting with # are ignored",
                "",
                f"story_points: {current_points}",
                "",
                "# Optional comment (leave empty for no comment):",
                "comment:",
                "",
                "",
                "__END_OF_COMMENT__",
            ])

            # Write to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                temp_path = f.name
                f.write('\n'.join(template))

            # Open vim
            curses.def_prog_mode()
            curses.endwin()

            try:
                subprocess.call(['vim', temp_path])
            finally:
                curses.reset_prog_mode()
                stdscr.refresh()

            # Parse result
            try:
                with open(temp_path, 'r') as f:
                    text = f.read()

                os.unlink(temp_path)

                parsed = self._parse_weight_template(text)
                if parsed is None:
                    self._show_message(stdscr, "Weight edit cancelled", height, width)
                    return

                new_points = parsed.get('story_points', '')
                comment_text = parsed.get('comment', '')

                # Check if changed
                old_points = str(current_points) if current_points else ''
                if new_points == old_points and not comment_text:
                    self._show_message(stdscr, "No changes", height, width)
                    return

                # Update issue
                changes = {}
                if new_points != old_points:
                    changes['story_points'] = new_points

                success, result = self._update_jira_issue(ticket_key, changes, comment_text)

                if success:
                    msg = f"✓ Updated {ticket_key}"
                    if new_points:
                        msg += f" (story points: {new_points})"
                    else:
                        msg += " (story points cleared)"
                    self._show_message(stdscr, msg, height, width)
                    return
                else:
                    error_message = result
                    # Continue loop to retry

            except Exception as e:
                error_message = f"Error: {str(e)}"
                # Continue loop to retry

    def _parse_weight_template(self, text: str) -> Optional[dict]:
        """Parse weight edit template."""
        lines = text.split('\n')
        result = {}
        current_field = None
        field_lines = []

        for line in lines:
            # Skip comments
            if line.strip().startswith('#'):
                continue

            # Check for field markers
            if line.strip() == '__END_OF_COMMENT__':
                if current_field:
                    result[current_field] = '\n'.join(field_lines).strip()
                current_field = None
                field_lines = []
                continue

            # Check for field start
            if ':' in line and not current_field:
                field, value = line.split(':', 1)
                field = field.strip()
                value = value.strip()

                if field == 'comment':
                    current_field = 'comment'
                    field_lines = []
                elif field == 'story_points':
                    result['story_points'] = value
            elif current_field:
                # Accumulate multi-line field
                field_lines.append(line)

        # Check if completely empty (cancelled)
        if not any(result.values()):
            return None

        return result

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
            "# For assignee/reporter: use Full Name, email, or username",
            "#   - 'None' for unassigned (assignee only)",
            "#   - Will prompt with picker for multiple matches",
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
            template.append("# Users: Use 'None' to unassign, 'currentUser()' for yourself, or search by name/email/username (partial matches show picker)")
            assignee = previous_fields.get('assignee', '')
            if assignee:
                template.append(f"assignee: {assignee}")
            else:
                template.append("# assignee: currentUser()")

            reporter = previous_fields.get('reporter', '')
            if reporter:
                template.append(f"reporter: {reporter}")
            else:
                template.append("# reporter: currentUser()")

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
                "# Users: Use 'None' to unassign, 'currentUser()' for yourself, or search by name/email/username (partial matches show picker)",
                "# assignee: currentUser()",
                "# reporter: currentUser()",
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
            # accountId already resolved by _resolve_all_user_fields()
            payload['fields']['assignee'] = {"accountId": fields['assignee']}

        if 'reporter' in fields and fields['reporter']:
            # accountId already resolved by _resolve_all_user_fields()
                payload['fields']['reporter'] = {"accountId": fields['reporter']}

        if 'priority' in fields and fields['priority']:
            payload['fields']['priority'] = {"name": fields['priority']}

        if 'labels' in fields and fields['labels']:
            labels = [l.strip() for l in fields['labels'].split(',')]
            payload['fields']['labels'] = labels

        if 'story_points' in fields and fields['story_points']:
            value = fields['story_points']
            # Check if value is explicitly None (case-insensitive) - skip adding the field
            if value.strip().lower() != 'none':
                try:
                    payload['fields']['customfield_10061'] = int(value)
                except ValueError:
                    return (False, f"Invalid story points value: {value}")

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
            "# For assignee/reporter: use Full Name, email, or username",
            "#   - 'None' for unassigned (assignee only)",
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

        # Format users consistently as "Real Name (username)"
        assignee = fields.get('assignee')
        assignee_name = self.viewer.utils.format_user(assignee) if assignee else 'None'

        reporter = fields.get('reporter')
        reporter_name = self.viewer.utils.format_user(reporter) if reporter else ''

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
            "# Users: Use 'None' to unassign, 'currentUser()' for yourself, or search by name/email/username (partial matches show picker)",
            f"assignee: {assignee_name}",
            f"reporter: {reporter_name}",
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

        # Format users consistently as "Real Name (username)"
        assignee = fields.get('assignee')
        assignee_name = self.viewer.utils.format_user(assignee) if assignee else 'None'

        reporter = fields.get('reporter')
        reporter_name = self.viewer.utils.format_user(reporter) if reporter else ''

        priority = fields.get('priority')
        priority_name = priority.get('name', '') if priority else ''

        labels = fields.get('labels', [])
        labels_str = ', '.join(labels) if labels else ''

        return {
            'summary': fields.get('summary', ''),
            'description': description,
            'assignee': assignee_name,
            'reporter': reporter_name,
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
                # accountId already resolved by _resolve_all_user_fields()
                update_payload['fields']['assignee'] = {"accountId": changes['assignee']}
            else:
                update_payload['fields']['assignee'] = None

        if 'reporter' in changes:
            if changes['reporter']:
                # accountId already resolved by _resolve_all_user_fields()
                update_payload['fields']['reporter'] = {"accountId": changes['reporter']}
            else:
                # Reporter field cannot be null in Jira
                return (False, "Reporter cannot be empty")

        if 'priority' in changes and changes['priority']:
            update_payload['fields']['priority'] = {"name": changes['priority']}

        if 'labels' in changes:
            if changes['labels']:
                labels = [l.strip() for l in changes['labels'].split(',')]
                update_payload['fields']['labels'] = labels
            else:
                update_payload['fields']['labels'] = []

        if 'story_points' in changes:
            value = changes['story_points']
            # Check if value is explicitly None or empty (case-insensitive)
            if not value or value.strip().lower() == 'none':
                update_payload['fields']['customfield_10061'] = None
            else:
                try:
                    update_payload['fields']['customfield_10061'] = int(value)
                except ValueError:
                    return (False, f"Invalid story points value: {value}")

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

    def _handle_vim_navigation(self, stdscr, tickets: List[dict], selected_idx: int,
                               scroll_offset: int, height: int, count: int, command: str) -> tuple:
        """Handle vim-style navigation commands uniformly.

        Args:
            stdscr: Curses screen
            tickets: List of tickets
            selected_idx: Current selection index
            scroll_offset: Current scroll offset
            height: Screen height
            count: Number prefix (0 means no prefix)
            command: Command character ('j', 'k', 'gg', 'G')

        Returns:
            Tuple of (new_selected_idx, new_scroll_offset)
        """
        visible_height = self._get_visible_height(height)

        if command == 'j':
            # <count>j - move down by count (default 1)
            move_by = count if count > 0 else 1
            new_idx = min(selected_idx + move_by, len(tickets) - 1)
            selected_idx = new_idx
            # Adjust scroll if needed
            if selected_idx >= scroll_offset + visible_height:
                scroll_offset = selected_idx - visible_height + 1
            elif selected_idx < scroll_offset:
                scroll_offset = selected_idx

        elif command == 'k':
            # <count>k - move up by count (default 1)
            move_by = count if count > 0 else 1
            new_idx = max(selected_idx - move_by, 0)
            selected_idx = new_idx
            if selected_idx < scroll_offset:
                scroll_offset = selected_idx

        elif command == 'gg':
            # <count>gg - go to line count (1-indexed), or top if count=0
            if count == 0:
                # gg with no count - go to top
                selected_idx = 0
                scroll_offset = 0
            else:
                # <count>gg - go to line number (1-indexed)
                new_idx = min(max(count - 1, 0), len(tickets) - 1)
                selected_idx = new_idx
                # Center the view
                scroll_offset = max(0, selected_idx - visible_height // 2)

        elif command == 'G':
            # <count>G - go to count lines from end, or bottom if count=0
            if count == 0:
                # G with no count - go to bottom
                selected_idx = len(tickets) - 1
                scroll_offset = max(0, len(tickets) - visible_height)
            else:
                # <count>G - go to count from the end (like vim)
                # In vim, 50G goes to line 50 (absolute), not 50 from end
                # Let me check vim behavior... actually 50G goes to line 50
                # So it's the same as 50gg
                new_idx = min(max(count - 1, 0), len(tickets) - 1)
                selected_idx = new_idx
                # Center the view
                scroll_offset = max(0, selected_idx - visible_height // 2)

        # Reset detail scroll when changing tickets
        self.detail_scroll_offset = 0

        return (selected_idx, scroll_offset)

    def _read_number_from_key(self, stdscr, first_key: int, display_callback=None) -> int:
        """Read a multi-digit number starting with first_key.

        Args:
            stdscr: Curses screen
            first_key: The first digit key pressed
            display_callback: Optional callback(digits_str) to update display while typing

        Returns:
            The parsed number, or 0 if no valid number
        """
        digits = [chr(first_key)]

        # Show initial digit
        if display_callback:
            display_callback(''.join(digits))

        # Set short timeout for additional digits
        stdscr.timeout(500)

        while True:
            try:
                next_key = stdscr.getch()
                if ord('0') <= next_key <= ord('9'):
                    digits.append(chr(next_key))
                    if display_callback:
                        display_callback(''.join(digits))
                else:
                    # Not a digit, push it back (by not consuming it)
                    break
            except:
                break

        stdscr.timeout(-1)  # Reset to blocking

        try:
            return int(''.join(digits))
        except ValueError:
            return 0  # Default to 0 if parse fails

    def _handle_backlog_move(self, stdscr, tickets: List[dict], all_tickets: List[dict],
                            selected_idx: int, scroll_offset: int, current_query: str,
                            direction: str, count: int, height: int, width: int):
        """
        Handle moving an issue in the backlog.

        Args:
            stdscr: Curses screen
            tickets: Current ticket list
            all_tickets: Full ticket list
            selected_idx: Currently selected index
            scroll_offset: Current scroll offset
            current_query: Current JQL query
            direction: 'up', 'down', 'top', or 'bottom'
            count: Number of positions to move (ignored for top/bottom)
            height: Screen height
            width: Screen width

        Returns:
            (new_tickets, new_selected_idx, new_scroll_offset)
        """
        if not tickets or selected_idx >= len(tickets):
            self._show_message(stdscr, "No ticket selected", height, width)
            return tickets, selected_idx, scroll_offset

        if len(tickets) == 1:
            self._show_message(stdscr, "Only one ticket in list", height, width)
            return tickets, selected_idx, scroll_offset

        current_ticket_key = tickets[selected_idx].get('key')
        current_pos = selected_idx

        # Calculate target position
        if direction == 'top':
            target_pos = 0
        elif direction == 'bottom':
            target_pos = len(tickets) - 1
        elif direction == 'up':
            target_pos = max(0, current_pos - count)
        elif direction == 'down':
            target_pos = min(len(tickets) - 1, current_pos + count)
        else:
            return tickets, selected_idx, scroll_offset

        # Check if already at target
        if current_pos == target_pos:
            if target_pos == 0:
                self._show_message(stdscr, "Already at top", height, width)
            elif target_pos == len(tickets) - 1:
                self._show_message(stdscr, "Already at bottom", height, width)
            else:
                self._show_message(stdscr, "Already at position", height, width)
            return tickets, selected_idx, scroll_offset

        # Determine anchor issue for ranking
        if target_pos == 0:
            # Moving to top - rank before first issue
            anchor_key = tickets[0].get('key')
            rank_before = anchor_key
            rank_after = None
        elif target_pos == len(tickets) - 1:
            # Moving to bottom - rank after last issue
            anchor_key = tickets[-1].get('key')
            rank_before = None
            rank_after = anchor_key
        elif target_pos < current_pos:
            # Moving up - rank before target position
            anchor_key = tickets[target_pos].get('key')
            rank_before = anchor_key
            rank_after = None
        else:
            # Moving down - rank after target position
            anchor_key = tickets[target_pos].get('key')
            rank_before = None
            rank_after = anchor_key

        # Show moving message
        self._show_message(stdscr, f"Moving {current_ticket_key}...", height, width, duration=300)

        # Call rank API
        success, error = self.viewer.utils.rank_issues(
            [current_ticket_key],
            rank_before=rank_before,
            rank_after=rank_after
        )

        if not success:
            self._show_message(stdscr, f"✗ Failed: {error}", height, width, duration=3000)
            return tickets, selected_idx, scroll_offset

        # Give Jira a moment to update its database after rank change
        time.sleep(0.5)

        # Re-fetch query with basic fields (fast refresh)
        self._show_message(stdscr, "Refreshing...", height, width, duration=100)

        basic_fields = ['key', 'summary', 'status', 'priority', 'assignee',
                       'updated', 'customfield_10061', 'customfield_10021',
                       'customfield_10023', 'customfield_10022']  # Include rank!

        try:
            # current_query is already modified in backlog mode (has ORDER BY Rank)
            new_all_tickets = self.viewer.utils.fetch_all_jql_results(current_query, basic_fields)

            # Tickets are already in rank order from Jira
            new_tickets_sorted = new_all_tickets

            # Find new position of moved ticket
            new_idx = next((i for i, t in enumerate(new_tickets_sorted)
                           if t.get('key') == current_ticket_key), 0)

            # Update all_tickets and tickets (both sorted in backlog mode)
            all_tickets = new_tickets_sorted
            tickets = new_tickets_sorted

            # Adjust scroll to keep selection visible
            visible_height = self._get_visible_height(height)
            if new_idx < scroll_offset:
                new_scroll = new_idx
            elif new_idx >= scroll_offset + visible_height:
                new_scroll = new_idx - visible_height + 1
            else:
                new_scroll = scroll_offset

            # Show success
            self._show_message(stdscr, f"✓ Moved to position {new_idx + 1}", height, width)

            # Note: Full details will be loaded on-demand when tickets are selected
            return tickets, new_idx, new_scroll

        except Exception as e:
            self._show_message(stdscr, f"✗ Refresh failed: {str(e)}", height, width, duration=3000)
            return tickets, selected_idx, scroll_offset

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

    def _handle_cache_refresh(self, stdscr, height: int, width: int):
        """Handle cache refresh menu (Shift+R)."""
        cache = self.viewer.utils.cache

        # Build menu options with cache ages
        options = [
            ('link_types', 'Link Types', cache.get_age('link_types')),
            ('users', 'Users', cache.get_age('users', key='all')),
            ('all', 'All Cache', '')
        ]

        # Draw menu
        menu_height = len(options) + 5
        menu_width = min(60, width - 4)
        start_y = (height - menu_height) // 2
        start_x = (width - menu_width) // 2

        cursor_pos = 0

        try:
            overlay = curses.newwin(menu_height, menu_width, start_y, start_x)

            while True:
                overlay.clear()
                overlay.box()
                overlay.addstr(0, 2, " Refresh Cache ", curses.A_BOLD)

                # List options
                for idx, (category, label, age) in enumerate(options):
                    attr = curses.A_REVERSE if idx == cursor_pos else curses.A_NORMAL

                    if age:
                        option_text = f" {idx + 1}. {label} (cached {age})"
                    else:
                        option_text = f" {idx + 1}. {label}"

                    overlay.addstr(idx + 2, 2, option_text[:menu_width - 4], attr)

                overlay.addstr(menu_height - 2, 2, "Enter: refresh  q: cancel")
                overlay.refresh()

                # Get user input
                ch = overlay.getch()

                if ch == ord('q') or ch == 27:  # q or ESC
                    return
                elif ch in [curses.KEY_DOWN, ord('j')]:
                    cursor_pos = min(cursor_pos + 1, len(options) - 1)
                elif ch in [curses.KEY_UP, ord('k')]:
                    cursor_pos = max(cursor_pos - 1, 0)
                elif ch == ord('\n'):  # Enter to refresh
                    category, label, _ = options[cursor_pos]

                    # Show refreshing message
                    overlay.clear()
                    overlay.box()
                    overlay.addstr(0, 2, " Refresh Cache ", curses.A_BOLD)
                    overlay.addstr(menu_height // 2, 2, f"Refreshing {label.lower()}...")
                    overlay.refresh()

                    # Perform refresh
                    if category == 'all':
                        # Refresh all categories
                        self.viewer.utils.get_link_types(force_refresh=True)
                        self.viewer.utils.get_users(force_refresh=True)
                        self._show_message(stdscr, "✓ Refreshed all cache", height, width)
                    elif category == 'link_types':
                        self.viewer.utils.get_link_types(force_refresh=True)
                        self._show_message(stdscr, "✓ Refreshed link types cache", height, width)
                    elif category == 'users':
                        self.viewer.utils.get_users(force_refresh=True)
                        self._show_message(stdscr, "✓ Refreshed users cache", height, width)

                    return

        except curses.error:
            pass

    def _get_status_color(self, status_letter: str) -> int:
        """Get curses color pair for a status letter."""
        if status_letter in ['C', 'V', 'Z', 'Y', 'M']:
            return curses.color_pair(1)  # Green for done
        elif status_letter in ['A', 'B', 'S', 'W']:
            return curses.color_pair(3)  # Blue for backlog
        elif status_letter in ['P', 'R', 'Q', 'T']:
            return curses.color_pair(2)  # Yellow for active
        elif status_letter in ['D', 'X', '_']:
            return curses.color_pair(4)  # Red for blocked
        else:
            return curses.A_NORMAL

    def _handle_issue_links(self, stdscr, ticket_key: str, tickets: list, height: int, width: int):
        """Handle managing issue links (L key)."""
        # Get ticket details to check for existing links
        with self.loading_lock:
            ticket = self.ticket_cache.get(ticket_key)

        if not ticket:
            ticket = self.viewer.fetch_ticket_details(ticket_key)

        if not ticket:
            self._show_message(stdscr, "Failed to load ticket", height, width)
            return

        # Check for existing issue links
        fields = ticket.get('fields', {})
        issue_links = fields.get('issuelinks', [])

        # Show action menu (conditionally shows Remove if links exist)
        action = self._show_link_action_menu(stdscr, height, width, has_links=len(issue_links) > 0)

        if action == 'add':
            self._add_issue_link(stdscr, ticket_key, tickets, height, width)
        elif action == 'remove':
            self._remove_issue_link(stdscr, ticket_key, issue_links, height, width)

    def _show_link_action_menu(self, stdscr, height: int, width: int, has_links: bool) -> Optional[str]:
        """Show conditional menu for link actions. Returns 'add', 'remove', or None."""
        options = [('add', 'Add Link')]
        if has_links:
            options.append(('remove', 'Remove Link'))

        menu_height = len(options) + 4
        menu_width = min(40, width - 4)
        start_y = (height - menu_height) // 2
        start_x = (width - menu_width) // 2

        cursor_pos = 0

        try:
            overlay = curses.newwin(menu_height, menu_width, start_y, start_x)

            while True:
                overlay.clear()
                overlay.box()
                overlay.addstr(0, 2, " Issue Links ", curses.A_BOLD)

                for idx, (action, label) in enumerate(options):
                    attr = curses.A_REVERSE if idx == cursor_pos else curses.A_NORMAL
                    # Highlight first letter
                    first_letter = label[0].lower()
                    overlay.addstr(idx + 2, 2, f" {idx + 1}. ", attr)
                    overlay.addstr(idx + 2, 2 + len(f" {idx + 1}. "), label[0], attr | curses.A_UNDERLINE)
                    overlay.addstr(idx + 2, 2 + len(f" {idx + 1}. ") + 1, label[1:], attr)

                overlay.addstr(menu_height - 1, 2, "a/r or Enter: select  q: cancel")
                overlay.refresh()

                ch = overlay.getch()

                if ch == ord('q') or ch == 27:  # q or ESC
                    return None
                elif ch in [curses.KEY_DOWN, ord('j')]:
                    cursor_pos = min(cursor_pos + 1, len(options) - 1)
                elif ch in [curses.KEY_UP, ord('k')]:
                    cursor_pos = max(cursor_pos - 1, 0)
                elif ch == ord('\n'):  # Enter
                    action, _ = options[cursor_pos]
                    return action
                elif ch == ord('a') or ch == ord('A'):  # Add
                    return 'add'
                elif ch == ord('r') or ch == ord('R'):  # Remove
                    if has_links:
                        return 'remove'

        except curses.error:
            return None

    def _prompt_for_link_type(self, stdscr, height: int, width: int) -> Optional[dict]:
        """Prompt user to select link type. Returns link type dict or None. Supports 'R' to refresh cache."""
        cache = self.viewer.utils.cache
        link_types = self.viewer.utils.get_link_types()

        if not link_types:
            self._show_message(stdscr, "✗ Failed to load link types", height, width)
            return None

        menu_height = min(len(link_types) + 6, height - 4)
        menu_width = min(60, width - 4)
        start_y = (height - menu_height) // 2
        start_x = (width - menu_width) // 2

        cursor_pos = 0
        search_filter = ""
        type_buffer = ""  # Buffer for type-to-jump

        try:
            overlay = curses.newwin(menu_height, menu_width, start_y, start_x)

            while True:
                # Filter link types by search
                filtered_types = link_types
                if search_filter:
                    search_lower = search_filter.lower()
                    filtered_types = [
                        lt for lt in link_types
                        if search_lower in lt.get('name', '').lower() or
                           search_lower in lt.get('inward', '').lower() or
                           search_lower in lt.get('outward', '').lower()
                    ]

                cursor_pos = min(cursor_pos, len(filtered_types) - 1) if filtered_types else 0

                overlay.clear()
                overlay.box()

                # Title with cache age
                cache_age = cache.get_age('link_types')
                title = f" Select Link Type (cached {cache_age}) "
                overlay.addstr(0, 2, title, curses.A_BOLD)

                # Show search filter or type buffer if active
                if search_filter:
                    overlay.addstr(1, 2, f"Filter: {search_filter}", curses.A_DIM)
                elif type_buffer:
                    overlay.addstr(1, 2, f"Jump: {type_buffer}", curses.A_DIM)

                # List filtered link types
                visible_height = menu_height - 5
                start_idx = max(0, cursor_pos - visible_height + 1) if cursor_pos >= visible_height else 0

                for idx in range(start_idx, min(start_idx + visible_height, len(filtered_types))):
                    lt = filtered_types[idx]
                    attr = curses.A_REVERSE if idx == cursor_pos else curses.A_NORMAL
                    name = lt.get('name', 'Unknown')
                    inward = lt.get('inward', '')
                    outward = lt.get('outward', '')
                    display = f" {name}: {inward} / {outward}"
                    overlay.addstr(idx - start_idx + 2, 2, display[:menu_width - 4], attr)

                # Footer
                overlay.addstr(menu_height - 2, 2, "R: refresh cache")
                overlay.addstr(menu_height - 1, 2, "Enter: select  /: filter  q: cancel")
                overlay.refresh()

                ch = overlay.getch()

                if ch == ord('q') or ch == 27:  # q or ESC
                    return None
                elif ch in [curses.KEY_DOWN, ord('j')]:
                    cursor_pos = min(cursor_pos + 1, len(filtered_types) - 1)
                    type_buffer = ""  # Clear type buffer on navigation
                elif ch in [curses.KEY_UP, ord('k')]:
                    cursor_pos = max(cursor_pos - 1, 0)
                    type_buffer = ""  # Clear type buffer on navigation
                elif ch == ord('R'):  # Refresh cache
                    overlay.clear()
                    overlay.box()
                    overlay.addstr(menu_height // 2, 2, "Refreshing link types...")
                    overlay.refresh()
                    link_types = self.viewer.utils.get_link_types(force_refresh=True)
                    if not link_types:
                        self._show_message(stdscr, "✗ Failed to refresh link types", height, width)
                        return None
                    # Reset filter and cursor
                    search_filter = ""
                    type_buffer = ""
                    cursor_pos = 0
                elif ch == ord('/'):  # Start filtering
                    curses.echo()
                    curses.curs_set(1)
                    overlay.addstr(menu_height - 3, 2, "Filter: ")
                    overlay.clrtoeol()
                    overlay.refresh()
                    filter_input = overlay.getstr(menu_height - 3, 10, menu_width - 14).decode('utf-8', errors='ignore')
                    curses.noecho()
                    curses.curs_set(0)
                    search_filter = filter_input.strip()
                    type_buffer = ""
                    cursor_pos = 0
                elif ch == ord('\n'):  # Enter to select
                    if filtered_types:
                        return filtered_types[cursor_pos]
                elif ch == curses.KEY_BACKSPACE or ch == 127:  # Backspace
                    if type_buffer:
                        type_buffer = type_buffer[:-1]
                        # Jump to match with shortened buffer
                        if type_buffer and filtered_types:
                            search_lower = type_buffer.lower()
                            for idx, lt in enumerate(filtered_types):
                                name = lt.get('name', '').lower()
                                if name.startswith(search_lower):
                                    cursor_pos = idx
                                    break
                elif 32 <= ch <= 126:  # Printable ASCII characters
                    # Add to type buffer and jump to match
                    char = chr(ch)
                    type_buffer += char

                    # Find first match (case insensitive)
                    search_lower = type_buffer.lower()
                    for idx, lt in enumerate(filtered_types):
                        name = lt.get('name', '').lower()
                        if name.startswith(search_lower):
                            cursor_pos = idx
                            break

        except curses.error:
            return None

    def _prompt_for_link_direction(self, stdscr, link_type: dict, height: int, width: int) -> Optional[str]:
        """Prompt user to select link direction. Returns 'inward' or 'outward' or None."""
        inward = link_type.get('inward', 'Inward')
        outward = link_type.get('outward', 'Outward')
        name = link_type.get('name', 'Unknown')

        options = [
            ('inward', inward),
            ('outward', outward)
        ]

        menu_height = 7
        menu_width = min(60, width - 4)
        start_y = (height - menu_height) // 2
        start_x = (width - menu_width) // 2

        cursor_pos = 0

        try:
            overlay = curses.newwin(menu_height, menu_width, start_y, start_x)

            while True:
                overlay.clear()
                overlay.box()
                overlay.addstr(0, 2, f" Link Direction ({name}) ", curses.A_BOLD)

                for idx, (direction, label) in enumerate(options):
                    attr = curses.A_REVERSE if idx == cursor_pos else curses.A_NORMAL
                    # Highlight first letter
                    overlay.addstr(idx + 2, 2, f" {idx + 1}. ", attr)
                    overlay.addstr(idx + 2, 2 + len(f" {idx + 1}. "), label[0], attr | curses.A_UNDERLINE)
                    overlay.addstr(idx + 2, 2 + len(f" {idx + 1}. ") + 1, label[1:], attr)

                overlay.addstr(menu_height - 1, 2, "i/o or Enter: select  q: cancel")
                overlay.refresh()

                ch = overlay.getch()

                if ch == ord('q') or ch == 27:  # q or ESC
                    return None
                elif ch in [curses.KEY_DOWN, ord('j')]:
                    cursor_pos = min(cursor_pos + 1, len(options) - 1)
                elif ch in [curses.KEY_UP, ord('k')]:
                    cursor_pos = max(cursor_pos - 1, 0)
                elif ch == ord('\n'):  # Enter
                    direction, _ = options[cursor_pos]
                    return direction
                elif ch == ord('i') or ch == ord('I'):  # Inward
                    return 'inward'
                elif ch == ord('o') or ch == ord('O'):  # Outward
                    return 'outward'

        except curses.error:
            return None

    def _prompt_for_jql_search(self, stdscr, height: int, width: int, has_current_tickets: bool = False, error_message: Optional[str] = None) -> Optional[str]:
        """Prompt user for JQL query. Returns JQL string, None, empty string, or '__SELECT_FROM_CURRENT__'."""
        menu_height = 8 if error_message else 7
        menu_width = min(70, width - 4)
        start_y = (height - menu_height) // 2
        start_x = (width - menu_width) // 2

        try:
            overlay = curses.newwin(menu_height, menu_width, start_y, start_x)
            overlay.keypad(True)

            # Show error message if provided
            if error_message:
                input_y = 4
                input_x = 10
            else:
                input_y = 3
                input_x = 10

            # Input buffer
            text = ""
            cursor_pos = 0
            max_width = menu_width - input_x - 3
            typing_mode = False  # Track if user has started typing

            curses.curs_set(1)

            while True:
                overlay.clear()
                overlay.box()
                overlay.addstr(0, 2, " Search for Issue ", curses.A_BOLD)

                if error_message:
                    overlay.addstr(1, 2, error_message[:menu_width - 4], curses.color_pair(4))  # Red color
                    overlay.addstr(2, 2, "Enter issue key or JQL query", curses.A_DIM)
                    overlay.addstr(4, 2, "Query: ")
                    if has_current_tickets and not typing_mode:
                        overlay.addstr(menu_height - 2, 2, "T: select from current / other key: type query")
                    overlay.addstr(menu_height - 1, 2, "ESC to go back  Empty to cancel")
                else:
                    overlay.addstr(1, 2, "Enter issue key or JQL query", curses.A_DIM)
                    overlay.addstr(3, 2, "Query: ")
                    if has_current_tickets and not typing_mode:
                        overlay.addstr(menu_height - 2, 2, "T: select from current / other key: type query")
                    overlay.addstr(menu_height - 1, 2, "ESC to go back  Empty to cancel")

                # Display text
                display_text = text[:max_width]
                overlay.addstr(input_y, input_x, display_text)
                overlay.move(input_y, input_x + min(cursor_pos, max_width))
                overlay.refresh()

                ch = overlay.getch()

                if ch == 27:  # ESC
                    curses.curs_set(0)
                    return None  # None means "go back"
                elif ch == ord('\n'):  # Enter
                    curses.curs_set(0)
                    # Return empty string "" for empty query (means "cancel completely")
                    # Return the query string for non-empty queries
                    return text.strip() if text.strip() else ""
                elif ch in [curses.KEY_BACKSPACE, 127, 8]:  # Backspace
                    if cursor_pos > 0:
                        text = text[:cursor_pos - 1] + text[cursor_pos:]
                        cursor_pos -= 1
                elif ch == curses.KEY_LEFT:
                    cursor_pos = max(0, cursor_pos - 1)
                elif ch == curses.KEY_RIGHT:
                    cursor_pos = min(len(text), cursor_pos + 1)
                elif ch == curses.KEY_HOME or ch == 1:  # Home or Ctrl-A
                    cursor_pos = 0
                elif ch == curses.KEY_END or ch == 5:  # End or Ctrl-E
                    cursor_pos = len(text)
                elif 32 <= ch <= 126:  # Printable ASCII
                    # Special handling for first keypress
                    if not typing_mode and has_current_tickets and (ch == ord('t') or ch == ord('T')):
                        # First key is 'T' - select from current tickets
                        curses.curs_set(0)
                        return "__SELECT_FROM_CURRENT__"
                    else:
                        # Enter typing mode and add character
                        typing_mode = True
                        if len(text) < max_width:
                            text = text[:cursor_pos] + chr(ch) + text[cursor_pos:]
                            cursor_pos += 1

        except curses.error:
            curses.curs_set(0)
            return None

    def _prompt_for_issue_selection(self, stdscr, jql: str, height: int, width: int) -> Optional[str]:
        """Execute JQL search and prompt user to select issue. Returns issue key or None."""
        # Show loading message
        menu_height = 5
        menu_width = min(60, width - 4)
        start_y = (height - menu_height) // 2
        start_x = (width - menu_width) // 2

        try:
            overlay = curses.newwin(menu_height, menu_width, start_y, start_x)
            overlay.clear()
            overlay.box()
            overlay.addstr(menu_height // 2, 2, "Searching...")
            overlay.refresh()

            # Normalize input (convert issue keys to JQL, upcase them)
            jql = self.normalize_jql_input(jql)

            # URL encode the JQL query
            encoded_jql = quote(jql)

            # Execute JQL query (using /search/jql endpoint, not deprecated /search)
            endpoint = f"/search/jql?jql={encoded_jql}&maxResults=50&fields=key,summary"
            response = self.viewer.utils.call_jira_api(endpoint)

            if not response or 'issues' not in response:
                return None

            issues = response['issues']
            if not issues:
                return None

            # Show issue selection menu
            menu_height = min(len(issues) + 6, height - 4)
            menu_width = min(80, width - 4)
            start_y = (height - menu_height) // 2
            start_x = (width - menu_width) // 2

            cursor_pos = 0
            search_filter = ""

            overlay = curses.newwin(menu_height, menu_width, start_y, start_x)

            while True:
                # Filter issues by search
                filtered_issues = issues
                if search_filter:
                    search_lower = search_filter.lower()
                    filtered_issues = [
                        issue for issue in issues
                        if search_lower in issue.get('key', '').lower() or
                           search_lower in issue.get('fields', {}).get('summary', '').lower()
                    ]

                cursor_pos = min(cursor_pos, len(filtered_issues) - 1) if filtered_issues else 0

                overlay.clear()
                overlay.box()
                overlay.addstr(0, 2, f" Select Issue ({len(filtered_issues)} found) ", curses.A_BOLD)

                # Show search filter if active
                if search_filter:
                    overlay.addstr(1, 2, f"Filter: {search_filter}", curses.A_DIM)

                # List filtered issues
                visible_height = menu_height - 4
                start_idx = max(0, cursor_pos - visible_height + 1) if cursor_pos >= visible_height else 0

                for idx in range(start_idx, min(start_idx + visible_height, len(filtered_issues))):
                    issue = filtered_issues[idx]
                    attr = curses.A_REVERSE if idx == cursor_pos else curses.A_NORMAL
                    key = issue.get('key', 'UNKNOWN')
                    summary = issue.get('fields', {}).get('summary', 'No summary')
                    status = issue.get('fields', {}).get('status', {}).get('name', 'Unknown')
                    status_letter = self.viewer.utils.get_status_letter(status)
                    status_color = self._get_status_color(status_letter)

                    # Draw status with color, then key and summary
                    y_pos = idx - start_idx + 2
                    x_pos = 2
                    try:
                        overlay.addstr(y_pos, x_pos, f"[{status_letter}] ", status_color | attr)
                        x_pos += len(f"[{status_letter}] ")
                        remaining = f"{key}: {summary}"
                        overlay.addstr(y_pos, x_pos, remaining[:menu_width - x_pos - 2], attr)
                    except curses.error:
                        pass

                # Footer
                overlay.addstr(menu_height - 1, 2, "Enter: select  /: filter  q: cancel")
                overlay.refresh()

                ch = overlay.getch()

                if ch == ord('q') or ch == 27:  # q or ESC
                    return None
                elif ch in [curses.KEY_DOWN, ord('j')]:
                    cursor_pos = min(cursor_pos + 1, len(filtered_issues) - 1)
                elif ch in [curses.KEY_UP, ord('k')]:
                    cursor_pos = max(cursor_pos - 1, 0)
                elif ch == ord('/'):  # Start filtering
                    curses.echo()
                    curses.curs_set(1)
                    overlay.addstr(menu_height - 2, 2, "Filter: ")
                    overlay.clrtoeol()
                    overlay.refresh()
                    filter_input = overlay.getstr(menu_height - 2, 10, menu_width - 14).decode('utf-8', errors='ignore')
                    curses.noecho()
                    curses.curs_set(0)
                    search_filter = filter_input.strip()
                    cursor_pos = 0
                elif ch == ord('\n'):  # Enter to select
                    if filtered_issues:
                        return filtered_issues[cursor_pos].get('key')

        except curses.error:
            return None

    def _prompt_for_link_comment(self, stdscr, from_key: str, to_key: str,
                                   link_type: str, direction: str, height: int, width: int) -> Optional[str]:
        """
        Prompt user to add an optional comment to the link using vim.

        Returns:
            - None if user aborted (deleted all content)
            - Empty string if user saved with only comments (no comment, but proceed)
            - Comment text if user added uncommented content
        """
        import tempfile
        import subprocess
        import os

        # Create temp file with link details as template
        template_lines = [
            f"# Creating Issue Link",
            f"#",
            f"# From: {from_key}",
            f"# Type: {link_type} ({direction})",
            f"# To:   {to_key}",
            f"#",
            f"# Enter an optional comment below (lines starting with # will be ignored).",
            f"# To create link WITHOUT a comment: leave only the # lines (this template).",
            f"# To CANCEL link creation: delete all content (including # lines).",
            f"#",
            ""
        ]
        template_content = '\n'.join(template_lines)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            temp_path = f.name
            f.write(template_content)

        # Open vim editor
        curses.def_prog_mode()
        curses.endwin()

        try:
            subprocess.call(['vim', temp_path])
        finally:
            curses.reset_prog_mode()
            stdscr.refresh()

        # Check if file still exists (user might have deleted it)
        if not os.path.exists(temp_path):
            return None

        # Read the file
        try:
            with open(temp_path, 'r') as f:
                content = f.read()

            os.unlink(temp_path)

            # Check if file is completely empty (user deleted everything = abort)
            if not content.strip():
                return None

            # Extract comment (remove lines starting with #)
            lines = content.split('\n')
            comment_lines = [line.rstrip() for line in lines if not line.strip().startswith('#')]
            comment_text = '\n'.join(comment_lines).strip()

            # Return comment text (empty string if only # lines remain = no comment but proceed)
            return comment_text

        except Exception as e:
            # Error reading file - treat as abort
            try:
                os.unlink(temp_path)
            except:
                pass
            return None

    def _prompt_for_current_ticket_selection(self, stdscr, tickets: list, height: int, width: int) -> Optional[str]:
        """Show current tickets for selection. Returns issue key or None."""
        if not tickets:
            return None

        menu_height = min(len(tickets) + 6, height - 4)
        menu_width = min(80, width - 4)
        start_y = (height - menu_height) // 2
        start_x = (width - menu_width) // 2

        cursor_pos = 0
        search_filter = ""

        try:
            overlay = curses.newwin(menu_height, menu_width, start_y, start_x)

            while True:
                # Filter tickets by search
                filtered_tickets = tickets
                if search_filter:
                    search_lower = search_filter.lower()
                    filtered_tickets = [
                        ticket for ticket in tickets
                        if search_lower in ticket.get('key', '').lower() or
                           search_lower in ticket.get('fields', {}).get('summary', '').lower()
                    ]

                cursor_pos = min(cursor_pos, len(filtered_tickets) - 1) if filtered_tickets else 0

                overlay.clear()
                overlay.box()
                overlay.addstr(0, 2, f" Select from Current Tickets ({len(filtered_tickets)}) ", curses.A_BOLD)

                # Show search filter if active
                if search_filter:
                    overlay.addstr(1, 2, f"Filter: {search_filter}", curses.A_DIM)

                # List filtered tickets
                visible_height = menu_height - 4
                start_idx = max(0, cursor_pos - visible_height + 1) if cursor_pos >= visible_height else 0

                for idx in range(start_idx, min(start_idx + visible_height, len(filtered_tickets))):
                    ticket = filtered_tickets[idx]
                    attr = curses.A_REVERSE if idx == cursor_pos else curses.A_NORMAL
                    key = ticket.get('key', 'UNKNOWN')
                    summary = ticket.get('fields', {}).get('summary', 'No summary')
                    status = ticket.get('fields', {}).get('status', {}).get('name', 'Unknown')
                    status_letter = self.viewer.utils.get_status_letter(status)
                    status_color = self._get_status_color(status_letter)

                    # Draw status with color, then key and summary
                    y_pos = idx - start_idx + 2
                    x_pos = 2
                    try:
                        overlay.addstr(y_pos, x_pos, f"[{status_letter}] ", status_color | attr)
                        x_pos += len(f"[{status_letter}] ")
                        remaining = f"{key}: {summary}"
                        overlay.addstr(y_pos, x_pos, remaining[:menu_width - x_pos - 2], attr)
                    except curses.error:
                        pass

                # Footer
                overlay.addstr(menu_height - 1, 2, "Enter: select  /: filter  q: cancel")
                overlay.refresh()

                ch = overlay.getch()

                if ch == ord('q') or ch == 27:  # q or ESC
                    return None
                elif ch in [curses.KEY_DOWN, ord('j')]:
                    cursor_pos = min(cursor_pos + 1, len(filtered_tickets) - 1)
                elif ch in [curses.KEY_UP, ord('k')]:
                    cursor_pos = max(cursor_pos - 1, 0)
                elif ch == ord('/'):  # Start filtering
                    curses.echo()
                    curses.curs_set(1)
                    overlay.addstr(menu_height - 2, 2, "Filter: ")
                    overlay.clrtoeol()
                    overlay.refresh()
                    filter_input = overlay.getstr(menu_height - 2, 10, menu_width - 14).decode('utf-8', errors='ignore')
                    curses.noecho()
                    curses.curs_set(0)
                    search_filter = filter_input.strip()
                    cursor_pos = 0
                elif ch == ord('\n'):  # Enter to select
                    if filtered_tickets:
                        return filtered_tickets[cursor_pos].get('key')

        except curses.error:
            return None

    def _add_issue_link(self, stdscr, ticket_key: str, tickets: list, height: int, width: int):
        """Complete flow to add an issue link."""
        while True:  # Outer loop - allows going back to link type selection
            # Step 1: Select link type
            link_type = self._prompt_for_link_type(stdscr, height, width)
            if not link_type:
                return  # User quit at link type selection

            # Step 2: Select direction
            direction = self._prompt_for_link_direction(stdscr, link_type, height, width)
            if not direction:
                continue  # Go back to link type selection

            # Steps 3-4: Search for target issue (allow retrying on failure)
            target_key = None
            error_message = None
            has_current_tickets = tickets and len(tickets) > 0
            while not target_key:
                # Step 3: Prompt for JQL query or select from current
                jql = self._prompt_for_jql_search(stdscr, height, width, has_current_tickets, error_message)
                if jql is None:
                    # None means ESC - go back to link type
                    break  # Break out of JQL loop to go back to link type
                elif jql == "":
                    # Empty string means cancel completely
                    return
                elif jql == "__SELECT_FROM_CURRENT__":
                    # User wants to select from current tickets
                    target_key = self._prompt_for_current_ticket_selection(stdscr, tickets, height, width)
                    if not target_key:
                        # User canceled from ticket selection
                        error_message = None  # Clear error for retry
                        continue
                else:
                    # Step 4: Execute search and select issue
                    target_key = self._prompt_for_issue_selection(stdscr, jql, height, width)
                    if not target_key:
                        # Search failed or no results - set error and loop to retry
                        error_message = "Search failed or no results. Try another query or press ESC to go back."
                        continue

            # If we broke out of the JQL loop without a target_key, go back to link type
            if not target_key:
                continue

            # Step 5: Prompt for optional comment
            link_type_name = link_type.get('name', 'Unknown')
            direction_label = link_type.get(direction, direction)

            comment_result = self._prompt_for_link_comment(
                stdscr, ticket_key, target_key, link_type_name, direction_label, height, width
            )

            if comment_result is None:
                # User aborted (exited without saving)
                self._show_message(stdscr, "Link creation cancelled", height, width)
                return

            comment_text = comment_result  # May be empty string if no comment

            # Step 6: Create link with optional comment
            try:
                # Show creating message
                menu_height = 5
                menu_width = min(60, width - 4)
                start_y = (height - menu_height) // 2
                start_x = (width - menu_width) // 2

                overlay = curses.newwin(menu_height, menu_width, start_y, start_x)
                overlay.clear()
                overlay.box()
                overlay.addstr(menu_height // 2, 2, "Creating link...")
                overlay.refresh()

                # Determine inward/outward issue based on direction
                if direction == 'inward':
                    inward_key = ticket_key
                    outward_key = target_key
                else:
                    inward_key = target_key
                    outward_key = ticket_key

                payload = {
                    "type": {"name": link_type_name},
                    "inwardIssue": {"key": inward_key},
                    "outwardIssue": {"key": outward_key}
                }

                # Add comment if provided
                if comment_text:
                    # Convert plain text to Atlassian Document Format (ADF)
                    adf_body = self._text_to_adf(comment_text)
                    payload["comment"] = {"body": adf_body}

                response = self.viewer.utils.call_jira_api('/issueLink', method='POST', data=payload)

                if response is not None:
                    self._show_message(stdscr, "✓ Link created", height, width)
                else:
                    self._show_message(stdscr, "✗ Failed to create link", height, width)

                return  # Exit after creating link

            except curses.error:
                pass

    def _prompt_for_remove_link_comment(self, stdscr, from_key: str, to_key: str,
                                          link_type: str, direction: str, height: int, width: int) -> Optional[str]:
        """
        Prompt user to add an optional comment when removing a link using vim.

        Returns:
            - None if user aborted (deleted all content)
            - Empty string if user saved with only comments (no comment, but proceed)
            - Comment text if user added uncommented content
        """
        import tempfile
        import subprocess
        import os

        # Create temp file with link details as template
        template_lines = [
            f"# Removing Issue Link",
            f"#",
            f"# From: {from_key}",
            f"# Type: {link_type} ({direction})",
            f"# To:   {to_key}",
            f"#",
            f"# Enter an optional comment below (lines starting with # will be ignored).",
            f"# Comment will be added to {from_key} before removing the link.",
            f"# To remove link WITHOUT a comment: leave only the # lines (this template).",
            f"# To CANCEL link removal: delete all content (including # lines).",
            f"#",
            ""
        ]
        template_content = '\n'.join(template_lines)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            temp_path = f.name
            f.write(template_content)

        # Open vim editor
        curses.def_prog_mode()
        curses.endwin()

        try:
            subprocess.call(['vim', temp_path])
        finally:
            curses.reset_prog_mode()
            stdscr.refresh()

        # Check if file still exists (user might have deleted it)
        if not os.path.exists(temp_path):
            return None

        # Read the file
        try:
            with open(temp_path, 'r') as f:
                content = f.read()

            os.unlink(temp_path)

            # Check if file is completely empty (user deleted everything = abort)
            if not content.strip():
                return None

            # Extract comment (remove lines starting with #)
            lines = content.split('\n')
            comment_lines = [line.rstrip() for line in lines if not line.strip().startswith('#')]
            comment_text = '\n'.join(comment_lines).strip()

            # Return comment text (empty string if only # lines remain = no comment but proceed)
            return comment_text

        except Exception as e:
            # Error reading file - treat as abort
            try:
                os.unlink(temp_path)
            except:
                pass
            return None

    def _remove_issue_link(self, stdscr, ticket_key: str, issue_links: list, height: int, width: int):
        """Handle removing an issue link."""
        if not issue_links:
            self._show_message(stdscr, "No issue links to remove", height, width)
            return

        # Format links for display
        formatted_links = []
        for link in issue_links:
            link_type = link.get('type', {})
            link_type_name = link_type.get('name', 'Unknown')

            # Determine direction and target
            if 'inwardIssue' in link:
                direction = link_type.get('inward', 'inward')
                target_issue = link['inwardIssue']
            else:
                direction = link_type.get('outward', 'outward')
                target_issue = link['outwardIssue']

            target_key = target_issue.get('key', 'UNKNOWN')
            target_summary = target_issue.get('fields', {}).get('summary', 'No summary')

            formatted_links.append({
                'id': link.get('id'),
                'display': f"{link_type_name} ({direction}): {target_key} - {target_summary}",
                'target_key': target_key,
                'link_type_name': link_type_name,
                'direction': direction
            })

        # Show selection menu
        menu_height = min(len(formatted_links) + 5, height - 4)
        menu_width = min(90, width - 4)
        start_y = (height - menu_height) // 2
        start_x = (width - menu_width) // 2

        cursor_pos = 0

        try:
            overlay = curses.newwin(menu_height, menu_width, start_y, start_x)

            while True:
                overlay.clear()
                overlay.box()
                overlay.addstr(0, 2, " Remove Issue Link ", curses.A_BOLD)

                # List links
                visible_height = menu_height - 4
                start_idx = max(0, cursor_pos - visible_height + 1) if cursor_pos >= visible_height else 0

                for idx in range(start_idx, min(start_idx + visible_height, len(formatted_links))):
                    link_info = formatted_links[idx]
                    attr = curses.A_REVERSE if idx == cursor_pos else curses.A_NORMAL
                    display = f" {idx + 1}. {link_info['display']}"
                    overlay.addstr(idx - start_idx + 2, 2, display[:menu_width - 4], attr)

                overlay.addstr(menu_height - 1, 2, "Enter: select  q: cancel")
                overlay.refresh()

                ch = overlay.getch()

                if ch == ord('q') or ch == 27:  # q or ESC
                    return
                elif ch in [curses.KEY_DOWN, ord('j')]:
                    cursor_pos = min(cursor_pos + 1, len(formatted_links) - 1)
                elif ch in [curses.KEY_UP, ord('k')]:
                    cursor_pos = max(cursor_pos - 1, 0)
                elif ch == ord('\n'):  # Enter to select
                    selected_link = formatted_links[cursor_pos]
                    link_id = selected_link['id']
                    display = selected_link['display']
                    target_key = selected_link['target_key']
                    link_type_name = selected_link['link_type_name']
                    direction = selected_link['direction']

                    # Prompt for optional comment
                    comment_result = self._prompt_for_remove_link_comment(
                        stdscr, ticket_key, target_key, link_type_name, direction, height, width
                    )

                    if comment_result is None:
                        # User aborted (exited without saving)
                        self._show_message(stdscr, "Link removal cancelled", height, width)
                        return

                    comment_text = comment_result  # May be empty string if no comment

                    # Show processing message
                    confirm_height = 5
                    confirm_width = min(80, width - 4)
                    confirm_y = (height - confirm_height) // 2
                    confirm_x = (width - confirm_width) // 2

                    confirm_overlay = curses.newwin(confirm_height, confirm_width, confirm_y, confirm_x)
                    confirm_overlay.clear()
                    confirm_overlay.box()
                    confirm_overlay.addstr(confirm_height // 2, 2, "Removing link...")
                    confirm_overlay.refresh()

                    # Add comment if provided
                    if comment_text:
                        adf_body = self._text_to_adf(comment_text)
                        comment_payload = {
                            "body": adf_body
                        }
                        comment_endpoint = f"/issue/{ticket_key}/comment"
                        self.viewer.utils.call_jira_api(comment_endpoint, method='POST', data=comment_payload)

                    # Delete the link
                    endpoint = f"/issueLink/{link_id}"
                    response = self.viewer.utils.call_jira_api(endpoint, method='DELETE')

                    if response is not None:
                        self._show_message(stdscr, "✓ Link removed", height, width)
                    else:
                        self._show_message(stdscr, "✗ Failed to remove link", height, width)

                    return

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
        """Calculate visible height for ticket list accounting for header, legend, separators, and query."""
        # height - 2 (status bar) - 1 (header) - legend_lines - 1 (sep before query) - query_lines - 1 (sep after query)
        return height - 5 - self.legend_lines - self.query_lines

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
                         search_query: str, current_query: str):
        """Draw the ticket list in the left pane."""
        y_offset = 0

        # Header
        try:
            header = f" Tickets ({len(tickets)})"
            if search_query:
                header += f" [Filter: {search_query}]"
            stdscr.addstr(y_offset, 0, header[:max_width], curses.A_BOLD)
            y_offset += 1
        except curses.error:
            pass

        # Draw legend
        legend_lines = self._draw_legend(stdscr, y_offset, max_width)
        y_offset += legend_lines

        # Draw separator before query
        try:
            stdscr.addstr(y_offset, 0, "=" * max_width)
            y_offset += 1
        except curses.error:
            pass

        # Draw wrapped query
        query_prefix = "Query: "
        query_lines = 0
        try:
            # Wrap the query to fit the width
            query_text = current_query
            lines_to_draw = []

            # First line with prefix
            if len(query_prefix + query_text) <= max_width:
                lines_to_draw.append(query_prefix + query_text)
            else:
                # Need to wrap
                first_line_len = max_width - len(query_prefix)
                lines_to_draw.append(query_prefix + query_text[:first_line_len])
                remaining = query_text[first_line_len:]

                # Subsequent lines (no prefix)
                while remaining:
                    lines_to_draw.append(remaining[:max_width])
                    remaining = remaining[max_width:]

            # Draw all query lines
            for line in lines_to_draw:
                stdscr.addstr(y_offset, 0, line[:max_width])
                y_offset += 1
                query_lines += 1
        except curses.error:
            pass

        # Store query lines for height calculation
        self.query_lines = query_lines

        # Draw separator after query
        try:
            stdscr.addstr(y_offset, 0, "=" * max_width)
            y_offset += 1
        except curses.error:
            pass

        # Draw tickets (start after header + legend + separators + query)
        ticket_start_y = y_offset
        # Calculate how many lines we can draw (inclusive range from ticket_start_y to max_height)
        visible_height = max_height - ticket_start_y + 1
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
            status_color = self._get_status_color(status_letter)

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
                attr = self._get_status_color(status_letter)
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
                        total: int, search_query: str, input_buffer: str = ""):
        """Draw status bar at bottom showing commands and position."""
        # Left side: mode indicator and search status
        if self.backlog_mode:
            status_left = " [BACKLOG: mN↑ MN↓ mm⭡ MM⭣ b=exit]"
        else:
            status_left = f" [NORMAL] {current}/{total}"

        # Add search indicator if active
        if search_query:
            status_left += f" (Search: {search_query})"

        # Add input buffer if user is typing
        if input_buffer:
            status_left += f" [{input_buffer}]"

        # Add loading indicator if still loading
        with self.loading_lock:
            if not self.loading_complete:
                status_left += f" [Loading {self.loading_count}/{self.loading_total}]"

        status_right = " q:quit j/k:move <n>j/k:<n> gg/G:top/bot <n>gg:line r:refresh e:edit t:transition f:flags c:comment w:weight v:browser ?:help "

        # Calculate spacing
        padding = width - len(status_left) - len(status_right)
        status = status_left + " " * max(0, padding) + status_right

        try:
            if self.use_colors:
                # Determine the styled portion (mode indicator)
                if self.backlog_mode:
                    mode_text = " [BACKLOG: mN↑ MN↓ mm⭡ MM⭣ b=exit]"
                    mode_color = curses.color_pair(3) | curses.A_BOLD | curses.A_REVERSE
                else:
                    mode_text = " [NORMAL]"
                    mode_color = curses.color_pair(2) | curses.A_BOLD | curses.A_REVERSE  # Green for normal

                # Draw mode indicator in color
                stdscr.addstr(y, 0, mode_text, mode_color)

                # Draw rest of status bar
                rest_of_status = status[len(mode_text):]
                stdscr.addstr(y, len(mode_text), rest_of_status[:width - len(mode_text) - 1], curses.A_REVERSE)
            else:
                # No colors - simple status bar
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
            "  R          Refresh cache (link types, users)",
            "  e          Edit ticket",
            "  w          Edit story points (weight)",
            "  f          Toggle flags",
            "  F          Toggle full mode (all comments)",
            "  b          Toggle backlog mode (rank ordering)",
            "  v          Open ticket in browser",
            "  y          Copy ticket URL to clipboard (yank)",
            "  t          Transition ticket",
            "  c          Add comment to ticket",
            "  l          Manage issue links",
            "  n          Create new issue",
            "  s          New query (JQL or ticket key)",
            "  S          Edit current query",
            "  /          Search/filter tickets",
            "  ?          Show this help",
            "  q          Quit",
            "",
            "Backlog Mode (press 'b' to toggle):",
            "  mN         Move up N positions (e.g., m3)",
            "  MN         Move down N positions (e.g., M2)",
            "  mm / m0    Move to top",
            "  MM         Move to bottom",
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

    def _copy_url_to_clipboard(self, ticket_key: str) -> tuple:
        """Copy ticket URL to clipboard. Returns (success, error_msg)."""
        # Get Jira URL from environment, fallback to default
        jira_url = os.environ.get('JIRA_URL', 'https://indeed.atlassian.net')
        url = f"{jira_url}/browse/{ticket_key}"

        last_error = None

        try:
            # Try different clipboard tools based on platform
            if sys.platform.startswith('linux'):
                # Try xclip first (X11)
                try:
                    result = subprocess.run(['xclip', '-selection', 'clipboard'],
                                   input=url.encode('utf-8'),
                                   timeout=1,
                                   capture_output=True)
                    if result.returncode == 0:
                        return (True, None)
                    last_error = f"xclip: {result.stderr.decode('utf-8').strip()}"
                except FileNotFoundError:
                    last_error = "xclip not found"
                except subprocess.TimeoutExpired:
                    last_error = "xclip timeout"

                # Try xsel as fallback (X11)
                try:
                    result = subprocess.run(['xsel', '--clipboard', '--input'],
                                   input=url.encode('utf-8'),
                                   timeout=1,
                                   capture_output=True)
                    if result.returncode == 0:
                        return (True, None)
                    last_error = f"xsel: {result.stderr.decode('utf-8').strip()}"
                except FileNotFoundError:
                    last_error = "xsel not found"
                except subprocess.TimeoutExpired:
                    last_error = "xsel timeout"

                # Try wl-copy for Wayland
                try:
                    result = subprocess.run(['wl-copy'],
                                   input=url.encode('utf-8'),
                                   timeout=1,
                                   capture_output=True)
                    if result.returncode == 0:
                        return (True, None)
                    last_error = f"wl-copy: {result.stderr.decode('utf-8').strip()}"
                except FileNotFoundError:
                    last_error = "wl-copy not found"
                except subprocess.TimeoutExpired:
                    last_error = "wl-copy timeout"

            elif sys.platform == 'darwin':
                # macOS pbcopy (should always be available)
                try:
                    result = subprocess.run(['pbcopy'],
                                   input=url.encode('utf-8'),
                                   timeout=1,
                                   capture_output=True)
                    if result.returncode == 0:
                        return (True, None)
                    last_error = f"pbcopy: {result.stderr.decode('utf-8').strip()}"
                except FileNotFoundError:
                    last_error = "pbcopy not found"
                except subprocess.TimeoutExpired:
                    last_error = "pbcopy timeout"

            elif sys.platform == 'win32':
                # Windows clip
                try:
                    result = subprocess.run(['clip'],
                                   input=url.encode('utf-8'),
                                   timeout=1,
                                   capture_output=True,
                                   shell=True)
                    if result.returncode == 0:
                        return (True, None)
                    last_error = f"clip: {result.stderr.decode('utf-8').strip()}"
                except FileNotFoundError:
                    last_error = "clip not found"
                except subprocess.TimeoutExpired:
                    last_error = "clip timeout"

        except Exception as e:
            last_error = str(e)

        return (False, last_error or "No clipboard tool available")
