"""Explicit dedicated-ledger snapshots and isolated recovery; CLI is mock-only.

Never imports a live entrypoint, reads environment settings, mounts a route,
starts a worker, checkpoints a source, or restores over an existing file.
An old snapshot is NOT authority to resume capture or resurrect deleted data.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time

from .usage_ledger import LedgerError
from .usage_ledger_paths import lexically_separate

REQUIRED = {
    'usage_ledger': {'mode','provider_id','request_id','session_id','at','payload'},
    'usage_ledger_meta': {'key','value'},
    'usage_ledger_retired': {'session_id','at'},
    'usage_ledger_removed': {'mode','provider_id','request_id'},
    'usage_ledger_storage': {'mode','raw_before','records_before','removed'},
}


def unavailable():
    raise LedgerError('ledger_backup_unavailable')


def _path(value):
    if not isinstance(value,(str,Path)): unavailable()
    p=Path(value)
    if not p.is_absolute() or '..' in p.parts or not p.name or p.is_symlink(): unavailable()
    return p.resolve()


def _source(path):
    if not path.is_file(): unavailable()
    conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)
    conn.execute('PRAGMA query_only=ON')
    return conn


def _schema(conn):
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 129")}
    if (len(tables)>128 or not REQUIRED.keys()<=tables
            or any(not (n.startswith('usage_ledger') or n.startswith('sqlite_')) for n in tables)):
        unavailable()
    for table,columns in REQUIRED.items():
        if not columns<={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}: unavailable()


def snapshot(source, destination, *, timeout_seconds=10):
    """SQLite backup API into an exclusively created 0600 file in a private dir.

    Only an explicit trusted caller supplies paths. No production command-line
    source/restore option, scheduling, source database initialization or retries.
    The source-local snapshot does not make several databases transactional.
    """
    target=None;identity=None;fd=None;complete=False
    try:
        if (type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds)
                or not 0<timeout_seconds<=30): unavailable()
        deadline=time.monotonic()+timeout_seconds
        def progress(*_):
            if time.monotonic()>deadline: unavailable()
        origin,target=_path(source),_path(destination)
        parent=target.parent.stat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_mode&0o077
                or not lexically_separate([origin,target])
                or any(Path(str(target)+suffix).exists() or Path(str(target)+suffix).is_symlink()
                       for suffix in ('','-wal','-shm','-journal'))): unavailable()
        with closing(_source(origin)) as src:
            _schema(src);progress()
            fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
            info=os.fstat(fd);identity=(info.st_dev,info.st_ino)
            with closing(sqlite3.connect(target,timeout=.2)) as dst:
                src.backup(dst,pages=64,progress=progress,sleep=.01)
                progress()
                # Only the new destination is converted; no source checkpoint.
                if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0]!='delete': unavailable()
                dst.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
                _schema(dst)
                if dst.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]: unavailable()
                if dst.execute('PRAGMA foreign_key_check').fetchone() is not None: unavailable()
            progress();os.fsync(fd);complete=True
        return {'ok':True,'method':'sqlite_online_backup','integrity':'ok',
                'scope':'dedicated_ledger_only','production_verified':False,'enables_production':False}
    except (OSError,ValueError,TypeError,sqlite3.Error,RuntimeError):
        raise LedgerError('ledger_backup_unavailable') from None
    finally:
        if fd is not None:os.close(fd)
        if not complete and identity is not None and target is not None:
            try:
                info=target.lstat()
                if (info.st_dev,info.st_ino)==identity:target.unlink()
            except OSError:pass


@contextmanager
def isolated_restore(source, *, timeout_seconds=10):
    """A fresh temporary destination only. Never accepts a production target."""
    with tempfile.TemporaryDirectory(prefix='guiting-ledger-recovery-') as temp:
        target=Path(temp)/'restored.sqlite'
        snapshot(source,target,timeout_seconds=timeout_seconds)
        yield target


def mock_rehearsal():
    """Exercise real Ledger + SQLite operations with disposable synthetic data."""
    from .usage_ledger import Ledger,build_view
    from .usage_ledger_journal import CollectionJournal,coverage
    from .usage_ledger_storage import maintain,page
    with tempfile.TemporaryDirectory(prefix='guiting-ledger-backup-mock-') as temp:
        root=Path(temp);ledger=Ledger(root/'source.sqlite')
        base={'mode':'api','provider_id':'synthetic-api','request_id':'one','turn_id':'one',
              'session_id':'synthetic-session','at':'2026-10-23T02:00:00Z','model':'synthetic-model',
              'dialect':'openai_responses','raw_usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120},
              'usage_complete':True,'status':'completed',
              'price':{'currency':'USD','price_id':'synthetic-price','write_in_input_rate':True,
                       'per_million':{'input':'1','output':'2'}}}
        with closing(ledger.connect()) as keep_wal:
            keep_wal.execute('PRAGMA wal_autocheckpoint=0')
            keep_wal.execute('BEGIN')
            keep_wal.execute('SELECT COUNT(*) FROM usage_ledger').fetchone()
            for i in range(3):ledger.append({**base,'request_id':str(i),'turn_id':str(i)})
            ledger.append({**base,'mode':'codex','dialect':'codex','price':None,'raw_usage':None,
                           'usage_complete':False,'request_id':'native'})
            ledger.purge_session('synthetic-retired',retire=True,mode='api')
            journal=CollectionJournal(ledger,'api')
            journal.start({**base,'request_id':'pending'})
            journal.issue('capture_paused',request_id='pause')
            maintain(ledger,'api',raw_before='2026-10-24T00:00:00Z')
            first=page(ledger,'api','synthetic-session',limit=1)
            expected=build_view(ledger,'api','synthetic-session')['summary']
            wal=Path(ledger.path+'-wal');wal_present=wal.is_file() and wal.stat().st_size>0
            report=snapshot(ledger.path,root/'backup.sqlite')
            with isolated_restore(root/'backup.sqlite') as restored_path:
                restored=Ledger(restored_path)
                checks={'committed_wal_observed':wal_present,
                        'summary_and_prices_preserved':build_view(restored,'api','synthetic-session')['summary']==expected,
                        'signed_cursor_preserved':bool(page(restored,'api','synthetic-session',limit=1,cursor=first['next_cursor'])['records']),
                        'unconfirmed_preserved':coverage(restored,'api')['unconfirmed']==1,
                        'pause_preserved':coverage(restored,'api')['capture_pauses']==1,
                        'raw_expiry_preserved':all(r['raw_usage'] is None for r in restored.records('api')),
                        'retirement_preserved':not restored.append({**base,'session_id':'synthetic-retired','request_id':'late',
                                                       'at':datetime.now(timezone.utc).isoformat()}),
                        'modes_separate':len(restored.records('api'))==3 and len(restored.records('codex'))==1}
                if not all(checks.values()):unavailable()
        return {**report,'basis':'disposable_mock_only','checks':checks,
                'production_restore_supported':False,'external_thread_exclusivity_verified':False,
                'native_notification_continuity_verified':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mock',action='store_true',required=True)
    parser.parse_args(argv)
    try:
        print(json.dumps(mock_rehearsal(),sort_keys=True));return 0
    except (OSError,ValueError,TypeError,sqlite3.Error):
        print(json.dumps({'ok':False,'error':'ledger_backup_unavailable','enables_production':False}));return 1


if __name__=='__main__':raise SystemExit(main())
