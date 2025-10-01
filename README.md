# Personal AI Tools

This repo contains tools I have built to enable me to use AI more easily in my daily workflows. They are mostly commands and scripts for having Claude interact with Jira, Confluence, Gitlab, and other things so I don't have to.

## Tools

- **jira-export** - Exports Jira tickets with full history and custom fields to JSON for AI analysis (run with `--help-ai` for detailed usage)
- **jira-api** - Bash wrapper for Jira REST API with cross-platform authentication (includes comprehensive usage docs and field IDs in script)
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
### PagerDuty API (`pagerduty-api`)
- **Token-based auth**: Uses PagerDuty API tokens with `Authorization: Token token=...` header
- **API v2**: Uses PagerDuty REST API v2 with proper Accept headers
- **Cross-platform**: Token file, environment variable, or keychain support

**Token Setup:**
1. Visit: https://indeed.pagerduty.com/api_keys
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

All Atlassian tools (`jira-api`, `jira-export`, `confluence-api`) support:
1. **Token file**: `~/.atlassian-mcp-token` (preferred for Linux/cross-platform)
2. **Environment variables**: `JIRA_TOKEN`, `CONFLUENCE_TOKEN`
3. **macOS Keychain**: Automatic fallback on macOS systems

### Jira API (`jira-api`)
- **Modern API**: Uses `/rest/api/3` (v2 is deprecated)
- **Search syntax**: Use `/search/jql?jql=QUERY` for ticket searches
- **User references**: Use `currentUser()` instead of usernames in JQL
- **Field IDs**: Bug tickets require environment field (`customfield_11674`)
- **Issue types**: Task=10009, Bug=10017, New Feature=11081 (CIPLAT project)

**Critical Field IDs (Indeed-specific):**
- **Story Points**: `customfield_10061` (not 10026)
- **Sprint**: `customfield_10021` (contains sprint info with start/end dates)
- **Rank**: `customfield_10022` (controls backlog ordering, format: `0|prefix:suffix`)

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

**Common JQL Patterns:**
```bash
# Recent activity
./jira-api GET "/search/jql?jql=project%3DCIPLAT%20AND%20updated%20%3E%3D%20-14d&fields=key,summary,status"

# Multi-project search
./jira-api GET "/search/jql?jql=project%20IN%20(CIPLAT,GITLAB,NEXUS)&fields=key,summary"

# Sprint tickets
./jira-api GET "/search/jql?jql=project%3DCIPLAT%20AND%20sprint%20%3D%20%22CIPLAT%202025-10-07%22&fields=key,summary,status"

# Epic children
./jira-api GET "/search/jql?jql=%22Epic%20Link%22%20%3D%20CIPLAT-2148&fields=key,summary,status"

# Status filtering
./jira-api GET "/search/jql?jql=project%3DCIPLAT%20AND%20status%20IN%20(%27Pending%20Triage%27,%27on%20Backlog%27)&fields=key,summary"
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

**Examples:**
```bash
# Default CIPLAT dashboard
sprint-dashboard

# CDPLAT team dashboard
sprint-dashboard cdplat

# Support team dashboards
sprint-dashboard ciplat-support
sprint-dashboard cdplat-support

# With options
sprint-dashboard cdplat --count 15 --color --length 100

# List available teams
sprint-dashboard --list-teams
```

**Configuration:**
Edit `teams.conf` to add new teams:
```ini
[my-team]
display_name = My Team Name
sprint_jql = project IN (PROJ1,PROJ2) AND sprint = "My Sprint Name"
backlog_jql = project IN (PROJ1,PROJ2) AND sprint is EMPTY AND statusCategory != Done AND status != Deferred
```

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

# CDPLAT backlog
backlog-dashboard cdplat

# Include deferred tickets
backlog-dashboard ciplat --include-deferred

# Show more items
backlog-dashboard ciplat --count 50
```

### Find Current Sprint (`find-current-sprint`)
- **Purpose**: Discovers the active sprint name for any Jira project
- **Usage**: `find-current-sprint PROJECT_KEY`
- **Output**: Current sprint name (e.g., "CIPLAT 2025-10-07", "CD Platform Sprint 131")
- **Use case**: Get sprint names to update `sprint-dashboard.conf` with accurate sprint-based JQL queries

**Examples:**
```bash
# Find current sprints
find-current-sprint CIPLAT    # → "CIPLAT 2025-10-07"
find-current-sprint MARVIN    # → "CD Platform Sprint 131"
find-current-sprint ORC       # → "CD Platform Sprint 131"

# Use output to update config
echo "sprint_jql = project=CIPLAT AND sprint = \"$(./find-current-sprint CIPLAT)\""
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
epic-dashboard CIPLAT-2148

# Multiple epics
epic-dashboard CIPLAT-2148,CIPLAT-2150

# Hide completed work for focus
epic-dashboard CIPLAT-2148 --hide-done

# Filter out minor contributors
epic-dashboard CIPLAT-2148 --hide-trivial-contributors

# With options
epic-dashboard CIPLAT-2148 --color --length 120
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
# Find active CIPLAT epics (last 30 days)
find-active-epics CIPLAT

# Find MARVIN epics with activity in last 60 days
find-active-epics MARVIN --days 60

# Use output with project dashboard
./find-active-epics CIPLAT --days 60
# → CIPLAT-2148: Nexus Replacement (Cloudsmith) - Phase 1 (updated 38d ago)
./epic-dashboard CIPLAT-2148
```
