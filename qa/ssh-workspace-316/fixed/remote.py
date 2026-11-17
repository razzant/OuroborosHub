"""Transient POSIX remote worker; invoked in memory, no installation."""
import difflib
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import tempfile
import time
import uuid

MAX_EDIT = 2 * 1024 * 1024


def bounded_int(req, key, default, low, high):
    value = req.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError('%s must be integer %s..%s' % (key, low, high))
    return value


def inside(root, value):
    p = Path(value)
    p = (root / p).resolve() if not p.is_absolute() else p.resolve()
    if p != root and root not in p.parents:
        raise ValueError('path escapes workspace')
    return p


def sha(data):
    return hashlib.sha256(data).hexdigest()


def text(data):
    try:
        return data.decode('utf-8'), False
    except UnicodeDecodeError:
        return data.decode('utf-8', errors='replace'), True


def regular(p, limit):
    s = p.stat()
    if not stat.S_ISREG(s.st_mode) or s.st_size > limit:
        raise ValueError('requires regular file of at most %d bytes' % limit)
    with p.open('rb') as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError('file grew past size limit')
    return data, s


def read_file(p, req, tail=False):
    cap = bounded_int(req, 'max_bytes', 16000, 1, 65536)
    lines = bounded_int(req, 'max_lines', 100, 1, 2000)
    if not stat.S_ISREG(p.stat().st_mode):
        raise ValueError('not a regular file')
    with p.open('rb') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode):
            raise ValueError('not a regular file')
        if tail:
            start = max(0, s.st_size - cap)
            f.seek(start)
            data = f.read(cap)
            chunks = io.BytesIO(data).readlines()
            partial_first_line = start > 0
            if start and len(chunks) > 1:
                chunks = chunks[1:]
                partial_first_line = False
            data = b''.join(chunks[-lines:])
            start = s.st_size - len(data)
            digest = None
        else:
            if s.st_size > 64 * 1024 * 1024:
                raise ValueError('read hashing limit 64 MiB; use log-tail for large logs')
            h = hashlib.sha256()
            hashed = 0
            for chunk in iter(lambda: f.read(65536), b''):
                hashed += len(chunk)
                if hashed > 64 * 1024 * 1024:
                    raise ValueError('file grew beyond read hashing limit')
                h.update(chunk)
            digest = h.hexdigest()
            start = bounded_int(req, 'offset', 0, 0, 2**63-1)
            if 'offset' not in req:
                wanted = bounded_int(req, 'start_line', 1, 1, 10000000)
                f.seek(0)
                for _ in range(wanted - 1):
                    # bounded scanning even for giant individual lines
                    while True:
                        piece = f.readline(65536)
                        if not piece or piece.endswith(b'\n'):
                            break
                start = f.tell()
            f.seek(start)
            raw = f.read(cap)
            data = b''.join(io.BytesIO(raw).readlines()[:lines])
        rendered, replacement = text(data)
        end = start + len(data)
        return dict(text=rendered, sha256=digest, start_byte=start, next_byte=end,
                    size=s.st_size, device=s.st_dev, inode=s.st_ino,
                    more=end < s.st_size, omitted_prefix=start > 0,
                    decoding_replaced=replacement, bytes_returned=len(data),
                    partial_first_line=partial_first_line if tail else False)


def listing(p, req):
    limit = bounded_int(req, 'limit', 100, 1, 1000)
    entries = []
    with os.scandir(p) as it:
        for entry in it:
            if len(entries) == limit:
                return dict(entries=entries, complete=False, reason='entry_limit')
            entries.append(dict(name=entry.name, kind='symlink' if entry.is_symlink() else
                                'directory' if entry.is_dir(follow_symlinks=False) else 'file'))
    return dict(entries=entries, complete=True)


def search(p, req):
    query = req['query']
    if not isinstance(query, str) or not query:
        raise ValueError('nonempty literal query required')
    limit = bounded_int(req, 'limit', 40, 1, 500)
    max_files = bounded_int(req, 'max_files', 1000, 1, 10000)
    matches, gaps = [], []
    count, walked, output = 0, 0, 0
    deadline = time.monotonic() + 10
    skip = {'.git', '__pycache__', 'node_modules', '.venv', 'venv'}
    def candidates():
        nonlocal walked
        if p.is_file():
            yield p
        else:
            def walk_error(error):
                gaps.append(str(error)[:300])
            for directory, dirs, files in os.walk(p, followlinks=False, onerror=walk_error):
                walked += len(dirs) + len(files) + 1
                if walked > 20000 or time.monotonic() > deadline:
                    gaps.append('walk/time limit')
                    return
                dirs[:] = [d for d in dirs if d not in skip and not (Path(directory)/d).is_symlink()]
                for name in files:
                    yield Path(directory)/name
    for file in candidates():
        if count >= max_files or time.monotonic() > deadline:
            gaps.append('file/time limit')
            break
        if file.is_symlink():
            continue
        count += 1
        try:
            data, _ = regular(file, MAX_EDIT)
            if b'\0' in data:
                gaps.append(str(file.relative_to(p if p.is_dir() else p.parent)) + ': binary')
                continue
            content = data.decode('utf-8')
        except (OSError, UnicodeError, ValueError) as error:
            gaps.append(str(file) + ': ' + str(error)[:200])
            if len(gaps) >= 100:
                gaps.append('gap limit')
                break
            continue
        # Match read(start_line=...), whose line boundaries are LF bytes.
        # splitlines() also treats form-feed, bare CR and Unicode separators as new lines.
        for number, line in enumerate(content.split('\n'), 1):
            if query in line:
                if len(matches) >= limit or output >= 32000:
                    return dict(matches=matches, scanned_files=count, complete=False,
                                gaps=gaps, reason='output_limit')
                output += min(len(line), 1000)
                matches.append(dict(path=str(file), line=number, text=line[:1000],
                                    line_shortened=len(line) > 1000))
    return dict(matches=matches, scanned_files=count, complete=not gaps, gaps=gaps,
                excluded_directories=sorted(skip))


def create(p, req):
    if not isinstance(req['content'], str):
        raise ValueError('content must be a string')
    data = req['content'].encode('utf-8')
    if len(data) > MAX_EDIT:
        raise ValueError('create limit 2 MiB')
    mode = bounded_int(req, 'mode', 384, 0, 511)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return dict(path=str(p), sha256=sha(data), bytes_written=len(data))


def patch(p, req, original_path):
    if original_path.is_symlink():
        raise ValueError('refusing symlink edit')
    data, s = regular(p, MAX_EDIT)
    if s.st_nlink != 1:
        raise ValueError('refusing hardlinked edit')
    expected = req['expected_sha256']
    if sha(data) != expected:
        raise ValueError('revision conflict; no edit applied')
    before = data.decode('utf-8')
    after = before
    edits = req['edits']
    if not isinstance(edits, list) or not edits:
        raise ValueError('nonempty edits list required')
    for edit in edits:
        old, new = edit['old'], edit['new']
        n = bounded_int(edit, 'count', 1, 1, 100000)
        if not isinstance(old, str) or not old or not isinstance(new, str) or after.count(old) != n:
            raise ValueError('replacement count conflict; no edit applied')
        after = after.replace(old, new)
    changed = after.encode('utf-8')
    if len(changed) > MAX_EDIT:
        raise ValueError('result exceeds edit limit')
    if changed == data:
        return dict(changed=False, sha256=expected, diff='', backup=None)
    backup = p.with_name(p.name + '.sshws-backup-' + uuid.uuid4().hex)
    # Recoverable original is written before replacement, never overwrites another file.
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    fd, tmp = tempfile.mkstemp(prefix='.sshws-', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(changed)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, stat.S_IMODE(s.st_mode) & 511)
        current, now = regular(p, MAX_EDIT)
        if sha(current) != expected or (now.st_dev, now.st_ino) != (s.st_dev, s.st_ino):
            raise ValueError('revision conflict before rename; backup retained at ' + str(backup))
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    diff = ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                       fromfile=str(p), tofile=str(p)))
    return dict(changed=True, sha256=sha(changed), backup=str(backup),
                diff=diff[:16000], diff_shortened=len(diff) > 16000)


def log_read(p, req):
    """Bounded byte stream, not a snapshot; cursor is not a whole-file revision."""
    cap = bounded_int(req, 'max_bytes', 16000, 1, 65536)
    cursor = req.get('cursor')
    if cursor is not None and not isinstance(cursor, dict):
        raise ValueError('cursor must be object')
    if cursor is not None and 'start' in req:
        raise ValueError('start is only valid without cursor')
    if req.get('start', 'begin') not in ('begin', 'end'):
        raise ValueError('start must be begin or end')
    fd = os.open(str(p), os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode):
            raise ValueError('not a regular file')
        identity = dict(path=str(p), dev=s.st_dev, ino=s.st_ino)
        start = s.st_size if req.get('start') == 'end' else 0
        if cursor is not None:
            start = bounded_int(cursor, 'offset', None, 0, 2**63-1)
            if cursor.get('version') != 1 or not isinstance(cursor.get('anchor'), str):
                raise ValueError('invalid cursor version or anchor')
            reason = None
            if any(cursor.get(k) != v for k, v in identity.items()):
                reason = 'identity_changed'
            elif s.st_size < start:
                reason = 'truncated'
            else:
                f.seek(max(0, start-64))
                if sha(f.read(min(64, start))) != cursor['anchor']:
                    reason = 'anchor_changed'
            if reason:
                return dict(reset_required=True, reason=reason, identity=identity,
                            size_observed=s.st_size, message='Choose fresh begin or end without cursor; nothing consumed.')
        f.seek(start)
        data = f.read(min(cap, max(0, s.st_size-start)))
        end = start + len(data)
        f.seek(max(0, end-64))
        anchor = sha(f.read(min(64, end)))
        after = os.fstat(f.fileno())
        current = p.stat()
        decoded, replaced = text(data)
        return dict(reset_required=False, text=decoded, decoding_replaced=replaced,
                    data_base64=base64.b64encode(data).decode('ascii'),
                    start_byte=start, next_byte=end, bytes_read=len(data),
                    size_observed=s.st_size, size_after=after.st_size,
                    more=end < after.st_size, partial_last_line=bool(data and not data.endswith(b'\n')),
                    changed_during_read=(s.st_size, s.st_mtime_ns, s.st_ctime_ns) !=
                                        (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                    path_replaced=(current.st_dev, current.st_ino) != (s.st_dev, s.st_ino),
                    cursor=dict(version=1, offset=end, anchor=anchor, **identity))


def execute(cwd, req):
    argv = req['argv']
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise ValueError('argv must be nonempty string array')
    timeout = bounded_int(req, 'timeout', 30, 1, 300)
    cap = bounded_int(req, 'max_bytes', 16000, 1, 65536)
    proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    totals = {'stdout': 0, 'stderr': 0}
    for stream, name in [(proc.stdout, 'stdout'), (proc.stderr, 'stderr')]:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + timeout
    timed_out, kill_attempt, kill_error = False, 'not_needed', None
    try:
        while selector.get_map():
            now = time.monotonic()
            if now >= deadline and not timed_out:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    kill_attempt = 'signal_sent'
                except ProcessLookupError:
                    kill_attempt = 'group_not_found'
                except OSError as error:
                    kill_attempt = 'failed'
                    kill_error = '%s: %s' % (type(error).__name__, error)
                deadline = now + 2
            if timed_out and now >= deadline:
                break  # escaped descendants may retain pipes; never wait indefinitely
            for key, _ in selector.select(0.05):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                name = key.data
                totals[name] += len(chunk)
                buffers[name].extend(chunk[:max(0, cap - len(buffers[name]))])
        try:
            rc = proc.wait(timeout=max(0.01, deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            # Child may have closed its streams but still be alive.
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                kill_attempt = 'signal_sent'
            except ProcessLookupError:
                kill_attempt = 'group_not_found'
            except OSError as error:
                kill_attempt = 'failed'
                kill_error = '%s: %s' % (type(error).__name__, error)
            try:
                rc = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                rc = None
        return dict(returncode=rc, timed_out=timed_out, group_stop=kill_attempt,
                    group_stop_error=kill_error, process_exit_verified=rc is not None,
                    descendants_exit_verified=False,
                    streams={name: dict(text=text(bytes(buf))[0], bytes_observed=totals[name],
                                        shortened=totals[name] > len(buf)) for name, buf in buffers.items()})
    finally:
        selector.close()
        for stream in (proc.stdout, proc.stderr):
            if not stream.closed:
                stream.close()


def dispatch(envelope):
    req = envelope['request']
    root = Path(envelope['root']).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('root must be directory')
    op = req['op']
    raw = Path(req.get('cwd' if op == 'exec' else 'path', '.'))
    original = raw if raw.is_absolute() else root / raw
    p = inside(root, str(original))
    if op == 'read':
        return read_file(p, req)
    if op == 'log-tail':
        return read_file(p, req, tail=True)
    if op == 'log-read':
        return log_read(p, req)
    if op == 'list':
        return listing(p, req)
    if op == 'search':
        return search(p, req)
    if op == 'create':
        return create(p, req)
    if op == 'patch':
        return patch(p, req, original)
    if op == 'exec':
        return execute(p, req)
    raise ValueError('unknown operation')


def main(envelope):
    try:
        return dict(ok=True, outcome='completed', result=dispatch(envelope))
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
        return dict(ok=False, outcome='error', error=type(error).__name__, message=str(error))
