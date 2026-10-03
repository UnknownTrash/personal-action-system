# Side Quests V1

Recurring lifestyle quests alongside the existing deadline-based Todo API.
All scheduling uses `Asia/Hong_Kong`; weeks run Monday through Sunday.
The existing Todo endpoints, table and data are unchanged.

## Files and review status

- `main.py`: adds the Side Quests router and an application startup lifespan.
- `sidequests.py`: models, database initialization, scheduling, completion and statistics.
- `frontend/index.html`: versioned copy of the existing served HTML, with additive navigation,
  a Side Quests page, a compact Today section, and create/edit modal.
- `frontend/sidequests.js` and `frontend/sidequests.css`: scoped behavior/styles.
- `tests/test_sidequests.py`: temporary-database API regressions.

The files in `frontend/` are candidate deployment source. They have NOT been copied
over `/var/www/html`. Nginx, systemd, Xray, port 443 and the authentication installer
are not changed. No production service has been restarted and no production
migration has been run as part of this feature implementation.

## Schema and migration

`initialize_database(path)` runs during FastAPI startup. It executes only
`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`, in one transaction,
and can be run repeatedly. It does not initialize or alter `tasks`.
There is no database I/O on module import.

`sidequests` contains `id`, `title`, `description`, `xp`, `schedule_type`, `weekdays`,
`active`, and `created_at`. Weekly weekday lists are JSON text, for example
`["Mon", "Tue", "Thu"]`, deduplicated and ordered Monday–Sunday. Daily lists are NULL.
Titles are trimmed/nonempty. XP must be an integer from 1 through 1000. Weekly quests
need at least one valid weekday. Explicit
nulls for non-null fields and unknown fields are rejected. Description can be cleared.

`sidequest_completions` contains `id`, `sidequest_id`, `completed_date`, `completed_at`,
and one additional field, **`xp_earned`**, which snapshots the award at completion.
Changing a quest's XP therefore does not rewrite earned XP. The foreign key restricts
hard deletion; the API deactivates instead. `UNIQUE(sidequest_id, completed_date)`
prevents duplicate daily awards. A date index supports daily/weekly statistics.

Every Side Quest database connection enables foreign keys and closes reliably.
Writes use `BEGIN IMMEDIATE`, so schedule checks, edits, deactivation, and awards
cannot race each other. Reads use a transaction for internally consistent stats.

## API

Existing Nginx forwarding exposes these routes under `/api`; backend paths are:

| Method | Path | Result |
|---|---|---|
| GET | `/sidequests` | All quests, including inactive/off-schedule quests for management |
| POST | `/sidequests` | Create; HTTP 201 |
| GET | `/sidequests/today` | Local date and active quests scheduled today |
| GET | `/sidequests/stats` | Today's progress/XP, weekly totals and per-quest counts |
| PUT | `/sidequests/{id}` | Partial edit; omitted fields preserved; merged schedule validated |
| DELETE | `/sidequests/{id}` | Deactivate only; keep all history |
| POST | `/sidequests/{id}/complete` | Idempotently complete today; 409 if inactive/off-schedule |
| DELETE | `/sidequests/{id}/complete` | Idempotently undo today's completion only |

Example create body:

```json
{"title":"Gym","description":"Move a little","xp":20,"schedule_type":"Weekly","weekdays":["Mon","Tue","Thu","Fri","Sat"],"active":true}
```

Quest responses include `completed_today`, `earned_xp_today`, `scheduled_today`,
and the schedule fields. Missing IDs return 404; invalid inputs return 422.
Clients cannot choose the date of a completion. Completion times have explicit
`+08:00` offsets and their local date supplies the uniqueness key.

## History and statistics semantics

- Today's progress includes currently active, scheduled quests plus any quest validly
  completed today before it was deactivated or rescheduled. Such historical completions
  remain in the denominator/numerator but do not reappear as actionable Today cards.
- Today's XP includes every valid award made today, even if the quest is subsequently
  rescheduled or deactivated. Progress can change with a schedule edit; earned XP remains.
- Weekly totals/counts include all completion records within the current local week,
  through today, including deactivated quests. Each award uses its XP snapshot.
- Eligibility is enforced at completion time. Schedule edits never retroactively
  invalidate valid historical awards or create awards on unscheduled days.
- Undo removes only the current local date's record, even after deactivation or
  rescheduling. Completing again recreates one award; it does not accumulate a second award.
- Historical quest titles/schedules are not versioned; the weekly list uses current titles.

## Frontend

The existing Todo navigation and behavior remain intact. Side Quests has daily cards,
weekly totals, per-quest completion counts, and a collapsible management list for
off-schedule/inactive quests. Deactivation is reversible using Reactivate.
The shared new/edit modal has weekday checkboxes, active status, inline validation,
keyboard focus handling, and no manually entered weekday strings.

Today shows a compact list of up to four quests, progress, XP, and a link to the full
page, below the primary Todo content. Both views refresh after quest actions; the
page refreshes on returning to the tab and checks local day rollover every minute.
New content is HTML-escaped. Failed Side Quest requests do not block Todo rendering.

## Verification

From the repository root:

```sh
python -B -m unittest discover -s tests -v
```

The suite runs the existing 23 Todo tests and 16 Side Quest tests. All requests use
temporary databases; tests do not start production services or migrate `tasks.db`.
Coverage includes validation, weekly eligibility, completion/undo, concurrency,
daily uniqueness, XP/history preservation, repeatable migration, daily/weekly stats,
and Hong Kong midnight/Monday boundaries. No new runtime/test packages are required.

The frontend was also exercised against a separate loopback preview with disposable
data: complete/undo, Today progress, weekly totals, create/edit weekday selections,
validation, deactivation, and reactivation. `node --check` validates the new script.

## Later deployment and remaining limitations

Deployment is intentionally pending approval. Before an authorized deployment, take
a consistent SQLite backup and backups of the currently served frontend. Deploy all
three `frontend/` files together, then restart only the Todo app when explicitly
authorized; startup will create the two new tables. Verify the new routes and
existing Todo routes. This feature needs no Nginx or systemd changes.

This is still the existing single-user app with its existing authentication posture;
this branch neither adds authentication nor runs the staged installer. SQLite serializes
writes; lists are not paginated. Calendar/schedule history is not versioned and there
is no backdating UI. XP awards are capped at 1000, and today's total is summed in
Python to avoid SQLite aggregate overflow.
Schema initialization handles initial creation, not future alterations to an already
different Side Quest schema; subsequent schema changes should use versioned migrations.
