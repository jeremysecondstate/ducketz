"""Render retained model history without reconnecting any archived experiment."""
import json
import sys
from pathlib import Path
sys.path[:0] = ["C:/dev/ducketz", "C:/dev/ducketz/tests"]
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
from visual_hyper_workspace_fixture import FixtureService, _write_window_png
from app.services.hyperliquid_paper_view import PaperViewSnapshot
from app.ui.hyper_workspace import HyperWorkspace
from app.ui.ducket_bucket import DucketBucketApp

output = Path(__file__).parent
retained = json.loads((output / "retained-model-history.json").read_text(encoding="utf-8"))
for width, height in ((1706, 1000), (880, 760)):
    root = tk.Tk()
    root.title("RETAINED ROUND 12 MODEL HISTORY - read-only archive preview")
    root.geometry(f"{width}x{height}+0+0")
    DucketBucketApp._apply_theme(SimpleNamespace(root=root))
    parent = ttk.Frame(root)
    parent.pack(fill="both", expand=True)
    snapshot = PaperViewSnapshot(observed_at_utc=retained["observed_at_utc"],
        market_recipe=retained["market_recipe"], policy=retained["policy"], model_history=retained["model_history"])
    view = HyperWorkspace(root, parent, service=FixtureService(snapshot), auto_load=False)
    view.show_snapshot(snapshot)
    view.opening_panel.pack_forget()
    view.metrics.pack_forget()
    view.workspace.pack_forget()
    view.ops.pack_forget()
    view.mode_status.configure(text="Retained round 12 · read-only archive preview")
    view.alert.configure(text="Retained native model forecasts and exact reports, observed 02:07:33 PT. Portfolio and runtime views omitted.")
    root.update()
    view.canvas.yview_moveto(0)
    root.lift()
    root.attributes("-topmost", True)
    root.update()
    _write_window_png(root.winfo_id(), output / f"peer-retained-{width}.png")
    print(json.dumps({"size": [width,height], "rows": len(view.model_history_tree.get_children()),
        "summary": view.model_history_summary.cget("text"),
        "selected_details": view.model_history_detail.get("1.0", "end")}, ensure_ascii=True))
    root.destroy()
