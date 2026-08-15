# AGENTS.md — MapleBowmanVision

给同事和 AI 用的项目说明。玩家操作见 [README.md](README.md)。

**Cursor 和本机 Codex 共用这份文件。** Codex 不会读 `.cursor/rules/`。新约束只写这里；`.cursor/rules/project.mdc` 只做指针，不要在那边单独加规则。

改代码前先读本节约束，再打开对应 `mbv/` 文件。不要把逻辑堆回根目录的兼容入口。仓库是独立的 `MapleBowmanVision`，不要改旁边的 Artale 项目 `MapleStoryAutoLevelUp-Optimized`。

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
- 本仓库 git 身份是 `lyn <lddahz789@126.com>`（仅 local config）。不要用公司姓名或公司邮箱提交。
- 用户规则：回复用简体中文。

## 目录

```
maple_bowman.py      # 兼容入口 + 再导出，批处理跑这个；不要往里加业务
game_overlay.py      # 再导出 mbv.overlay
control_panel.py     # 再导出 mbv.panel
Start.bat            # 唯一日常入口；UAC 后进入输入待命，不自动启动挂机
Setup.bat / setup_env.py
config.example.json
CHANGELOG.md
STRATEGIES.md       # 职业策略接口、配置与协作扩展规范
mbv/
  paths.py           # ROOT、assets、logs
  template_store.py  # 五类采集图片、怪物分类与可恢复删除
  config.py          # load/save、SessionLog
  performance.py     # FPS、分段耗时、CPU/内存滚动快照
  win32.py           # ctypes、完整性级别
  input.py           # Keyboard、VK、SendInput/PostMessage
  window.py          # 找窗口、客户区截图
  vision.py          # ROI、血蓝、模板、玩家融合、攻击框
  calibrate.py       # 状态区/识别区独立校准、冻结帧采集、框选策略索敌范围
  overlay.py         # HUD、交互框选、F7/面板 Debug 框显隐
  panel.py           # Tk 控制面板
  bot.py             # BowmanBot 主循环
  strategies/<职业>/ # 按职业子包拆分的目标选择与行动决策
  cli.py             # argparse、main/run
tests/test_core.py
assets/{monsters,player,player_head,player_title}/
```

改功能时改 `mbv/` 里对应模块。根目录三个 `.py` 只为旧 import / 批处理保留。

## 数据流

1. `cli.main` → 默认 `panel.run_control_panel`；`--overlay-only` 则只开 HUD。
2. 面板线程跑 `BowmanBot.run`：`mss` 截客户区 → 裁 HP/MP/小地图/战斗区。
3. 玩家：姓名板模板优先，失败则头部、称号；姓名板和头部暂时丢失且称号也无有效位置时，小地图只用于确认本人是否仍在上次可靠视觉位置。`PlayerAnchor.box` 顶边是脚底高度，水平中心是角色 X；攻击框锚在检测框几何中心（`raw_box` 优先）。
4. 怪物：战斗区模板匹配；当前职业策略在同一批检测结果中决定攻击、追踪或按小地图位置返回平台安全点。
5. `input.delivery=background`：始终 `SendInput` 扫描码；失焦时额外 Post/Send 消息，且不要先松开扫描码。`foreground`：仅 SendInput，失焦停机。
6. 挂机需要管理员完整性 ≥ 游戏（UAC）。`Start.bat` 启动时提权并授权输入，但只进入待命；必须再点“启动挂机”或按 F8 才会发键。

## 关键行为细节

- **校准 UI**：框选和点色只用 `interactive_overlay`，不要加回 `cv2.imshow` / OpenCV 鼠标回调。
- **同帧检测复用**：`BowmanBot.run` 每帧建一个 `SceneFeatures`，四路 `find_detections` 共用；模板特征按缩放比例缓存在 `Template` 上。新增检测请传同一个 `SceneFeatures`，不要重复传原始帧。
- **怪物模板前景**：新怪物模板必须在采集时生成并保存 Alpha，使用框选边缘估计背景并保留最大主体连通域；不得再按固定色相排除棕色，也不得在前景异常时静默保存整幅背景。怪物检测同时使用可配置的颜色与轮廓混合权重。
- **稳定战斗锚点**：姓名板/头部/称号的原始框只提供水平中心，高度必须用 `PlayerAnchor.box` 的统一脚底 Y；HUD、选敌和攻击范围采集共用 `last_attack_anchor`。轻量 EMA 只平滑小抖动，大幅移动立即跳转。
- **冻结帧采集**：`capture_frozen_selection` 先截图，把帧传给 `interactive_overlay(..., frozen_frame=)`，裁切同一帧。不要改回「先框再截第二张图」。
- **小地图标记采集**：点选时用 `magnified_roi_preview` 放大小地图，但 HSV 必须通过 `map_magnified_point` 映射回原始冻结帧，再用 `analyze_player_marker_sample` 提取点击附近的连续亮色并做唯一性验证；不得直接从插值预览或固定 `5×5` 背景中位数取色。保存采集位置供首帧消歧。
- **姓名板身份去重与遮挡补位**：名字字形阈值只使用模板 Alpha 有效区；多模板候选先分别保留并计算身份分，再用 `deduplicate_nameplate_detections` 跨模板去重。不得让身份无效但原始相关分较高的模板提前压掉有效模板。姓名板已确认本人后，头部/称号可在预测位置和辅助最大位移约束内连续续跟踪。首次或完全丢失时，普通辅助命中不得认人；只有达到 `player_auxiliary_identity_threshold` 且连续多帧位置一致的辅助模板才可建立受限身份。
- **小地图静止确认**：以最近一次可靠姓名板命中，或姓名板身份已建立后的连续头部命中，固定记录 `(唯一实时小地图标记, 视觉 PlayerAnchor, 小地图尺寸)`；不得与上一帧滚动比较，配置初始标记、称号及仅靠辅助模板建立的启动身份都不能建基准。两路视觉暂失且称号也无有效位置时，当前标记距固定基准不超过 2 像素且不超过 0.2 秒，才可返回 `小地图静止确认` 锚点；该返回不得调用 `track.record()`、刷新身份/视觉时间、速度或基准。移动、缺失、多候选、尺寸变化或超时必须同帧清基准并阻断通用 hold，移动后回原点也不能复活。
- **职业策略**：策略只放 `mbv/strategies/`，必须实现注册元数据、目标选择和动作决策；不得在 `mbv/bot.py` 增加按职业分支。完整规范见 `STRATEGIES.md`。
- **通用索敌区**：所有职业策略共享 `targeting.box.{forward,back,up,down}`，相对稳定战斗锚点并随朝向翻转。不得把索敌框放回某个职业的策略设置；旧策略/`behavior.bow_attack_box` 只用于一次性兼容迁移。
- **独立校准**：战斗识别区与小地图平台安全点相互独立。`capture_platform_center` 必须像玩家标记采集一样放大小地图并映射回原始冻结帧，坐标相对小地图内部归一化且写入 `platform_center_space=minimap`；重采小地图会使玩家标记和平台安全点失效，重采战斗区不得使平台安全点失效。旧战斗画面平台中心不能换算，必须要求重采。
- **Debug 框 / F7**：`BowmanBot.set_calibration_overlay_visible` / `toggle_calibration_overlay`；HUD 用 `overlay_draw_plan`。与采集时 `overlay.hide()` 独立。
- **性能监控**：视觉 worker 每个成功帧只向 `PerformanceMonitor` 提交一次聚合数据；Tk 主线程低频读取冻结快照。整帧耗时不含末尾 FPS 节流，轻量喝药未执行的检测阶段不得补零，禁止从 worker 直接更新 Tk。
- **面板不抢焦点**：`prevent_window_activate` 只设 `WS_EX_NOACTIVATE`。
- **配置持久化**：挂机配置的鼠标控件要自动写入个人 `config.json`；`ControlPanel.quit/_destroy` 退出前再调用统一 `_persist_settings`。新增策略参数不能只改运行内存或 Entry。
- **配置**：`load_config` 要求 `version == 1`，并补输入、识别锚点和 `strategy`；旧弓箭框迁移只能缩放一次。

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
| 怪物分类、所有采集图片增删 | `mbv/template_store.py`、`mbv/panel.py` |
| 校准、采模板、冻结帧 | `mbv/calibrate.py`、`mbv/overlay.py` |
| HUD 颜色、Debug 框、F7 | `mbv/overlay.py`、`mbv/bot.py`、`mbv/panel.py` |
| 面板按钮、配置项 | `mbv/panel.py` |
| FPS、耗时、CPU/内存性能监控 | `mbv/performance.py`、`mbv/bot.py`、`mbv/panel.py` |
| 公共安全、补药、动作执行 | `mbv/bot.py` |
| 职业目标选择、回位、攻击/巡逻决策 | `mbv/strategies/`、`STRATEGIES.md` |
| 启动参数 | `mbv/cli.py` |

## 提交（必须带版本号和更新日志）

用户明确要求 `git commit` 时，**同一次提交**必须包含下面三项，缺一不可：

1. **版本号**：递增 `mbv/__init__.py` 里的 `__version__`。语义化版本：`x.y.Z` 修复，`x.Y.0` 新功能，`X.0.0` 不兼容变更。
2. **更新日志**：在 `CHANGELOG.md` 顶部增加对应该版本的条目，写用户能感知的变化，日期用提交当天。
3. **和代码一起提交**：不要只提交代码、不改版本和日志；也不要单独先提版本再提功能。

纯文档或注释的小改动也至少升补丁号，并写一行 changelog。提交说明里带上版本号，例如 `feat: 冻结帧采集 (0.2.0)`。

已推送到 `origin/main` 的历史不要 rebase / amend / force-push。推送前先 `git fetch` 并 rebase 到 `origin/main`。

## Cursor 与 Codex 并行

两边会改同一份工作区。开始任务前先看 `git status` 和别人未提交的 diff，不要覆盖对方正在写的文件。

- 先 `git pull --rebase`（或 fetch + rebase）再动手，避免和远程分叉。
- 业务只改 `mbv/`。`mbv/panel.py` 直接从 `mbv.bot` / `mbv.calibrate` / `mbv.config` / `mbv.input` 导入，不要再 `import maple_bowman`。
- 同一帧的多路 `find_detections` 必须共用一个 `SceneFeatures`。
- 做完跑：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`
- 未要求不要 commit / push。

## 语言与风格

- 用户可见字符串用简体中文。
- 保持现有类型标注和 `from __future__ import annotations`。
- 新增测试放 `tests/`，与现有 `unittest` 风格一致。
- 不要为「完全后台、游戏不是前台」去写注入或驱动。
