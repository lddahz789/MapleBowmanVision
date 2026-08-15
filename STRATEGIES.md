# 职业打怪策略扩展规范

职业策略负责“如何选目标、何时回位、攻击/追踪/巡逻如何排序”。窗口截图、玩家与怪物识别、输入权限、补药和退出等安全逻辑仍由公共运行层负责。

## 当前策略：弓箭手动态

- 职业：弓箭手
- 标识：`bowman_dynamic`
- 描述：根据小地图玩家标记的水平与垂直位置优先回到平台安全点附近；掉到下层时跳回，位于上层时下跳。范围内按玩家攻击锚点到怪物中心的二维距离优先攻击最近单位；每次攻击都先按住当前目标方向，在方向键仍按下时发出攻击，被怪物击退后也不依赖上次朝向。
- 依赖采集：小地图与玩家标记、小地图平台安全点、战斗识别区、玩家定位模板、怪物模板。
- 决策优先级：输入/窗口安全 → HP/MP 补药 → 返回小地图平台安全点 → 区内攻击 → 同层追踪 → 拾取/巡逻。
- 索敌框：使用公共 `targeting.box`，不属于弓箭手策略；框随角色朝向左右翻转。
- `platform_center_tolerance`：水平安全半径，是小地图宽度的比例；超出后忽略目标并优先左右回位。
- `platform_center_vertical_tolerance`：垂直安全半径，是小地图高度的比例；超出后根据上下层关系跳回或下跳。
- `platform_return_jump_interval_seconds`：连续回位跳跃之间的最短间隔，防止按键过密。

## 当前策略：原地攻击

- 职业：通用
- 标识：`stationary_attack`
- 描述：保持玩家当前位置不动，使用左右两个面向索敌区的并集选择最近同层怪物，不受当前朝向限制；仅在目标换边时短按方向键并原地攻击，范围外目标不追踪，也不巡逻或返回平台中心。
- 依赖采集：战斗识别区、玩家定位模板、怪物模板；不依赖平台中心。
- 决策优先级：输入/窗口安全 → HP/MP 补药 → 区内攻击 → 原地等待。
- 索敌框：使用公共 `targeting.box`，框随角色朝向左右翻转。
- 专属参数：无。

## 当前策略：标飞安全输出

- 职业：飞侠·标飞
- 标识：`throwing_star_safe`
- 描述：安全输出区在放大的小地图上框选，并与玩家标记使用同一坐标；启用后，玩家标记低于安全区时优先朝安全区水平中心移动并向上跳。标飞可维护多个跟随角色但不随面向翻转的独立索敌区，按区域并集过滤怪物；近身水平重叠达到阈值时可执行跳跃攻击。
- 依赖采集：战斗识别区、玩家定位模板、怪物模板和小地图玩家标记；`use_target_regions=true` 时至少要求一个已启用的标飞索敌区，`use_safe_output_area=true` 时要求重新框选小地图安全输出区。
- 决策优先级：输入/窗口安全 → HP/MP 补药 → 可选安全区回位 → 近身跳跃攻击 → 普通向下攻击 → 安全区内巡逻。
- 上方边界：当前不处理玩家位于安全区上方的下跳回位；发现位于上方时原地停住并等待人工处理。
- 移动限制：不追怪、不自动拾取；无有效目标时只在安全输出区内左右巡逻，到边界前自动折返。
- `use_target_regions`：启用策略内的多索敌区；区域相对稳定角色锚点移动，但不随角色面向翻转，重采战斗识别区后自动失效。
- `use_common_target_box`：是否在多索敌区过滤后继续套用公共角色相对索敌框，默认关闭。
- `only_targets_below_player`：是否只保留角色脚底下方的目标。
- `auto_face_target`：索敌成功后若目标在另一侧，先单独短按一次方向键改变面向，下一帧再攻击；不会进入移动或追踪。
- `target_face_tap_seconds`：标飞自动转向时方向键的短按时长，默认 `0.025` 秒。
- `target_priority_mode`：支持“区域优先级后水平距离”“水平距离最近”“识别分最高”。区域重叠时取最小优先级数值。
- `throwing_star_safe_output_area`：保存 `space=minimap` 和相对小地图的 `x/y/w/h`；重采战斗识别区时保留，重采小地图时失效。旧战斗画面坐标不迁移。
- `jump_interval_seconds`：连续回位跳跃之间的最短间隔。
- `minimum_target_vertical_gap`：怪物中心必须低于角色脚底的最小归一化高度差，同层目标会被过滤。
- `use_close_jump_attack`：是否启用近身重叠跳跃攻击，默认开启。
- `close_overlap_threshold`：水平重叠宽度除以角色与怪物较小宽度，默认阈值 `0.2`，等于阈值时触发。
- `jump_attack_cooldown_seconds`：两次跳跃攻击之间的最短间隔。

## 新增策略必须遵循

1. 按职业在 `mbv/strategies/<profession>/` 建子包，再为每个策略新建独立模块；目录、文件名和 `key` 使用稳定的 ASCII `snake_case`，不要把职业分支写回 `mbv/bot.py`。
2. 实现 `CombatStrategy` 协议，至少提供：
   - `key`：持久化配置标识，发布后不得随意改名。
   - `display_name`：面板下拉显示的简体中文名称。
   - `profession`：职业分类。
   - `description`：一到三句话说明执行逻辑；用户切换下拉选项时会直接看到。
   - `required_recognition_data`：依赖的公共采集数据键。
   - `default_settings` 和 `setting_fields`：策略专属默认值及面板字段；数字字段同时声明鼠标微调步长和上下限。
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

- `TargetSelectionContext` 输入的是同一帧已经完成的检测结果以及公共 `target_area`。策略不得重新执行模板匹配；标飞多索敌区从自身 `settings` 读取并相对稳定角色锚点换算，屏幕方向固定且不随面向翻转。
- `player_anchor` 是公共视觉层提供的稳定战斗锚点；策略选敌和行动判断必须使用它，不得重新采用姓名板、头部或称号原始框的纵向中心。
- `StrategyActionContext` 只包含归一化位置、已选目标、公共行为配置和策略设置。策略返回动作意图，`BowmanBot` 统一执行按键并写运行状态。
- `StrategyDecision.action` 目前支持 `stop`、`face`、`attack`、`chase`、`move`、`jump`、`down_jump`、`jump_attack`、`pickup`。`face` 只短按方向键改变面向，不得保持方向键或进入移动；需要新动作时先扩展公共动作执行器和测试，不要在策略里直接发键。
- `StrategyDecision.face_each_attack` 仅对 `attack` 生效；为 `true` 时每次按住目标方向，等待 `behavior.face_tap_seconds` 后在按键仍按下时攻击，发出后再释放方向。需要尽量保持原位的策略可设为 `false`，此时只在目标换边时点按方向。
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
        "platform_center_tolerance": 0.08,
        "platform_center_vertical_tolerance": 0.06,
        "platform_return_jump_interval_seconds": 0.45
      },
      "stationary_attack": {},
      "throwing_star_safe": {
        "use_target_regions": true,
        "use_common_target_box": false,
        "only_targets_below_player": true,
        "auto_face_target": true,
        "target_priority_mode": "region_priority_then_distance",
        "target_regions": [],
        "target_face_tap_seconds": 0.025,
        "use_safe_output_area": false,
        "patrol_inside_safe_area": true,
        "jump_interval_seconds": 0.35,
        "safe_patrol_edge_margin": 0.02,
        "minimum_target_vertical_gap": 0.02
      }
    }
  }
}
```
