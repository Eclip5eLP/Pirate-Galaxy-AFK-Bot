"""Read HotSpot's exported layout tables (gHotSpotVMStructs and friends)."""
from dataclasses import dataclass

from . import pe
from .process import Process


@dataclass(frozen=True)
class VMField:
    type_name: str
    field_name: str
    type_string: str
    is_static: bool
    offset: int
    address: int


class VMStructs:
    """Layout information for one running JVM, keyed by the VM's own names."""

    def __init__(self, process: Process):
        self.process = process
        modules = process.modules()
        if "jvm.dll" not in modules:
            raise RuntimeError(f"process {process.pid} has no jvm.dll")
        self.base, self.path = modules["jvm.dll"]
        self._exports = pe.exports(self.path)
        self.fields = {}
        self.sizes = {}
        self.constants = {}
        self._read_fields()
        self._read_types()
        self._read_constants("gHotSpotVMIntConstant", "gHotSpotVMIntConstants", 4)
        self._read_constants("gHotSpotVMLongConstant", "gHotSpotVMLongConstants", 8)

    def _export_address(self, name):
        return self.base + self._exports[name]

    def _export_u64(self, name):
        return self.process.u64(self._export_address(name))

    def _table(self, name):
        return self.process.u32(self._export_address(name))

    def _read_fields(self):
        p = self.process
        entry = self._table("gHotSpotVMStructs")
        stride = self._export_u64("gHotSpotVMStructEntryArrayStride")
        type_at = self._export_u64("gHotSpotVMStructEntryTypeNameOffset")
        name_at = self._export_u64("gHotSpotVMStructEntryFieldNameOffset")
        string_at = self._export_u64("gHotSpotVMStructEntryTypeStringOffset")
        static_at = self._export_u64("gHotSpotVMStructEntryIsStaticOffset")
        offset_at = self._export_u64("gHotSpotVMStructEntryOffsetOffset")
        address_at = self._export_u64("gHotSpotVMStructEntryAddressOffset")
        while True:
            type_ptr = p.u32(entry + type_at)
            if type_ptr == 0:
                return
            type_name = p.cstr(type_ptr)
            field_name = p.cstr(p.u32(entry + name_at))
            string_ptr = p.u32(entry + string_at)
            is_static = p.u32(entry + static_at) != 0
            field = VMField(
                type_name,
                field_name,
                p.cstr(string_ptr) if string_ptr else "",
                is_static,
                p.u64(entry + offset_at),
                p.u32(entry + address_at),
            )
            self.fields[(type_name, field_name)] = field
            entry += stride

    def _read_types(self):
        p = self.process
        entry = self._table("gHotSpotVMTypes")
        stride = self._export_u64("gHotSpotVMTypeEntryArrayStride")
        name_at = self._export_u64("gHotSpotVMTypeEntryTypeNameOffset")
        size_at = self._export_u64("gHotSpotVMTypeEntrySizeOffset")
        while True:
            name_ptr = p.u32(entry + name_at)
            if name_ptr == 0:
                return
            self.sizes[p.cstr(name_ptr)] = p.u64(entry + size_at)
            entry += stride

    def _read_constants(self, prefix, table, width):
        p = self.process
        entry = self._table(table)
        stride = self._export_u64(prefix + "EntryArrayStride")
        name_at = self._export_u64(prefix + "EntryNameOffset")
        value_at = self._export_u64(prefix + "EntryValueOffset")
        while True:
            name_ptr = p.u32(entry + name_at)
            if name_ptr == 0:
                return
            value = p.u32(entry + value_at) if width == 4 else p.u64(entry + value_at)
            if width == 4 and value >= 0x80000000:
                value -= 0x100000000
            self.constants[p.cstr(name_ptr)] = value
            entry += stride

    def offset(self, type_name, field_name):
        """Offset of a non-static field."""
        return self.fields[(type_name, field_name)].offset

    def static_address(self, type_name, field_name):
        """Address of a static VM field."""
        return self.fields[(type_name, field_name)].address
