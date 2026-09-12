"""Export existing event recordings in bounded, independently recoverable parts."""
import hashlib
import json
import logging
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
                group = {'start': x, 'end': y, 'duration': y-x}
            else:
                # Segment overlaps must not count twice toward expected duration.
                group['duration'] += max(0, y-max(x, group['end']))
                group['end'] = max(group['end'], y)
            size += estimate
    if group:
        plan.append(group)
    return plan


def validate_video(path, expected):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'json', str(path)], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('Video probe failed')
    duration = float(json.loads(result.stdout)['format']['duration'])
    if duration <= 0 or duration < expected-max(2, expected*0.01):
        raise ValueError(f'Incomplete video: {duration:.2f}s, expected {expected:.2f}s')


def download(base, event, part, path):
    camera = quote(event['camera'], safe='')
    url = f"{base}/api/{camera}/start/{part['start']}/end/{part['end']}/clip.mp4"
    partial = path.with_suffix('.partial')
    try:
        with requests.get(url, stream=True, timeout=(10, 90)) as response:
            if response.status_code in (400, 404):
                # A status code alone is not proof of missing media.
                rows = recordings(base, event['camera'], part['start'], part['end'])
                if not make_plan(rows, part['start'], part['end']):
                    raise SourceMissing('recordings_missing')
            response.raise_for_status()
            total = 0
            with partial.open('wb') as out:
                for chunk in response.iter_content(1024*1024):
                    total += len(chunk)
                    if total > HARD_BYTES:
                        raise ValueError('Part exceeded bounded download limit; retained for retry')
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            length = response.headers.get('Content-Length')
            if length and int(length) != total:
                raise ValueError('Truncated HTTP body')
        validate_video(partial, part['duration'])
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


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
        raise ValueError('Remote file verification failed; retaining local part')
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
    missing = False
    damaged = None
    for part in planned:
        if part['uploaded']:
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
                missing = True
                continue
            except ValueError as e:
                damaged = e
                continue
        try:
            validate_video(path, part['duration'])
        except ValueError as e:
            damaged = e
            continue
        size, md5 = path.stat().st_size, checksum(path)
        if part['md5'] and (part['md5'] != md5 or part['size'] != size):
            raise ValueError('Local recovery file changed; manual inspection required')
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
    remaining = [p for p in work_queue.parts(event_id) if not p['uploaded']]
    if not remaining:
        work_queue.complete(event_id)
    elif successes:
        work_queue.defer(event_id)
    elif damaged is not None:
        raise damaged
    elif missing:
        raise SourceMissing('recordings_missing_or_partial')
    else:
        work_queue.defer(event_id)
