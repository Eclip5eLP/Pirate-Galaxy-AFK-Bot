"""Parse the export table of a PE file on disk."""
import struct


def exports(path):
    """Return {export name: RVA} for a PE file. Forwarded exports are not distinguished."""
    with open(path, "rb") as f:
        data = f.read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError(f"{path} is not a PE file")
    section_count, optional_size = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    is64 = struct.unpack_from("<H", data, optional)[0] == 0x20B
    export_rva = struct.unpack_from("<I", data, optional + (112 if is64 else 96))[0]

    sections = []
    for i in range(section_count):
        header = optional + optional_size + i * 40
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, header + 8)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_pointer))

    def offset(rva):
        for virtual_address, size, raw_pointer in sections:
            if virtual_address <= rva < virtual_address + size:
                return raw_pointer + rva - virtual_address
        raise ValueError(f"RVA {rva:#x} is outside all sections")

    table = offset(export_rva)
    name_count = struct.unpack_from("<I", data, table + 24)[0]
    functions, names, ordinals = struct.unpack_from("<III", data, table + 28)
    functions, names, ordinals = offset(functions), offset(names), offset(ordinals)

    result = {}
    for i in range(name_count):
        name_at = offset(struct.unpack_from("<I", data, names + 4 * i)[0])
        name = data[name_at:data.index(b"\0", name_at)].decode("latin-1")
        ordinal = struct.unpack_from("<H", data, ordinals + 2 * i)[0]
        result[name] = struct.unpack_from("<I", data, functions + 4 * ordinal)[0]
    return result
