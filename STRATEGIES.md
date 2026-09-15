# 职业打怪策略扩展规范

职业策略负责“如何选目标、何时回位、攻击/追踪/巡逻如何排序”。窗口截图、玩家与怪物识别、输入权限、补药和退出等安全逻辑仍由公共运行层负责。

## 当前策略：弓箭手动态

- 职业：弓箭手
- 标识：`bowman_dynamic`
- 描述：根据小地图玩家标记的水平与垂直位置优先回到平台安全点附近；掉到下层时跳回，位于上层时下跳。范围内按玩家攻击锚点到怪物中心的二维距离优先攻击最近单位；怪物贴身时用近身技能，当前目标附近聚集到指定数量时用 AOE，否则用单体技能。
- 依赖采集：小地图与玩家标记、小地图平台安全点、战斗识别区、玩家定位模板、怪物模板。
- 决策优先级：输入/窗口安全 → HP/MP 补药 → 返回小地图平台安全点 → 近身技能 → 聚怪 AOE → 单体技能 → 同层追踪 → 拾取/巡逻。
- 索敌框：使用公共 `targeting.box`，不属于弓箭手策略；框随角色朝向左右翻转。
- `aoe_skill_key` / `single_skill_key` / `melee_skill_key`：面板直接采集三种技能键；AOE、近身留空表示不用，单体留空则回退到公共 `keys.attack`。
- `aoe_min_monsters`：当前目标及其附近怪物达到此数量才使用 AOE，默认 `2`。
- `aoe_cluster_distance_multiplier`：两怪矩形边缘距离除以两者平均可见体型，默认不超过 `0.75` 视为聚集。
- `melee_enter_distance_multiplier`：玩家水平锚点到怪物边缘的间隙除以怪物宽度，默认不超过 `0.35` 进入近身模式。
- `melee_exit_distance_multiplier`：已进入近身模式后，距离倍率超过此值才恢复普通技能，默认 `0.9`。
- `platform_center_tolerance`：水平安全半径，是小地图宽度的比例；超出后忽略目标并优先左右回位。
- `platform_center_vertical_tolerance`：垂直安全半径，是小地图高度的比例；超出后根据上下层关系跳回或下跳。
- `platform_return_jump_interval_seconds`：连续回位跳跃之间的最短间隔，防止按键过密。

## 当前策略：原地攻击

- 职业：通用
- 标识：`stationary_attack`
- 描述：使用左右两个面向索敌区的并集选择最近同层怪物，不受当前朝向限制；仅在目标换边时短按方向键并原地攻击。每隔 45 秒向右短走一步，随后按小地图位置优先回到平台安全点，回位后继续输出。
- 依赖采集：小地图与玩家标记、小地图平台安全点、战斗识别区、玩家定位模板、怪物模板。
- 决策优先级：输入/窗口安全 → HP/MP 补药与 Buff → 平时及周期短步后的平台安全点回位 → 定时向右短步 → 区内目标近身技能 → 普通攻击 → 原地等待。只要实时位置超出容差，即使没有周期短步也优先回位。
- `melee_skill_key`：原地策略独立采集的近身技能键；留空关闭，不继承弓箭手动态的按键。对当前最近同层目标判定近身，远离后恢复公共 `keys.attack`，不追怪、不连续按方向键。
- `melee_enter_distance_multiplier` / `melee_exit_distance_multiplier`：默认 `0.35` / `0.9`，支持直接输入数字。稳定玩家水平锚点到目标框边缘的间隙除以怪物宽度，小于等于进入值时使用近身技能；已进入时持续使用，直到超过退出值。退出值不能小于进入值；逻辑与弓箭手动态共用 `mbv/strategies/melee.py`，配置分别保存。
- 索敌框：使用公共 `targeting.box`，框随角色朝向左右翻转。
- `periodic_step_interval_seconds`：两次向右短步之间的间隔，默认 45 秒。
- `periodic_step_seconds`：向右短步首轮的按键时长，默认 0.12 秒。先跨帧停攻 0.6 秒，输入层独立计时抬键（调度精度约 50 毫秒），抬键后等待 0.25 秒再验证。无位移时最多三轮，后续时长分别为首轮的 2 倍和 3 倍、单轮上限 0.5 秒。
- `platform_center_tolerance`：平时及周期短步后返回安全点时的小地图水平容差。
- `platform_center_vertical_tolerance`：平时及周期短步后返回安全点时的小地图垂直容差。
- 短步必须在唯一实时小地图标记中观察到正确方向至少 0.5 像素位移，才更新周期计时并进入待回位；未移动不能直接判为“回位完成”。反向位移或三轮无位移会暂停，不能把消息发送成功等同于游戏移动成功。
- 公共 `move` 执行器在最近攻击后保留 0.6 秒停攻时间；同方向连续 2 秒无进展重新抬按一次，4 秒仍无进展则暂停。进展依据唯一实时小地图位置，记录 `movement_start/progress/retry/failed`。
- `window_message` 模式的移动使用约 0.10 秒按下、0.05 秒抬起的脉冲。攻击/转向短按不改变，classic/旧后台模式仍使用原扫描码；不伪造焦点、不向其它窗口发送全局按键。此方式仍需客户端实测，游戏不接受窗口移动消息时会安全失败。
- `hybrid` 模式的实际移动与转向由公共层获取限时前台授权，使用独立扫描码通道；普通技能与补给由 Keyboard 按焦点路由：游戏前台 SendInput、后台 PostMessage；普通技能不申请激活租约。转向不作为新的巡逻动作，不重置周期右移计时；激活后等新帧、短按后抬键并等待，再发送技能。普通攻击、只转向、跳跃攻击共用此流程，策略不得直接发键。原地短步验证与返回安全点尽量共用一次焦点授权，遇药/Buff、定位安全门或停止则释放移动，按用户授权不再恢复原窗口。移动动作仍必须通过小地图位移验证。
- `platform_return_jump_interval_seconds`：回安全点需要跨层时，连续跳跃之间的最短间隔。

### 原地定时路线拾取

- 清怪侧保存在会话 `combat_side`：从本帧双向索敌区内的 `eligible_detections` 优先清当前侧，连续缺失 0.4 秒才换侧；不保存旧目标框、不宣称死亡，恢复/中断重置确认，清怪结束解锁。攻击决策携带 `face_tap_seconds=0.08`，仅 foreground 普通攻击消费，所有必要转向均稳定准备 120 ms、点按至少 80 ms（保留更长个人配置）、抬键等待 120 ms，同侧不重转；其它模式时序不变。返程清怪时短暂仅地图定位也按原视觉宽限停走，安全返程不延迟。

- `route_pickup_enabled` 默认关闭，热切换并保存；`route_pickup_interval_seconds` 默认 180 秒（5–86400），`route_pickup_dwell_seconds` 默认 1.5 秒（0.3–10），`route_pickup_timeout_seconds` 默认 90 秒（5–600），`route_pickup_visual_grace_seconds` 默认 1 秒（0.2–3），`route_pickup_collect_timeout_seconds` 默认 60 秒（10–600），均支持直接输入。原有去程上限和停留数值保留，使用公共 `keys.pickup`。
- `capture_fields` 声明 `stationary_pickup_point`，类型为 `point`、坐标空间为 `minimap`。复用安全点的冻结放大小地图采样流程，保存于当前档案的 `recognition`，带独立 `_space/_captured` 标记；启用时必须采样。目标必须与安全点同层且在回位半径之外，路径需可步行，不提供障碍寻路。
- 决策顺序：公共安全/药/Buff → 活跃路线（去程、到点和正常返程均先原地清理区内同层目标，再移动/拾取）→ 原有安全点回位 → 到期路线 → 周期右移 → 原地攻击。清怪期间不移动、不拾取、不追怪，连续无有效目标 0.4 秒后才继续；目标重现或上游中断会重新确认。新路线只能从安全点发起，不打断已执行的周期短步及其回位。
- 会话阶段为 `idle → outbound → collect → returning → idle`。清怪保留原阶段，结束后继续下一步；正常返程到安全点时也先完成清怪和无目标确认，再记录路线完成。短暂定位丢失在原阶段停止所有路线输入，默认等待 1 秒，恢复继续；超出宽限、关闭开关、点位失效或掉层改为安全返程，不被清怪拦截。仅有小地图时不攻击、不发拾取键，位置完全不可用或导航到期仍停止。
- `outbound_started_at` 只限制当前到点前路段（含清怪/等待），到点立即切换独立计时；被击退离开目标点需回到目标点时重新计该路段。`collection_started_at` 为首次到点时刻，到点阶段默认最多 60 秒（含清怪/中断），不反复刷新，以免无限打怪。`dwell_elapsed` 累计有效拾取时间，清怪、短暂丢失及上游药/Buff 只暂停累计，不清零；相邻有效帧间隔大于 0.5 秒或缺少近期拾取发键记录不计入。有效拾取达到目标后返程。
- 返程状态写入 `return_reason`：`collected`、`outbound_timeout`、`collection_timeout`、`localization_timeout`、`disabled`、`point_invalid`、`off_platform`；到家保留原因用于诊断。阶段/返程原因变化立即记录，其余进度最多每秒记录一次。
- 运行层持有 `runtime_state`，策略注册实例无可变会话状态；完整到家才记录下一次拾取时间。观察到离开安全范围后完成回位，发 `reset_periodic_step` 重启右移倒计时；没有实际离开（例如一直原地清怪后超时）不能冒充位移而推迟右移。暂停/重新启动清空路线；药/Buff 跨帧暂停保留路线。
- 路线拾取保留决策的 `pickup_interval_seconds` 作为兼容标识（非 None 表示该阶段拾取），不再用其值限频点按。移动执行器先检查实际行动状态，再持续保持普通拾取键，并约每 0.1 秒补自动重复 keydown 而非抬按；普通方向/技能输入不增加此重复。每帧续期，最多 0.8 秒无续期后独立抬键。遇怪、药/Buff、定位丢失、准备/分段、到家和暂停须松开；到点连续帧不得每帧结束移动租约而抬掉拾取键，不为拾取抬方向键。混合模式拾取在游戏前台用扫描码、后台用窗口消息，自有技能键纳入移动保护排除集合，不因拾取误判用户输入；`cooperative_movement` 请求约 2.5 秒一段的前台移动，分段抬键但不恢复原窗口，不重置有向位移无进展监控，不取消原 watchdog 保护。

## 当前策略：箭雨

- `periodic_step_enabled`：定时向右小步独立开关，默认 true，热更新及持久化，不暂停、不重置计时或路线。关闭只阻止新 `step`；公共层已开始的小步继续有限时抬键、验证和回位收尾，追怪/拾取/普通回位仍工作。再次开启沿用原到期时间。原地策略默认始终允许周期小步，不读取箭雨开关。

- 职业：弓箭手；标识：`bowman_arrow_rain`。独立继承原地攻击，不改变 `stationary_attack`；参数保存在自己的 `strategy.options` 节点，不自动复制或覆盖原策略的个人参数。
- `attack_range_px`：左右各自的水平射程，默认 300 px，范围 10–2000，可直接输入；以原始战斗截图中的稳定角色锚点到怪物框中心计算，不受检测缩放、画面宽度及面向影响。公共 `targeting.box` 的前/后/上/下四边定义有效索敌区，按当前面向原样换算，不再被射程覆盖，也不取左右最大值；安全点和拾取点沿用当前档案采样。
- 只筛选战斗识别区与有效索敌区交集内的真实怪物，优先最近射程内目标；没有射程内目标才靠近区内最近远处目标，区外目标不追。使用公共带小地图进展验证的 `move`，不是无反馈 `chase`；入范围后转为 `cast`，先释放移动和拾取。旧怪物框不触发追踪，定位丢失不盲走（`allow_player_lost_recovery=false`）。不自动跨层、绕障或跳跃追怪。
- 仅已在连续原地 `cast` 时允许 0.25 秒漏检续攻：`TargetSelection.continuity_state` 保存固定真实目标、时间、玩家框、唯一标记及画面/小地图尺寸，由 Bot 会话持有；本帧真实视觉仍命中、相对固定玩家框变化 ≤3 px、标记变化每轴 ≤0.5 px 且旧目标仍在当前搜索/施法范围时才可沿用。续用不刷新时间/位置，移动、非 cast 动作、补给/上游中断、暂停/配置、明确过滤均断链；首次施法和追怪仍要求新目标。其它策略忽略此可选上下文，龙咆哮仍严格要求本帧真实计数。
- `TargetSelection.attack_area_override` 传递本帧通用索敌参数快照，HUD 黄色“有效索敌区”与候选过滤一致；`skill_area_override` / `skill_area_label` 单独传递按射程换算的前后对称粉色“箭雨施法范围”，下沿标签避免与索敌区重叠。无目标时两框仍可更新；共同服从索敌区 Debug 开关，缺失屏幕定位时均不画旧框。原地、动态及其它策略不设置技能范围字段，沿用原绘制与选敌逻辑。
- 普通偏离及周期短步后的回位：射程内目标先停走施法，只有远处目标则先靠近；最后一个有效目标消失后连续确认 0.4 秒，再回位。目标重现立即中断回位清怪/靠近；药/Buff或定位中断清空无目标计时。视觉目标消失不等于确认死亡，持续刷怪可能推迟回位。
- 已在安全点时仍保留周期右移优先级，防止连续攻击使防原地限制的小步永久饿死。短步完成后的回位才执行先清怪规则；短步本身沿用原有限时执行及位移验证。
- 普通攻击显式使用 `StrategyActionContext.default_attack_key`（公共 `keys.attack`，应绑定箭雨），近身攻击仍使用本策略独立近身键。两者均返回 `cast`，不设置面向、不走 `attack/face`，按公共攻击间隔限频；包括拾取去程、到点及正常返程清怪。无方向技能不需要路线清怪侧锁或换边等待。
- 活跃拾取路线沿用原阶段/计时/长按拾取/实际回位确认；禁用拾取、点位失效、掉层、定位异常等安全返程仍优先，不能被怪物阻塞。公共窗口、暂停、补给、唯一标记与小地图禁攻击门禁保持不变。混合后台不会为箭雨攻击申请前台，实际导航仍按原移动规则。

## 当前策略：标飞安全输出

- 职业：飞侠·标飞
- 标识：`throwing_star_safe`
- 描述：安全输出区在放大的小地图上框选，并与玩家标记使用同一坐标；启用后，玩家标记低于安全区时优先朝安全区水平中心移动并向上跳。标飞可维护多个跟随角色但不随面向翻转的独立索敌区，按区域并集过滤怪物；目标进入可调近距离时可令每次攻击先跳跃。
- 依赖采集：战斗识别区、玩家定位模板、怪物模板和小地图玩家标记；`use_target_regions=true` 时至少要求一个已启用的标飞索敌区，`use_safe_output_area=true` 时要求重新框选小地图安全输出区。
- 决策优先级：输入/窗口安全 → HP/MP 补药 → 可选安全区回位 → 近目标每次跳跃攻击 → 近身重叠跳跃攻击 → 普通向下攻击 → 可选安全区巡逻。
- 上方边界：当前不处理玩家位于安全区上方的下跳回位；发现位于上方时原地停住并等待人工处理。
- 移动限制：不追怪、不自动拾取；巡逻默认关闭，启用后无有效目标时只在安全输出区内左右巡逻，到边界前自动折返。
- `use_target_regions`：启用策略内的多索敌区；区域相对稳定角色锚点移动，但不随角色面向翻转，重采战斗识别区后自动失效。
- `use_common_target_box`：是否在多索敌区过滤后继续套用公共角色相对索敌框，默认关闭。
- `only_targets_below_player`：是否只保留角色脚底下方的目标。
- `auto_face_target`：索敌成功后若目标在另一侧，先单独短按一次方向键改变面向，下一帧再攻击；不会进入移动或追踪。
- `target_face_tap_seconds`：标飞自动转向时方向键的短按时长，默认 `0.025` 秒。
- `target_priority_mode`：支持“区域优先级后水平距离”“水平距离最近”“识别分最高”。区域重叠时取最小优先级数值。
- `throwing_star_safe_output_area`：保存 `space=minimap` 和相对小地图的 `x/y/w/h`；重采战斗识别区时保留，重采小地图时失效。旧战斗画面坐标不迁移。
- `jump_interval_seconds`：连续回位跳跃之间的最短间隔。
- `minimum_target_vertical_gap`：怪物中心必须低于角色脚底的最小归一化高度差，同层目标会被过滤。
- `use_near_target_jump_attack`：目标进入近距离时，是否把每次攻击改为跳跃攻击，默认开启。
- `near_target_jump_attack_distance_px`：目标中心到稳定角色锚点的最大水平像素距离，默认 `120`；等于边界时触发。
- `use_close_jump_attack`：是否启用近身重叠跳跃攻击，默认开启。
- `close_overlap_threshold`：水平重叠宽度除以角色与怪物较小宽度，默认阈值 `0.2`，等于阈值时触发。
- `jump_attack_cooldown_seconds`：两次跳跃攻击之间的最短间隔。

## 当前策略：龙咆哮·定点

- 职业：战士·龙骑士；标识：`dragon_roar`。不追怪、不转向、不巡逻、不定时短步、不拾取。
- 定点沿用公共 `platform_center`（`_space=minimap`、`_captured=true`），由 `required_recognition_data` 声明必需；没有专属定点按钮，旧 `dragon_roar_point` 不使用或回退。沿用公共小地图、玩家标记、战斗区校准和怪物模板，不需要姓名牌/头部模板。
- `localization_mode="minimap"` 声明独立小地图定位：不建立屏幕身份、不运行玩家模板匹配、不伪造屏幕锚点。怪物中心位于固定 `regions.combat` 即参与计数，不套用公共索敌框、同层或旧 `attack_regions`；旧区域配置保留但不使用。模板仍使用公共跨模板 NMS 和过滤项，不能保证遮挡怪物逐只可见。
- 数量只用本帧真实检测，`detections_fresh=false` 的短暂保留框一律不参与计数。`monster_count_threshold=2` 表示严格大于 2（至少 3 只）才施放；范围 0–23，受公共单帧最多 24 个检测结果限制。
- `skill_key` 必须独立采集，空值不施放、不回退普通攻击；面板施放间隔用毫秒（默认 1000，范围 100–10000），配置仍保存 `cast_interval_seconds=1.0`（0.1–10 秒），由公共 `cast` 执行器按实际成功发送按键的时间限频，不宣称游戏实际施法成功。
- 优先级：公共输入/窗口安全 → 补药/Buff → 唯一实时标记、点位和战斗区有效性 → 定点回位 → 本帧范围计数 → 无方向施法/原地等待。标记缺失或歧义立即停止战斗/移动，不等待姓名牌恢复。
- `return_tolerance_x=0.015`、`return_tolerance_y=0.06` 分别以小地图宽、高的比例表示允许偏离量；超过任一容差进入回位，回到两轴容差的六成以内才结束，避免边界来回抖动。
- 横向回位走公共 `move` 位移验证；水平接近后按小地图上下层关系使用 `jump` / `down_jump`，`return_jump_interval_seconds=0.45`（0.1–2 秒）。只支持可直接走回/跳回的位置，不支持绕障碍、爬绳、自动换图。普通移动无进展仍按公共 2 秒重试/4 秒暂停保护。
- `return_timeout_seconds=15`（3–60 秒）从本次开始回位计时，包含上游中断等待。超时锁存 `blocked` 并停止移动/施法，检查路径后需暂停再启动。独立地图定位不适用其他策略的视觉配对过期期限，仍要求每帧唯一实时标记及公共移动进展保护。
- 会话 `runtime_state` 保存 `phase`、实时 `monster_count`、回位起始时间；`navigation_active=true` 且元数据 `allow_player_lost_recovery=false`，包括首次策略决策前也禁止完全丢失定位时左右盲走。暂停重启清会话，注册实例无会话状态。
- 重采小地图使定点失效；重采战斗区直接更新固定计数范围并保留定点。两个客户端档案独立，不修改原已选策略或个人校准。

## 新增策略必须遵循

1. 按职业在 `mbv/strategies/<profession>/` 建子包，再为每个策略新建独立模块；目录、文件名和 `key` 使用稳定的 ASCII `snake_case`，不要把职业分支写回 `mbv/bot.py`。
2. 实现 `CombatStrategy` 协议，至少提供：
   - `key`：持久化配置标识，发布后不得随意改名。
   - `display_name`：面板下拉显示的简体中文名称。
   - `profession`：职业分类。
   - `description`：一到三句话说明执行逻辑；用户切换下拉选项时会直接看到。
   - `required_recognition_data`：依赖的公共采集数据键。
   - `default_settings` 和 `setting_fields`：策略专属默认值及面板字段；数字字段同时声明鼠标微调步长和上下限，技能键字段声明 `capture_key=true`。`display_multiplier` 只换算面板单位（默认 1）；例如龙咆哮施放间隔用 1000 显示为毫秒，配置/运行值、步长与边界仍以秒声明，面板负责读写和微调换算。
   - `select_targets(context)`：使用公共 `context.target_area` 做目标筛选，只返回攻击目标和追踪目标。
   - `decide(context)`：只做策略决策，返回 `StrategyDecision`，不得直接调用键盘或 Win32。
3. 在职业子包导出实现，再在 `mbv/strategies/__init__.py` 调用 `register_strategy(...)` 注册。面板会自动增加下拉项、说明和参数输入框。
4. 策略专属配置只能放在 `strategy.options.<strategy_key>` 下；公共角色相对索敌区固定放在 `targeting.box`，公共补药、输入、视觉阈值继续使用现有公共配置。策略专属多区域必须明确保存坐标空间，选敌与 HUD 共用同一换算函数。
   策略面板参数必须接入统一持久化流程：鼠标微调立即保存，退出前由 `_persist_settings` 再完整落盘。
5. 多策略共享的采集依赖放在公共 `recognition` 节点，并把键加入 `required_recognition_data`；只属于单个策略的多区域放在该策略 `settings` 中，由 `capture_fields.settings_path` 声明。标飞区域使用 `player_anchor_v1`，偏移和尺寸分别按战斗区宽高归一化。
6. 策略不能读内存、抓包、注入或绕过输入权限。公共安全门、补药、姓名板丢失位移恢复与停止条件不得复制到策略模块或降低优先级。
7. 给策略增加测试，至少覆盖：注册和说明文本、默认配置迁移、目标选择、最高优先级动作、边界值，以及缺少依赖数据时的安全行为。
8. 更新本文件，增加策略的职业、用途、采集依赖、决策优先级和专属参数说明；提交时还需按 `AGENTS.md` 更新版本号和 `CHANGELOG.md`。

## 接口边界

- 龙咆哮显式声明 `localization_mode="minimap"`，是以下视觉锚点和 `minimap_only` 禁止施法规则的唯一现有例外：仅放行无方向 `cast`，仍禁止 attack/chase/pickup/step/face/jump_attack。其他策略默认 `visual`，继续要求身份配对、视觉时限及到位等待；不得将小地图坐标换算为屏幕坐标。

- `TargetSelectionContext` 输入的是同一帧已经完成的检测结果以及公共 `target_area`。策略不得重新执行模板匹配；标飞多索敌区从自身 `settings` 读取并相对稳定角色锚点换算，屏幕方向固定且不随面向翻转。
- `player_anchor` 是公共视觉层提供的稳定战斗锚点；策略选敌和行动判断必须使用它，不得重新采用姓名板、头部或称号原始框的纵向中心。
- `StrategyActionContext` 包含归一化位置、已选目标、当前索敌区候选、上次攻击技能、公共行为配置和策略设置。策略返回动作意图与可选技能键，`BowmanBot` 统一执行按键并写运行状态。
- `runtime_state` 是运行层隔离的会话字典，决策可返回替换值；`started_at` 为本次挂机启动时间。导航任务设置 `navigation_active=true` 时，公共层在定位丢失后只等待，不执行左右找人位移；仍保留其它安全门。`StrategyToggleField.live_preview` 允许声明经过策略状态机处理的热开关，其余开关继续按原流程刷新配置。
- 导航期间公共层在药/Buff和定位门禁前记录连续定位缺失，`localization_lost_seconds` 同时覆盖丢失期间及恢复首个决策帧，避免上游提前返回而漏记；`action_interrupted` 表示上一行动帧未进入策略决策，策略不得将这段时间算作有效拾取。暂停/重新启动清除这些会话观测，不修改身份记录或延长小地图导航期限。
- `StrategyActionContext.minimap_only=true` 表示运行层只有经过身份配对的唯一实时小地图位置，屏幕玩家与目标坐标均不可用。所有策略只允许原有安全点／安全区回位或等待；到位返回 `MINIMAP_WAITING_VISUAL`，不得攻击、追怪、拾取或启动新周期短步。公共层保留窗口、药/Buff、标记唯一性、导航时限与实际位移验证，并拒绝该模式下的攻击类决策。不得把小地图比例直接当作屏幕比例。
- `StrategyDecision.action` 目前支持 `stop`、`face`、`attack`、`cast`、`chase`、`move`、`step`、`jump`、`down_jump`、`jump_attack`、`pickup`。`face` 只短按方向键改变面向，不得保持方向键或进入移动；`step` 以限定时长短按移动键，并由公共执行器记录周期动作和待回位状态。需要新动作时先扩展公共动作执行器和测试，不要在策略里直接发键。
- `StrategyDecision.face_each_attack` 保留作接口兼容，不再改变输入行为：所有模式的普通 `attack` 共用独立转向，仅首次、换边或有效性被撤销后重发方向，不定时刷新、不在每次技能时按住方向。方向点按尊重 `behavior.face_tap_seconds`（下限 0.02 秒、混合上限 0.1 秒），真实换向仍须等待上一技能与后续视觉帧；发键不等于视觉确认。混合模式例外：游戏后台时延后转向，普通攻击/跳攻沿用游戏当前面向只发技能，不能伪造方向；仅游戏已在前台时执行独立转向，不为战斗激活窗口。仅 foreground 普通攻击在移动发键／拾取释放后的首次恢复增加 0.12 秒跨帧释放与目标方向确认、至少 0.08 秒点按和 0.12 秒抬键等待；其它输入模式原时序不变，策略无需新增按键逻辑。
- `cast` 是不需要面向的技能意图，必须显式提供非空 `attack_key`；公共层先释放移动/拾取，不点方向键、不申请混合激活，使用 `attack_interval_seconds` 按实际发键时间限频。依然经过窗口/身份/暂停、药/Buff和小地图禁攻击门禁。
- `cast_interval_from_start` 默认 false，保留龙咆哮等策略从抬键完成计时；箭雨显式 true，仅同键且上次攻击记录未被其它动作改写时按实际按下起点计算间隔。`last_attack` 仍记录成功抬键完成，失败不更新冷却；不得使用截图前时间、追赶式补发或额外输入线程。目标诊断由 `TargetSelection.diagnostic` 传给公共日志，每秒汇总状态/原因帧数，写盘失败不干扰行动。
- `TargetSelectionContext.detections_fresh` 标识本帧真实检测，默认 True 保持旧调用兼容；依赖数量触发的策略必须忽略 False 的旧框。`StrategyCaptureField.required` 允许声明无开关的必采区域/点。`allow_player_lost_recovery=false` 阻断首次策略决策前的无定位盲走和旧视觉框行动。
- 面板“框选通用索敌范围”始终写入 `targeting.box`，与当前选中的职业策略无关。

## 配置示例

```json
{
  "targeting": {
    "box": {
      "forward": 0.2808,
      "back": 0.072,
      "up": 0.144,
      "down": 0.144
    }
  },
  "strategy": {
    "active": "bowman_dynamic",
    "options": {
      "bowman_dynamic": {
        "aoe_skill_key": "",
        "single_skill_key": "",
        "melee_skill_key": "",
        "aoe_min_monsters": 2,
        "aoe_cluster_distance_multiplier": 0.75,
        "melee_enter_distance_multiplier": 0.35,
        "melee_exit_distance_multiplier": 0.9,
        "platform_center_tolerance": 0.08,
        "platform_center_vertical_tolerance": 0.06,
        "platform_return_jump_interval_seconds": 0.45
      },
      "stationary_attack": {
        "melee_skill_key": "",
        "melee_enter_distance_multiplier": 0.35,
        "melee_exit_distance_multiplier": 0.9,
        "periodic_step_interval_seconds": 45.0,
        "periodic_step_seconds": 0.12,
        "platform_center_tolerance": 0.015,
        "platform_center_vertical_tolerance": 0.06,
        "platform_return_jump_interval_seconds": 0.45
      },
      "throwing_star_safe": {
        "use_target_regions": true,
        "use_common_target_box": false,
        "only_targets_below_player": true,
        "auto_face_target": true,
        "target_priority_mode": "region_priority_then_distance",
        "target_regions": [],
        "target_face_tap_seconds": 0.025,
        "use_near_target_jump_attack": true,
        "near_target_jump_attack_distance_px": 120.0,
        "use_safe_output_area": false,
        "patrol_inside_safe_area": false,
        "jump_interval_seconds": 0.35,
        "safe_patrol_edge_margin": 0.02,
        "minimum_target_vertical_gap": 0.02
      }
    }
  }
}
```
