import datetime as dt
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('curator', Path(__file__).resolve().parents[1] / 'scripts' / 'title_maintenance.py')
curator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(curator)


class CuratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'state_5.sqlite'
        self.state = self.root / 'private' / 'state.json'
        self.now = int(time.time())
        with closing(sqlite3.connect(str(self.db))) as c:
            c.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, name TEXT, updated_at INTEGER, archived INTEGER, source TEXT, agent_path TEXT, thread_source TEXT, first_user_message TEXT)')
            c.commit()

    def add(self, ident, **values):
        row = {'id': ident, 'title': 'Raw first message', 'name': 'Investigate timeout',
               'updated_at': self.now, 'archived': 0, 'source': 'desktop', 'agent_path': '/root',
               'thread_source': None, 'first_user_message': 'PRIVATE_TRANSCRIPT_SENTINEL'}
        row.update(values)
        with closing(sqlite3.connect(str(self.db))) as c:
            c.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)', tuple(row.values()))
            c.commit()

    def entry(self, ident, **changes):
        entry = {'id': ident, 'title': 'Investigate timeout', 'observed_updated_at': self.now, 'review_status': 'complete'}
        entry.update(changes)
        return entry

    def test_scope_timestamps_and_no_transcript_in_output(self):
        self.add('seconds')
        self.add('milliseconds', updated_at=self.now * 1000)
        self.add('old', updated_at=self.now - 31 * 86400)
        self.add('old-ms', updated_at=(self.now - 31 * 86400) * 1000)
        self.add('archived', archived=1)
        self.add('child', source=json.dumps({'subagent': {'thread_spawn': {}}}))
        self.add('child-path', agent_path='/root/helper')
        self.add('automation', first_user_message='Automation: Example check')
        self.add('automation-metadata', thread_source='automation')
        self.add('maintenance')
        self.add('excluded-title', name='Example check')
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = curator.scan(self.db, {'maintenance_thread_id': 'maintenance', 'excluded_titles': ['Example check']}, now=self.now)
        self.assertEqual({x['id'] for x in result['candidates']}, {'seconds', 'milliseconds'})
        self.assertNotIn('PRIVATE_TRANSCRIPT_SENTINEL', json.dumps(result))
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_sqlite_connection_refuses_writes(self):
        connection, _ = curator.connect(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('DELETE FROM threads')
        finally:
            connection.close()

    def test_external_edits_preserved_and_partial_retried(self):
        for ident in ('done', 'partial', 'changed', 'manual'):
            self.add(ident)
        state = {'threads': {ident: {'title': 'Investigate timeout', 'updated_at': self.now, 'review_status': 'complete'} for ident in ('done', 'partial', 'changed', 'manual')}}
        state['threads']['partial']['review_status'] = 'partial'
        state['threads']['changed']['updated_at'] -= 10
        state['threads']['manual']['title'] = 'An older title'
        result = curator.scan(self.db, state, now=self.now)
        self.assertEqual({x['id'] for x in result['candidates']}, {'partial', 'changed'})
        self.assertEqual(result['external_title_edits_skipped'], ['manual'])

    def test_checkpoint_conflict_does_not_partially_save(self):
        self.add('one')
        self.add('two')
        curator.write_state(self.state, {'version': 1, 'threads': {}})
        before = self.state.read_bytes()
        with self.assertRaises(ValueError):
            curator.checkpoint(self.db, self.state, [self.entry('one'), self.entry('two', title='A stale proposal')])
        self.assertEqual(self.state.read_bytes(), before)

    def test_conversation_update_keeps_checkpoint_partial(self):
        self.add('one', updated_at=self.now + 1)
        result = curator.checkpoint(self.db, self.state, [self.entry('one')])
        self.assertEqual(result['partial'], 1)
        with self.assertRaises(ValueError):
            curator.complete(self.db, self.state)
        self.assertNotIn('last_completed_date', curator.read_state(self.state))

    def test_completion_requires_every_candidate_reviewed(self):
        self.add('one')
        self.add('two')
        before = self.db.read_bytes()
        curator.checkpoint(self.db, self.state, [self.entry('one')])
        with self.assertRaises(ValueError):
            curator.complete(self.db, self.state)
        curator.checkpoint(self.db, self.state, [self.entry('two')])
        result = curator.complete(self.db, self.state, timezone='UTC')
        self.assertTrue(result['completed'])
        self.assertEqual(result['date'], dt.datetime.now(dt.timezone.utc).date().isoformat())
        self.assertEqual(before, self.db.read_bytes())
        if os.name == 'posix':
            self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_daily_gate_avoids_even_idle_probe(self):
        date = curator.local_now('UTC').date().isoformat()
        with patch.object(curator, 'idle_seconds', side_effect=AssertionError('must not probe')):
            result = curator.gate({'last_completed_date': date}, timezone='UTC')
        self.assertEqual(result['reason'], 'completed_today')

    def test_idle_gate_rechecks_activity_and_threshold(self):
        with patch.object(curator, 'idle_seconds', return_value=899):
            self.assertFalse(curator.gate({})['eligible'])
        with patch.object(curator, 'idle_seconds', return_value=900):
            self.assertTrue(curator.gate({})['eligible'])
        state = {'last_completed_date': curator.local_now().date().isoformat()}
        with patch.object(curator, 'idle_seconds', return_value=1):
            self.assertEqual(curator.gate(state, idle_only=True)['reason'], 'user_active')

    def test_unsupported_idle_platform_fails_closed(self):
        with patch.object(curator.platform, 'system', return_value='Linux'):
            with self.assertRaisesRegex(RuntimeError, 'requires macOS'):
                curator.idle_seconds()

    def test_missing_idle_metric_fails(self):
        with patch.object(curator.platform, 'system', return_value='Darwin'), patch.object(curator.subprocess, 'check_output', return_value='no metric'):
            with self.assertRaisesRegex(RuntimeError, 'not returned'):
                curator.idle_seconds()

    def test_schema_and_corrupt_state_fail_closed(self):
        invalid = self.root / 'state_99.sqlite'
        with closing(sqlite3.connect(str(invalid))) as c:
            c.execute('CREATE TABLE threads (id TEXT)')
            c.commit()
        self.assertEqual(curator.database_path(self.root), invalid)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported threads schema'):
            curator.scan(invalid, {})
        self.state.parent.mkdir()
        self.state.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            curator.read_state(self.state)

    def test_environment_and_missing_database(self):
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root)}):
            self.assertEqual(curator.codex_home(), self.root)
        with self.assertRaises(FileNotFoundError):
            curator.database_path(self.root / 'missing')
        self.assertFalse((self.root / 'missing').exists())


if __name__ == '__main__':
    unittest.main()
