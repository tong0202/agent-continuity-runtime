from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import threading
import time
import uuid


_REGISTRY_LOCK = threading.RLock()
_PROCESS_LEASES: dict[Path, tuple[str, int]] = {}


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class RuntimeLease:
    """Atomic, reentrant, single-host process lease."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.token = ""
        self.acquired = False

    def acquire(self) -> None:
        with _REGISTRY_LOCK:
            registered = _PROCESS_LEASES.get(self.path)
            if registered is not None:
                token, count = registered
                _PROCESS_LEASES[self.path] = (token, count + 1)
                self.token = token
                self.acquired = True
                return

            self.path.parent.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            payload = {
                "pid": os.getpid(),
                "token": token,
                "acquired_at_ns": time.time_ns(),
            }
            for _ in range(3):
                try:
                    descriptor = os.open(
                        self.path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except FileExistsError:
                    try:
                        owner = json.loads(self.path.read_text(encoding="utf-8"))
                        owner_pid = int(owner["pid"])
                    except (OSError, ValueError, KeyError, json.JSONDecodeError):
                        owner_pid = -1
                    if _process_alive(owner_pid):
                        raise RuntimeError(
                            f"runtime is already owned by process {owner_pid}: {self.path}"
                        )
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                _PROCESS_LEASES[self.path] = (token, 1)
                self.token = token
                self.acquired = True
                return
            raise RuntimeError(f"could not acquire runtime lease: {self.path}")

    def release(self) -> None:
        if not self.acquired:
            return
        with _REGISTRY_LOCK:
            registered = _PROCESS_LEASES.get(self.path)
            if registered is None or registered[0] != self.token:
                self.acquired = False
                return
            _, count = registered
            if count > 1:
                _PROCESS_LEASES[self.path] = (self.token, count - 1)
            else:
                _PROCESS_LEASES.pop(self.path, None)
                try:
                    owner = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    owner = {}
                if owner.get("token") == self.token:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
            self.acquired = False

    def __enter__(self) -> RuntimeLease:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
