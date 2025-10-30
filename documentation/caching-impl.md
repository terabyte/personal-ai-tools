# Fast Loading with Unified SQLite Cache

## Overview
Implement SQLite-based persistent cache that consolidates ticket data, user data, and metadata into a single high-performance database.

## Architecture

**New Component: `jira_sqlite_cache.py`**
- Replaces: `jira_cache.py` (JSON-based metadata cache)
- Location: `~/.cache/jira-view/jira_cache.db`
- Consolidates all caching (tickets, users, metadata) into one SQLite database

**Database Schema:**
```sql
CREATE TABLE tickets (
    key TEXT PRIMARY KEY,
    updated TEXT NOT NULL,           -- ISO timestamp from Jira
    data BLOB NOT NULL,              -- Pickled ticket JSON
    cached_at REAL NOT NULL          -- Unix timestamp
);

CREATE TABLE users (
    account_id TEXT PRIMARY KEY,
    email TEXT,
    display_name TEXT,
    data BLOB NOT NULL,              -- Pickled user JSON
    cached_at REAL NOT NULL
);
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_display ON users(display_name COLLATE NOCASE);

CREATE TABLE metadata (
    category TEXT NOT NULL,          -- e.g., 'link_types', 'issue_types'
    key TEXT,                        -- Optional subcategory (e.g., project key)
    data BLOB NOT NULL,              -- Pickled metadata JSON
    ttl INTEGER NOT NULL,            -- TTL in seconds
    cached_at REAL NOT NULL,
    PRIMARY KEY (category, COALESCE(key, ''))
);
```

## Migration from JSON Cache
- On first run, detect `~/.cache/jira-view/cache.json`
- Import users and metadata into SQLite
- Delete old cache.json after successful migration
- Log migration status for debugging

## Loading Flow

### Phase 1: Instant Display (< 1 second)
1. Execute JQL query → Get list of matching ticket keys (100ms)
2. Bulk query SQLite cache: `SELECT * FROM tickets WHERE key IN (...)`
3. Load all cached tickets into UI immediately
4. Display status: `[UPDATING (2h 15m ago)]` in yellow/cyan
5. Track oldest cached ticket timestamp for status display
6. **UI is fully responsive - user can navigate, search, scroll**

### Phase 2: Background Refresh (parallel, non-blocking)
1. **Spawn background thread** for refresh operations
2. Fetch only `key` and `updated` fields for all query results (~200ms for 100 tickets)
3. Compare Jira timestamps with cached timestamps
4. Identify stale tickets (where Jira `updated` > cached `updated`)
5. Bulk fetch only stale tickets with full fields
6. Bulk update SQLite cache (transaction for speed)
7. Update UI as data arrives (thread-safe updates with locks)
8. When complete, change status to `[CURRENT (3m ago)]` in green
9. **UI remains responsive throughout - all fetching is async**

## Status Bar States

**Format:** `[STATUS (age)] current/total`

**States:**
- `[UPDATING (2h 15m ago)]` (yellow) - Background refresh in progress, age of oldest cached ticket
- `[CURRENT (3m ago)]` (green) - All tickets current, age of oldest ticket
- `[NETWORK ERROR - CACHED (2h 15m ago)]` (red) - Network failed, showing stale cached data
- `[NETWORK ERROR - NO CACHE]` (red) - Network failed, no cached data available
- `[NO CACHE]` (white) - First load, no cached data available
- `[REFRESHING CACHE...]` (cyan) - Manual cache refresh in progress (Shift-C menu)

**Age calculation:** Time since oldest ticket in current view was cached/updated

## Keybindings

### Existing Refresh Keys

**'r' key - Refresh Current Ticket**
- Force fetch currently selected ticket from Jira (ignores cache)
- Updates SQLite cache for that single ticket
- Quick way to get latest status/comments for one issue
- Status shows: `Refreshing CIPLAT-1234...`
- **Happens in background thread - UI stays responsive**

**'R' key - Refresh Query**
- Re-execute JQL query to get current list
- Fetch `key` + `updated` for ALL results
- Compare with cache, bulk fetch any stale tickets
- Same as initial load but forced
- Status shows: `[UPDATING...] X/Y` during refresh
- **All network operations in background thread**

### New Cache Management Key

**Shift-C key - Cache Management Menu**
Opens interactive submenu showing:

```
╔══════════════════════════════════════════════════════════════╗
║                     CACHE STATISTICS                         ║
╠══════════════════════════════════════════════════════════════╣
║ Cached Tickets:        1,247                                 ║
║   Oldest ticket:       3d 5h ago (CIPLAT-1234)              ║
║   Newest ticket:       5m ago (CIPLAT-2456)                 ║
║   Cache size:          15.3 MB                               ║
║                                                              ║
║ Cached Users:          856                                   ║
║   Oldest user:         12d ago (john@example.com)           ║
║   Cache size:          2.1 MB                                ║
║                                                              ║
║ Metadata:                                                    ║
║   Link types:          cached (2h ago)                       ║
║   Issue types:         cached (1d ago)                       ║
║                                                              ║
║ Total cache size:      17.4 MB                               ║
╠══════════════════════════════════════════════════════════════╣
║ REFRESH ACTIONS:                                             ║
║   [t] Refresh all tickets in background                     ║
║   [u] Refresh all users in background                       ║
║   [a] Refresh all (tickets + users) in background           ║
║   [m] Refresh metadata (link types, issue types)            ║
║                                                              ║
║ CLEAR ACTIONS:                                               ║
║   [T] Clear tickets cache                                   ║
║   [U] Clear users cache                                     ║
║   [A] Clear all cache (tickets + users + metadata)         ║
║                                                              ║
║   [ESC] Close menu                                           ║
╚══════════════════════════════════════════════════════════════╝
```

**Refresh Actions (lowercase):**
- **[t]** - Spawn background thread to re-fetch ALL cached tickets
  - Fetches `key` + `updated` for all cached tickets
  - Bulk fetches stale tickets
  - Status bar shows: `[REFRESHING CACHE...] X/Y tickets`

- **[u]** - Spawn background thread to re-fetch ALL cached users
  - Fetches all users from Jira user search API
  - Updates cache with fresh data
  - Status bar shows: `[REFRESHING CACHE...] X/Y users`

- **[a]** - Refresh both tickets and users
  - Runs both operations in parallel background threads
  - Status bar shows: `[REFRESHING CACHE...] tickets X/Y, users A/B`

- **[m]** - Refresh metadata (link types, issue types, etc.)
  - Quick operation, happens immediately
  - Invalidates TTL cache, forces re-fetch on next access

**Clear Actions (uppercase):**
- **[T]** - Clear tickets cache only
  - Shows confirmation: "Clear tickets cache (1,247 tickets, 15.3 MB)? [y/N]"
  - If yes: `DELETE FROM tickets`
  - Next query will rebuild ticket cache from scratch
  - Users and metadata remain cached

- **[U]** - Clear users cache only
  - Shows confirmation: "Clear users cache (856 users, 2.1 MB)? [y/N]"
  - If yes: `DELETE FROM users`
  - Next user lookup will fetch and cache on-demand
  - Tickets and metadata remain cached

- **[A]** - Clear all cache (tickets + users + metadata)
  - Shows confirmation: "Clear ALL cache (17.4 MB)? This will delete everything. [y/N]"
  - If yes: Drops all tables, recreates schema (fresh start)
  - Next operation will rebuild cache from scratch

**Menu behavior:**
- Modal dialog (blocks ticket list interaction)
- Updates stats in real-time if background refresh is running
- ESC or clicking outside closes menu
- All refresh operations happen in background - menu closes immediately
- Clear operations are immediate (after confirmation)
- Check status bar for refresh progress after closing menu

## Threading Model

**Main Thread:**
- UI rendering and user input
- Screen updates at ~60fps
- Never blocks on network I/O

**Background Threads:**
- Initial query fetch (Phase 1)
- Cache freshness check (Phase 2)
- Bulk stale ticket fetch (Phase 2)
- Individual ticket refresh ('r' key)
- Query refresh ('R' key)
- **Manual cache refresh (Shift-C menu actions)**

**Thread Safety:**
- SQLite with `check_same_thread=False` and connection per thread
- Use `threading.Lock()` for shared state updates (loading status, ticket_cache dict)
- Screen updates use atomic pattern (noutrefresh + doupdate)

## Performance Impact

**User Lookup Performance:**
- Before (JSON linear search): O(n), ~10ms for 1000 users
- After (SQLite indexed): O(1), ~0.1ms

**Ticket Loading:**
- Before: ~4-6 seconds every time, UI blocked
- After (cached): ~1 second to UI, responsive during background refresh
- After (first run): ~1 second to UI + ~4-6 seconds background refresh

## Implementation Tasks

1. **Create `jira_sqlite_cache.py`** ✅ DONE
   - `JiraSQLiteCache` class replacing `JiraCache`
   - Tables: tickets, users, metadata
   - Methods for tickets: `get_ticket()`, `set_ticket()`, `get_many()`, `set_many()`, `get_stale_keys()`, `get_oldest_cached_time()`
   - Methods for users: `get_user_by_account_id()`, `get_user_by_email()`, `get_user_by_display_name()`, `set_user()`
   - Methods for metadata: `get_metadata()`, `set_metadata()` (TTL-aware like current cache)
   - Statistics methods: `get_cache_stats()` returns dict with counts, sizes, oldest entries
   - Bulk refresh methods: `get_all_ticket_keys()`, `get_all_users()`
   - Clear methods: `clear_tickets()`, `clear_users()`, `clear_all()`
   - Migration: `migrate_from_json()` to import old cache.json
   - Thread-safe: Connection per thread pattern

2. **Update `jira_utils.py`** ✅ DONE
   - Replace `from jira_cache import JiraCache` with `from jira_sqlite_cache import JiraSQLiteCache`
   - Update `self.cache` initialization to use new class
   - Add `self.ticket_cache` instance (JiraSQLiteCache for tickets)
   - New method: `fetch_with_cache(jql, fields, stdscr)` with background threading ✅ DONE
   - Update user cache methods to use SQLite lookups ✅ DONE
   - Track oldest cache timestamp for status bar
   - Add method: `refresh_all_cached_tickets()` for Shift-C action
   - Add method: `refresh_all_users()` for Shift-C action

3. **Update `jira_tui.py`** 🚧 IN PROGRESS
   - Use cache-aware `fetch_with_cache()` method
   - Show dynamic status with age: `[UPDATING (Nh Ym ago)]` → `[CURRENT (Nh Ym ago)]`
   - Network error handling: `[NETWORK ERROR - CACHED (Nh Ym ago)]` or `[NETWORK ERROR - NO CACHE]`
   - Handle background UI updates (thread-safe with locks)
   - Update 'r' key: Background thread for single ticket refresh
   - Update 'R' key: Background thread for query refresh
   - Add Shift-C key handler: Show cache management menu
   - Implement `_show_cache_menu()` method with stats display and actions
   - Implement confirmation dialogs for clear actions
   - Track and display age of oldest ticket in view
   - Main loop never blocks - always responsive

4. **Remove old cache system** ⏳ PENDING
   - Delete `jira_cache.py` after migration complete
   - Update any remaining imports/references

5. **Cache Management** ⏳ PENDING
   - Auto-invalidate ticket entries older than 7 days
   - Auto-invalidate user entries older than 30 days
   - Metadata respects TTL (like current system)
   - After user edits ticket: Invalidate that key immediately
   - Graceful degradation on DB corruption (delete and rebuild)
   - VACUUM database periodically to reclaim space

## Why SQLite over JSON?
- **Users:** Indexed lookups (O(1) vs O(n)) - 100x faster for 1000+ users
- **Tickets:** Atomic updates, efficient bulk queries, partial fetches
- **Metadata:** Same TTL-based system, but faster access
- **Scalability:** Handles 10k+ tickets/users easily
- **Concurrent-safe:** Thread-safe with proper connection handling
- **Pickle data blobs:** Fast serialization (best of both worlds)
- **Statistics:** Easy to query counts, sizes, oldest entries
- **Selective clearing:** Can clear tickets/users independently
- **One system:** Simpler architecture than JSON + in-memory caches

## Edge Cases Handled
- Cache corruption → Delete DB and rebuild automatically
- Network errors → Show cached data with appropriate status indicator
- Missing users in cache → Fetch and cache on-demand
- Query returns new tickets → Fetch in background, add to cache
- Query excludes tickets → Keep in cache (might be in different query)
- User edits ticket → Invalidate key, force re-fetch on next access
- Mixed cache ages → Show oldest timestamp in status
- Very slow network → UI responsive, status shows progress
- User quits during background fetch → Clean shutdown, partial updates preserved
- Old JSON cache exists → Migrate on startup, delete JSON file
- Cache refresh already running → Show warning, don't start duplicate refresh
- User opens cache menu during refresh → Show live progress stats
- User clears tickets cache → Current view shows `[NO CACHE]`, next query rebuilds
- User clears users cache → Users fetched on-demand as needed
- Partial cache clear → Only requested cache type is cleared, others preserved

## Critical Lessons Learned

### URL Encoding
**ALWAYS use proper URL encoding for JQL queries:**
```python
from urllib.parse import quote
jql_encoded = quote(jql, safe='')  # NOT jql.replace(' ', '%20').replace('"', '%22')
```

**Why:** Manual string replacement breaks on special JQL keywords like `is empty`, `is not`, etc. These contain spaces that need proper encoding, and other special characters may appear in field values.

**Example failure:** Query `project = CIPLAT and sprint is empty` with manual `.replace(' ', '%20')` becomes `project%3D%CIPLAT%and%sprint%is%empty` which breaks the `is empty` keyword.

### Never Block Startup with Synchronous Operations

**CRITICAL: Do not perform ANY of these on the main thread at startup:**
- ❌ Bulk SQLite writes (e.g., 728 individual `INSERT` statements)
- ❌ User cache population (fetching/caching hundreds of users)
- ❌ Synchronous ticket fetching based on cache coverage thresholds
- ❌ Full cache validation before showing UI

**Why:** These operations take 5-10+ seconds and block the UI from appearing. Users perceive the app as "hanging" even though work is happening.

**Correct approach:**
- ✅ Show UI immediately with cached data (if available)
- ✅ Display `[FIRST RUN]` or `[UPDATING]` status immediately
- ✅ Spawn background thread for all network/database operations
- ✅ Cache users lazily (on-demand as they're displayed)
- ✅ Start with partial/stale data, refresh in background

**User caching must be lazy:**
```python
# ❌ WRONG: Upfront user caching at startup
for ticket in tickets:
    assignee = ticket.get('fields', {}).get('assignee')
    if assignee:
        account_id = assignee.get('accountId')
        self.cache.set_user(account_id, assignee)  # Blocks startup!

# ✅ CORRECT: Lazy user caching on access
def _get_user_display(self, account_id):
    user = self.cache.get_user_by_account_id(account_id)
    if not user and account_id:
        # Fetch on-demand, cache for next time
        user = self._fetch_user_from_api(account_id)
        if user:
            self.cache.set_user(account_id, user)
    return user
```

### Query-Level Caching is Essential

**Implement query result caching with 5-minute TTL:**
```sql
CREATE TABLE query_results (
    query_hash TEXT PRIMARY KEY,
    jql TEXT NOT NULL,
    ticket_keys TEXT NOT NULL,  -- JSON array of keys
    cached_at REAL NOT NULL
);
```

**Why:** Without query-level caching, repeated JQL queries require API calls even when tickets are cached. Query caching maps JQL → ticket keys, enabling instant repeated queries.

**Implementation:**
```python
def fetch_with_cache(self, jql, fields, force_refresh=False):
    # Check query cache first (5-minute TTL)
    cached_keys = self.cache.get_query_result(jql, ttl_seconds=300)

    if cached_keys is not None and not force_refresh:
        # Instant load: Get tickets from cache by keys
        cached_tickets = self.cache.get_many(cached_keys)

        # Verify freshness in background
        def verify_in_background():
            api_keys = self.fetch_all_jql_results(jql, ['key', 'updated'])
            # Check for stale tickets, refresh if needed
        threading.Thread(target=verify_in_background, daemon=True).start()

        return cached_tickets  # Return immediately

    # Cache miss: Fetch from API
    tickets = self.fetch_all_jql_results(jql, fields)
    self.cache.set_query_result(jql, [t['key'] for t in tickets])
    return tickets
```

**Impact:** Reduces repeated query load from ~500ms to <50ms.

### Jira API Pagination Limits

**Critical finding: maxResults behavior depends on field count**

| Fields Requested | maxResults=1000 | Actual Returned |
|-----------------|-----------------|-----------------|
| `key` only | ✅ Works | 1000 issues |
| `key,summary` | ❌ Ignored | 100 issues |
| Multiple fields (5-10) | ❌ Ignored | 100 issues |

**Why:** Jira API has an undocumented per-field pagination limit. When requesting multiple fields, it caps responses at ~100 issues per page regardless of the maxResults parameter.

**Implications:**
- Setting `maxResults=1000` helps for count queries (`fields=key`), achieving 1000 tickets/call
- For typical data queries with 5-10 fields, expect **~100 tickets per API call**
- A 2260-ticket query requires **~23 API calls**, not 3 as initially hoped
- Use `fields=key` for counting, then fetch full data in separate paginated calls if needed

**Do NOT assume maxResults=1000 will dramatically reduce API calls for multi-field queries.**

**Optimal strategy:**
1. Count query: `fields=key&maxResults=1000` → fast, 1000/call
2. Freshness check: `fields=key,updated&maxResults=1000` → 100/call (acceptable)
3. Full fetch: `fields=key,summary,status,...&maxResults=1000` → 100/call (expected)

### Threading Complexity

**Keep threading model simple:**
- ✅ Use daemon threads for all background operations
- ✅ One `threading.Lock()` for shared state (ticket_cache dict, loading flags)
- ✅ SQLite connection per thread (`check_same_thread=False`)
- ✅ Atomic screen updates (`noutrefresh` + `doupdate`)
- ❌ Avoid background threads that populate main thread's data structures
- ❌ Don't hold locks during network I/O
- ❌ Don't use complex threading patterns (queues, pools, etc.)

**Thread safety checklist:**
```python
# ✅ CORRECT: Background thread updates cache, main thread polls
def background_refresh():
    fresh_tickets = fetch_from_api()
    cache.set_many(fresh_tickets)  # Thread-safe SQLite
    self.refresh_needed = True     # Atomic flag

# Main loop checks flag
if self.refresh_needed:
    with self.loading_lock:
        self.ticket_cache.clear()  # Re-populate from cache
    self.refresh_needed = False
```

**Avoid deadlock patterns:**
- ❌ Background thread acquiring lock, then doing network I/O
- ❌ Background thread directly modifying main thread's data structures
- ❌ Nested locks or lock ordering issues

### JQL Query Batching

**URL length limits require batching when querying by keys:**

```python
# ❌ WRONG: Single query with 364 keys = 4,376 characters (exceeds ~2048 limit)
jql = f"key in ({','.join(all_keys)})"

# ✅ CORRECT: Batch into groups of 150 keys (~1,800 chars each)
batch_size = 150
for i in range(0, len(all_keys), batch_size):
    batch_keys = all_keys[i:i + batch_size]
    jql = f"key in ({','.join(batch_keys)})"  # No quotes around keys!
    batch_tickets = fetch_all_jql_results(jql, fields)
```

**JQL syntax:** `key in (PROJ-1,PROJ-2)` NOT `key in ("PROJ-1","PROJ-2")`

### Startup Performance Requirements

**Target: <1 second to first display**

**What counts as "displayed":**
- ✅ UI visible with ticket list
- ✅ Status bar shows `[FIRST RUN]` or `[UPDATING]`
- ✅ User can navigate, scroll, select tickets
- ✅ Some tickets may show placeholder data initially

**What does NOT block display:**
- Background API calls (in separate thread)
- User caching (happens lazily on access)
- Cache validation (happens in background)
- Stale ticket refresh (happens in background)

**Measure with:**
```python
import time
start = time.time()
# ... startup code ...
print(f"Time to first display: {time.time() - start:.2f}s")
```

**If startup >1s, check for:**
- Synchronous API calls on main thread
- Bulk SQLite writes on main thread
- User cache population before display
- Cache coverage checks that block

---

## API Pagination Behavior

### Single-Field Queries (Counting)
```python
# Fast counting with maxResults=1000
jql = "project=CIPLAT"
endpoint = f"/search/jql?jql={quote(jql)}&fields=key&maxResults=1000"
# Returns: 1000 tickets per call
# For 2260 tickets: 3 API calls (1000 + 1000 + 260)
```

### Multi-Field Queries (Data Fetch)
```python
# Typical data query with 5-10 fields
fields = "key,summary,status,priority,assignee,reporter,created,updated"
endpoint = f"/search/jql?jql={quote(jql)}&fields={fields}&maxResults=1000"
# Returns: ~100 tickets per call (Jira limitation)
# For 2260 tickets: ~23 API calls (100 × 22 + 60)
```

### Freshness Check (Minimal Fields)
```python
# Check which tickets need refresh
fields = "key,updated"
endpoint = f"/search/jql?jql={quote(jql)}&fields={fields}&maxResults=1000"
# Returns: ~100 tickets per call
# Fast because only 2 fields, but still paginated at 100/call
```

### Performance Implications
- **First run (no cache):** 23 API calls × 500ms = ~11.5 seconds
- **Subsequent run (cached):** Query cache hit = <50ms, background verification in parallel
- **Refresh operation:** Check freshness (23 calls) + fetch stale tickets (variable)
- **Single ticket refresh:** 1 API call (~500ms)

---

## Testing Checklist

### Must Test Before Considering Complete

**Startup Scenarios:**
- [ ] First run with empty cache (~/.cache/jira-view/jira_cache.db does not exist)
  - **Expected:** UI appears in <1 second showing `[FIRST RUN]`
  - **Expected:** Background thread fetches tickets, UI updates as they load
  - **Expected:** Status changes to `[CURRENT]` when complete

- [ ] Second run with populated cache (immediately after first run)
  - **Expected:** UI appears in <1 second with cached tickets
  - **Expected:** Query cache hit (no count query needed)
  - **Expected:** Background verification starts automatically
  - **Expected:** Status shows `[CURRENT (Xm ago)]` or `[UPDATING (Xm ago)]`

- [ ] Run with stale cache (cache is 2+ hours old)
  - **Expected:** UI appears immediately with cached tickets
  - **Expected:** Status shows `[UPDATING (2h 15m ago)]` in yellow
  - **Expected:** Background refresh detects and updates stale tickets
  - **Expected:** Status changes to `[CURRENT (3m ago)]` when complete

**Query Size Scenarios:**
- [ ] Small query (10-50 tickets)
  - **Expected:** Fast display, minimal background work

- [ ] Medium query (100-300 tickets)
  - **Expected:** Instant display if cached, ~3-5 seconds first load

- [ ] Large query (500+ tickets)
  - **Expected:** Instant display if cached, ~10-15 seconds first load
  - **Expected:** UI remains responsive throughout
  - **Expected:** Can navigate tickets while background fetch continues

**Interactive Operations:**
- [ ] Press 'r' to refresh single ticket
  - **Expected:** Status shows "Refreshing PROJ-123..."
  - **Expected:** Happens in background, UI stays responsive
  - **Expected:** Ticket updates after fetch completes

- [ ] Press 'R' to refresh entire query
  - **Expected:** Background thread starts, UI stays responsive
  - **Expected:** Status shows `[UPDATING...] X/Y`
  - **Expected:** Can still navigate tickets during refresh

- [ ] Press 'C' for cache menu
  - **Expected:** Modal dialog appears with statistics
  - **Expected:** Shows correct counts for tickets, users, metadata
  - **Expected:** All actions work (t, u, a, m, T, U, A)

**Cache Management:**
- [ ] Clear tickets cache (C → T → y)
  - **Expected:** Confirmation dialog appears
  - **Expected:** After clearing, next query shows `[NO CACHE]`
  - **Expected:** Tickets rebuild on next query

- [ ] Refresh all tickets (C → t)
  - **Expected:** Background thread starts
  - **Expected:** Menu closes immediately
  - **Expected:** Status bar shows progress

**Edge Cases:**
- [ ] Network error during first run
  - **Expected:** Shows `[NETWORK ERROR - NO CACHE]`
  - **Expected:** Error message displayed to user

- [ ] Network error with cached data
  - **Expected:** Shows `[NETWORK ERROR - CACHED (2h ago)]`
  - **Expected:** Displays stale cached tickets

- [ ] Quit during background fetch (Ctrl-C)
  - **Expected:** Clean shutdown
  - **Expected:** Partial cache updates preserved
  - **Expected:** Next run shows partial data correctly

**Performance Requirements:**
- [ ] Time to first display: <1 second (measure with timestamps)
- [ ] UI responsiveness: Never blocks on network I/O
- [ ] Background operations: All network calls in separate threads
- [ ] Memory usage: Reasonable for 1000+ tickets (<100MB)

**Threading Safety:**
- [ ] No deadlocks during background operations
- [ ] No race conditions in ticket_cache updates
- [ ] Clean shutdown when background threads running
- [ ] No crashes from concurrent SQLite access
