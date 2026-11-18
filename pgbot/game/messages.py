"""Read the in-game message log and pick out pickup messages. Read-only.

The client keeps the last messages as display text: singleton `aom.bfI` -> list `bfK` (class `aom$if`, base `b`)
-> ArrayList `dj` of `aom$do` entries with text `bfV`. Newest entries are inserted at index 0.
"""
import re
from dataclasses import dataclass

from ..mem import java
from ..mem.jvm import JVM

ENTRY_CLASS = "com/splitscreen/aom$do"

# Display text patterns from the English language file.
PICKUP_PATTERNS = (
    ("cryonite", re.compile(r"^([\d,.]+) Cryonite received", re.I)),
    ("blueprint", re.compile(r"^Blueprint '(.+)' collected\s*(.*)$", re.I)),
    ("blueprint", re.compile(r"^Converted (?:known )?blueprint '(.+)' into ([\d,.]+) energy", re.I)),
    ("graviton", re.compile(r"graviton", re.I)),
    ("item", re.compile(r"(collected|found)", re.I)),
)
IGNORED = (re.compile(r"^Energy reloaded", re.I), re.compile(r"Energy Orb", re.I), re.compile(r"^Current energy", re.I))


@dataclass(frozen=True)
class Message:
    text: str
    kind: str | None  # cryonite, blueprint, graviton, item, or None for other messages


def classify(text):
    for pattern in IGNORED:
        if pattern.search(text):
            return None
    for kind, pattern in PICKUP_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def read_log(jvm: JVM):
    """Return the message texts, newest first. Call inside jvm.consistent()."""
    try:
        log = jvm.get_static("com/splitscreen/aom", "bfI")
    except KeyError:
        return []
    if not log:
        return []
    holder = jvm.get(log, "bfK")
    if not holder:
        return []
    texts = []
    for entry in java.array_list(jvm, jvm.get(holder, "dj")):
        if entry and jvm.class_name(entry) == ENTRY_CLASS:
            texts.append(java.string(jvm, jvm.get(entry, "bfV")) or "")
    return texts


class MessageWatcher:
    """Turns repeated reads of the log into a stream of new messages."""

    def __init__(self):
        self._previous = None

    def new_messages(self, texts):
        """texts: newest first. Returns new Message objects, oldest first."""
        if self._previous is None:
            self._previous = list(texts)
            return []
        new = []
        if self._previous:
            # New entries are added at the front; find where the old list starts again.
            for start in range(len(texts) + 1):
                if texts[start:start + len(self._previous)] == self._previous[:len(texts) - start]:
                    new = texts[:start]
                    break
            else:
                new = texts
        else:
            new = texts
        self._previous = list(texts)
        return [Message(text, classify(text)) for text in reversed(new)]
