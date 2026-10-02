"""Read-only peer UI fixture. All model numbers here are illustrative test data."""
import sys
from pathlib import Path
sys.path[:0] = ["C:/dev/ducketz", "C:/dev/ducketz/tests"]
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
from test_hyper_workspace import _history_row
from visual_hyper_workspace_fixture import FixtureService, _snapshot, _write_window_png
from app.ui.hyper_workspace import HyperWorkspace
from app.ui.ducket_bucket import DucketBucketApp

output = Path(__file__).parent
for width, height in ((1706, 1000), (880, 760)):
    root = tk.Tk()
    root.title("ILLUSTRATIVE MODEL HISTORY FIXTURE - read-only")
    root.geometry(f"{width}x{height}+0+0")
    DucketBucketApp._apply_theme(SimpleNamespace(root=root))
    parent = ttk.Frame(root)
    parent.pack(fill="both", expand=True)
    snapshot = _snapshot()
    snapshot.policy = {"entry_band": .075}
    families = ["ensemble", "extra_trees", "hist_gradient_boosting", "logistic", "mlp", "random_forest"]
    snapshot.model_history = [_history_row(coin=coin, family=family, weight=None if family == "ensemble" else .2)
                              for coin in ("BTC", "ETH", "HYPE", "ZEC") for family in families]
    view = HyperWorkspace(root, parent, service=FixtureService(snapshot), auto_load=False)
    view.show_snapshot(snapshot)
    root.update()
    view.canvas.yview_moveto(1)
    view.model_history_detail.yview_moveto(1)
    root.update()
    before = view.model_history_detail.yview()
    view.show_snapshot(snapshot)
    root.update()
    after = view.model_history_detail.yview()
    print({"size": [width,height], "detail_scroll_before_refresh": before, "after": after,
           "column_widths": {column: view.model_history_tree.column(column,"width")
                              for column in view.model_history_tree["columns"]}})
    root.lift()
    root.attributes("-topmost", True)
    root.update()
    _write_window_png(root.winfo_id(), output / f"peer-populated-{width}.png")
    root.destroy()
