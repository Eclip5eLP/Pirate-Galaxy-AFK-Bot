"""Decode common Java objects: strings, boxed values, collections, Trove maps and the game's PropertyMap."""
from functools import lru_cache

from .jvm import JVM, TornRead

TROVE_FULL = 1
PROPERTY_MAP = "com/splitscreen/common/data/properties/PropertyMap"


@lru_cache(maxsize=4096)
def java_hash(text):
    """Java String.hashCode(), used as the PropertyMap key."""
    units = text.encode("utf-16-be")
    value = 0
    for i in range(0, len(units), 2):
        value = (31 * value + (units[i] << 8 | units[i + 1])) & 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def string(jvm: JVM, oop):
    """Read a java.lang.String, or None for null."""
    if oop == 0:
        return None
    info = jvm.klass_of(oop)
    if info.name != "java/lang/String":
        raise TornRead(f"expected String, found {info.name}")
    value = jvm.get(oop, "value")
    if value == 0:
        raise TornRead("String without value")
    return jvm.array_bytes(value, 2).decode("utf-16-le", "replace")


def unbox(jvm: JVM, oop):
    """Convert a boxed value or String to Python. Other objects stay as addresses, null becomes None."""
    if oop == 0:
        return None
    name = jvm.class_name(oop)
    if name == "java/lang/String":
        return string(jvm, oop)
    if name in ("java/lang/Integer", "java/lang/Float", "java/lang/Long", "java/lang/Double", "java/lang/Boolean",
                "java/lang/Short", "java/lang/Byte"):
        return jvm.get(oop, "value")
    return oop


def array_list(jvm: JVM, oop):
    """Return the element addresses of a java.util.ArrayList."""
    size = jvm.get(oop, "size")
    data = jvm.get(oop, "elementData")
    if data == 0:
        if size:
            raise TornRead("ArrayList without storage")
        return []
    elements = jvm.oop_array(data)
    if not 0 <= size <= len(elements):
        raise TornRead(f"ArrayList size {size} with capacity {len(elements)}")
    return elements[:size]


def _bucket_table(jvm: JVM, oop, count):
    table = jvm.get(oop, "table")
    if table == 0:
        return {}
    result = {}
    for head in jvm.oop_array(table):
        entry = head
        steps = 0
        while entry:
            result[jvm.get(entry, "key")] = jvm.get(entry, "value")
            entry = jvm.get(entry, "next")
            steps += 1
            if steps > count + 1:
                raise TornRead("bucket chain longer than map size")
    return result


def hashtable(jvm: JVM, oop):
    """Return {key address: value address} of a java.util.Hashtable."""
    return _bucket_table(jvm, oop, jvm.get(oop, "count"))


def hash_map(jvm: JVM, oop):
    """Return {key address: value address} of a java.util.HashMap."""
    return _bucket_table(jvm, oop, jvm.get(oop, "size"))


def tint_object_map(jvm: JVM, oop):
    """Return {int key: value address} of a gnu.trove.TIntObjectHashMap."""
    keys = jvm.get(oop, "_set")
    values = jvm.get(oop, "_values")
    states = jvm.get(oop, "_states")
    if not keys or not values or not states:
        return {}
    key_list = jvm.int_array(keys)
    value_list = jvm.oop_array(values)
    state_list = jvm.array_bytes(states, 1)
    if not len(key_list) == len(value_list) == len(state_list):
        raise TornRead("Trove map arrays have different lengths")
    return {key_list[i]: value_list[i] for i, state in enumerate(state_list) if state == TROVE_FULL}


class Properties:
    """One read of a PropertyMap. Create a new one for each snapshot."""

    def __init__(self, jvm: JVM, oop):
        if jvm.class_name(oop) != PROPERTY_MAP:
            raise TornRead("expected PropertyMap")
        self._jvm = jvm
        overrides = jvm.get(oop, "HY")
        self._base = tint_object_map(jvm, jvm.get(oop, "HX"))
        self._values = dict(self._base)
        if overrides:
            self._values.update(tint_object_map(jvm, overrides))

    def reference(self, key, default=None):
        """The base value before runtime changes, as the client's getIntReference() returns. For hitpoints: maximum."""
        value = unbox(self._jvm, self._base.get(java_hash(key), 0))
        return default if value is None else value

    def __contains__(self, key):
        return java_hash(key) in self._values

    def raw(self, key):
        """Return the value's object address, or 0 if the key is not set."""
        return self._values.get(java_hash(key), 0)

    def get(self, key, default=None):
        value = unbox(self._jvm, self.raw(key))
        return default if value is None else value

    def get_int(self, key):
        """The value only if it is a boxed whole number, else None. get() hands back an object's address for other
        classes: a torn read of a container that was being replaced gave addresses (82,133,840) as part counts, and
        677 blueprints counted as complete (2026-11-20)."""
        oop = self.raw(key)
        if not oop:
            return None
        if self._jvm.class_name(oop) not in ("java/lang/Integer", "java/lang/Short", "java/lang/Byte",
                                             "java/lang/Long"):
            return None
        value = self._jvm.get(oop, "value")
        return value if isinstance(value, int) else None

    def __len__(self):
        return len(self._values)
