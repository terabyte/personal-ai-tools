#!/usr/bin/env python3

"""
Jira TUI - Terminal User Interface for interactive ticket viewing
Provides a split-pane interface with vim keybindings for browsing tickets
"""

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
                      'customfield_10061', 'customfield_10021']

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

        # Fetch tickets
        tickets, single_ticket_mode = self._fetch_tickets(query_or_ticket)

        if not tickets:
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
            if tickets:
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
            elif key == 10:  # Ctrl+J - Scroll detail pane down
                detail_height = height - 2
                if self.detail_scroll_offset + detail_height < self.detail_total_lines:
                    self.detail_scroll_offset += 1
            elif key == 11:  # Ctrl+K - Scroll detail pane up
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
                all_tickets, _ = self._fetch_tickets(query_or_ticket)
                tickets = all_tickets
                selected_idx = min(selected_idx, len(tickets) - 1)

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

                # Reload transitions in background
                if all_tickets:
                    thread = threading.Thread(target=self._load_transitions_background, args=(all_tickets,), daemon=True)
                    thread.start()

                # Re-apply search filter if active
                if search_query:
                    tickets = self._filter_tickets(all_tickets, search_query)
                    selected_idx = min(selected_idx, len(tickets) - 1)
            elif key == ord('f'):  # Toggle full mode
                self.show_full = not self.show_full
            elif key == ord('v'):  # Open in browser
                if tickets:
                    current_key = tickets[selected_idx].get('key')
                    self._open_in_browser(current_key)
            elif key == ord('/'):  # Search
                search_query = self._get_search_input(stdscr, height - 1, width)
                # Filter tickets by search query or restore full list if empty
                if search_query:
                    tickets = self._filter_tickets(all_tickets, search_query)
                else:
                    tickets = all_tickets
                selected_idx = 0
                scroll_offset = 0
            elif key == ord('t') or key == ord('T'):  # Transition
                if tickets:
                    current_key = tickets[selected_idx].get('key')
                    self._handle_transition(stdscr, current_key, height, width)
                    # Refresh current ticket after transition
                    full_ticket = self.viewer.fetch_ticket_details(current_key)
                    if full_ticket:
                        with self.loading_lock:
                            self.ticket_cache[current_key] = full_ticket
            elif key == ord('c') or key == ord('C'):  # Comment
                if tickets:
                    current_key = tickets[selected_idx].get('key')
                    self._handle_comment(stdscr, current_key, height, width)
                    # Refresh current ticket after comment
                    full_ticket = self.viewer.fetch_ticket_details(current_key)
                    if full_ticket:
                        with self.loading_lock:
                            self.ticket_cache[current_key] = full_ticket
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

                            # Clear caches
                            self.ticket_cache.clear()
                            self.transitions_cache.clear()

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

                            # Clear caches
                            self.ticket_cache.clear()
                            self.transitions_cache.clear()

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

    def _fetch_tickets(self, query_or_ticket: str) -> tuple:
        """
        Fetch tickets from Jira.

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
                'description', 'reporter', 'created', 'issuetype', 'labels',
                'parent', 'issuelinks', 'comment'
            ]
            issues = self.viewer.utils.fetch_all_jql_results(query_or_ticket, fields, expand='changelog')
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
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all fetch tasks
            future_to_key = {
                executor.submit(self._fetch_single_ticket, ticket.get('key')): ticket.get('key')
                for ticket in tickets if ticket.get('key')
            }

            # Process results as they complete
            for future in as_completed(future_to_key):
                ticket_key = future_to_key[future]
                try:
                    full_ticket = future.result()
                    if full_ticket:
                        with self.loading_lock:
                            self.ticket_cache[ticket_key] = full_ticket
                            self.loading_count += 1

                        # Fetch transitions for this ticket (don't wait for it)
                        executor.submit(self._cache_transitions, ticket_key)
                except Exception:
                    # Skip failed tickets
                    pass

        # Mark loading as complete
        with self.loading_lock:
            self.loading_complete = True

    def _cache_transitions(self, ticket_key: str) -> None:
        """Cache transitions for a ticket."""
        transitions = self._fetch_transitions(ticket_key)
        with self.loading_lock:
            self.transitions_cache[ticket_key] = transitions

    def _load_transitions_background(self, tickets: List[dict]) -> None:
        """Background thread to load transitions for all tickets."""
        max_workers = 5  # Fetch up to 5 transitions concurrently

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all transition fetch tasks
            for ticket in tickets:
                ticket_key = ticket.get('key')
                if ticket_key:
                    executor.submit(self._cache_transitions, ticket_key)

    def _handle_transition(self, stdscr, ticket_key: str, height: int, width: int):
        """Handle ticket transition (T key)."""
        # Get transitions from cache or fetch
        with self.loading_lock:
            transitions = self.transitions_cache.get(ticket_key)

        if transitions is None:
            transitions = self._fetch_transitions(ticket_key)

        if not transitions:
            self._show_message(stdscr, "No transitions available", height, width)
            return

        # Draw transition selection overlay
        overlay_height = min(len(transitions) + 4, height - 4)
        overlay_width = min(60, width - 4)
        start_y = (height - overlay_height) // 2
        start_x = (width - overlay_width) // 2

        # Create window for overlay
        try:
            overlay = curses.newwin(overlay_height, overlay_width, start_y, start_x)
            overlay.box()
            overlay.addstr(0, 2, " Select Transition ", curses.A_BOLD)

            # List transitions
            for idx, transition in enumerate(transitions[:overlay_height - 4], 1):
                name = transition.get('name', 'Unknown')
                overlay.addstr(idx + 1, 2, f"{idx}. {name[:overlay_width - 6]}")

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

                    # Call Jira API to perform transition
                    endpoint = f"/issue/{ticket_key}/transitions"
                    payload = {"transition": {"id": transition_id}}
                    response = self.viewer.utils.call_jira_api(endpoint, method='POST', data=payload)

                    if response is not None:
                        self._show_message(stdscr, f"✓ Transitioned to {transition.get('name')}", height, width)
                    else:
                        self._show_message(stdscr, "✗ Transition failed", height, width)
                else:
                    self._show_message(stdscr, "Invalid choice", height, width)
            except (ValueError, KeyError):
                self._show_message(stdscr, "Invalid input", height, width)

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

    def _text_to_adf(self, text: str) -> dict:
        """Convert plain text to Atlassian Document Format (ADF)."""
        # Split text into lines and create paragraph for each non-empty line
        lines = text.split('\n')
        content = []

        for line in lines:
            if line.strip():  # Non-empty line
                content.append({
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": line
                        }
                    ]
                })
            else:  # Empty line - add empty paragraph for spacing
                content.append({
                    "type": "paragraph",
                    "content": []
                })

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

    def _show_message(self, stdscr, message: str, height: int, width: int):
        """Show a temporary message overlay."""
        msg_width = min(len(message) + 4, width - 4)
        msg_height = 3
        start_y = (height - msg_height) // 2
        start_x = (width - msg_width) // 2

        try:
            overlay = curses.newwin(msg_height, msg_width, start_y, start_x)
            overlay.box()
            overlay.addstr(1, 2, message[:msg_width - 4])
            overlay.refresh()
            curses.napms(1500)  # Show for 1.5 seconds
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

            # Calculate available space for summary (key + status + separators = ~18 chars)
            summary_max = max_width - len(key) - 6
            summary = fields.get('summary', 'No summary')[:summary_max]

            # Highlight selection
            is_selected = i == selected_idx
            base_attr = curses.A_REVERSE if is_selected else curses.A_NORMAL

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

                # Draw key in green
                stdscr.addstr(y, x_pos, key, curses.color_pair(1) | base_attr)
                x_pos += len(key)

                # Draw colon and summary
                stdscr.addstr(y, x_pos, f": {summary}", base_attr)
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

        fields = ticket.get('fields', {})

        # Extract key information
        summary = fields.get('summary', 'No summary')
        status = fields.get('status', {}).get('name', 'Unknown')
        status_letter = self.viewer.utils.get_status_letter(status)
        issue_type = fields.get('issuetype', {}).get('name', 'Unknown')
        assignee = fields.get('assignee')
        assignee_name = self.viewer.utils.get_assignee_name(assignee) if assignee else 'Unassigned'
        priority = fields.get('priority', {}).get('name', 'None')
        reporter = fields.get('reporter')
        reporter_name = reporter.get('displayName', 'Unknown') if reporter else 'Unknown'
        created_str = fields.get('created', '')
        updated_str = fields.get('updated', '')
        labels = fields.get('labels', [])
        parent = fields.get('parent')
        issuelinks = fields.get('issuelinks', [])

        # Draw content line by line
        y = 0
        lines = []

        # Header (wrap all lines to ensure they fit)
        lines.append(("KEY", f" {ticket_key}"[:max_width - 2]))  # Tagged for coloring
        # Wrap summary if too long
        summary_wrapped = self._wrap_text(summary, max_width - 3)
        for s_line in summary_wrapped:
            lines.append(("SUMMARY", f" {s_line}"))

        lines.append(("", ""))
        lines.append((f"STATUS_{status_letter}", f" Status: {status}"[:max_width - 2]))
        lines.append(("", f" Type: {issue_type}"[:max_width - 2]))
        lines.append(("", f" Assignee: {assignee_name}"[:max_width - 2]))
        lines.append(("", f" Reporter: {reporter_name}"[:max_width - 2]))
        lines.append((f"PRIORITY_{priority}", f" Priority: {priority}"[:max_width - 2]))

        # Created date with relative time
        created_date, created_rel, created_color = self._format_date_with_relative(created_str)
        created_line = f" Created: {created_date}"
        if created_rel:
            created_line += f" ({created_rel})"
        lines.append((f"DATE_{created_color}", created_line[:max_width - 2]))

        # Updated date with relative time
        updated_date, updated_rel, updated_color = self._format_date_with_relative(updated_str)
        updated_line = f" Updated: {updated_date}"
        if updated_rel:
            updated_line += f" ({updated_rel})"
        lines.append((f"DATE_{updated_color}", updated_line[:max_width - 2]))

        # Labels
        if labels:
            labels_str = ', '.join(labels)
            lines.append(("", f" Labels: {labels_str}"[:max_width - 2]))

        # Parent
        if parent:
            parent_key = parent.get('key', 'Unknown')
            parent_summary = parent.get('fields', {}).get('summary', '')
            parent_text = f"{parent_key}: {parent_summary}" if parent_summary else parent_key
            lines.append(("", f" Parent: {parent_text}"[:max_width - 2]))

        # Linked issues
        if issuelinks:
            lines.append(("", f" Linked: {len(issuelinks)} issue(s)"[:max_width - 2]))
            for link in issuelinks[:5]:  # Show first 5 linked issues
                link_type = link.get('type', {}).get('name', 'Unknown')
                # Check if it's an inward or outward link
                if 'inwardIssue' in link:
                    linked_issue = link['inwardIssue']
                    direction = link.get('type', {}).get('inward', '')
                elif 'outwardIssue' in link:
                    linked_issue = link['outwardIssue']
                    direction = link.get('type', {}).get('outward', '')
                else:
                    continue

                linked_key = linked_issue.get('key', 'Unknown')
                linked_summary = linked_issue.get('fields', {}).get('summary', '')
                link_text = f"  {direction}: {linked_key}"
                if linked_summary:
                    link_text += f" - {linked_summary}"
                lines.append(("", link_text[:max_width - 2]))

        lines.append(("", ""))

        # Description
        description = fields.get('description')
        if description:
            lines.append(("HEADER", (" Description:")[:max_width - 2]))
            desc_text = self.viewer.format_description(description, False, indent="")
            # Wrap description
            for line in desc_text.split('\n'):
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

        status_right = " q:quit j/k:move g/G:top/bot r:refresh t:transition c:comment v:browser ?:help "

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
            "  Ctrl+J     Scroll detail pane down (1 line)",
            "  Ctrl+K     Scroll detail pane up (1 line)",
            "  Ctrl+B     Scroll detail pane down (half-page)",
            "  Ctrl+U     Scroll detail pane up (half-page)",
            "",
            "Actions:",
            "  r          Refresh current view",
            "  f          Toggle full mode (all comments)",
            "  v          Open ticket in browser",
            "  t          Transition ticket",
            "  c          Add comment to ticket",
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
