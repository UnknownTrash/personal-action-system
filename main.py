from fastapi import FastAPI, HTTPException, Query
from pydantic import AfterValidator, BaseModel, field_validator
from typing import Annotated, Literal, Optional
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
import sidequests


@asynccontextmanager
async def lifespan(app):
    sidequests.initialize_database(DB_FILE)
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(sidequests.create_router(lambda: DB_FILE))

DB_FILE = "/opt/focus/tasks.db"
LOCAL_TIMEZONE = ZoneInfo("Asia/Hong_Kong")
Priority = Literal["High", "Medium", "Low"]
TaskStatus = Literal["Not Started", "In Progress", "Done"]
EstimatedTime = Literal["10 min", "20 min", "30 min", "1 hour", "2 hours+"]
DEADLINE_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?)?"
)


def parse_deadline(value):
    """Interpret date-only values at 23:59:59.999999 Hong Kong time.

    Naive date-times are local time. Stored strings are never rewritten.
    """
    if not value:
        return None
    try:
        deadline = datetime.fromisoformat(value)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            deadline = deadline.replace(hour=23, minute=59, second=59, microsecond=999999)
        if deadline.tzinfo is None:
            return deadline.replace(tzinfo=LOCAL_TIMEZONE)
        return deadline.astimezone(LOCAL_TIMEZONE)
    except (TypeError, ValueError, OverflowError):
        # Legacy invalid deadlines must not prevent reading the remaining tasks.
        return None


def validate_deadline(value):
    if not DEADLINE_PATTERN.fullmatch(value) or parse_deadline(value) is None:
        raise ValueError(
            "Deadline must be an ISO date (YYYY-MM-DD) or date-time "
            "(YYYY-MM-DDTHH:MM[:SS[.ffffff]], optionally Z or +/-HH:MM); "
            "use null to clear it"
        )
    return value


Deadline = Annotated[str, AfterValidator(validate_deadline)]


def deadline_sort_key(task):
    deadline = parse_deadline(task["deadline"])
    return (
        deadline is None,
        deadline or datetime.max.replace(tzinfo=LOCAL_TIMEZONE),
        task["id"],
    )


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    deadline: Optional[Deadline] = None
    priority: Priority = "Medium"
    category: Optional[str] = None
    estimated_time: Optional[EstimatedTime] = None
    is_today: bool = False


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    deadline: Optional[Deadline] = None
    priority: Optional[Priority] = None
    category: Optional[str] = None
    estimated_time: Optional[EstimatedTime] = None
    status: Optional[TaskStatus] = None
    is_today: Optional[bool] = None

    @field_validator("title", "is_today", "priority", "status")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("Omit this field to keep its value; null is not allowed")
        return value


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def row_to_task(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "deadline": row["deadline"],
        "priority": row["priority"],
        "category": row["category"],
        "estimated_time": row["estimated_time"],
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "is_today": bool(row["is_today"]),
    }


@app.get("/tasks")
def get_tasks(
    status: Optional[TaskStatus] = Query(None),
    category: Optional[str] = Query(None),
    sort: str = Query("deadline")
):
    conn = get_db()

    query = """
        SELECT id, title, description, deadline, priority,
               category, estimated_time, status, created_at,
               started_at, completed_at, is_today
        FROM tasks
        WHERE 1=1
    """
    params = []

    if status:
        query += " AND status = ?"
        params.append(status)

    if category:
        query += " AND category = ?"
        params.append(category)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    tasks = [row_to_task(row) for row in rows]
    if sort == "priority":
        priority_order = {"High": 1, "Medium": 2, "Low": 3}
        tasks.sort(key=lambda task: (
            priority_order.get(task["priority"], 4), deadline_sort_key(task)
        ))
    else:
        tasks.sort(key=deadline_sort_key)
    return tasks


@app.post("/tasks")
def add_task(task: TaskCreate):
    conn = get_db()

    now = datetime.now(LOCAL_TIMEZONE).isoformat(timespec="seconds")

    cursor = conn.execute("""
        INSERT INTO tasks (
            title,
            description,
            deadline,
            priority,
            category,
            estimated_time,
            status,
            created_at,
            started_at,
            completed_at,
            is_today
        )
        VALUES (?, ?, ?, ?, ?, ?, 'Not Started', ?, NULL, NULL, ?)
    """, (
        task.title,
        task.description,
        task.deadline,
        task.priority,
        task.category,
        task.estimated_time,
        now,
        int(task.is_today)
    ))

    task_id = cursor.lastrowid
    conn.commit()

    row = conn.execute("""
        SELECT id, title, description, deadline, priority,
               category, estimated_time, status, created_at,
               started_at, completed_at, is_today
        FROM tasks
        WHERE id = ?
    """, (task_id,)).fetchone()

    conn.close()

    return row_to_task(row)


@app.put("/tasks/{task_id}")
def update_task(task_id: int, update: TaskUpdate):
    conn = get_db()

    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ?",
        (task_id,)
    ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Task not found")

    values = dict(row)

    data = update.model_dump(exclude_unset=True)

    for key, value in data.items():
        if key in values:
            values[key] = int(value) if key == "is_today" else value

    if data.get("status") == "In Progress" and not values.get("started_at"):
        values["started_at"] = datetime.now(LOCAL_TIMEZONE).isoformat(timespec="seconds")

    if data.get("status") == "Done":
        values["completed_at"] = datetime.now(LOCAL_TIMEZONE).isoformat(timespec="seconds")

    conn.execute("""
        UPDATE tasks
        SET title = ?,
            description = ?,
            deadline = ?,
            priority = ?,
            category = ?,
            estimated_time = ?,
            status = ?,
            started_at = ?,
            completed_at = ?,
            is_today = ?
        WHERE id = ?
    """, (
        values["title"],
        values["description"],
        values["deadline"],
        values["priority"],
        values["category"],
        values["estimated_time"],
        values["status"],
        values["started_at"],
        values["completed_at"],
        values["is_today"],
        task_id
    ))

    conn.commit()

    updated = conn.execute("""
        SELECT id, title, description, deadline, priority,
               category, estimated_time, status, created_at,
               started_at, completed_at, is_today
        FROM tasks
        WHERE id = ?
    """, (task_id,)).fetchone()

    conn.close()

    return row_to_task(updated)


@app.delete("/tasks/{task_id}")
def delete_task(task_id: int):
    conn = get_db()

    cursor = conn.execute(
        "DELETE FROM tasks WHERE id = ?",
        (task_id,)
    )

    conn.commit()
    conn.close()

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Task not found")

    return {"message": "Task deleted"}


@app.get("/tasks/today")
def get_today_tasks():
    conn = get_db()

    rows = conn.execute("""
        SELECT id, title, description, deadline, priority,
               category, estimated_time, status, created_at,
               started_at, completed_at, is_today
        FROM tasks
        WHERE status != 'Done'
    """).fetchall()

    conn.close()

    now = datetime.now(LOCAL_TIMEZONE)
    today = now.date()
    tomorrow = today + timedelta(days=1)

    result = []

    for row in rows:
        task = row_to_task(row)
        deadline = parse_deadline(task["deadline"])

        include = False

        if task["is_today"]:
            include = True

        if deadline:
            if deadline.date() <= today:
                include = True

            if (
                task["priority"] == "High"
                and deadline.date() <= tomorrow
            ):
                include = True

        if include:
            result.append(task)

    result.sort(key=deadline_sort_key)

    return result


@app.get("/tasks/completed")
def get_completed_tasks():
    conn = get_db()

    rows = conn.execute("""
        SELECT id, title, description, deadline, priority,
               category, estimated_time, status, created_at,
               started_at, completed_at, is_today
        FROM tasks
        WHERE status = 'Done'
        ORDER BY completed_at DESC
    """).fetchall()

    conn.close()

    return [row_to_task(row) for row in rows]


@app.get("/recommendation")
def get_recommendation(
    exclude_id: Optional[int] = Query(None)
):
    conn = get_db()

    rows = conn.execute("""
        SELECT id, title, description, deadline, priority,
               category, estimated_time, status, created_at,
               started_at, completed_at, is_today
        FROM tasks
        WHERE status != 'Done'
    """).fetchall()

    conn.close()

    now = datetime.now(LOCAL_TIMEZONE)
    today = now.date()
    tomorrow = today + timedelta(days=1)

    scored = []

    for row in rows:
        task = row_to_task(row)

        if exclude_id is not None and task["id"] == exclude_id:
            continue

        score = 0
        deadline = parse_deadline(task["deadline"])

        if deadline:
            if deadline < now:
                score += 100
            elif deadline.date() == today:
                score += 80
            elif deadline.date() == tomorrow:
                score += 60
            else:
                days = (deadline.date() - today).days
                score += max(0, 30 - days)

        if task["priority"] == "High":
            score += 40
        elif task["priority"] == "Medium":
            score += 20
        elif task["priority"] == "Low":
            score += 5

        if task["status"] == "In Progress":
            score += 15

        if task["is_today"]:
            score += 25

        time_bonus = {
            "10 min": 8,
            "20 min": 6,
            "30 min": 4,
            "1 hour": 2,
            "2 hours+": 0
        }

        score += time_bonus.get(task["estimated_time"], 0)
        scored.append((score, task))

    if not scored:
        return {"task": None}

    scored.sort(key=lambda x: x[0], reverse=True)

    return {
        "score": scored[0][0],
        "task": scored[0][1]
    }
