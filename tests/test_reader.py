import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from chunk_cleaner.region_reader import (Cancelled, NBTReader, UnsafeData, decompress,
                                         inspect_region, read_lz4)
from tests.fixtures import chunk_nbt, lz4_stream, region, tag


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "r.-1.2.mca"

    def test_all_compressions_old_new_and_external(self):
        for method in (1, 2, 3, 4):
            for old in (False, True):
                for external in (False, True):
                    with self.subTest(method=method, old=old, external=external):
                        region(self.path, [0, 12, 599], method=method, old=old, external=external,
                               slots=[0, 31, 1023])
                        result = inspect_region(self.path)
                        self.assertEqual((result.count, result.maximum), (3, 599))

    def test_lz4_raw_compressed_multiblock_and_checksum(self):
        data = b"some representative repeated bytes " * 500
        for compressed in (False, True):
            stream = lz4_stream(data, compressed)
            self.assertEqual(read_lz4(stream), data)
            damaged = bytearray(stream)
            damaged[17] ^= 1
            with self.assertRaises(UnsafeData):
                read_lz4(bytes(damaged))
            with self.assertRaises(UnsafeData):
                read_lz4(stream[:-1])
            with self.assertRaises(UnsafeData):
                read_lz4(stream + b"trailing")

    def test_nbt_field_validation(self):
        bad = [chunk_nbt(missing=True), chunk_nbt(-1), chunk_nbt(0, kind=3),
               chunk_nbt(extra=tag(4, "InhabitedTime", struct.pack(">q", 1))),
               chunk_nbt(extra=tag(10, "Level", tag(4, "InhabitedTime", bytes(8)) + b"\0")),
               chunk_nbt()[:-1], chunk_nbt() + b"x", b"\x01\0\0\x01"]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(UnsafeData):
                    NBTReader(raw).inhabited_time()

    def test_coordinate_mismatch(self):
        region(self.path, raw_nbt=chunk_nbt(coords=(0, 0)))
        with self.assertRaises(UnsafeData):
            inspect_region(self.path)

    def test_nested_tags_and_lists_do_not_shadow_metadata(self):
        nested = tag(10, "Other", tag(4, "InhabitedTime", struct.pack(">q", 999)) + b"\0")
        nested += tag(9, "Items", b"\x0a" + struct.pack(">i", 1) +
                      tag(4, "InhabitedTime", struct.pack(">q", 888)) + b"\0")
        self.assertEqual(NBTReader(chunk_nbt(7, extra=nested)).inhabited_time(), 7)

    def test_empty_region(self):
        region(self.path, [])
        result = inspect_region(self.path)
        self.assertEqual((result.count, result.maximum), (0, None))

    def test_bad_offset_overlap_and_length(self):
        for damage in ("offset", "overlap", "length", "alignment", "compression"):
            region(self.path, [1, 2])
            data = bytearray(self.path.read_bytes())
            if damage == "offset":
                data[:4] = b"\xff\xff\xff\x01"
            elif damage == "overlap":
                data[4:8] = data[:4]
            elif damage == "length":
                data[8192:8196] = struct.pack(">I", 9000)
            elif damage == "compression":
                data[8196] = 99
            else:
                data.pop()
            self.path.write_bytes(data)
            with self.subTest(damage=damage):
                with self.assertRaises(UnsafeData):
                    inspect_region(self.path)

    def test_decompression_limit_and_truncated_stream(self):
        import zlib
        with patch("chunk_cleaner.region_reader.MAX_BYTES", 100):
            with self.assertRaises(UnsafeData):
                decompress(zlib.compress(b"x" * 1000), 2)
        with self.assertRaises(UnsafeData):
            decompress(zlib.compress(b"x")[:-1], 2)

    def test_external_missing(self):
        region(self.path, external=True)
        next(self.path.parent.glob("*.mcc")).unlink()
        with self.assertRaises(OSError):
            inspect_region(self.path)

    def test_cancel(self):
        region(self.path)
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            inspect_region(self.path, event)


if __name__ == "__main__":
    unittest.main()
