"""What the bot learned, as plain data: the Learned tab (gui/learned.py) and the remote page (web/pgbot) both show it.

report(learner, enabled, completed) -> {"header": text, "ship": key, "cards": [card]}
card: {"area", "label", "text", "on", "reset", "header": [column], "rows": [row], "notes": [text], "empty"}
row: {"entry": key or None (a row with a key has its own on/off switch, "on"), "cells": [[text, colour or None]]}
Colours are the window's names: "error", "ok", "warning", "muted", "text".
"""
from ..game import assets
from .learner import type_name
from .store import AREAS

# Farming levels the bot learns separately (the bot's caution: enemies above, at or below the ship's level).
LEVELS = {"lower": "Higher level", "same": "Same level", "high": "Lower level"}
EMPTY = "Nothing yet. This fills in while the bot plays."
MAX_ROWS = 40


def _pct(value):
    return "-" if value is None else f"{value * 100:.0f}%"


def _cell(value, first=False):
    if isinstance(value, tuple):
        return [value[0], value[1]]
    return [value, "text" if first else "muted"]


def _card(store, area, extra="", reset=True, header=None, rows=None, switch=False, notes=None):
    label, text = AREAS[area]
    out = []
    for entry, cells in rows or []:
        row = {"entry": entry if switch else None, "cells": [_cell(c, i == 0) for i, c in enumerate(cells)]}
        if switch and entry is not None:
            row["on"] = store.on(area, entry)
        out.append(row)
    return {"area": area, "label": label, "text": text + (f" {extra}" if extra else ""), "on": store.on(area),
            "reset": reset, "header": header or [], "rows": out, "notes": notes or [], "empty": EMPTY}


def _planet(learner, planet_map):
    name = learner.store.shared.get("planet_names", {}).get(planet_map)
    return name or assets.display_name(planet_map.split(".")[0]) or planet_map.split(".")[0]


def report(learner, enabled, completed=None, squads=False):
    """learner: a Learner over a copy of the data. completed: template id -> bool (blueprints done), or None."""
    store = learner.store
    cfg = store.ship.get("config") or {}
    ship_key = store.ship_key
    header = (f"{'Learning on' if enabled else 'Learning off'}  ·  "
              f"{store.ship.get('name') or (assets.display_name(ship_key) if ship_key else 'no ship yet')}"
              + (f", armor {cfg.get('armor')}, {len(cfg.get('items', []))} items, {len(cfg.get('drones', []))} drones"
                 if cfg else ""))
    cards = [_fights(learner), _card(store, "pulls", reset=False), _card(store, "retreat", reset=False),
             _routes(learner), _places(learner)]
    if squads:
        cards.append(_squads(learner))
    cards += [_drops(learner, completed), _card(store, "bp_hunt", reset=False), _drones(learner), _skills(learner)]
    return {"header": header, "ship": ship_key, "cards": cards}


def _fights(learner):
    store = learner.store
    enemies = store.ship.get("enemies", {})
    behaviour = store.shared.get("behaviour", {})
    by_name = {}
    for etype, e in enemies.items():
        by_name.setdefault(type_name(etype), []).append((etype, e))
    rows = []
    for name, entries in sorted(by_name.items(), key=lambda kv: -sum(e.get("fights", 0) for _, e in kv[1]))[:MAX_ROWS]:
        fights = sum(e.get("fights", 0.0) for _, e in entries)
        deaths = sum(e.get("deaths", 0.0) for _, e in entries)
        retreats = sum(e.get("retreats", 0.0) for _, e in entries)
        att = sum(e.get("att_s", 0.0) for _, e in entries)
        dmg = sum(e.get("dmg", 0.0) for _, e in entries)
        kills = sum(e.get("kills", 0.0) for _, e in entries)
        kill_s = sum(e.get("kill_s", 0.0) for _, e in entries)
        levels = sorted({etype.rsplit("|", 1)[1] for etype, _ in entries})
        helpers = behaviour.get(name)
        helper_text = (f"{helpers['helpers'] / helpers['fights']:.1f}" if helpers and helpers["fights"] >= 3 else "-")
        # Per farming level (enemies above / at / below the ship), from which group size it was deadly.
        profile = {}
        for etype, _ in entries:
            for level, text in learner.danger_profile(etype).items():
                if level not in profile or text[1] == "error":
                    profile[level] = text
        parts = [f"{LEVELS.get(level, level).lower()}: {text}" for level, (text, _) in sorted(profile.items())]
        color = "error" if any(c == "error" for _, c in profile.values()) else \
            "ok" if profile and all(c == "ok" for _, c in profile.values()) else "muted"
        verdict = ("; ".join(parts), color) if parts else (
            "still learning" if att < 20 or kills < 3 else "normal", "muted")
        rows.append((name, [f"{name} (L{', '.join(levels)})", f"{fights:.0f}", f"{deaths:.0f}", f"{retreats:.0f}",
                            f"{dmg / att * 100:.2f}%/s" if att >= 1 else "-",
                            f"{kill_s / kills:.1f} s" if kills >= 0.5 else "-", helper_text, verdict]))
    situations = store.ship.get("situations", {})
    risky = sorted(((k, s) for k, s in situations.items() if s.get("deaths", 0) >= 1),
                   key=lambda kv: -kv[1]["deaths"])[:8]
    notes = ["Fights it died in (enemy | group size | level mode): " + "; ".join(
        f"{k.replace('|', ' | ')}: {s['deaths']:.0f} of {s['n']:.0f}" for k, s in risky)] if risky else []
    return _card(store, "fight_risk", "Turn an enemy off to go back to the danger list in Settings for it.",
                 header=["Enemy", "Fights", "Deaths", "Retreats", "Damage", "Kill time", "Brings help", "Rating"],
                 rows=rows, switch=True, notes=notes)


def _routes(learner):
    rows = []
    for planet, cells in sorted(learner.store.shared.get("routes", {}).items()):
        active = learner.route_costs(planet)
        stuck = sum(c.get("stuck", 0) for c in cells.values())
        rows.append((planet, [_planet(learner, planet), f"{len(cells)}", f"{stuck}", f"{len(active)}"]))
    return _card(learner.store, "routes", "Turn a planet off to fly there without these detours.",
                 header=["Planet", "Stuck spots", "Times stuck", "Avoided"], rows=rows, switch=True)


def _places(learner):
    rows = []
    for planet, cells in sorted(learner.store.shared.get("places", {}).items()):
        totals = {}
        for cell in cells.values():
            for etype, n in cell.items():
                totals[type_name(etype)] = totals.get(type_name(etype), 0) + n
        top = ", ".join(n for n, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:4])
        rows.append((None, [_planet(learner, planet), f"{len(cells)}", top]))
    return _card(learner.store, "patrol", header=["Planet", "Areas", "Seen most"], rows=rows)


def _squads(learner):
    rows = []
    for planet, types in sorted(learner.store.shared.get("squads", {}).items()):
        for etype, e in sorted(types.items(), key=lambda kv: -kv[1]["moving"]):
            if e["moving"] < 1:
                continue
            rating = ("squad", "error") if learner.is_squad_type(planet, etype) else \
                ("not a squad", "ok") if learner.squad_ordinary(planet, etype) else "still learning"
            rows.append((None, [planet, etype.replace("|", " L"), f"{e['seen']:.0f} min", f"{e['moving']:.0f} min",
                                rating]))
    return _card(learner.store, "squads", header=["Planet", "Enemy", "Seen", "Flying in a group", "Rating"],
                 rows=rows[:MAX_ROWS])


def _drops(learner, completed):
    rows = []
    drops = learner.store.shared.get("drops", {})
    by_name = {}
    for etype, d in drops.items():
        by_name.setdefault(type_name(etype), []).append((etype, d))
    for name, entries in sorted(by_name.items(), key=lambda kv: -sum(d.get("kills", 0) for _, d in kv[1]))[:MAX_ROWS]:
        kills = sum(d.get("kills", 0.0) for _, d in entries)
        if kills < 1:
            continue
        total = {key: sum(d.get(key, 0.0) for _, d in entries) for key in ("cryo", "xp", "bp", "gold")}
        seen = {key: any(key in d for _, d in entries) for key in ("cryo", "xp")}
        names = {}
        for _, d in entries:
            for bp, n in d.get("names", {}).items():
                names[bp] = names.get(bp, 0) + n
        rates = [learner.completed_rate(etype, completed) for etype, _ in entries] if completed else []
        rates = [r[0] for r in rates if r is not None]
        levels = sorted({etype.rsplit("|", 1)[1] for etype, _ in entries})
        top = sorted(names.items(), key=lambda kv: -kv[1])[:3]
        rows.append((None, [f"{name} (L{', '.join(levels)})", f"{kills:.0f}",
                            f"{total['cryo'] / kills:,.0f}" if seen["cryo"] else "-",
                            f"{total['xp'] / kills:,.0f}" if seen["xp"] else "-",
                            f"{total['bp'] / kills:.2f}" if total["bp"] else "-",
                            f"{total['gold']:.0f}" if total["gold"] else "-",
                            f"{max(rates):.2f}" if rates else "-",
                            ", ".join(n for n, _ in top) or "-"]))
    return _card(learner.store, "value", header=["Enemy", "Kills", "Cryonite per kill", "XP per kill",
                                                 "Blueprints per kill", "Gold", "Completed per kill",
                                                 "Blueprints seen"], rows=rows)


def _drones(learner):
    rows = []
    for desc, e in learner.store.ship.get("drones", {}).items():
        unit = {"energy": "energy per second", "cryonite": "cryonite per pickup", "combat": "damage taken"}.get(
            e["role"], "")
        # One row per farming level it is known at (learned separately: lower, same, higher level enemies).
        known = [(level, learner.drone_compare(desc, level)) for level in LEVELS]
        known = [(level, k) for level, k in known if k is not None] or [(None, None)]
        for i, (level, k) in enumerate(known):
            ok = learner.drone_ok(desc, level) if level else None
            rows.append((desc if i == 0 else None, [
                assets.display_name(desc) if i == 0 else "", e["role"] if i == 0 else "",
                LEVELS[level] if level else "-",
                f"{e['active_s'] / 60:.0f} min" if i == 0 else "", f"{e['cost']:,.0f}" if i == 0 else "",
                f"{k[0]:.3g}" if k else "-", f"{k[1]:.3g}" if k else "-", unit,
                ("worth it", "ok") if ok else ("not worth it", "warning") if ok is False else "still learning"]))
    return _card(learner.store, "drones", "Turn a drone off to use it the normal way again.",
                 header=["Drone", "Used for", "Enemies", "Time on", "Recharge cost", "With it", "Without", "Compared",
                         "Rating"], rows=rows, switch=True)


def _skills(learner):
    rows = []
    skills = learner.store.ship.get("skills", {})
    for desc, e in sorted(skills.items(), key=lambda kv: -max(kv[1].get("n", 0), kv[1].get("uses", 0),
                                                                 kv[1].get("presses", 0))):
        kind = "deploy" if learner.is_deploy(desc) else e.get("kind") or ("attack" if e.get("n") else "other")
        uses = max(e.get("n", 0), e.get("uses", 0), e.get("presses", 0))
        effect, rating = "-", "still learning"
        if kind == "attack" and e.get("n"):
            effect = f"{_pct(e['drop'] / e['n'])} of the target's hull"
            scores = [(LEVELS[level], learner.skill_score(desc, level)) for level in LEVELS]
            scores = [f"{name.lower()} {score:.2f}" for name, score in scores if score is not None]
            rating = "score " + ", ".join(scores) if scores else "still learning"
        elif kind in ("defense", "deploy"):
            parts = []
            if e.get("measured", 0) >= 5 and e.get("expected", 0) >= 0.1:
                kept = max(0.0, 1 - e["taken"] / e["expected"])
                parts.append(f"keeps off {kept * 100:.0f}% of the damage")
                rating = ("works", "ok") if kept >= 0.05 else ("no effect seen", "warning")
            if e.get("kills", 0) >= 6:
                faster = 1 - e["ratio"] / e["kills"]
                parts.append(f"kills {faster * 100:.0f}% faster")
                if faster >= 0.05:
                    rating = ("works", "ok")
            if e.get("heal_uses", 0) >= 3 and e.get("healed", 0) / e["heal_uses"] >= 0.02:
                parts.append(f"heals {e['healed'] / e['heal_uses'] * 100:.0f}% hull")
                rating = ("works", "ok")
            if e.get("wasted"):
                parts.append(f"{e['wasted']:.0f} times with little coming in")
            effect = ", ".join(parts) or "-"
        elif kind == "buff" and e.get("kills", 0) >= 6:
            faster = 1 - e["ratio"] / e["kills"]
            effect = f"kills {faster * 100:.0f}% faster"
            rating = ("works", "ok") if faster >= 0.03 else ("no effect seen", "warning")
        label = {"attack": "Attack", "defense": "Shield", "deploy": "Turret / drone", "buff": "Buff"}.get(kind, "Other")
        rows.append((None, [assets.display_name(desc), label, f"{uses:.0f}", effect, f"{e.get('energy', 0):.0f}",
                            rating]))
    repair = learner.store.ship.get("repair") or {}
    if repair.get("n", 0) >= 1:
        rows.append((None, ["Repair", "Repair", f"{repair['n']:.0f}", f"heals about {_pct(repair['heal'])}", "-", "-"]))
    return _card(learner.store, "skills", header=["Skill", "Type", "Uses", "Effect", "Energy", "Rating"], rows=rows)
