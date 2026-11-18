"""Where the game is installed.

The setting "Game folder" wins when set. Otherwise the folder is detected, in this order:
1. the launcher's standard folder (%APPDATA%\\Splitscreen Studios\\Pirate Galaxy);
2. Windows' list of installed programs (an uninstall entry named Pirate Galaxy);
3. Desktop / Start menu shortcuts that point to the game's Launcher.exe;
4. a running game (the folder its java_vm belongs to).
If none is found, problem() says so and the user is asked to set the folder in Settings.
"""
import os
import pathlib
import re
import time
import winreg

from ..mem.process import _image_path, find_pids

DEFAULT = pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home() / "AppData" / "Roaming")) \
    / "Splitscreen Studios" / "Pirate Galaxy"
_setting = ""
_folder = None   # resolved folder, or None if not found
_how = ""        # how it was found (for the log)
_detected_at = 0.0
REDETECT_S = 30.0  # not found: look again at most this often (the game may be installed or started meanwhile)


def check(path):
    """None if the folder looks like a Pirate Galaxy install, else what is missing."""
    if not path or not str(path).strip():
        return "no folder given"
    path = pathlib.Path(str(path).strip().strip('"')).expanduser()
    if not path.is_dir():
        return "the folder does not exist"
    # Other programs have a Launcher.exe too (a HoYoPlay shortcut was taken for the game): the game's folder also
    # holds its own Java (java_vm) or Launcher.jar.
    if not (path / "Launcher.exe").exists():
        return "no Launcher.exe in it"
    if not (path / "java_vm" / "bin" / "javaw.exe").exists() and not (path / "Launcher.jar").exists():
        return "it has a Launcher.exe but is not the Pirate Galaxy folder (no java_vm or Launcher.jar)"
    return None


def _uninstall_entries():
    roots = ((winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"))
    for root, path in roots:
        try:
            with winreg.OpenKey(root, path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as entry:
                            name = winreg.QueryValueEx(entry, "DisplayName")[0]
                            if "pirate galaxy" not in str(name).lower():
                                continue
                            for value in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                try:
                                    yield str(winreg.QueryValueEx(entry, value)[0])
                                except OSError:
                                    pass
                    except OSError:
                        continue
        except OSError:
            continue


def _shortcut_targets():
    places = [pathlib.Path.home() / "Desktop",
              pathlib.Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
              pathlib.Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
              pathlib.Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu"
              / "Programs"]
    pattern = re.compile(r"[A-Za-z]:\\[^\x00<>|*?\"]{0,240}?\\Launcher\.exe", re.I)
    for place in places:
        try:
            links = list(place.rglob("*.lnk"))[:500]
        except OSError:
            continue
        for link in links:
            try:
                raw = link.read_bytes()[:20000]
            except OSError:
                continue
            for text in (raw.decode("latin-1"), raw.decode("utf-16-le", errors="ignore")):
                for match in pattern.findall(text):
                    yield match


def _running_game():
    for pid in find_pids(r"\java_vm\bin\javaw.exe"):
        path = _image_path(pid)
        if path:
            yield str(pathlib.Path(path).parent.parent.parent)


def _candidate_folder(text):
    """A folder from a path that may point at a file inside the install (Launcher.exe, an uninstaller)."""
    path = pathlib.Path(str(text).strip().strip('"').split('",')[0].split(",")[0])
    if not path.exists():
        # Shortcuts keep the path they were made with; one from an older Windows install pointed to another user's
        # profile (Users\<old name>\AppData\Roaming\...): try the same path in this user's AppData.
        lowered = str(path).lower()
        marker = "\\appdata\\roaming\\"
        if marker in lowered:
            rest = str(path)[lowered.index(marker) + len(marker):]
            path = pathlib.Path(os.environ.get("APPDATA", "")) / rest
    return path if path.is_dir() else path.parent


def detect():
    """(folder, how) or (None, why not)."""
    if check(DEFAULT) is None:
        return DEFAULT, "standard folder"
    for how, source in (("installed programs", _uninstall_entries), ("shortcut", _shortcut_targets),
                        ("running game", _running_game)):
        try:
            for text in source():
                folder = _candidate_folder(text)
                if check(folder) is None:
                    return folder, how
        except OSError:
            continue
    return None, "not found in the standard folder, the installed programs, shortcuts or a running game"


def set_folder(path):
    """The setting's value (empty: detect)."""
    global _setting, _folder, _how, _detected_at
    _detected_at = time.monotonic()
    _setting = str(path or "").strip().strip('"')
    if _setting:
        problem = check(_setting)
        _folder, _how = (pathlib.Path(_setting).expanduser(), "setting") if problem is None else (None, problem)
    else:
        _folder, _how = detect()


def problem():
    """None when the game folder is known, else a message for the user."""
    if _folder is not None:
        return None
    if _setting:
        return f"Game folder in Settings is not usable ({_how}): {_setting}"
    return "The game's folder was not found. Set it in Settings, Game start, Game folder."


def described():
    return f"{_folder} ({_how})" if _folder is not None else "unknown"


def folder():
    if _folder is None and not _setting and time.monotonic() - _detected_at > REDETECT_S:
        set_folder("")  # the game may have been installed or started since
    return _folder


def game_data():
    base = folder() or DEFAULT
    return base / "gamedata" / "Pirate Galaxy"


def launcher():
    return (folder() or DEFAULT) / "Launcher.exe"


def javaw():
    return (folder() or DEFAULT) / "java_vm" / "bin" / "javaw.exe"


def game_pids():
    """The game's Java processes (the client, and the launcher's own Java for a moment while starting)."""
    return find_pids(str(javaw()))


def launcher_pids():
    return find_pids(str(launcher()))


set_folder("")
