"""Learned tab: what the bot learned (docs/LEARNING.md), a switch per area and per entry, and a reset per area.

Reads the running bot's learner when there is one (live numbers), else the files. Switches are written through the
same store, so a running bot follows them at once.
"""
import json
import time
import tkinter as tk
from tkinter import ttk

from ..learn import report
from ..learn.learner import Learner
from ..bot.core import SQUAD_BY_MOVEMENT
from ..learn.store import AREAS, Store
from .widgets import RoundedButton, Toggle

REFRESH_S = 15
COLLAPSED_ROWS = 8  # rows shown per list until "Show all" is clicked


class LearnedPage:
    def __init__(self, app, page):
        self.app = app
        self.colors = app.COLORS
        self.page = page
        self._store = None
        self._last = 0.0
        self._signature = None
        self._vars = []  # keep Tk variables alive
        self._expanded = set()  # lists opened with "Show all" (kept across refreshes)
        top = tk.Frame(page, bg=self.colors["bg"])
        top.pack(fill="x", pady=(0, 8))
        self.header = app._label(top, "", "muted", 9, bg="bg", wraplength=700)
        self.header.pack(side="left")
        RoundedButton(top, "Refresh", self.refresh, self.colors["neutral"], self.colors["bg"]).pack(side="right")
        holder = tk.Frame(page, bg=self.colors["bg"])
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, bg=self.colors["bg"], highlightthickness=0)
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=self.colors["bg"])
        window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width - 12))

    # Data -----------------------------------------------------------------------------------------------------

    def live_store(self):
        """The store switches are written to: the running bot's, else one read from the files."""
        bot = self.app.bot
        live = getattr(bot, "learner", None) if bot is not None else None
        if live is not None:
            return live.store, True
        if self._store is None:
            self._store = Store()
        return self._store, False

    def learner(self):
        """A learner over a copy of the data (the bot thread keeps writing the live one)."""
        store, live = self.live_store()
        if not live:
            self._store = Store()  # fresh from the files, with the last ship used
            store = self._store
            ships = store.ship_files()
            if ships:
                store.use_ship(ships[-1][0])
        return Learner(store=store.copy()), live

    def tick(self):
        if self.page.winfo_ismapped() and time.monotonic() - self._last > REFRESH_S:
            self.refresh(force=False)

    # Building -------------------------------------------------------------------------------------------------

    def refresh(self, force=True):
        self._last = time.monotonic()
        try:
            learner, live = self.learner()
            store = learner.store
            signature = hash(json.dumps([store.shared, store.ship, store.switches, self.app.config.learning],
                                        sort_keys=True, default=str))
            if not force and signature == self._signature:
                return  # nothing new: leave the tab (and its scroll position) alone
            self._signature = signature
            top = self.canvas.yview()[0]
            self._build(learner, live)
            self.canvas.update_idletasks()
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            self.canvas.yview_moveto(top)
        except Exception as error:  # the tab must never take the window down
            for child in self.body.winfo_children():
                child.destroy()
            self.app._label(self.body, f"Could not show what was learned: {type(error).__name__}: {error}",
                            "error", 10, bg="bg").pack(anchor="w")

    def _build(self, learner, live):
        for child in self.body.winfo_children():
            child.destroy()
        self._vars = []
        store = learner.store
        ships = store.ship_files()
        if live and store.ship_key and store.ship_key not in [s for s, _ in ships]:
            ships.append((store.ship_key, store.ship))
        ship_key = store.ship_key or (ships[-1][0] if ships else None)
        if store.ship_key is None and ship_key:
            store.use_ship(ship_key)
        bot = self.app.bot
        completed = None
        if bot is not None and getattr(bot, "blueprints", None) is not None:
            completed = bot.blueprints.completed
        # The same report the remote page shows (learn/report.py).
        data = report.report(learner, self.app.config.learning, completed, squads=SQUAD_BY_MOVEMENT)
        self.header.configure(text=data["header"])
        for item in data["cards"]:
            label, text = AREAS[item["area"]]
            card = self._card(item["area"], item["text"][len(text):].strip(), reset=item["reset"])
            if not item["header"]:
                continue
            rows = [(row["entry"], [tuple(c) if c[1] not in ("text", "muted") else c[0] for c in row["cells"]])
                    for row in item["rows"]]
            self._table(card, item["header"], rows, item["area"] if any(r["entry"] for r in item["rows"]) else None,
                        empty=item["empty"])
            for note in item["notes"]:
                self.app._label(card, note, "subtle", 8, wraplength=820).pack(anchor="w", pady=(6, 0))

    def _card(self, area, extra="", reset=True):
        label, text = AREAS[area]
        card = self.app._card(self.body, None, fill="x", pady=(0, 12))
        head = tk.Frame(card, bg=self.colors["panel"])
        head.pack(fill="x")
        self.app._label(head, label, "text", 11, bold=True).pack(side="left")
        if reset:
            RoundedButton(head, "Reset", lambda a=area: self._reset(a), self.colors["neutral"],
                          self.colors["panel"], width=80, height=28).pack(side="right")
        self._switch(head, area).pack(side="right", padx=(0, 12))
        self.app._label(card, text + (f" {extra}" if extra else ""), "subtle", 8, wraplength=820).pack(
            anchor="w", pady=(4, 6))
        return card

    def _switch(self, parent, area, entry=None):
        store = self.live_store()[0]
        var = tk.BooleanVar(value=store.on(area, entry))
        self._vars.append(var)
        var.trace_add("write", lambda *_: self._set(area, var.get(), entry))
        return Toggle(parent, var, self.colors["accent"], self.colors["panel"])

    def _set(self, area, value, entry):
        try:
            self.live_store()[0].set_switch(area, value, entry)
        except Exception as error:
            self.app._append_log(self.app.LogEvent("warning", f"Could not save the Learned switch: {error}"))

    def _reset(self, area):
        from tkinter import messagebox
        if not messagebox.askyesno("Reset", f"Forget what the bot learned about {AREAS[area][0].lower()}?"):
            return
        self.live_store()[0].reset(area)
        self.refresh()

    def _table(self, card, header, rows, switch_area=None, empty="Nothing yet. This fills in while the bot plays."):
        """rows: [(entry key or None, [cells])]. A switch per row when switch_area is given."""
        if not rows:
            self.app._label(card, empty, "muted", 9).pack(anchor="w")
            return
        grid = tk.Frame(card, bg=self.colors["panel"])
        grid.pack(fill="x")
        for col, text in enumerate(header):
            self.app._label(grid, text, "subtle", 8, bold=True).grid(row=0, column=col, sticky="w", padx=(0, 14))
        key = tuple(header)
        total = len(rows)
        if total > COLLAPSED_ROWS and key not in self._expanded:
            rows = rows[:COLLAPSED_ROWS]
        for r, (entry, cells) in enumerate(rows, start=1):
            for col, text in enumerate(cells):
                color = "text" if col == 0 else "muted"
                if isinstance(text, tuple):
                    text, color = text
                self.app._label(grid, text, color, 9).grid(row=r, column=col, sticky="w", padx=(0, 14), pady=1)
            if switch_area is not None and entry is not None:
                self._switch(grid, switch_area, entry).grid(row=r, column=len(header), sticky="e")
        if total > COLLAPSED_ROWS:
            opened = key in self._expanded
            link = self.app._label(card, "Show fewer" if opened else f"Show all ({total})", "accent", 9)
            link.configure(cursor="hand2")
            link.pack(anchor="w", pady=(4, 0))
            link.bind("<Button-1>", lambda _e, k=key: self._toggle(k))

    def _toggle(self, key):
        if key in self._expanded:
            self._expanded.discard(key)
        else:
            self._expanded.add(key)
        self.refresh()
