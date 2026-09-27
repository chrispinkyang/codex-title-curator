#!/usr/bin/env python3
"""Private progress tracking and read-only inventory for Codex title curation."""
import argparse
from contextlib import closing
import datetime as dt
import json
import math
import os
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo


def codex_home():
    return Path(os.environ.get('CODEX_HOME') or '~/.codex').expanduser()


def local_now(timezone=None):
    return dt.datetime.now(ZoneInfo(timezone)) if timezone else dt.datetime.now().astimezone()


def read_state(path):
    if not path.exists():
        return {'version': 1, 'threads': {}}
    state = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(state, dict) or not isinstance(state.get('threads', {}), dict):
        raise ValueError('Unsupported state format')
    if state.get('version', 1) != 1:
        raise ValueError('Unsupported state version')
    if not isinstance(state.get('excluded_titles', []), list):
        raise ValueError('excluded_titles must be a list')
    return state


def write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.title-curator-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def idle_seconds():
    if platform.system() != 'Darwin':
        raise RuntimeError('Idle detection requires macOS; use manual mode on this platform')
    raw = subprocess.check_output(['/usr/sbin/ioreg', '-c', 'IOHIDSystem'], text=True, timeout=10)
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', raw)
    if not match:
        raise RuntimeError('HIDIdleTime was not returned')
    return int(match.group(1)) / 1_000_000_000


def gate(state, timezone=None, minimum=900, idle_only=False):
    today = local_now(timezone).date().isoformat()
    if not idle_only and state.get('last_completed_date') == today:
        return {'eligible': False, 'reason': 'completed_today', 'date': today}
    idle = idle_seconds()
    return {'eligible': idle >= minimum,
            'reason': 'idle' if idle >= minimum else 'user_active',
            'idle_seconds': round(idle), 'date': today}


def database_path(home, explicit=None):
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError('The specified metadata database does not exist')
        return path
    candidates = []
    for p in home.glob('state_*.sqlite'):
        match = re.fullmatch(r'state_(\d+)\.sqlite', p.name)
        if match and p.is_file():
            candidates.append((int(match.group(1)), p))
    if not candidates:
        raise FileNotFoundError('No local Codex state database found; use app task tools')
    return max(candidates, key=lambda pair: pair[0])[1].resolve()


def connect(path):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    columns = {r[1] for r in connection.execute('PRAGMA table_info(threads)')}
    required = {'id', 'title', 'updated_at', 'archived', 'source'}
    if not required.issubset(columns):
        connection.close()
        raise RuntimeError('Unsupported threads schema; missing: ' + ', '.join(sorted(required - columns)))
    return connection, columns


def seconds(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError('Invalid update timestamp')
    return result / 1000 if result >= 100_000_000_000 else result


def selected_columns(columns):
    wanted = ['id', 'title', 'name', 'updated_at', 'archived', 'source',
              'agent_path', 'thread_source', 'first_user_message']
    return ', '.join('"' + name + '"' for name in wanted if name in columns)


def title_of(row):
    return row.get('name') or row['title']


def excluded(row, state):
    if row['archived'] or row['id'] == state.get('maintenance_thread_id'):
        return True
    source = str(row.get('source') or '').lower()
    if 'subagent' in source or source == 'review' or row.get('agent_path') not in (None, '', '/root'):
        return True
    if 'automation' in str(row.get('thread_source') or '').lower():
        return True
    if (row.get('first_user_message') or '').lstrip().startswith('Automation:'):
        return True
    return title_of(row) in state.get('excluded_titles', [])


def scan(path, state, days=30, now=None):
    cutoff = (time.time() if now is None else now) - days * 86400
    candidates, external = [], []
    connection, columns = connect(path)
    with closing(connection):
        # Supports both seconds and milliseconds without modifying the source DB.
        rows = connection.execute('SELECT ' + selected_columns(columns) + ' FROM threads WHERE archived=0 AND updated_at>=? ORDER BY updated_at DESC', (cutoff,))
        considered = 0
        for item in rows:
            row = dict(item)
            updated = seconds(row['updated_at'])
            if updated < cutoff or excluded(row, state):
                continue
            considered += 1
            title = title_of(row)
            old = state.get('threads', {}).get(row['id'])
            if old and title != old.get('title'):
                external.append(row['id'])
                continue
            if old and old.get('review_status') == 'complete' and updated <= seconds(old.get('updated_at', 0)):
                continue
            candidates.append({'id': row['id'], 'title': title, 'updated_at': updated})
    candidates.sort(key=lambda r: r['updated_at'], reverse=True)
    return {'candidates': candidates, 'external_title_edits_skipped': external,
            'considered': considered, 'days': days}


def checkpoint(path, state_path, entries):
    if not isinstance(entries, list) or not entries:
        raise ValueError('Checkpoint entries must be a nonempty JSON array')
    state = read_state(state_path)
    connection, columns = connect(path)
    records = {}
    with closing(connection):
        for entry in entries:
            ident = entry['id']
            if not isinstance(ident, str) or ident in records:
                raise ValueError('Checkpoint IDs must be unique strings')
            item = connection.execute('SELECT ' + selected_columns(columns) + ' FROM threads WHERE id=?', (ident,)).fetchone()
            if item is None or excluded(dict(item), state):
                raise ValueError('Checkpoint target is missing or excluded')
            row = dict(item)
            if entry['title'] != title_of(row):
                raise ValueError('Title changed since verification; reread the target')
            observed = seconds(entry['observed_updated_at'])
            current = seconds(row['updated_at'])
            if observed > current:
                raise ValueError('Review timestamp is newer than the stored conversation')
            status = entry.get('review_status', 'complete')
            if status not in ('complete', 'partial'):
                raise ValueError('review_status must be complete or partial')
            records[ident] = {'title': entry['title'], 'updated_at': observed,
                              'review_status': 'partial' if current > observed else status,
                              'reviewed_at': dt.datetime.now(dt.timezone.utc).isoformat()}
    state.setdefault('threads', {}).update(records)
    write_state(state_path, state)
    return {'recorded': len(records), 'partial': sum(r['review_status'] == 'partial' for r in records.values())}


def complete(path, state_path, days=30, timezone=None):
    state = read_state(state_path)
    result = scan(path, state, days)
    if result['candidates']:
        raise ValueError('Unreviewed or partial tasks remain; do not mark the day complete')
    now = local_now(timezone)
    state['last_completed_date'] = now.date().isoformat()
    state['last_completed_at'] = now.isoformat()
    write_state(state_path, state)
    return {'completed': True, 'date': state['last_completed_date']}


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', type=Path, default=codex_home())
    parser.add_argument('--state', type=Path)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--timezone', help='IANA timezone; defaults to the OS local date')
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('gate', help='Check daily/idle eligibility; no inventory reads')
    p.add_argument('--idle-only', action='store_true')
    p.add_argument('--min-idle-seconds', type=positive, default=900)
    p = subs.add_parser('scan', help='Read local candidates without conversation bodies')
    p.add_argument('--days', type=positive, default=30)
    p = subs.add_parser('checkpoint', help='Record app-verified titles in private state')
    p.add_argument('--entries', type=Path, required=True)
    p = subs.add_parser('complete', help='Finish the local day if no candidates remain')
    p.add_argument('--days', type=positive, default=30)
    args = parser.parse_args(argv)
    try:
        home = args.codex_home.expanduser().resolve()
        state_path = (args.state or home / 'title-curator' / 'state.json').expanduser().resolve()
        if args.command == 'gate':
            result = gate(read_state(state_path), args.timezone, args.min_idle_seconds, args.idle_only)
        else:
            path = database_path(home, args.db)
            if state_path == path:
                raise ValueError('Private state must not replace the app metadata database')
            if args.command == 'scan':
                result = scan(path, read_state(state_path), args.days)
            elif args.command == 'checkpoint':
                entries = json.loads(args.entries.expanduser().read_text(encoding='utf-8'))
                result = checkpoint(path, state_path, entries)
            else:
                result = complete(path, state_path, args.days, args.timezone)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({'eligible': False, 'reason': 'check_failed', 'error': type(exc).__name__ + ': ' + str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
