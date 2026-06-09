#!/usr/bin/env python3
"""Platform abstraction layer — detects OS and provides appropriate implementations."""

import sys
import time
import threading
import subprocess

PLATFORM = sys.platform  # 'darwin', 'win32', 'linux'

_system_awake = threading.Event()
_system_awake.set()


def get_platform_name():
    if PLATFORM == "darwin":
        return "macOS"
    elif PLATFORM == "win32":
        return "Windows"
    else:
        return "Linux"


# === CLIPBOARD + PASTE ===

def get_selected_text():
    """Clear clipboard, simulate copy, and return the new clipboard content."""
    if PLATFORM == "darwin":
        marker = "---VOICE_INJECT_EMPTY---"
        
        prev_vol = "75"
        try:
            # Use a more robust AppleScript syntax
            res = subprocess.run(["osascript", "-e", "get alert volume of (get volume settings)"], capture_output=True, text=True)
            if res.returncode == 0:
                prev_vol = res.stdout.strip()
                subprocess.run(["osascript", "-e", "set volume alert volume 0"])
        except Exception:
            pass

        try:
            subprocess.run(["pbcopy"], input=marker.encode(), check=True)
            
            from pynput.keyboard import Key, Controller
            keyboard = Controller()
            with keyboard.pressed(Key.cmd):
                keyboard.press('c')
                keyboard.release('c')
                
            time.sleep(0.25) # Slightly longer for safety
            res = subprocess.run(["pbpaste"], capture_output=True, text=True)
            text = res.stdout.strip()
        finally:
            # Restore alert volume regardless of success
            try:
                subprocess.run(["osascript", "-e", f"set volume alert volume {prev_vol}"])
            except Exception:
                pass

        if text == marker:
            # Clear marker from clipboard so user doesn't accidentally paste it
            subprocess.run(["pbcopy"], input=b"", check=True)
            return ""
        return text
    elif PLATFORM == "win32":
        try:
            subprocess.run(["powershell", "-Command", "Set-Clipboard -Value ''"], check=True, shell=True)
            import ctypes
            user32 = ctypes.windll.user32
            VK_CONTROL = 0x11
            VK_C = 0x43
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            user32.keybd_event(VK_C, 0, 0, 0)
            user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.15)
            res = subprocess.run(["powershell", "-Command", "Get-Clipboard"], capture_output=True, text=True, shell=True)
            return res.stdout.strip()
        except Exception:
            return ""
    else:
        # Linux fallback (requires xclip)
        try:
            subprocess.run(["xclip", "-selection", "clipboard", "/dev/null"], check=True)
            subprocess.run(["xdotool", "key", "ctrl+c"], check=True)
            time.sleep(0.15)
            res = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True)
            return res.stdout.strip()
        except Exception:
            return ""


def copy_and_paste(text: str):
    """Copy text to clipboard and simulate paste keystroke."""
    if not text:
        return

    if not text.endswith(" "):
        text += " "

    if PLATFORM == "darwin":
        _paste_macos(text)
    elif PLATFORM == "win32":
        _paste_windows(text)
    else:
        _paste_linux(text)


def _paste_macos(text):
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
    except Exception as e:
        print(f"⚠️ Clipboard copy failed: {e}")
        return
    subprocess.run([
        "osascript", "-e",
        'tell application "System Events" to keystroke "v" using command down'
    ], capture_output=True, text=True)


def _paste_windows(text):
    try:
        subprocess.run(["clip"], input=text.encode(), check=True, shell=True)
    except Exception as e:
        print(f"⚠️ Clipboard copy failed: {e}")
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        VK_CONTROL = 0x11
        VK_V = 0x56
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_V, 0, 0, 0)
        user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    except Exception as e:
        print(f"⚠️ Paste simulation failed: {e}")


def _paste_linux(text):
    for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
        try:
            subprocess.run(cmd, input=text.encode(), check=True)
            break
        except FileNotFoundError:
            continue
    else:
        print("⚠️ No clipboard tool found (install xclip or xsel)")
        return
    try:
        subprocess.run(["xdotool", "key", "ctrl+v"], check=True)
    except FileNotFoundError:
        print("⚠️ xdotool not found, text copied but not pasted")


# === SLEEP/WAKE DETECTION ===

def start_sleep_wake_observer():
    """Start platform-appropriate sleep/wake observer. Non-blocking (runs in its own thread)."""
    if PLATFORM == "darwin":
        threading.Thread(target=_sleep_wake_macos, daemon=True).start()
    elif PLATFORM == "win32":
        threading.Thread(target=_sleep_wake_windows, daemon=True).start()
    else:
        threading.Thread(target=_sleep_wake_linux, daemon=True).start()


def _sleep_wake_macos():
    try:
        from Foundation import NSObject
        from AppKit import NSWorkspace, NSWorkspaceWillSleepNotification, NSWorkspaceDidWakeNotification
        from PyObjCTools import AppHelper
        import objc

        class SleepWakeObserver(NSObject):
            def handleSleep_(self, notification):
                print("💤 System going to sleep...")
                _system_awake.clear()

            def handleWake_(self, notification):
                print("☀️ System woke up, restarting in 2s...")
                time.sleep(2)
                _system_awake.set()

        observer = SleepWakeObserver.alloc().init()
        nc = NSWorkspace.sharedWorkspace().notificationCenter()
        nc.addObserver_selector_name_object_(
            observer, objc.selector(observer.handleSleep_, signature=b'v@:@'),
            NSWorkspaceWillSleepNotification, None)
        nc.addObserver_selector_name_object_(
            observer, objc.selector(observer.handleWake_, signature=b'v@:@'),
            NSWorkspaceDidWakeNotification, None)
        AppHelper.runConsoleEventLoop()
    except ImportError:
        print("⚠️ pyobjc not available, sleep/wake detection disabled")
    except Exception as e:
        print(f"⚠️ Sleep/wake observer failed: {e}")


def _sleep_wake_windows():
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        PBT_APMSUSPEND = 0x0004
        PBT_APMRESUMEAUTOMATIC = 0x0012
        WM_POWERBROADCAST = 0x0218

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST:
                if wparam == PBT_APMSUSPEND:
                    print("💤 System going to sleep...")
                    _system_awake.clear()
                elif wparam == PBT_APMRESUMEAUTOMATIC:
                    print("☀️ System woke up, restarting in 2s...")
                    time.sleep(2)
                    _system_awake.set()
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wnd_proc_cb = WNDPROC(wnd_proc)

        wc = wintypes.WNDCLASSW()
        wc.lpfnWndProc = wnd_proc_cb
        wc.lpszClassName = "VoiceInjectPowerWatcher"
        wc.hInstance = user32.GetModuleHandleW(None)

        if not user32.RegisterClassW(ctypes.byref(wc)):
            print("⚠️ Failed to register power watcher window class")
            return

        hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, "VoiceInject Power", 0,
            0, 0, 0, 0, None, None, wc.hInstance, None
        )
        if not hwnd:
            print("⚠️ Failed to create power watcher window")
            return

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    except Exception as e:
        print(f"⚠️ Windows sleep/wake detection failed: {e}")


def _sleep_wake_linux():
    """Monitor systemd's PrepareForSleep signal via D-Bus (if available)."""
    try:
        import dbus
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib

        DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()

        def on_prepare_for_sleep(sleeping):
            if sleeping:
                print("💤 System going to sleep...")
                _system_awake.clear()
            else:
                print("☀️ System woke up, restarting in 2s...")
                time.sleep(2)
                _system_awake.set()

        bus.add_signal_receiver(
            on_prepare_for_sleep,
            signal_name="PrepareForSleep",
            dbus_interface="org.freedesktop.login1.Manager",
            bus_name="org.freedesktop.login1"
        )

        loop = GLib.MainLoop()
        loop.run()
    except ImportError:
        print("⚠️ dbus/gi not available, sleep/wake detection disabled on Linux")
    except Exception as e:
        print(f"⚠️ Linux sleep/wake detection failed: {e}")


# === HOTKEY INFO ===

def get_hotkey_description():
    """Return human-readable hotkey description for current platform."""
    if PLATFORM == "darwin":
        return "Double-tap Left Option ⌥"
    else:
        return "Double-tap Left Alt"


def get_hotkey_key():
    """Return the pynput key to listen for."""
    from pynput import keyboard
    if PLATFORM == "darwin":
        return keyboard.Key.alt_l
    else:
        return keyboard.Key.alt_l
