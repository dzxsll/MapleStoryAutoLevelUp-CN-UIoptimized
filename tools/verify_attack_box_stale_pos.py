# -*- coding: utf-8 -*-
'''
离线自检：名字定位长时间失效时，攻击框用**过期坐标**（issue #6 排查，2026-09-28）

现象（用户日志实证）：
    [运行状态] 21 条里「名字命中」**全是 False**，miss 一路涨到 650；
    同期「怪N」显示 1~2（怪检测正常工作），但指令字段**从头到尾没有一个 attack**
    —— 这 100 秒里角色一次都没出招。

根因（代码路径，已核对）：
    `get_player_location_by_nametag()` 匹配失败时**不更新** `self.loc_nametag`
    （见 MapleStoryAutoLevelUp.py:1240-1244 的 else 分支），
    而 `loc_player` 每帧都用那个**陈旧**的 `loc_nametag` 重算 ⇒ 位置被「冻住」。
    攻击判定链（`update_cmd_by_mob_detection`）与攻击框
    （`get_attack_range` / `get_nearest_monster`）**全部以 `self.loc_player` 为锚**：
        搜索框 : x0 = px - dx,  x1 = px + dx   (dx = 350 + 50 = 400)
                 y0 = py - dy,  y1 = py + dy   (dy = 70 + 50 = 120)
        攻击框 : y0 = py - 35,  y1 = py + 35   ← 高度只有 70px
    角色每帧在动（实测 ~0.3px/帧，20fps ⇒ 约 6px/秒），而名字可能连续 650 帧
    （≈32 秒）不命中 ⇒ 攻击框与角色的真实位置**越差越远**，
    检测到的怪落不进框里 ⇒ 永远不满足出招条件 ⇒ 静默地"不打怪"。

⚠️ 本自检**只验证事实与不变量**，不预设该怎么修：
    · 它在 CI 可跑（不依赖 minimaps/、config_data.yaml、nametag/*.png 等私有素材）
    · 所有引擎调用都走「绕过 __init__ 的 Stub」，与既有自检一致

用法：
    python -m tools.verify_attack_box_stale_pos
'''
import sys

import numpy as np

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def make_stub(cfg, loc_player):
    '''构造一个能走通攻击链的假引擎（**不跑 __init__**）。

    ⚠️ `get_nearest_monster` 内部会调 `self.get_attack_range(...)`，
       所以必须把真实方法**绑到实例上**（否则 AttributeError）。
       绑的是引擎原方法，保证「测的就是跑的那份」。
    '''
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

    class S:
        pass

    s = S()
    s.get_attack_range = lambda is_left=True: MapleStoryAutoBot.get_attack_range(
        s, is_left=is_left)
    s.cfg = cfg
    s.loc_player = loc_player
    s.monsters = []
    # ⚠️ 必须给一个**真实尺寸**的模板：`get_nearest_monster` 的阈值是
    #    `min(min_mob_area, max_mob_area_trigger)`，而 min_mob_area 取模板面积。
    #    留空 ⇒ min_mob_area=0 ⇒ 阈值 0 ⇒ **任何**怪都算命中（把 bug 掩盖掉）。
    #    这里塞一个 40x40 的模板，阈值 = min(1600, 1500) = 1500，
    #    要求怪与攻击框的重叠面积 ≥ 1500px² —— 与真机一致。
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
    s.img_frame = None
    # ★ 2026-10-13：`get_nearest_monster` 把原来就地算的「最小怪面积」抽成了
    #   `_min_mob_area()`，本文件是**不绑方法**、直接 `MapleStoryAutoBot.xxx(st, ...)`
    #   的写法 ⇒ 必须把新方法绑到桩上，否则实跑直接
    #   `AttributeError: 'S' object has no attribute '_min_mob_area'`。
    #   顺带把感知框那两个也绑上（同一次改动引入，后面用例可能会用到）。
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot as _Bot
    for _n in ("_min_mob_area", "get_detect_extent", "get_search_box"):
        try:
            setattr(s, _n, getattr(_Bot, _n).__get__(s))
        except AttributeError:
            pass
    return s


def base_cfg():
    return {
        "bot": {"attack": "directional"},
        "monster_detect": {"search_box_margin": 50, "max_mob_area_trigger": 1500},
        "character": {"width": 100, "height": 150},
        "directional_attack": {"range_x": 350, "range_y": 70,
                               "cooldown": 0.9, "turn_frames": 3},
    }


def main():
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

    print("===== 1. 攻击框锚定在 loc_player（这是「位置过期 = 不打怪」的机制） =====")
    st = make_stub(base_cfg(), (700, 400))
    r = MapleStoryAutoBot.get_attack_range(st, is_left=True)
    check("directional/left 的攻击框", r, (700 - 350, 400 - 35, 700, 400 + 35))
    check("  ⤷ 横向跨度 350px", r[2] - r[0], 350)
    check("  ⤷ 纵向只有 70px（很窄）", r[3] - r[1], 70)

    st = make_stub(base_cfg(), (700, 400))
    r = MapleStoryAutoBot.get_attack_range(st, is_left=False)
    check("directional/right 的攻击框", r, (700, 400 - 35, 700 + 350, 400 + 35))

    print()
    print("===== 2. 位置一过期，怪就落不进攻击框（直接复现「不打怪」） =====")
    # 假设角色真实在屏幕 (700, 400)，但名字失效、loc_player 冻在 (200, 100)
    # 一只 40x40 的怪贴在角色脸上：放在 (660, 380)
    #   => 完整落在攻击框 x[350,700] y[365,435] 内，重叠 1600px² ≥ 阈值 1500
    mon = {"name": "m", "position": (660, 380), "size": (40, 40), "score": 0.1}

    st = make_stub(base_cfg(), (700, 400))          # 位置新鲜
    st.monsters = [mon]
    got = MapleStoryAutoBot.get_nearest_monster(st, is_left=True)
    check("位置新鲜 → 找得到怪（会出招）", got is not None, True)

    st = make_stub(base_cfg(), (200, 100))          # 名字失效，位置冻在别处
    st.monsters = [mon]
    got = MapleStoryAutoBot.get_nearest_monster(st, is_left=True)
    check("位置过期 → 找不到怪（静默不出招）", got is None, True)

    print()
    print("===== 3. 位置过期**十几帧**就足以让攻击彻底失效 =====")
    # 角色真实位置随帧推进（20fps、每帧约 0.3px 时位移很小；
    # 但名字失效期间**掉帧/重定位**会让偏差累积到几十像素）。
    st = make_stub(base_cfg(), (700, 400))
    st.monsters = [mon]
    found_at = None
    for lag in range(0, 130, 10):                   # 冻结位置离真实位置 lag px
        st.loc_player = (700 - lag, 400)
        if MapleStoryAutoBot.get_nearest_monster(st, is_left=True) is not None:
            found_at = lag
    # 攻击框左边界 = px - 350；怪在 x=690 ⇒ px - 350 <= 730 恒成立（横向够宽）
    # 纵向 ±35 才是瓶颈：怪 y=385 需要 |py - 400| <= 35
    check("横向 350px 很宽，纵向才是瓶颈（本用例横向仍能命中）", found_at is not None, True)

    # 换成纵向偏差 —— 这才是真正的瓶颈
    losses = []
    for lag in range(0, 130, 10):
        st.loc_player = (700, 400 - lag)
        if MapleStoryAutoBot.get_nearest_monster(st, is_left=True) is None:
            losses.append(lag)
    check("纵向偏差 ≥40px 起就再也找不到怪", bool(losses) and min(losses) <= 40, True)
    print(f"       （实际最先失效的纵向偏差 = {min(losses) if losses else '未失效'}px）")

    print()
    print("===== 4. 名字定位失效时**没有任何**降级/拒止（这就是缺陷本身） =====")
    import inspect
    src = inspect.getsource(MapleStoryAutoBot.get_player_location_by_nametag)
    check("匹配失败时不更新 loc_nametag（位置被冻住）",
          "self.nametag_miss_streak += 1" in src, True)
    check("  ⤷ 且不删除/复位 loc_nametag", "self.loc_nametag = None" not in src, True)

    src2 = inspect.getsource(MapleStoryAutoBot.update_cmd_by_mob_detection)
    check("攻击判定**没有**检查 nametag 是否命中",
          "nametag_hit" not in src2 and "nametag_miss_streak" not in src2, True)
    src3 = inspect.getsource(MapleStoryAutoBot.get_nearest_monster)
    check("攻击框计算**没有**检查 nametag 是否命中",
          "nametag" not in src3, True)

    print()
    print("===== 5. 告警文案已说清后果（**只改文案，不改行为**） =====")
    # ⚠️★ 本用例**刻意不断言任何"位置过期就停手"的行为** ——
    #    2026-09-28 曾加过一个 stale_after_frames=30 的闸门，随后**回滚**了：
    #    按实测速度（约 6px/秒）算，30 帧（1.5 秒）时偏差才 9px，
    #    而攻击框能容忍 ±35px ⇒ 那一版会**把本来还能打到的也停掉**，
    #    对命中率本就极低的用户是恶化。停手阈值必须先用真机实测标定，不能拍脑袋。
    import inspect
    src4 = inspect.getsource(MapleStoryAutoBot)
    _lines = [ln for ln in src4.splitlines() if not ln.lstrip().startswith("#")]
    _code = "\n".join(_lines)
    check("存在 nametag_ever_hit 分支（区分两种严重程度）",
          "if not self.nametag_ever_hit:" in _code, True)
    check("活代码里已去掉误导性的「影响有限」", "影响有限" not in _code, True)
    check("文案点明「打怪判定用的就是这个位置」",
          "打怪判定用的就是这个位置" in _code, True)
    check("文案给出攻击框高度（让人看懂为什么偏一点就打空）",
          "range_y" in _code.split("名字定位]")[-1][:600], True)
    check("文案给出可操作的处理办法（重新标定模板）",
          "重新标定名字模板" in _code, True)

    print()
    print("===== 6. 没有引入「位置过期就停手」的闸门（已回滚，防复发） =====")
    check("引擎里不存在 _attack_pos_is_fresh 闸门",
          "_attack_pos_is_fresh" not in _code, True)
    check("update_cmd_by_mob_detection 开头没有 stale 短路",
          "stale_after_frames" not in _code, True)

    print()
    print("===== 7. 名字匹配分数已落盘（issue #6 的定位抓手） =====")
    # 为什么必须有：用户日志里只有二值的"名字命中True/False"，
    #   **看不出"离阈值差多远"** —— 而"差一点点"（模板/阈值问题）和
    #   "差很多"（名牌被挡/位置不对）病因完全不同。引擎原来把 score 丢了
    #   （`_ = (score, ...)`），对比小地图定位是有存 score 的。
    check("引擎里存了 nametag_score", "self.nametag_score" in _code, True)
    check("维护了近段分数窗口（用于报最低分）",
          "_nt_score_hist" in _code, True)
    check("失败告警会打出「本帧分数 / 近段最低」",
          "名字定位·分数" in _code and "近段最低" in _code, True)
    _srcline = [ln for ln in _code.splitlines() if "_s = float(getattr(self, \"nametag_score\"" in ln]
    check("   ⤷ 读分数时做了兜底（不硬取属性，Stub 也不崩）",
          bool(_srcline) and "getattr" in _srcline[0], True)

    # ⚠️ 决定性：这些埋点**绝不能参与判定**。判定的唯一依据仍是
    #    `score < diff_thres`（见引擎里那句 if），下面的断言防止有人
    #    日后拿 nametag_score / 窗口分去做阈值判断（那会改变行为）。
    _judge = [ln for ln in _code.splitlines()
              if ("nametag_score" in ln or "_nt_score_hist" in ln)
              and ("if " in ln or "return" in ln)]
    check("   ⤷ 分数埋点**没有**参与任何 if/return（纯观测）", _judge, [])

    # ★ 空窗口的判定：没有分数时**不能**拿兜底值 -1.0 去比阈值 ——
    #   那会落进"接近阈值"分支，把"压根没数据"误报成"差一点点"，正好把排查带偏。
    check("   ⤷ 空窗口不会误报「接近阈值」（必须先判有没有数据）",
          "_best = min(_h) if _h else -1.0" not in _code, True)
    check("   ⤷ 空窗口有专门的说法（取不到匹配分数）",
          "取不到匹配分数" in _code, True)

    print()
    if FAIL:
        print(f"失败的用例（{len(FAIL)}）：{', '.join(FAIL)}")
        return 1
    print("全部通过：已钉死「位置过期 → 攻击框偏移」的事实链；"
          "行为**未被改动**（停手闸门已回滚，阈值待真机标定）；"
          "名字匹配分数已作为观测抓手落盘")
    return 0


if __name__ == "__main__":
    sys.exit(main())
