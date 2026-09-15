from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Callable

from PIL import ImageTk

from mbv.local_monsters import MonsterLibrary, import_frames, read_frame, resolve_library_root, library_root_setting
from mbv.panel_theme import BG, PANEL, FG, MUTED, FONT, FONT_SMALL
from mbv.panel_widgets import RoundedButton
from mbv.template_store import TemplateRoots


class LocalMonsterDialog:
    def __init__(self, parent: tk.Misc, roots: TemplateRoots, category: str,
                 settings: dict, protect_combo: Callable, save_settings: Callable):
        self.roots, self.category = roots, category
        self.save_settings = save_settings
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-monsters")
        self.future = None
        self.after_id = None
        self.closed = False
        self.monsters = []
        self.frames = []
        self.monster = None
        self.photo = None
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("本地怪物资源库")
        self.dialog.configure(bg=BG)
        self.dialog.geometry("800x560")
        self.dialog.minsize(660, 480)
        self.dialog.transient(parent)
        self.dialog.protocol("WM_DELETE_WINDOW", self.close)
        self.dialog.bind("<Destroy>", self.destroyed)
        self.root_path = tk.StringVar(value=str(resolve_library_root(settings.get("root"))))
        self.dataset = tk.StringVar(value="测试服" if settings.get("dataset") == "BetaData" else "正式服")
        self.query = tk.StringVar()
        self.status = tk.StringVar(value=f"添加到当前分类：{category or '未分类'}。保留透明原图，识别时自动按 1.5 倍使用。")
        row = tk.Frame(self.dialog, bg=BG)
        row.pack(fill="x", padx=12, pady=10)
        tk.Label(row, text="资源库", bg=BG, fg=FG, font=FONT).pack(side="left")
        self.path_entry = tk.Entry(row, textvariable=self.root_path, font=FONT)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=8)
        RoundedButton(row, text="选择目录", command=self.browse).pack(side="left")
        search = tk.Frame(self.dialog, bg=BG)
        search.pack(fill="x", padx=12)
        combo = ttk.Combobox(search, values=["正式服", "测试服"], textvariable=self.dataset,
                             state="readonly", width=8, font=FONT)
        combo.pack(side="left")
        protect_combo(combo)
        self.dataset_combo = combo
        entry = tk.Entry(search, textvariable=self.query, font=FONT)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<Return>", lambda _event: self.search())
        RoundedButton(search, text="搜索名字 / ID", command=self.search).pack(side="left")
        body = tk.Frame(self.dialog, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=10)
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=2)
        body.columnconfigure(2, weight=3)
        body.rowconfigure(1, weight=1)
        for col, label in enumerate(("搜索结果（最多 100 条）", "动作帧（可多选）", "预览")):
            tk.Label(body, text=label, bg=BG, fg=FG, font=FONT_SMALL).grid(row=0, column=col, sticky="w")
        self.results = tk.Listbox(body, exportselection=False, font=FONT, width=22)
        self.results.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.frame_list = tk.Listbox(body, exportselection=False, selectmode="extended", font=FONT, width=18)
        self.frame_list.grid(row=1, column=1, sticky="nsew", padx=(0, 6))
        self.preview = tk.Label(body, text="选择怪物和动作帧", bg=PANEL, fg=MUTED, font=FONT)
        self.preview.grid(row=1, column=2, sticky="nsew")
        self.results.bind("<<ListboxSelect>>", self.select_monster)
        self.frame_list.bind("<<ListboxSelect>>", self.preview_frame)
        tk.Label(self.dialog, textvariable=self.status, bg=BG, fg=MUTED, font=FONT_SMALL,
                 wraplength=740, justify="left", anchor="w").pack(fill="x", padx=12)
        actions = tk.Frame(self.dialog, bg=BG)
        actions.pack(fill="x", padx=12, pady=10)
        RoundedButton(actions, text="添加所选帧到素材", command=self.add).pack(side="left")
        RoundedButton(actions, text="关闭", command=self.close).pack(side="right")
        self.root_path.trace_add("write", self.invalidate)
        self.dataset.trace_add("write", self.invalidate)
        self.dialog.grab_set()
        entry.focus_set()

    def invalidate(self, *_args) -> None:
        self.monsters = []
        self.results.delete(0, "end")
        self.clear_frames()

    def clear_frames(self) -> None:
        self.frames = []
        self.monster = None
        self.frame_list.delete(0, "end")
        self.photo = None
        self.preview.configure(image="", text="选择怪物和动作帧")

    def browse(self) -> None:
        if self.future is not None:
            return
        path = filedialog.askdirectory(parent=self.dialog, title="选择包含 catalog.sqlite 的目录")
        if path:
            self.root_path.set(path)

    def submit(self, work: Callable, done: Callable) -> None:
        if self.future is not None or self.closed:
            return
        self.status.set("处理中，请稍候…")
        self.results.configure(state="disabled")
        self.frame_list.configure(state="disabled")
        self.path_entry.configure(state="disabled")
        self.dataset_combo.configure(state="disabled")
        self.future = self.pool.submit(work)
        self.after_id = self.dialog.after(50, lambda: self.poll(done))

    def poll(self, done: Callable) -> None:
        self.after_id = None
        if self.closed:
            return
        if not self.future.done():
            self.after_id = self.dialog.after(50, lambda: self.poll(done))
            return
        future, self.future = self.future, None
        self.results.configure(state="normal")
        self.frame_list.configure(state="normal")
        self.path_entry.configure(state="normal")
        self.dataset_combo.configure(state="readonly")
        try:
            done(future.result())
        except Exception as exc:
            self.status.set(f"操作失败：{exc}")

    def search(self) -> None:
        if self.future is not None:
            return
        self.invalidate()
        root = self.root_path.get()
        dataset = "BetaData" if self.dataset.get() == "测试服" else "Data"
        query = self.query.get().strip()
        if not query:
            self.status.set("请输入怪物中文名字或 ID。")
            return
        library = MonsterLibrary(resolve_library_root(root))

        def done(rows):
            if root != self.root_path.get() or dataset != ("BetaData" if self.dataset.get() == "测试服" else "Data"):
                self.status.set("资源库已改变，请重新搜索。")
                return
            self.library = library
            self.monsters = rows
            for item in rows:
                self.results.insert("end", f"{item.name} · {item.resource_id}")
            self.save_settings({"root": library_root_setting(library.root), "dataset": dataset})
            self.status.set(f"找到 {len(rows)} 条；请选择怪物。" if rows else "没有找到匹配怪物，请换名字或 ID。")

        self.submit(lambda: library.search(query, dataset), done)

    def select_monster(self, _event=None) -> None:
        if self.future is not None:
            return
        selection = self.results.curselection()
        if not selection:
            return
        monster = self.monsters[selection[0]]
        library = self.library
        self.clear_frames()

        def done(frames):
            if monster not in self.monsters:
                return
            self.monster = monster
            self.frames = frames
            for frame in frames:
                self.frame_list.insert("end", frame.node)
            self.status.set(f"{monster.name}：{len(frames)} 帧（最多展示 120）。Ctrl / Shift 多选，单次最多 60 帧。")
            if frames:
                self.frame_list.selection_set(0)
                self.preview_frame()

        self.submit(lambda: library.frames(monster), done)

    def preview_frame(self, _event=None) -> None:
        if self.future is not None:
            return
        selection = self.frame_list.curselection()
        if not selection or not self.frames:
            return
        frame = self.frames[selection[0]]

        def done(image):
            if frame not in self.frames:
                return
            image.thumbnail((260, 300))
            self.photo = ImageTk.PhotoImage(image, master=self.dialog)
            self.preview.configure(image=self.photo, text="")
            self.status.set("可 Ctrl / Shift 多选帧；添加后可在“管理怪物模板”中删除。")

        self.submit(lambda: read_frame(frame.path), done)

    def add(self) -> None:
        if self.future is not None or self.monster is None:
            return
        frames = [self.frames[index] for index in self.frame_list.curselection()]
        monster = self.monster
        self.submit(lambda: import_frames(frames, monster, self.roots, self.category),
                    lambda counts: self.status.set(f"添加 {counts[0]} 帧，跳过重复 {counts[1]} 帧。关闭后刷新识别素材。"))

    def close(self) -> None:
        if self.future is not None:
            self.status.set("正在处理本地资源，请等待完成后关闭。")
            return
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.dialog.grab_release()
        self.dialog.destroy()

    def destroyed(self, event) -> None:
        if event.widget == self.dialog:
            self.closed = True
            if self.after_id is not None:
                self.dialog.after_cancel(self.after_id)
                self.after_id = None
            self.pool.shutdown(wait=False, cancel_futures=True)
