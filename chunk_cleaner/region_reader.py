"""Bounded, read-only Anvil/NBT reader. Any ambiguity fails closed."""

from dataclasses import dataclass
from pathlib import Path
import re
import struct
import zlib

MAX_BYTES = 64 * 1024 * 1024
REGION_NAME = re.compile(r"r\.(-?\d+)\.(-?\d+)\.mca\Z")
EXTERNAL_NAME = re.compile(r"c\.(-?\d+)\.(-?\d+)\.mcc\Z")


class UnsafeData(ValueError):
    pass


class Cancelled(Exception):
    pass


def check_cancel(cancel=None):
    if cancel is not None and cancel.is_set():
        raise Cancelled("已取消")


def coordinates(path: Path) -> tuple[int, int]:
    match = REGION_NAME.fullmatch(path.name)
    if not match:
        raise UnsafeData("区域文件名无效")
    coords = tuple(map(int, match.groups()))
    if path.name != f"r.{coords[0]}.{coords[1]}.mca":
        raise UnsafeData("区域文件名不是规范坐标格式")
    return coords


def read_lz4(data: bytes) -> bytes:
    # Minecraft uses lz4-java's block stream, NOT the standard LZ4 frame.
    try:
        import lz4.block
        import xxhash
    except ImportError as exc:
        raise UnsafeData("缺少 lz4/xxhash 依赖，无法验证 LZ4 区块") from exc
    out = bytearray()
    pos = 0
    while True:
        if pos + 21 > len(data) or data[pos:pos + 8] != b"LZ4Block":
            raise UnsafeData("LZ4 block 头损坏或缺失结束标记")
        token = data[pos + 8]
        method = token & 0xF0
        size, original, checksum = struct.unpack_from("<III", data, pos + 9)
        pos += 21
        if method not in (0x10, 0x20) or original > (1 << (10 + (token & 15))):
            raise UnsafeData("无效的 LZ4 block 参数")
        if original == 0 or size == 0:
            if original or size or checksum or pos != len(data):
                raise UnsafeData("无效的 LZ4 结束标记")
            return bytes(out)
        if pos + size > len(data) or len(out) + original > MAX_BYTES:
            raise UnsafeData("LZ4 长度无效或解压超限")
        block = data[pos:pos + size]
        pos += size
        if method == 0x10:
            if size != original:
                raise UnsafeData("LZ4 raw 长度不匹配")
            decoded = block
        else:
            try:
                decoded = lz4.block.decompress(block, uncompressed_size=original)
            except Exception as exc:
                raise UnsafeData("LZ4 解压失败") from exc
        if len(decoded) != original:
            raise UnsafeData("LZ4 解压长度不匹配")
        # StreamingXXHash32.asChecksum() in lz4-java masks to 28 bits.
        if (xxhash.xxh32(decoded, seed=0x9747B28C).intdigest() & 0x0FFFFFFF) != checksum:
            raise UnsafeData("LZ4 校验和错误")
        out.extend(decoded)


def decompress(data: bytes, method: int) -> bytes:
    if len(data) > MAX_BYTES:
        raise UnsafeData("压缩数据超过 64 MiB 限制")
    if method in (1, 2):
        try:
            decoder = zlib.decompressobj(31 if method == 1 else 15)
            result = decoder.decompress(data, MAX_BYTES + 1)
            if len(result) > MAX_BYTES or not decoder.eof or decoder.unused_data:
                raise UnsafeData("压缩数据不完整、有多余数据或解压超过 64 MiB")
            return result
        except zlib.error as exc:
            raise UnsafeData("gzip/zlib 解压失败") from exc
    if method == 3:
        return data
    if method == 4:
        return read_lz4(data)
    raise UnsafeData(f"不支持的压缩类型：{method}")


class NBTReader:
    """Validate the whole tree while retaining only the relevant scalar tags."""

    def __init__(self, data: bytes, cancel=None):
        self.data = data
        self.pos = 0
        self.nodes = 0
        self.fields = {}
        self.cancel = cancel

    def take(self, count):
        if count < 0 or self.pos + count > len(self.data):
            raise UnsafeData("NBT 数据截断或长度无效")
        start = self.pos
        self.pos += count
        return self.data[start:self.pos]

    def number(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def string(self):
        # Names of relevant tags are ASCII; other modified-UTF8 names are opaque.
        return self.take(self.number(">H"))

    def payload(self, tag, path, depth):
        self.nodes += 1
        if depth > 64 or self.nodes > 2_000_000:
            raise UnsafeData("NBT 嵌套或节点数量超过限制")
        if self.nodes % 1024 == 0:
            check_cancel(self.cancel)
        relevant = path in ((b"InhabitedTime",), (b"Level", b"InhabitedTime"),
                            (b"xPos",), (b"zPos",), (b"Level", b"xPos"),
                            (b"Level", b"zPos"))
        if relevant:
            if path in self.fields:
                raise UnsafeData("重复的区块元数据字段")
            if tag not in (3, 4):
                raise UnsafeData("区块元数据字段类型错误")
        formats = {1: ">b", 2: ">h", 3: ">i", 4: ">q", 5: ">f", 6: ">d"}
        if tag in formats:
            value = self.number(formats[tag])
            if relevant:
                self.fields[path] = (tag, value)
        elif tag in (7, 11, 12):
            count = self.number(">i")
            if count < 0:
                raise UnsafeData("NBT 数组长度为负")
            self.take(count * {7: 1, 11: 4, 12: 8}[tag])
        elif tag == 8:
            self.string()
        elif tag == 9:
            child = self.number(">B")
            count = self.number(">i")
            if count < 0 or count > 2_000_000 or child > 12 or (child == 0 and count):
                raise UnsafeData("NBT 列表参数错误")
            for _ in range(count):
                self.payload(child, None, depth + 1)
        elif tag == 10:
            seen = set()
            while True:
                child = self.number(">B")
                if child == 0:
                    break
                name = self.string()
                if name in seen:
                    raise UnsafeData("NBT compound 含重复字段")
                seen.add(name)
                # Only root and Level paths can contain metadata we use.
                next_path = path + (name,) if path in ((), (b"Level",)) else None
                self.payload(child, next_path, depth + 1)
        else:
            raise UnsafeData(f"未知 NBT tag：{tag}")

    def inhabited_time(self, expected=None):
        if self.number(">B") != 10:
            raise UnsafeData("NBT 根节点不是 compound")
        self.string()
        self.payload(10, (), 0)
        if self.pos != len(self.data):
            raise UnsafeData("NBT 结束后仍有多余数据")
        tags = [self.fields[p] for p in ((b"InhabitedTime",), (b"Level", b"InhabitedTime"))
                if p in self.fields]
        if len(tags) != 1 or tags[0][0] != 4 or tags[0][1] < 0:
            raise UnsafeData("InhabitedTime 缺失、重复、非 long 或为负")
        if expected is not None:
            for prefix in ((), (b"Level",)):
                for name, value in zip((b"xPos", b"zPos"), expected):
                    actual = self.fields.get(prefix + (name,))
                    if actual is not None and actual != (3, value):
                        raise UnsafeData("区块坐标与区域位置表不一致")
        return tags[0][1]


@dataclass(frozen=True)
class RegionStats:
    count: int
    maximum: int | None


def inspect_region(path: Path, cancel=None) -> RegionStats:
    rx, rz = coordinates(path)
    size = path.stat().st_size
    if size < 8192 or size % 4096:
        raise UnsafeData("MCA 文件长度无效（需完整头部及 4096 字节对齐）")
    count, maximum = 0, None
    occupied = {0, 1}
    with path.open("rb") as handle:
        header = handle.read(8192)
        entries = []
        for index in range(1024):
            location = int.from_bytes(header[index * 4:index * 4 + 4], "big")
            offset, sectors = location >> 8, location & 255
            if not offset and not sectors:
                continue
            if offset < 2 or sectors == 0 or (offset + sectors) * 4096 > size:
                raise UnsafeData("MCA 区块偏移/长度越界")
            allocated = set(range(offset, offset + sectors))
            if occupied.intersection(allocated):
                raise UnsafeData("MCA 区块扇区重叠")
            occupied.update(allocated)
            entries.append((index, offset, sectors))
        for index, offset, sectors in entries:
            check_cancel(cancel)
            handle.seek(offset * 4096)
            raw = handle.read(5)
            if len(raw) != 5:
                raise UnsafeData("MCA 记录截断")
            length, compression = struct.unpack(">IB", raw)
            if length < 1 or length + 4 > sectors * 4096:
                raise UnsafeData("MCA 压缩记录长度无效")
            cx, cz = rx * 32 + index % 32, rz * 32 + index // 32
            if compression & 128:
                if length != 1:
                    raise UnsafeData("外置区块记录长度无效")
                external = path.parent / f"c.{cx}.{cz}.mcc"
                if external.is_symlink() or getattr(external, "is_junction", lambda: False)():
                    raise UnsafeData("外置区块是链接")
                if external.stat().st_size > MAX_BYTES:
                    raise UnsafeData("外置区块超过 64 MiB")
                with external.open("rb") as stream:
                    data = stream.read(MAX_BYTES + 1)
            else:
                data = handle.read(length - 1)
                if len(data) != length - 1:
                    raise UnsafeData("压缩记录截断")
            value = NBTReader(decompress(data, compression & 127), cancel).inhabited_time((cx, cz))
            maximum = value if maximum is None else max(maximum, value)
            count += 1
    return RegionStats(count, maximum)
