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


class JiraUtils:
    """Shared utilities for Jira API interactions and display formatting."""

    def __init__(self, script_dir: Path = None):
        self.script_dir = script_dir or Path(__file__).parent
        self.jira_api = self.script_dir / "jira-api"

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

    def call_jira_api(self, endpoint: str) -> Optional[dict]:
        """Call jira-api script and return parsed JSON response."""
        try:
            cmd = [str(self.jira_api), "GET", endpoint]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                print(f"❌ Jira API call failed: {result.stderr}", file=sys.stderr)
                return None

            return json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
            print(f"❌ Error calling Jira API: {e}", file=sys.stderr)
            return None

    def fetch_all_jql_results(self, jql: str, fields: List[str], max_items: int = 1000) -> List[dict]:
        """Fetch all results for a JQL query using proper nextPageToken pagination."""
        jql_encoded = jql.replace(' ', '%20').replace('"', '%22')
        fields_str = ','.join(fields)
        all_issues = []
        next_page_token = None
        max_results = 100

        while True:
            # Build endpoint with nextPageToken if we have one
            if next_page_token:
                endpoint = f"/search/jql?jql={jql_encoded}&fields={fields_str}&maxResults={max_results}&nextPageToken={next_page_token}"
            else:
                endpoint = f"/search/jql?jql={jql_encoded}&fields={fields_str}&maxResults={max_results}"

            response = self.call_jira_api(endpoint)
            if not response:
                return all_issues

            issues = response.get('issues', [])
            next_page_token = response.get('nextPageToken')

            # If no issues returned, we're done
            if not issues:
                break

            all_issues.extend(issues)

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
            'Done': 'C',  # Use C for Closed/Done to free up D
            'Closed': 'C',
            'Deferred': 'D',  # Use D for Deferred (red)
            'Blocked': 'X'
        }
        return status_map.get(status_name, '?')

    def format_status_indicator(self, status_name: str, use_colors: bool) -> str:
        """Format status indicator with appropriate colors."""
        letter = self.get_status_letter(status_name)

        if use_colors:
            if letter == 'C':
                return f'\033[32m[{letter}]\033[0m'  # Green for closed/done
            elif letter in ['P', 'R', 'Q']:
                return f'\033[33m[{letter}]\033[0m'  # Yellow for active/pending
            elif letter in ['D', 'X']:
                return f'\033[31m[{letter}]\033[0m'  # Red for deferred/blocked
            elif letter == 'T':
                return f'\033[33m[{letter}]\033[0m'  # Yellow for triage
            elif letter == 'B':
                return f'\033[32m[{letter}]\033[0m'  # Green for backlog
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
                          show_due_date_prefix: bool = False, show_sprint: bool = True,
                          show_asterisk: bool = False, sprint_start_date: str = None) -> str:
        """Format a complete ticket line with all standard information."""
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

        # Status indicator
        status_indicator = self.format_status_indicator(status_name, use_colors)

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
        if show_asterisk and sprint_start_date:
            asterisk = self.get_sprint_asterisk(issue, sprint_start_date, use_colors)

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

        # Format final line
        return f'{index:2d}. {due_date_prefix}{asterisk}{status_indicator} {key} {days_part} [{story_points}pt{assignee_part}] {sprint_part}{due_part}{priority_part}: {summary}{summary_suffix}'

    def get_sprint_asterisk(self, issue: dict, sprint_start_date: str, use_colors: bool) -> str:
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

            # Find the active sprint start date
            for sprint in sprints:
                if sprint.get('state') == 'active':
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

    def print_status_legend(self, use_colors: bool, context: str = 'full') -> None:
        """Print status legend based on context."""
        if context == 'backlog':
            if use_colors:
                print("\033[32m[B]\033[0m Backlog  \033[33m[T]\033[0m Triage  \033[33m[P]\033[0m In Progress  \033[33m[R]\033[0m In Review  \033[33m[Q]\033[0m Requirements  \033[31m[D]\033[0m Deferred  \033[31m[X]\033[0m Blocked")
            else:
                print("[B] Backlog  [T] Triage  [P] In Progress  [R] In Review  [Q] Requirements  [D] Deferred  [X] Blocked")
        else:  # full context for project-dashboard
            if use_colors:
                print("\033[32m[C]\033[0m Done  \033[33m[P]\033[0m In Progress  \033[33m[R]\033[0m In Review  \033[33m[T]\033[0m Triage  \033[32m[B]\033[0m Backlog  \033[33m[Q]\033[0m Requirements  \033[31m[D]\033[0m Deferred  \033[31m[X]\033[0m Blocked")
            else:
                print("[C] Done  [P] In Progress  [R] In Review  [T] Triage  [B] Backlog  [Q] Requirements  [D] Deferred  [X] Blocked")

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
            if status_category == 'done':
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
                           include_deferred: bool = False, include_done: bool = False):
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