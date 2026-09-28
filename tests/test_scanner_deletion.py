import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from chunk_cleaner.deletion import AuditFailure, delete_candidates
from chunk_cleaner.region_reader import Cancelled, UnsafeData
from chunk_cleaner.scanner import ExternalIndex, Settings, group_files, scan, scan_one
from tests.fixtures import chunk_nbt, region


class WorldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "服务器 with spaces"
        self.root.mkdir()
        self.logs = Path(self.temp.name) / "logs"
        self.path = self.root / "world/region/r.0.0.mca"
        self.settings = Settings(self.root, 600)

    def candidate(self):
        row = scan_one(self.path, self.settings)
        self.assertTrue(row.eligible, row.reason)
        return row

    def execute(self, rows, **kwargs):
        return delete_candidates(self.settings, rows, confirmed=True, log_directory=self.logs, **kwargs)

    def test_strict_thresholds_and_empty(self):
        for values, n, expected in [([0, 599], 600, True), ([0, 600], 600, False),
                                     ([0, 601], 600, False), ([0], 0, False), ([], 600, False)]:
            region(self.path, values)
            row = scan_one(self.path, Settings(self.root, n))
            self.assertEqual(row.eligible, expected)
            self.assertTrue(self.path.exists())

    def test_protection_all_axes_and_boundaries(self):
        settings = Settings(self.root, 600, 3)
        for x, z, protected in [(-2, 1, True), (2, -2, True), (0, 0, True),
                                 (3, 0, False), (-3, 0, False), (0, 3, False), (10, 1, False)]:
            path = self.path.with_name(f"r.{x}.{z}.mca")
            region(path)
            row = scan_one(path, settings)
            self.assertEqual(row.status == "坐标豁免", protected)
        region(self.path)
        self.assertTrue(scan_one(self.path, Settings(self.root, 600, 0)).eligible)

    def test_all_worlds_and_nested_dimensions(self):
        paths = [self.path, self.root / "world_nether/DIM-1/region/r.0.0.mca",
                 self.root / "world_the_end/DIM1/region/r.0.0.mca",
                 self.root / "world/dimensions/example/custom/region/r.0.0.mca"]
        for path in paths:
            region(path)
        # An entity store is not scanned independently.
        region(self.root / "world/entities/r.1.1.mca")
        rows, notes = scan(self.settings)
        self.assertEqual({row.path for row in rows}, set(map(str, paths)))
        self.assertFalse(notes)
        protected, _ = scan(Settings(self.root, 600, 1))
        self.assertTrue(all(row.status == "坐标豁免" for row in protected))

    def test_errors_keep_entire_file(self):
        for raw in (chunk_nbt(missing=True), chunk_nbt(-1), chunk_nbt(kind=3)):
            region(self.path, raw_nbt=raw)
            self.assertEqual(scan_one(self.path, self.settings).status, "错误保留")
        invalid = self.path.with_name("r.bad.name.mca")
        invalid.write_bytes(self.path.read_bytes())
        self.assertEqual(scan_one(invalid, self.settings).status, "错误保留")

    def test_confirm_required(self):
        region(self.path)
        row = self.candidate()
        with self.assertRaises(PermissionError):
            delete_candidates(self.settings, [row], log_directory=self.logs)
        self.assertTrue(self.path.exists())
        self.assertFalse(self.logs.exists())

    def test_group_deletion_and_audit(self):
        region(self.path, external=True)
        region(self.root / "world/entities/r.0.0.mca", external=True)
        region(self.root / "world/poi/r.0.0.mca", external=True)
        other = region(self.root / "world/entities/r.1.0.mca", external=True)
        row = self.candidate()
        self.assertEqual(len(row.files), 6)
        result = self.execute([row])
        self.assertEqual(result["deleted_bytes"], row.size)
        self.assertTrue(all(not Path(item.path).exists() for item in row.files))
        self.assertTrue(other.exists())
        events = [json.loads(line) for line in Path(result["log"]).read_text().splitlines()]
        self.assertEqual(events[0]["event"], "plan")
        self.assertEqual(sum(e["event"] == "deleted" for e in events), 6)

    def test_only_selected_regions_deleted(self):
        region(self.path)
        other = region(self.path.with_name("r.1.0.mca"))
        rows, _ = scan(self.settings)
        self.execute([next(row for row in rows if row.path == str(self.path))])
        self.assertFalse(self.path.exists())
        self.assertTrue(other.exists())

    def test_content_changed_same_size_and_mtime(self):
        region(self.path)
        row = self.candidate()
        previous = self.path.stat()
        data = bytearray(self.path.read_bytes())
        data[4096] ^= 1  # timestamp header; NBT remains valid
        self.path.write_bytes(data)
        os.utime(self.path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        result = self.execute([row])
        self.assertEqual(result["deleted_bytes"], 0)
        self.assertEqual(result["results"][0]["status"], "跳过")
        self.assertTrue(self.path.exists())

    def test_associated_file_added_removed_changed(self):
        for action in ("added", "removed", "changed"):
            with self.subTest(action=action):
                region(self.path)
                side = self.root / "world/entities/r.0.0.mca"
                if action != "added":
                    region(side)
                elif side.exists():
                    side.unlink()
                row = self.candidate()
                if action == "added":
                    region(side)
                elif action == "removed":
                    side.unlink()
                else:
                    side.write_bytes(b"changed")
                self.assertEqual(self.execute([row])["deleted_bytes"], 0)
                self.assertTrue(self.path.exists())

    def test_new_protection_blocks_stale_candidates(self):
        region(self.path)
        row = self.candidate()
        with self.assertRaises(UnsafeData):
            delete_candidates(Settings(self.root, 600, 1), [row], confirmed=True, log_directory=self.logs)
        self.assertTrue(self.path.exists())

    def test_new_time_threshold_rechecked(self):
        region(self.path, [100])
        row = self.candidate()
        result = delete_candidates(Settings(self.root, 100), [row], confirmed=True, log_directory=self.logs)
        self.assertEqual(result["deleted_bytes"], 0)

    def test_cancel_scan_and_delete(self):
        region(self.path)
        row = self.candidate()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            scan(self.settings, cancel)
        self.assertEqual(self.execute([row], cancel=cancel)["deleted_bytes"], 0)
        self.assertTrue(self.path.exists())

    def test_terrain_unlink_failure_keeps_sidecars(self):
        region(self.path)
        side = region(self.root / "world/entities/r.0.0.mca")
        row = self.candidate()
        with patch.object(Path, "unlink", side_effect=PermissionError("文件被占用")):
            result = self.execute([row])
        self.assertEqual(result["deleted_bytes"], 0)
        self.assertTrue(side.exists())
        self.assertTrue(self.path.exists())

    def test_partial_failure_is_reported(self):
        region(self.path)
        side = region(self.root / "world/entities/r.0.0.mca")
        row = self.candidate()
        original = Path.unlink

        def fail_side(path, *args, **kwargs):
            if path == side:
                raise PermissionError("模拟占用")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_side):
            result = self.execute([row])
        self.assertEqual(result["results"][0]["status"], "部分删除")
        self.assertFalse(self.path.exists())
        self.assertTrue(side.exists())
        self.assertEqual(result["deleted_bytes"], row.size - side.stat().st_size)

    def test_symlink_directory_and_file_not_followed(self):
        outside = region(Path(self.temp.name) / "outside/region/r.0.0.mca")
        try:
            (self.root / "world").symlink_to(outside.parent.parent, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"系统未授权创建符号链接：{exc}")
        rows, notes = scan(self.settings)
        self.assertFalse(rows)
        self.assertTrue(notes)
        (self.root / "world").unlink()
        self.path.parent.mkdir(parents=True)
        self.path.symlink_to(outside)
        self.assertEqual(scan_one(self.path, self.settings).status, "错误保留")
        self.assertTrue(outside.exists())

    def test_logs_inside_world_rejected(self):
        region(self.path)
        with self.assertRaises(UnsafeData):
            delete_candidates(self.settings, [self.candidate()], confirmed=True,
                              log_directory=self.root / "logs")
        self.assertTrue(self.path.exists())

    def test_no_delete_when_audit_fails(self):
        region(self.path)
        with patch("chunk_cleaner.deletion.os.fsync", side_effect=OSError("磁盘已满")):
            with self.assertRaises(AuditFailure):
                self.execute([self.candidate()])
        self.assertTrue(self.path.exists())

    def test_audit_failure_mid_delete_stops_batch(self):
        region(self.path)
        side = region(self.root / "world/entities/r.0.0.mca")
        other = region(self.path.with_name("r.1.0.mca"))
        rows = [self.candidate(), scan_one(other, self.settings)]
        original = os.fsync
        calls = 0

        def fail_after_unlink(fd):
            nonlocal calls
            calls += 1
            if calls == 3:  # plan, attempt, deleted
                raise OSError("模拟日志落盘失败")
            return original(fd)

        with patch("chunk_cleaner.deletion.os.fsync", fail_after_unlink):
            with self.assertRaises(AuditFailure):
                self.execute(rows)
        self.assertFalse(self.path.exists())
        self.assertTrue(side.exists())
        self.assertTrue(other.exists())

    def test_external_index_refresh_and_negative_coordinates(self):
        path = region(self.path.with_name("r.-1.-1.mca"), external=True)
        unrelated = region(self.path.with_name("r.0.-1.mca"), external=True)
        inventory = ExternalIndex()
        first = group_files(path, self.root, inventory)
        self.assertEqual(len(first), 2)
        orphan = path.parent / "c.-1.-1.mcc"
        orphan.write_bytes(b"same region leftover")
        updated = group_files(path, self.root, inventory)
        self.assertEqual(len(updated), 3)
        self.assertIn(orphan, updated)
        row = scan_one(path, self.settings, inventory=inventory)
        self.assertTrue(row.eligible, row.reason)
        self.execute([row])
        self.assertFalse(orphan.exists())
        self.assertTrue(unrelated.exists())

    def test_scan_file_changes_fail_closed(self):
        region(self.path)
        from chunk_cleaner.scanner import inspect_region as original

        def changed_during_scan(path, cancel):
            result = original(path, cancel)
            region(path, [599])
            return result

        with patch("chunk_cleaner.scanner.inspect_region", changed_during_scan):
            row = scan_one(self.path, self.settings)
        self.assertEqual(row.status, "错误保留")
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
