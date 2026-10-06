"""On-demand local socket inventory. No notifications, history, or background work."""
import copy
import datetime
import threading

from local_listeners import EXPOSURE_NOTICE, PROCESS_NOTICE, classify_exposure, snapshot as collect_listeners


class LocalPortInventory:
    def __init__(self, collector=None, clock=None):
        self.collector = collector or collect_listeners
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.lock = threading.Lock()
        self.check_lock = threading.Lock()
        self.inventory = None
        self.last_complete = None

    @staticmethod
    def _empty():
        return {'checked_at': None, 'state': 'pending', 'listeners': [], 'count': 0,
                'process_visibility': 'unknown', 'process_visibility_notice': PROCESS_NOTICE,
                'exposure_notice': EXPOSURE_NOTICE, 'skipped_lines': 0, 'truncated': False,
                'error': None, 'stale': False, 'last_attempt_at': None}

    def cached_inventory(self):
        """Read in-memory results only. The old persisted alert state is never read."""
        with self.lock:
            result = copy.deepcopy(self.inventory if self.inventory is not None else self._empty())
        result.update(cached=True, refreshing=self.check_lock.locked())
        return result

    @staticmethod
    def _normalize(raw):
        fields = ('checked_at', 'state', 'process_visibility', 'process_visibility_notice',
                  'exposure_notice', 'skipped_lines', 'truncated', 'error')
        result = LocalPortInventory._empty()
        result.update({key: copy.deepcopy(raw[key]) for key in fields if key in raw})
        row_fields = ('protocol', 'state', 'address', 'port', 'family', 'exposure', 'processes', 'process_visibility')
        for original in raw.get('listeners', []):
            row = {key: copy.deepcopy(original[key]) for key in row_fields if key in original}
            row['exposure_class'], row['exposure_reason'] = classify_exposure(row['address'])
            row['internet_reachability'] = 'not_checked'
            row['potential_public_tcp'] = row['protocol'] == 'tcp' and row['exposure_class'] in {'wildcard', 'public-address'}
            result['listeners'].append(row)
        result['count'] = len(result['listeners'])
        return result

    def refresh(self):
        """One bounded collection per explicit request; concurrent requests share cache."""
        if not self.check_lock.acquire(blocking=False):
            return self.cached_inventory()
        try:
            attempted = self.clock().isoformat()
            try:
                result = self._normalize(self.collector())
            except Exception:
                result = self._empty()
                result.update(state='error', error='Could not read local listening sockets.')
            complete = result['state'] == 'ok' and not result['truncated'] and not result['skipped_lines'] and not result['error']
            with self.lock:
                if complete:
                    result.update(stale=False, last_attempt_at=attempted)
                    self.last_complete = copy.deepcopy(result)
                else:
                    message = result.get('error') or 'The socket inventory is incomplete.'
                    if self.last_complete is not None:
                        result = copy.deepcopy(self.last_complete)
                        result.update(state='error', stale=True, error=message + ' Showing the previous complete snapshot.')
                    else:
                        result.update(state='error' if result['state'] == 'error' else 'partial', stale=False, error=message)
                    result['last_attempt_at'] = attempted
                self.inventory = result
                response = copy.deepcopy(result)
            response.update(cached=bool(response['stale']), refreshing=False)
            return response
        finally:
            self.check_lock.release()
