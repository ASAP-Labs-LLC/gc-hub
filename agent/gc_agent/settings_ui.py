"""The tray's Settings… dialog (tkinter), run as its own process
(``agent_main.py --settings``) so it never shares a thread with the tray.
Saving rewrites agent.json atomically; the running agent notices the change,
reloads it and retries anything that was on hold."""
from __future__ import annotations

from pathlib import Path

from . import config

FIELDS = ("hub_url", "token", "watch_dir", "include_subdirs", "poll_seconds", "stable_seconds",
          "results_mirror_path")


def _num(key, v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    try:
        f = float(str(v).strip())
    except ValueError:
        raise config.ConfigError("agent.json: bad %s (must be a number)" % key)
    return int(f) if f.is_integer() else f


def apply_settings(cfg, form):
    """Return the validated new config: *cfg* with *form*'s values applied.
    A blank token keeps the current one (the dialog never shows it)."""
    new = dict(cfg)
    for key in FIELDS:
        if key not in form:
            continue
        v = form[key]
        if key == "token":
            v = (v or "").strip()
            if not v:
                continue
        elif key in ("poll_seconds", "stable_seconds"):
            v = _num(key, v)
        elif key == "include_subdirs":
            v = bool(v)
        else:
            v = (v or "").strip()
        new[key] = v
    return config.validate(new)


def run(root):  # pragma: no cover - interactive
    import tkinter as tk
    from tkinter import filedialog, messagebox

    path = Path(root) / "agent.json"
    try:
        raw = config.read_raw(path)
        cur = config.validate(raw)
    except config.ConfigError as exc:
        raw, cur = {}, dict(config.DEFAULTS)
        messagebox.showwarning("GC agent settings", str(exc))

    win = tk.Tk()
    win.title("GC agent settings")
    vars_ = {}
    row = 0

    def add(label, key, browse=None, secret=False):
        nonlocal row
        tk.Label(win, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
        v = tk.StringVar(value="" if secret else str(cur.get(key, "")))
        e = tk.Entry(win, textvariable=v, width=60, show="*" if secret else "")
        e.grid(row=row, column=1, padx=6, pady=3)
        if browse:
            tk.Button(win, text="…", command=lambda: browse(v)).grid(row=row, column=2, padx=4)
        vars_[key] = v
        row += 1

    def pick_dir(v):
        got = filedialog.askdirectory(initialdir=v.get() or None)
        if got:
            v.set(got)

    def pick_file(v):
        got = filedialog.asksaveasfilename(initialfile=v.get() or "distill_results.csv",
                                           confirmoverwrite=False, defaultextension=".csv")
        if got:
            v.set(got)

    add("Hub URL", "hub_url")
    add("Token (blank = keep)", "token", secret=True)
    add("Watch folder", "watch_dir", pick_dir)
    add("Poll seconds", "poll_seconds")
    add("Stable seconds", "stable_seconds")
    add("Results CSV (blank = off)", "results_mirror_path", pick_file)
    sub = tk.BooleanVar(value=bool(cur.get("include_subdirs", True)))
    tk.Checkbutton(win, text="Include subfolders", variable=sub).grid(row=row, column=1, sticky="w")
    row += 1

    def save():
        form = {k: v.get() for k, v in vars_.items()}
        form["include_subdirs"] = sub.get()
        try:
            new = apply_settings(dict(raw, **{k: cur[k] for k in cur if k not in raw}), form)
        except config.ConfigError as exc:
            messagebox.showerror("GC agent settings", str(exc))
            return
        config.save(path, new)
        win.destroy()

    tk.Button(win, text="Save", command=save).grid(row=row, column=1, sticky="e", padx=6, pady=8)
    tk.Button(win, text="Cancel", command=win.destroy).grid(row=row, column=2, padx=6, pady=8)
    win.mainloop()
    return 0
