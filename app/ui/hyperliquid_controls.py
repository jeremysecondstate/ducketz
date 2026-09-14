"""Native Tk controls with the restrained tint and borders from Concept B."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from app.ui.theme import MUTED_TEXT, SURFACE, TEXT

INSET = "#0b1b2b"
MINT = "#78edc1"


def _skin(master, top, bottom, border):
    """Nine-slice widget paint, generated in code rather than from logo assets."""
    factor, width, height = 4, 48, 40
    canvas = Image.new("RGBA", (width * factor, height * factor))
    draw = ImageDraw.Draw(canvas)
    first = tuple(bytes.fromhex(top.lstrip("#")))
    last = tuple(bytes.fromhex(bottom.lstrip("#")))
    for y in range(height * factor):
        # A uniform center prevents bands when Tk tiles the nine-slice center.
        t = .5
        if y < 2 * factor:
            t = .25 + y / (2 * factor) * .25
        elif y > (height - 2) * factor:
            t = .5 + (y - (height - 2) * factor) / (2 * factor) * .25
        color = tuple(round(a + (b - a) * t) for a, b in zip(first, last))
        draw.line((0, y, width * factor, y), fill=(*color, 255))
    mask = Image.new("L", canvas.size)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width * factor - 1, height * factor - 1), radius=4 * factor, fill=255)
    canvas.putalpha(mask)
    draw.rounded_rectangle((2, 2, width * factor - 3, height * factor - 3), radius=4 * factor, outline=border, width=factor)
    return ImageTk.PhotoImage(canvas.resize((width, height), Image.Resampling.LANCZOS), master=master)


def install_order_styles(root, fonts, scale=1.0):
    """Keep native focus, keyboard, and radio behavior; omit the radio indicator."""
    style = ttk.Style(root)
    owner = root._root()
    if not hasattr(owner, "_hyper_order_images"):
        owner._hyper_order_images = []
        disabled = _skin(owner, "#142231", "#101c2a", "#283a4c")
        owner._hyper_order_images.append(disabled)
        palettes = {
            "Long": (("#13362f", "#10262b", "#357b67"), ("#205447", "#143831", "#78edc1")),
            "Short": (("#30202b", "#221c28", "#904b5b"), ("#57303b", "#391f2d", "#ef7785")),
            "Product": (("#112437", "#0b1b2b", "#314b62"), ("#19423f", "#123331", "#69dcbc")),
            "Action": (("#12283b", "#0c1d2d", "#35556e"), ("#1c3b50", "#102a3c", "#7ea4bf")),
            "Primary": (("#9bf7db", "#61e7bc", "#a3ffe4"), ("#b4ffe6", "#7af1ca", "#c6ffed")),
            "Field": ((INSET, INSET, "#35556e"), (INSET, INSET, "#73cdb5")),
        }
        for name, (normal, active) in palettes.items():
            base, highlight = _skin(owner, *normal), _skin(owner, *active)
            owner._hyper_order_images.extend((base, highlight))
            style.element_create(f"HyperOrder{name}.surface", "image", base,
                                 ("disabled", disabled), ("selected", highlight),
                                 ("focus", highlight), ("active", highlight), border=5, sticky="nsew")
        # Native checkbox, with a larger outlined square and mint check.
        for checked in (False, True):
            img = Image.new("RGBA", (20, 20))
            pen = ImageDraw.Draw(img)
            pen.rounded_rectangle((2, 2, 17, 17), radius=2, fill="#153b36" if checked else INSET, outline=MINT if checked else "#60849d", width=1)
            if checked:
                pen.line((5, 9, 8, 12, 14, 6), fill=MINT, width=2)
            owner._hyper_order_images.append(ImageTk.PhotoImage(img, master=owner))
        style.element_create("HyperOrder.check", "image", owner._hyper_order_images[-2], ("selected", owner._hyper_order_images[-1]), sticky="")
        arrow = Image.new("RGBA", (22, 20))
        ImageDraw.Draw(arrow).line((6, 7, 11, 12, 16, 7), fill=TEXT, width=2)
        owner._hyper_order_images.append(ImageTk.PhotoImage(arrow, master=owner))
        style.element_create("HyperOrder.downarrow", "image", owner._hyper_order_images[-1], sticky="")

    for name in ("Long", "Short", "Product"):
        style.layout(f"HyperOrder{name}.TRadiobutton", [(f"HyperOrder{name}.surface", {
            "sticky": "nsew", "children": [("Radiobutton.padding", {
                "sticky": "nsew", "children": [("Radiobutton.focus", {
                    "sticky": "nsew", "children": [("Radiobutton.label", {"sticky": "nsew"})]})]})]})])
        style.configure(f"HyperOrder{name}.TRadiobutton", anchor="center", font=fonts["body"],
                        foreground="#f48a96" if name == "Short" else MINT if name == "Long" else TEXT,
                        padding=(round(10 * scale), round(4 * scale)), background=SURFACE)
    for name in ("Action", "Primary"):
        style.layout(f"HyperOrder{name}.TButton", [(f"HyperOrder{name}.surface", {
            "sticky": "nsew", "children": [("Button.padding", {"sticky": "nsew", "children": [
                ("Button.focus", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})]})])
        style.configure(f"HyperOrder{name}.TButton", font=fonts["strong"] if name == "Primary" else fonts["body"],
                        foreground="#06281e" if name == "Primary" else TEXT, anchor="center",
                        padding=(round(10 * scale), round((6 if name == "Primary" else 4) * scale)))
        style.map(f"HyperOrder{name}.TButton", foreground=[("disabled", MUTED_TEXT)])
    style.layout("HyperOrder.TCombobox", [("HyperOrderField.surface", {"sticky": "nsew", "children": [
        ("HyperOrder.downarrow", {"side": "right", "sticky": "ns"}),
        ("Combobox.padding", {"sticky": "nsew", "children": [("Combobox.textarea", {"sticky": "nsew"})]})]})])
    style.layout("HyperOrderInline.TCombobox", [("HyperOrder.downarrow", {"side": "right", "sticky": "ns"}),
        ("Combobox.padding", {"sticky": "nsew", "children": [("Combobox.textarea", {"sticky": "nsew"})]})])
    for name in ("HyperOrder.TCombobox", "HyperOrderInline.TCombobox"):
        style.configure(name, foreground=TEXT, fieldbackground=INSET, background=INSET, arrowcolor=TEXT,
                        arrowsize=round(14 * scale), padding=(round(8 * scale), round(5 * scale)))
        style.map(name, foreground=[("readonly", TEXT)], fieldbackground=[("readonly", INSET)], selectbackground=[("readonly", INSET)], selectforeground=[("readonly", TEXT)])
    style.layout("HyperOrderField.TFrame", [("HyperOrderField.surface", {"sticky": "nsew"})])
    style.layout("HyperOrderField.TEntry", [("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})])
    style.layout("HyperOrderLink.TButton", [("Button.padding", {"sticky": "nsew", "children": [("Button.focus", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
    style.configure("HyperOrderLink.TButton", foreground=MINT, background=SURFACE, font=fonts["small"], padding=(3, 2))
    style.configure("HyperOrderField.TEntry", padding=0, foreground=TEXT, fieldbackground=INSET, background=INSET, insertcolor=TEXT)
    style.layout("HyperOrder.TCheckbutton", [("Checkbutton.padding", {"sticky": "nsew", "children": [
        ("HyperOrder.check", {"side": "left", "sticky": ""}), ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [("Checkbutton.label", {"sticky": "w"})]})]})])
    style.configure("HyperOrder.TCheckbutton", font=fonts["body"], foreground=TEXT, background=SURFACE, padding=(0, 3))
    style.configure("HyperOrders.Treeview", font=fonts["body"], rowheight=round(38 * scale), background=INSET, fieldbackground=INSET, foreground=TEXT, bordercolor="#35556e", lightcolor=INSET, darkcolor=INSET, borderwidth=1)
    style.configure("HyperOrders.Treeview.Heading", font=fonts["small"], foreground=MUTED_TEXT, background=INSET, bordercolor=INSET, lightcolor=INSET, darkcolor=INSET, relief="flat", padding=(8, 8))
    style.map("HyperOrders.Treeview", background=[("selected", "#174b4a")], foreground=[("selected", TEXT)])


class UnitEntry(ttk.Frame):
    def __init__(self, parent, variable, *, font, unit="", unit_variable=None):
        super().__init__(parent, style="HyperOrderField.TFrame", padding=(10, 7))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.entry = ttk.Entry(self, textvariable=variable, font=font, width=1, style="HyperOrderField.TEntry")
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<FocusIn>", lambda e: self.state(["focus"]))
        self.entry.bind("<FocusOut>", lambda e: self.state(["!focus"]))
        if unit_variable is not None:
            self.unit = ttk.Combobox(self, textvariable=unit_variable, values=("USDC", "BASE"), state="readonly", width=6, font=font, style="HyperOrderInline.TCombobox")
        else:
            self.unit = tk.Label(self, text=unit, bg=INSET, fg=MUTED_TEXT, font=font)
        self.unit.grid(row=0, column=1, padx=(8, 0), sticky="e")

    def set_scale(self, scale):
        self.configure(padding=(round(10 * scale), round(7 * scale)))
        self.rowconfigure(0, minsize=round(26 * scale))


class EmptyOrders(tk.Frame):
    def __init__(self, parent, fonts):
        super().__init__(parent, bg=INSET, highlightbackground="#35556e", highlightthickness=1)
        self.group = tk.Frame(self, bg=INSET)
        self.group.place(relx=.5, rely=.5, anchor="center")
        self.icon = tk.Canvas(self.group, width=46, height=48, highlightthickness=0, bg=INSET)
        self.icon.pack(pady=(0, 10))
        self.title = tk.Label(self.group, bg=INSET, fg=TEXT, font=fonts["body"])
        self.title.pack()
        self.detail = tk.Label(self.group, bg=INSET, fg=MUTED_TEXT, font=fonts["small"])
        self.detail.pack(pady=(6, 0))
        self.bind("<Configure>", lambda e: [w.configure(wraplength=max(100, e.width - 32)) for w in (self.title, self.detail)])
        self.set_scale(1)

    def set_scale(self, scale):
        self.icon.configure(width=round(46 * scale), height=round(48 * scale))
        self.icon.delete("all")
        points = [12, 4, 30, 4, 39, 13, 39, 42, 12, 42, 12, 4]
        self.icon.create_line(*(p * scale for p in points), fill="#7ca0bd", width=1.5 * scale, joinstyle="round")
        self.icon.create_line(*(p * scale for p in (30, 4, 30, 13, 39, 13)), fill="#7ca0bd", width=1.5 * scale)
        for y in (23, 29):
            self.icon.create_line(19 * scale, y * scale, 32 * scale, y * scale, fill="#45627b", width=scale)

    def set_message(self, title, detail):
        self.title.configure(text=title)
        self.detail.configure(text=detail)


class ExposureTable(tk.Frame):
    def __init__(self, parent, fonts, asset_class):
        super().__init__(parent, bg=INSET, highlightbackground="#35556e", highlightthickness=1)
        self.fonts, self.asset_class = fonts, asset_class
        self._rows, self._scope, self._scale = [], "All accounts", 1

    def set_rows(self, rows, scope):
        self._rows, self._scope = rows, scope
        self._draw()

    def set_scale(self, scale):
        self._scale = scale
        self._draw()

    def _draw(self):
        for child in self.winfo_children():
            child.destroy()
        self.columnconfigure(1, weight=1)
        self.columnconfigure(0, minsize=round(100 * self._scale))
        for col, text in enumerate(("Market", f"Position · {self._scope}")):
            tk.Label(self, text=text, bg=INSET, fg=MUTED_TEXT, font=self.fonts["small"], anchor="w", pady=0).grid(row=0, column=col, sticky="ew", padx=10, pady=7)
        for i, (coin, long, short, present) in enumerate(self._rows):
            row = i * 2 + 1
            tk.Frame(self, height=1, bg="#263e52").grid(row=row, column=0, columnspan=2, sticky="ew")
            market = tk.Frame(self, bg=INSET)
            market.grid(row=row + 1, column=0, sticky="w", padx=(8, 10), pady=5)
            icon = self.asset_class(market, coin.lower(), size=round(25 * self._scale))
            icon.configure(background=INSET)
            icon.pack(side="left", padx=(0, 6))
            tk.Label(market, text=coin, bg=INSET, fg=TEXT, font=self.fonts["body"]).pack(side="left")
            position = tk.Frame(self, bg=INSET)
            position.grid(row=row + 1, column=1, sticky="ew", padx=(0, 10), pady=3)
            def quantity(value):
                return f"{value:,.8f}".rstrip("0").rstrip(".") or "0"
            primary = f"Net {quantity(long - short)}" if present else "Watching"
            secondary = f"Long {quantity(long)} / Short {quantity(short)}" if present else "No positions"
            tk.Label(position, text=primary, bg=INSET, fg=TEXT, font=self.fonts["body"], anchor="w", pady=0).pack(fill="x")
            detail = tk.Label(position, text=secondary, bg=INSET, fg=MUTED_TEXT, font=self.fonts["small"], anchor="w", justify="left", pady=0)
            detail.pack(fill="x")
            position.bind("<Configure>", lambda e, label=detail: label.configure(wraplength=max(80, e.width)))
