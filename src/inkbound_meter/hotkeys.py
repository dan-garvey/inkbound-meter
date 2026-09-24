"""Global Windows hotkeys without installing a keyboard hook."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter


def parse_hotkey(value: str) -> tuple[int, int]:
    parts = value.upper().replace(" ", "").split("+")
    modifiers = 0x4000  # MOD_NOREPEAT
    keys = {"CTRL": 2, "ALT": 1, "SHIFT": 4}
    for token in parts[:-1]:
        if token not in keys:
            raise ValueError("Use Ctrl, Alt or Shift with a function key or letter.")
        modifiers |= keys[token]
    key = parts[-1]
    if key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        if key == "F12":
            raise ValueError("Windows reserves F12. Choose another key.")
        return modifiers, 0x70 + int(key[1:]) - 1
    if len(key) == 1 and key.isascii() and key.isalnum() and len(parts) > 1:
        return modifiers, ord(key)
    raise ValueError("Use F1–F24 (except F12), or Ctrl/Alt/Shift plus a letter or number.")


class Hotkeys(QAbstractNativeEventFilter):
    def __init__(self, application, bindings: dict[str, tuple[str, object]]) -> None:
        super().__init__()
        self.application = application
        self.callbacks = {}
        self.errors = []
        self.user32 = None
        if sys.platform != "win32":
            return
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.RegisterHotKey.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user32.RegisterHotKey.restype = wintypes.BOOL
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.UnregisterHotKey.restype = wintypes.BOOL
        application.installNativeEventFilter(self)
        for index, (name, (sequence, callback)) in enumerate(bindings.items(), 1):
            try:
                modifiers, key = parse_hotkey(sequence)
                if not self.user32.RegisterHotKey(None, index, modifiers, key):
                    raise ValueError(f"{sequence} is already in use")
                self.callbacks[index] = callback
            except ValueError as exc:
                self.errors.append(f"{name}: {exc}")

    def nativeEventFilter(self, event_type, message):
        if self.user32 and event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam in self.callbacks:  # WM_HOTKEY
                self.callbacks[msg.wParam]()
                return True, 0
        return False, 0

    def close(self):
        if self.user32:
            for index in self.callbacks:
                self.user32.UnregisterHotKey(None, index)
            self.application.removeNativeEventFilter(self)
            self.callbacks.clear()
