# Personal AI Tools

This repo contains tools I have built to enable me to use AI more easily in my daily workflows. They are mostly commands and scripts for having Claude interact with Jira, Confluence, Gitlab, and other things so I don't have to.

## Testing

This project now has comprehensive test coverage with a focus on thread safety and concurrency correctness.

**⚠️ IMPORTANT: Run tests before committing any code changes!**

**Run tests:**
```bash
./test.sh quick     # REQUIRED before commits - Fast tests (~5s)
./test.sh           # Deep check with coverage (≥65%)
./test.sh long      # Verify no deadlocks (run 10x, ~50s)
```

**Pre-Commit Checklist:**
1. ✅ Run `./test.sh quick` - All tests must pass
2. ✅ Verify no new threading issues introduced
3. ✅ Check that startup remains <1 second
4. ✅ Commit with descriptive message

**Current Status:** 22 tests, 100% pass rate, 65% coverage, <6s execution time

**Why tests matter:** Our tests have already caught concurrency bugs during development that would have caused production hangs. The test suite includes:
- 7 critical tests (startup performance, threading, correctness)
- 11 threading tests (race conditions, deadlocks, stress tests)
- Thread-safety verification for all concurrent operations

See [TESTING_SUMMARY.md](TESTING_SUMMARY.md) for comprehensive details.

## License

This software is released into the public domain under the UNLICENSE. See the UNLICENSE file for details.

## Configuration for Your Organization

These tools are designed to work with any Jira/Confluence/GitLab instance. The default configuration uses Indeed's URLs and domain, but you can customize these via environment variables:

**Required customization:**
- `JIRA_URL` - Your Jira instance URL (default: https://indeed.atlassian.net)
- `CONFLUENCE_URL` - Your Confluence instance URL (default: https://indeed.atlassian.net)
- `GITLAB_URL` - Your GitLab instance URL (default: https://code.corp.indeed.com)
- `PAGERDUTY_URL` - Your PagerDuty instance URL (default: https://api.pagerduty.com)
- `EMAIL_DOMAIN` - Your organization's email domain (default: indeed.com)

**Team configuration:**
1. Copy `teams.conf.example` to `teams.conf`
2. Edit `teams.conf` with your project keys and team names
3. Use `jira-discover-fields` to find your instance-specific custom field IDs

**Instance-specific field IDs:**
Custom field IDs vary between Jira instances. Use the `jira-discover-fields` tool to discover your field IDs for:
- Story Points
- Sprint
- Rank
- Epic Link
- Other custom fields

## Tools

- **jira-export** - Exports Jira tickets with full history and custom fields to JSON for AI analysis (run with `--help-ai` for detailed usage)
- **jira-api** - Bash wrapper for Jira REST API with cross-platform authentication (includes comprehensive usage docs and field IDs in script)
- **jira-discover-fields** - Discover custom field IDs in your Jira instance (essential for configuration)
- **jira-view** - Interactive ticket viewer with color-coded formatting - view individual tickets or query and select from results
- **confluence-api** - Bash wrapper for Confluence REST API with cross-platform authentication (includes comprehensive usage docs in script)
- **calendar-link** - Generates Google Calendar event URLs from command line parameters with optional browser integration
- **gitlab-api** - Bash wrapper for GitLab REST API with keychain authentication
- **pagerduty-api** - Bash wrapper for PagerDuty REST API with cross-platform authentication
- **sprint-dashboard** - Configurable team dashboard showing current sprint status and backlog items (supports multiple teams via config file)
- **backlog-dashboard** - Backlog triage tool showing prioritized backlog items with status indicators and due date tracking
- **epic-dashboard** - Epic-based project status report showing ticket breakdown and contributor progress
- **find-current-sprint** - Helper script to find the active sprint name for a given Jira project
- **find-active-epics** - Helper script to find epics with recent activity for a given Jira project

## API Usage Notes

### Authentication

All Atlassian tools (`jira-api`, `jira-export`, `confluence-api`) support:
1. **Token file**: `~/.atlassian-mcp-token` (preferred for Linux/cross-platform)
2. **Environment variables**: `JIRA_TOKEN`, `CONFLUENCE_TOKEN`
3. **macOS Keychain**: Automatic fallback on macOS systems

### PagerDuty API (`pagerduty-api`)
- **Token-based auth**: Uses PagerDuty API tokens with `Authorization: Token token=...` header
- **API v2**: Uses PagerDuty REST API v2 with proper Accept headers
- **Cross-platform**: Token file, environment variable, or keychain support

**Token Setup:**
1. Visit your PagerDuty instance: `https://YOUR-DOMAIN.pagerduty.com/api_keys`
2. Create new API key with description "Personal CLI Tools"
3. Store in `~/.pagerduty-token` file or `PAGERDUTY_TOKEN` environment variable

**Common Examples:**
```bash
# List incidents
pagerduty-api GET /incidents

# Get triggered incidents only
pagerduty-api GET "/incidents?statuses%5B%5D=triggered"

# List services
pagerduty-api GET /services

# Current on-call for schedule
pagerduty-api GET "/oncalls?schedule_ids%5B%5D=SCHEDULE_ID"

# List users
pagerduty-api GET /users
```

### Jira API (`jira-api`)
- **Modern API**: Uses `/rest/api/3` (v2 is deprecated)
- **Search syntax**: Use `/search/jql?jql=QUERY` for ticket searches
- **User references**: Use `currentUser()` instead of usernames in JQL
- **Field IDs**: Bug tickets require environment field (`customfield_11674`)
- **Issue types**: Task=10009, Bug=10017, New Feature=11081 (CIPLAT project)

**Critical Field IDs (INSTANCE-SPECIFIC - these are Indeed's, use `jira-discover-fields` to find yours):**
- **Story Points**: `customfield_10061` (varies by instance, commonly 10026 or 10061)
- **Sprint**: `customfield_10021` (contains sprint info with start/end dates)
- **Rank**: `customfield_10022` (controls backlog ordering, format: `0|prefix:suffix`)

**To find your field IDs:**
```bash
# List all custom fields
./jira-discover-fields

# Search for specific field
./jira-discover-fields --search "story points"

# Show fields used in a project
./jira-discover-fields --project MYPROJ
```

**Backlog Sorting (Rank Field):**
- **Format**: `0|hzzwg1:000i` where `hzzwg1` is prefix, `000i` is suffix
- **Sorting**: Lexicographic by prefix, then suffix (NOT numeric padding)
- **Example order**: `000i` → `000r` → `000v` → `001` → `002` → `004` → `00i`
- **Critical**: Use pure string comparison, don't pad or convert to numbers
- **Complete backlog required**: Must fetch ALL items before sorting (use pagination)

**Backlog Definition (Best Practice):**
```bash
# Better than listing specific statuses
project IN (PROJ1,PROJ2) AND sprint is EMPTY AND statusCategory != Done AND status != Deferred

# Catches all backlog items regardless of specific status
# Excludes: items in sprints, completed work, deferred items
```

**Common JQL Patterns (NOTE: project names in examples are fictional, replace with your projects):**
```bash
# Recent activity
./jira-api GET "/search/jql?jql=project%3DMYPROJ%20AND%20updated%20%3E%3D%20-14d&fields=key,summary,status"

# Multi-project search
./jira-api GET "/search/jql?jql=project%20IN%20(PROJ1,PROJ2,PROJ3)&fields=key,summary"

# Sprint tickets
./jira-api GET "/search/jql?jql=project%3DMYPROJ%20AND%20sprint%20%3D%20%22Sprint%202025-10-07%22&fields=key,summary,status"

# Epic children
./jira-api GET "/search/jql?jql=%22Epic%20Link%22%20%3D%20MYPROJ-1234&fields=key,summary,status"

# Status filtering
./jira-api GET "/search/jql?jql=project%3DMYPROJ%20AND%20status%20IN%20(%27Pending%20Triage%27,%27on%20Backlog%27)&fields=key,summary"
```

**API Endpoint Failures:**
- **Board endpoints DON'T WORK**: `/board/ID`, `/agile/1.0/board/ID` return 404 HTML
- **Use JQL instead**: Query tickets directly with sprint/status filters
- **HTML responses**: Usually mean wrong endpoint or permissions issue

**URL Encoding Requirements:**
- **Spaces**: `%20` (e.g., `project%20%3D%20CIPLAT`)
- **Quotes**: `%22` (e.g., `%22Epic%20Link%22`)
- **Equals**: `%3D` (e.g., `project%3DCIPLAT`)
- **IN clauses**: `%20IN%20` (e.g., `status%20IN%20(...)`)

**Status Names (exact case):**
- `"Pending Triage"`, `"on Backlog"`, `"In Progress"`, `"Pending Review"`, `"Done"`, `"Closed"`, `"Blocked"`

### Jira View (`jira-view`)
- **Interactive TUI**: Split-pane terminal interface with vim keybindings for browsing tickets
- **Color-coded display**: Status indicators, priorities, and relative timestamps with visual formatting
- **Smart comments**: Shows last 2 days of comments by default (minimum 10)
- **Full mode**: `--full` flag includes all comments and complete change history
- **Dense but readable**: Formatted descriptions, bullet lists, and code blocks
- **Graceful fallback**: Works in basic mode when curses is unavailable

**Examples:**
```bash
# View a single ticket
jira-view PLAT-1234

# View with colors (auto-detects by default)
jira-view PLAT-1234 --color

# View with all comments and history
jira-view PLAT-1234 --full

# Query and select interactively
jira-view "project=PLAT AND status='In Progress'"

# Query with ordering
jira-view "project=PLAT AND assignee=currentUser() ORDER BY updated DESC"

# Complex queries
jira-view "project IN (PLAT,BACKEND) AND sprint='Sprint 123' AND status NOT IN (Done,Closed)"
```

**Interactive TUI mode:**
When given a JQL query, jira-view displays a split-pane interface with vim keybindings:
- **Left pane**: Scrollable list of matching tickets (key + status + summary)
- **Right pane**: Full ticket details for currently selected ticket
- **Navigation**: `j`/`k` or arrow keys to move up/down, `g`/`G` for top/bottom
- **Actions**: `v` to open in browser, `f` to toggle full mode (all comments), `r` to refresh
- **Search**: `/` to filter tickets, `?` for help, `q` to quit
- **Auto-scrolling**: Selection automatically scrolls list to keep current ticket visible

**Features:**
- **Relative timestamps**: Shows "2d ago", "5h ago" with color coding (green < 2d, yellow 2-4d)
- **JQL hints**: Suggests fixes for common JQL syntax mistakes (e.g., relative date formats)
- **Graceful fallback**: Runs in basic mode when curses is unavailable (shows first result only)

### Confluence API (`confluence-api`)
- **Space-specific searches**: Use `space=SPACEKEY` parameter for better results
- **Blog posts**: Search with `type=blogpost and space=SPACEKEY`
- **User identification**: Authors use `accountId`, not usernames
- **Content expansion**: Add `expand=history,body.storage` to get creator and content
- **Search scope**: Default searches may not include all accessible spaces

### Calendar Link (`calendar-link`)
- **Date format**: Use `YYYYMMDDTHHMMSS/YYYYMMDDTHHMMSS` for timed events
- **All-day events**: Use `YYYYMMDD/YYYYMMDD` (end date is day *after* event)
- **Browser integration**: Use `-o` flag to prompt for opening in Google Chrome
- **URL encoding**: Spaces and special characters are automatically encoded
- **Parameters**: Required: `-t TITLE -d DATES`, Optional: `-l LOCATION -D DESCRIPTION -z TIMEZONE`

**Examples:**
```bash
# Generate URL only
calendar-link -t "Team Meeting" -d "20250926T140000/20250926T150000"

# Generate and prompt to open in browser
calendar-link -t "Project Review" -d "20250927T100000/20250927T110000" \
              -l "Conference Room A" -D "Quarterly review meeting" -o

# All-day event
calendar-link -t "Conference" -d "20250928/20250929" -l "Austin, TX"
```

### Sprint Dashboard (`sprint-dashboard`)
- **Multi-team support**: Configure teams in `teams.conf` with custom JQL queries
- **Status tracking**: Shows sprint work categorized by status (TO-DO, IN PROGRESS, IN REVIEW, DONE)
- **Backlog view**: Ranked backlog items with status indicators ([B]acklog, [T]riage, Bloc[X]ed)
- **Activity tracking**: Shows days since last update with color coding (green < 2d, yellow 2-4d)
- **Auto-sizing**: Adapts summary length to terminal width automatically
- **Color support**: ANSI colors for xterm with `--color` flag

**Examples (NOTE: team names are from teams.conf, configure yours first):**
```bash
# Default team (first in config)
sprint-dashboard

# Specific team dashboard
sprint-dashboard platform-team

# Support team dashboards
sprint-dashboard support-team

# With options
sprint-dashboard backend-team --count 15 --color --length 100

# List available teams
sprint-dashboard --list-teams
```

**Configuration:**
Edit `teams.conf` to add new teams:
```ini
[my-team]
display_name = My Team Name
projects = PROJ1,PROJ2
sprint_name = My Sprint Name
# backlog_jql = custom query  # Optional - defaults to project-based query
```

**Automatic Behavior:**
- **Sprint JQL**: Auto-generates `project IN (projects) AND sprint = "sprint_name"`
- **Backlog JQL**: Auto-generates `project IN (projects) AND sprint is EMPTY AND statusCategory != Done AND type not in (Deploy, Request)`
- **Sprint detection**: Uses `find-current-sprint` on first project if no `sprint_name` configured
- **Overrides**: Use `--sprint "name"` or `--backlog-jql "query"` to override defaults

### Backlog Dashboard (`backlog-dashboard`)
- **Triage focus**: Shows backlog items prioritized for cleanup sessions
- **Three sections**: Pending Triage → Due Soon → Other Backlog
- **Complete backlog**: Fetches all backlog items using proper pagination
- **Status indicators**: Color-coded status markers with comprehensive legend
- **Due date awareness**: Highlights items with approaching deadlines
- **Deferred control**: Optional inclusion of deferred tickets

**Examples:**
```bash
# Default backlog triage (excludes deferred)
backlog-dashboard

# Specific team backlog
backlog-dashboard backend-team

# Include deferred tickets
backlog-dashboard platform-team --include-deferred

# Show more items
backlog-dashboard platform-team --count 50
```

### Find Current Sprint (`find-current-sprint`)
- **Purpose**: Discovers the active sprint name for any Jira project
- **Usage**: `find-current-sprint PROJECT_KEY`
- **Output**: Current sprint name (e.g., "Platform Sprint 2025-10-07", "Backend Sprint 131")
- **Use case**: Get sprint names to update `teams.conf` with accurate sprint-based JQL queries

**Examples:**
```bash
# Find current sprints
find-current-sprint PLAT      # → "Platform Sprint 2025-10-07"
find-current-sprint BACKEND   # → "Backend Sprint 131"
find-current-sprint FRONTEND  # → "Frontend Sprint 131"

# Use output to update config
echo "sprint_jql = project=PLAT AND sprint = \"$(./find-current-sprint PLAT)\""
```

### Epic Dashboard (`epic-dashboard`)
- **Epic tracking**: Shows all tickets linked to specific epic(s) with status breakdown
- **Status indicators**: Color-coded status markers ([C]losed, [P]rogress, [R]eview, [T]riage, [B]acklog, [Q]requirements, [D]eferred, [X]blocked)
- **Activity tracking**: Days since last update with color coding (green < 2d, yellow 2-4d)
- **Sprint information**: Shows which tickets are actively in sprints vs backlog
- **Progress metrics**: Per-person creation/resolution stats with story point percentages
- **Multi-epic support**: Analyze multiple related epics together
- **Flexible display**: Show/hide completed tickets, filter trivial contributors

**Examples:**
```bash
# Single epic status (show all)
epic-dashboard PLAT-1234

# Multiple epics
epic-dashboard PLAT-1234,PLAT-5678

# Hide completed work for focus
epic-dashboard PLAT-1234 --hide-done

# Filter out minor contributors
epic-dashboard PLAT-1234 --hide-trivial-contributors

# With options
epic-dashboard PLAT-1234 --color --length 120
```

**Output Format:**
- Status breakdown with counts (Done: 5 | To Do: 11 | Blocked: 1)
- Tickets by status with format: `[S] TICKET-123 (2d) [3pt, user] [Sprint=...] [P:priority]: Summary...`
- Progress report showing created/resolved tickets and story point percentages per person (sorted by completion)

### Find Active Epics (`find-active-epics`)
- **Purpose**: Discovers epics with recent activity to identify active projects
- **Usage**: `find-active-epics PROJECT_KEY [--days N]`
- **Default timeframe**: 30 days (configurable with `--days`)
- **Output**: Epic keys, summaries, and days since last update
- **Use case**: Find which epics to analyze with `epic-dashboard`

**Examples:**
```bash
# Find active Platform epics (last 30 days)
find-active-epics PLAT

# Find Backend epics with activity in last 60 days
find-active-epics BACKEND --days 60

# Use output with project dashboard
./find-active-epics PLAT --days 60
# → PLAT-1234: Authentication Service Migration (updated 38d ago)
./epic-dashboard PLAT-1234
```

# Dev Notes

When developing features, make sure you test API endpoints before coding as
they often do not work as expected. Jira has been around a long time and gone
through many different revisions of their API and we operate against Jira Cloud
which has some restrictions. Also sometimes permissions will foil us. In any
case, if you test the APIs first directly to see how they work you will have
better results.

