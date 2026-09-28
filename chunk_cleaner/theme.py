"""A consistent, dependency-free desktop theme for Tk on macOS and Windows."""

import sys
from tkinter import font, ttk

COLORS = {
    "canvas": "#F3F5F2",
    "surface": "#FFFFFF",
    "sidebar": "#FAFBF9",
    "ink": "#202B25",
    "muted": "#748078",
    "line": "#E1E7E0",
    "green": "#276047",
    "green_hover": "#1D4C38",
    "green_soft": "#EAF3EC",
    "red": "#A63D36",
    "red_hover": "#8F302B",
    "red_soft": "#FCF0ED",
    "disabled": "#E9EDE8",
}


def apply_theme(root):
    available = set(font.families(root))
    choices = (("PingFang SC", "Heiti SC") if sys.platform == "darwin"
               else ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"))
    family = next((item for item in choices if item in available), "TkDefaultFont")
    mono = "Menlo" if sys.platform == "darwin" else "Consolas"
    # Pixel sizes keep layout measurements stable across Tk versions; Tk/OS still
    # handles Retina rendering. Windows scaling is accounted for explicitly.
    scale = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72)) if sys.platform == "win32" else 1.0

    def px(value):
        return round(value * scale)

    fonts = {
        "body": (family, -px(13)),
        "small": (family, -px(12)),
        "strong": (family, -px(13), "bold"),
        "title": (family, -px(23), "bold"),
        "section": (family, -px(16), "bold"),
        "metric": (family, -px(27), "bold"),
        "mono": (mono, -px(12)),
    }
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        font.nametofont(name).configure(family=family, size=-px(13))
    root.option_add("*Font", fonts["body"])
    root.option_add("*TCombobox*Listbox.font", fonts["body"])
    root.configure(background=COLORS["canvas"])
    style = ttk.Style(root)
    style.theme_use("clam")
    c = COLORS
    style.configure(".", background=c["surface"], foreground=c["ink"], font=fonts["body"],
                    bordercolor=c["line"], lightcolor=c["line"], darkcolor=c["line"],
                    troughcolor=c["canvas"], focuscolor=c["green"])
    for name, color in (("TFrame", c["surface"]), ("Canvas.TFrame", c["canvas"]),
                        ("Sidebar.TFrame", c["sidebar"]), ("Soft.TFrame", c["green_soft"]),
                        ("Danger.TFrame", c["red_soft"])):
        style.configure(name, background=color)
    style.configure("TLabel", background=c["surface"], font=fonts["body"])
    for name, opts in {
        "Title.TLabel": {"font": fonts["title"]},
        "Section.TLabel": {"font": fonts["section"]},
        "Strong.TLabel": {"font": fonts["strong"]},
        "Muted.TLabel": {"foreground": c["muted"], "font": fonts["small"]},
        "Sidebar.TLabel": {"background": c["sidebar"]},
        "SidebarHeading.TLabel": {"background": c["sidebar"], "font": fonts["strong"]},
        "SidebarMuted.TLabel": {"background": c["sidebar"], "foreground": c["muted"], "font": fonts["small"]},
        "Metric.TLabel": {"font": fonts["metric"]},
        "GreenMetric.TLabel": {"font": fonts["metric"], "foreground": c["green"]},
        "Badge.TLabel": {"background": c["green_soft"], "foreground": c["green"], "padding": (12, 6), "font": fonts["small"]},
        "Soft.TLabel": {"background": c["green_soft"], "foreground": c["green"], "font": fonts["small"]},
        "DangerTitle.TLabel": {"background": c["red_soft"], "foreground": c["red"], "font": fonts["title"]},
        "DangerMuted.TLabel": {"background": c["red_soft"], "foreground": c["red"], "font": fonts["small"]},
    }.items():
        style.configure(name, **opts)
    style.configure("TButton", padding=(px(12), px(8)), borderwidth=1, relief="flat",
                    background=c["surface"], foreground=c["ink"], focusthickness=1)
    style.map("TButton", background=[("disabled", c["canvas"]), ("active", c["green_soft"])],
              foreground=[("disabled", "#A4ADA6")], bordercolor=[("focus", c["green"])])
    for name, base, hover in (("Primary.TButton", c["green"], c["green_hover"]),
                              ("Danger.TButton", c["red"], c["red_hover"])):
        style.configure(name, font=fonts["strong"], background=base, foreground="white",
                        bordercolor=base, padding=(px(18), px(10)))
        style.map(name, background=[("disabled", c["disabled"]), ("active", hover)],
                  foreground=[("disabled", "#98A29A"), ("!disabled", "white")],
                  bordercolor=[("disabled", c["disabled"]), ("!disabled", base)])
    style.configure("Quiet.TButton", borderwidth=0, padding=(px(8), px(6)), foreground=c["muted"])
    style.configure("Filter.Toolbutton", padding=(px(12), px(6)), relief="flat", borderwidth=0,
                    background=c["surface"], foreground=c["muted"], font=fonts["small"])
    style.map("Filter.Toolbutton", background=[("selected", c["green_soft"]), ("active", c["canvas"])],
              foreground=[("selected", c["green"]), ("active", c["ink"])])
    style.configure("TEntry", padding=px(8), fieldbackground=c["surface"], borderwidth=1,
                    insertcolor=c["ink"])
    style.map("TEntry", bordercolor=[("focus", c["green"])],
              fieldbackground=[("disabled", c["canvas"])], foreground=[("disabled", "#9EA89F")])
    style.configure("TCombobox", padding=px(7), arrowsize=px(12), fieldbackground=c["surface"])
    style.map("TCombobox", fieldbackground=[("readonly", c["surface"]), ("disabled", c["canvas"])],
              selectbackground=[("readonly", c["surface"])], selectforeground=[("readonly", c["ink"])])
    style.configure("TCheckbutton", padding=(0, px(5)), indicatorcolor=c["surface"],
                    indicatormargin=(0, 0, px(8), 0))
    style.configure("Sidebar.TCheckbutton", background=c["sidebar"])
    style.map("TCheckbutton", indicatorcolor=[("selected", c["green"])],
              foreground=[("disabled", c["muted"])])
    style.configure("Treeview", font=fonts["body"], rowheight=px(39), borderwidth=0,
                    fieldbackground=c["surface"], background=c["surface"], foreground=c["ink"])
    style.configure("Treeview.Heading", background="#F6F8F5", foreground=c["muted"],
                    font=fonts["small"], padding=(px(8), px(10)), borderwidth=0, relief="flat")
    style.map("Treeview", background=[("selected", "#E2EFE5")], foreground=[("selected", "#214D36")])
    style.map("Treeview.Heading", background=[("active", "#EEF2EC")])
    style.configure("Horizontal.TProgressbar", background=c["green"], troughcolor=c["line"],
                    borderwidth=0, lightcolor=c["green"], darkcolor=c["green"], thickness=3)
    style.configure("TScrollbar", borderwidth=0, arrowsize=px(10), background="#CCD6CB",
                    troughcolor=c["surface"], relief="flat")
    return fonts, px
