"""Bounded, read-only Windows process and Mono metadata access.

No remote allocation, writes, injected code, debugger attachment, or game calls.
The caller must verify the runtime binary before using this Mono layout.
"""

from __future__ import annotations

import ctypes
import struct
import sys
from ctypes import wintypes
from pathlib import Path


class MemoryUnavailable(ValueError):
    pass


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("usage", wintypes.DWORD),
        ("pid", wintypes.DWORD),
        ("heap", ctypes.c_size_t),
        ("module", wintypes.DWORD),
        ("threads", wintypes.DWORD),
        ("parent", wintypes.DWORD),
        ("priority", wintypes.LONG),
        ("flags", wintypes.DWORD),
        ("exe", wintypes.WCHAR * 260),
    ]


class _ModuleEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("module", wintypes.DWORD),
        ("pid", wintypes.DWORD),
        ("global_usage", wintypes.DWORD),
        ("usage", wintypes.DWORD),
        ("base", ctypes.c_void_p),
        ("bytes", wintypes.DWORD),
        ("handle", wintypes.HMODULE),
        ("name", wintypes.WCHAR * 256),
        ("path", wintypes.WCHAR * 260),
    ]


class ProcessMemory:
    ACCESS = 0x0010 | 0x1000 | 0x100000  # VM_READ, QUERY_LIMITED_INFORMATION, SYNCHRONIZE

    def __init__(self):
        self.handle = None
        if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise MemoryUnavailable("Live cooldowns require 64-bit Windows")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "ReadProcessMemory": (
                [
                    wintypes.HANDLE,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    ctypes.POINTER(ctypes.c_size_t),
                ],
                wintypes.BOOL,
            ),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        }
        for prefix, entry in (("Process", _ProcessEntry), ("Module", _ModuleEntry)):
            for suffix in ("FirstW", "NextW"):
                signatures[f"{prefix}32{suffix}"] = (
                    [wintypes.HANDLE, ctypes.POINTER(entry)],
                    wintypes.BOOL,
                )
        for name, (args, result) in signatures.items():
            fn = getattr(self.api, name)
            fn.argtypes, fn.restype = args, result
        games = [
            p
            for p in self._entries(2, 0, "Process", _ProcessEntry)
            if p.exe.lower() == "inkbound.exe"
        ]
        if not games:
            raise MemoryUnavailable("Waiting for Inkbound")
        if len(games) != 1:
            raise MemoryUnavailable("More than one Inkbound process is running")
        self.pid = games[0].pid
        self.handle = self.api.OpenProcess(self.ACCESS, False, self.pid)
        if not self.handle:
            raise MemoryUnavailable("Cannot read Inkbound memory")
        try:
            self.modules = {
                m.name.lower(): (m.base, Path(m.path))
                for m in self._entries(8 | 16, self.pid, "Module", _ModuleEntry)
            }
        except Exception:
            self.close()
            raise

    def _entries(self, flags, pid, prefix, entry_type):
        handle = self.api.CreateToolhelp32Snapshot(flags, pid)
        if handle == ctypes.c_void_p(-1).value:
            raise MemoryUnavailable("Game process is changing; retrying")
        try:
            entry = entry_type()
            entry.size = ctypes.sizeof(entry)
            ok = getattr(self.api, f"{prefix}32FirstW")(handle, ctypes.byref(entry))
            while ok:
                yield entry
                entry = entry_type()
                entry.size = ctypes.sizeof(entry)
                ok = getattr(self.api, f"{prefix}32NextW")(handle, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(handle)

    def alive(self):
        return bool(self.handle) and self.api.WaitForSingleObject(self.handle, 0) == 0x102

    def read(self, address: int, size: int) -> bytes:
        if not 0x10000 <= address < 0x7FFFFFFFFFFF or not 0 <= size <= 1024 * 1024:
            raise MemoryUnavailable("Invalid game memory range")
        if not size:
            return b""
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if (
            not self.api.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(count))
            or count.value != size
        ):
            raise MemoryUnavailable("Game state changed while reading")
        return buffer.raw

    def ptr(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def integer(self, address):
        return struct.unpack("<i", self.read(address, 4))[0]

    def cstring(self, address):
        chunks = b""
        for _ in range(3):
            chunk = self.read(address, min(128, 4096 - address % 4096))
            chunks += chunk
            if b"\0" in chunk:
                return chunks.split(b"\0", 1)[0].decode("utf-8")
            address += len(chunk)
        raise MemoryUnavailable("Invalid metadata name")

    def string(self, address, limit=160):
        if not address:
            return ""
        count = self.integer(address + 16)
        if not 0 <= count <= limit:
            raise MemoryUnavailable("Invalid game string")
        return self.read(address + 20, count * 2).decode("utf-16-le")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class MonoReader:
    """Layout verified against mono-2.0-bdwgc.dll SHA-256 in party_memory.py.

    Resolve managed field offsets by name at runtime; only Mono's native layout
    is fixed. Walk the registered assembly/class tables, never the process heap.
    """

    def __init__(self, memory, base):
        self.memory = memory
        self.base = base
        self._fields = {}

    def find_class(self, namespace, name):
        m = self.memory
        # RIP-relative global referenced by mono_assembly_foreach in this runtime.
        node = m.ptr(self.base + 0x72F030)
        seen = set()
        for _ in range(256):
            if not node or node in seen:
                break
            seen.add(node)
            image = m.ptr(m.ptr(node) + 0x60)
            image_name = m.cstring(m.ptr(image + 0x30))
            if image_name in ("Assembly-CSharp", "Assembly-CSharp.dll"):
                count = m.integer(image + 0x4E8)
                if not 0 < count <= 65536:
                    raise MemoryUnavailable("Invalid managed class table")
                table = m.read(m.ptr(image + 0x4F0), count * 8)
                for (klass,) in struct.iter_unpack("<Q", table):
                    chain = set()
                    for _ in range(1024):
                        if not klass or klass in chain:
                            break
                        chain.add(klass)
                        if (
                            m.cstring(m.ptr(klass + 0x48)) == name
                            and m.cstring(m.ptr(klass + 0x50)) == namespace
                        ):
                            return klass
                        klass = m.ptr(klass + 0x108)
            node = m.ptr(node + 8)
        raise MemoryUnavailable("Waiting for game state to load")

    def fields(self, klass, depth=0):
        if klass in self._fields:
            return self._fields[klass]
        if depth > 16:
            raise MemoryUnavailable("Invalid managed inheritance")
        m = self.memory
        count = m.integer(klass + 0x100)
        if not 0 <= count <= 1024:
            raise MemoryUnavailable("Invalid managed field table")
        fields = m.read(m.ptr(klass + 0x98), count * 32) if count else b""
        parent = m.ptr(klass + 0x30)
        result = dict(self.fields(parent, depth + 1)) if parent else {}
        for offset in range(0, len(fields), 32):
            name = m.cstring(struct.unpack_from("<Q", fields, offset + 8)[0])
            result[name] = struct.unpack_from("<i", fields, offset + 24)[0]
        self._fields[klass] = result
        return result

    def object_class(self, obj):
        return self.memory.ptr(self.memory.ptr(obj))

    def require_class(self, obj, name):
        if self.memory.cstring(self.memory.ptr(self.object_class(obj) + 0x48)) != name:
            raise MemoryUnavailable("Unexpected game object")
        return obj

    def field(self, obj, name):
        offset = self.fields(self.object_class(obj)).get(name)
        if offset is None or not 16 <= offset <= 65536:
            raise MemoryUnavailable(f"Game field unavailable: {name}")
        return obj + offset

    def ref(self, obj, name):
        return self.memory.ptr(self.field(obj, name))

    def integer(self, obj, name):
        return self.memory.integer(self.field(obj, name))

    def boolean(self, obj, name):
        value = self.memory.read(self.field(obj, name), 1)[0]
        if value not in (0, 1):
            raise MemoryUnavailable("Invalid game boolean")
        return bool(value)

    def static_ref(self, klass, name):
        m = self.memory
        info = m.ptr(klass + 0xD0)
        domains = int.from_bytes(m.read(info, 2), "little")
        slots = m.integer(klass + 0x5C)
        offset = self.fields(klass).get(name)
        if domains > 16 or not 0 <= slots <= 4096 or offset is None or not 0 <= offset <= 4096:
            raise MemoryUnavailable("Invalid managed static data")
        for index in range(domains + 1):
            vtable = m.ptr(info + 8 + index * 8)
            if vtable:
                data = m.ptr(vtable + 0x48 + slots * 8)
                obj = m.ptr(data + offset) if data else 0
                if obj and self.object_class(obj) == klass:
                    return obj
        raise MemoryUnavailable("Waiting for game initialization")

    def objects(self, obj, limit=1024):
        """Read a List<T> of references, rejecting concurrent structural changes."""
        m = self.memory
        header = m.read(obj + 16, 24)
        array, count, version = struct.unpack_from("<Qii", header)
        if not 0 <= count <= limit:
            raise MemoryUnavailable("Invalid game list length")
        if count and m.ptr(array + 24) < count:
            raise MemoryUnavailable("Invalid game array length")
        data = m.read(array + 32, count * 8) if count else b""
        if m.read(obj + 16, 24) != header:
            raise MemoryUnavailable("Game list changed while reading")
        return [v[0] for v in struct.iter_unpack("<Q", data)]
