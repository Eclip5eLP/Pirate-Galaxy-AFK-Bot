"""Open a process for reading and read its memory. Windows only."""
import ctypes
import os
import struct
from ctypes import wintypes

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
LIST_MODULES_ALL = 3

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)

_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
_kernel32.ReadProcessMemory.restype = wintypes.BOOL
_psapi.EnumProcessModulesEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
]
_psapi.EnumProcessModulesEx.restype = wintypes.BOOL
_psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
_psapi.GetModuleFileNameExW.restype = wintypes.DWORD
_psapi.EnumProcesses.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_psapi.EnumProcesses.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


class MemoryReadError(Exception):
    """An address could not be read."""


def _image_path(pid):
    """Return a process's executable path, or None. Uses query-only rights, so it cannot read memory."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        path = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(path))
        if _kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return path.value
        return None
    finally:
        _kernel32.CloseHandle(handle)


def find_pids(executable_suffix):
    """Return ids of processes whose executable path ends with the given suffix, ignoring case."""
    capacity = 4096
    while True:
        pids = (wintypes.DWORD * capacity)()
        returned = wintypes.DWORD()
        if not _psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(returned)):
            raise OSError(ctypes.get_last_error(), "cannot list processes")
        count = returned.value // ctypes.sizeof(wintypes.DWORD)
        if count < capacity:
            break
        capacity *= 2
    suffix = executable_suffix.lower()
    found = []
    for pid in pids[:count]:
        path = _image_path(pid) if pid else None
        if path and path.lower().endswith(suffix):
            found.append(pid)
    return found


class Process:
    """A process opened with query and read rights only."""

    def __init__(self, pid):
        self.pid = pid
        self._handle = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self._handle:
            raise OSError(ctypes.get_last_error(), f"cannot open process {pid} for reading")
        self.reads = 0

    def close(self):
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def modules(self):
        """Return {lowercase file name: (base address, full path)}."""
        needed = wintypes.DWORD()
        capacity = 1024
        while True:
            handles = (ctypes.c_void_p * capacity)()
            ok = _psapi.EnumProcessModulesEx(
                self._handle, handles, ctypes.sizeof(handles), ctypes.byref(needed), LIST_MODULES_ALL,
            )
            if not ok:
                raise OSError(ctypes.get_last_error(), "cannot list modules")
            count = needed.value // ctypes.sizeof(ctypes.c_void_p)
            if count <= capacity:
                break
            capacity = count
        result = {}
        path = ctypes.create_unicode_buffer(1024)
        for base in handles[:count]:
            if base and _psapi.GetModuleFileNameExW(self._handle, base, path, len(path)):
                result[os.path.basename(path.value).lower()] = (base, path.value)
        return result

    def read(self, address, size):
        if size == 0:
            return b""
        # Buffers per call, never shared: two threads (bot and blueprint book) reading through one Process got each
        # other's byte counts and values, so reads failed or, worse, silently returned the other thread's data
        # (2026-11-12: "read failed" every 2 s, nonsense targets, ship died standing still).
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        ok = _kernel32.ReadProcessMemory(self._handle, address, buffer, size, ctypes.byref(count))
        self.reads += 1
        if not ok or count.value != size:
            raise MemoryReadError(f"cannot read {size} bytes at {address:#x}")
        return buffer.raw

    def u32(self, address):
        value = ctypes.c_uint32()
        count = ctypes.c_size_t()
        ok = _kernel32.ReadProcessMemory(self._handle, address, ctypes.byref(value), 4, ctypes.byref(count))
        self.reads += 1
        if not ok or count.value != 4:
            raise MemoryReadError(f"cannot read 4 bytes at {address:#x}")
        return value.value

    def u16(self, address):
        return struct.unpack("<H", self.read(address, 2))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def cstr(self, address, limit=512):
        """Read a NUL-terminated ASCII string, tolerating the end of a mapped page."""
        out = bytearray()
        while len(out) < limit:
            chunk_size = min(64, 4096 - ((address + len(out)) & 0xFFF))
            chunk = self.read(address + len(out), chunk_size)
            end = chunk.find(b"\0")
            if end >= 0:
                out += chunk[:end]
                return out.decode("latin-1")
            out += chunk
        raise MemoryReadError(f"unterminated string at {address:#x}")
