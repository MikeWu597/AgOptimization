"""Explicitly confirmed, audited permanent deletion of unchanged candidates."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

from .region_reader import Cancelled, UnsafeData, check_cancel, coordinates, inspect_region
from .scanner import fingerprint, safe_path, snapshot


class AuditFailure(RuntimeError):
    pass


def application_logs():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return base / "MinecraftRegionCleaner" / "logs"


def delete_candidates(settings, rows, *, confirmed=False, cancel=None, progress=None,
                      log_directory=None):
    if confirmed is not True:
        raise PermissionError("必须在确认窗口明确授权删除")
    rows = tuple(rows)
    if not rows or any(not row.eligible or not row.files for row in rows):
        raise ValueError("删除列表为空或包含非候选项")
    if len({row.path for row in rows}) != len(rows):
        raise ValueError("删除列表含重复区域")
    for row in rows:
        x, z = coordinates(Path(row.path))
        if settings.protected(x, z):
            raise UnsafeData("删除列表含坐标豁免区域，请重新扫描")

    logs = Path(log_directory) if log_directory is not None else application_logs()
    logs = logs.resolve()
    if logs.is_relative_to(settings.root):
        raise UnsafeData("日志目录必须位于扫描根目录之外")
    logs.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    log_path = logs / f"delete-{run_id}.jsonl"
    results = []
    deleted_bytes = 0
    with log_path.open("x", encoding="utf-8") as log:
        def write(event):
            try:
                log.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **event},
                                     ensure_ascii=False) + "\n")
                log.flush()
                os.fsync(log.fileno())
            except OSError as exc:
                raise AuditFailure(f"写入操作日志失败，停止后续删除：{log_path}；{exc}") from exc

        write({"event": "plan", "settings": settings.to_dict(),
               "regions": [row.to_dict() for row in rows]})
        for index, row in enumerate(rows):
            result = {"region": row.path, "status": "跳过", "reason": "", "files": []}
            try:
                check_cancel(cancel)
                path = Path(row.path)
                if snapshot(path, settings.root, cancel) != row.files:
                    raise UnsafeData("文件或关联清单已变化，请重新扫描")
                stats = inspect_region(path, cancel)
                if not stats.count or stats.maximum >= settings.threshold:
                    raise UnsafeData("删除前复核不满足计时条件")
                if snapshot(path, settings.root, cancel) != row.files:
                    raise UnsafeData("复核期间文件已变化")
                # Delete terrain first: if it is locked, leave all sidecars intact.
                ordered = sorted(row.files, key=lambda item: (item.path != row.path, item.path))
                for item in ordered:
                    check_cancel(cancel)
                    target = Path(item.path)
                    safe_path(target, settings.root)
                    if fingerprint(target, settings.root, cancel) != item:
                        raise UnsafeData(f"文件变化：{target}")
                    write({"event": "delete_attempt", "path": item.path, "size": item.size})
                    target.unlink()
                    deleted_bytes += item.size
                    result["files"].append({"path": item.path, "size": item.size})
                    write({"event": "deleted", "path": item.path, "size": item.size})
                result["status"] = "已删除"
            except Cancelled:
                result["status"] = "部分删除" if result["files"] else "已取消"
                result["reason"] = "用户取消；已删除文件不会恢复"
                results.append(result)
                write({"event": "region_result", **result})
                if progress:
                    progress(index + 1, len(rows), result)
                break
            except (OSError, ValueError) as exc:
                result["status"] = "部分删除" if result["files"] else "跳过"
                result["reason"] = str(exc)
            results.append(result)
            write({"event": "region_result", **result})
            if progress:
                progress(index + 1, len(rows), result)
        summary = {"results": results, "deleted_bytes": deleted_bytes,
                   "log": str(log_path), "unprocessed": len(rows) - len(results)}
        write({"event": "complete", **summary})
    return summary
