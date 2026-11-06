"""Default-off private request retention, never a dispatcher or credential.

Trusted POSIX filesystem/parents and server-owned namespace/ACL are required.
Packets may contain private settings. Hashes are not signatures or anti-rollback.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from .session_lifecycle import LifecycleError
from .session_lifecycle_commands import authorize_config_command
from .session_lifecycle_coordination import encode,parse

MAX_BYTES=256*1024
FORMAT='guiting.private-command-packet.v1'


def fail(category='coordination_packet_invalid'):
    raise LifecycleError(category)


@dataclass(frozen=True,repr=False)
class PacketReference:
    request_id:str
    packet_sha:str


class PrivateCommandPackets:
    production_supported=False
    http_integrated=False
    automatic_recovery_supported=False

    def __init__(self,directory,*,binding,authorize,enabled=False):
        self.directory=Path(directory);self.binding=binding
        self.authorize=authorize;self.enabled=enabled is True

    def _guard(self,access,action,target):
        if not self.enabled:fail('lifecycle_disabled')
        authorize_config_command(self.authorize,access,action,target)

    def _identity(self,request_id,action,target):
        if (type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',request_id)
                or type(action) is not str or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,127}',action)
                or (target is not None and (type(target) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',target)))
                or type(self.binding) is not str or not re.fullmatch('[0-9a-f]{64}',self.binding)):
            fail()
        return hashlib.sha256(encode([self.binding,request_id])).hexdigest()+'.json'

    @contextmanager
    def _directory(self):
        if os.name!='posix':fail('coordination_packet_platform_unsupported')
        path=self.directory
        fd=None
        try:
            if not path.is_absolute() or '..' in path.parts or path.resolve()!=path:fail()
            fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            s=os.fstat(fd)
            if s.st_uid!=os.getuid() or stat.S_IMODE(s.st_mode)!=0o700:fail('coordination_packet_private_directory_required')
            yield fd
        except (OSError,RuntimeError):
            raise LifecycleError('coordination_packet_unavailable') from None
        finally:
            if fd is not None:os.close(fd)

    @staticmethod
    def _read(fd,name):
        try:handle=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        except FileNotFoundError:return None
        with os.fdopen(handle,'rb') as f:
            before=os.fstat(f.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid()
                    or stat.S_IMODE(before.st_mode)!=0o600 or before.st_nlink!=1
                    or not 0<before.st_size<=MAX_BYTES):fail('coordination_packet_private_file_required')
            raw=f.read(MAX_BYTES+1);after=os.fstat(f.fileno())
        current=os.stat(name,dir_fd=fd,follow_symlinks=False)
        token=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink)
        if token(before)!=token(after) or token(after)!=token(current) or len(raw)!=before.st_size:
            fail('coordination_packet_changed')
        return raw

    def prepare(self,request_id,action,target,arguments,*,access):
        self._guard(access,action,target);name=self._identity(request_id,action,target)
        if type(arguments) is not dict:fail()
        envelope={'format':FORMAT,'binding':self.binding,'request_id':request_id,
            'principal':access.principal_id,'action':action,'target':target,'arguments':arguments}
        raw=encode(envelope)
        if len(raw)>MAX_BYTES:fail('coordination_packet_too_large')
        with self._directory() as fd:
            existing=self._read(fd,name)
            if existing is None:
                stage='.packet-'+uuid.uuid4().hex
                try:
                    handle=os.open(stage,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
                    with os.fdopen(handle,'wb') as f:
                        f.write(raw);f.flush();os.fsync(f.fileno())
                    self._guard(access,action,target)
                    try:os.link(stage,name,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
                    except FileExistsError:pass  # competing request must be byte-identical
                finally:
                    try:os.unlink(stage,dir_fd=fd)
                    except FileNotFoundError:pass
                existing=self._read(fd,name)
            if existing!=raw:fail('coordination_packet_conflict')
            # Also retry the durability barrier after an earlier uncertain fsync.
            os.fsync(fd);self._guard(access,action,target)
        return PacketReference(request_id,hashlib.sha256(raw).hexdigest())

    def load(self,request_id,action,target,*,expected_sha,access):
        self._guard(access,action,target);name=self._identity(request_id,action,target)
        if type(expected_sha) is not str or not re.fullmatch('[0-9a-f]{64}',expected_sha):fail()
        with self._directory() as fd:
            raw=self._read(fd,name)
            if raw is None:fail('coordination_packet_missing')
            if hashlib.sha256(raw).hexdigest()!=expected_sha:fail('coordination_packet_changed')
            value=parse(raw)
            expected={'format':FORMAT,'binding':self.binding,'request_id':request_id,
                'principal':access.principal_id,'action':action,'target':target,'arguments':value.get('arguments')}
            if value!=expected or type(value.get('arguments')) is not dict or encode(value)!=raw:
                fail('coordination_packet_conflict')
            self._guard(access,action,target)
            return value['arguments']
