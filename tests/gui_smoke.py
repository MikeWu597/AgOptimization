"""Explicit GUI integration test. Opens Tk; deletes only synthetic temporary files."""

import functools
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch

from chunk_cleaner.deletion import delete_candidates
from chunk_cleaner.gui import CleanerApp, parse_threshold
from tests.fixtures import region


def children(widget):
    for child in widget.winfo_children():
        yield child
        yield from children(child)


def pump(root, app):
    deadline = time.monotonic() + 15
    while app.busy and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    root.update()
    assert not app.busy, "GUI worker timed out"


def main():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary).resolve()
        world_root = base / "服务器 with spaces"
        protected = region(world_root / "world/region/r.0.0.mca", [0])
        candidate = region(world_root / "world/region/r.3.-2.mca", [0, 599])
        retained = region(world_root / "world/region/r.4.0.mca", [600])
        root = tk.Tk()
        app = CleanerApp(root)
        try:
            app.folder.set(str(world_root))
            app.protection.set(True)
            app.radius.set("3")
            root.update()
            assert root.winfo_width() >= 900
            app.start_scan()
            pump(root, app)
            assert app.valid
            assert len(app.rows) == 3
            assert len(app.selected) == 1
            assert not app.delete_button.instate(["disabled"])
            assert app.metrics["total"].get() == "3"
            assert app.metrics["eligible"].get() == "1"
            assert app.metrics["protected"].get() == "1"
            assert app.radius_entry.instate(["!disabled"])
            app.search.set("does-not-exist")
            root.update()
            assert not app.visible
            assert "筛选外 1 个" in app.selection_text.get()
            assert app.empty.winfo_ismapped()
            app.search.set("r.3.-2")
            assert len(app.visible) == 1
            app.toggle(str(next(iter(app.visible))))
            assert not app.selected
            app.toggle(str(next(iter(app.visible))))
            assert len(app.selected) == 1
            app.search.set("")
            app.view_filter.set("豁免")
            assert len(app.visible) == 1
            assert all(app.rows[i].status == "坐标豁免" for i in app.visible)
            app.select_none()
            assert app.delete_button.instate(["disabled"])
            app.select_all()
            assert not app.selected, "Filtered selection must not select hidden candidates"
            app.view_filter.set("候选")
            app.select_all()
            assert len(app.selected) == 1
            app.view_filter.set("全部")
            app.tree.selection_set(str(next(iter(app.selected))))
            root.update()
            assert app.details_frame.winfo_ismapped()
            app.toggle_details()
            assert not app.details_frame.winfo_manager()

            for width, height in ((1280, 850), (1080, 720)):
                root.geometry(f"{app.px(width)}x{app.px(height)}")
                root.update()
                for widget in (app.delete_button, app.all_button, app.none_button, app.export_button, app.search_entry):
                    assert widget.winfo_ismapped(), str(widget)
                    assert widget.winfo_rootx() >= root.winfo_rootx(), str(widget)
                    assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width(), str(widget)
                    assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height(), str(widget)
                assert app.tree.winfo_height() > app.px(180)
                app.sidebar_canvas.yview_moveto(1)
                root.update()
                assert app.cancel_button.winfo_rooty() + app.cancel_button.winfo_height() <= app.sidebar_canvas.winfo_rooty() + app.sidebar_canvas.winfo_height()
            app.sidebar_canvas.yview_moveto(0)

            exported = base / "manifest.json"
            with patch("chunk_cleaner.gui.filedialog.asksaveasfilename", return_value=str(exported)):
                app.export()
            manifest = json.loads(exported.read_text(encoding="utf-8"))
            assert sum(row["selected"] for row in manifest["rows"]) == 1

            app.threshold.set("599")
            assert not app.valid and not app.selected
            assert app.delete_button.instate(["disabled"])
            app.threshold.set("600")
            app.view_filter.set("候选")  # Detached rows must be removed on a new scan.
            app.start_scan()
            pump(root, app)
            assert len(app.tree.get_children()) == 1
            app.view_filter.set("全部")
            assert len(app.tree.get_children()) == 3

            modal_errors = []

            def handle_confirmation(accept):
                try:
                    dialog = next(widget for widget in root.winfo_children() if isinstance(widget, tk.Toplevel))
                    widgets = list(children(dialog))
                    delete = next(widget for widget in widgets if isinstance(widget, ttk.Button)
                                  and widget.cget("text") == "永久删除以上文件")
                    assert delete.instate(["disabled"])
                    if not accept:
                        dialog.destroy()
                        return
                    checks = [widget for widget in widgets if isinstance(widget, ttk.Checkbutton)]
                    assert len(checks) == 2
                    dialog.update_idletasks()
                    assert delete.winfo_ismapped()
                    assert delete.winfo_rooty() + delete.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
                    checks[0].invoke()
                    assert delete.instate(["disabled"])
                    checks[1].invoke()
                    assert not delete.instate(["disabled"])
                    delete.invoke()
                except Exception as exc:
                    modal_errors.append(exc)
                    for widget in root.winfo_children():
                        if isinstance(widget, tk.Toplevel):
                            widget.destroy()

            root.after(100, lambda: handle_confirmation(False))
            app.confirm_delete()
            assert not modal_errors, modal_errors
            assert candidate.exists() and not app.busy
            root.after(100, lambda: handle_confirmation(True))
            with patch("chunk_cleaner.gui.delete_candidates", functools.partial(delete_candidates, log_directory=base / "logs")), \
                    patch("chunk_cleaner.gui.messagebox.showinfo"), \
                    patch("chunk_cleaner.gui.messagebox.showerror") as errors:
                app.confirm_delete()
                pump(root, app)
                assert not errors.called, errors.call_args
            assert not modal_errors, modal_errors
            assert not candidate.exists()
            assert protected.exists() and retained.exists()
            assert not app.valid
            assert app.delete_button.instate(["disabled"])
            assert parse_threshold("0.5", "秒") == 10
            print("GUI smoke PASS: metrics, filters, search, scoped selection, layout at 1280x850/1080x720, inspector, scan, protection, export, invalidation, confirmation, deletion")
        finally:
            app.close()


if __name__ == "__main__":
    main()
