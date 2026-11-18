"""RemoteClient: the thread that links the bot to the web page through the PHP relay (docs/REMOTE.md).

Hard rule (REMOTE.md 5a): nothing here may ever stop, slow or block the bot or the control window.
- All network work happens in this daemon thread; the Tk thread only drops references into one-slot boxes and
  bounded deques and takes commands with get_nowait().
- Every request has a timeout; failures back off up to 2 min; every exception is caught and counted.
"""
import collections
import hashlib
import json
import math
import queue
import threading
import time

from . import penumbra

RELAY_URL = "https://eclip5e.com/development/pgbot/api.php"  # remote.json "relay_url" overrides (folder moved)
PUSH_RUNNING_S = 1.0
PUSH_IDLE_S = 3.0
BACKOFF_S = (5, 10, 20, 40, 80, 120)
MAP_RADIUS = 14000.0
MAX_UNITS = 120
LOG_KEEP = 500
COMMANDS = {"start", "pause", "resume", "stop", "stop_in_orbit", "keep_going", "set_mode", "set_setting", "get_settings",
            "set_learn_switch", "reset_learned"}
LEARNED_S = 60.0  # the Learned report (learn/report.py) is rebuilt and, when changed, sent at most this often
VIEW_COMMANDS = {"get_settings"}  # allowed in view-only mode


def _num(value, digits=0):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits) if digits else int(round(float(value)))


class RemoteClient:
    def __init__(self, config, link=None, catalog=None, learned=None):
        self.config = config
        self._catalog = catalog  # callable -> {"modes": [[key, label, text]], "planets": [...]}, read once
        self._catalog_data = None
        self._learned = learned  # callable -> the Learned report (learn/report.py) or None; called in this thread
        self._learned_at = 0.0
        self._learned_hash = None
        self._want_learned = True
        self.link = link or penumbra.Link()
        self.commands = queue.Queue(maxsize=50)  # validated commands for the Tk thread
        self._latest = None                      # (Snapshot, extras) offered by the Tk thread; newest wins
        self._log = collections.deque(maxlen=LOG_KEEP)
        self._acks = collections.deque(maxlen=100)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._action = None                      # "link" / "unlink" / "cancel" requested by the tab
        self._settings_hash = None
        self._want_settings = True
        self._cmd_seen = int(self.link.data.get("cmd_seen") or 0)
        self._device = None
        self._failures = 0
        self._retry_at = 0.0
        self.thread = None
        # Shown by the Remote tab (read from the Tk thread; plain values only).
        self.state = "off"        # off / not_linked / linking / connecting / connected / offline / signed_out
        self.detail = ""
        self.user_code = None
        self.verify_url = None
        self.last_command = ""
        self.last_ok = 0.0
        self.error_count = 0

    # Called from the Tk thread: never blocks ---------------------------------------------------------------

    def start(self):
        self.thread = threading.Thread(target=self._run, name="remote", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    @property
    def alive(self):
        return self.thread is not None and self.thread.is_alive()

    def offer(self, snapshot, extras):
        self._latest = (snapshot, extras)

    def log(self, event):
        self._log.append({"t": round(event.time, 1), "level": event.level, "text": event.text[:500]})

    def ack(self, seq, ok, message=""):
        self._acks.append({"seq": seq, "ok": bool(ok), "msg": str(message)[:200]})
        self._wake.set()

    def request(self, action):
        self._action = action
        self._wake.set()

    def set_allow_control(self, value):
        self.link.data["allow_control"] = bool(value)
        self._save_link()
        self._wake.set()

    def set_enabled(self, value):
        self.link.data["enabled"] = bool(value)
        self._save_link()
        self._wake.set()

    def set_bot_name(self, name):
        name = " ".join(str(name).split())[:40]
        if name and name != self.link.data.get("bot_name"):
            self.link.data["bot_name"] = name
            self._save_link()

    def resend_settings(self):
        self._want_settings = True
        self._wake.set()

    @property
    def page_url(self):
        return self.relay_url.rsplit("/", 1)[0] + "/"

    @property
    def relay_url(self):
        return self.link.data.get("relay_url") or RELAY_URL

    # Thread -------------------------------------------------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            delay = 1.0
            try:
                delay = self._step()
                self._failures = 0
            except penumbra.PenumbraError as error:
                delay = self._failed(str(error), signed_out=error.signed_out)
            except Exception as error:  # a bug in here must never reach the bot: count it and carry on
                self.error_count += 1
                delay = self._failed(f"{type(error).__name__}: {error}")
            self._wake.wait(max(0.2, delay))
            self._wake.clear()

    def _failed(self, text, signed_out=False):
        if signed_out and self.link.linked:
            self.link.clear()
            self.state, self.detail = "signed_out", "Signed out by the account, link again"
            return 2.0
        if self.state == "linking":
            self.detail = f"Retrying: {text}"
            return 5.0
        delay = BACKOFF_S[min(self._failures, len(BACKOFF_S) - 1)]
        self._failures += 1
        self._retry_at = time.time() + delay
        self.state, self.detail = "offline", text[:120]
        return delay

    def _save_link(self):
        try:
            self.link.save()
        except OSError as error:
            self.detail = f"Could not save remote.json: {error}"

    def _step(self):
        action, self._action = self._action, None
        if action == "unlink":
            if self.link.data.get("access_token"):
                penumbra.sign_out(self.link.data["access_token"])
            self.link.clear()
            self._device = None
            self.user_code = self.verify_url = None
        elif action == "link":
            self._device = None
            self.state = "linking"
        elif action == "cancel":
            self._device = None
            self.user_code = self.verify_url = None
            if self.state == "linking":
                self.state = "not_linked"
        if self.state == "linking":
            return self._link_step()
        if not self.link.linked:
            if self.state != "signed_out":
                self.state, self.detail = "not_linked", ""
            return 1.0
        if not self.link.data.get("enabled", True):
            self.state, self.detail = "off", "Sending switched off"
            return 1.0
        if self.state not in ("connected",):
            self.state = "connecting"
        return self._push()

    def _link_step(self):
        if self._device is None:
            self._device = penumbra.start_device_login()
            self._device["until"] = time.time() + float(self._device.get("expires_in") or 600)
            self.user_code = self._device["user_code"]
            self.verify_url = self._device.get("verification_uri_complete") or self._device.get("verification_uri")
            self.detail = "Enter the code on the account page"
            return float(self._device.get("interval") or 5)
        if time.time() > self._device["until"]:
            self._device = None
            self.user_code = self.verify_url = None
            self.state, self.detail = "not_linked", "The code expired, link again"
            return 1.0
        try:
            reply = penumbra.poll_device_login(self._device["device_code"])
        except penumbra.PenumbraError as error:
            if error.code == "slow_down":
                self._device["interval"] = float(self._device.get("interval") or 5) + 5
                return self._device["interval"]
            if error.code in ("access_denied", "expired_token"):
                self._device = None
                self.user_code = self.verify_url = None
                self.state = "not_linked"
                self.detail = "Linking was declined" if error.code == "access_denied" else "The code expired"
                return 1.0
            raise
        if reply is None:
            return float(self._device.get("interval") or 5)
        self.link.store_tokens(reply)
        self._device = None
        self.user_code = self.verify_url = None
        self.state, self.detail = "connecting", f"Linked as {self.link.user_name}"
        return 0.2

    # Push / commands ----------------------------------------------------------------------------------------

    def _push(self):
        sent_log = list(self._log)
        sent_acks = list(self._acks)
        body = {
            "bot_id": self.link.data["bot_id"], "name": self.link.data.get("bot_name"),
            "allow_control": bool(self.link.data.get("allow_control")), "cmd_seen": self._cmd_seen,
            "state": self._state(), "log": sent_log, "acks": sent_acks,
        }
        settings = self._settings()
        digest = hashlib.sha1(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        if self._want_settings or digest != self._settings_hash:
            body["settings"] = settings
        learned_digest = None
        if self._learned is not None and (self._want_learned or time.time() - self._learned_at >= LEARNED_S):
            self._learned_at = time.time()
            try:
                learned = self._learned()
            except Exception:
                learned = None
                self.error_count += 1
            if learned is not None:
                learned_digest = hashlib.sha1(json.dumps(learned, sort_keys=True).encode()).hexdigest()
                if self._want_learned or learned_digest != self._learned_hash:
                    body["learned"] = learned
        raw = json.dumps(body, separators=(",", ":")).encode()
        status, reply = self._post(raw)
        if status == 401:
            self.link.force_refresh()  # signed_out errors from here end in _failed
            status, reply = self._post(raw)
        if status >= 400 or not reply.get("ok"):
            # Never a "signed out" code: a relay problem must not unlink the bot (only the account server can).
            raise penumbra.PenumbraError(f"relay: {reply.get('error') or f'http_{status}'}", status)
        # Delivered: drop what was sent (new lines may have arrived meanwhile; they stay).
        for _ in range(min(len(sent_log), len(self._log))):
            self._log.popleft()
        for _ in range(min(len(sent_acks), len(self._acks))):
            self._acks.popleft()
        if "settings" in body:
            self._settings_hash, self._want_settings = digest, False
        if "learned" in body:
            self._learned_hash, self._want_learned = learned_digest, False
        if reply.get("want_settings"):
            self._want_settings = True
        if reply.get("want_learned"):
            self._want_learned = True
        self._take_commands(reply.get("cmds") or [])
        self.state, self.detail, self.last_ok = "connected", "", time.time()
        running = bool((self._latest or (None, {}))[1].get("running"))
        return PUSH_RUNNING_S if running else PUSH_IDLE_S

    def _post(self, raw):
        return penumbra.request(f"{self.relay_url}?a=push", body=raw, content_type="application/json",
                                token=self.link.access_token(), timeout=10)

    def _take_commands(self, cmds):
        highest = self._cmd_seen
        for item in cmds[:20]:
            if not isinstance(item, dict):
                continue
            try:
                seq = int(item.get("seq"))
            except (TypeError, ValueError):
                continue
            if seq <= self._cmd_seen:
                continue  # already handled (reply lost and resent)
            highest = max(highest, seq)
            cmd, args = item.get("cmd"), item.get("args") if isinstance(item.get("args"), dict) else {}
            if cmd not in COMMANDS:
                self.ack(seq, False, "unknown command")
                continue
            if cmd not in VIEW_COMMANDS and not self.link.data.get("allow_control"):
                self.ack(seq, False, "view only: allow control in the bot's Remote tab")
                continue
            if cmd == "get_settings":
                self.resend_settings()
                self.ack(seq, True)
                continue
            if cmd in ("set_learn_switch", "reset_learned"):
                self._want_learned = True  # the page sees the change in the next push
            try:
                self.commands.put_nowait({"seq": seq, "cmd": cmd, "args": args})
                self.last_command = cmd
            except queue.Full:
                self.ack(seq, False, "busy")
        if highest != self._cmd_seen:
            self._cmd_seen = highest
            self.link.data["cmd_seen"] = highest
            self._save_link()

    # Payload ------------------------------------------------------------------------------------------------

    def _settings(self):
        from ..bot.config import settings
        groups = []
        for group, fields in settings():
            items = []
            for item in fields:
                meta = item.metadata
                default = item.default
                kind = "bool" if isinstance(default, bool) else "text" if isinstance(default, str) else (
                    "int" if isinstance(default, int) else "float")
                items.append({"name": item.name, "label": meta["label"], "kind": kind, "unit": meta["unit"],
                              "min": meta["min"], "max": meta["max"], "step": meta["step"],
                              "percent": meta["percent"], "hint": meta["hint"],
                              "value": getattr(self.config, item.name)})
            groups.append({"group": group, "items": items})
        if self._catalog_data is None and self._catalog is not None:
            self._catalog_data = self._catalog()
        return {"groups": groups, **(self._catalog_data or {})}

    def _state(self):
        snapshot, extras = self._latest or (None, {})
        cfg = self.config
        state = {
            "t": round(time.time(), 1),
            "bot": {k: extras.get(k) for k in ("running", "paused", "stop_in_orbit", "status", "caution", "caution_label",
                                               "run_s", "attached")},
            "mode": {"mode": cfg.mode, "seek_enemies": cfg.seek_enemies, "seek_blueprint": cfg.seek_blueprint,
                     "fly_planet": cfg.fly_planet, "farm_planet": cfg.farm_planet, "bp_system": cfg.bp_system,
                     "ship": cfg.ship},
            "bp": extras.get("bp"),
            "session": extras.get("session") or {},
            "connected": bool(snapshot is not None and snapshot.connected and snapshot.world is not None),
        }
        world = snapshot.world if snapshot is not None else None
        if world is None:
            return state
        from ..game import assets
        hull = (world.hitpoints / world.max_hitpoints) if world.hitpoints is not None and world.max_hitpoints else None
        state["ship"] = {
            "name": assets.display_name(world.ship_name) if world.ship_name else None, "level": world.ship_level,
            "hull": _num(hull, 3), "energy": world.energy,
            "cap": max(world.energy_cap or 0, extras.get("energy_max") or 0) or None,
            "cryonite": world.cryonite, "pilot_level": world.pilot_level, "xp": world.xp,
        }
        state["place"] = {
            "zone": world.zone, "planet": "Starbase" if world.at_starbase else assets.display_name(world.current_planet),
            "dead": world.dead, "travelling": world.travelling,
        }
        state["drones"] = [{"name": assets.display_name(d.desc) if d.desc else f"Drone {d.index + 1}",
                            "active": d.active, "charge": _num(d.charge, 2)} for d in world.drones or []]
        if world.zone == "planet" and world.x is not None:
            state["map"] = self._map(world, extras)
        return state

    @staticmethod
    def _map(world, extras):
        units = []
        for unit in world.units:
            if math.isnan(unit.x) or abs(unit.x - world.x) > MAP_RADIUS or abs(unit.y - world.y) > MAP_RADIUS:
                continue
            if unit.class_name.endswith("/tb"):
                kind = "energy"
            elif unit.is_player:
                kind = "player"
            elif unit.marker and "alien" in unit.marker:
                kind = "aggressive" if "agressive" in unit.marker or unit.hostile else "neutral"
            elif unit.is_item:
                kind = "gold" if unit.rare or "_rare" in (unit.mesh or "") else "loot"
            else:
                continue
            entry = [kind, _num(unit.x - world.x), _num(unit.y - world.y)]
            if kind in ("aggressive", "neutral"):
                from ..game import assets
                entry += [unit.level, assets.display_name(unit.desc or unit.name) if (unit.desc or unit.name) else "",
                          bool(unit.target_id is not None and unit.target_id == world.own_id)]
            units.append(entry)
            if len(units) >= MAX_UNITS:
                break
        heading = None
        if world.heading and any(world.heading):
            hx, hy = world.heading
            heading = _num(math.atan2(hy, hx), 3)
        route = [[_num(x - world.x), _num(y - world.y)] for x, y in (extras.get("route") or [])[:40]]
        # The page draws the planet's minimap under the dots: its file name (as the game names it, lower case; the
        # page loads maps/<name>) and the map rule (assets.world_to_map) put the ship on it.
        from ..game import assets
        from ..data import myzen
        energy = []
        try:
            info = myzen.planet_info(assets.display_name(world.current_planet)) or {}
            energy = [[_num(x - world.x), _num(y - world.y)] for x, y in info.get("energy", [])
                      if abs(x - world.x) <= MAP_RADIUS and abs(y - world.y) <= MAP_RADIUS][:30]
        except Exception:
            pass
        return {"x": _num(world.x), "y": _num(world.y), "radius": MAP_RADIUS, "heading": heading, "units": units,
                "route": route, "image": (world.planet_map or "").lower() or None, "energy": energy,
                "offset": assets.MAP_OFFSET, "span": assets.MAP_SPAN}


def describe(state):
    """Short label for the tab."""
    return {"off": "Off", "not_linked": "Not linked", "linking": "Waiting for approval",
            "connecting": "Connecting...", "connected": "Connected", "offline": "Offline",
            "signed_out": "Signed out"}.get(state, state)

