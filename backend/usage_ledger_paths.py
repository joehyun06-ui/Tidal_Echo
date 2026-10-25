"""SQLite file-family separation, without opening databases or creating files."""
from pathlib import Path
import stat

from .usage_ledger import LedgerError

SIDECARS = ('-wal', '-shm', '-journal')


def database_family(path):
    return (path, *(path.with_name(path.name + suffix) for suffix in SIDECARS))


def overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def lexically_separate(paths):
    """Pure path check for credential-free plans; never touches the filesystem."""
    seen = []
    for path in paths:
        for member in database_family(path):
            if any(overlap(member, old) for old in seen): return False
            seen.append(member)
    return True


def separate_paths(ledger_path, relay_db, native_db):
    """Check main files and SQLite sidecars before any ledger initialization.

    Metadata is a point-in-time check, not protection against filesystem races or
    proof of persistent mounts, permissions, capacity, backups or external writers.
    """
    values = [ledger_path, relay_db] + ([native_db] if native_db is not None else [])
    bases, seen = [], []
    for value in values:
        if not isinstance(value, (str, Path)) or not str(value) or str(value) == ':memory:':
            raise LedgerError('ledger_composition_invalid')
        raw = Path(value)
        if not raw.is_absolute() or '..' in raw.parts or not raw.name:
            raise LedgerError('ledger_composition_invalid')
        base = raw.resolve()
        bases.append(base)
        for member in database_family(base):
            resolved = member.resolve()
            try:
                info = resolved.stat()
            except FileNotFoundError:
                info = None
            if info is not None and not stat.S_ISREG(info.st_mode):
                raise LedgerError('ledger_composition_invalid')
            identity = (info.st_dev, info.st_ino) if info is not None else None
            if any(overlap(resolved, old) or identity is not None and identity == old_id
                   for old, old_id in seen):
                raise LedgerError('ledger_composition_invalid')
            seen.append((resolved, identity))
    return bases[0], bases[1], bases[2] if len(bases) == 3 else None
