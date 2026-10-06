"""Dated personal tasks stored in the panel's existing SQLite database."""
import datetime
import re
import uuid


class TaskError(Exception):
    pass


def validate(payload, creating=False):
    if not isinstance(payload, dict) or set(payload) - {'title', 'due_date', 'notes', 'completed'}:
        raise TaskError('Unknown task fields.')
    result = {}
    for field, value in payload.items():
        if field == 'completed':
            if not isinstance(value, bool):
                raise TaskError('Choose whether the task is completed.')
        else:
            limit = 4000 if field == 'notes' else 200 if field == 'title' else 10
            if not isinstance(value, str) or len(value) > limit or '\x00' in value:
                raise TaskError('Invalid task ' + field + '.')
            value = value.strip()
            if field == 'title' and not value:
                raise TaskError('Enter a task title.')
            if field == 'due_date':
                try:
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                        raise ValueError()
                    date = datetime.date.fromisoformat(value)
                    if not 2000 <= date.year <= 2200:
                        raise ValueError()
                except ValueError:
                    raise TaskError('Choose a valid task date between 2000 and 2200.')
        result[field] = value
    if creating and not {'title', 'due_date'} <= result.keys():
        raise TaskError('Enter a title and date.')
    return result


class Tasks:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, due_date TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '', completed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')

    def snapshot(self):
        with self.store.lock:
            rows = self.store.db.execute('SELECT * FROM tasks ORDER BY completed, due_date, created_at, id').fetchall()
        return {'tasks': [dict(row, completed=bool(row['completed'])) for row in rows]}

    def add(self, payload):
        fields = validate(payload, creating=True)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        identifier = uuid.uuid4().hex
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)',
                (identifier, fields['title'], fields['due_date'], fields.get('notes', ''),
                 fields.get('completed', False), now, now))
        return identifier

    def update(self, identifier, payload):
        fields = validate(payload)
        fields['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.store.lock, self.store.db:
            cursor = self.store.db.execute('UPDATE tasks SET ' + ','.join(key + '=?' for key in fields) + ' WHERE id=?',
                                           (*fields.values(), identifier))
            if not cursor.rowcount:
                raise KeyError(identifier)

    def delete(self, identifier):
        with self.store.lock, self.store.db:
            if not self.store.db.execute('DELETE FROM tasks WHERE id=?', (identifier,)).rowcount:
                raise KeyError(identifier)
