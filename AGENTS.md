# AGENTS.md — MapleBowmanVision

给同事和 AI 用的项目说明。玩家操作见 [README.md](README.md)。

**Cursor 和本机 Codex 共用这份文件。** Codex 不会读 `.cursor/rules/`。新约束只写这里；`.cursor/rules/project.mdc` 只做指针，不要在那边单独加规则。

改代码前先读本节约束，再打开对应 `mbv/` 文件。不要把逻辑堆回根目录的兼容入口。仓库是独立的 `MapleBowmanVision`，不要改旁边的 Artale 项目 `MapleStoryAutoLevelUp-Optimized`。

## 这是什么

Windows 上的 **NewMaple 与经典冒险岛 / 怀旧服** 视觉挂机原型。

- 默认档案目标是 `NewMaple.exe` / `NewMaple`；`classic` 档案仍匹配 `Maplestory_Classic.exe` 与 `MapleStory` / `冒险岛`。
- 两个档案的配置、校准和采集素材必须隔离，不能跨客户端复用。
- 只做：窗口客户区截图 → OpenCV 模板/HSV → `SendInput` 扫描码（后台模式再补 `PostMessage`）。
- 仓库：https://github.com/lddahz789/MapleBowmanVision

这 **不是** Artale 机器人，也不是官方客户端外挂框架。

## 硬约束（不要做）

- 不要读游戏内存、不要抓包、不要注入进程、不要写内核/过滤驱动。
- 不要从 `MapleStoryAutoLevelUp-Optimized`（Artale）搬符文解密、跑图、组队红条主定位。
- 不要用 `PostMessage` 代替扫描码作为唯一后台方案：经典版读 `GetAsyncKeyState` / DirectInput，不吃窗口消息。失焦后若只 PostMessage，角色会停。
- 用户于 2026-09-05 明确要求尝试 PostMessage 后台版本：允许新增独立的 `window_message` 实验模式，保留 classic/旧 `background` 扫描码兼容逻辑。实验模式必须仅向绑定输入窗口投递消息、使用窗口截图，不得静默回退到全局 SendInput；游戏是否接受按键须与 API 投递成功分开报告。
- 用户随后实测确认 NewMaple 可后台挂机；后续修复应保留 `window_message` 路径及其失焦运行能力。
- 用户于 2026-09-05 明确要求新增 `hybrid` 混合后台：允许实际移动临时激活游戏并使用独立 SendInput 通道；技能、转向、Buff、喝药仍严格 PostMessage。此授权只属于显式选择的混合模式，不改变 `window_message` 的禁止全局输入承诺。
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
profiles/newmaple/
  config.example.json # NewMaple 默认配置；个人 config.json 不提交
  assets/             # NewMaple 独立模板与回收站
CHANGELOG.md
STRATEGIES.md       # 职业策略接口、配置与协作扩展规范
mbv/
  paths.py           # ROOT、运行档案、分档 assets、logs
  template_store.py  # 五类采集图片、怪物分类与可恢复删除
  config.py          # load/save、SessionLog
  performance.py     # FPS、分段耗时、CPU/内存滚动快照
  win32.py           # ctypes、完整性级别
  input.py           # Keyboard、VK、SendInput/PostMessage
  background_capture.py # 隔离进程 PrintWindow 截图与超时处理
  background_probe.py # 只读窗口截图与姓名板匹配诊断
  buffs.py           # 三组定时 Buff 的独立调度
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
- **姓名板身份去重与遮挡补位**：名字字形阈值只使用模板 Alpha 有效区；多模板候选先分别保留并计算身份分，再用 `deduplicate_nameplate_detections` 跨模板去重。不得让身份无效但原始相关分较高的模板提前压掉有效模板。姓名板已确认本人且普通 hold 超时后，可在预测位置附近用 `player_nameplate_recovery_threshold` 生成受限候选，但仍须通过本人字形、辅助最大位移和连续多帧确认；首次全图识别不得使用这个较低阈值。头部/称号可在预测位置和辅助最大位移约束内连续续跟踪；在 `player_auxiliary_continuation_seconds` 内也只允许最后可靠位置附近的辅助候选连续多帧恢复，不得任意换锁。首次或无姓名板身份时，普通辅助命中不得认人；只有达到 `player_auxiliary_identity_threshold` 且连续多帧位置一致的辅助模板才可建立受限身份。
- **小地图静止确认**：以最近一次可靠姓名板命中，或姓名板身份已建立后的连续头部命中，固定记录 `(唯一实时小地图标记, 视觉 PlayerAnchor, 小地图尺寸, 背景快照)`；不得与上一帧滚动比较，配置初始标记、称号及仅靠辅助模板建立的启动身份都不能建基准。旧补位仍限制 2 像素／0.2 秒。用户于 2026-09-05 要求小地图主定位优化：超过旧时限必须同时满足固定标记位移 ≤0.5 像素及分散背景纹理稳定校验，才允许延长到 `player_minimap_occlusion_seconds`（默认 3 秒、最大 5 秒）。该返回不得调用 `track.record()`、刷新身份/视觉时间、速度或基准。移动、缺失、多候选、尺寸变化、背景证据不足或超时必须同帧清基准并阻断通用 hold，移动后回原点也不能复活。
- **小地图主导航**：本人姓名板身份及真实视觉／唯一标记配对建立后，视觉遮挡时允许 `minimap_only` 决策，只按地图回安全点／安全区，到位等待。导航从最近真实配对计时（默认 10 秒，上限 30 秒），过期停止动作，不盲目找人；目标、屏幕锚点必须清空，禁止攻击、追怪、拾取和新短步。依然检查窗口、小地图唯一性和移动进展；不能把地图坐标直接换成屏幕坐标。由现有 `player_minimap_assist_enabled` 总开关控制。
- **职业策略**：策略只放 `mbv/strategies/`，必须实现注册元数据、目标选择和动作决策；不得在 `mbv/bot.py` 增加按职业分支。完整规范见 `STRATEGIES.md`。
- **弓箭手动态技能分流**：AOE、单体、近身技能键属于 `strategy.options.bowman_dynamic`，由策略字段的 `capture_key` 生成采集控件。近身判定与聚怪判定使用怪物检测框尺寸归一化，不能写死屏幕像素；近身必须使用进入/退出双阈值，策略只返回技能键，公共执行器负责实际发键。
- **通用索敌区与策略多区域**：所有职业策略共享 `targeting.box.{forward,back,up,down}`，相对稳定战斗锚点并随朝向翻转。标飞 `target_regions` 同样跟随稳定战斗锚点，但保持屏幕方向、不随面向翻转；多个区域只做候选并集与优先级。旧策略/`behavior.bow_attack_box` 只用于一次性兼容迁移。
- **只转向动作**：策略需要预先面向目标时返回 `StrategyDecision(action="face")`，由公共执行器释放左右移动键后仅短按一次目标方向；策略不得用 `move` 模拟转向，也不得直接发键。
- **姓名板丢失恢复**：超过 `vision.player_hold_seconds` 且当前真实视觉／有效小地图补位和主导航均不可用时，才由 `BowmanBot` 按 `behavior.player_lost_move_seconds` 交替左右位移；不能仅因姓名板丢失覆盖仍有效的头部／称号定位。恢复定位、喝药、暂停或改配置时必须释放恢复按键并重置状态；主导航超时等待，不回退盲目移动。
- **独立校准**：战斗识别区与小地图平台安全点相互独立。`capture_platform_center` 必须像玩家标记采集一样放大小地图并映射回原始冻结帧，坐标相对小地图内部归一化且写入 `platform_center_space=minimap`；重采小地图会使玩家标记和平台安全点失效，重采战斗区不得使平台安全点失效。旧战斗画面平台中心不能换算，必须要求重采。
- **标飞安全输出区**：必须在放大的小地图上框选，保存 `space=minimap` 和相对小地图坐标；运行判断只使用小地图玩家标记。重采小地图时失效，重采战斗识别区时保留，旧战斗画面安全区不得迁移。
- **Debug 框 / F7**：默认显示全部框；F7 只控制总显隐，各项通过 `calibration_overlay_hidden_items` 独立排除且切换总开关时保留。HUD 用 `overlay_draw_plan`，与采集时 `overlay.hide()` 独立。
- **性能监控**：视觉 worker 每个成功帧只向 `PerformanceMonitor` 提交一次聚合数据；Tk 主线程低频读取冻结快照。整帧耗时不含末尾 FPS 节流，轻量喝药未执行的检测阶段不得补零，禁止从 worker 直接更新 Tk。
- **面板不抢焦点**：`prevent_window_activate` 只设 `WS_EX_NOACTIVATE`。
- **配置持久化**：挂机配置的鼠标控件要自动写入当前档案的个人 `config.json`；`ControlPanel.quit/_destroy` 退出前再调用统一 `_persist_settings`。新增策略参数不能只改运行内存或 Entry。
- **全局喝药与定时 Buff**：喝药会话开关关闭时，挂机和暂停状态都不得发送药键。三组 Buff 各有持久化独立开关，关闭时保留按键、间隔和上次发送时间；空按键或零间隔同样表示禁用。Buff 只能在挂机运行时调度；同到期时每帧最多发送一个。F8/失焦暂停、单项开关与配置刷新必须保留每项上次发送时间，避免尚在游戏冷却期时误发并重新计时。每次 Buff 先保留停攻准备时间，再使用较长按键和施法保护窗口；窗口内不得恢复攻击或发送下一 Buff。
- **下拉框滚轮保护**：所有 `ttk.Combobox` 都必须绑定 `disable_combobox_mousewheel`，防止滚动控制面板时误改选项。
- **运行档案隔离**：`Start.bat` 默认 `newmaple`，`Start.bat classic` 才使用原怀旧服。所有模板采集、分类管理、删除回收和运行时加载都必须通过当前配置解析素材根目录，不能重新写死根目录 `assets/`。
- **独立后台实验**：`window_message` 同时启用纯窗口按键和窗口截图；启动不切前台、不置顶，失焦时隐藏 HUD。PrintWindow 放在可终止的辅助进程，超时、空白或失败暂停挂机；最小化不支持，非空缓存画面仍可能陈旧，不能把截图成功当作游戏持续渲染或施法成功的证明。旧 `postmessage/window` 别名保持兼容含义。重复发键和抬键必须持同一把锁，所有输入模式暂停都要实际释放按住的键。
- **配置**：`load_config` 要求 `version == 1`，并补输入、识别锚点和 `strategy`；旧弓箭框迁移只能缩放一次。
- **连续帧确认**：每次 `_track_player` 开始调用 `track.begin_frame()`；同帧多次扫描不得增加确认帧数。空帧、歧义帧通过 `mark_miss()` 清理本帧未更新的待确认候选；暂停或真实视觉记录也要清除旧确认链。
- **Buff 准备与公平调度**：0.45 秒准备期跨帧推进，不阻塞视觉循环；暂停、退出、关闭单项、换按键或配置刷新不得让旧准备任务发键。发键前复核窗口，前台模式才要求焦点；已完成的 0.18 秒按键保持与 1.2 秒保护窗口保留。到期项目按最早到期时间调度，避免短间隔槽位饿死后续槽位；准备取消不得消耗冷却。
- **配置落盘保护**：`save_config` 先序列化，再同目录写临时文件、flush/fsync 和原子替换；上一份可解析配置写入 `config.json.bak`，备份和临时文件禁止提交。原子落盘不等于解决多实例并发覆盖。
- **线程异常提示**：worker 只记录异常，由 Tk 主线程 `_tick` 消费并显示；不要在 except 中注册延后捕获异常变量的 lambda，也不要从 worker 操作 Tk。
- **移动与位移验证**：原地攻击每帧优先判断小地图安全点，不限周期动作之后。纯窗口移动走 `Keyboard.movement_down` 的抬按脉冲，不能改变普通攻击/转向或回退全局输入；重复投递错误必须交回主循环处理。短步跨帧准备/按住/验证，输入层独立限时抬键；仅唯一实时标记向正确方向移动至少 0.5 像素才能记完成，三轮失败暂停。普通回位记录有向进展，连续 2 秒无进展重试、4 秒暂停；暂停、配置、药/Buff/识别中断必须释放移动键，旧准备阶段不得直接算完成。
- **混合后台移动**：前台授权与扫描码保持放 `mbv/hybrid_movement.py`，不得在策略中切窗。启动不置顶，后台仍窗口截图；发移动键前检查游戏 HWND/PID/前台，首次激活后等新视觉帧。键鼠空闲 0.7 秒才尝试，移动会话限 4 秒、按键无续期限 0.5 秒、行动心跳限 0.8 秒；独立 watchdog 约 20 毫秒检查，暂停/退出信号也必须取消。2026-09-05 恢复旧窗口失败导致崩溃后，用户授权切到前台不再返回旧窗口：移动结束/中断只抬键，不恢复原窗口；用户切窗或操作后不抢回焦点。输入异常应在公共行动边界暂停并提示，不让视觉线程退出；故障期间不继续暂停喝药，须显式启动重新绑定，抬键失败不能清除错误继续移动。焦点操作仍放可终止子进程，不允许无界阻塞。输入竞态仍需用户端实测，不能承诺完全不打断。技能通道不得因为移动授权而临时整体切到 SendInput。

## 怎么跑

- **原地路线拾取**：仅同平台小地图点位；采样与安全点一致，重采小地图失效、重采战斗区保留。去程遇有效怪物先攻击、返程不攻击；关闭开关改为回位，暂停立即释放。路线状态必须由运行层按会话保存，不能写入全局策略实例。确认离开并回到安全点才重置周期右移；仅发送过按键不代表拾取成功。移动叠加拾取仍走普通按键通道，混合后台路线主动分段不得清空无进展监控或取消原 watchdog。定位丢失时导航任务不盲走找人。
- **拾取中断宽限与计时**：短暂定位丢失默认先停 1 秒等恢复（可调 0.2–3），超过才转返程；无可信位置不能回位，不能延长小地图导航总期限。公共层记录跨上游门禁的连续丢失时长，恢复首帧也要交给策略；不能只统计进入策略的帧。去程上限与到点有效拾取累计分离，清怪/药/Buff/识别中断不占用有效拾取时长，不清零已累计值。到点阶段另有总上限防止无限清怪，返程须记录原因；保留用户已有间隔、停留和去程上限数值。

```bat
Setup.bat
Start.bat
Start.bat classic
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
