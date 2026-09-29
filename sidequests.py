"""Recurring quests. Weekdays are JSON arrays; completion XP is a historical snapshot."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import sqlite3
from typing import Annotated, Callable, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StringConstraints,
                      ValidationError, field_validator, model_validator)

LOCAL_TIMEZONE = ZoneInfo("Asia/Hong_Kong")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
Weekday = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
Schedule = Literal["Daily", "Weekly"]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
XP = Annotated[int, Field(strict=True, ge=1, le=1000)]


def local_now():
    return datetime.now(LOCAL_TIMEZONE)


class SideQuestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title
    description: Optional[str] = None
    xp: XP
    schedule_type: Schedule
    weekdays: Optional[list[Weekday]] = None
    active: StrictBool = True

    @model_validator(mode="after")
    def validate_schedule(self):
        if self.schedule_type == "Weekly":
            if not self.weekdays:
                raise ValueError("Weekly quests require at least one weekday")
            self.weekdays = [day for day in WEEKDAYS if day in self.weekdays]
        else:
            self.weekdays = None
        return self


class SideQuestUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[Title] = None
    description: Optional[str] = None
    xp: Optional[XP] = None
    schedule_type: Optional[Schedule] = None
    weekdays: Optional[list[Weekday]] = None
    active: Optional[StrictBool] = None

    @field_validator("title", "xp", "schedule_type", "active")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Omit this field to preserve its value; null is not allowed")
        return value


@contextmanager
def database(path, *, write=False):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # Serialize eligibility checks with edits and completion writes.
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database(path):
    """Add only Side Quest tables/indexes, atomically and idempotently, at startup."""
    with database(path, write=True) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sidequests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                description TEXT,
                xp INTEGER NOT NULL CHECK (typeof(xp) = 'integer' AND xp BETWEEN 1 AND 1000),
                schedule_type TEXT NOT NULL CHECK (schedule_type IN ('Daily', 'Weekly')),
                weekdays TEXT,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sidequest_completions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sidequest_id INTEGER NOT NULL REFERENCES sidequests(id) ON DELETE RESTRICT,
                completed_date TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                xp_earned INTEGER NOT NULL CHECK (typeof(xp_earned) = 'integer' AND xp_earned BETWEEN 1 AND 1000),
                UNIQUE (sidequest_id, completed_date)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS sidequest_completions_date
            ON sidequest_completions (completed_date)
        """)


def scheduled(quest, day):
    return quest["active"] and (
        quest["schedule_type"] == "Daily"
        or WEEKDAYS[day.weekday()] in (quest["weekdays"] or [])
    )


def quests_for_date(conn, day, quest_id=None):
    query = """
        SELECT q.*, c.completed_at AS today_completed_at, c.xp_earned AS today_xp
        FROM sidequests q
        LEFT JOIN sidequest_completions c
          ON c.sidequest_id = q.id AND c.completed_date = ?
    """
    params = [day.isoformat()]
    if quest_id is not None:
        query += " WHERE q.id = ?"
        params.append(quest_id)
    query += " ORDER BY q.id"
    quests = []
    for row in conn.execute(query, params):
        quest = dict(row)
        quest["weekdays"] = json.loads(quest["weekdays"]) if quest["weekdays"] else None
        quest["active"] = bool(quest["active"])
        quest["completed_today"] = quest.pop("today_completed_at") is not None
        quest["earned_xp_today"] = quest.pop("today_xp") or 0
        quest["scheduled_today"] = bool(scheduled(quest, day))
        quests.append(quest)
    return quests


def require_quest(conn, day, quest_id):
    quests = quests_for_date(conn, day, quest_id)
    if not quests:
        raise HTTPException(404, "Side Quest not found")
    return quests[0]


def create_router(db_path: Callable[[], str]):
    router = APIRouter(prefix="/sidequests", tags=["Side Quests"])

    @router.get("")
    def list_quests():
        with database(db_path()) as conn:
            return quests_for_date(conn, local_now().date())

    @router.post("", status_code=201)
    def create_quest(quest: SideQuestCreate):
        with database(db_path(), write=True) as conn:
            now = local_now()
            cursor = conn.execute("""
                INSERT INTO sidequests (title, description, xp, schedule_type, weekdays, active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (quest.title, quest.description, quest.xp, quest.schedule_type,
                  json.dumps(quest.weekdays) if quest.weekdays is not None else None,
                  int(quest.active), now.isoformat(timespec="microseconds")))
            return require_quest(conn, now.date(), cursor.lastrowid)

    @router.get("/today")
    def today_quests():
        with database(db_path()) as conn:
            day = local_now().date()
            return {"date": day.isoformat(), "quests": [
                q for q in quests_for_date(conn, day) if q["scheduled_today"]
            ]}

    @router.get("/stats")
    def statistics():
        with database(db_path()) as conn:
            day = local_now().date()
            week_start = day - timedelta(days=day.weekday())
            week_end = week_start + timedelta(days=7)
            all_today = quests_for_date(conn, day)
            # Completed quests remain part of today's progress even if a later edit
            # deactivates or reschedules them. The actionable Today list stays separate.
            today = [q for q in all_today if q["scheduled_today"] or q["completed_today"]]
            # Sum in Python so aggregate totals cannot overflow SQLite's 64-bit SUM.
            today_xp = sum(row[0] for row in conn.execute("""
                SELECT xp_earned FROM sidequest_completions WHERE completed_date = ?
            """, (day.isoformat(),)))
            per_quest = [dict(row) for row in conn.execute("""
                SELECT q.id, q.title, COUNT(*) AS completions, SUM(c.xp_earned) AS xp
                FROM sidequest_completions c JOIN sidequests q ON q.id = c.sidequest_id
                WHERE c.completed_date >= ? AND c.completed_date < ? AND c.completed_date <= ?
                GROUP BY q.id, q.title ORDER BY completions DESC, q.id
            """, (week_start.isoformat(), week_end.isoformat(), day.isoformat()))]
            return {
                "timezone": "Asia/Hong_Kong",
                "today": {"date": day.isoformat(), "scheduled_count": len(today),
                          "completed_count": sum(q["completed_today"] for q in today), "xp": today_xp},
                "week": {"start_date": week_start.isoformat(),
                         "end_date": (week_end - timedelta(days=1)).isoformat(),
                         "completions": sum(q["completions"] for q in per_quest),
                         "xp": sum(q["xp"] for q in per_quest), "quests": per_quest},
            }

    @router.put("/{quest_id}")
    def edit_quest(quest_id: int, update: SideQuestUpdate):
        with database(db_path(), write=True) as conn:
            day = local_now().date()
            existing = require_quest(conn, day, quest_id)
            values = {key: existing[key] for key in SideQuestCreate.model_fields}
            values.update(update.model_dump(exclude_unset=True))
            try:
                quest = SideQuestCreate.model_validate(values)
            except ValidationError as error:
                raise HTTPException(422, detail=error.errors(include_context=False)) from error
            conn.execute("""
                UPDATE sidequests SET title = ?, description = ?, xp = ?, schedule_type = ?,
                                      weekdays = ?, active = ? WHERE id = ?
            """, (quest.title, quest.description, quest.xp, quest.schedule_type,
                  json.dumps(quest.weekdays) if quest.weekdays is not None else None,
                  int(quest.active), quest_id))
            return require_quest(conn, day, quest_id)

    @router.delete("/{quest_id}")
    def deactivate_quest(quest_id: int):
        with database(db_path(), write=True) as conn:
            day = local_now().date()
            require_quest(conn, day, quest_id)
            conn.execute("UPDATE sidequests SET active = 0 WHERE id = ?", (quest_id,))
            return require_quest(conn, day, quest_id)

    @router.post("/{quest_id}/complete")
    def complete_quest(quest_id: int):
        with database(db_path(), write=True) as conn:
            now = local_now()
            quest = require_quest(conn, now.date(), quest_id)
            if not quest["scheduled_today"]:
                raise HTTPException(409, "Side Quest is inactive or not scheduled today")
            conn.execute("""
                INSERT INTO sidequest_completions (sidequest_id, completed_date, completed_at, xp_earned)
                VALUES (?, ?, ?, ?) ON CONFLICT(sidequest_id, completed_date) DO NOTHING
            """, (quest_id, now.date().isoformat(), now.isoformat(timespec="microseconds"), quest["xp"]))
            return require_quest(conn, now.date(), quest_id)

    @router.delete("/{quest_id}/complete")
    def uncomplete_quest(quest_id: int):
        with database(db_path(), write=True) as conn:
            day = local_now().date()
            require_quest(conn, day, quest_id)
            # Undo is allowed after deactivation/rescheduling too, but only for today.
            conn.execute("DELETE FROM sidequest_completions WHERE sidequest_id = ? AND completed_date = ?",
                         (quest_id, day.isoformat()))
            return require_quest(conn, day, quest_id)

    return router
