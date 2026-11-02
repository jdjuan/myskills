#!/usr/bin/env python3
"""Local, append-only skill invocation records. No prompt or project contents."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import uuid
import threading
from portable import log_fd

_WRITE_LOCK = threading.Lock()

LOG_NAME = 'usage.jsonl'


def record(skill_path):
    manifest = Path(skill_path).expanduser().resolve(strict=True)
    if manifest.name != 'SKILL.md' or not manifest.is_file():
        raise ValueError('Pass the invoked skill’s SKILL.md path.')
    event = {'id': str(uuid.uuid4()), 'timestamp': datetime.now(timezone.utc).isoformat()}
    # Do not follow a log symlink or block on a FIFO.
    fd = log_fd(manifest.parent / LOG_NAME, write=True)
    with _WRITE_LOCK, os.fdopen(fd, 'a', encoding='utf-8') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Usage log must be a regular file.')
        if os.name == 'nt':
            import msvcrt
            # Lock the first byte; records append at EOF while the lock is held.
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            stream.seek(0, os.SEEK_END)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(event) + '\n')
        stream.flush()
        if os.name == 'nt':
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    return event


def read_usage(folders):
    events = {}
    for folder in folders:
        try:
            fd = log_fd(Path(folder) / LOG_NAME)
            with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    continue
                for line in stream:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            continue
                        event_id = str(uuid.UUID(event['id']))
                        timestamp = datetime.fromisoformat(event['timestamp'])
                        if timestamp.tzinfo is None:
                            continue
                        events[event_id] = timestamp.astimezone(timezone.utc).isoformat()
                    except (ValueError, TypeError, KeyError, AttributeError):
                        continue
        except OSError:
            continue
    recent = sorted(events.values(), reverse=True)
    return {'count': len(recent), 'lastUsed': recent[0] if recent else None, 'recent': recent[:10]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('skill_path', help='Absolute path to the invoked SKILL.md')
    args = parser.parse_args()
    try:
        record(args.skill_path)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Could not record skill usage: {error}\n')
