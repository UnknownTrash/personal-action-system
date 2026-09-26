from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
import sqlite3
from datetime import datetime, timedelta

app = FastAPI()

DB_FILE = "/opt/focus/tasks.db"


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    deadline: Optional[str] = None
    priority: str = "Medium"
    category: Optional[str] = None
    estimated_time: Optional[str] = None
    is_today: bool = False


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    deadline: Optional[str] = None
    priority: Optional[str] = None
    category: Optional[str] = None
    estimated_time: Optional[str] = None
    status: Optional[str] = None
    is_today: Optional[bool] = None


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


def parse_deadline(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@app.get("/tasks")
def get_tasks(
    status: Optional[str] = Query(None),
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

    if sort == "priority":
        query += """
            ORDER BY
                CASE priority
                    WHEN 'High' THEN 1
                    WHEN 'Medium' THEN 2
                    WHEN 'Low' THEN 3
                    ELSE 4
                END,
                deadline ASC
        """
    else:
        query += """
            ORDER BY
                CASE WHEN deadline IS NULL OR deadline = '' THEN 1 ELSE 0 END,
                deadline ASC,
                id ASC
        """

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return [row_to_task(row) for row in rows]


@app.post("/tasks")
def add_task(task: TaskCreate):
    conn = get_db()

    now = datetime.now().isoformat(timespec="seconds")

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
        values["started_at"] = datetime.now().isoformat(timespec="seconds")

    if data.get("status") == "Done":
        values["completed_at"] = datetime.now().isoformat(timespec="seconds")

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

    now = datetime.now()
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

    result.sort(
        key=lambda t: (
            parse_deadline(t["deadline"]) is None,
            parse_deadline(t["deadline"]) or datetime.max
        )
    )

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

    now = datetime.now()
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
