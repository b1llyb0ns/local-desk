"""Calendar-day diary with bounded weekly reads and versioned saves."""
import datetime as dt
import re


class DiaryError(Exception):
    pass


class DiaryConflict(DiaryError):
    def __init__(self, entry):
        super().__init__('This entry was changed in another tab. Choose which version to keep.')
        self.entry = entry


def calendar_date(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError()
        return dt.date.fromisoformat(value)
    except ValueError:
        raise DiaryError('Enter a valid date.')


class Diary:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.execute('''CREATE TABLE IF NOT EXISTS diary_entries (
                entry_date TEXT PRIMARY KEY, content TEXT NOT NULL,
                revision INTEGER NOT NULL, updated_at TEXT NOT NULL)''')

    def week(self, value):
        day = calendar_date(value)
        try:
            start = day - dt.timedelta(days=day.weekday())
            end = start + dt.timedelta(days=6)
        except OverflowError:
            raise DiaryError('This week is outside the supported date range.')
        with self.store.lock:
            entries = [dict(row) for row in self.store.db.execute(
                'SELECT * FROM diary_entries WHERE entry_date BETWEEN ? AND ? ORDER BY entry_date',
                (start.isoformat(), end.isoformat()))]
        return {'week_start': start.isoformat(), 'week_end': end.isoformat(), 'entries': entries}

    def save(self, value, payload):
        day = calendar_date(value).isoformat()
        if not isinstance(payload, dict) or set(payload) != {'content', 'revision'}:
            raise DiaryError('Provide the entry text and its revision.')
        content, revision = payload['content'], payload['revision']
        if not isinstance(content, str) or len(content) > 12000 or '\x00' in content:
            raise DiaryError('Entries must contain no more than 12,000 characters.')
        if type(revision) is not int or not 0 <= revision < 2**53:
            raise DiaryError('Invalid entry revision.')
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with self.store.lock, self.store.db:
            if revision == 0:
                cursor = self.store.db.execute('INSERT OR IGNORE INTO diary_entries VALUES (?,?,1,?)',
                                               (day, content, now))
            else:
                cursor = self.store.db.execute('''UPDATE diary_entries
                    SET content=?, revision=revision+1, updated_at=? WHERE entry_date=? AND revision=?''',
                    (content, now, day, revision))
            row = self.store.db.execute('SELECT * FROM diary_entries WHERE entry_date=?', (day,)).fetchone()
            entry = dict(row) if row else {'entry_date': day, 'content': '', 'revision': 0, 'updated_at': None}
            if cursor.rowcount != 1:
                raise DiaryConflict(entry)
        return entry
