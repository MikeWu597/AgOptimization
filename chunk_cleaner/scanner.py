"""Candidate discovery and immutable snapshots, with no writes to worlds."""

from dataclasses import asdict, dataclass, field
import hashlib
import os
from pathlib import Path
import stat

from .region_reader import (EXTERNAL_NAME, Cancelled, UnsafeData, check_cancel,
                            coordinates, inspect_region)

WORLDS = ("world", "world_nether", "world_the_end")


@dataclass(frozen=True)
class Settings:
    root: Path
    threshold: int
    protected_radius: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root).resolve(strict=True))
        if not self.root.is_dir():
            raise ValueError("根目录不是文件夹")
        if type(self.threshold) is not int or self.threshold < 0:
            raise ValueError("计时阈值必须是非负整数")
        if self.protected_radius is not None and (
                type(self.protected_radius) is not int or self.protected_radius < 0):
            raise ValueError("豁免阈值 R 必须是非负整数")

    def protected(self, x, z):
        return (self.protected_radius is not None and
                abs(x) < self.protected_radius and abs(z) < self.protected_radius)

    def to_dict(self):
        return {"root": str(self.root), "threshold_ticks": self.threshold,
                "protected_radius": self.protected_radius}


def is_link(path):
    info = path.lstat()
    return (stat.S_ISLNK(info.st_mode) or
            bool(getattr(info, "st_file_attributes", 0) & 0x400))  # Windows reparse point


def safe_path(path: Path, root: Path):
    """Reject any symlink/reparse-point component, including an existing leaf."""
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise UnsafeData("路径超出选定目录") from exc
    current = root
    if is_link(root):
        raise UnsafeData("根目录被替换为链接")
    for part in parts:
        if part in ("..", "."):
            raise UnsafeData("非规范路径")
        current /= part
        if is_link(current):
            raise UnsafeData(f"不跟随链接/重解析点：{current}")


@dataclass(frozen=True)
class FileStamp:
    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    sha256: str


def fingerprint(path: Path, root: Path, cancel=None):
    safe_path(path, root)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise UnsafeData(f"非普通文件或有硬链接：{path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if signature(before) != signature(opened):
            raise UnsafeData("读取前文件已变化")
        while True:
            check_cancel(cancel)
            data = stream.read(1024 * 1024)
            if not data:
                break
            digest.update(data)
        after = os.fstat(stream.fileno())
    safe_path(path, root)
    if signature(before) != signature(after) or signature(after) != signature(path.stat()):
        raise UnsafeData("读取期间文件已变化")
    return FileStamp(str(path), before.st_size, before.st_mtime_ns, before.st_ctime_ns,
                     before.st_dev, before.st_ino, digest.hexdigest())


def signature(info):
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino)


class ExternalIndex:
    """Avoid rescanning huge directory listings for every region during a scan.

    Only directory membership is cached. Contents are always fingerprinted, and
    deletion uses fresh, uncached directory listings.
    """

    def __init__(self):
        self.cache = {}

    def get(self, folder: Path, cancel=None):
        before = signature(folder.stat())
        cached = self.cache.get(folder)
        if cached is not None and cached[0] == before:
            return cached[1]
        groups = {}
        with os.scandir(folder) as entries:
            for entry in entries:
                check_cancel(cancel)
                if not entry.name.lower().endswith(".mcc"):
                    continue
                match = EXTERNAL_NAME.fullmatch(entry.name)
                if not match:
                    raise UnsafeData(f"存在无法识别的外置区块文件：{entry.name}")
                cx, cz = map(int, match.groups())
                if entry.name != f"c.{cx}.{cz}.mcc":
                    raise UnsafeData("外置区块文件名不是规范坐标格式")
                groups.setdefault((cx // 32, cz // 32), []).append(folder / entry.name)
        if signature(folder.stat()) != before:
            raise UnsafeData("目录在扫描期间变化，请停服后重扫")
        self.cache[folder] = (before, groups)
        return groups


def group_files(region: Path, root: Path, inventory=None, cancel=None):
    """Include matching MCA and external chunk files from all three stores."""
    safe_path(region, root)
    relative = region.relative_to(root)
    if relative.parts[0] not in WORLDS or region.parent.name != "region":
        raise UnsafeData("不是指定世界中的地形区域文件")
    rx, rz = coordinates(region)
    files = [region]
    inventory = inventory if inventory is not None else ExternalIndex()
    for store in ("region", "entities", "poi"):
        folder = region.parent.parent / store
        if not os.path.lexists(folder):
            continue
        safe_path(folder, root)
        if not folder.is_dir():
            raise UnsafeData(f"存储路径不是目录：{folder}")
        sibling = folder / region.name
        if store != "region" and os.path.lexists(sibling):
            files.append(sibling)
        files.extend(inventory.get(folder, cancel).get((rx, rz), ()))
    for path in files:
        safe_path(path, root)
        if not path.is_file():
            raise UnsafeData(f"清理项不是普通文件：{path}")
    return tuple(sorted(set(files), key=str))


def snapshot(region: Path, root: Path, cancel=None, inventory=None):
    paths = group_files(region, root, inventory, cancel)
    stamps = tuple(fingerprint(path, root, cancel) for path in paths)
    if group_files(region, root, inventory, cancel) != paths:
        raise UnsafeData("关联文件列表在读取期间变化")
    return stamps


@dataclass
class ScanRow:
    path: str
    status: str
    reason: str = ""
    count: int = 0
    maximum: int | None = None
    files: tuple[FileStamp, ...] = field(default_factory=tuple)
    size: int = 0

    @property
    def eligible(self):
        return self.status == "候选"

    def to_dict(self):
        return asdict(self)


def scan_one(path: Path, settings: Settings, cancel=None, inventory=None):
    row = ScanRow(str(path), "保留")
    try:
        safe_path(path, settings.root)
        x, z = coordinates(path)
        if settings.protected(x, z):
            row.status, row.reason = "坐标豁免", "|x| < R 且 |z| < R；关联文件一并保留"
            return row
        before = snapshot(path, settings.root, cancel, inventory)
        stats = inspect_region(path, cancel)
        row.count, row.maximum = stats.count, stats.maximum
        row.size = sum(item.size for item in before)
        if stats.count == 0:
            row.reason = "空区域文件，未加入候选"
        elif stats.maximum >= settings.threshold:
            row.reason = "存在 InhabitedTime 大于或等于阈值的区块"
        else:
            if snapshot(path, settings.root, cancel, inventory) != before:
                raise UnsafeData("扫描期间文件变化，请停服后重扫")
            row.files, row.status = before, "候选"
            row.reason = "所有区块计时严格小于阈值"
    except Cancelled:
        raise
    except (OSError, ValueError, OverflowError, RecursionError) as exc:
        row.status, row.reason = "错误保留", str(exc)
        row.files = ()
    return row


def discover(settings: Settings, cancel=None):
    paths, notes = [], []
    for name in WORLDS:
        world = settings.root / name
        if not os.path.lexists(world):
            notes.append(f"未找到 {name}，已跳过")
            continue
        try:
            safe_path(world, settings.root)
            if not world.is_dir():
                raise UnsafeData("世界路径不是文件夹")
        except (OSError, ValueError) as exc:
            notes.append(f"{world}：{exc}")
            continue

        def walk_error(exc):
            notes.append(str(exc))

        for current, dirs, files in os.walk(world, followlinks=False, onerror=walk_error):
            check_cancel(cancel)
            folder = Path(current)
            try:
                safe_path(folder, settings.root)
                clean_dirs = []
                for directory in dirs:
                    child = folder / directory
                    if is_link(child):
                        notes.append(f"跳过链接目录：{child}")
                    elif directory not in ("entities", "poi"):
                        clean_dirs.append(directory)
                dirs[:] = sorted(clean_dirs)
                if folder.name == "region":
                    dirs[:] = []
                    paths.extend(folder / name for name in sorted(files)
                                 if name.lower().endswith(".mca"))
            except (OSError, ValueError) as exc:
                notes.append(f"{folder}：{exc}")
                dirs[:] = []
    return paths, notes


def scan(settings: Settings, cancel=None, progress=None):
    paths, notes = discover(settings, cancel)
    rows = []
    inventory = ExternalIndex()
    for index, path in enumerate(paths):
        check_cancel(cancel)
        row = scan_one(path, settings, cancel, inventory)
        rows.append(row)
        if progress:
            progress(index + 1, len(paths), row)
    check_cancel(cancel)
    return rows, notes
