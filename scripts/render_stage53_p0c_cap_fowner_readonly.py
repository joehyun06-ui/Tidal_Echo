"""Stage53 P0C: owner-authorized anonymous effective CAP_FOWNER query.

Linux x86_64 only. One read-only capget(2) syscall on this Python thread,
with pid=0. No filesystem target inspected: no /var/data access, no file
opens, directory enumeration, header/schema reads, or privilege changes.

A successful answer only reports the current thread's effective bit; it
never establishes the errno/cause of an earlier G1B-1 attempt.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys

STAGE = 53
GATE = "G1B1_PERMISSION_P0C"
LINUX_CAPABILITY_VERSION_3 = 0x20080522
SYS_CAPGET_X86_64 = 125
CAP_FOWNER_BIT = 3


class StopCheck(ValueError):
    """No sensitive exception is ever printed."""


class CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32),
                ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32)]


def mask_to_hint(effective: int) -> str:
    if not isinstance(effective, int) or effective < 0 or effective > 0xFFFFFFFF:
        raise StopCheck("mask_invalid")
    return "YES" if effective & (1 << CAP_FOWNER_BIT) else "NO"


def current_cap_fowner_effective(*, synthetic_mask: int | None = None) -> str:
    if synthetic_mask is not None:
        return mask_to_hint(synthetic_mask)

    # Do not fall back to /proc, libcap, platform-specific syscall numbers,
    # or other process/namespace inspection if our reviewed ABI is absent.
    if sys.platform != "linux" or os.name != "posix":
        raise StopCheck("platform_not_supported")
    if not hasattr(os, "uname") or os.uname().machine != "x86_64":
        raise StopCheck("architecture_not_reviewed")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        syscall = libc.syscall
        syscall.restype = ctypes.c_long  # syscall is variadic; no fixed argtypes
        header = CapHeader(LINUX_CAPABILITY_VERSION_3, 0)  # current thread
        data = (CapData * 2)()  # v3 kernel ABI requires two 32-bit words
        status = syscall(ctypes.c_long(SYS_CAPGET_X86_64),
                         ctypes.byref(header), ctypes.byref(data))
        if status != 0 or header.version != LINUX_CAPABILITY_VERSION_3:
            raise StopCheck("capget_unavailable")
        return mask_to_hint(int(data[0].effective))
    except Exception:
        # Includes ctypes.ArgumentError; never print a low-level error.
        raise StopCheck("capget_unavailable") from None


def report(*, synthetic_mask: int | None = None) -> dict:
    return {
        "stage": STAGE,
        "gate": GATE,
        "scope": "SYNTHETIC_ONLY" if synthetic_mask is not None else "AUTHORIZED_CURRENT_PROCESS_CAPABILITY_ONLY",
        "result": "CAP_FOWNER_EFFECTIVE_HINT_ONLY",
        "cap_fowner_effective": current_cap_fowner_effective(synthetic_mask=synthetic_mask),
        "filesystem_objects_inspected": 0,
        "directory_opens": 0,
        "candidate_file_opens": 0,
        "candidate_content_bytes_read": 0,
        "g1b1_retried": False,
        "source_writes": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--perform-stage53-p0c", required=True, action="store_true")
    parser.add_argument("--owner-authorized-stage53-p0c", required=True, action="store_true")
    parser.add_argument("--synthetic-effective-mask", type=int)
    args = parser.parse_args()
    try:
        if args.synthetic_effective_mask is not None and os.environ.get("GUITING_STAGE53_SYNTHETIC_ONLY") != "true":
            raise StopCheck("synthetic_gate_missing")
        data = report(synthetic_mask=args.synthetic_effective_mask)
    except Exception:
        print('{"stage":53,"gate":"G1B1_PERMISSION_P0C","result":"STOP_CAPABILITY_CHECK_UNAVAILABLE"}')
        return 2
    print(json.dumps(data, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
