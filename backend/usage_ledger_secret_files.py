"""Read a bounded private key file beneath the configured trusted POSIX root.

No creation, environment access, logging, application imports or fallback reads.
The service's persistent root is operator-owned; descendant traversal uses dirfds.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import stat


def read_key_file(value, persistent_root):
    if (not all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_DIRECTORY', 'geteuid'))
            or os.open not in os.supports_dir_fd):
        raise ValueError()
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError()
    path, root = Path(value), Path(persistent_root)
    if (not path.is_absolute() or not root.is_absolute()
            or '..' in path.parts or '..' in root.parts
            or root.resolve(strict=True) != root):
        raise ValueError()
    parts = path.relative_to(root).parts
    if not 1 <= len(parts) <= 32:
        raise ValueError()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, 'O_CLOEXEC', 0)
    with ExitStack() as owned:
        directory = os.open(root, directory_flags)
        owned.callback(os.close, directory)
        for component in parts[:-1]:
            directory = os.open(component, directory_flags, dir_fd=directory)
            owned.callback(os.close, directory)
            metadata = os.fstat(directory)
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ValueError()
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW
                             | getattr(os, 'O_CLOEXEC', 0) | os.O_NONBLOCK,
                             dir_fd=directory)
        owned.callback(os.close, descriptor)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077
                or not 32 <= metadata.st_size <= 512):
            raise ValueError()
        data = os.read(descriptor, 513)
        if len(data) != metadata.st_size or os.fstat(descriptor).st_size != metadata.st_size:
            raise ValueError()
        return data.decode('ascii')
