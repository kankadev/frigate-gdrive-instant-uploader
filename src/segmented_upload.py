"""Export existing event recordings in bounded, independently recoverable parts."""
import hashlib
import json
import logging
import math
import time
import os
from pathlib import Path
import subprocess
from urllib.parse import quote

import requests
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from src import database, work_queue

TARGET_BYTES = 128 * 1024**2
MAX_SECONDS = 300
HARD_BYTES = 512 * 1024**2


class SourceMissing(Exception):
    pass


def recordings(base, camera, start, end):
    response = requests.get(f'{base}/api/{quote(camera, safe="")}/recordings',
                            params={'after': start, 'before': end}, timeout=(10, 60))
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError('Invalid recordings response')
    for r in rows:
        if not isinstance(r, dict) or not all(k in r for k in ('start_time', 'end_time', 'segment_size')):
            raise ValueError('Invalid recording segment')
    return rows


def make_plan(rows, start, end):
    plan = []
    group = None
    size = 0
    for r in sorted(rows, key=lambda r: r['start_time']):
        a, b = max(start, r['start_time']), min(end, r['end_time'])
        if b <= a:
            continue
        # Split even an unusually large individual segment, preserving its range.
        import math
        count = max(1, math.ceil((b-a)/MAX_SECONDS), math.ceil(r['segment_size']*1024**2/TARGET_BYTES))
        for n in range(count):
            x, y = a+(b-a)*n/count, a+(b-a)*(n+1)/count
            estimate = r['segment_size']*1024**2/count
            if group and (y-group['start'] > MAX_SECONDS or size+estimate > TARGET_BYTES):
                plan.append(group)
                group, size = None, 0
            if group is None:
                group = {'start': x, 'end': y, 'duration': y-x, 'intervals': [[x,y]]}
            else:
                # Segment overlaps must not count twice toward expected duration.
                group['duration'] += max(0, y-max(x, group['end']))
                group['end'] = max(group['end'], y)
                if x <= group['intervals'][-1][1]:
                    group['intervals'][-1][1] = max(y, group['intervals'][-1][1])
                else:
                    group['intervals'].append([x,y])
            size += estimate
    if group:
        plan.append(group)
    return plan


class MediaError(ValueError):
    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind, self.detail = kind, detail


def intervals(rows, start, end):
    result = []
    for r in sorted(rows, key=lambda r: r['start_time']):
        a, b = max(start, r['start_time']), min(end, r['end_time'])
        if b <= a:
            continue
        if result and a <= result[-1][1]:
            result[-1][1] = max(b, result[-1][1])
        else:
            result.append([a, b])
    return result


def interval_duration(spans):
    return sum(b-a for a, b in spans)


def uncovered(spans, archived):
    total = 0
    for a, b in spans:
        total += (b-a) - sum(max(0, min(b,y)-max(a,x)) for x,y in archived)
    return max(0, total)


def tolerance(expected):
    return min(2, max(0.05, expected * 0.01))


def validate_video(path, expected):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration:stream=codec_type', '-of', 'json', str(path)],
        capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise MediaError('media_probe_failed', 'ffprobe rejected the downloaded file')
    try:
        data = json.loads(result.stdout)
        duration = float(data.get('format', {}).get('duration', 'nan'))
    except (ValueError, TypeError, AttributeError):
        raise MediaError('media_probe_invalid', 'ffprobe did not return valid metadata')
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError('media_duration_missing', 'Downloaded MP4 has no positive finite duration')
    if not any(x.get('codec_type') == 'video' for x in data.get('streams', [])):
        raise MediaError('media_video_missing', 'Downloaded file has no video stream')
    if duration < expected-tolerance(expected):
        raise MediaError('media_incomplete', f'Downloaded {duration:.3f}s; expected {expected:.3f}s')
    return duration


def download(base, event, part, path):
    # Read current coverage even for HTTP 200: a successful response can contain
    # only a fraction of a previously planned recording. Keep original duration.
    rows = recordings(base, event['camera'], part['start'], part['end'])
    spans = intervals(rows, part['start'], part['end'])
    present = interval_duration(spans)
    if not spans:
        raise SourceMissing('recordings_missing')
    shrunk = present < part['duration']-tolerance(part['duration'])
    if part.get('planned_intervals'):
        shrunk = shrunk or uncovered(json.loads(part['planned_intervals']), spans) > tolerance(part['duration'])
    if shrunk and (part.get('md5') or part.get('drive_id')):
        raise MediaError('recovery_identity_conflict', 'Source changed after upload identity was assigned; inspect existing remote identity')
    expected = present if shrunk else part['duration']
    start, end = part['start'], part['end']
    expanded = (math.floor(start)-1, math.ceil(end)+1)
    windows = [expanded] if end-start < 2 else [(start,end), expanded]
    partial = path.with_suffix('.partial')
    try:
        for attempt, (a,b) in enumerate(windows):
            url = f"{base}/api/{quote(event['camera'], safe='')}/start/{a}/end/{b}/clip.mp4"
            try:
                with requests.get(url, stream=True, timeout=(10,90)) as response:
                    response.raise_for_status()
                    total = 0
                    with partial.open('wb') as out:
                        for chunk in response.iter_content(1024*1024):
                            total += len(chunk)
                            if total > HARD_BYTES:
                                raise MediaError('media_size_limit', 'Download exceeded the bounded size limit')
                            out.write(chunk)
                        out.flush()
                        os.fsync(out.fileno())
                    length = response.headers.get('Content-Length')
                    if length and int(length) != total:
                        raise MediaError('media_transport_truncated', 'HTTP body length does not match Content-Length')
                duration = validate_video(partial, expected)
                # Durable before the spool rename; retry uses the same expectation.
                work_queue.update_part(event['id'], part['part'],
                    coverage='partial_pending' if shrunk else 'full',
                    archived_duration=duration, source_intervals=json.dumps(spans),
                    source_missing_since=time.time() if shrunk else None,
                    source_checked_at=time.time(), export_start=a, export_end=b,
                    issue='source_shrunk' if shrunk else None, issue_detail=None)
                partial.replace(path)
                return
            except MediaError as ex:
                if attempt+1 == len(windows) or ex.kind not in {
                        'media_duration_missing','media_video_missing','media_incomplete','media_probe_failed'}:
                    raise
    finally:
        partial.unlink(missing_ok=True)


def confirm_coverage(event, base, part):
    """Confirm a gap after one hour, without reuploading already rescued bytes."""
    now = time.time()
    if now-(part.get('source_checked_at') or 0) < 60:
        return False
    rows = recordings(base, event['camera'], part['start'], part['end'])
    current = intervals(rows, part['start'], part['end'])
    if current and part['coverage'] == 'missing_pending' and not part['uploaded']:
        work_queue.update_part(event['id'], part['part'], coverage='full',
            source_missing_since=None, source_checked_at=now, issue=None)
        return False
    archived = json.loads(part.get('source_intervals') or '[]')
    if uncovered(current, archived) > 0.05:
        raise MediaError('source_coverage_changed', 'Additional source intervals appeared after rescue; review without replacing archived bytes')
    first = part.get('source_missing_since')
    confirmed = first is not None and now-first >= 3600
    work_queue.update_part(event['id'],part['part'],source_checked_at=now,
        coverage=('partial' if part['uploaded'] else 'unavailable') if confirmed else part['coverage'])
    return confirmed


def checksum(path):
    digest = hashlib.md5(usedforsecurity=False)
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verified_remote(service, part):
    if not part['drive_id']:
        return False
    try:
        remote = service.files().get(fileId=part['drive_id'],
            fields='id,size,md5Checksum,trashed', supportsAllDrives=True).execute()
    except HttpError as e:
        if e.resp.status == 404:
            return False
        raise
    if remote.get('trashed') or int(remote.get('size', -1)) != part['size'] or remote.get('md5Checksum') != part['md5']:
        raise MediaError('drive_verification_failed', 'Remote size/checksum mismatch; retaining local part')
    return True


def step(event, base, drive):
    """Upload at most three parts, then yield to other events. Never delete source media."""
    event_id = event['id']
    planned = work_queue.parts(event_id)
    if not planned:
        rows = recordings(base, event['camera'], event['start_time'], event['end_time'])
        plan = make_plan(rows, event['start_time'], event['end_time'])
        if not plan:
            raise SourceMissing('recordings_missing')
        work_queue.save_plan(event_id, plan)
        planned = work_queue.parts(event_id)

    spool = Path(database.DB_PATH).parent / 'spool'
    spool.mkdir(exist_ok=True, mode=0o700)
    successes = 0
    waiting = False
    damaged = None
    for part in planned:
        if part.get('coverage') in ('partial_pending', 'missing_pending') and (part['uploaded'] or part['coverage']=='missing_pending'):
            try:
                if not confirm_coverage(event, base, part):
                    waiting = True
            except MediaError as ex:
                damaged = ex
            continue
        if part['uploaded'] or part.get('coverage') == 'unavailable':
            continue
        key = hashlib.sha256(f"{event_id}:{part['part']}".encode()).hexdigest()
        path = spool / (key+'.mp4')
        # Resolves an upload completed just before a crash or lost response.
        if verified_remote(drive.service, part):
            work_queue.update_part(event_id, part['part'], uploaded=1)
            path.unlink(missing_ok=True)
            successes += 1
            continue
        if not path.exists():
            if sum(p.stat().st_size for p in spool.glob('*.mp4')) > 2*1024**3:
                raise OSError('Persistent spool limit reached; retaining pending work')
            try:
                download(base, event, part, path)
            except SourceMissing:
                now = time.time()
                work_queue.update_part(event_id, part['part'],coverage='missing_pending',
                    source_intervals='[]', source_missing_since=now, source_checked_at=now,
                    issue='source_missing')
                waiting = True
                continue
            except ValueError as e:
                damaged = e
                continue
        try:
            part = next(p for p in work_queue.parts(event_id) if p['part']==part['part'])
            expected = (interval_duration(json.loads(part['source_intervals']))
                        if part.get('coverage')=='partial_pending' else part['duration'])
            validate_video(path, expected)
        except ValueError as e:
            damaged = e
            continue
        size, md5 = path.stat().st_size, checksum(path)
        if part['md5'] and (part['md5'] != md5 or part['size'] != size):
            raise MediaError('local_recovery_changed', 'Local recovery file changed; inspect before reusing upload identity')
        if not part['drive_id']:
            part['drive_id'] = drive.service.files().generateIds(count=1, space='drive', type='files').execute()['ids'][0]
        work_queue.update_part(event_id, part['part'], drive_id=part['drive_id'], size=size, md5=md5)
        part.update(size=size, md5=md5)
        filename = drive.generate_filename(event['camera'], event['start_time'], event_id, event.get('label'))
        parent = None
        for name in [drive.UPLOAD_DIR, *filename.split('__')[0].split('-')[:3]]:
            parent = drive.find_or_create_folder(name, parent)
            if not parent:
                raise RuntimeError('Drive folder unavailable')
        if len(planned) > 1:
            filename = filename[:-4] + f"__part-{part['part']+1:05d}.mp4"
        media = MediaFileUpload(str(path), mimetype='video/mp4', resumable=True, chunksize=10*1024**2)
        try:
            request = drive.service.files().create(
                body={'id': part['drive_id'], 'name': filename, 'parents': [parent]},
                media_body=media, fields='id', supportsAllDrives=True)
            response = None
            while response is None:
                _, response = request.next_chunk(num_retries=0)
        except HttpError as e:
            if e.resp.status != 409:
                raise
        finally:
            # MediaFileUpload otherwise leaves the descriptor to garbage collection.
            media.stream().close()
        if not verified_remote(drive.service, part):
            raise RuntimeError('Drive did not confirm uploaded file')
        work_queue.update_part(event_id, part['part'], uploaded=1)
        path.unlink(missing_ok=True)
        successes += 1
        logging.info('Verified upload event %s part %s/%s (%s bytes)', event_id, part['part']+1, len(planned), size)
        if successes >= 3:
            break
    current = work_queue.parts(event_id)
    remaining = [p for p in current if not p['uploaded'] and p.get('coverage')!='unavailable']
    waiting = waiting or any(p.get('coverage') in ('partial_pending','missing_pending') for p in current)
    if damaged is not None and not successes:
        raise damaged
    if not remaining and not waiting:
        if any(p.get('coverage') in ('partial','unavailable') for p in current):
            work_queue.finish_with_gaps(event_id)
        else:
            work_queue.complete(event_id)
    elif successes:
        work_queue.progress(event_id)
    elif waiting:
        work_queue.await_source(event_id)
    else:
        work_queue.defer(event_id)
