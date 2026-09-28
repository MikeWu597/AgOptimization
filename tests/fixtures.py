"""Synthetic worlds only; no user's Minecraft data is needed by tests."""

import gzip
from pathlib import Path
import struct
import zlib


def tag(kind, name, payload):
    name = name.encode("utf-8")
    return bytes([kind]) + struct.pack(">H", len(name)) + name + payload


def chunk_nbt(value=0, *, old=False, kind=4, missing=False, coords=None, extra=b""):
    payload = b""
    if not missing:
        payload += tag(kind, "InhabitedTime", struct.pack(">q" if kind == 4 else ">i", value))
    if coords:
        payload += tag(3, "xPos", struct.pack(">i", coords[0]))
        payload += tag(3, "zPos", struct.pack(">i", coords[1]))
    payload += extra + b"\0"
    if old:
        payload = tag(10, "Level", payload) + b"\0"
    return b"\x0a\x00\x00" + payload


def lz4_stream(data, compressed=True):
    import lz4.block
    import xxhash
    blocks = []
    # A small block size also exercises multi-block framing.
    for offset in range(0, len(data), 1024):
        block = data[offset:offset + 1024]
        encoded = lz4.block.compress(block, store_size=False) if compressed else block
        token = 0x20 if compressed else 0x10
        checksum = xxhash.xxh32(block, seed=0x9747B28C).intdigest() & 0x0FFFFFFF
        blocks.append(b"LZ4Block" + bytes([token]) + struct.pack("<III", len(encoded), len(block), checksum) + encoded)
    return b"".join(blocks) + b"LZ4Block\x10" + bytes(12)


def region(path: Path, values=(0,), *, method=2, external=False, old=False, slots=None,
           raw_nbt=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytearray(8192)
    records = []
    sector = 2
    rx, rz = map(int, path.name.split(".")[1:3])
    for index, value in zip(slots if slots is not None else range(len(values)), values):
        cx, cz = rx * 32 + index % 32, rz * 32 + index // 32
        raw = raw_nbt if raw_nbt is not None else chunk_nbt(value, old=old, coords=(cx, cz))
        encoded = {1: gzip.compress, 2: zlib.compress, 3: lambda x: x, 4: lz4_stream}[method](raw)
        if external:
            (path.parent / f"c.{cx}.{cz}.mcc").write_bytes(encoded)
            record = struct.pack(">IB", 1, method | 128)
        else:
            record = struct.pack(">IB", len(encoded) + 1, method) + encoded
        count = (len(record) + 4095) // 4096
        header[index * 4:index * 4 + 4] = ((sector << 8) | count).to_bytes(4, "big")
        records.append(record.ljust(count * 4096, b"\0"))
        sector += count
    path.write_bytes(header + b"".join(records))
    return path
