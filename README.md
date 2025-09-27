# Personal AI Tools

This repo contains tools I have built to enable me to use AI more easily in my daily workflows. They are mostly commands and scripts for having Claude interact with Jira, Confluence, Gitlab, and other things so I don't have to.

## Tools

- **jira-export** - Exports Jira tickets with full history and custom fields to JSON for AI analysis (run with `--help-ai` for detailed usage)
- **jira-api** - Bash wrapper for Jira REST API with cross-platform authentication (includes comprehensive usage docs and field IDs in script)
- **confluence-api** - Bash wrapper for Confluence REST API with cross-platform authentication (includes comprehensive usage docs in script)
- **calendar-link** - Generates Google Calendar event URLs from command line parameters with optional browser integration
- **gitlab-api** - Bash wrapper for GitLab REST API with keychain authentication
- **team-dashboard** - Configurable team dashboard showing current sprint status and backlog items (supports multiple teams via config file)

## API Usage Notes

### Authentication
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

### Team Dashboard (`team-dashboard`)
- **Multi-team support**: Configure teams in `team-dashboard.conf` with custom JQL queries
- **Status tracking**: Shows sprint work categorized by status (TO-DO, IN PROGRESS, IN REVIEW, DONE)
- **Backlog view**: Ranked backlog items with status indicators ([B]acklog, [T]riage, Bloc[X]ed)
- **Activity tracking**: Shows days since last update with color coding (green < 2d, yellow 2-4d)
- **Auto-sizing**: Adapts summary length to terminal width automatically
- **Color support**: ANSI colors for xterm with `--color` flag

**Examples:**
```bash
# Default CIPLAT dashboard
team-dashboard

# CDPLAT team dashboard
team-dashboard cdplat

# Support team dashboards
team-dashboard ciplat-support
team-dashboard cdplat-support

# With options
team-dashboard cdplat --count 15 --color --length 100

# List available teams
team-dashboard --list-teams
```

**Configuration:**
Edit `team-dashboard.conf` to add new teams:
```ini
[my-team]
display_name = My Team Name
sprint_jql = project IN (PROJ1,PROJ2) AND updated >= -14d
backlog_jql = project IN (PROJ1,PROJ2) AND status IN ('Pending Triage','on Backlog','Blocked')
```
