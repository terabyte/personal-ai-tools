#!/usr/bin/env python3

"""
Jira TUI - Terminal User Interface for interactive ticket viewing
Provides a split-pane interface with vim keybindings for browsing tickets
"""

import sys
import subprocess
import webbrowser
from typing import List, Optional
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

        try:
            return curses.wrapper(self._curses_main, query_or_ticket)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"❌ Error in TUI: {e}", file=sys.stderr)
            return 1

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

            issues = self.viewer.utils.fetch_all_jql_results(query_or_ticket, fields, max_items=100)

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

        # Pre-fetch and cache all ticket details for fast switching
        stdscr.addstr(0, 0, "Loading ticket details...")
        stdscr.refresh()
        for idx, ticket in enumerate(tickets):
            ticket_key = ticket.get('key')
            full_ticket = self.viewer.fetch_ticket_details(ticket_key)
            if full_ticket:
                self.ticket_cache[ticket_key] = full_ticket
            # Show progress
            if (idx + 1) % 5 == 0 or idx == len(tickets) - 1:
                stdscr.addstr(0, 0, f"Loading ticket details... {idx + 1}/{len(tickets)}")
                stdscr.refresh()
        stdscr.clear()

        # State management
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
                    visible_height = height - 3
                    if selected_idx >= scroll_offset + visible_height:
                        scroll_offset = selected_idx - visible_height + 1
            elif key == ord('k') or key == curses.KEY_UP:  # Up
                if selected_idx > 0:
                    selected_idx -= 1
                    # Auto-scroll if needed
                    if selected_idx < scroll_offset:
                        scroll_offset = selected_idx
            elif key == ord('g'):  # Go to top
                selected_idx = 0
                scroll_offset = 0
            elif key == ord('G'):  # Go to bottom
                selected_idx = len(tickets) - 1
                visible_height = height - 3
                scroll_offset = max(0, len(tickets) - visible_height)
            elif key == ord('r'):  # Refresh
                tickets, _ = self._fetch_tickets(query_or_ticket)
                selected_idx = min(selected_idx, len(tickets) - 1)
                # Re-cache ticket details
                self.ticket_cache.clear()
                stdscr.addstr(0, 0, "Refreshing...")
                stdscr.refresh()
                for idx, ticket in enumerate(tickets):
                    ticket_key = ticket.get('key')
                    full_ticket = self.viewer.fetch_ticket_details(ticket_key)
                    if full_ticket:
                        self.ticket_cache[ticket_key] = full_ticket
            elif key == ord('f'):  # Toggle full mode
                self.show_full = not self.show_full
            elif key == ord('v'):  # Open in browser
                if tickets:
                    current_key = tickets[selected_idx].get('key')
                    self._open_in_browser(current_key)
            elif key == ord('/'):  # Search
                search_query = self._get_search_input(stdscr, height - 1, width)
                # Filter tickets by search query
                if search_query:
                    tickets = self._filter_tickets(tickets, search_query)
                    selected_idx = 0
                    scroll_offset = 0
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
            # JQL query
            fields = ['key', 'summary', 'status', 'priority', 'assignee', 'updated',
                     'customfield_10061', 'customfield_10021']
            issues = self.viewer.utils.fetch_all_jql_results(query_or_ticket, fields, max_items=100)
            return issues, False

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

        # Draw tickets
        visible_height = max_height - 1
        for i in range(scroll_offset, min(scroll_offset + visible_height, len(tickets))):
            y = i - scroll_offset + 1

            issue = tickets[i]
            fields = issue.get('fields', {})
            key = issue.get('key', 'N/A')
            status = fields.get('status', {}).get('name', 'Unknown')
            status_letter = self.viewer.utils.get_status_letter(status)

            # Calculate available space for summary (key + status + separators = ~18 chars)
            summary_max = max_width - len(key) - 6
            summary = fields.get('summary', 'No summary')[:summary_max]

            # Format line
            line = f"[{status_letter}] {key}: {summary}"

            # Highlight if selected
            attr = curses.A_REVERSE if i == selected_idx else curses.A_NORMAL

            try:
                stdscr.addstr(y, 0, line[:max_width - 1], attr)
            except curses.error:
                pass

    def _draw_ticket_details(self, stdscr, ticket_key: str, x_offset: int,
                            max_height: int, max_width: int):
        """Draw ticket details in the right pane."""
        # Get full ticket details from cache
        ticket = self.ticket_cache.get(ticket_key)
        if not ticket:
            try:
                stdscr.addstr(1, x_offset + 2, "Failed to load ticket details")
            except curses.error:
                pass
            return

        fields = ticket.get('fields', {})

        # Extract key information
        summary = fields.get('summary', 'No summary')
        status = fields.get('status', {}).get('name', 'Unknown')
        assignee = fields.get('assignee')
        assignee_name = self.viewer.utils.get_assignee_name(assignee) if assignee else 'Unassigned'
        priority = fields.get('priority', {}).get('name', 'None')

        # Draw content line by line
        y = 0
        lines = []

        # Header
        lines.append(f" {ticket_key}")
        lines.append(f" {summary[:max_width - 3]}")
        lines.append("")
        lines.append(f" Status: {status}")
        lines.append(f" Assignee: {assignee_name}")
        lines.append(f" Priority: {priority}")
        lines.append("")

        # Description
        description = fields.get('description')
        if description:
            lines.append(" Description:")
            desc_text = self.viewer.format_description(description, self.use_colors, indent="")
            desc_text = self._strip_ansi(desc_text)
            # Wrap description
            for line in desc_text.split('\n'):
                wrapped = self._wrap_text(line, max_width - 4)
                lines.extend([f"  {l}" for l in wrapped])

        lines.append("")

        # Comments
        comments_data = fields.get('comment', {})
        all_comments = comments_data.get('comments', [])

        if all_comments:
            if self.show_full:
                comments_to_show = all_comments
                lines.append(f" ──── All Comments ({len(all_comments)}) ────")
            else:
                comments_to_show = self.viewer.filter_recent_comments(all_comments)
                lines.append(f" ──── Recent Comments ({len(comments_to_show)}/{len(all_comments)}) ────")

            for comment in comments_to_show:
                comment_text = self.viewer.format_comment(comment, self.use_colors)
                comment_text = self._strip_ansi(comment_text)
                for line in comment_text.split('\n'):
                    wrapped = self._wrap_text(line, max_width - 4)
                    lines.extend([f"  {l}" for l in wrapped])
                lines.append("")
        else:
            lines.append(" ──── Comments ────")
            lines.append("  (No comments)")
            lines.append("")

        # History (only if full mode)
        if self.show_full:
            changelog = ticket.get('changelog', {})
            histories = changelog.get('histories', [])

            if histories:
                lines.append(f" ──── Change History ({len(histories)}) ────")
                for history in histories:
                    history_lines = self.viewer.format_history_entry(history, self.use_colors)
                    for line in history_lines:
                        line = self._strip_ansi(line)
                        wrapped = self._wrap_text(line, max_width - 4)
                        lines.extend([f"  {l}" for l in wrapped])
                    lines.append("")

        # Draw all lines (with scrolling support if needed)
        for i, line in enumerate(lines[:max_height - 1]):
            try:
                stdscr.addstr(i, x_offset + 1, line[:max_width - 2])
            except curses.error:
                pass

    def _draw_status_bar(self, stdscr, y: int, width: int, current: int,
                        total: int, search_query: str):
        """Draw status bar at bottom showing commands and position."""
        status_left = f" {current}/{total}"
        status_right = " q:quit j/k:move g/G:top/bot r:refresh f:full v:browser ?:help "

        # Calculate spacing
        padding = width - len(status_left) - len(status_right)
        status = status_left + " " * max(0, padding) + status_right

        try:
            stdscr.addstr(y, 0, status[:width], curses.A_REVERSE)
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
            "",
            "Actions:",
            "  r          Refresh current view",
            "  f          Toggle full mode (all comments)",
            "  v          Open ticket in browser",
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
        """Simple text wrapping."""
        if len(text) <= width:
            return [text]

        lines = []
        while text:
            lines.append(text[:width])
            text = text[width:]
        return lines

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
