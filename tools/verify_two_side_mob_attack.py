# -*- coding: utf-8 -*-
'''
离线自检：「左右两侧同时有怪 → 一次都不出招，且原地左右抽搐」（issue #12）

现象（issue #12 评论区 caoli5288 原话，2026-09-29）：
    「左右同时存在怪物时，角色在原地左右抽搐，但是不攻击」
    「偶发的识别到怪物，不攻击，往怪物身上创」

根因（代码路径，已核对 `src/engine/MapleStoryAutoLevelUp.py`）：
    ① `get_attack_direction()` 在**两侧都有怪、且曼哈顿距离差 < 50px** 时
       返回 `None`（原注释的理由是「avoid confusion」）。
       `update_cmd_by_mob_detection()` 拿到 None 后**直接 return**，
       于是「两边都有怪」反而成了**比只有一边更差**的情形 ——
       一边有怪必打，两边都有怪却一次不出招。

    ② 距离差在 50px 边界附近抖动时，目标在 left/right 之间**帧间横跳**：
       每帧都命中转身状态机的「怪换边」分支（第 4418-4422 行）
       → 角色被按住方向键反复转向 → 表现为「原地左右抽搐」，
       而转身分支每帧都 `return`（第 4429 行）⇒ **永远轮不到出招**。
       ⇒ ①②不是两个独立 bug：②正是①在"边界抖动"下的惯性后果。

本自检**只钉事实与不变量**，不依赖游戏窗口 / minimaps / config_data.yaml，
可在 CI 跑。引擎调用全部走「绕过 __init__ 的 Stub」，与既有自检口径一致。

用法：
    python -m tools.verify_two_side_mob_attack
'''
import sys

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def make_stub(cfg, loc_player, monsters, **kw):
    '''构造能走通索敌链的假引擎（**不跑 __init__**）。

    ⚠️ 必须把真实方法**绑到实例上**（否则 AttributeError）；
       绑的是引擎原方法，保证「测的就是跑的那份」。
    '''
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

    class S:
        pass

    s = S()
    for _name in ("get_attack_range", "get_nearest_monster",
                  "get_attack_direction", "update_cmd_by_mob_detection",
                  "get_face_direction",
                  # 2026-10-13 加：感知框/攻击框分开 —— 索敌框改由 get_search_box 出，
                  # 攻击判定走 _mobs_in_attack_box（内部用 _min_mob_area）
                  "get_detect_extent", "get_search_box",
                  "_min_mob_area", "_mobs_in_attack_box",
                  # 2026-10-14 加：「没出招·归因」纯观测日志（读攻击框尺寸与前台状态）
                  "_attack_box_half", "_game_window_front", "_mob_no_attack_log",
                  # 2026-10-14 加：[出招] 计数（4 处出招都调它，纯观测）
                  "_note_attack", "_log_attack_heartbeat",
                  # 2026-10-14 加：「离够得着最近的一只差多少」（归因行里的数字）
                  "_engage_gap", "_engage_gap_facts",
               # 2026-10-14 加：[怪检测] 明细行（纯观测；它要读判据名与攻击框）
               "_engage_mode", "_log_monster_detect",

               # 2026-10-14 加：梯上让路收紧（`update_cmd_by_mob_detection` 的让路块只问它）

               # 2026-10-14 加「宽判定余量」（B）：唯一读点 + 唯一拼框点


               "_engage_margin", "_attack_boxes",


               "_ladder_still_s", "_ladder_yield",
               "_rope_stuck_s", "_rope_giveup_now",
               "_rope_ladder_giveup_tick", "_budget_note_lost",
               # 爬梯走完（2026-10-15，用户 m11669）：路线侧每帧无条件问一次
               "_ladder_finish_cfg", "_ladder_finish_tick"):
        setattr(s, _name, getattr(MapleStoryAutoBot, _name).__get__(s))
    s.cfg = cfg
    s.loc_player = loc_player
    s.monsters = monsters
    # ⚠️ 真实尺寸模板：`get_nearest_monster` 的重叠阈值取
    #    `min(min_mob_area, max_mob_area_trigger)`。留空 ⇒ 阈值 0 ⇒ 任何怪都算命中，
    #    会把「冲突」掩盖掉。这里 40x40 ⇒ 阈值 = min(1600, 1500) = 1500，与真机一致。
    import numpy as np
    s.monsters_info = {"m": [(np.zeros((40, 40, 3), np.uint8), None)]}
    s.is_on_ladder = False
    s.is_using_home_route = False
    s.t_last_attack = 0.0
    s.cmd_action = "none"
    s.cmd_move_x = "none"
    s.cmd_move_y = "none"
    s.cmd_move_x_last = "none"
    s._turn_frames_left = 0
    s._turn_dir = None
    s._turn_pending_dir = None
    s._attack_dir_last = None
    s.img_frame = None
    s._perf = lambda *a, **k: None
    s._mob_skip = 0
    # ⚠️ `update_cmd_by_mob_detection` 会用 `self.img_frame.shape` 夹搜索框边界，
    #    必须给一个**有 shape 的假帧**（不真跑图像处理，只当尺寸来源）。
    s.img_frame = np.zeros((600, 800, 3), np.uint8)
    # ⚠️ 本自检只测**索敌决策**，不测图像匹配。真跑 get_monsters_in_range 会
    #    在 800x600 帧上做模板匹配（实测 >120 秒），且它测的是别的东西。
    #    这里替换成"原样返回当前的 self.monsters"，让决策链可离线复现。
    #    ★ 必须是**动态读取**（闭包读 s.monsters），不能把列表冻在闭包里 ——
    #      否则用例中途改 st.monsters 时，桩还返回旧列表，
    #      表现为"喂了怪却说没怪、一次不出招"，非常难查（本用例第一版就踩了）。
    s.get_monsters_in_range = lambda *a, **k: s.monsters
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def base_cfg():
    return {
        "bot": {"attack": "directional"},
        "monster_detect": {"search_box_margin": 50, "max_mob_area_trigger": 1500,
                           "mode": "contour_only", "contour_blur": 5,
                           "diff_thres": 0.8, "with_enemy_hp_bar": False,
                           # ★ 2026-10-14：宽判定余量在本文件里关掉 —— 本文件钉的是
                           #   "两侧都有怪时选近的打"的几何，余量会把期望挪 40px。
                           #   余量自己的行为在 tools/verify_engage_margin.py 里钉。
                           "mob_engage_margin": 0},
        "character": {"width": 100, "height": 150},
        "directional_attack": {"range_x": 350, "range_y": 70,
                               "cooldown": 0.9, "turn_frames": 3},
    }


def mob(cx, cy, size=(40, 40), score=0.1):
    '''按**中心点**造一只怪（position 是左上角）。'''
    w, h = size
    return {"name": "m", "position": (cx - w // 2, cy - h // 2),
            "size": (w, h), "score": score}


PLAYER = (700, 400)


def main():
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

    print("===== 1. 单侧有怪 → 必打（这是「正常」的基准） =====")
    st = make_stub(base_cfg(), PLAYER, [mob(600, 400)])
    check("只有左侧的怪 → 方向 left",
          MapleStoryAutoBot.get_attack_direction(st, st.monsters[0], None),
          "left")
    st = make_stub(base_cfg(), PLAYER, [mob(800, 400)])
    check("只有右侧的怪 → 方向 right",
          MapleStoryAutoBot.get_attack_direction(st, None, st.monsters[0]),
          "right")

    print()
    print("===== 2. ★两侧都有怪 → 必须选**近的**那只，不许两边都不打（已修） =====")
    # 玩家 x=700。左怪中心 660（距 40），右怪中心 745（距 45）
    #   ⇒ 修复前：距离差 5px < 50 ⇒ 返回 None（一次不打）
    #   ⇒ 修复后：返回 "left"（更近的那只）
    st = make_stub(base_cfg(), PLAYER, [mob(660, 400), mob(745, 400)])
    _l = MapleStoryAutoBot.get_nearest_monster(st, is_left=True)
    _r = MapleStoryAutoBot.get_nearest_monster(st, is_left=False)
    check("  左侧找得到怪（攻击框判定没挡住）", _l is not None, True)
    check("  右侧找得到怪（攻击框判定没挡住）", _r is not None, True)
    check("★两侧都有怪 → 选近的 left（不再是 None）",
          MapleStoryAutoBot.get_attack_direction(st, _l, _r), "left")

    print()
    print("===== 3. 上一步的下游后果：整帧真的发出 attack 指令 =====")
    st = make_stub(base_cfg(), PLAYER, [mob(660, 400), mob(745, 400)])
    st.t_last_attack = 0.0
    st._turn_frames_left = 0
    st.cmd_move_x_last = "left"        # 朝向已就位，不需要先转身
    st._turn_dir = "left"
    MapleStoryAutoBot.update_cmd_by_mob_detection(st)
    check("  cmd_action == attack（识别到怪就出招了）",
          st.cmd_action == "attack", True)

    print()
    print("===== 4. 距离差不接近（>50px）时选近的 —— 修复前后都必须成立（不许回归） =====")
    # 左怪 660（距 40）、右怪 900（距 200）⇒ 差 160 > 50 ⇒ 选近的 left
    st = make_stub(base_cfg(), PLAYER, [mob(660, 400), mob(900, 400)])
    _l = MapleStoryAutoBot.get_nearest_monster(st, is_left=True)
    _r = MapleStoryAutoBot.get_nearest_monster(st, is_left=False)
    check("  左侧更近 → 选 left",
          MapleStoryAutoBot.get_attack_direction(st, _l, _r), "left")
    # 反过来的对照组：右怪更近 → 必须选 right（防"永远选 left"的假修复）
    st = make_stub(base_cfg(), PLAYER, [mob(400, 400), mob(740, 400)])
    _l = MapleStoryAutoBot.get_nearest_monster(st, is_left=True)
    _r = MapleStoryAutoBot.get_nearest_monster(st, is_left=False)
    check("  右侧更近 → 选 right",
          MapleStoryAutoBot.get_attack_direction(st, _l, _r), "right")

    print()
    print("===== 5. ★距离**并列**时必须有确定结果（不许退化成 None） =====")
    # 左怪 660（距 40）、右怪 740（距 40）⇒ 完全并列。
    # ⚠️ 这是修复最关键的一行：若写成严格小于，并列时两个分支都不命中
    #    ⇒ 又回到"两边都不打"。必须用 <= 让 left 稳定胜出。
    st = make_stub(base_cfg(), PLAYER, [mob(660, 400), mob(740, 400)])
    _l = MapleStoryAutoBot.get_nearest_monster(st, is_left=True)
    _r = MapleStoryAutoBot.get_nearest_monster(st, is_left=False)
    _d = MapleStoryAutoBot.get_attack_direction(st, _l, _r)
    check("★并列时给出确定方向（left），不是 None",
          _d, "left")

    print()
    print("===== 6. ★「谁更近」帧间微小摆动 → 方向不许翻面（迟滞），且必须持续出招 =====")
    # 真实场景（issue #12）：两只怪**都在动**，谁更近只差十几像素、帧间来回翻。
    #   修复前：方向逐帧横跳 ⇒ 转身永远追不上 ⇒ 角色原地左右抽搐、一帧不出招。
    #   修复后：迟滞（默认 30px）压住这种小优势摆动 ⇒ 方向稳住 ⇒ 照常出招。
    #
    # ⚠️ 三个陷阱（本用例前两版都踩了，记下来免得后人重踩）：
    #   ① `update_cmd_by_mob_detection` 出招后会**自己**把 t_last_attack 设成
    #      `time.time()`；每帧重置冷却会让 0.9s 冷却永远挡着 ⇒ 0 次出招。
    #      这里模拟真实的 20fps 节拍：每帧把时间推进 0.05 秒（20 帧 = 1 秒，
    #      按 cooldown 0.9s 算，预期能落地 1 次）。
    #   ② 摆动幅度必须**小于迟滞窗口**（这里 ±10px vs 窗口 30px），
    #      否则就是"另一侧明显更近"、迟滞本就该放行，测的就不是迟滞了。
    #   ③ 桩 `get_monsters_in_range` 必须**动态读** st.monsters（见 make_stub 说明），
    #      冻成闭包会让"喂了怪却说没怪"，表现为静默 0 次出招。
    import src.engine.MapleStoryAutoLevelUp as _mod
    st = make_stub(base_cfg(), PLAYER, [])
    _real_time = _mod.time.time
    _fake_t = [1000.0]
    _mod.time.time = lambda: _fake_t[0]

    attacked = 0
    turn_frames = 0
    dirs = []
    try:
        for i in range(20):
            _fake_t[0] += 0.05                     # 20fps 的真实节拍
            # 两只怪分居玩家左右两侧，**谁更近**逐帧小幅摆动（±10px）：
            #   左怪中心 = 700 - (30 - swing)、右怪中心 = 700 + (30 + swing)
            #   swing = 0  → 左距 30、右距 30（并列，left 胜出）
            #   swing = 10 → 左距 20、右距 40（左明显更近…但优势 20 < 迟滞窗口 30）
            # ⚠️ 两边都必须在攻击框内（横向 ±350、纵向 ±35），且中心明确分居左右。
            #    本用例第四版曾把两只怪都放在玩家 20px 内 ⇒ 中心几乎贴着玩家，
            #    左右归属随几像素漂移，测的根本不是迟滞。
            swing = 0 if i % 2 == 0 else 10
            st.monsters = [mob(700 - (30 - swing), 400),
                           mob(700 + (30 + swing), 400)]
            st.cmd_move_y = "none"
            # ⚠️ 必须用 `get_nearest_monster` 取左右怪，**不能**拿
            #    `st.monsters[0] / [1]` 顶替 —— 列表顺序是「谁在左边」，
            #    而本函数要的是「攻击框内、分别位于左右两侧的最近怪」。
            #    传错会让判定结果与引擎内部不一致（本用例第三版就踩了这个坑）。
            _l = MapleStoryAutoBot.get_nearest_monster(st, is_left=True)
            _r = MapleStoryAutoBot.get_nearest_monster(st, is_left=False)
            dirs.append(MapleStoryAutoBot.get_attack_direction(st, _l, _r))
            MapleStoryAutoBot.update_cmd_by_mob_detection(st)
            if st.cmd_action == "attack":
                attacked += 1
            elif st.cmd_move_x in ("left", "right"):
                turn_frames += 1
            # 复刻 hunting.on_frame 的收尾：记下本帧实际发出的方向
            st.cmd_move_x_last = st.cmd_move_x
            # 复刻 update_cmd_by_route 每帧把 cmd_move_x/cmd_action 改回去的事实
            st.cmd_action = "none"
            st.cmd_move_x = "none"
    finally:
        _mod.time.time = _real_time

    print(f"       （20 帧里：出招 {attacked} 次，转向 {turn_frames} 次；"
          f"判定的方向序列 = {''.join(d[0] if d else '-' for d in dirs)}）")
    check("★微小摆动下方向被迟滞稳住（20 帧内不翻面）",
          len(set(dirs)) == 1, True)
    check("★方向稳住后能正常出招（不再 0 次）", attacked > 0, True)
    check("  转身帧数不再是每帧都在转（抽搐消失）",
          turn_frames <= 15, True)

    print()
    print("===== 7. 白盒：病灶已从源码消失（防日后被「顺手重构」改回去） =====")
    import inspect
    src = inspect.getsource(MapleStoryAutoBot.get_attack_direction)
    _code = "\n".join(ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#"))
    # 旧行为的标志：两侧都合法时要求距离差 > 50 才动手
    check("  已删除「距离差 < 50 就不打」这个阈值",
          "- 50" not in _code, True)
    check("  改成按距离取近者（<= 保证并列有确定结果）",
          "distance_left <= distance_right" in _code, True)
    _src2 = inspect.getsource(MapleStoryAutoBot.update_cmd_by_mob_detection)
    _code2 = "\n".join(ln for ln in _src2.splitlines()
                       if not ln.lstrip().startswith("#"))
    check("  转身进行中不再无条件因换边而重置计时",
          "_turn_pending_dir" in _code2, True)
    # ⚠️ 最关键的回归防线：旧写法把两个条件用 or 串起来，
    #    只要"怪换边"成立就重置 _turn_left ⇒ 抽搐。这个形状不许再出现。
    check("  旧的无条件换边重置写法已消失",
          "or getattr(self, \"_turn_dir\", None) != attack_direction)" not in _code2,
          True)

    print()
    if FAIL:
        print(f"失败的用例（{len(FAIL)}）：{', '.join(FAIL)}")
        return 1
    print("全部通过：两侧都有怪时**选近的打**（不再两边都停手）；"
          "目标左右横跳时转身不再被无限打断，每 N 帧必落地一次出招")
    return 0


if __name__ == "__main__":
    sys.exit(main())
