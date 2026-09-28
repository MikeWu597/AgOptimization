"""Tk widgets are used exclusively on the main thread; workers post queue events."""

import csv
from decimal import Decimal, DecimalException
import json
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .deletion import delete_candidates
from .region_reader import Cancelled
from .scanner import Settings, scan
from .theme import COLORS, apply_theme


def format_size(size):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:,.1f} {unit}"
        size /= 1024


def parse_threshold(value, unit):
    try:
        number = Decimal(value)
        ticks = number * {"tick": 1, "秒": 20, "分钟": 1200}[unit]
        if not ticks.is_finite() or ticks < 0 or ticks != ticks.to_integral_value():
            raise ValueError
        if ticks > 9223372036854775807:
            raise ValueError
        return int(ticks)
    except (DecimalException, ValueError, KeyError):
        raise ValueError("计时阈值需换算为非负整数 tick，且不能超过 long 上限") from None


class CleanerApp:
    def __init__(self, root):
        self.root = root
        root.title("Minecraft 区域清理工具")
        self.fonts, self.px = apply_theme(root)
        screen_width = max(760, root.winfo_screenwidth() - self.px(40))
        screen_height = max(540, root.winfo_screenheight() - self.px(90))
        root.geometry(f"{min(self.px(1280), screen_width)}x{min(self.px(850), screen_height)}")
        root.minsize(min(self.px(1080), screen_width), min(self.px(680), screen_height))
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.valid = False
        self.rows = []
        self.selected = set()
        self.scan_settings = None
        self.visible = set()
        self.controls = []
        self.folder = tk.StringVar(value=str(Path.cwd()))
        self.threshold = tk.StringVar(value="600")
        self.unit = tk.StringVar(value="tick")
        self.protection = tk.BooleanVar(value=False)
        self.radius = tk.StringVar(value="3")
        self.status = tk.StringVar(value="选择服务器根目录，然后开始只读扫描。建议先停止服务器。")
        self.summary = tk.StringVar(value="尚未扫描")
        self.formula = tk.StringVar()
        self.phase = tk.StringVar(value="等待扫描")
        self.selection_text = tk.StringVar(value="尚未选择文件")
        self.time_hint = tk.StringVar()
        self.protection_hint = tk.StringVar()
        self.detail_title = tk.StringVar(value="文件详情 / 选择一行查看原因与关联文件")
        self.metrics = {name: tk.StringVar(value="0 B" if name == "size" else "0")
                        for name in ("total", "eligible", "protected", "size")}
        self.view_filter = tk.StringVar(value="全部")
        self.search = tk.StringVar()
        self.build()
        for variable in (self.folder, self.threshold, self.unit, self.protection, self.radius):
            variable.trace_add("write", self.invalidate)
        self.view_filter.trace_add("write", self.apply_filter)
        self.search.trace_add("write", self.apply_filter)
        self.invalidate()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = root.after(80, self.poll)

    def control(self, widget):
        self.controls.append(widget)
        return widget

    def build(self):
        c, px = COLORS, self.px
        header = ttk.Frame(self.root, padding=(px(26), px(16)))
        header.pack(fill="x")
        mark = tk.Canvas(header, width=px(40), height=px(40), background=c["surface"], highlightthickness=0)
        mark.pack(side="left", padx=(0, px(14)))
        for x, y, color in ((3, 3, "#276047"), (22, 3, "#83A586"), (3, 22, "#83A586"), (22, 22, "#C9DBBF")):
            mark.create_rectangle(px(x), px(y), px(x + 15), px(y + 15), fill=color, outline="")
        heading = ttk.Frame(header)
        heading.pack(side="left")
        ttk.Label(heading, text="区块清理", style="Title.TLabel").pack(anchor="w")
        ttk.Label(heading, text="Minecraft 存档管理", style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Label(header, textvariable=self.phase, style="Badge.TLabel").pack(side="right")
        ttk.Separator(self.root).pack(fill="x")

        # Reserve the action bar before allocating the expanding workspace.
        footer = ttk.Frame(self.root)
        footer.pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(footer, mode="determinate")
        self.progress.pack(fill="x")
        bottom = ttk.Frame(footer, padding=(px(26), px(15)))
        bottom.pack(fill="x")
        self.delete_button = ttk.Button(bottom, text="审阅删除清单", style="Danger.TButton",
                                        command=self.confirm_delete, state="disabled")
        self.delete_button.pack(side="right", padx=(px(24), 0))
        copy = ttk.Frame(bottom)
        copy.pack(side="left", fill="x", expand=True)
        ttk.Label(copy, textvariable=self.selection_text, style="Strong.TLabel").pack(anchor="w")
        self.status_label = ttk.Label(copy, textvariable=self.status, style="Muted.TLabel", wraplength=px(720))
        self.status_label.pack(anchor="w", pady=(px(4), 0))

        workspace = ttk.Frame(self.root, style="Canvas.TFrame")
        workspace.pack(fill="both", expand=True)
        sidebar = ttk.Frame(workspace, style="Sidebar.TFrame", width=px(300))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        ttk.Separator(workspace, orient="vertical").pack(side="left", fill="y")
        self.sidebar = sidebar
        self.sidebar_canvas = tk.Canvas(sidebar, background=c["sidebar"], highlightthickness=0, borderwidth=0)
        sidebar_scroll = ttk.Scrollbar(sidebar, orient="vertical", command=self.sidebar_canvas.yview)
        self.sidebar_canvas.configure(yscrollcommand=sidebar_scroll.set)
        sidebar_scroll.pack(side="right", fill="y")
        self.sidebar_canvas.pack(side="left", fill="both", expand=True)
        form = ttk.Frame(self.sidebar_canvas, style="Sidebar.TFrame", padding=(px(20), px(25)))
        form_window = self.sidebar_canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda _event: self.sidebar_canvas.configure(scrollregion=self.sidebar_canvas.bbox("all")))
        self.sidebar_canvas.bind("<Configure>", lambda event: self.sidebar_canvas.itemconfigure(form_window, width=event.width))
        self.root.bind("<MouseWheel>", self.scroll_sidebar, add=True)
        ttk.Label(form, text="01   选择存档", style="SidebarHeading.TLabel").pack(anchor="w")
        ttk.Label(form, text="服务器根目录", style="SidebarMuted.TLabel").pack(anchor="w", pady=(px(18), px(7)))
        self.folder_entry = self.control(ttk.Entry(form, textvariable=self.folder))
        self.folder_entry.pack(fill="x")
        actions = ttk.Frame(form, style="Sidebar.TFrame")
        actions.pack(fill="x", pady=(px(8), 0))
        self.control(ttk.Button(actions, text="浏览文件夹", command=self.choose_folder)).pack(side="left")
        self.control(ttk.Button(actions, text="程序目录", command=self.program_folder)).pack(side="right")
        ttk.Label(form, text="扫描目录中的主世界、下界与末地。", style="SidebarMuted.TLabel",
                  wraplength=px(252)).pack(anchor="w", pady=(px(10), px(21)))
        ttk.Separator(form).pack(fill="x")
        ttk.Label(form, text="02   清理规则", style="SidebarHeading.TLabel").pack(anchor="w", pady=(px(22), px(15)))
        ttk.Label(form, text="区块累计活动时间小于", style="Sidebar.TLabel").pack(anchor="w")
        duration = ttk.Frame(form, style="Sidebar.TFrame")
        duration.pack(fill="x", pady=(px(8), 0))
        self.control(ttk.Entry(duration, textvariable=self.threshold, width=13)).pack(side="left", fill="x", expand=True)
        self.unit_box = self.control(ttk.Combobox(duration, textvariable=self.unit, values=("tick", "秒", "分钟"),
                                                width=5, state="readonly"))
        self.unit_box.pack(side="left", padx=(px(8), 0))
        ttk.Label(form, textvariable=self.time_hint, style="SidebarMuted.TLabel", wraplength=px(252)
                  ).pack(anchor="w", pady=(px(7), px(21)))
        self.control(ttk.Checkbutton(form, text="保护中心区域", variable=self.protection,
                                    style="Sidebar.TCheckbutton")).pack(anchor="w")
        radius_row = ttk.Frame(form, style="Sidebar.TFrame")
        radius_row.pack(fill="x", pady=(px(8), 0))
        ttk.Label(radius_row, text="区域坐标范围 R", style="Sidebar.TLabel").pack(side="left")
        self.radius_entry = self.control(ttk.Entry(radius_row, textvariable=self.radius, width=6, justify="center"))
        self.radius_entry.pack(side="right")
        ttk.Label(form, textvariable=self.protection_hint, style="SidebarMuted.TLabel", wraplength=px(252)
                  ).pack(anchor="w", pady=(px(8), px(22)))
        self.scan_button = self.control(ttk.Button(form, text="开始扫描", style="Primary.TButton", command=self.start_scan))
        self.scan_button.pack(fill="x")
        self.cancel_button = ttk.Button(form, text="取消当前操作", command=self.request_cancel, state="disabled")
        self.cancel_button.pack(fill="x", pady=(px(8), 0))
        note = ttk.Frame(form, style="Soft.TFrame", padding=px(14))
        note.pack(fill="x", pady=(px(24), 0))
        ttk.Label(note, text="删除前请停服并备份", style="Soft.TLabel", font=self.fonts["strong"]).pack(anchor="w")
        ttk.Label(note, text="低计时也可能包含建筑。\n扫描只读，删除需要再次确认。", style="Soft.TLabel",
                  wraplength=px(230)).pack(anchor="w", pady=(px(6), 0))

        main = ttk.Frame(workspace, style="Canvas.TFrame", padding=px(22))
        main.pack(side="left", fill="both", expand=True)
        metrics = ttk.Frame(main, style="Canvas.TFrame")
        metrics.pack(fill="x", pady=(0, px(18)))
        for column, key, caption in ((0, "total", "扫描区域"), (1, "eligible", "可清理区域"),
                                     (2, "protected", "坐标豁免"), (3, "size", "预计释放")):
            metrics.columnconfigure(column, weight=1, uniform="metrics")
            border = tk.Frame(metrics, background=c["surface"], highlightbackground=c["line"], highlightthickness=1)
            border.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else px(10), 0))
            card = ttk.Frame(border, padding=(px(16), px(14)))
            card.pack(fill="both", expand=True)
            ttk.Label(card, text=caption, style="Muted.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=self.metrics[key],
                      style="GreenMetric.TLabel" if key == "size" else "Metric.TLabel"
                      ).pack(anchor="w", pady=(px(7), 0))

        border = tk.Frame(main, background=c["surface"], highlightbackground=c["line"], highlightthickness=1)
        border.pack(fill="both", expand=True)
        results = ttk.Frame(border)
        results.pack(fill="both", expand=True)
        top = ttk.Frame(results, padding=(px(18), px(15)))
        top.pack(fill="x")
        ttk.Label(top, text="扫描结果", style="Section.TLabel").pack(side="left")
        self.export_button = ttk.Button(top, text="导出清单", command=self.export, style="Quiet.TButton", state="disabled")
        self.export_button.pack(side="right")
        self.none_button = ttk.Button(top, text="清除勾选", command=self.select_none, style="Quiet.TButton", state="disabled")
        self.none_button.pack(side="right", padx=px(5))
        self.all_button = ttk.Button(top, text="勾选当前候选", command=self.select_all, style="Quiet.TButton", state="disabled")
        self.all_button.pack(side="right", padx=px(5))
        filters = ttk.Frame(results, padding=(px(18), 0, px(18), px(12)))
        filters.pack(fill="x")
        self.filter_buttons = {}
        for caption in ("全部", "候选", "豁免", "异常"):
            button = ttk.Radiobutton(filters, text=caption, value=caption, variable=self.view_filter, style="Filter.Toolbutton")
            button.pack(side="left", padx=(0, px(3)))
            self.filter_buttons[caption] = button
        self.search_entry = ttk.Entry(filters, textvariable=self.search, width=18)
        self.search_entry.pack(side="right")
        ttk.Label(filters, text="搜索路径", style="Muted.TLabel").pack(side="right", padx=(px(8), px(8)))
        ttk.Separator(results).pack(fill="x")

        # The inspector is compact until a row is selected.
        inspector = ttk.Frame(results)
        inspector.pack(side="bottom", fill="x")
        ttk.Separator(inspector).pack(fill="x")
        detail_header = ttk.Frame(inspector, padding=(px(18), px(8)))
        detail_header.pack(fill="x")
        ttk.Label(detail_header, textvariable=self.detail_title, style="Muted.TLabel").pack(side="left")
        self.detail_button = ttk.Button(detail_header, text="展开", command=self.toggle_details, style="Quiet.TButton")
        self.detail_button.pack(side="right")
        self.details_frame = ttk.Frame(inspector, padding=(px(18), 0, px(12), px(12)))
        self.details = tk.Text(self.details_frame, height=5, wrap="word", state="disabled", borderwidth=0,
                               highlightthickness=0, background=c["surface"], foreground=c["ink"],
                               font=self.fonts["small"], padx=0, pady=px(5), spacing1=2, spacing3=3)
        scrollbar = ttk.Scrollbar(self.details_frame, command=self.details.yview)
        self.details.configure(yscrollcommand=scrollbar.set)
        self.details.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.table = ttk.Frame(results)
        self.table.pack(fill="both", expand=True)
        columns = ("selected", "status", "path", "count", "maximum", "size")
        self.tree = ttk.Treeview(self.table, columns=columns, show="headings", selectmode="browse")
        for name, title, width in zip(columns, ("选择", "状态", "区域文件", "区块", "计时 / tick", "整组大小"),
                                      (48, 88, 270, 60, 110, 100)):
            self.tree.heading(name, text=title, anchor="w" if name == "path" else "center")
            self.tree.column(name, width=px(width), minwidth=px(40 if name != "path" else 150),
                             stretch=name == "path", anchor="w" if name == "path" else "center")
        self.tree.tag_configure("even", background=c["surface"])
        self.tree.tag_configure("odd", background="#F9FBF8")
        for status, color in (("候选", c["green"]), ("错误保留", c["red"]), ("坐标豁免", c["muted"]),
                              ("已删除", c["muted"]), ("部分删除", c["red"]), ("跳过", c["red"])):
            self.tree.tag_configure(status, foreground=color)
        ybar = ttk.Scrollbar(self.table, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(self.table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        self.table.rowconfigure(0, weight=1)
        self.table.columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self.click_checkbox)
        self.tree.bind("<space>", self.toggle_focused)
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.empty = ttk.Frame(self.table, padding=px(12))
        icon = tk.Canvas(self.empty, width=px(62), height=px(62), background=c["surface"], highlightthickness=0)
        icon.pack(pady=(0, px(14)))
        for x in range(3):
            for y in range(3):
                color = c["green"] if (x, y) == (1, 1) else ("#E5EDE3" if (x + y) % 2 else "#CCDCC9")
                icon.create_rectangle(px(3 + 20 * x), px(3 + 20 * y), px(19 + 20 * x), px(19 + 20 * y), fill=color, outline="")
        self.empty_title = ttk.Label(self.empty, text="从一次扫描开始", style="Section.TLabel")
        self.empty_title.pack()
        self.empty_caption = ttk.Label(self.empty, text="选择服务器目录，设置规则后开始扫描。", style="Muted.TLabel")
        self.empty_caption.pack(pady=(px(8), 0))
        self.empty.place(relx=0.5, rely=0.5, anchor="center")
        self.root.bind("<Configure>", self.resize_status, add=True)

    def scroll_sidebar(self, event):
        if str(event.widget).startswith(str(self.sidebar)):
            bounds = self.sidebar_canvas.bbox("all")
            if bounds and bounds[3] > self.sidebar_canvas.winfo_height():
                step = -int(event.delta / 120) if sys.platform == "win32" else -int(event.delta)
                self.sidebar_canvas.yview_scroll(step, "units")
                return "break"

    def resize_status(self, event):
        if event.widget is self.root:
            self.status_label.configure(wraplength=max(self.px(400), event.width - self.px(300)))

    def toggle_details(self):
        if self.details_frame.winfo_manager():
            self.details_frame.pack_forget()
            self.detail_button.configure(text="展开")
        else:
            self.details_frame.pack(fill="x")
            self.detail_button.configure(text="收起")

    def expand_details(self):
        if not self.details_frame.winfo_manager():
            self.toggle_details()

    def matches_filter(self, row):
        mode = self.view_filter.get()
        match = (mode == "全部" or (mode == "候选" and row.eligible) or
                 (mode == "豁免" and row.status == "坐标豁免") or
                 (mode == "异常" and row.status in ("错误保留", "部分删除", "跳过")))
        return match and self.search.get().strip().casefold() in row.path.casefold()

    def apply_filter(self, *_):
        self.visible.clear()
        for index, row in enumerate(self.rows):
            if self.matches_filter(row):
                self.tree.move(str(index), "", "end")
                self.visible.add(index)
            else:
                self.tree.detach(str(index))
        self.update_empty()
        self.update_summary()

    def update_empty(self):
        if self.visible:
            self.empty.place_forget()
            return
        if self.busy:
            title, caption = "正在扫描存档", "结果会陆续出现在这里，可以随时取消。"
        elif self.rows:
            title, caption = "没有匹配的区域文件", "尝试切换筛选条件或清空搜索。"
        elif self.valid:
            title, caption = "没有找到区域文件", "检查服务器根目录，或展开详情查看扫描信息。"
        else:
            title, caption = "从一次扫描开始", "选择服务器目录，设置规则后开始扫描。"
        self.empty_title.configure(text=title)
        self.empty_caption.configure(text=caption)
        self.empty.place(relx=0.5, rely=0.5, anchor="center")

    def settings(self):
        radius = None
        if self.protection.get():
            try:
                radius = int(self.radius.get())
            except ValueError:
                raise ValueError("R 必须是非负整数") from None
        return Settings(Path(self.folder.get()), parse_threshold(self.threshold.get(), self.unit.get()), radius)

    def invalidate(self, *_):
        self.valid = False
        self.selected.clear()
        try:
            ticks = parse_threshold(self.threshold.get(), self.unit.get())
            protection = (f"|x| < {self.radius.get()} 且 |z| < {self.radius.get()} 时豁免"
                          if self.protection.get() else "坐标豁免：关闭")
            self.formula.set(f"实际阈值 n = {ticks:,} tick（严格小于）\n{protection}")
            self.time_hint.set(f"{ticks:,} tick，等于阈值的区块会保留。")
        except ValueError:
            self.formula.set("请输入有效计时阈值")
            self.time_hint.set("请输入有效的活动时间。")
        self.protection_hint.set(
            f"|x| < {self.radius.get()} 且 |z| < {self.radius.get()} 时保留。\n使用区域坐标，正负坐标对称保护。"
            if self.protection.get() else "开启后，指定坐标范围内的区域始终保留。")
        self.radius_entry.configure(state="normal" if self.protection.get() and not self.busy else "disabled")
        if self.rows:
            self.phase.set("需要重新扫描")
            self.status.set("设置已改变，旧清单不可删除，请重新扫描。")
            for index in range(len(self.rows)):
                self.tree.set(str(index), "selected", "—")
            self.update_summary()
        self.update_buttons()

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.root, title="选择包含三个 world 文件夹的服务器根目录")
        if folder:
            self.folder.set(folder)

    def program_folder(self):
        path = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
        self.folder.set(str(path))

    def set_busy(self, busy):
        self.busy = busy
        for widget in self.controls:
            widget.configure(state="disabled" if busy else "normal")
        if not busy:
            self.unit_box.configure(state="readonly")
        self.radius_entry.configure(state="normal" if self.protection.get() and not busy else "disabled")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.update_empty()
        self.update_buttons()

    def update_buttons(self):
        available = self.valid and not self.busy
        for widget in (self.all_button, self.none_button, self.export_button):
            widget.configure(state="normal" if available else "disabled")
        self.delete_button.configure(state="normal" if available and self.selected else "disabled")

    def start_scan(self):
        try:
            settings = self.settings()
        except (ValueError, OSError) as exc:
            messagebox.showerror("设置无效", str(exc), parent=self.root)
            return
        self.scan_settings = settings
        self.valid = False
        self.tree.delete(*(str(i) for i in range(len(self.rows))))
        self.rows.clear()
        self.visible.clear()
        self.selected.clear()
        self.set_details("")
        self.detail_title.set("扫描详情")
        self.phase.set("正在扫描")
        self.cancel.clear()
        self.progress.configure(value=0, maximum=1)
        self.set_busy(True)
        self.summary.set("正在扫描…")
        self.update_summary()
        self.status.set(f"发现区域文件中：{settings.root}")

        def worker():
            try:
                result = scan(settings, self.cancel,
                              lambda i, n, row: self.events.put(("scan_progress", (i, n, row))))
                self.events.put(("scan_done", result))
            except Cancelled:
                self.events.put(("cancelled", None))
            except Exception as exc:
                self.events.put(("error", f"扫描失败：{exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def insert_row(self, row):
        index = len(self.rows)
        self.rows.append(row)
        if row.eligible:
            self.selected.add(index)
        relative = str(Path(row.path).relative_to(self.scan_settings.root))
        unknown = row.status in ("坐标豁免", "错误保留")
        self.tree.insert("", "end", iid=str(index), values=(
            "☑" if row.eligible else "—", row.status, relative, "—" if unknown else row.count,
            "—" if row.maximum is None else f"{row.maximum:,}", "—" if unknown else format_size(row.size)), tags=("even" if index % 2 == 0 else "odd", row.status))
        if self.matches_filter(row):
            self.visible.add(index)
            self.empty.place_forget()
        else:
            self.tree.detach(str(index))

    def update_summary(self):
        selected_bytes = sum(self.rows[i].size for i in self.selected)
        candidates = sum(row.eligible for row in self.rows)
        protected = sum(row.status == "坐标豁免" for row in self.rows)
        self.metrics["total"].set(f"{len(self.rows):,}")
        self.metrics["eligible"].set(f"{candidates:,}")
        self.metrics["protected"].set(f"{protected:,}")
        self.metrics["size"].set(format_size(selected_bytes))
        hidden = len(self.selected - self.visible)
        if self.selected:
            text = f"已勾选 {len(self.selected):,} 个区域，预计释放 {format_size(selected_bytes)}"
            if hidden:
                text += f"（包含筛选外 {hidden} 个）"
        else:
            text = "尚未选择文件"
        self.selection_text.set(text)
        self.summary.set(text)
        self.update_buttons()

    def toggle(self, iid):
        if not iid or not self.valid or self.busy:
            return
        index = int(iid)
        if not self.rows[index].eligible:
            return
        if index in self.selected:
            self.selected.remove(index)
        else:
            self.selected.add(index)
        self.tree.set(iid, "selected", "☑" if index in self.selected else "☐")
        self.update_summary()

    def click_checkbox(self, event):
        if self.tree.identify_column(event.x) == "#1":
            self.toggle(self.tree.identify_row(event.y))

    def toggle_focused(self, _event):
        self.toggle(self.tree.focus())
        return "break"

    def select_all(self):
        self.selected.update(i for i in self.visible if self.rows[i].eligible)
        self.refresh_checks()

    def select_none(self):
        self.selected.clear()
        self.refresh_checks()

    def refresh_checks(self):
        for index, row in enumerate(self.rows):
            self.tree.set(str(index), "selected", ("☑" if index in self.selected else "☐") if row.eligible else "—")
        self.update_summary()

    def set_details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def show_detail(self, _event=None):
        selection = self.tree.selection()
        if selection:
            row = self.rows[int(selection[0])]
            self.detail_title.set(f"{Path(row.path).name} / {row.status}")
            self.expand_details()
            lines = [row.path, f"{row.status}：{row.reason}"]
            lines.extend(f"关联清理项：{item.path}  ({format_size(item.size)})" for item in row.files)
            self.set_details("\n".join(lines))

    def export(self):
        path = filedialog.asksaveasfilename(parent=self.root, title="导出扫描清单", defaultextension=".json",
                                          filetypes=(("JSON", "*.json"), ("CSV", "*.csv")))
        if not path:
            return
        try:
            if Path(path).suffix.lower() not in (".csv", ".json"):
                raise OSError("清单文件必须使用 .json 或 .csv 扩展名")
            if Path(path).suffix.lower() == ".csv":
                with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(("根目录", "阈值tick", "豁免R", "已勾选", "路径", "状态", "原因", "区块数", "最大计时", "整组字节数", "关联文件"))
                    for index, row in enumerate(self.rows):
                        writer.writerow((str(self.scan_settings.root), self.scan_settings.threshold,
                                         self.scan_settings.protected_radius, index in self.selected, row.path,
                                         row.status, row.reason, row.count, row.maximum, row.size,
                                         "\n".join(item.path for item in row.files)))
            else:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump({"settings": self.scan_settings.to_dict(), "rows": [
                        {**row.to_dict(), "selected": i in self.selected} for i, row in enumerate(self.rows)]},
                        handle, ensure_ascii=False, indent=2)
            self.status.set(f"清单已导出：{path}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)

    def confirm_delete(self):
        if not self.valid or self.busy or not self.selected:
            return
        try:
            unchanged = self.settings() == self.scan_settings
        except (ValueError, OSError) as exc:
            self.invalidate()
            messagebox.showerror("设置或目录已变化", str(exc), parent=self.root)
            return
        if not unchanged:
            self.invalidate()
            return
        rows = tuple(self.rows[i] for i in sorted(self.selected))
        dialog = tk.Toplevel(self.root)
        dialog.title("审阅删除清单")
        px, c = self.px, COLORS
        width, height = px(920), px(660)
        dialog.geometry(f"{width}x{height}")
        dialog.minsize(px(780), px(590))
        dialog.configure(background=c["surface"])
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=px(24))
        frame.pack(fill="both", expand=True)
        files = [item for row in rows for item in row.files]
        banner = ttk.Frame(frame, style="Danger.TFrame", padding=(px(18), px(15)))
        banner.pack(fill="x")
        ttk.Label(banner, text="确认永久删除", style="DangerTitle.TLabel").pack(anchor="w")
        ttk.Label(banner, text="文件不会进入回收站，请核对下方清单。", style="DangerMuted.TLabel"
                  ).pack(anchor="w", pady=(px(6), 0))
        counts = ttk.Frame(frame, padding=(0, px(18)))
        counts.pack(fill="x")
        for caption, value in (("区域", str(len(rows))), ("关联文件", str(len(files))),
                               ("预计释放", format_size(sum(item.size for item in files)))):
            block = ttk.Frame(counts)
            block.pack(side="left", padx=(0, px(46)))
            ttk.Label(block, text=caption, style="Muted.TLabel").pack(anchor="w")
            ttk.Label(block, text=value, style="Section.TLabel").pack(anchor="w", pady=(px(4), 0))
        ttk.Label(frame, text=f"根目录：{self.scan_settings.root}", style="Muted.TLabel",
                  wraplength=px(860)).pack(anchor="w")
        ttk.Label(frame, text=self.formula.get().replace("\n", "；"), style="Muted.TLabel",
                  wraplength=px(860)).pack(anchor="w", pady=(px(5), px(12)))

        stopped, backed_up = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        accepted = [False]
        bottom = ttk.Frame(frame)
        bottom.pack(side="bottom", fill="x")
        ttk.Separator(bottom).pack(fill="x", pady=(px(14), px(9)))
        buttons = ttk.Frame(bottom)
        confirm = ttk.Button(buttons, text="永久删除以上文件", style="Danger.TButton", state="disabled")

        def ready():
            confirm.configure(state="normal" if stopped.get() and backed_up.get() else "disabled")

        ttk.Checkbutton(bottom, text="我已停止服务器及其他写入存档的程序", variable=stopped,
                        command=ready).pack(anchor="w")
        ttk.Checkbutton(bottom, text="我已备份存档，理解低计时区块仍可能包含建筑", variable=backed_up,
                        command=ready).pack(anchor="w", pady=(0, px(8)))

        def accept():
            if stopped.get() and backed_up.get():
                accepted[0] = True
                dialog.destroy()

        confirm.configure(command=accept)
        buttons.pack(fill="x", pady=(px(7), 0))
        confirm.pack(side="right")
        ttk.Button(buttons, text="返回检查", command=dialog.destroy).pack(side="right", padx=px(10))
        ttk.Label(buttons, text="中途失败可能导致部分文件已删除。", style="Muted.TLabel").pack(side="left")

        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)
        listing = ttk.Treeview(list_frame, columns=("path", "size"), show="headings", selectmode="browse")
        listing.heading("path", text="将删除的文件（相对根目录）", anchor="w")
        listing.heading("size", text="大小")
        listing.column("path", width=px(640), minwidth=px(350))
        listing.column("size", width=px(100), minwidth=px(90), stretch=False, anchor="e")
        for index, item in enumerate(files):
            listing.insert("", "end", values=(str(Path(item.path).relative_to(self.scan_settings.root)), format_size(item.size)),
                           tags=("even" if index % 2 == 0 else "odd",))
        listing.tag_configure("odd", background="#F9FBF8")
        scrollbar = ttk.Scrollbar(list_frame, command=listing.yview)
        horizontal = ttk.Scrollbar(list_frame, orient="horizontal", command=listing.xview)
        listing.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        listing.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        self.root.wait_window(dialog)
        if accepted[0]:
            self.start_delete(rows)

    def start_delete(self, rows):
        self.valid = False
        self.phase.set("正在删除")
        self.cancel.clear()
        self.set_busy(True)
        self.progress.configure(value=0, maximum=len(rows))
        self.status.set("正在复核文件并删除；请保持服务器关闭。")
        settings = self.scan_settings

        def worker():
            try:
                result = delete_candidates(settings, rows, confirmed=True, cancel=self.cancel,
                                           progress=lambda i, n, result: self.events.put(("delete_progress", (i, n, result))))
                self.events.put(("delete_done", result))
            except Exception as exc:
                self.events.put(("error", f"删除中止：{exc}\n可能已有文件删除，请检查应用数据目录中的操作日志。"))
        threading.Thread(target=worker, daemon=True).start()

    def request_cancel(self):
        self.cancel.set()
        self.phase.set("正在取消")
        self.cancel_button.configure(state="disabled")
        self.status.set("正在取消；等待当前读取操作结束。已完成的删除不会恢复。")

    def poll(self):
        try:
            for _ in range(100):
                kind, value = self.events.get_nowait()
                if kind == "scan_progress":
                    i, total, row = value
                    self.insert_row(row)
                    self.progress.configure(value=i, maximum=max(1, total))
                    self.status.set(f"已扫描 {i:,} / {total:,} 个区域文件")
                    if i % 100 == 0 or i == total:
                        self.update_summary()
                elif kind == "scan_done":
                    _, notes = value
                    self.valid = True
                    self.phase.set("扫描完成")
                    self.set_busy(False)
                    self.update_summary()
                    self.status.set("扫描完成。请检查候选和豁免范围，再审阅删除清单。")
                    self.set_details("\n".join(notes) if notes else "扫描完成，无目录访问异常。")
                    if notes:
                        self.detail_title.set(f"扫描信息 / {len(notes)} 条提示")
                    if not self.rows:
                        self.expand_details()
                elif kind == "cancelled":
                    self.phase.set("扫描已取消")
                    self.valid = False
                    self.selected.clear()
                    self.set_busy(False)
                    self.refresh_checks()
                    self.status.set("扫描已取消；未修改任何存档。部分扫描结果不可用于删除，请重新扫描。")
                elif kind == "delete_progress":
                    i, total, result = value
                    self.progress.configure(value=i, maximum=total)
                    self.status.set(f"处理 {i}/{total}：{result['status']}")
                    for index, row in enumerate(self.rows):
                        if row.path == result["region"]:
                            row.status = result["status"]
                            row.reason = result["reason"]
                            self.tree.set(str(index), "status", row.status)
                            self.tree.set(str(index), "selected", "—")
                            self.tree.item(str(index), tags=("even" if index % 2 == 0 else "odd", row.status))
                elif kind == "delete_done":
                    self.phase.set("处理结束")
                    self.selected.clear()
                    self.set_busy(False)
                    self.update_summary()
                    self.status.set(f"处理结束，实际删除 {format_size(value['deleted_bytes'])}。继续操作前请重新扫描。")
                    lines = [f"实际删除：{format_size(value['deleted_bytes'])}", f"日志：{value['log']}"]
                    lines.extend(f"{item['status']}  {item['region']}\n{item['reason']}" for item in value["results"])
                    self.set_details("\n".join(lines))
                    self.detail_title.set("操作结果")
                    self.expand_details()
                    self.apply_filter()
                    problems = sum(result["status"] != "已删除" for result in value["results"])
                    messagebox.showinfo("删除结果", f"实际删除：{format_size(value['deleted_bytes'])}\n"
                                        f"跳过/未完整删除：{problems} 组\n未处理：{value['unprocessed']} 组\n"
                                        f"详细日志：{value['log']}", parent=self.root)
                elif kind == "error":
                    self.phase.set("操作中止")
                    self.valid = False
                    self.selected.clear()
                    self.set_busy(False)
                    self.status.set("操作失败，旧清单已失效。")
                    self.set_details(value)
                    self.expand_details()
                    messagebox.showerror("操作失败", value, parent=self.root)
        except queue.Empty:
            pass
        self.poll_id = self.root.after(80, self.poll)

    def close(self):
        if self.busy:
            if messagebox.askyesno("操作进行中", "要取消当前操作吗？请等待取消完成后再关闭窗口。",
                                   parent=self.root):
                self.request_cancel()
            return
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def main():
    root = tk.Tk()
    CleanerApp(root)
    root.mainloop()
