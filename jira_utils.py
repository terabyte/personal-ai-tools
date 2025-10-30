#!/usr/bin/env python3

"""
Jira Utilities - Shared functionality for Jira dashboard tools
Common functions for API calls, pagination, formatting, and display
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from jira_cache import JiraCache


class JiraUtils:
    """Shared utilities for Jira API interactions and display formatting."""

    def __init__(self, script_dir: Path = None):
        self.script_dir = script_dir or Path(__file__).parent
        self.jira_api = self.script_dir / "jira-api"

        # Initialize cache with Jira URL from environment
        jira_url = os.environ.get('JIRA_URL', 'https://indeed.atlassian.net')
        self.cache = JiraCache(jira_url)

        # In-memory user cache: accountId -> user dict
        self._user_cache: Dict[str, dict] = {}

        # Current user cache (accountId of the authenticated user)
        self._current_user_id: Optional[str] = None

    def get_terminal_width(self) -> int:
        """Get terminal width, fallback to generous default for modern terminals."""
        try:
            return shutil.get_terminal_size().columns
        except:
            return 120

    def supports_colors(self) -> bool:
        """Check if terminal supports colors - be more permissive for xterm."""
        term = os.environ.get('TERM', '')
        return any(term_type in term for term_type in ['xterm', 'color', 'screen'])

    def calculate_summary_length(self, terminal_width: int, override_length: Optional[int] = None, reserved_space: int = 50) -> int:
        """Calculate optimal summary length based on terminal width."""
        if override_length:
            return override_length

        available_space = terminal_width - reserved_space

        # Use at least 80 chars, at most 250 chars
        if available_space < 80:
            return 80
        elif available_space > 250:
            return 250
        else:
            return available_space

    def call_jira_api(self, endpoint: str, method: str = "GET", data: Optional[dict] = None) -> Optional[dict]:
        """Call jira-api script and return parsed JSON response."""
        try:
            cmd = [str(self.jira_api), method, endpoint]

            # Add JSON data if provided (for POST/PUT requests)
            if data is not None:
                cmd.extend(['-d', json.dumps(data)])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                print(f"❌ Jira API call failed: {result.stderr}", file=sys.stderr)
                return None

            # Some POST requests return empty response (e.g., transitions)
            if not result.stdout.strip():
                return {}

            return json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
            print(f"❌ Error calling Jira API: {e}", file=sys.stderr)
            return None

    def get_jql_count(self, jql: str) -> int:
        """Get the total count of results for a JQL query by fetching only keys.

        This makes a fast query fetching minimal data (just keys) to determine
        the total count before fetching full results.

        Args:
            jql: JQL query string

        Returns:
            Total count of matching issues
        """
        jql_encoded = jql.replace(' ', '%20').replace('"', '%22')
        count = 0
        next_page_token = None
        max_results = 100

        while True:
            # Build endpoint - fetch only 'key' field for speed
            if next_page_token:
                endpoint = f"/search/jql?jql={jql_encoded}&fields=key&maxResults={max_results}&nextPageToken={next_page_token}"
            else:
                endpoint = f"/search/jql?jql={jql_encoded}&fields=key&maxResults={max_results}"

            response = self.call_jira_api(endpoint)
            if not response:
                break

            issues = response.get('issues', [])
            next_page_token = response.get('nextPageToken')

            if not issues:
                break

            count += len(issues)

            # If no nextPageToken, we're on the last page
            if not next_page_token:
                break

        return count

    def fetch_all_jql_results(self, jql: str, fields: List[str], max_items: int = 1000, expand: Optional[str] = None, progress_callback=None, skip_count: bool = False) -> List[dict]:
        """Fetch all results for a JQL query using proper nextPageToken pagination.

        Args:
            jql: JQL query string
            fields: List of fields to fetch
            max_items: Maximum number of items to fetch (default 1000)
            expand: Optional expand parameter
            progress_callback: Optional callback function(fetched_count, total_count) called after each page
            skip_count: If True, skip the initial count query (default False)
        """
        jql_encoded = jql.replace(' ', '%20').replace('"', '%22')
        fields_str = ','.join(fields)
        all_issues = []
        next_page_token = None
        max_results = 100

        # Get total count first with a fast query (unless skipped)
        if not skip_count:
            total_count = self.get_jql_count(jql)
        else:
            total_count = None

        while True:
            # Build endpoint with nextPageToken if we have one
            if next_page_token:
                endpoint = f"/search/jql?jql={jql_encoded}&fields={fields_str}&maxResults={max_results}&nextPageToken={next_page_token}"
            else:
                endpoint = f"/search/jql?jql={jql_encoded}&fields={fields_str}&maxResults={max_results}"

            # Add expand parameter if provided
            if expand:
                endpoint += f"&expand={expand}"

            response = self.call_jira_api(endpoint)
            if not response:
                return all_issues

            issues = response.get('issues', [])
            next_page_token = response.get('nextPageToken')

            # If no issues returned, we're done
            if not issues:
                break

            all_issues.extend(issues)

            # Call progress callback if provided
            if progress_callback and total_count is not None:
                progress_callback(len(all_issues), total_count)

            # If no nextPageToken, we're on the last page
            if not next_page_token:
                break

            # Safety limit to prevent runaway queries
            if len(all_issues) >= max_items:
                break

        return all_issues

    def calculate_days_since_update(self, updated_str: str) -> Tuple[int, str]:
        """Calculate days since last update and return (days, formatted_string)."""
        try:
            # Parse ISO format: 2025-09-26T14:10:21.467-0500
            # Fix timezone format for Python compatibility
            if updated_str.count(':') == 2 and ('+' in updated_str[-5:] or '-' in updated_str[-5:]):
                # Add colon to timezone: -0500 -> -05:00
                updated_str = updated_str[:-2] + ':' + updated_str[-2:]

            updated_dt = datetime.fromisoformat(updated_str)
            now = datetime.now(timezone.utc)
            days_diff = (now - updated_dt.astimezone(timezone.utc)).days

            if days_diff == 0:
                return days_diff, 'today'
            elif days_diff == 1:
                return days_diff, '1d'
            else:
                return days_diff, f'{days_diff}d'
        except Exception:
            return -1, '?d'

    def format_days_with_color(self, days: int, days_text: str, use_colors: bool) -> str:
        """Format days with appropriate color coding."""
        if use_colors:
            if days < 2:
                return f'\033[32m({days_text})\033[0m'  # Green < 2 days
            elif days <= 4:
                return f'\033[33m({days_text})\033[0m'  # Yellow 2-4 days
            else:
                return f'({days_text})'  # Standard (no color) > 4 days
        else:
            return f'({days_text})'

    def get_status_letter(self, status_name: str) -> str:
        """Map status name to single letter indicator."""
        status_map = {
            'Pending Triage': 'T',
            'on Backlog': 'B',
            'To Do': 'T',
            'In Progress': 'P',
            'In Review': 'R',
            'Pending Review': 'R',
            'Pending Requirements': 'Q',
            'Pending Verification': 'V',
            'Pending Closure': 'Z',
            'Pending Deploy': 'Y',  # Changed from W to Y
            'Pending Merge': 'M',
            'Wish List': 'W',  # New W for Wish List
            'Accepted': 'A',
            'Scheduled': 'S',
            'Done': 'C',  # Use C for Closed/Done to free up D
            'Closed': 'C',
            'Deferred': 'D',  # Use D for Deferred (red)
            'Abandoned': '_',  # Use _ for Abandoned (red)
            'Blocked': 'X'
        }
        return status_map.get(status_name, '?')

    def format_status_indicator(self, status_name: str, use_colors: bool) -> str:
        """Format status indicator with appropriate colors."""
        letter = self.get_status_letter(status_name)

        if use_colors:
            if letter in ['C', 'V', 'Z', 'Y', 'M']:
                return f'\033[32m[{letter}]\033[0m'  # Green for closed/done/verification/closure/deploy/merge
            elif letter in ['A', 'B', 'S', 'W']:
                return f'\033[34m[{letter}]\033[0m'  # Blue for accepted/backlog/scheduled/wishlist
            elif letter in ['P', 'R', 'Q']:
                return f'\033[33m[{letter}]\033[0m'  # Yellow for active/pending
            elif letter in ['D', 'X', '_']:
                return f'\033[31m[{letter}]\033[0m'  # Red for deferred/blocked/abandoned
            elif letter == 'T':
                return f'\033[33m[{letter}]\033[0m'  # Yellow for triage
            else:
                return f'[{letter}]'  # Standard for unknown
        else:
            return f'[{letter}]'

    def format_story_points(self, points) -> str:
        """Format story points as integer if whole number, otherwise float."""
        if points is None:
            return "0"
        try:
            if float(points) == int(float(points)):
                return str(int(float(points)))
            else:
                return str(points)
        except (ValueError, TypeError):
            return "0"

    def get_assignee_name(self, assignee_data) -> str:
        """Extract username from assignee data, preferring email username."""
        if not assignee_data:
            return ""

        email = assignee_data.get('emailAddress', '')
        if email and '@' in email:
            return email.split('@')[0]  # LDAP username
        else:
            return assignee_data.get('displayName', '')

    def get_sprint_info(self, fields: dict) -> str:
        """Extract active sprint name from ticket fields."""
        sprints = fields.get('customfield_10021', [])
        if sprints:
            for sprint in sprints:
                if sprint.get('state') == 'active':
                    sprint_name = sprint.get('name', '')
                    if sprint_name:
                        return f'[Sprint={sprint_name}]'
        return ''

    def format_ticket_line(self, issue: dict, index: int, summary_length: int, use_colors: bool,
                          show_due_date_prefix: bool = False, show_sprint: bool = False,
                          show_asterisk: bool = False, show_status: bool = True, sprint_name: str = None) -> str:
        """Format a complete ticket line with all standard information.

        Standard format: [state] KEY-123 (updated) [story pts, assignee if present] [DUE:if present] [P:priority]: Summary
        """
        fields = issue.get('fields', {})
        key = issue.get('key', 'N/A')

        # Get basic fields
        full_summary = fields.get('summary', 'No summary')
        summary = full_summary[:summary_length]
        summary_suffix = '...' if len(full_summary) > summary_length else ''

        priority = fields.get('priority', {}).get('name', 'No Priority')
        status_name = fields.get('status', {}).get('name', 'Unknown')
        status_category = fields.get('status', {}).get('statusCategory', {}).get('key', '')
        assignee = fields.get('assignee')
        assignee_name = self.get_assignee_name(assignee)

        story_points = self.format_story_points(fields.get('customfield_10061'))

        # Calculate days since update
        updated_str = fields.get('updated', '')
        days, days_text = self.calculate_days_since_update(updated_str)
        days_part = self.format_days_with_color(days, days_text, use_colors)

        # Status indicator (always show for consistency)
        status_indicator = self.format_status_indicator(status_name, use_colors) if show_status else ''

        # Due date prefix (for DUE SOON sections)
        due_date_prefix = ""
        if show_due_date_prefix:
            due_date_str = fields.get('duedate', '')
            if due_date_str:
                try:
                    due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
                    formatted_date = due_date.strftime('%Y-%m-%d')
                    due_date_prefix = f'{formatted_date} '
                except:
                    due_date_prefix = '????-??-?? '
            else:
                due_date_prefix = '????-??-?? '

        # Asterisk for tickets added after sprint start
        asterisk = ""
        if show_asterisk:
            asterisk = self.get_sprint_asterisk(issue, sprint_name, use_colors)

        # Build metadata parts
        assignee_part = f', {assignee_name}' if assignee_name else ''

        # Sprint info (omit for done tickets)
        sprint_part = ''
        if show_sprint and status_category != 'done':
            sprint_info = self.get_sprint_info(fields)
            sprint_part = f'{sprint_info} ' if sprint_info else ''

        # Due date inline (if not shown as prefix)
        due_date_field = fields.get('duedate', '')
        due_part = f'[DUE:{due_date_field}] ' if due_date_field and not show_due_date_prefix else ''

        priority_part = f'[P:{priority}]'

        # Impediment flag
        impediment_flag = ''
        flags = fields.get('customfield_10023', [])
        if flags:
            flag_values = []
            for flag in flags:
                if isinstance(flag, dict):
                    flag_values.append(flag.get('value', str(flag)))
                else:
                    flag_values.append(str(flag))
            if flag_values:
                flags_str = ', '.join(flag_values)
                if use_colors:
                    impediment_flag = f' \033[31m[{flags_str}]\033[0m'
                else:
                    impediment_flag = f' [{flags_str}]'

        # Format final line in standard format
        return f'{index:2d}. {due_date_prefix}{asterisk}{status_indicator} {key} {days_part} [{story_points}pt{assignee_part}] {sprint_part}{due_part}{priority_part}:{impediment_flag} {summary}{summary_suffix}'

    def get_sprint_asterisk(self, issue: dict, sprint_name: str, use_colors: bool) -> str:
        """Get asterisk indicator for tickets added after sprint start."""
        try:
            fields = issue.get('fields', {})
            created_str = fields.get('created', '')
            sprints = fields.get('customfield_10021', [])

            if not created_str or not sprints:
                return '  '  # Space for alignment

            # Parse ticket creation date
            if created_str.count(':') == 2 and ('+' in created_str[-5:] or '-' in created_str[-5:]):
                created_str = created_str[:-2] + ':' + created_str[-2:]
            created_dt = datetime.fromisoformat(created_str)

            # Find the specified sprint or active sprint start date
            for sprint in sprints:
                # Match by sprint name if provided, otherwise use active sprint
                sprint_matches = (sprint_name and sprint.get('name') == sprint_name) or \
                                (not sprint_name and sprint.get('state') == 'active')

                if sprint_matches:
                    start_date_str = sprint.get('startDate', '')
                    if start_date_str:
                        # Parse sprint start date
                        if start_date_str.count(':') == 2 and ('+' in start_date_str[-5:] or '-' in start_date_str[-5:]):
                            start_date_str = start_date_str[:-2] + ':' + start_date_str[-2:]
                        elif start_date_str.endswith('Z'):
                            start_date_str = start_date_str[:-1] + '+00:00'

                        start_dt = datetime.fromisoformat(start_date_str)

                        # Check if created at least 1 day after sprint start
                        time_diff = created_dt.astimezone(timezone.utc) - start_dt.astimezone(timezone.utc)
                        if time_diff.days >= 1:
                            return '\033[31m*\033[0m ' if use_colors else '* '

            return '  '  # Space for alignment
        except Exception:
            return '  '

    def sort_by_rank(self, issues: List[dict]) -> List[dict]:
        """Sort issues by Jira rank field (customfield_10022)."""
        def get_rank_sort_key(issue):
            fields = issue.get('fields', {})
            rank = fields.get('customfield_10022', '')

            if rank and '|' in rank and ':' in rank:
                # Extract rank part after pipe: 'hzzwg1:000i'
                rank_part = rank.split('|')[1]
                if ':' in rank_part:
                    prefix, suffix = rank_part.split(':', 1)
                    # For Jira rank ordering, treat as pure string comparison
                    # Jira's rank values are designed to sort lexicographically
                    suffix_clean = suffix.rstrip(':')
                    return (prefix, suffix_clean)

            # Fallback to issue key for consistent ordering
            issue_key = issue.get('key', 'ZZZZ-9999')
            issue_num = issue_key.split('-')[-1] if '-' in issue_key else '9999'
            return ('zzz_fallback', issue_num.zfill(10))

        return sorted(issues, key=get_rank_sort_key)

    def rank_issues(self, issues: List[str], rank_before: str = None, rank_after: str = None,
                    rank_custom_field_id: int = 10022) -> Tuple[bool, str]:
        """
        Rank issues before or after a target issue using Jira Agile API.

        Args:
            issues: List of issue keys to rank (max 50)
            rank_before: Position issues before this issue key
            rank_after: Position issues after this issue key (mutually exclusive with rank_before)
            rank_custom_field_id: Custom field ID for rank (default 10022)

        Returns:
            (success: bool, error_message: str)

        Example:
            # Move PROJ-123 before PROJ-456
            success, error = utils.rank_issues(['PROJ-123'], rank_before='PROJ-456')
        """
        if not issues:
            return (False, "No issues provided")

        if len(issues) > 50:
            return (False, "Can only rank up to 50 issues at once")

        if not rank_before and not rank_after:
            return (False, "Must specify either rank_before or rank_after")

        if rank_before and rank_after:
            return (False, "Cannot specify both rank_before and rank_after")

        # Build request payload
        payload = {
            "issues": issues,
            "rankCustomFieldId": rank_custom_field_id
        }

        if rank_before:
            payload["rankBeforeIssue"] = rank_before
        else:
            payload["rankAfterIssue"] = rank_after

        # Call Jira Agile API
        # Note: Use relative path because jira-api prepends /rest/api/3
        try:
            response = self.call_jira_api('../../agile/1.0/issue/rank', method='PUT', data=payload)

            # Empty response (200) means success
            if response is not None and not response:
                return (True, "")

            # Check for errors
            if response is None:
                return (False, "API call failed")

            if 'errorMessages' in response:
                return (False, "; ".join(response['errorMessages']))

            if 'errors' in response:
                error_list = [f"{k}: {v}" for k, v in response['errors'].items()]
                return (False, "; ".join(error_list))

            # 207 multi-status - check individual issue results
            if isinstance(response, dict) and 'entries' in response:
                failed = []
                for entry in response['entries']:
                    if entry.get('status') != 200:
                        issue_key = entry.get('issueKey', 'unknown')
                        errors = entry.get('errors', {})
                        if errors:
                            error_msg = "; ".join([f"{k}: {v}" for k, v in errors.items()])
                            failed.append(f"{issue_key}: {error_msg}")
                        else:
                            failed.append(f"{issue_key}: status {entry.get('status')}")

                if failed:
                    return (False, "; ".join(failed))

            # Success
            return (True, "")

        except Exception as e:
            return (False, f"Exception: {str(e)}")

    def print_status_legend(self, use_colors: bool, context: str = 'full') -> None:
        """Print status legend based on context."""
        if context == 'backlog':
            if use_colors:
                print("\033[34m[B]\033[0m Backlog  \033[34m[A]\033[0m Accepted  \033[34m[S]\033[0m Scheduled  \033[34m[W]\033[0m Wish List  \033[33m[T]\033[0m Triage  \033[33m[P]\033[0m In Progress  \033[33m[R]\033[0m In Review  \033[33m[Q]\033[0m Requirements")
                print("\033[32m[V]\033[0m Verification  \033[32m[Y]\033[0m Deploy  \033[32m[M]\033[0m Merge  \033[32m[Z]\033[0m Closure  \033[31m[D]\033[0m Deferred  \033[31m[_]\033[0m Abandoned  \033[31m[X]\033[0m Blocked")
            else:
                print("[B] Backlog  [A] Accepted  [S] Scheduled  [W] Wish List  [T] Triage  [P] In Progress  [R] In Review  [Q] Requirements")
                print("[V] Verification  [Y] Deploy  [M] Merge  [Z] Closure  [D] Deferred  [_] Abandoned  [X] Blocked")
        else:  # full context for epic-dashboard
            if use_colors:
                print("\033[34m[B]\033[0m Backlog  \033[34m[A]\033[0m Accepted  \033[34m[S]\033[0m Scheduled  \033[34m[W]\033[0m Wish List  \033[33m[T]\033[0m Triage  \033[33m[P]\033[0m In Progress  \033[33m[R]\033[0m In Review  \033[33m[Q]\033[0m Requirements")
                print("\033[32m[C]\033[0m Done  \033[32m[V]\033[0m Verification  \033[32m[Y]\033[0m Deploy  \033[32m[M]\033[0m Merge  \033[32m[Z]\033[0m Closure  \033[31m[D]\033[0m Deferred  \033[31m[_]\033[0m Abandoned  \033[31m[X]\033[0m Blocked")
            else:
                print("[B] Backlog  [A] Accepted  [S] Scheduled  [W] Wish List  [T] Triage  [P] In Progress  [R] In Review  [Q] Requirements")
                print("[C] Done  [V] Verification  [Y] Deploy  [M] Merge  [Z] Closure  [D] Deferred  [_] Abandoned  [X] Blocked")

    def categorize_tickets_by_status(self, issues: List[dict]) -> Tuple[Dict[str, List[dict]], Dict[str, int]]:
        """Categorize tickets by status and return both tickets and counts."""
        from collections import defaultdict

        categories = defaultdict(list)
        status_counts = defaultdict(int)

        for issue in issues:
            fields = issue.get('fields', {})
            status_info = fields.get('status', {})
            status_name = status_info.get('name', 'Unknown')
            status_category = status_info.get('statusCategory', {}).get('key', '')

            # Categorize by status
            if status_category == 'done' or status_name in ['Pending Verification', 'Abandoned']:
                categories['done'].append(issue)
                status_counts['done'] += 1
            elif 'blocked' in status_name.lower():
                categories['blocked'].append(issue)
                status_counts['blocked'] += 1
            elif status_category == 'new' or 'triage' in status_name.lower() or 'backlog' in status_name.lower():
                categories['todo'].append(issue)
                status_counts['todo'] += 1
            elif status_category == 'indeterminate' or 'progress' in status_name.lower():
                categories['in_progress'].append(issue)
                status_counts['in_progress'] += 1
            elif 'review' in status_name.lower():
                categories['in_review'].append(issue)
                status_counts['in_review'] += 1
            else:
                categories['other'].append(issue)
                status_counts['other'] += 1

        return dict(categories), dict(status_counts)

    def separate_by_triage_and_due_dates(self, issues: List[dict], days_threshold: int = 30) -> Tuple[List[dict], List[dict], List[dict]]:
        """Separate issues into triage, due soon, and other categories."""
        triage_items = []
        due_soon_items = []
        other_items = []

        # Calculate threshold date
        threshold_date = datetime.now() + timedelta(days=days_threshold)

        for issue in issues:
            fields = issue.get('fields', {})
            status_name = fields.get('status', {}).get('name', 'Unknown')
            due_date_str = fields.get('duedate', '')

            # Check if item has due date within threshold
            has_upcoming_due_date = False
            if due_date_str:
                try:
                    due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
                    if due_date <= threshold_date:
                        has_upcoming_due_date = True
                except:
                    pass

            # Prioritize due dates over triage status
            if has_upcoming_due_date:
                due_soon_items.append(issue)
            elif status_name == 'Pending Triage':
                triage_items.append(issue)
            else:
                other_items.append(issue)

        # Sort due soon items by due date
        def get_due_date_sort_key(issue):
            due_date_str = issue.get('fields', {}).get('duedate', '')
            if due_date_str:
                try:
                    return datetime.strptime(due_date_str, '%Y-%m-%d')
                except:
                    return datetime.max
            return datetime.max

        due_soon_items.sort(key=get_due_date_sort_key)

        return triage_items, due_soon_items, other_items

    def add_common_arguments(self, parser, include_team: bool = True, include_count: bool = True,
                           default_count: int = 10, include_show_all: bool = False,
                           include_deferred: bool = False, include_done: bool = False,
                           include_backlog_jql: bool = False):
        """Add common command line arguments to argument parser."""
        if include_team:
            parser.add_argument('team', nargs='?', default='ciplat', help='Team name from config file (default: ciplat)')
            parser.add_argument('-t', '--team-name', dest='team', help='Team name (same as positional argument)')

        if include_count:
            parser.add_argument('-n', '--count', type=int, default=default_count, help='Number of items to show (default: %(default)s)')

        parser.add_argument('-l', '--length', type=int, help='Summary length in characters (default: auto-detect)')
        parser.add_argument('-c', '--color', action='store_true', help='Force enable colors (auto-detects by default)')
        parser.add_argument('--no-color', action='store_true', help='Force disable colors')

        if include_show_all:
            parser.add_argument('--hide-trivial-contributors', dest='show_all', action='store_false', help='Hide contributors with <10%% in both created and resolved (default: show all)')

        if include_deferred:
            parser.add_argument('--include-deferred', action='store_true', help='Include deferred tickets (default: exclude)')

        if include_done:
            parser.add_argument('--hide-done', dest='show_done', action='store_false', help='Hide done tickets, show count only (default: show done tickets)')

        if include_backlog_jql:
            parser.add_argument('--backlog-jql', help='Custom JQL query for backlog items (overrides default project-based query)')

        if include_team:
            parser.add_argument('--list-teams', action='store_true', help='Show available teams from config file')

    def determine_colors(self, args) -> bool:
        """Determine if colors should be used based on arguments and environment."""
        if hasattr(args, 'color') and args.color:
            return True
        elif hasattr(args, 'no_color') and args.no_color:
            return False
        else:
            return self.supports_colors()

    def load_team_config(self, team: str, config_file: Path) -> Dict[str, str]:
        """Load team configuration from config file."""
        import configparser

        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_file}")

        config = configparser.ConfigParser()
        config.read(config_file)

        if team not in config:
            available_teams = list(config.sections())
            raise ValueError(f"Team '{team}' not found. Available teams: {available_teams}")

        return dict(config[team])

    def list_teams(self, config_file: Path) -> None:
        """List all available teams from config file."""
        import configparser

        if not config_file.exists():
            print(f"❌ Config file not found: {config_file}")
            return

        config = configparser.ConfigParser()
        config.read(config_file)

        print("Available teams:")
        for team in config.sections():
            display_name = config.get(team, 'display_name', fallback=team)
            print(f"  {team} - {display_name}")

    def get_link_types(self, force_refresh: bool = False) -> List[dict]:
        """
        Get issue link types from cache or API.

        Args:
            force_refresh: If True, bypass cache and fetch from API

        Returns:
            List of link type dicts with id, name, inward, outward
        """
        # Check if cache bypass is globally enabled
        no_cache = os.environ.get('JIRA_NO_CACHE', 'false').lower() == 'true'
        force_refresh = force_refresh or no_cache

        cached = self.cache.get('link_types', force_refresh=force_refresh)
        if cached:
            return cached

        # Fetch from API
        response = self.call_jira_api('/issueLinkType')
        if response and 'issueLinkTypes' in response:
            link_types = response['issueLinkTypes']
            if not no_cache:  # Only cache if not in no-cache mode
                self.cache.set('link_types', link_types, ttl=86400)  # 24 hours
            return link_types
        return []

    def fetch_all_users(self, max_total: int = 10000) -> List[dict]:
        """
        Fetch all users with pagination, up to max_total.

        Args:
            max_total: Maximum number of users to fetch (default 10000)

        Returns:
            List of user dicts with accountId, displayName, emailAddress
        """
        all_users = []
        start_at = 0
        page_size = 1000  # Jira API max per request

        while len(all_users) < max_total:
            endpoint = f'/user/search?query=&maxResults={page_size}&startAt={start_at}'
            response = self.call_jira_api(endpoint)

            if not response or not isinstance(response, list):
                break

            if len(response) == 0:  # No more users
                break

            all_users.extend(response)
            start_at += len(response)

            if len(response) < page_size:  # Last page (incomplete page)
                break

        return all_users[:max_total]

    def get_users(self, query: str = None, force_refresh: bool = False) -> List[dict]:
        """
        Get users via Jira API search.

        Args:
            query: Search query for users (required for meaningful results)
            force_refresh: Ignored (kept for API compatibility)

        Returns:
            List of user dicts with accountId, displayName, emailAddress

        Note: Empty queries return external users without emails. Always provide
        a search query (e.g., first few letters of name) for best results.
        """
        if not query or len(query.strip()) < 2:
            # Don't search with empty/short queries - returns external users
            return []

        from urllib.parse import quote
        query_param = query.strip()
        query_encoded = quote(query_param)

        # Search with actual query (returns real users with email addresses)
        endpoint = f'/user/search?query={query_encoded}&maxResults=1000'
        response = self.call_jira_api(endpoint)

        if not response or not isinstance(response, list):
            return []

        # Cache the search results
        for user in response:
            self.cache_user(user)

        return response

    def cache_user(self, user: dict) -> None:
        """Add a user to the in-memory cache.

        Args:
            user: User dict with accountId, displayName, emailAddress
        """
        account_id = user.get('accountId')
        if account_id:
            self._user_cache[account_id] = user

    def get_cached_user(self, account_id: str) -> Optional[dict]:
        """Get user from cache by accountId.

        Args:
            account_id: Jira accountId

        Returns:
            User dict or None if not cached
        """
        return self._user_cache.get(account_id)

    def get_current_user_id(self) -> Optional[str]:
        """Get the accountId of the currently authenticated user.

        Returns:
            accountId of current user, or None if unable to fetch
        """
        # Return cached value if available
        if self._current_user_id:
            return self._current_user_id

        # Fetch from API
        response = self.call_jira_api('/myself')
        if not response or 'accountId' not in response:
            return None

        # Cache for future use
        self._current_user_id = response['accountId']

        # Also cache the user object
        self.cache_user(response)

        return self._current_user_id

    def format_user(self, user: Optional[dict]) -> str:
        """Format user for display as 'Real Name (username)'.

        Args:
            user: User dict with displayName, emailAddress, or None

        Returns:
            Formatted string like 'Carl Myers (cmyers)' or 'None' if user is None
        """
        if not user:
            return 'None'

        display_name = user.get('displayName', 'Unknown')
        email = user.get('emailAddress', '')

        # Extract username from email (prefix before @)
        if email and '@' in email:
            username = email.split('@')[0]
            return f"{display_name} ({username})"

        # If no email, check if displayName looks like an email
        if '@' in display_name:
            username = display_name.split('@')[0].strip()
            return f"{display_name} ({username})"

        # No email info available
        return display_name

    def format_user_by_id(self, account_id: Optional[str]) -> str:
        """Format user by accountId, looking up from cache.

        Args:
            account_id: Jira accountId or None

        Returns:
            Formatted user string or 'None' if not found
        """
        if not account_id:
            return 'None'

        user = self.get_cached_user(account_id)
        if user:
            return self.format_user(user)

        # Not in cache - return accountId as fallback
        return f"[{account_id[:8]}...]"

    def get_issue_types(self, project_key: str, force_refresh: bool = False) -> List[dict]:
        """
        Get issue types for a project from cache or API.

        Args:
            project_key: Jira project key (e.g., 'CIPLAT')
            force_refresh: If True, bypass cache and fetch from API

        Returns:
            List of issue type dicts with id, name, subtask
        """
        # Check if cache bypass is globally enabled
        no_cache = os.environ.get('JIRA_NO_CACHE', 'false').lower() == 'true'
        force_refresh = force_refresh or no_cache

        cached = self.cache.get('issue_types', key=project_key, force_refresh=force_refresh)
        if cached:
            return cached

        # Fetch from API
        endpoint = f'/issue/createmeta?projectKeys={project_key}'
        response = self.call_jira_api(endpoint)
        if response and 'projects' in response:
            # Extract issue types
            projects = response['projects']
            if projects and 'issuetypes' in projects[0]:
                issue_types = projects[0]['issuetypes']
                if not no_cache:  # Only cache if not in no-cache mode
                    self.cache.set('issue_types', issue_types, ttl=86400, key=project_key)  # 24 hours
                return issue_types
        return []