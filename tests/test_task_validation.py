"""Run with: venv/bin/python -B -m unittest discover -s tests -v.

Only standard-library test tools are needed. Every test uses a temporary SQLite
database; the deployed service and real task data are never used for requests.
"""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import main


SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    deadline TEXT,
    description TEXT,
    priority TEXT DEFAULT 'Medium',
    category TEXT,
    estimated_time TEXT,
    status TEXT DEFAULT 'Not Started',
    created_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    is_today INTEGER DEFAULT 0
)
"""


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        # Hong Kong: September 28, 00:30; UTC: September 27, 16:30.
        instant = datetime(2026, 9, 27, 16, 30, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


class TaskValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='task-validation-')
        self.addCleanup(directory.cleanup)
        self.database = str(Path(directory.name) / 'tasks.db')
        with sqlite3.connect(self.database) as conn:
            conn.execute(SCHEMA)
        database_patch = patch.object(main, 'DB_FILE', self.database)
        database_patch.start()
        self.addCleanup(database_patch.stop)

    async def request(self, method, url, data=None):
        parsed = urlsplit(url)
        body = json.dumps(data).encode() if data is not None else b''
        messages = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await main.app({
            'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
            'http_version': '1.1', 'method': method, 'scheme': 'http',
            'path': parsed.path, 'raw_path': parsed.path.encode(),
            'query_string': parsed.query.encode(), 'root_path': '',
            'headers': [(b'content-type', b'application/json')],
            'client': ('127.0.0.1', 1234), 'server': ('test', 80),
        }, receive, send)
        status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
        response = b''.join(m.get('body', b'') for m in messages
                            if m['type'] == 'http.response.body')
        return status, json.loads(response)

    def seed(self, deadline=None, priority='Medium', status='Not Started', is_today=False):
        with sqlite3.connect(self.database) as conn:
            cursor = conn.execute(
                'INSERT INTO tasks (title, deadline, priority, status, is_today) VALUES (?, ?, ?, ?, ?)',
                ('Stored task', deadline, priority, status, int(is_today)))
            return cursor.lastrowid

    def snapshot(self):
        with sqlite3.connect(self.database) as conn:
            return conn.execute('SELECT * FROM tasks ORDER BY id').fetchall()

    async def test_existing_routes_are_preserved(self):
        routes = {(method, route.path) for route in main.app.routes
                  for method in getattr(route, 'methods', [])}
        expected = {('GET', '/tasks'), ('POST', '/tasks'),
                    ('PUT', '/tasks/{task_id}'), ('DELETE', '/tasks/{task_id}'),
                    ('GET', '/tasks/today'), ('GET', '/tasks/completed'),
                    ('GET', '/recommendation')}
        self.assertTrue(expected <= routes)

    async def test_all_valid_priorities_on_create_and_update(self):
        for value in ('High', 'Medium', 'Low'):
            with self.subTest(value=value):
                status, task = await self.request('POST', '/tasks', {'title': 'Test', 'priority': value})
                self.assertEqual(status, 200)
                self.assertEqual(task['priority'], value)
                status, updated = await self.request('PUT', f"/tasks/{task['id']}", {'priority': value})
                self.assertEqual((status, updated['priority']), (200, value))

    async def test_all_valid_estimates_on_create_and_update(self):
        for value in ('10 min', '20 min', '30 min', '1 hour', '2 hours+', None):
            with self.subTest(value=value):
                status, task = await self.request('POST', '/tasks', {'title': 'Test', 'estimated_time': value})
                self.assertEqual((status, task['estimated_time']), (200, value))
                status, updated = await self.request('PUT', f"/tasks/{task['id']}", {'estimated_time': value})
                self.assertEqual((status, updated['estimated_time']), (200, value))

    async def test_status_transitions_and_filter(self):
        task_id = self.seed()
        for value in ('Not Started', 'In Progress', 'Done'):
            with self.subTest(value=value):
                status, task = await self.request('PUT', f'/tasks/{task_id}', {'status': value})
                self.assertEqual((status, task['status']), (200, value))
                status, tasks = await self.request('GET', '/tasks?status=' + value.replace(' ', '%20'))
                self.assertEqual(status, 200)
                self.assertEqual([task['id'] for task in tasks], [task_id])
        self.assertIsNotNone(task['started_at'])
        self.assertIsNotNone(task['completed_at'])
        self.assertEqual((await self.request('GET', '/tasks/completed'))[1][0]['id'], task_id)

    async def test_invalid_enum_values_return_422_without_writing(self):
        task_id = self.seed()
        before = self.snapshot()
        invalid = {'priority': ('high', 'Urgent', '', 1, None),
                   'estimated_time': ('15 min', '1 Hour', '', 10),
                   'status': ('done', 'Completed', '', 1, None)}
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    methods = [('PUT', f'/tasks/{task_id}')]
                    if field != 'status':
                        methods.append(('POST', '/tasks'))
                    for method, url in methods:
                        status, body = await self.request(method, url, {'title': 'Test', field: value})
                        self.assertEqual(status, 422)
                        self.assertTrue(any(error['loc'][-1] == field for error in body['detail']))
        self.assertEqual(self.snapshot(), before)

    async def test_status_filter_rejects_invalid_values(self):
        for value in ('done', 'Completed', '', 'null'):
            self.assertEqual((await self.request('GET', '/tasks?status=' + value))[0], 422)

    async def test_deadline_formats_round_trip_without_rewriting(self):
        for value in (None, '2026-09-28', '2026-09-28T01:00',
                      '2026-09-28T01:00:00', '2026-09-28 01:00:00',
                      '2026-09-28T01:00:00.123456', '2026-09-27T17:00:00Z',
                      '2026-09-28T01:00:00+08:00', '2026-09-27T13:00:00-04:00'):
            with self.subTest(value=value):
                status, task = await self.request('POST', '/tasks', {'title': 'Test', 'deadline': value})
                self.assertEqual(status, 200)
                self.assertEqual(task['deadline'], value)
                status, task = await self.request('PUT', f"/tasks/{task['id']}", {'deadline': value})
                self.assertEqual((status, task['deadline']), (200, value))

    async def test_bad_deadlines_return_422_without_writing(self):
        task_id = self.seed()
        before = self.snapshot()
        for value in ('tomorrow', '', '2026-02-30', '2026-13-01',
                      '2026-09-28T24:00', '2026-09-28T01:00:00+08:99',
                      '2026-09-28T01:00:00+24:00', '2026-09-28X01:00',
                      '2026-09-28T01:00:00garbage', 1770000000, True, {}):
            with self.subTest(value=value):
                for method, url in [('POST', '/tasks'), ('PUT', f'/tasks/{task_id}')]:
                    status, errors = await self.request(method, url, {'title': 'Test', 'deadline': value})
                    self.assertEqual(status, 422)
                    self.assertEqual(errors['detail'][0]['loc'][-1], 'deadline')
        self.assertEqual(self.snapshot(), before)

    async def test_omitted_update_fields_are_preserved_and_nullable_fields_can_clear(self):
        task_id = self.seed('2026-09-28T01:00:00', priority='High', status='In Progress')
        _, task = await self.request('PUT', f'/tasks/{task_id}', {'description': 'Changed'})
        self.assertEqual((task['deadline'], task['priority'], task['status']),
                         ('2026-09-28T01:00:00', 'High', 'In Progress'))
        status, task = await self.request('PUT', f'/tasks/{task_id}', {'deadline': None, 'estimated_time': None})
        self.assertEqual(status, 200)
        self.assertIsNone(task['deadline'])
        self.assertIsNone(task['estimated_time'])

    async def test_naive_and_offset_deadlines_represent_same_instant(self):
        values = ('2026-09-28T01:00:00', '2026-09-27T17:00:00Z',
                  '2026-09-28T01:00:00+08:00', '2026-09-27T13:00:00-04:00')
        parsed = [main.parse_deadline(value) for value in values]
        self.assertTrue(all(value == parsed[0] for value in parsed))
        self.assertTrue(all(value.tzinfo.key == 'Asia/Hong_Kong' for value in parsed))
        self.assertEqual(main.parse_deadline('2026-09-28').isoformat(),
                         '2026-09-28T23:59:59.999999+08:00')

    async def test_date_only_deadline_is_due_today_until_local_day_ends(self):
        status, task = await self.request('POST', '/tasks',
                                          {'title': 'All day', 'deadline': '2026-09-28'})
        self.assertEqual(status, 200)
        self.assertEqual(task['deadline'], '2026-09-28')
        before = self.snapshot()
        for now, expected_score in (
            (datetime(2026, 9, 28, 0, 0, tzinfo=main.LOCAL_TIMEZONE), 100),
            (datetime(2026, 9, 28, 12, 0, tzinfo=main.LOCAL_TIMEZONE), 100),
            (datetime(2026, 9, 28, 23, 59, 59, 999999, tzinfo=main.LOCAL_TIMEZONE), 100),
            (datetime(2026, 9, 29, 0, 0, tzinfo=main.LOCAL_TIMEZONE), 120),
        ):
            with self.subTest(now=now):
                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return now.astimezone(tz) if tz else now.replace(tzinfo=None)

                with patch.object(main, 'datetime', Clock):
                    status, recommendation = await self.request('GET', '/recommendation')
                    self.assertEqual((status, recommendation['score']), (200, expected_score))
                    status, today = await self.request('GET', '/tasks/today')
                    self.assertEqual(status, 200)
                    self.assertEqual([item['id'] for item in today], [task['id']])
        self.assertEqual(self.snapshot(), before)

    async def test_date_only_sorts_after_explicit_times_on_same_local_day(self):
        all_day = self.seed('2026-09-28', is_today=True)
        last_second = self.seed('2026-09-28T23:59:59', is_today=True)
        midnight = self.seed('2026-09-28T00:00:00', is_today=True)
        with patch.object(main, 'datetime', FrozenDatetime):
            for url in ('/tasks', '/tasks?sort=priority', '/tasks/today'):
                status, tasks = await self.request('GET', url)
                self.assertEqual(status, 200)
                self.assertEqual([task['id'] for task in tasks], [midnight, last_second, all_day])

    async def test_null_title_and_today_updates_return_422_without_writing(self):
        task_id = self.seed(is_today=True)
        before = self.snapshot()
        for field in ('title', 'is_today'):
            with self.subTest(field=field):
                status, body = await self.request('PUT', f'/tasks/{task_id}', {field: None})
                self.assertEqual(status, 422)
                self.assertTrue(any(error['loc'][-1] == field for error in body['detail']))
                self.assertEqual(self.snapshot(), before)
        status, task = await self.request('PUT', f'/tasks/{task_id}', {})
        self.assertEqual(status, 200)
        self.assertEqual(task['title'], 'Stored task')
        self.assertTrue(task['is_today'])
        self.assertEqual(self.snapshot(), before)
        status, task = await self.request('PUT', f'/tasks/{task_id}',
                                          {'title': 'Updated title', 'is_today': False})
        self.assertEqual((status, task['title'], task['is_today']), (200, 'Updated title', False))

    async def test_all_nullable_fields_remain_clearable(self):
        values = {'deadline': '2026-09-28', 'description': 'Description',
                  'category': 'Work', 'estimated_time': '20 min'}
        status, task = await self.request('POST', '/tasks', {'title': 'Nullable', **values})
        self.assertEqual(status, 200)
        task_id = task['id']
        status, task = await self.request('PUT', f'/tasks/{task_id}', {'is_today': True})
        self.assertEqual(status, 200)
        for field, value in values.items():
            self.assertEqual(task[field], value)
        status, task = await self.request('PUT', f'/tasks/{task_id}',
                                          dict.fromkeys(values))
        self.assertEqual(status, 200)
        for field in values:
            self.assertIsNone(task[field])

    async def test_new_application_timestamps_have_explicit_hong_kong_offset(self):
        expected = '2026-09-28T00:30:00+08:00'
        with patch.object(main, 'datetime', FrozenDatetime):
            status, task = await self.request('POST', '/tasks', {'title': 'Timestamps'})
            self.assertEqual(status, 200)
            self.assertEqual(task['created_at'], expected)
            self.assertIsNone(task['started_at'])
            self.assertIsNone(task['completed_at'])
            task_id = task['id']
            status, task = await self.request('PUT', f'/tasks/{task_id}', {'status': 'In Progress'})
            self.assertEqual(status, 200)
            self.assertEqual(task['started_at'], expected)
            status, task = await self.request('PUT', f'/tasks/{task_id}', {'status': 'Done'})
            self.assertEqual(status, 200)
            for field in ('created_at', 'started_at', 'completed_at'):
                self.assertEqual(task[field], expected)
        with sqlite3.connect(self.database) as conn:
            stored = conn.execute('SELECT created_at, started_at, completed_at FROM tasks WHERE id = ?',
                                  (task_id,)).fetchone()
        self.assertEqual(stored, (expected, expected, expected))

    async def test_existing_timestamps_are_preserved_on_reads_and_unrelated_updates(self):
        task_id = self.seed(status='Done')
        timestamps = ('2026-09-13T10:00:00', '2026-09-13T11:00:00', '2026-09-13T12:00:00')
        with sqlite3.connect(self.database) as conn:
            conn.execute('UPDATE tasks SET created_at = ?, started_at = ?, completed_at = ? WHERE id = ?',
                         (*timestamps, task_id))
        before = self.snapshot()
        for url in ('/tasks', '/tasks/completed'):
            status, tasks = await self.request('GET', url)
            self.assertEqual(status, 200)
            self.assertEqual(tuple(tasks[0][field] for field in
                                   ('created_at', 'started_at', 'completed_at')), timestamps)
        self.assertEqual(self.snapshot(), before)
        for update in ({}, {'description': 'Updated'}, {'status': 'In Progress'}):
            status, task = await self.request('PUT', f'/tasks/{task_id}', update)
            self.assertEqual(status, 200)
            self.assertEqual(tuple(task[field] for field in
                                   ('created_at', 'started_at', 'completed_at')), timestamps)

    async def test_list_sorting_is_chronological_with_mixed_timezones(self):
        late = self.seed('2026-09-28T00:15:00+09:00')
        early = self.seed('2026-09-27T14:00:00Z')
        middle = self.seed('2026-09-27T23:00:00')
        no_deadline = self.seed()
        invalid = self.seed('legacy invalid')
        for url in ('/tasks', '/tasks?sort=priority'):
            status, tasks = await self.request('GET', url)
            self.assertEqual(status, 200)
            self.assertEqual([task['id'] for task in tasks], [early, middle, late, no_deadline, invalid])

    async def test_priority_sort_keeps_priority_before_deadline(self):
        low = self.seed('2026-09-01T01:00:00', priority='Low')
        high = self.seed('2026-10-01T01:00:00', priority='High')
        medium = self.seed('2026-09-28T01:00:00')
        _, tasks = await self.request('GET', '/tasks?sort=priority')
        self.assertEqual([task['id'] for task in tasks], [high, medium, low])

    async def test_today_uses_hong_kong_calendar_and_sorts_mixed_dates(self):
        overdue = self.seed('2026-09-27T23:00:00')
        today = self.seed('2026-09-27T17:00:00Z')
        tomorrow_high = self.seed('2026-09-28T18:00:00Z', priority='High')
        self.seed('2026-09-28T18:00:00Z')  # Tomorrow, ordinary priority.
        self.seed('2026-09-29T18:00:00Z', priority='High')  # Two local days away.
        self.seed('2026-09-27T23:00:00', status='Done')
        flagged = self.seed(is_today=True)
        with patch.object(main, 'datetime', FrozenDatetime):
            status, tasks = await self.request('GET', '/tasks/today')
        self.assertEqual(status, 200)
        self.assertEqual([task['id'] for task in tasks], [overdue, today, tomorrow_high, flagged])

    async def test_recommendation_handles_overdue_local_and_aware_deadlines(self):
        overdue = self.seed('2026-09-27T23:00:00')
        upcoming = self.seed('2026-09-27T17:00:00Z')
        with patch.object(main, 'datetime', FrozenDatetime):
            status, recommendation = await self.request('GET', '/recommendation')
            self.assertEqual((status, recommendation['task']['id'], recommendation['score']), (200, overdue, 120))
            _, recommendation = await self.request('GET', f'/recommendation?exclude_id={overdue}')
            self.assertEqual((recommendation['task']['id'], recommendation['score']), (upcoming, 100))

    async def test_recommendation_scores_naive_local_deadline_as_today(self):
        self.seed('2026-09-28T01:00:00')
        with patch.object(main, 'datetime', FrozenDatetime):
            status, recommendation = await self.request('GET', '/recommendation')
        self.assertEqual((status, recommendation['score']), (200, 100))

    async def test_legacy_deadlines_are_readable_and_not_rewritten(self):
        for value in (None, '', 'legacy invalid', '2026-09-14T02:54:00',
                      '2026-09-14T21:01:00', '2026-09-22T03:15:00',
                      '2026-09-27T17:00:00Z'):
            self.seed(value, is_today=True)
        before = self.snapshot()
        with patch.object(main, 'datetime', FrozenDatetime):
            for url in ('/tasks', '/tasks?sort=priority', '/tasks/today', '/recommendation'):
                self.assertEqual((await self.request('GET', url))[0], 200)
        self.assertEqual(self.snapshot(), before)

    async def test_defaults_delete_and_not_found_remain_compatible(self):
        status, task = await self.request('POST', '/tasks', {'title': 'Default task'})
        self.assertEqual((status, task['priority'], task['status']), (200, 'Medium', 'Not Started'))
        task_id = task['id']
        self.assertEqual((await self.request('DELETE', f'/tasks/{task_id}'))[0], 200)
        self.assertEqual((await self.request('DELETE', f'/tasks/{task_id}'))[0], 404)
        self.assertEqual((await self.request('PUT', f'/tasks/{task_id}', {'priority': 'High'}))[0], 404)
        self.assertEqual((await self.request('GET', '/recommendation'))[1], {'task': None})


if __name__ == '__main__':
    unittest.main()
