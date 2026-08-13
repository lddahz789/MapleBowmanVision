# AGENTS.md — MapleBowmanVision

给同事和 AI 用的项目说明。玩家操作见 [README.md](README.md)。

改代码前先读本节约束，再打开对应 `mbv/` 文件。不要把逻辑堆回根目录的兼容入口。

## 这是什么

Windows 上的 **经典冒险岛 / 怀旧服** 弓箭手视觉挂机原型。

- 目标进程通常是 `Maplestory_Classic.exe`，窗口标题含 `MapleStory` 或 `冒险岛`。
- 只做：窗口客户区截图 → OpenCV 模板/HSV → `SendInput` 扫描码（后台模式再补 `PostMessage`）。
- 仓库：https://github.com/lddahz789/MapleBowmanVision

这 **不是** Artale 机器人，也不是官方客户端外挂框架。

## 硬约束（不要做）

- 不要读游戏内存、不要抓包、不要注入进程、不要写内核/过滤驱动。
- 不要从 `MapleStoryAutoLevelUp-Optimized`（Artale）搬符文解密、跑图、组队红条主定位。
- 不要用 `PostMessage` 代替扫描码作为唯一后台方案：经典版读 `GetAsyncKeyState` / DirectInput，不吃窗口消息。失焦后若只 PostMessage，角色会停。
- 不要替换 Tk 窗口过程（`GWL_WNDPROC`）来做 `WS_EX_NOACTIVATE`：64 位上会 `OverflowError`。面板只用扩展样式。
- 不要提交个人 `config.json`、`assets/**/*.png`、`logs/`。默认配置是 `config.example.json`。
- 不要 `git commit`，除非用户明确要求。
- **每次提交必须同时更新版本号和更新日志**（见下方「提交」）。
- 用户规则：回复用简体中文。

## 目录

```
maple_bowman.py      # 兼容入口 + 再导出，批处理跑这个；不要往里加业务
game_overlay.py      # 再导出 mbv.overlay
control_panel.py     # 再导出 mbv.panel
Start.bat            # 观察/面板（pythonw，不发键除非之后点启动挂机并过 UAC）
Start-Observer.bat   # 调用 Start.bat，不要复制粘贴第二份启动逻辑
Start-Bot.bat        # UAC 后 --enable-input
Setup.bat / setup_env.py
config.example.json
CHANGELOG.md
mbv/
  paths.py           # ROOT、assets、logs
  config.py          # load/save、SessionLog
  win32.py           # ctypes、完整性级别
  input.py           # Keyboard、VK、SendInput/PostMessage
  window.py          # 找窗口、客户区截图
  vision.py          # ROI、血蓝、模板、玩家融合、攻击框
  calibrate.py       # 校准、冻结帧采集、框选攻击范围
  overlay.py         # HUD、交互框选、F7 显隐
  panel.py           # Tk 控制面板
  bot.py             # BowmanBot 主循环
  cli.py             # argparse、main/run
tests/test_core.py
assets/{monsters,player,player_head,player_title}/
```

改功能时改 `mbv/` 里对应模块。根目录三个 `.py` 只为旧 import / 批处理保留。

## 数据流

1. `cli.main` → 默认 `panel.run_control_panel`；`--overlay-only` 则只开 HUD。
2. 面板线程跑 `BowmanBot.run`：`mss` 截客户区 → 裁 HP/MP/小地图/战斗区。
3. 玩家：姓名板模板优先，失败则头部、称号；多路互相校验，避免锁别人。`PlayerAnchor.box` 顶边是脚底高度，水平中心是角色 X；攻击框锚在检测框几何中心（`raw_box` 优先）。
4. 怪物：战斗区模板匹配。在 `bow_attack_box` 还原出的矩形内选最近目标；框外同高度带则 chase。
5. `input.delivery=background`：始终 `SendInput` 扫描码；失焦时额外 Post/Send 消息，且不要先松开扫描码。`foreground`：仅 SendInput，失焦停机。
6. 挂机需要管理员完整性 ≥ 游戏（UAC）。观察模式 `input_authorized=False` 不发键。

## 关键行为细节

- **校准 UI**：框选和点色只用 `interactive_overlay`，不要加回 `cv2.imshow` / OpenCV 鼠标回调。
- **冻结帧采集**：`capture_frozen_selection` 先截图，把帧传给 `interactive_overlay(..., frozen_frame=)`，裁切同一帧。不要改回「先框再截第二张图」。
- **攻击框**：`bow_attack_box.{forward,back,up,down}` 是相对角色中心、占战斗区宽高的比例，随 `bot.direction` 左右翻转。不要再做成 `max(左,右)` 对称修正。旧配置只有 `bow_attack_range` / `bow_vertical_tolerance` 时，`attack_box_from_config` 会合成对称框。
- **F7**：`BowmanBot.toggle_calibration_overlay`；HUD 用 `overlay_draw_plan`。与采集时 `overlay.hide()` 独立。
- **面板不抢焦点**：`prevent_window_activate` 只设 `WS_EX_NOACTIVATE`。
- **配置**：`load_config` 要求 `version == 1`，并补 `input.delivery`、`behavior.bow_attack_box`。

## 怎么跑

```bat
Setup.bat
Start.bat
```

测试（在仓库根，用项目 venv）：

```bat
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

需要游戏窗口的功能不要在 CI 里真开游戏；现有测试 mock 了 SendInput / 采集。键盘测试若补丁 `user32`，补丁 `mbv.win32.user32` 或 `maple_bowman.user32`（同一 `ctypes.windll.user32` 对象）。采集逻辑测试应 patch `mbv.calibrate.*`，不要以为补丁 `maple_bowman.focus_game_window` 会作用到校准模块内部名字。

## 改哪里

| 需求 | 文件 |
|---|---|
| 按键发不出去 / 后台 | `mbv/input.py` |
| 找不到窗口 / 截图 | `mbv/window.py` |
| 认错人、认错怪、攻击距离 | `mbv/vision.py` |
| 校准、采模板、冻结帧 | `mbv/calibrate.py`、`mbv/overlay.py` |
| HUD 颜色、F7 | `mbv/overlay.py`、`mbv/bot.py` |
| 面板按钮、配置项 | `mbv/panel.py` |
| 走位/攻击/补药状态机 | `mbv/bot.py` |
| 启动参数 | `mbv/cli.py` |

## 提交（必须带版本号和更新日志）

用户明确要求 `git commit` 时，**同一次提交**必须包含下面三项，缺一不可：

1. **版本号**：递增 `mbv/__init__.py` 里的 `__version__`。语义化版本：`x.y.Z` 修复，`x.Y.0` 新功能，`X.0.0` 不兼容变更。
2. **更新日志**：在 `CHANGELOG.md` 顶部增加对应该版本的条目，写用户能感知的变化，日期用提交当天。
3. **和代码一起提交**：不要只提交代码、不改版本和日志；也不要单独先提版本再提功能。

纯文档或注释的小改动也至少升补丁号，并写一行 changelog。提交说明里带上版本号，例如 `feat: 冻结帧采集 (0.2.0)`。

## 语言与风格

- 用户可见字符串用简体中文。
- 保持现有类型标注和 `from __future__ import annotations`。
- 新增测试放 `tests/`，与现有 `unittest` 风格一致。
- 不要为「完全后台、游戏不是前台」去写注入或驱动。
