"""Resolve Java classes and read Java objects in a running 32-bit HotSpot 8 JVM.

Object addresses are valid only inside one consistent() call, because G1 moves objects.
Klass addresses and field offsets are stable, so they are cached.
"""
import struct
import time
from dataclasses import dataclass

from .process import MemoryReadError, Process
from .vmstructs import VMStructs

JVM_ACC_STATIC = 0x0008
ARRAY_LENGTH_OFFSET = 8
ARRAY_DATA_OFFSET = 12
ARRAY_WIDE_DATA_OFFSET = 16
MAX_ARRAY_LENGTH = 1 << 24
MIN_OBJECT_ALIGNMENT = 8
OBJECT_READ_SIZE = 64
FIELD_FORMATS = {"I": "<i", "F": "<f", "B": "<b", "S": "<h", "J": "<q", "D": "<d"}
FIELD_SIZES = {"L": 4, "[": 4, "I": 4, "F": 4, "Z": 1, "B": 1, "S": 2, "C": 2, "J": 8, "D": 8}


class TornRead(Exception):
    """Data looked inconsistent, probably because the JVM changed it during the read. Retrying is correct."""


class SnapshotFailed(Exception):
    """A consistent read could not be completed within the retry limit."""


@dataclass(frozen=True)
class Field:
    name: str
    signature: str
    offset: int
    is_static: bool


class KlassInfo:
    def __init__(self, address, name, super_address, layout_helper, fields):
        self.address = address
        self.name = name
        self.super_address = super_address
        self.layout_helper = layout_helper
        self.fields = fields
        self.resolved = {}

    @property
    def is_array(self):
        return self.layout_helper < 0


class JVM:
    def __init__(self, process: Process):
        self.process = process
        self.vm = VMStructs(process)
        vm = self.vm
        self._klass_ptr_at = vm.offset("oopDesc", "_metadata._klass")
        self._k_name = vm.offset("Klass", "_name")
        self._k_super = vm.offset("Klass", "_super")
        self._k_layout = vm.offset("Klass", "_layout_helper")
        self._k_mirror = vm.offset("Klass", "_java_mirror")
        self._ik_fields = vm.offset("InstanceKlass", "_fields")
        self._ik_count = vm.offset("InstanceKlass", "_java_fields_count")
        self._ik_constants = vm.offset("InstanceKlass", "_constants")
        self._cp_size = vm.sizes["ConstantPool"]
        self._sym_length = vm.offset("Symbol", "_length")
        self._sym_body = vm.offset("Symbol", "_body")
        self._u2_data = vm.offset("Array<u2>", "_data")
        self._slots = vm.constants["FieldInfo::field_slots"]
        self._fi_flags = vm.constants["FieldInfo::access_flags_offset"]
        self._fi_name = vm.constants["FieldInfo::name_index_offset"]
        self._fi_sig = vm.constants["FieldInfo::signature_index_offset"]
        self._fi_low = vm.constants["FieldInfo::low_packed_offset"]
        self._fi_high = vm.constants["FieldInfo::high_packed_offset"]
        self._tag_size = vm.constants["FIELDINFO_TAG_SIZE"]
        self._tag_mask = vm.constants["FIELDINFO_TAG_MASK"]
        self._tag_offset = vm.constants["FIELDINFO_TAG_OFFSET"]

        heap = process.u32(vm.static_address("Universe", "_collectedHeap"))
        reserved = heap + vm.offset("CollectedHeap", "_reserved")
        self.heap_start = process.u32(reserved + vm.offset("MemRegion", "_start"))
        words = process.u32(reserved + vm.offset("MemRegion", "_word_size"))
        self.heap_end = self.heap_start + words * 4
        self._gc_active_at = heap + vm.offset("CollectedHeap", "_is_gc_active")
        self._gc_count_at = heap + vm.offset("CollectedHeap", "_total_collections")

        self._klasses = {}
        self._by_name = {}
        self._objects = {}
        self._snapshot_active = False
        self.retries = 0
        self.failures = 0

    # Consistency ---------------------------------------------------------------------------------------------

    def gc_state(self):
        """Return (GC running, number of collections started)."""
        active = self.process.read(self._gc_active_at, 1)[0] != 0
        return active, self.process.u32(self._gc_count_at)

    def consistent(self, read, attempts=50, wait_s=0.002):
        """Run read() until no GC occurred during it and no inconsistent data was seen.

        read() must follow object paths from a root each time and keep no addresses from earlier attempts.
        """
        last_error = None
        for attempt in range(attempts):
            if attempt:
                self.retries += 1
                time.sleep(wait_s)
            active, count = self.gc_state()
            if active:
                last_error = "GC active"
                continue
            self._objects.clear()
            self._snapshot_active = True
            try:
                result = read()
            except (TornRead, MemoryReadError) as error:
                last_error = error
                continue
            finally:
                self._snapshot_active = False
                self._objects.clear()
            active, after = self.gc_state()
            if active or after != count:
                last_error = "GC during read"
                continue
            return result
        self.failures += 1
        raise SnapshotFailed(str(last_error))

    # Classes -------------------------------------------------------------------------------------------------

    def _symbol(self, address):
        address &= ~1
        length = self.process.read(address + self._sym_length, 2)
        size = struct.unpack("<H", length)[0]
        return self.process.read(address + self._sym_body, size).decode("latin-1")

    def _parse_fields(self, klass):
        p = self.process
        count = p.u16(klass + self._ik_count)
        if count == 0:
            return {}
        array = p.u32(klass + self._ik_fields)
        constants = p.u32(klass + self._ik_constants)
        raw = p.read(array + self._u2_data, count * self._slots * 2)
        slots = struct.unpack(f"<{count * self._slots}H", raw)
        pool = constants + self._cp_size
        fields = {}
        for i in range(count):
            entry = slots[i * self._slots:(i + 1) * self._slots]
            low, high = entry[self._fi_low], entry[self._fi_high]
            if low & self._tag_mask != self._tag_offset:
                continue
            name = self._symbol(p.u32(pool + entry[self._fi_name] * 4))
            signature = self._symbol(p.u32(pool + entry[self._fi_sig] * 4))
            fields[name] = Field(
                name,
                signature,
                (low | (high << 16)) >> self._tag_size,
                bool(entry[self._fi_flags] & JVM_ACC_STATIC),
            )
        return fields

    def klass(self, address):
        """Return KlassInfo for a Klass address. Raises TornRead if it does not look like a Klass."""
        info = self._klasses.get(address)
        if info is not None:
            return info
        p = self.process
        if address == 0 or address & 3:
            raise TornRead(f"bad klass pointer {address:#x}")
        try:
            name = self._symbol(p.u32(address + self._k_name))
            layout = struct.unpack("<i", p.read(address + self._k_layout, 4))[0]
            mirror = p.u32(address + self._k_mirror)
        except MemoryReadError as error:
            raise TornRead(f"unreadable klass {address:#x}") from error
        if not name or not name.isprintable() or not self.heap_start <= mirror < self.heap_end:
            raise TornRead(f"implausible klass {address:#x}")
        fields = self._parse_fields(address) if layout > 0 else {}
        info = KlassInfo(address, name, p.u32(address + self._k_super), layout, fields)
        self._klasses[address] = info
        return info

    def _refresh_class_names(self):
        vm, p = self.vm, self.process
        dictionary = p.u32(vm.static_address("SystemDictionary", "_dictionary"))
        size = p.u32(dictionary + vm.offset("BasicHashtable<mtInternal>", "_table_size"))
        buckets = p.u32(dictionary + vm.offset("BasicHashtable<mtInternal>", "_buckets"))
        next_at = vm.offset("BasicHashtableEntry<mtInternal>", "_next")
        literal_at = vm.offset("IntptrHashtableEntry", "_literal")
        for head in struct.unpack(f"<{size}I", p.read(buckets, size * 4)):
            entry = head & ~1
            while entry:
                klass = p.u32(entry + literal_at) & ~1
                self._by_name[self._symbol(p.u32(klass + self._k_name))] = klass
                entry = p.u32(entry + next_at) & ~1

    def find_class(self, name):
        """Find a loaded class by internal name such as java/util/ArrayList. Returns KlassInfo or None."""
        if name not in self._by_name:
            self._refresh_class_names()
        address = self._by_name.get(name)
        return self.klass(address) if address else None

    def field_of(self, info, name):
        """Find a field on a class or its superclasses."""
        field = info.resolved.get(name)
        if field is not None:
            return field
        current = info
        while True:
            field = current.fields.get(name)
            if field is not None:
                info.resolved[name] = field
                return field
            if not current.super_address:
                raise KeyError(f"{info.name} has no field {name}")
            current = self.klass(current.super_address)

    # Objects -------------------------------------------------------------------------------------------------

    def check_oop(self, oop):
        if oop % MIN_OBJECT_ALIGNMENT or not self.heap_start <= oop < self.heap_end:
            raise TornRead(f"bad object pointer {oop:#x}")
        return oop

    def klass_of(self, oop):
        return self.klass(self.process.u32(self.check_oop(oop) + self._klass_ptr_at))

    def class_name(self, oop):
        return self.klass_of(oop).name


    def _object(self, oop):
        """Return (KlassInfo, bytes) for an instance, using one read for most objects."""
        cached = self._objects.get(oop) if self._snapshot_active else None
        if cached is not None:
            return cached
        self.check_oop(oop)
        p = self.process
        try:
            data = p.read(oop, OBJECT_READ_SIZE)
        except MemoryReadError:
            data = p.read(oop, 8)
        info = self.klass(struct.unpack_from("<I", data, self._klass_ptr_at)[0])
        size = info.layout_helper & ~1
        if not info.is_array and size > len(data):
            data = p.read(oop, size)
        result = (info, data)
        if self._snapshot_active:
            self._objects[oop] = result
        return result

    def _decode(self, data, offset, signature):
        kind = signature[0]
        if kind in "L[":
            oop = struct.unpack_from("<I", data, offset)[0]
            return self.check_oop(oop) if oop else 0
        if kind == "Z":
            return data[offset] != 0
        if kind == "C":
            return chr(struct.unpack_from("<H", data, offset)[0])
        return struct.unpack_from(FIELD_FORMATS[kind], data, offset)[0]

    def has_field(self, oop, name):
        info, _ = self._object(oop)
        try:
            self.field_of(info, name)
        except KeyError:
            return False
        return True

    def get(self, oop, name):
        """Read an instance field. References come back as object addresses, null as 0."""
        info, data = self._object(oop)
        try:
            field = self.field_of(info, name)
        except KeyError as error:
            raise TornRead(str(error)) from error
        if info.is_array or field.offset + FIELD_SIZES[field.signature[0]] > len(data):
            raise TornRead(f"field {name} is outside {info.name}")
        return self._decode(data, field.offset, field.signature)

    def get_static(self, class_name, name):
        """Read a static field through the class mirror. Raises KeyError if the class is not loaded."""
        info = self.find_class(class_name)
        if info is None:
            raise KeyError(f"class {class_name} is not loaded")
        field = self.field_of(info, name)
        mirror = self.check_oop(self.process.u32(info.address + self._k_mirror))
        data = self.process.read(mirror + field.offset, FIELD_SIZES[field.signature[0]])
        return self._decode(data, 0, field.signature)

    def array_length(self, oop):
        length = self.process.u32(self.check_oop(oop) + ARRAY_LENGTH_OFFSET)
        if length > MAX_ARRAY_LENGTH:
            raise TornRead(f"implausible array length {length}")
        return length

    def array_bytes(self, oop, element_size):
        """Return the raw contents of a primitive or reference array."""
        length = self.array_length(oop)
        data = ARRAY_WIDE_DATA_OFFSET if element_size == 8 else ARRAY_DATA_OFFSET
        return self.process.read(oop + data, length * element_size)

    def int_array(self, oop):
        raw = self.array_bytes(oop, 4)
        return list(struct.unpack(f"<{len(raw) // 4}i", raw))

    def oop_array(self, oop):
        raw = self.array_bytes(oop, 4)
        return list(struct.unpack(f"<{len(raw) // 4}I", raw))
