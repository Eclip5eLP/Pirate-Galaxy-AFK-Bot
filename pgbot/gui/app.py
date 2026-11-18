"""Control window: Farming tab (status, ship values, mode, live map, log) and Settings tab (all bot settings)."""
import base64
import math
import pathlib
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np

from ..bot.config import MODES, Config, settings
from ..data import myzen
from ..game import assets, location
from .monitor import LogEvent, Monitor
from .learned import LearnedPage
from .widgets import Bar, RoundedButton, TabBar, Toggle

APP_TITLE = "Pirate Galaxy AFK Farming Bot"
WINDOW = (940, 850)
MAP_SIZE = 340
LEFT_WIDTH = 290
STATUS_CHARS = 24  # longer status texts are shortened so the layout never moves
MAP_RADIUS_WORLD = 14000.0  # world units shown around the ship
COLORS = {
    "bg": "#0f1217", "panel": "#181c23", "border": "#232934", "text": "#eef1f6", "muted": "#7f8a9c",
    "subtle": "#5b6577", "hp": "#ef5b5b", "energy": "#4aa8ff", "ok": "#3ecf8e", "warning": "#f5b544",
    "error": "#ff6b6b", "pickup": "#7ee2a8", "death": "#ff3b3b", "blueprint": "#5aa9ff", "blueprint_rare": "#ffc940",
    "loot": "#d26bff", "info": "#b9c1ce", "start": "#2fb67c", "pause": "#d99a2b",
    "stop": "#e05252", "accent": "#4aa8ff", "input": "#20252e", "input_border": "#2f3644", "neutral": "#3a4250",
}
FONT = "Segoe UI"
FONT_BOLD = "Segoe UI Semibold"
ZONE_LABELS = {
    "planet": "On planet", "orbit": "In orbit", "hangar": "In hangar", "starmap": "Starmap",
    "loading": "Loading", "unknown": "Unknown screen",
}
MODE_LABELS = {key: label for key, label, _ in MODES}
MODE_BY_LABEL = {label: key for key, label, _ in MODES}
MODE_TEXT = {key: text for key, _, text in MODES}


# Creating this file stops a running bot (as pressing Stop does); the file is then removed.
STOP_FILE = pathlib.Path(__file__).resolve().parent.parent.parent / "logs" / "STOP"
STOP_ORBIT_FILE = STOP_FILE.with_name("STOP_ORBIT")  # creating it acts like the "Stop in orbit" button

ICON_DIR = pathlib.Path(__file__).resolve().parent / "assets"  # the game's skull (pg_logo*.png in PirateGalaxy.jar)


def _own_taskbar_entry():
    """Own taskbar group, so Windows shows the window's icon instead of python.exe's (call before the window)."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PirateGalaxyAFKBot.GUI")
    except (AttributeError, OSError):
        pass


class ControlApp:
    COLORS = COLORS
    LogEvent = LogEvent

    def _set_icon(self):
        """Skull icon for the title bar, taskbar and Alt+Tab (Tk's feather otherwise)."""
        try:
            self.root.iconbitmap(default=str(ICON_DIR / "icon.ico"))
            # Largest first: Windows scales the taskbar icon from these; the game's own skull is 64 px at most, the
            # 128/256 px ones are pixel-exact 2x/4x copies of it (smooth upscales looked blurry; user).
            self._icons = [tk.PhotoImage(file=str(ICON_DIR / f"icon{s}.png")) for s in (256, 128, 64, 32, 16)]
            self.root.iconphoto(True, *self._icons)
        except (tk.TclError, OSError):
            pass

    def __init__(self, bot=None):
        self.bot = bot
        self.config = bot.config if bot is not None else Config.load()
        self.monitor = Monitor()
        _own_taskbar_entry()
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self._set_icon()
        self.root.configure(bg=COLORS["bg"])
        self.root.geometry(f"{WINDOW[0]}x{WINDOW[1]}")
        self.root.resizable(False, False)
        self._dark_title_bar()
        self._style()
        self._map_cache = {}
        self._map_photo = None
        self._session = {"start": time.time(), "xp": None, "cryonite": None}
        self._setting_vars = {}
        self._mode_save_job = None
        self._data_events = queue.Queue()
        self.remote = None
        self._remote_error = ""
        self._build()
        self.monitor.start()
        self._start_remote()
        self.root.after(500, self._check_game_folder)
        # Clicking anywhere outside an input field leaves the field (Tk keeps the focus otherwise).
        self.root.bind_all("<Button-1>", self._click_away, add="+")
        self.root.after(200, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    # Look ---------------------------------------------------------------------------------------------------

    def _dark_title_bar(self):
        """Ask Windows for a dark title bar to match the window (Windows 10 20H1+)."""
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        except (AttributeError, OSError):
            pass

    def _style(self):
        """Dark flat look for the ttk input widgets."""
        style = ttk.Style(self.root)
        style.theme_use("clam")
        c = COLORS
        common = dict(fieldbackground=c["input"], background=c["input"], foreground=c["text"], arrowcolor=c["muted"],
                      bordercolor=c["input_border"], lightcolor=c["input"], darkcolor=c["input"],
                      insertcolor=c["text"], selectbackground=c["accent"], selectforeground="#ffffff", padding=5)
        for name in ("TCombobox", "TSpinbox", "TEntry"):
            style.configure(name, **common)
            style.map(name, fieldbackground=[("readonly", c["input"]), ("disabled", c["panel"])],
                      foreground=[("disabled", c["subtle"])], bordercolor=[("focus", c["accent"])],
                      lightcolor=[("focus", c["input"])], background=[("active", c["input"])],
                      arrowcolor=[("active", c["text"])])
        style.map("TCombobox", selectbackground=[("readonly", c["input"])], selectforeground=[("readonly", c["text"])])
        style.configure("Vertical.TScrollbar", troughcolor=c["bg"], background=c["neutral"], bordercolor=c["bg"],
                        arrowcolor=c["muted"], lightcolor=c["neutral"], darkcolor=c["neutral"], gripcount=0)
        style.map("Vertical.TScrollbar", background=[("active", c["subtle"])])
        self.root.option_add("*TCombobox*Listbox.background", c["input"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

    def _label(self, parent, text="", color="text", size=10, bold=False, bg="panel", **kwargs):
        return tk.Label(parent, text=text, bg=COLORS[bg], fg=COLORS[color],
                        font=(FONT_BOLD if bold else FONT, size), anchor="w", justify="left", **kwargs)

    def _card(self, parent, title=None, **pack):
        outer = tk.Frame(parent, bg=COLORS["border"])
        outer.pack(**pack)
        inner = tk.Frame(outer, bg=COLORS["panel"], padx=16, pady=14)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        if title:
            self._label(inner, title.upper(), "subtle", 8, bold=True).pack(anchor="w", pady=(0, 8))
        return inner

    # Layout -------------------------------------------------------------------------------------------------

    def _build(self):
        header = tk.Frame(self.root, bg=COLORS["bg"], padx=18, pady=14)
        header.pack(fill="x")
        title = tk.Frame(header, bg=COLORS["bg"])
        title.pack(side="left")
        self._label(title, APP_TITLE, "text", 15, bold=True, bg="bg").pack(anchor="w")
        self.connection = self._label(title, "Searching for the game...", "muted", 9, bg="bg")
        self.connection.pack(anchor="w")
        buttons = tk.Frame(header, bg=COLORS["bg"])
        buttons.pack(side="right")
        self.start_button = RoundedButton(buttons, "Start", self._start, COLORS["start"], COLORS["bg"])
        self.pause_button = RoundedButton(buttons, "Pause", self._pause, COLORS["pause"], COLORS["bg"])
        self.orbit_button = RoundedButton(buttons, "Stop in orbit", self._stop_in_orbit, COLORS["neutral"],
                                          COLORS["bg"], width=150)
        self.stop_button = RoundedButton(buttons, "Stop", self._stop, COLORS["stop"], COLORS["bg"])
        for button in (self.start_button, self.pause_button, self.orbit_button, self.stop_button):
            button.pack(side="left", padx=(8, 0))

        tabs_row = tk.Frame(self.root, bg=COLORS["bg"], padx=18)
        tabs_row.pack(fill="x", pady=(0, 12))
        self.pages = {}
        container = tk.Frame(self.root, bg=COLORS["bg"], padx=18)
        container.pack(fill="both", expand=True, pady=(0, 18))
        self.pages["farm"] = tk.Frame(container, bg=COLORS["bg"])
        self.pages["settings"] = tk.Frame(container, bg=COLORS["bg"])
        self.pages["learned"] = tk.Frame(container, bg=COLORS["bg"])
        self.pages["remote"] = tk.Frame(container, bg=COLORS["bg"])
        self._build_farm(self.pages["farm"])
        self._build_settings(self.pages["settings"])
        self.learned = LearnedPage(self, self.pages["learned"])
        self._build_remote(self.pages["remote"])
        self.tabs = TabBar(tabs_row, [("farm", "Farming"), ("settings", "Settings"), ("learned", "Learned"),
                                      ("remote", "Remote")], self._show_page, COLORS, FONT, FONT_BOLD)
        self.tabs.pack(anchor="w")
        self.tabs.select("farm")
        self._refresh_buttons()

    def _show_page(self, key):
        for name, page in self.pages.items():
            if name == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        if key == "learned":
            self.root.after(50, self.learned.refresh)

    def _build_farm(self, page):
        left = tk.Frame(page, bg=COLORS["bg"], width=LEFT_WIDTH)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        status_card = self._card(left, "Status", fill="x")
        self.status_dot = tk.Canvas(status_card, width=12, height=12, bg=COLORS["panel"], highlightthickness=0)
        self.status_dot.pack(side="left", anchor="n", pady=(9, 0), padx=(0, 8))
        self._dot = self.status_dot.create_oval(1, 1, 11, 11, fill=COLORS["subtle"], outline="")
        status_text = tk.Frame(status_card, bg=COLORS["panel"])
        status_text.pack(side="left", fill="x")
        self.status = self._label(status_text, "Not connected", "text", 13, bold=True)
        self.status.pack(anchor="w")
        self.location = self._label(status_text, "", "muted", 10)
        self.location.pack(anchor="w")

        self._build_mode(left)

        ship_card = self._card(left, "Ship", fill="x", pady=(12, 0))
        self.hp_text = self._meter_row(ship_card, "Hull")
        self.hp_bar = Bar(ship_card, COLORS["hp"], COLORS["panel"])
        self.hp_bar.pack(anchor="w", pady=(4, 12))
        self.energy_text = self._meter_row(ship_card, "Energy")
        self.energy_bar = Bar(ship_card, COLORS["energy"], COLORS["panel"])
        self.energy_bar.pack(anchor="w", pady=(4, 0))

        stats_card = self._card(left, "Session", fill="x", pady=(12, 0))
        self.cryonite_text = self._stat_row(stats_card, "Cryonite")
        self.level_text = self._stat_row(stats_card, "Level")
        self.xp_text = self._stat_row(stats_card, "XP")
        self.ship_text = self._stat_row(stats_card, "Ship")
        self.read_text = self._label(left, "", "subtle", 8, bg="bg")
        self.read_text.pack(anchor="w", pady=(6, 0))

        right = tk.Frame(page, bg=COLORS["bg"])
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        map_card = self._card(right, "Map", anchor="nw", fill="x")
        self.map = tk.Canvas(map_card, width=MAP_SIZE, height=MAP_SIZE, bg=COLORS["bg"], highlightthickness=0)
        self.map.pack(anchor="w")
        legend = tk.Frame(map_card, bg=COLORS["panel"])
        legend.pack(anchor="w", pady=(10, 0))
        for name, color in (("You", "#5aff5a"), ("Aggressive", "#e63c3c"), ("Neutral", "#e6a03c"),
                            ("Energy", "#3cc8ff"), ("Loot", COLORS["loot"]),
                            ("Gold BP", COLORS["blueprint_rare"]), ("Player", "#ffffff")):
            dot = tk.Canvas(legend, width=8, height=8, bg=COLORS["panel"], highlightthickness=0)
            dot.create_oval(0, 0, 7, 7, fill=color, outline="")
            dot.pack(side="left", padx=(0, 4))
            self._label(legend, name, "muted", 8).pack(side="left", padx=(0, 10))

        log_card = self._card(right, "Log", fill="both", expand=True, pady=(12, 0))
        self.log = tk.Text(log_card, height=8, width=40, bg=COLORS["panel"], fg=COLORS["info"], relief="flat", bd=0,
                           font=("Cascadia Mono", 9), wrap="word", state="disabled", highlightthickness=0,
                           spacing1=2, spacing3=2, cursor="arrow")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("time", foreground=COLORS["subtle"])
        for level in ("pickup", "warning", "error", "info", "death", "blueprint", "blueprint_rare"):
            self.log.tag_configure(level, foreground=COLORS[level])
        self.log.tag_configure("death", font=("Cascadia Mono", 9, "bold"))

    def _build_mode(self, parent):
        card = self._card(parent, "Mode", fill="x", pady=(12, 0))
        self.mode_var = tk.StringVar(value=MODE_LABELS.get(self.config.mode, MODES[0][1]))
        combo = ttk.Combobox(card, textvariable=self.mode_var, values=[label for _, label, _ in MODES],
                             state="readonly", font=(FONT, 10), width=28)
        combo.pack(fill="x")
        combo.bind("<<ComboboxSelected>>", lambda _: self._mode_changed())
        self.mode_text = self._label(card, "", "muted", 9, wraplength=LEFT_WIDTH - 40)
        self.mode_text.pack(anchor="w", pady=(6, 0), fill="x")
        # Fixed-height area for the mode's own input, so switching modes never moves the cards below.
        self.mode_extra = tk.Frame(card, bg=COLORS["panel"], height=112)
        self.mode_extra.pack(fill="x", pady=(8, 0))
        self.mode_extra.pack_propagate(False)

        self.enemy_frame = tk.Frame(self.mode_extra, bg=COLORS["panel"])
        self._label(self.enemy_frame, "Full enemy names, comma separated", "subtle", 8).pack(anchor="w")
        self.enemy_var = tk.StringVar(value=self.config.seek_enemies)
        self.enemy_combo = ttk.Combobox(self.enemy_frame, textvariable=self.enemy_var, font=(FONT, 10),
                                        values=assets.enemy_names())
        self.enemy_combo.pack(fill="x", pady=(3, 0))
        self.enemy_combo.bind("<KeyRelease>", self._filter_enemies)
        self.enemy_combo.bind("<<ComboboxSelected>>", self._enemy_picked)
        self.enemy_var.trace_add("write", lambda *_: self._mode_input_changed())
        self._label(self.enemy_frame, "Example: Mantis Harvester, Imperial Signor", "subtle", 8).pack(
            anchor="w", pady=(3, 0))
        self._enemy_before_pick = self.config.seek_enemies

        self.bp_frame = tk.Frame(self.mode_extra, bg=COLORS["panel"])
        self._label(self.bp_frame, "Blueprint (type to search)", "subtle", 8).pack(anchor="w")
        self.bp_var = tk.StringVar(value=self.config.seek_blueprint)
        self.bp_combo = ttk.Combobox(self.bp_frame, textvariable=self.bp_var, font=(FONT, 10))
        self.bp_combo.pack(fill="x", pady=(3, 0))
        self.bp_combo.bind("<KeyRelease>", self._filter_blueprints)
        self.bp_combo.bind("<<ComboboxSelected>>", lambda _: self._mode_input_changed())
        self.bp_info = self._label(self.bp_frame, "", "muted", 8, wraplength=LEFT_WIDTH - 40)
        self.bp_info.pack(anchor="w", pady=(3, 0), fill="x")
        self.bp_var.trace_add("write", lambda *_: self._mode_input_changed())

        self.fly_frame = tk.Frame(self.mode_extra, bg=COLORS["panel"])
        self._label(self.fly_frame, "Planet (type to search)", "subtle", 8).pack(anchor="w")
        self.fly_var = tk.StringVar(value=self.config.fly_planet)
        self.fly_combo = ttk.Combobox(self.fly_frame, textvariable=self.fly_var, font=(FONT, 10),
                                      values=self._planet_choices())
        self.fly_combo.pack(fill="x", pady=(3, 0))
        self.fly_combo.bind("<KeyRelease>", self._filter_planets)
        self.fly_combo.bind("<<ComboboxSelected>>", lambda _: self._mode_input_changed())
        self.fly_info = self._label(self.fly_frame, "", "muted", 8, wraplength=LEFT_WIDTH - 40)
        self.fly_info.pack(anchor="w", pady=(3, 0), fill="x")
        self.fly_var.trace_add("write", lambda *_: self._mode_input_changed())

        self.sys_frame = tk.Frame(self.mode_extra, bg=COLORS["panel"])
        self._label(self.sys_frame, "Star system (empty: the current one)", "subtle", 8).pack(anchor="w")
        self.sys_var = tk.StringVar(value=self.config.bp_system)
        self.sys_combo = ttk.Combobox(self.sys_frame, textvariable=self.sys_var, font=(FONT, 10),
                                      values=[name for name, _, _ in myzen.SYSTEM_LEVELS])
        self.sys_combo.pack(fill="x", pady=(3, 0))
        self.sys_combo.bind("<<ComboboxSelected>>", lambda _: self._mode_input_changed())
        self._label(self.sys_frame, "Ship (empty: the current one)", "subtle", 8).pack(anchor="w", pady=(4, 0))
        self.ship_var = tk.StringVar(value=self.config.ship)
        self.ship_combo = ttk.Combobox(self.sys_frame, textvariable=self.ship_var, font=(FONT, 10),
                                       values=self._ship_choices(), postcommand=self._refresh_ship_choices)
        self.ship_combo.pack(fill="x", pady=(3, 0))
        self.ship_combo.bind("<<ComboboxSelected>>", lambda _: self._mode_input_changed())
        self.sys_var.trace_add("write", lambda *_: self._mode_input_changed())
        self.ship_var.trace_add("write", lambda *_: self._mode_input_changed())
        self._show_mode()

    @staticmethod
    def _ship_choices():
        """Owned ships as last read in a hangar (state.json), shown as "<own name> (<ship name>)"."""
        try:
            import json
            from ..bot.core import STATE_FILE
            from ..game import ships
            entries = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("owned_ships") or []
            return [""] + [ships.label(e) for e in entries]
        except (OSError, ValueError, ImportError):
            return [""]

    def _refresh_ship_choices(self):
        self.ship_combo.configure(values=self._ship_choices())

    def _meter_row(self, parent, label):
        row = tk.Frame(parent, bg=COLORS["panel"])
        row.pack(fill="x")
        self._label(row, label, "muted", 9).pack(side="left")
        value = self._label(row, "-", "text", 10, bold=True)
        value.pack(side="right")
        return value

    def _stat_row(self, parent, label):
        row = tk.Frame(parent, bg=COLORS["panel"])
        row.pack(fill="x", pady=2)
        self._label(row, label, "muted", 9).pack(side="left")
        value = self._label(row, "-", "text", 10)
        value.pack(side="right", padx=(24, 0))
        return value

    # Mode ---------------------------------------------------------------------------------------------------

    def _mode_changed(self):
        self.config.mode = MODE_BY_LABEL[self.mode_var.get()]
        self._show_mode()
        self._save_mode_soon()

    def _show_mode(self):
        mode = self.config.mode
        self.mode_text.configure(text=MODE_TEXT[mode])
        self.enemy_frame.pack_forget()
        self.bp_frame.pack_forget()
        self.fly_frame.pack_forget()
        self.sys_frame.pack_forget()
        if mode == "bp_completion":
            self.sys_frame.pack(fill="x")
        if mode == "fly":
            self.fly_frame.pack(fill="x")
            self._show_fly_info()
        elif mode == "seek_enemy":
            self.enemy_frame.pack(fill="x")
        elif mode == "seek_blueprint":
            self.bp_frame.pack(fill="x")
            self._show_blueprint_info()

    def _mode_input_changed(self):
        self.config.seek_enemies = self.enemy_var.get()
        self.config.seek_blueprint = self.bp_var.get().strip()
        self.config.fly_planet = self.fly_var.get().strip()
        self.config.bp_system = self.sys_var.get().strip()
        self.config.ship = self.ship_var.get().strip()
        self._show_blueprint_info()
        self._show_fly_info()
        self._save_mode_soon()

    def _save_mode_soon(self):
        """Mode inputs apply at once (a running bot picks them up) and are saved shortly after typing stops."""
        if self._mode_save_job is not None:
            self.root.after_cancel(self._mode_save_job)
        self._mode_save_job = self.root.after(800, self._save_config)

    def _filter_enemies(self, event):
        """Filter the list by the name being typed (the part after the last comma)."""
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        text = self.enemy_var.get()
        self._enemy_before_pick = text
        part = text.split(",")[-1].strip().lower()
        names = assets.enemy_names()
        self.enemy_combo.configure(values=[n for n in names if part in n.lower()] if part else names)

    def _enemy_picked(self, _):
        """A picked name replaces the part being typed, so several names can be chained with commas."""
        picked = self.enemy_var.get()
        earlier = [p.strip() for p in self._enemy_before_pick.split(",")[:-1] if p.strip()]
        text = ", ".join(earlier + [picked])
        self._enemy_before_pick = text + ", "
        self.enemy_var.set(text)
        self.enemy_combo.icursor("end")

    @staticmethod
    def _planet_choices():
        """Planets from the site data in system progression order (Sirius left out: no gate reaches it)."""
        data = myzen.load()
        if not data:
            return []
        order = {name: i for i, (name, _, _) in enumerate(myzen.SYSTEM_LEVELS)}
        planets = [p for p in data.get("planets", {}).values() if p.get("system") not in ("Sirius",)]
        planets.sort(key=lambda p: (order.get(p.get("system"), 99), p.get("label", "")))
        return [p["label"] for p in planets]

    def _filter_planets(self, event):
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        text = myzen.normalize(self.fly_var.get().strip())
        names = self._planet_choices()
        # Accents ignored: "venar" finds Vénar.
        self.fly_combo.configure(values=[n for n in names if text in myzen.normalize(n)] if text else names)

    def _show_fly_info(self):
        if self.config.mode != "fly":
            return
        info = myzen.planet_info(self.config.fly_planet) if self.config.fly_planet else None
        if info is None:
            self.fly_info.configure(text="Pick a planet from the list", fg=COLORS["muted"])
        else:
            self.fly_info.configure(text=f"{info['label']} in {info.get('system', '?')}; the bot stops in its orbit",
                                    fg=COLORS["muted"])

    def _filter_blueprints(self, event):
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        text = self.bp_var.get().strip().lower()
        names = myzen.blueprint_names()
        matches = [n for n in names if text in n.lower()] if text else names
        self.bp_combo.configure(values=matches[:300])

    def _show_blueprint_info(self):
        if self.config.mode != "seek_blueprint":
            return
        if myzen.load() is None:
            self.bp_info.configure(text="No site data yet: Settings, Update game data", fg=COLORS["warning"])
            return
        name = self.config.seek_blueprint
        drops = myzen.blueprint_drops(name) if name else []
        if not name:
            self.bp_info.configure(text="", fg=COLORS["muted"])
        elif not drops:
            self.bp_info.configure(text="Not dropped by enemies, or unknown", fg=COLORS["warning"])
        else:
            drop = drops[0]
            low, high = drop["enemy_levels"] or (None, None)
            level = (f"R{low}" + (f"-{high}" if high != low else "") + " ") if low is not None else ""
            text = f"{level}{' / '.join(myzen.full_enemy_names(drop))} on {', '.join(drop['planets']) or '?'}"
            if len(drops) > 1:
                text += f"  (+{len(drops) - 1} more)"
            self.bp_info.configure(text=text, fg=COLORS["muted"])

    # Settings -----------------------------------------------------------------------------------------------

    def _build_settings(self, page):
        footer = tk.Frame(page, bg=COLORS["bg"])
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        RoundedButton(footer, "Save", self._save_settings, COLORS["accent"], COLORS["bg"]).pack(side="left")
        RoundedButton(footer, "Undo changes", self._load_settings_into_form, COLORS["neutral"], COLORS["bg"],
                      width=130).pack(side="left", padx=(8, 0))
        RoundedButton(footer, "Defaults", self._defaults_into_form, COLORS["neutral"], COLORS["bg"]
                      ).pack(side="left", padx=(8, 0))
        self.settings_note = self._label(footer, "", "muted", 9, bg="bg")
        self.settings_note.pack(side="left", padx=(14, 0))

        # Scrollable column of setting groups.
        holder = tk.Frame(page, bg=COLORS["bg"])
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=COLORS["bg"])
        window = canvas.create_window(0, 0, window=body, anchor="nw")
        body.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width - 12))
        canvas.bind_all("<MouseWheel>", lambda e: self._wheel(canvas, e))
        self._settings_canvas = canvas

        self._build_data_card(body)
        self._field_labels = {}
        for group, fields in settings():
            card = self._card(body, group, fill="x", pady=(12, 0))
            rows = tk.Frame(card, bg=COLORS["panel"])
            rows.pack(fill="x")
            rows.columnconfigure(0, weight=1)
            for row, item in enumerate(fields):
                self._setting_row(rows, row, item)
        self._load_settings_into_form()

    def _wheel(self, canvas, event):
        if self.pages["settings"].winfo_ismapped():
            canvas.yview_scroll(int(-event.delta / 120), "units")
        elif self.pages["learned"].winfo_ismapped():
            self.learned.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _build_data_card(self, parent):
        card = self._card(parent, "Game data", fill="x")
        row = tk.Frame(card, bg=COLORS["panel"])
        row.pack(fill="x")
        text = tk.Frame(row, bg=COLORS["panel"])
        text.pack(side="left", fill="x", expand=True)
        self._label(text, "Blueprint drops, energy fields and landing zones", "text", 10).pack(anchor="w")
        self.data_note = self._label(text, "", "muted", 9)
        self.data_note.pack(anchor="w")
        self.data_button = RoundedButton(row, "Update", self._update_data, COLORS["accent"], COLORS["panel"])
        self.data_button.pack(side="right")
        self._label(card, "From The Hitchhiker's Guide to Pirate Galaxy (prelude-myzen.co.uk). Used by Seek Blueprint "
                          "and for energy fields the bot has not seen yet.", "subtle", 8,
                    wraplength=760).pack(anchor="w", pady=(8, 0))
        self._show_data_note()

    def _show_data_note(self):
        data = myzen.load()
        if data is None:
            self.data_note.configure(text="Not downloaded yet", fg=COLORS["warning"])
        else:
            count = len(myzen.blueprint_names())
            self.data_note.configure(text=f"Updated {data['fetched']}  ·  {count:,} blueprints with enemy drops  ·  "
                                          f"{len(data['planets'])} planets", fg=COLORS["muted"])

    def _update_data(self):
        self.data_button.set_enabled(False)
        self.data_note.configure(text="Downloading...", fg=COLORS["muted"])

        def work():
            try:
                myzen.update(lambda text: self._data_events.put(("progress", text)))
                self._data_events.put(("done", None))
            except Exception as error:  # network or parse errors are shown, not raised
                self._data_events.put(("error", f"{type(error).__name__}: {error}"))
        threading.Thread(target=work, name="site-data", daemon=True).start()

    def _poll_data(self):
        while True:
            try:
                kind, text = self._data_events.get_nowait()
            except queue.Empty:
                return
            if kind == "progress":
                self.data_note.configure(text=text, fg=COLORS["muted"])
            elif kind == "done":
                self.data_button.set_enabled(True)
                self._show_data_note()
                self._show_blueprint_info()
                self._append_log(LogEvent("info", "Game data updated"))
            else:
                self.data_button.set_enabled(True)
                self.data_note.configure(text=f"Update failed: {text}", fg=COLORS["error"])

    def _setting_row(self, card, row, item):
        meta = item.metadata
        text = tk.Frame(card, bg=COLORS["panel"])
        text.grid(row=row, column=0, sticky="w", pady=5)
        label = self._label(text, meta["label"], "text", 10)
        label.pack(anchor="w")
        if meta["hint"]:
            self._label(text, meta["hint"], "subtle", 8).pack(anchor="w")
        self._field_labels[item.name] = label
        default = item.default
        if isinstance(default, bool):
            var = tk.BooleanVar()
            Toggle(card, var, COLORS["accent"], COLORS["panel"]).grid(row=row, column=1, sticky="e", padx=(12, 0))
        elif isinstance(default, str):
            var = tk.StringVar()
            ttk.Entry(card, textvariable=var, width=44, font=(FONT, 10)).grid(row=row, column=1, sticky="e",
                                                                              padx=(12, 0))
        else:
            var = tk.StringVar()
            low, high, step = meta["min"], meta["max"], meta["step"] or 1
            if meta["percent"]:
                low, high, step = low * 100, high * 100, step * 100
            spin = ttk.Spinbox(card, textvariable=var, from_=low, to=high, increment=step, width=10,
                               font=(FONT, 10), justify="right")
            spin.grid(row=row, column=1, sticky="e", padx=(12, 0))
        self._label(card, meta["unit"], "muted", 9, width=6).grid(row=row, column=2, sticky="w", padx=(6, 0))
        var.trace_add("write", lambda *_: self._settings_dirty())
        self._setting_vars[item.name] = (var, item)

    def _settings_dirty(self):
        self.settings_note.configure(text="Unsaved changes", fg=COLORS["warning"])

    def _put_form(self, config):
        for name, (var, item) in self._setting_vars.items():
            value = getattr(config, name)
            if isinstance(item.default, bool):
                var.set(bool(value))
            elif isinstance(item.default, str):
                var.set(value)
            elif item.metadata["percent"]:
                var.set(f"{value * 100:g}")
            else:
                var.set(f"{value:g}" if isinstance(value, float) else str(value))

    def _load_settings_into_form(self):
        self._put_form(self.config)
        self.settings_note.configure(text="", fg=COLORS["muted"])

    def _defaults_into_form(self):
        self._put_form(Config())
        self.settings_note.configure(text="Defaults loaded, not saved yet", fg=COLORS["warning"])

    def _save_settings(self):
        values, errors = {}, []
        for name, (var, item) in self._setting_vars.items():
            label = self._field_labels[name]
            label.configure(fg=COLORS["text"])
            if isinstance(item.default, bool):
                values[name] = bool(var.get())
                continue
            if name == "game_folder":
                folder = var.get().strip().strip('"')
                problem = location.check(folder) if folder else None
                if problem:
                    errors.append(f"{item.metadata['label']} ({problem})")
                    label.configure(fg=COLORS["error"])
                    continue
                values[name] = folder
                continue
            if isinstance(item.default, str):
                values[name] = ", ".join(part.strip() for part in var.get().split(",") if part.strip())
                continue
            meta = item.metadata
            try:
                number = float(var.get().replace(",", "."))
            except ValueError:
                errors.append(meta["label"])
                label.configure(fg=COLORS["error"])
                continue
            if meta["percent"]:
                number /= 100
            low, high = meta["min"], meta["max"]
            if (low is not None and number < low) or (high is not None and number > high):
                errors.append(meta["label"])
                label.configure(fg=COLORS["error"])
                continue
            values[name] = int(round(number)) if isinstance(item.default, int) else number
        if not errors and values.get("refill_until", 1) <= values.get("refill_below", 0):
            errors.append("Refill up to energy must be above Refill below energy")
        if errors:
            self.settings_note.configure(text="Check: " + ", ".join(errors), fg=COLORS["error"])
            return
        for name, value in values.items():
            setattr(self.config, name, value)  # in place: a running bot uses the new values at once
        self._save_config()
        self._put_form(self.config)
        self.settings_note.configure(text="Saved", fg=COLORS["ok"])
        self._check_game_folder()

    INPUT_CLASSES = ("TEntry", "TCombobox", "TSpinbox", "Entry", "Spinbox", "Listbox")

    def _click_away(self, event):
        widget = event.widget
        if isinstance(widget, str) or widget.winfo_class() in self.INPUT_CLASSES:
            return  # a click into a field (or a dropdown list) keeps working as usual
        focused = self.root.focus_get()
        if focused is not None and focused.winfo_class() in self.INPUT_CLASSES:
            try:
                focused.selection_clear()
            except tk.TclError:
                pass
            self.root.focus_set()

    def _check_game_folder(self):
        problem = location.problem()
        if problem:
            self._append_log(LogEvent("warning", problem))
        else:
            self._append_log(LogEvent("info", f"Game folder: {location.described()}"))

    def _save_config(self):
        self._mode_save_job = None
        try:
            self.config.save()
        except OSError as error:
            self._append_log(LogEvent("error", f"Could not save settings: {error}"))

    # Bot controls -------------------------------------------------------------------------------------------

    def _refresh_buttons(self):
        running = self.bot is not None and self.bot.running
        paused = running and self.bot.paused
        self.start_button.set_enabled(self.bot is not None and not running)
        self.pause_button.set_text("Resume" if paused else "Pause")
        self.pause_button.set_enabled(running)
        self.stop_button.set_enabled(running)
        orbit = running and getattr(self.bot, "stop_in_orbit", False)
        self.orbit_button.set_text("Keep going" if orbit else "Stop in orbit")
        self.orbit_button.set_enabled(running)

    def _start(self):
        if self.bot is not None:
            self.bot.start()
        self._refresh_buttons()

    def _pause(self):
        if self.bot is not None:
            self.bot.toggle_pause()
        self._refresh_buttons()

    def _stop_in_orbit(self):
        if self.bot is not None and self.bot.running:
            self.bot.set_stop_in_orbit(not self.bot.stop_in_orbit)
        self._refresh_buttons()

    def _stop(self):
        if self.bot is not None:
            self.bot.stop()
        self._refresh_buttons()

    def _close(self):
        self._stop()
        if self._mode_save_job is not None:
            self._save_config()
        self.monitor.stop()
        self._remote_call(lambda r: r.stop())
        self.root.destroy()

    # Updates ------------------------------------------------------------------------------------------------

    def _append_log(self, event):
        self._remote_call(lambda r: r.log(event))
        stamp = time.strftime("%H:%M:%S", time.localtime(event.time))
        self.log.configure(state="normal")
        self.log.insert("end", f"{stamp}  ", "time")
        if event.level.startswith("blueprint") and event.text.count("'") >= 2:
            # Only the quoted blueprint name is coloured (blue normal, gold rare); the rest is a normal pickup line.
            head, name, tail = event.text.split("'", 2)
            self.log.insert("end", head + "'", "pickup")
            self.log.insert("end", name, event.level)
            self.log.insert("end", f"'{tail}\n", "pickup")
        else:
            self.log.insert("end", f"{event.text}\n", event.level)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _tick(self):
        while True:
            try:
                event = self.monitor.events.get_nowait()
            except queue.Empty:
                break
            self._append_log(event)
        if self.bot is not None:
            for event in self.bot.drain_events():
                self._append_log(event)
        self._poll_data()
        if self.bot is not None and self.bot.running and STOP_FILE.exists():
            # Stop request from outside (tools or an assistant managing the run): the same as pressing Stop.
            STOP_FILE.unlink(missing_ok=True)
            self._stop()
        if self.bot is not None and self.bot.running and STOP_ORBIT_FILE.exists():
            # The same as pressing "Stop in orbit" (finish up, take off, stop in orbit).
            STOP_ORBIT_FILE.unlink(missing_ok=True)
            if not self.bot.stop_in_orbit:
                self._stop_in_orbit()
        snapshot = self.monitor.latest
        self._show(snapshot)
        self._refresh_buttons()
        self._remote_tick(snapshot)
        self.learned.tick()
        self.root.after(250, self._tick)

    def _set_status(self, text, color):
        self._status_full = text
        if len(text) > STATUS_CHARS:
            text = text[:STATUS_CHARS - 1].rstrip() + "…"
        self.status.configure(text=text)
        self.status_dot.itemconfigure(self._dot, fill=COLORS[color])

    def _travel_text(self, world, place):
        """"Travelling to Ob  ·  1:23 left" while travelling, else None (user, 2026-11-17: the area only showed the
        last location). The destination is the starmap entry picked before the jump (the bot's destination as a
        fallback); the time left is the client's own countdown: player "travel" (trip seconds, set at the jump) + 2 s
        (class_771) minus the time since the jump started."""
        now = time.monotonic()
        if not world.travelling:
            self._travel = None
            self._travel_seen_start = True
            if world.zone == "starmap" and world.selected_target is not None:
                self._travel_pick = world.selected_target.name
            return None
        travel = getattr(self, "_travel", None)
        if travel is None or (world.travel_s and travel[1] != world.travel_s):
            pick = getattr(self, "_travel_pick", None)
            # Timed only if the GUI saw the jump start; opened mid-trip, the time left is unknown.
            fresh = travel is not None or getattr(self, "_travel_seen_start", False)
            self._travel = travel = (now if fresh else None, world.travel_s, pick)
        dest = travel[2]
        if not dest and world.selected_target is not None:
            dest = world.selected_target.name  # stays the destination during the trip (seen 2026-11-17 02:01)
        if not dest and self.bot is not None and self.bot.running:
            dest = getattr(self.bot, "travel_to", None)
        dest = assets.display_name(dest) if dest else "?"
        text = f"Travelling  ·  {place} → {dest}"
        if travel[0] is not None and travel[1]:
            left = max(0, int(travel[1] + 2 - (now - travel[0])))
            text += f"  ·  {left // 60}:{left % 60:02d} left"
        return text

    def _show(self, snapshot):
        world = snapshot.world
        self.connection.configure(text="Connected to the game" if snapshot.connected else "Game not running",
                                  fg=COLORS["ok"] if snapshot.connected else COLORS["error"])
        if not snapshot.connected or world is None:
            self._set_status("Waiting for the game", "subtle")
            self.location.configure(text="")
            self._draw_message("No game data")
            return
        bot_running = self.bot is not None and self.bot.running
        if world.dead:
            self._set_status("Dead", "error")
        elif world.travelling:
            self._set_status("Travelling", "energy")
        elif bot_running:
            self._set_status(self.bot.status, "warning" if self.bot.paused else "ok")
        else:
            self._set_status("Idle", "subtle")
        place = "Starbase" if world.at_starbase else (assets.display_name(world.current_planet) or "-")
        self.location.configure(text=self._travel_text(world, place) or
                                f"{ZONE_LABELS.get(world.zone, world.zone)}  ·  {place}")

        if world.hitpoints is not None and world.max_hitpoints:
            fraction = world.hitpoints / world.max_hitpoints
            self.hp_bar.set(fraction)
            self.hp_text.configure(text=f"{fraction * 100:.0f}%")
        if world.energy is not None:
            # Computed cap; the highest energy seen only if it is ever above that (unread bonuses).
            self._session["energy_max"] = max(self._session.get("energy_max", 0), world.energy)
            cap = max(world.energy_cap or 0, self._session["energy_max"]) or 1
            self.energy_bar.set(world.energy / cap)
            self.energy_text.configure(text=f"{world.energy:,} / {cap:,}" if world.energy_cap else f"{world.energy:,}")
        if world.cryonite is not None:
            if self._session["cryonite"] is None:
                self._session["cryonite"] = world.cryonite
            gained = world.cryonite - self._session["cryonite"]
            self.cryonite_text.configure(text=f"{world.cryonite:,}   {gained:+,}")
        self.level_text.configure(text=f"{world.pilot_level or '-'}")
        if world.xp is not None:
            if self._session["xp"] is None:
                self._session["xp"] = world.xp
            self.xp_text.configure(text=f"{world.xp:,}   {world.xp - self._session['xp']:+,}")
        ship = assets.display_name(world.ship_name)
        if ship and world.ship_level is not None:
            ship = f"[{world.ship_level}] {ship}"
        self.ship_text.configure(text=ship or "-")
        self.read_text.configure(text=f"Memory read {snapshot.read_ms:.0f} ms")
        if world.zone == "planet" and world.planet_map and world.x is not None:
            self._draw_map(world)
        else:
            self._draw_message(f"{ZONE_LABELS.get(world.zone, world.zone)}\n{place}")

    # Remote (docs/REMOTE.md) --------------------------------------------------------------------------------
    # Hard rule: nothing here may stop or slow the bot. Every call into the remote goes through _remote_call, which
    # switches the remote off on any error instead of letting it reach the window or the bot.

    def _start_remote(self):
        try:
            from ..remote.client import RemoteClient
            self.remote = RemoteClient(self.config, catalog=self._remote_catalog, learned=self._remote_learned)
            self.remote.start()
            self._remote_error = ""
            self._remote_vars_from_link()
        except Exception as error:
            self.remote = None
            self._remote_error = f"Remote could not start: {type(error).__name__}: {error}"
        self._remote_tick_count = 0

    def _remote_call(self, fn):
        if self.remote is None:
            return None
        try:
            return fn(self.remote)
        except Exception as error:
            remote, self.remote = self.remote, None
            self._remote_error = f"Remote switched off after an error: {type(error).__name__}: {error}"
            try:
                remote.stop()
            except Exception:
                pass
            return None

    @staticmethod
    def _remote_catalog():
        """Static lists the page needs for its mode selector (read once, in the remote thread)."""
        return {"modes": [[key, label, text] for key, label, text in MODES],
                "planets": ControlApp._planet_choices(),
                "systems": [name for name, _, _ in myzen.SYSTEM_LEVELS],
                "ships": [name for name in ControlApp._ship_choices() if name]}

    def _remote_learned(self):
        """The Learned report for the page (remote thread): over a copy of the running bot's data, else the files."""
        from ..learn import report
        from ..learn.learner import Learner
        from ..learn.store import Store
        from ..bot.core import SQUAD_BY_MOVEMENT
        bot = self.bot
        live = getattr(bot, "learner", None) if bot is not None else None
        if live is not None:
            store = live.store.copy()
        else:
            store = Store()
            ships = store.ship_files()
            if not ships:
                return None
            store.use_ship(ships[-1][0])
        completed = None
        if bot is not None and getattr(bot, "blueprints", None) is not None:
            completed = bot.blueprints.completed
        return report.report(Learner(store=store), self.config.learning, completed, squads=SQUAD_BY_MOVEMENT)

    def _remote_extras(self):
        bot = self.bot
        running = bot is not None and bot.running
        extras = {
            "running": running, "paused": running and bot.paused, "attached": bot is not None,
            "status": getattr(self, "_status_full", ""),
            "energy_max": self._session.get("energy_max"),
            "session": {"start": round(self._session["start"]), "cryonite_start": self._session["cryonite"],
                        "xp_start": self._session["xp"]},
        }
        if running:
            caution = getattr(bot, "caution", None)
            extras["caution"] = caution
            extras["stop_in_orbit"] = bool(getattr(bot, "stop_in_orbit", False))
            extras["caution_label"] = {"lower": "Lower level", "same": "Same level", "high": "High level"}.get(
                caution, "")
            started = getattr(bot, "started", None)
            extras["run_s"] = round(time.monotonic() - started) if started else None
            route = getattr(bot, "route", None)
            extras["route"] = list(route) if route else []
            bp = getattr(bot, "bp", None)
            if self.config.mode == "bp_completion" and bp and bp.get("summary"):
                extras["bp"] = {"system": bp.get("system"), "planet": bp.get("planet"), **bp["summary"]}
        return extras

    def _remote_tick(self, snapshot):
        if self.remote is None:
            self._remote_tick_count = getattr(self, "_remote_tick_count", 0) + 1
            if self._remote_tick_count % 4 == 0:
                self._remote_refresh_tab()
            return
        self._remote_call(lambda r: r.offer(snapshot, self._remote_extras()))
        for _ in range(5):  # a few per tick at most: a flood of commands cannot hold up the window
            item = self._remote_call(self._remote_next)
            if item is None:
                break
            self._remote_apply(item)
        self._remote_tick_count += 1
        if self._remote_tick_count % 4 == 0:
            self._remote_refresh_tab()

    @staticmethod
    def _remote_next(remote):
        try:
            return remote.commands.get_nowait()
        except queue.Empty:
            return None

    def _remote_apply(self, item):
        try:
            ok, text = self._remote_command(item["cmd"], item.get("args") or {})
        except Exception as error:
            ok, text = False, f"{item.get('cmd')} failed: {type(error).__name__}: {error}"
        self._remote_call(lambda r: r.ack(item["seq"], ok, text))
        self._append_log(LogEvent("info" if ok else "warning", f"Remote: {text}"))
        debug = getattr(self.bot, "_debug", None)
        if debug is not None:
            debug(f"remote: {text}")  # also in the run log (logs/bot-*.log) for later analysis

    def _remote_command(self, cmd, args):
        """Applies one command from the web page through the same paths as the window's own controls."""
        bot = self.bot
        running = bot is not None and bot.running
        if cmd == "start":
            if bot is None:
                return False, "start refused: no bot attached"
            if running:
                return True, "already running"
            self._start()
            return True, "started"
        if cmd in ("pause", "resume"):
            if not running:
                return False, f"{cmd} refused: not running"
            if bot.paused == (cmd == "pause"):
                return True, f"already {'paused' if bot.paused else 'running'}"
            self._pause()
            return True, "paused" if cmd == "pause" else "resumed"
        if cmd in ("stop_in_orbit", "keep_going"):
            if not running:
                return False, f"{cmd} refused: not running"
            if bot.stop_in_orbit != (cmd == "stop_in_orbit"):
                self._stop_in_orbit()
            return True, "stopping in orbit" if cmd == "stop_in_orbit" else "keeps going"
        if cmd == "stop":
            if not running:
                return True, "already stopped"
            self._stop()
            return True, "stopped"
        if cmd == "set_mode":
            return self._remote_set_mode(args)
        if cmd == "set_setting":
            return self._remote_set_setting(args)
        if cmd in ("set_learn_switch", "reset_learned"):
            return self._remote_learned_command(cmd, args)
        return False, f"unknown command {cmd!r}"

    def _remote_learned_command(self, cmd, args):
        """A switch or a reset in the Learned report, written through the same store as the Learned tab."""
        from ..learn.store import AREAS
        area = args.get("area")
        if area not in AREAS:
            return False, f"{cmd} refused: unknown area"
        store = self.learned.live_store()[0]
        label = AREAS[area][0]
        if cmd == "reset_learned":
            store.reset(area)
            self.learned.refresh()
            return True, f"learned {label.lower()} reset"
        value, entry = args.get("value"), args.get("entry")
        if not isinstance(value, bool) or (entry is not None and not isinstance(entry, str)):
            return False, "switch refused: bad value"
        store.set_switch(area, value, entry[:200] if entry else None)
        self.learned.refresh()
        return True, f"learned {label.lower()}{f' / {entry}' if entry else ''} {'on' if value else 'off'}"

    def _remote_set_mode(self, args):
        mode = args.get("mode", self.config.mode)
        if mode not in MODE_LABELS:
            return False, "mode refused: unknown mode"
        fly = args.get("fly_planet")
        if fly is not None:
            fly = str(fly).strip()
            choices = {myzen.normalize(name): name for name in self._planet_choices()}
            if fly and myzen.normalize(fly) not in choices:
                return False, f"mode refused: unknown planet {fly[:40]!r}"
            fly = choices.get(myzen.normalize(fly), "")
        blueprint = args.get("seek_blueprint")
        if blueprint is not None:
            blueprint = str(blueprint).strip()[:200]
            names = {name.lower(): name for name in myzen.blueprint_names()}
            if blueprint and names and blueprint.lower() not in names:
                return False, f"mode refused: unknown blueprint {blueprint[:40]!r}"
            blueprint = names.get(blueprint.lower(), blueprint)
        enemies = args.get("seek_enemies")
        if enemies is not None:
            enemies = ", ".join(p.strip() for p in str(enemies)[:400].split(",") if p.strip())
        system = args.get("bp_system")
        if system is not None:
            system = str(system).strip()
            systems = {myzen.normalize(name): name for name, _, _ in myzen.SYSTEM_LEVELS}
            if system and myzen.normalize(system) not in systems:
                return False, f"mode refused: unknown star system {system[:40]!r}"
            system = systems.get(myzen.normalize(system), "")
        ship = args.get("ship")
        if ship is not None:
            ship = " ".join(str(ship).split())[:80]  # free text like the window's box: own name or ship name
        if mode == "fly" and not (fly if fly is not None else self.config.fly_planet):
            return False, "mode refused: Fly to Planet needs a planet"
        if fly is not None:
            self.fly_var.set(fly)
        if blueprint is not None:
            self.bp_var.set(blueprint)
        if enemies is not None:
            self.enemy_var.set(enemies)
            self._enemy_before_pick = enemies
        if system is not None:
            self.sys_var.set(system)
        if ship is not None:
            self.ship_var.set(ship)
        self.mode_var.set(MODE_LABELS[mode])
        self._mode_changed()
        detail = {"fly": self.config.fly_planet, "seek_blueprint": self.config.seek_blueprint,
                  "seek_enemy": self.config.seek_enemies,
                  "bp_completion": f"{self.config.bp_system or 'current system'}, "
                                   f"{self.config.ship or 'current ship'}"}.get(mode, "")
        return True, f"mode set to {MODE_LABELS[mode]}" + (f" ({detail})" if detail else "")

    def _remote_set_setting(self, args):
        name = args.get("name")
        if name not in self._setting_vars:
            return False, "setting refused: unknown setting"
        var, item = self._setting_vars[name]
        meta, default, value = item.metadata, item.default, args.get("value")
        if isinstance(default, bool):
            if not isinstance(value, bool):
                return False, f"{meta['label']} refused: not on/off"
        elif name == "game_folder":
            value = str(value).strip().strip('"')[:400]
            problem = location.check(value) if value else None
            if problem:
                return False, f"{meta['label']} refused: {problem}"
        elif isinstance(default, str):
            value = ", ".join(p.strip() for p in str(value)[:400].split(",") if p.strip())
        else:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return False, f"{meta['label']} refused: not a number"
            if math.isnan(value) or (meta["min"] is not None and value < meta["min"]) or (
                    meta["max"] is not None and value > meta["max"]):
                return False, f"{meta['label']} refused: out of range"
            if isinstance(default, int):
                value = int(round(value))
        if name == "refill_until" and value <= self.config.refill_below or \
                name == "refill_below" and value >= self.config.refill_until:
            return False, "refused: Refill up to energy must be above Refill below energy"
        setattr(self.config, name, value)  # in place: a running bot uses it at once
        self._save_config()
        if isinstance(default, bool):
            var.set(value)
        elif isinstance(default, str):
            var.set(value)
        elif meta["percent"]:
            var.set(f"{value * 100:g}")
        else:
            var.set(f"{value:g}" if isinstance(value, float) else str(value))
        self.settings_note.configure(text=f"{meta['label']} changed from the website", fg=COLORS["ok"])
        shown = f"{value * 100:g}%" if meta["percent"] and not isinstance(default, bool) else (
            ("on" if value else "off") if isinstance(default, bool) else
            f"{value:g}{(' ' + meta['unit']) if meta['unit'] else ''}" if isinstance(value, (int, float)) else
            f"'{value}'")
        return True, f"{meta['label']} set to {shown}"

    def _build_remote(self, page):
        card = self._card(page, "Penumbra account", fill="x")
        top = tk.Frame(card, bg=COLORS["panel"])
        top.pack(fill="x")
        self.remote_dot = tk.Canvas(top, width=12, height=12, bg=COLORS["panel"], highlightthickness=0)
        self.remote_dot.pack(side="left", padx=(0, 8))
        self._remote_dot = self.remote_dot.create_oval(1, 1, 11, 11, fill=COLORS["subtle"], outline="")
        self.remote_state = self._label(top, "Starting...", "text", 13, bold=True)
        self.remote_state.pack(side="left")
        self.remote_link_button = RoundedButton(top, "Link account", self._remote_link, COLORS["accent"],
                                                COLORS["panel"], width=130)
        self.remote_link_button.pack(side="right")
        self.remote_detail = self._label(card, "", "muted", 9, wraplength=820)
        self.remote_detail.pack(anchor="w", pady=(6, 0))

        self.remote_code_frame = tk.Frame(card, bg=COLORS["panel"])
        self._label(self.remote_code_frame, "Approve this bot on your account page with the code:", "muted", 9
                    ).pack(anchor="w", pady=(12, 2))
        code_row = tk.Frame(self.remote_code_frame, bg=COLORS["panel"])
        code_row.pack(anchor="w")
        self.remote_code = self._label(code_row, "", "text", 24, bold=True)
        self.remote_code.pack(side="left", padx=(0, 18))
        RoundedButton(code_row, "Open link", self._remote_open_verify, COLORS["accent"], COLORS["panel"]
                      ).pack(side="left")
        RoundedButton(code_row, "Cancel", lambda: self._remote_call(lambda r: r.request("cancel")),
                      COLORS["neutral"], COLORS["panel"]).pack(side="left", padx=(8, 0))
        self._label(self.remote_code_frame, "The code works for 10 minutes. No password is typed into the bot.",
                    "subtle", 8).pack(anchor="w", pady=(4, 0))

        bot_card = self._card(page, "This bot", fill="x", pady=(12, 0))
        rows = tk.Frame(bot_card, bg=COLORS["panel"])
        rows.pack(fill="x")
        rows.columnconfigure(0, weight=1)
        self.remote_name_var = tk.StringVar()
        self.remote_send_var = tk.BooleanVar(value=True)
        self.remote_control_var = tk.BooleanVar(value=False)
        entries = (
            ("Bot name", "Shown on the web page (useful with several PCs).", None),
            ("Send live data to the web page", "Off: nothing is sent; the bot runs exactly as without the remote.",
             self.remote_send_var),
            ("Allow control from the website", "Off: the page can only watch. On: start, pause, stop, mode and "
                                                "settings can be changed from the page. The buttons here always work.",
             self.remote_control_var),
        )
        for row, (label, hint, var) in enumerate(entries):
            text = tk.Frame(rows, bg=COLORS["panel"])
            text.grid(row=row, column=0, sticky="w", pady=5)
            self._label(text, label, "text", 10).pack(anchor="w")
            self._label(text, hint, "subtle", 8).pack(anchor="w")
            if var is None:
                entry = ttk.Entry(rows, textvariable=self.remote_name_var, width=28, font=(FONT, 10))
                entry.grid(row=row, column=1, sticky="e", padx=(12, 0))
                entry.bind("<FocusOut>", lambda _: self._remote_save_name())
                entry.bind("<Return>", lambda _: self._remote_save_name())
            else:
                Toggle(rows, var, COLORS["accent"], COLORS["panel"]).grid(row=row, column=1, sticky="e",
                                                                          padx=(12, 0))
        self.remote_send_var.trace_add("write", lambda *_: self._remote_call(
            lambda r: r.set_enabled(self.remote_send_var.get())))
        self.remote_control_var.trace_add("write", lambda *_: self._remote_call(
            lambda r: r.set_allow_control(self.remote_control_var.get())))

        page_card = self._card(page, "Web page", fill="x", pady=(12, 0))
        page_row = tk.Frame(page_card, bg=COLORS["panel"])
        page_row.pack(fill="x")
        self.remote_url = self._label(page_row, "", "text", 10)
        self.remote_url.pack(side="left")
        RoundedButton(page_row, "Open page", self._remote_open_page, COLORS["neutral"], COLORS["panel"]
                      ).pack(side="right")
        self.remote_info = self._label(page_card, "", "muted", 9)
        self.remote_info.pack(anchor="w", pady=(8, 0))
        self.remote_restart = RoundedButton(page_card, "Restart remote", self._remote_restart, COLORS["neutral"],
                                            COLORS["panel"], width=140)

    def _remote_vars_from_link(self):
        data = self.remote.link.data
        self.remote_name_var.set(data.get("bot_name", ""))
        self.remote_send_var.set(bool(data.get("enabled", True)))
        self.remote_control_var.set(bool(data.get("allow_control")))

    def _remote_save_name(self):
        self._remote_call(lambda r: r.set_bot_name(self.remote_name_var.get()))

    def _remote_link(self):
        def act(remote):
            remote.request("unlink" if remote.link.linked else "link")
        self._remote_call(act)

    def _remote_open_verify(self):
        url = self.remote.verify_url if self.remote is not None else None
        if url and url.startswith("https://"):
            import webbrowser
            webbrowser.open(url)

    def _remote_open_page(self):
        url = self.remote.page_url if self.remote is not None else None
        if url and url.startswith("https://"):
            import webbrowser
            webbrowser.open(url)

    def _remote_restart(self):
        self._remote_call(lambda r: r.stop())
        self.remote = None
        self._start_remote()
        self._remote_refresh_tab()

    def _remote_refresh_tab(self):
        remote = self.remote
        alive = remote is not None and remote.alive
        if not alive:
            self.remote_state.configure(text="Remote stopped")
            self.remote_dot.itemconfigure(self._remote_dot, fill=COLORS["error"])
            self.remote_detail.configure(text=self._remote_error or "The remote thread ended; the bot is not "
                                                                    "affected.", fg=COLORS["error"])
            self.remote_code_frame.pack_forget()
            self.remote_link_button.set_enabled(False)
            if not self.remote_restart.winfo_ismapped():
                self.remote_restart.pack(anchor="w", pady=(10, 0))
            return
        if self.remote_restart.winfo_ismapped():
            self.remote_restart.pack_forget()
        from ..remote.client import describe
        state = remote.state
        color = {"connected": "ok", "linking": "warning", "connecting": "warning", "offline": "error",
                 "signed_out": "error"}.get(state, "subtle")
        self.remote_state.configure(text=describe(state) + (
            f"  ·  {remote.link.user_name}" if remote.link.linked else ""))
        self.remote_dot.itemconfigure(self._remote_dot, fill=COLORS[color])
        self.remote_detail.configure(text=remote.detail, fg=COLORS["error" if state in ("offline", "signed_out")
                                                                   else "muted"])
        self.remote_link_button.set_text("Unlink" if remote.link.linked else "Link account")
        self.remote_link_button.set_enabled(state != "linking")
        if remote.user_code:
            self.remote_code.configure(text=remote.user_code)
            if not self.remote_code_frame.winfo_ismapped():
                self.remote_code_frame.pack(anchor="w", fill="x")
        elif self.remote_code_frame.winfo_ismapped():
            self.remote_code_frame.pack_forget()
        self.remote_url.configure(text=remote.page_url)
        parts = []
        if remote.last_ok:
            parts.append(f"Last sent {max(0, time.time() - remote.last_ok):.0f} s ago")
        if remote.last_command:
            parts.append(f"last command: {remote.last_command}")
        if remote.error_count:
            parts.append(f"{remote.error_count} internal errors (ignored)")
        self.remote_info.configure(text="  ·  ".join(parts))

    # Map ----------------------------------------------------------------------------------------------------

    def _draw_message(self, text):
        self.map.delete("all")
        self.map.create_text(MAP_SIZE / 2, MAP_SIZE / 2, text=text, fill=COLORS["muted"], font=(FONT, 12),
                             justify="center")

    def _map_image(self, name):
        if name not in self._map_cache:
            data = assets.find_asset(name)
            image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None
            self._map_cache[name] = image
        return self._map_cache[name]

    def _draw_map(self, world):
        image = self._map_image(world.planet_map)
        if image is None:
            self._draw_message(f"Map not found\n{world.planet_map}")
            return
        height, width = image.shape[:2]
        cx, cy = assets.world_to_map(world.x, world.y, width, height)
        radius = MAP_RADIUS_WORLD / assets.MAP_SPAN * width
        x0, y0 = int(cx - radius), int(cy - radius)
        size = int(radius * 2)
        canvas = np.zeros((size, size, 3), np.uint8)
        sx0, sy0 = max(0, x0), max(0, y0)
        sx1, sy1 = min(width, x0 + size), min(height, y0 + size)
        if sx1 > sx0 and sy1 > sy0:
            canvas[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = image[sy0:sy1, sx0:sx1]
        canvas = cv2.resize(canvas, (MAP_SIZE, MAP_SIZE), interpolation=cv2.INTER_LINEAR)
        scale = MAP_SIZE / (MAP_RADIUS_WORLD * 2)

        def to_px(x, y):
            return int(MAP_SIZE / 2 + (x - world.x) * scale), int(MAP_SIZE / 2 + (y - world.y) * scale)

        # Energy fields from the site data: rings in the energy colour.
        info = myzen.planet_info(assets.display_name(world.current_planet))
        for x, y in (info or {}).get("energy", []):
            if abs(x - world.x) <= MAP_RADIUS_WORLD and abs(y - world.y) <= MAP_RADIUS_WORLD:
                cv2.circle(canvas, to_px(x, y), 7, (255, 200, 60), 1, cv2.LINE_AA)
        for unit in world.units:
            if math.isnan(unit.x) or abs(unit.x - world.x) > MAP_RADIUS_WORLD or abs(unit.y - world.y) > MAP_RADIUS_WORLD:
                continue
            px = to_px(unit.x, unit.y)
            if unit.class_name.endswith("/tb"):
                cv2.circle(canvas, px, 3, (255, 200, 60), -1)
            elif unit.is_player:
                cv2.circle(canvas, px, 4, (255, 255, 255), 1)
            elif unit.marker and "alien" in unit.marker:
                color = (60, 60, 230) if "agressive" in unit.marker or unit.hostile else (60, 160, 230)
                cv2.circle(canvas, px, 4, color, -1)
            elif unit.is_item and (unit.rare or "_rare" in (unit.mesh or "")):
                cv2.circle(canvas, px, 4, (64, 201, 255), -1)  # gold blueprint (BGR of #ffc940)
                cv2.circle(canvas, px, 6, (64, 201, 255), 1, cv2.LINE_AA)
            elif unit.is_item:
                cv2.circle(canvas, px, 3, (255, 107, 210), -1)  # loot: pink-purple (BGR of #d26bff)
        route = getattr(self.bot, "route", None) if self.bot is not None and self.bot.running else None
        if route and len(route) > 1:
            points = np.array([to_px(x, y) for x, y in route], np.int32)
            cv2.polylines(canvas, [points], False, (255, 255, 255), 1, cv2.LINE_AA)
        centre = (MAP_SIZE // 2, MAP_SIZE // 2)
        if world.heading and any(world.heading):
            hx, hy = world.heading
            length = math.hypot(hx, hy) or 1
            tip = (int(centre[0] + hx / length * 12), int(centre[1] + hy / length * 12))
            cv2.arrowedLine(canvas, centre, tip, (90, 255, 90), 2, tipLength=0.5)
        cv2.circle(canvas, centre, 5, (90, 255, 90), -1)
        ok, png = cv2.imencode(".png", canvas)
        if ok:
            self._map_photo = tk.PhotoImage(data=base64.b64encode(png.tobytes()).decode("ascii"))
            self.map.delete("all")
            self.map.create_image(0, 0, image=self._map_photo, anchor="nw")

    def run(self):
        self.root.mainloop()


def main(autostart=False):
    """autostart: press Start once the window is up (python run_gui.py --start)."""
    from ..bot.core import Bot
    STOP_FILE.unlink(missing_ok=True)
    app = ControlApp(bot=Bot())
    if autostart:
        app.root.after(1500, app._start)
    app.run()


if __name__ == "__main__":
    main()
