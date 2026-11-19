# PG Bot

An AFK farming bot for **Pirate Galaxy** (Windows). It reads what the game shows from the game's memory (read only:
nothing is ever written to the game) and plays with normal mouse and keyboard input sent to the game window, so you
can keep using your PC while it farms.

> **Use at your own risk.** Pirate Galaxy's Code of Conduct forbids software that automates play. Accounts can be
> muted, banned or suspended for it. The bot does not change or patch the game, but that does not make it allowed.

## What it does

Pick a mode in the window:

- **Park & Patrol**: farm around the start spot; sweeps the open ground nearby when nothing shows up.
- **Park**: stay at the start spot and fight whatever comes into range.
- **Wander**: roam the whole planet and fight everything on the way.
- **Seek Enemy**: hunt only the named enemies (full names), at their spawn areas when known.
- **Seek Blueprint**: hunt the enemies that drop a chosen blueprint, on its planet.
- **BP Completion**: complete a star system's blueprints, planet by planet. Progress is read in a hangar; it can
  switch to a chosen ship there.
- **Collect Energy**: fly between energy fields and collect orbs.
- **Fly to Planet**: fly to a planet's orbit (through star gates if needed) and stop there.

In every mode it repairs, refills energy, collects loot (gold blueprints first), keeps away from dangerous squads,
retreats when the hull gets low, rescues itself after being shot down, uses drones and skills, and learns per ship
which fights, skills and drones work (see the **Learned** tab). It can start the game and log in by itself, and
restart it after a crash.

When travelling between planets it uses **Hyperjump** if the jump leaves energy at or above **Refill below energy**
(setting **Hyperjump while travelling**, on by default). Asteroid-field planets without terrain are handled too.

## Requirements

- Windows 10 or 11.
- Pirate Galaxy installed with its own launcher (the standalone client). The game must be logged in once with
  **Remember me**, so the bot can log in by itself.
- Python 3.13 (64-bit) from [python.org](https://www.python.org/downloads/windows/), with "tcl/tk" (on by default).

## Install

Download or clone this branch, then in its folder:

```
py -3.13 -m pip install -r requirements.txt
```

## Start

```
py -3.13 run_gui.py
```

1. **Settings** tab: check the game folder (found by itself in most cases) and the pilot row to log in with.
2. **Farming** tab: pick a mode and its planet / enemies / blueprint, then press **Start**.
3. Put the game on a second virtual desktop (Win + Tab) if you want to keep working: the bot plays in the background.

Stopping: **Stop in orbit** finishes the current fight and nearby loot, takes off and stops once in orbit (the safe
way). **Stop** stops at once. The game window can be any size; the bot finds its screens by their position on the
window edges and centre.

## Your data

Settings, what the bot learned and cached game data are kept in `%LOCALAPPDATA%\pgbot`. Run logs go to the `logs`
folder next to `run_gui.py`. Delete the folder to start fresh.

## Remote page (optional)

The **Remote** tab can link the bot to a web page for watching and controlling it from a phone or another PC (sign in
with a Penumbra account). Nothing is sent until you link it there, and the page can only control the bot after you
turn on **Allow control**.

The page shows the status, ship, travel destination and time left, a live minimap, the log, BP Completion progress,
gains per hour and the **Learned** report. It has the in-game chat, newest line first. With control allowed it can
start, pause and stop the bot (also in orbit), change the mode and settings, and send chat messages to Global,
Alliance, Clan or an open private chat. The bot types them into the game's chat box when it is not fighting.
