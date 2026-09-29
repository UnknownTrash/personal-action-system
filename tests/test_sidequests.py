"""Side Quest API regressions; every request uses a disposable SQLite database."""
import asyncio
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import main
import sidequests
import test_task_validation as todo_tests


class SideQuestTests(unittest.IsolatedAsyncioTestCase):
    request = todo_tests.TaskValidationTests.request

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='sidequests-test-')
        self.addCleanup(directory.cleanup)
        self.database = str(Path(directory.name) / 'tasks.db')
        with sqlite3.connect(self.database) as conn:
            conn.execute(todo_tests.SCHEMA)
            conn.execute("INSERT INTO tasks (title, deadline) VALUES ('Existing Todo', '2026-09-28')")
        db_patch = patch.object(main, 'DB_FILE', self.database)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        clock = patch.object(sidequests, 'local_now', return_value=self.at('2026-09-28T12:00:00+08:00'))
        clock.start()
        self.addCleanup(clock.stop)
        sidequests.initialize_database(self.database)

    @staticmethod
    def at(value):
        return datetime.fromisoformat(value).astimezone(sidequests.LOCAL_TIMEZONE)

    async def create(self, **values):
        status, quest = await self.request('POST', '/sidequests',
                                          {'title': 'Cook', 'xp': 10, 'schedule_type': 'Daily', **values})
        self.assertEqual(status, 201, quest)
        return quest

    async def complete(self, quest, instant=None):
        if instant:
            with patch.object(sidequests, 'local_now', return_value=self.at(instant)):
                return await self.request('POST', f"/sidequests/{quest['id']}/complete")
        return await self.request('POST', f"/sidequests/{quest['id']}/complete")

    def rows(self, table):
        self.assertIn(table, ('tasks', 'sidequests', 'sidequest_completions'))
        with sqlite3.connect(self.database) as conn:
            return conn.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()

    async def test_create_daily_and_weekly(self):
        daily = await self.create(title='  Cook a proper meal  ')
        self.assertEqual(daily['title'], 'Cook a proper meal')
        self.assertIsNone(daily['weekdays'])
        self.assertTrue(daily['active'])
        self.assertTrue(daily['scheduled_today'])
        self.assertFalse(daily['completed_today'])
        self.assertTrue(daily['created_at'].endswith('+08:00'))
        weekly = await self.create(title='Gym', xp=20, schedule_type='Weekly',
                                   weekdays=['Fri', 'Mon', 'Mon', 'Thu'])
        self.assertEqual(weekly['weekdays'], ['Mon', 'Thu', 'Fri'])
        with sqlite3.connect(self.database) as conn:
            text = conn.execute('SELECT weekdays FROM sidequests WHERE id = ?', (weekly['id'],)).fetchone()[0]
        self.assertEqual(text, '["Mon", "Thu", "Fri"]')

    async def test_reject_invalid_fields_and_weekdays(self):
        invalid = [
            {'title': ''}, {'title': '   '}, {'title': None},
            {'xp': 0}, {'xp': -1}, {'xp': 1001}, {'xp': 10**100},
            {'xp': True}, {'xp': 1.5}, {'xp': '20'}, {'xp': None},
            {'schedule_type': 'Monthly'},
            {'weekdays': ['Monday']}, {'weekdays': ['mon']}, {'weekdays': ['Mon', 'Bad']},
            {'weekdays': 'Mon'}, {'weekdays': [1]},
            {'schedule_type': 'Weekly'}, {'schedule_type': 'Weekly', 'weekdays': []},
            {'schedule_type': 'Weekly', 'weekdays': None}, {'active': None}, {'active': 'false'},
        ]
        for values in invalid:
            with self.subTest(values=values):
                status, _ = await self.request('POST', '/sidequests',
                    {'title': 'Test', 'xp': 10, 'schedule_type': 'Daily', **values})
                self.assertEqual(status, 422)
        self.assertEqual(self.rows('sidequests'), [])

    async def test_xp_boundaries_apply_to_create_and_edit(self):
        one = await self.create(title='One XP', xp=1)
        maximum = await self.create(title='Maximum XP', xp=1000)
        self.assertEqual((one['xp'], maximum['xp']), (1, 1000))
        for value in (1, 1000):
            status, edited = await self.request('PUT', f"/sidequests/{one['id']}", {'xp': value})
            self.assertEqual((status, edited['xp']), (200, value))
        before = self.rows('sidequests')
        for value in (0, -1, 1001, 10**100):
            with self.subTest(value=value):
                self.assertEqual((await self.request('PUT', f"/sidequests/{one['id']}", {'xp': value}))[0], 422)
                self.assertEqual(self.rows('sidequests'), before)

    async def test_today_only_includes_active_scheduled_quests(self):
        daily = await self.create()
        weekly = await self.create(title='Gym', schedule_type='Weekly', weekdays=['Mon'])
        await self.create(title='Tomorrow', schedule_type='Weekly', weekdays=['Tue'])
        await self.create(title='Inactive', active=False)
        status, today = await self.request('GET', '/sidequests/today')
        self.assertEqual(status, 200)
        self.assertEqual(today['date'], '2026-09-28')
        self.assertEqual([q['id'] for q in today['quests']], [daily['id'], weekly['id']])
        with patch.object(sidequests, 'local_now', return_value=self.at('2026-09-29T12:00:00+08:00')):
            _, tomorrow = await self.request('GET', '/sidequests/today')
        self.assertNotIn(weekly['id'], [q['id'] for q in tomorrow['quests']])
        self.assertIn(daily['id'], [q['id'] for q in tomorrow['quests']])

    async def test_complete_duplicate_and_database_unique_constraint(self):
        quest = await self.create(xp=20)
        for _ in range(2):
            status, result = await self.complete(quest)
            self.assertEqual(status, 200)
            self.assertTrue(result['completed_today'])
            self.assertEqual(result['earned_xp_today'], 20)
        rows = self.rows('sidequest_completions')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], '2026-09-28')
        self.assertTrue(rows[0][3].endswith('+08:00'))
        with sqlite3.connect(self.database) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('INSERT INTO sidequest_completions (sidequest_id, completed_date, completed_at, xp_earned) VALUES (?, ?, ?, ?)',
                             (quest['id'], rows[0][2], rows[0][3], 20))
        _, stats = await self.request('GET', '/sidequests/stats')
        self.assertEqual(stats['today'], {'date': '2026-09-28', 'scheduled_count': 1, 'completed_count': 1, 'xp': 20})

    async def test_concurrent_completion_awards_xp_once(self):
        quest = await self.create(xp=20)
        results = await asyncio.gather(self.complete(quest), self.complete(quest))
        self.assertEqual([result[0] for result in results], [200, 200])
        self.assertEqual(len(self.rows('sidequest_completions')), 1)
        _, stats = await self.request('GET', '/sidequests/stats')
        self.assertEqual(stats['week']['xp'], 20)

    async def test_cannot_complete_inactive_or_unscheduled_quest(self):
        for values in ({'active': False}, {'schedule_type': 'Weekly', 'weekdays': ['Tue']}):
            quest = await self.create(**values)
            self.assertEqual((await self.complete(quest))[0], 409)
        self.assertEqual(self.rows('sidequest_completions'), [])

    async def test_undo_only_today_and_can_complete_again_without_double_xp(self):
        quest = await self.create()
        await self.complete(quest, '2026-09-27T12:00:00+08:00')
        history = self.rows('sidequest_completions')
        await self.complete(quest)
        for _ in range(2):
            status, result = await self.request('DELETE', f"/sidequests/{quest['id']}/complete")
            self.assertEqual(status, 200)
            self.assertFalse(result['completed_today'])
        self.assertEqual(self.rows('sidequest_completions'), history)
        _, stats = await self.request('GET', '/sidequests/stats')
        self.assertEqual((stats['today']['completed_count'], stats['today']['xp']), (0, 0))
        await self.complete(quest)
        _, stats = await self.request('GET', '/sidequests/stats')
        self.assertEqual((stats['week']['completions'], stats['week']['xp']), (1, 10))

    async def test_week_statistics_and_per_quest_counts(self):
        gym = await self.create(title='Gym', xp=20, schedule_type='Weekly', weekdays=['Mon', 'Tue'])
        cook = await self.create()
        await self.complete(cook, '2026-09-27T12:00:00+08:00')  # Previous week.
        await self.complete(gym)
        await self.complete(cook)
        with patch.object(sidequests, 'local_now', return_value=self.at('2026-09-29T12:00:00+08:00')):
            await self.complete(gym)
            _, stats = await self.request('GET', '/sidequests/stats')
        self.assertEqual(stats['timezone'], 'Asia/Hong_Kong')
        self.assertEqual((stats['today']['scheduled_count'], stats['today']['completed_count'], stats['today']['xp']), (2, 1, 20))
        self.assertEqual((stats['week']['start_date'], stats['week']['end_date']), ('2026-09-28', '2026-10-04'))
        self.assertEqual((stats['week']['completions'], stats['week']['xp']), (3, 50))
        self.assertEqual([(q['title'], q['completions'], q['xp']) for q in stats['week']['quests']],
                         [('Gym', 2, 40), ('Cook', 1, 10)])

    async def test_edit_and_deactivate_preserve_history_and_earned_xp(self):
        quest = await self.create()
        await self.complete(quest)
        history = self.rows('sidequest_completions')
        status, edited = await self.request('PUT', f"/sidequests/{quest['id']}",
            {'title': 'New title', 'description': 'New description', 'xp': 50,
             'schedule_type': 'Weekly', 'weekdays': ['Tue'], 'active': False})
        self.assertEqual(status, 200)
        self.assertEqual(edited['created_at'], quest['created_at'])
        self.assertEqual(self.rows('sidequest_completions'), history)
        _, stats = await self.request('GET', '/sidequests/stats')
        self.assertEqual((stats['week']['completions'], stats['week']['xp'], stats['today']['xp']), (1, 10, 10))
        self.assertEqual(stats['today'], {
            'date': '2026-09-28', 'scheduled_count': 1, 'completed_count': 1, 'xp': 10
        })
        _, today = await self.request('GET', '/sidequests/today')
        self.assertEqual(today['quests'], [])
        self.assertEqual((await self.request('DELETE', f"/sidequests/{quest['id']}"))[0], 200)
        self.assertEqual(self.rows('sidequest_completions'), history)
        _, all_quests = await self.request('GET', '/sidequests')
        self.assertEqual(len(all_quests), 1)
        self.assertFalse(all_quests[0]['active'])
        # Reactivation, and undo after a schedule edit, both remain available.
        self.assertEqual((await self.request('PUT', f"/sidequests/{quest['id']}", {'active': True}))[0], 200)
        self.assertEqual((await self.request('DELETE', f"/sidequests/{quest['id']}/complete"))[0], 200)
        self.assertEqual(self.rows('sidequest_completions'), [])

    async def test_edit_validates_merged_schedule_and_nulls(self):
        quest = await self.create(description='Description')
        before = self.rows('sidequests')
        for values in ({'schedule_type': 'Weekly'}, {'weekdays': ['Bad']},
                       {'title': None}, {'xp': None}, {'active': None}, {'schedule_type': None}):
            self.assertEqual((await self.request('PUT', f"/sidequests/{quest['id']}", values))[0], 422)
        self.assertEqual(self.rows('sidequests'), before)
        self.assertEqual((await self.request('PUT', f"/sidequests/{quest['id']}", {}))[0], 200)
        status, result = await self.request('PUT', f"/sidequests/{quest['id']}",
            {'schedule_type': 'Weekly', 'weekdays': ['Mon'], 'description': None})
        self.assertEqual(status, 200)
        self.assertIsNone(result['description'])
        self.assertEqual((await self.request('PUT', f"/sidequests/{quest['id']}", {'weekdays': []}))[0], 422)
        status, result = await self.request('PUT', f"/sidequests/{quest['id']}", {'schedule_type': 'Daily'})
        self.assertEqual(status, 200)
        self.assertIsNone(result['weekdays'])

    async def test_hong_kong_day_rollover_resets_completion(self):
        quest = await self.create()
        self.assertEqual((await self.complete(quest, '2026-09-28T15:59:59+00:00'))[0], 200)
        with patch.object(sidequests, 'local_now', return_value=self.at('2026-09-28T16:00:00+00:00')):
            _, today = await self.request('GET', '/sidequests/today')
            self.assertEqual(today['date'], '2026-09-29')
            self.assertFalse(today['quests'][0]['completed_today'])
            await self.complete(quest)
            _, stats = await self.request('GET', '/sidequests/stats')
        self.assertEqual((stats['today']['xp'], stats['week']['xp']), (10, 20))
        self.assertEqual([row[2] for row in self.rows('sidequest_completions')], ['2026-09-28', '2026-09-29'])

    async def test_week_boundary_uses_hong_kong_monday_not_utc(self):
        quest = await self.create()
        await self.complete(quest, '2026-09-27T15:59:59+00:00')  # Sunday HK.
        with patch.object(sidequests, 'local_now', return_value=self.at('2026-09-27T16:00:00+00:00')):
            _, stats = await self.request('GET', '/sidequests/stats')
            self.assertEqual((stats['week']['start_date'], stats['week']['completions']), ('2026-09-28', 0))
            await self.complete(quest)
            _, stats = await self.request('GET', '/sidequests/stats')
            self.assertEqual((stats['week']['completions'], stats['week']['xp']), (1, 10))

    async def test_weekly_eligibility_uses_local_weekday_at_utc_boundary(self):
        quest = await self.create(schedule_type='Weekly', weekdays=['Tue'])
        self.assertEqual((await self.complete(quest, '2026-09-28T15:59:59+00:00'))[0], 409)
        self.assertEqual((await self.complete(quest, '2026-09-28T16:00:00+00:00'))[0], 200)

    async def test_missing_quests_return_404(self):
        for method, path, data in [('PUT', '/sidequests/999', {}), ('DELETE', '/sidequests/999', None),
                                   ('POST', '/sidequests/999/complete', None), ('DELETE', '/sidequests/999/complete', None)]:
            self.assertEqual((await self.request(method, path, data))[0], 404)

    async def test_lifespan_migration_is_repeatable_and_preserves_todo_and_history(self):
        before = self.rows('tasks')
        quest = await self.create()
        await self.complete(quest)
        history = self.rows('sidequest_completions')
        with sqlite3.connect(self.database) as conn:
            schema_before = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tasks'").fetchone()
        for _ in range(2):
            async with main.lifespan(main.app):
                pass
        self.assertEqual(self.rows('tasks'), before)
        self.assertEqual(self.rows('sidequest_completions'), history)
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tasks'").fetchone(), schema_before)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('PRAGMA foreign_keys = ON')
                conn.execute('DELETE FROM sidequests WHERE id = ?', (quest['id'],))


if __name__ == '__main__':
    unittest.main()
