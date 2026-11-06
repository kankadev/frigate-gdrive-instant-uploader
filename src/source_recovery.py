"""Bounded metadata-only checks for recordings finalized after missing-source closure."""
import json
import logging
import math

import requests

from src import segmented_upload, work_queue


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def recheck_one(base):
    """One source request per worker cycle; no downloads, Drive calls or deletion."""
    job = work_queue.claim_missing_source()
    if job is None:
        return None
    try:
        event = json.loads(job['metadata'])
        valid = (isinstance(event, dict) and event.get('id') == job['event_id']
                 and isinstance(event.get('camera'), str) and bool(event['camera'])
                 and _number(event.get('start_time')) and _number(event.get('end_time'))
                 and 0 <= event['start_time'] < event['end_time'])
    except (ValueError, TypeError):
        valid = False
    if not valid:
        result = 'invalid_metadata'
    else:
        try:
            rows = segmented_upload.recordings(base, event['camera'],
                event['start_time'], event['end_time'], timeout=(5, 15))
            for row in rows:
                if (not all(_number(row.get(k)) for k in ('start_time', 'end_time', 'segment_size'))
                        or row['start_time'] >= row['end_time'] or row['segment_size'] <= 0):
                    raise ValueError('Invalid source interval')
            present = segmented_upload.intervals(rows, event['start_time'], event['end_time'])
            result = 'available' if present else 'missing'
        except requests.RequestException:
            # A timeout, 404 or auth/server failure cannot prove missing media.
            result = 'request_failed'
        except (ValueError, TypeError, KeyError):
            result = 'invalid_source'
    applied = work_queue.finish_source_check(job, result)
    # Fixed result values only; exception text can contain credentials or URLs.
    logging.info('Late source check: %s', result if applied else 'state_changed')
    return result if applied else 'state_changed'
