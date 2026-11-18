"""Small custom Tk widgets for a flat, modern look: rounded buttons and thin progress bars."""
import tkinter as tk


def _rounded_rect(canvas, x0, y0, x1, y1, radius, **kwargs):
    radius = min(radius, (x1 - x0) / 2, (y1 - y0) / 2)
    points = [
        x0 + radius, y0, x1 - radius, y0, x1, y0, x1, y0 + radius,
        x1, y1 - radius, x1, y1, x1 - radius, y1, x0 + radius, y1,
        x0, y1, x0, y1 - radius, x0, y0 + radius, x0, y0,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=16, **kwargs)


def _shade(hex_color, factor):
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    if factor >= 1:
        r, g, b = (int(c + (255 - c) * (factor - 1)) for c in (r, g, b))
    else:
        r, g, b = (int(c * factor) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


class RoundedButton(tk.Canvas):
    """Flat rounded button with hover and pressed states."""

    def __init__(self, parent, text, command, color, background, width=112, height=36, radius=10,
                 text_color="#ffffff", disabled_color="#2b313c", disabled_text="#5d6675", font=("Segoe UI Semibold", 10)):
        super().__init__(parent, width=width, height=height, bg=background, highlightthickness=0, bd=0)
        self._command = command
        self._color = color
        self._text_color = text_color
        self._disabled_color = disabled_color
        self._disabled_text = disabled_text
        self._enabled = True
        self._hover = False
        self._shape = _rounded_rect(self, 1, 1, width - 1, height - 1, radius, fill=color, outline="")
        self._label = self.create_text(width / 2, height / 2, text=text, fill=text_color, font=font)
        self.bind("<Enter>", lambda _: self._set_hover(True))
        self.bind("<Leave>", lambda _: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)

    def _paint(self, pressed=False):
        if not self._enabled:
            fill, text = self._disabled_color, self._disabled_text
        elif pressed:
            fill, text = _shade(self._color, 0.82), self._text_color
        elif self._hover:
            fill, text = _shade(self._color, 1.12), self._text_color
        else:
            fill, text = self._color, self._text_color
        self.itemconfigure(self._shape, fill=fill)
        self.itemconfigure(self._label, fill=text)
        self.configure(cursor="hand2" if self._enabled else "arrow")

    def _set_hover(self, hover):
        self._hover = hover
        self._paint()

    def _press(self, _):
        if self._enabled:
            self._paint(pressed=True)

    def _release(self, event):
        if not self._enabled:
            return
        self._paint()
        if 0 <= event.x <= int(self["width"]) and 0 <= event.y <= int(self["height"]):
            self._command()

    def set_enabled(self, enabled):
        if enabled != self._enabled:
            self._enabled = enabled
            self._paint()

    def set_text(self, text):
        self.itemconfigure(self._label, text=text)


class Toggle(tk.Canvas):
    """On/off switch bound to a tk.BooleanVar."""

    def __init__(self, parent, variable, on_color, background, track="#2a303b", width=38, height=20):
        super().__init__(parent, width=width, height=height, bg=background, highlightthickness=0, bd=0, cursor="hand2")
        # Note: never use self._w here, Tk keeps the widget path in it.
        self._var, self._on, self._track, self._width, self._height = variable, on_color, track, width, height
        self.bind("<Button-1>", lambda _: variable.set(not variable.get()))
        variable.trace_add("write", lambda *_: self._paint())
        self._paint()

    def _paint(self):
        self.delete("all")
        on = bool(self._var.get())
        _rounded_rect(self, 0, 0, self._width, self._height, self._height / 2, fill=self._on if on else self._track,
                      outline="")
        knob = self._height - 6
        x = self._width - knob - 3 if on else 3
        self.create_oval(x, 3, x + knob, 3 + knob, fill="#ffffff", outline="")


class TabBar(tk.Frame):
    """Text tabs with an accent underline on the selected one."""

    def __init__(self, parent, tabs, command, colors, font, bold_font):
        super().__init__(parent, bg=colors["bg"])
        self._colors, self._command, self._items = colors, command, {}
        for key, label in tabs:
            item = tk.Frame(self, bg=colors["bg"], cursor="hand2")
            item.pack(side="left", padx=(0, 22))
            text = tk.Label(item, text=label, bg=colors["bg"], font=(bold_font, 11), cursor="hand2")
            text.pack()
            line = tk.Frame(item, height=2, bg=colors["bg"])
            line.pack(fill="x", pady=(4, 0))
            for widget in (item, text):
                widget.bind("<Button-1>", lambda _, k=key: self.select(k))
            self._items[key] = (text, line)

    def select(self, key):
        for name, (text, line) in self._items.items():
            active = name == key
            text.configure(fg=self._colors["text"] if active else self._colors["muted"])
            line.configure(bg=self._colors["accent"] if active else self._colors["bg"])
        self._command(key)


class Bar(tk.Canvas):
    """Thin rounded progress bar."""

    def __init__(self, parent, color, background, track="#2a303b", width=236, height=8):
        super().__init__(parent, width=width, height=height, bg=background, highlightthickness=0, bd=0)
        self._width, self._height, self._color = width, height, color
        _rounded_rect(self, 0, 0, width, height, height / 2, fill=track, outline="")
        self._fill = None
        self._value = None

    def set(self, fraction):
        fraction = max(0.0, min(1.0, fraction))
        if self._value is not None and abs(fraction - self._value) < 0.002:
            return
        self._value = fraction
        if self._fill is not None:
            self.delete(self._fill)
            self._fill = None
        if fraction > 0:
            width = max(self._height, self._width * fraction)
            self._fill = _rounded_rect(self, 0, 0, width, self._height, self._height / 2, fill=self._color, outline="")
