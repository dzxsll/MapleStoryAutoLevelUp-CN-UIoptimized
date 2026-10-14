# -*- coding: utf-8 -*-
'''
离线自检：真实配置 + 真实地图资源跑一遍 load_config（2026-09-12）

为什么需要它：load_config 里**没有 try/except**，抛异常时异常不进日志，
界面上的表现就是「点开始没反应」，查都没处查。
（2026-09-12 真出过一次：在 load_config 里误用 self.cfg —— 它要到函数末尾
 406 行才赋值，那会儿还是 None —— None.get() 抛 AttributeError，全程静默。）

这个脚本用**真实的 config_default + config_custom + 真实 minimaps/** 走完整条
加载链，任何一步炸了都能当场看见堆栈，不用开着游戏去点开始。

不开游戏、不抢焦点、不初始化驱动（load_config 不碰 kb / capture）。

用法：
    python -m tools.verify_load_config
'''
import sys
import types

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot
from src.utils.common import load_yaml

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def merge(base, custom):
    '''把 custom 深度合并进 base（模拟 controller 的配置合并）。'''
    for k, v in custom.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base


def new_bot(custom_path="config/config_custom.yaml"):
    '''不跑 __init__（它会创建抓帧器、需要游戏窗口），只补 load_config 要用的字段。'''
    args = types.SimpleNamespace(test_image="", is_ui=False, init_state="",
                                 debug=False, disable_viz=True)
    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.args = args
    b.cfg = None                       # ⚠️ load_config 末尾才会赋值，开头就是 None
    b.data = load_yaml("config/config_data.yaml")
    b.monsters_info = {}
    b.img_routes = []
    b.img_route_home = None
    b.img_map = None
    b.img_route = None
    b.color_code = {}
    b.color_code_up_down = {}
    b.is_show_debug_window = False
    b.img_route_debug = None
    b.idx_routes = 0
    b.is_using_home_route = False
    b.is_terminated = False
    b.kb = None
    b.capture = None
    return b


def main():
    custom_path = "config/config_custom.yaml"   # 换成你的方案名即可
    cfg = merge(load_yaml("config/config_default.yaml"),
                load_yaml(custom_path))
    map_name = cfg["bot"]["map"]
    mode = cfg["bot"]["mode"]
    print(f"配置: 地图={map_name!r} 模式={mode!r} "
          f"match_all_mobs={cfg['monster_detect'].get('match_all_mobs')}")

    # ⚠️ 录制进行中时地图目录是空的（选了"清掉重录"会先备份再重建，
    #    map.png 要等录制结束才写）—— 这时跑 load_config 必然 FileNotFoundError，
    #    是**暂时状态**不是代码问题，直接跳过免得误报。
    import os
    map_dir = os.path.join("minimaps", map_name) if map_name else ""
    if not map_name or not os.path.exists(os.path.join(map_dir, "map.png")):
        print(f"\n⚠️ 跳过：minimaps/{map_name}/map.png 还不存在"
              f"（多半是正在录制中）。等录完再跑本自检。")
        return 0

    print("\n【1】真实配置 + 真实资源跑 load_config（异常会直接抛出来，不静默）")
    b = new_bot()
    ret = b.load_config(cfg)
    check("load_config 返回 0", ret, 0)

    print("\n【2】怪物模板确实加载到了（跨图通用模式）")
    check("至少一种怪", len(b.monsters_info) > 0, True)
    print(f"       实际加载: {list(b.monsters_info.keys())}")

    print("\n【3】路线 / 回正线按模式加载正确（2026-09-12：定点已删，只有巡逻 normal）")
    print(f"       主线段数={len(b.img_routes)} 回正线={'有' if b.img_route_home is not None else '无'}")
    check("模式是巡逻 normal", mode, "normal")
    # ⚠️ **不断言「必须有主线」**：路线是用户录的，录废了会被 validate_route_image
    #    剔除并打 ERROR（废都南方工地的 route1.png 此刻就是废线：17 像素、跨度 8px）。
    #    那是**数据问题**不是代码问题，让用例一直红着会掩盖真正的回归。
    #    这里只在真的没加载到任何主线时提醒一句。
    if len(b.img_routes) == 0:
        print("       ⚠️ 主线段数为 0 —— 这张图的 route*.png 全被判废了（见上方 ERROR），"
              "需要重录。这是数据问题，不是加载逻辑坏了。")
    # 回正线不能混进主线段里（否则会被当普通路线来回走）
    # ⚠️ 不能直接 `img_route_home in img_routes` —— 那是 numpy 数组的
    # 逐元素比较、返回 array，if array 会报"truth value ambiguous"。
    # 用身份比较：回正线图像是**另一个对象**，不会等于任何主线图像。
    in_main = (b.img_route_home is not None
               and any(r is b.img_route_home for r in b.img_routes))
    check("回正线没混进 img_routes", in_main, False)

    print("\n【4】self.cfg 已在末尾赋值（否则运行时全崩）")
    check("self.cfg 不为 None", b.cfg is not None, True)
    check("self.cfg 就是传入的 cfg", b.cfg is cfg, True)

    print("\n【5】v0.9 回正线新配置项齐全，且 home_timeout < watchdog.timeout")
    # ⚠️ 为什么必须断言这两件事：
    #   · 缺键 → 引擎里所有 home_params() 走默认值，配置调了不生效（静默），
    #     而且 _check_cfg_completeness 会把「配置过期」报成 ERROR 刷屏；
    #   · home_timeout >= watchdog.timeout → 回正接不上时，看门狗会**先**抽随机动作
    #     乱走，把正在进行的回正打断，"超时出口"形同虚设（设计 11-②）。
    from src.utils.home_route import (DEFAULT_DRAW_PARAMS, DEFAULT_HOME_PARAMS,
                                      home_params)
    route_cfg = cfg.get("route") or {}
    rec_cfg = cfg.get("route_recoder") or {}
    missing = [k for k in DEFAULT_HOME_PARAMS if k not in route_cfg]
    missing += [f"route_recoder.{k}" for k in DEFAULT_DRAW_PARAMS if k not in rec_cfg]
    check("回正/手绘配置项一个都不缺", missing, [])
    # ── 落点横段「三态分区」的跳跃区间参数（2026-09-14 加）──────────────────
    # ⚠️ 为什么也在这里验：这两个键只**手绘保存**时用一次，引擎运行期不读，
    #    缺了不会崩、只会静默回落到老的「汇聚到单拐角」行为（震荡 bug 原样回来）
    #    —— 正是最难查的那类"配了不生效"。
    from src.utils.home_route import (DEFAULT_JUMP_BAND_PARAMS, jump_band_params)
    missing_jb = [k for k in DEFAULT_JUMP_BAND_PARAMS if k not in route_cfg]
    check("★ 跳跃区间（jump band）配置项一个都不缺", missing_jb, [])
    _jb = jump_band_params(cfg)
    print(f"       home_jump_band_max_drop={_jb['home_jump_band_max_drop']}px  "
          f"home_jump_band_inset={_jb['home_jump_band_inset']}px")
    check("★ 跳跃区间默认值 = (8, 5)",
          (_jb["home_jump_band_max_drop"], _jb["home_jump_band_inset"]), (8, 5))
    # ⚠️ 负数回落的也是 **5**（_JUMP_BAND_FLOORS 的回落默认与 config 默认同值）：
    #    回落成 1 = 把「跳跃区几乎铺满横段」这个已修的坏值静默放回来。
    check("★ 填负数回落默认（不是『关掉』，也不是旧的 1）",
          (jump_band_params({"route": dict(route_cfg, home_jump_band_max_drop=-1,
                                           home_jump_band_inset=-1)})),
          {"home_jump_band_max_drop": 8, "home_jump_band_inset": 5})
    check("★ 跳跃区间参数**不混进** DEFAULT_HOME_PARAMS（那边条数有断言钉着）",
          [k for k in DEFAULT_JUMP_BAND_PARAMS if k in DEFAULT_HOME_PARAMS], [])
    # ── v0.9 第二版：落点锚整块删掉，相关的键必须**一个都不剩** ──────────────
    # ⚠️ 为什么要反向断言"键不存在"：留着旧键，用户照着旧教程去改它，改了不生效
    #    也没报错（引擎根本不读），属于最难查的那类"静默失效"。
    #    127,0,127 这个色本身也要从**两张**色码表里彻底消失（旧 PNG 里还有的话，
    #    load_config 会 wipe_legacy_anchor 抹掉并打日志）。
    from src.utils.home_route import LEGACY_ANCHOR_RGB, color_code_maps
    dead_keys = ["color_code_home_anchor", "home_anchor_radius",
                 "home_anchor_clearance", "home_anchor_min_clearance",
                 "home_anchor_direction_check", "home_anchor_up_tol",
                 "home_anchor_rearm_margin", "home_anchor_rearm_timeout",
                 "home_return_range", "home_return_min"]
    left = [k for k in dead_keys if k in route_cfg]
    check("★ 落点锚 / 有效半径那批旧键已全部删除", left, [])
    _cc_tbl, _ud_tbl = color_code_maps(cfg)
    check("★ 127,0,127 不在任何一张色码表里",
          LEGACY_ANCHOR_RGB in (set(_cc_tbl) | set(_ud_tbl)), False)
    check("★ 也只有两张色码表（色码 + 上下行），没有第三张锚点表",
          len(color_code_maps(cfg)), 2)
    home_timeout = route_cfg.get("home_timeout")
    wd_timeout = (cfg.get("watchdog") or {}).get("timeout")
    print(f"       home_timeout={home_timeout}s  watchdog.timeout={wd_timeout}s")
    check("回正超时一定**先于**看门狗（超时出口才有效）", home_timeout < wd_timeout, True)
    # ⚠️ 冷静期（QA 实测必修）：回正失败退出后，「主线附近找不到像素」这条老入口
    #    要静默 home_reenter_lock 秒。配成 0 = 关掉 → 超时退出那一帧会被老路径
    #    **同帧**拉回回正，home_enter_t 归零 → 8s 超时永远走不到 → 既不走也不打。
    lock = route_cfg.get("home_reenter_lock")
    print(f"       home_reenter_lock={lock}s（回正失败后的冷静期）")
    check("回正失败冷静期 > 0（=0 会让超时出口被老路径同帧抵消）",
          isinstance(lock, (int, float)) and lock > 0, True)
    # ── 配置不变式：去武装兜底重新武装 必须 **晚于** 回正失败的冷静期 ──────────
    # ⚠️ 为什么必须硬拦（缺了它，已修的 QA-20 死锁会**复活**）：
    #   · home_rearm_timeout（默认 30s）：回正线去武装后无条件重新武装的兜底。
    #     没有它，角色一直在坑底晃悠时这条线永远武装不回来
    #     （= 这条回正线被静默删除，另一种死法）。
    #   · home_reenter_lock（默认 10s）：回正**失败**后的入口冷静期（D1）。
    #   如果 rearm_timeout <= reenter_lock，兜底会在冷静期**还没过**时就重新武装
    #   → 「8s 超时 → 立刻重进」的环被接上一半 → QA-20 那个死锁复活：
    #   8s 超时走 _exit_home_route 之后，同一帧又被拉回回正，home_enter_t 每帧
    #   归零、回正期间不打怪 = **既不走也不打**。
    #
    #   现在是 30 > 10 侥幸成立，但**用户手改配置就能踩到**，而且踩到时不报错、
    #   只是偶发卡死 —— 属于最难查的那类问题，所以必须硬拦而不是靠注释。
    #
    #   ⚠️ 用 home_params() 取**有效值**（缺键 / 填 0 都回落默认，D4），与引擎实际
    #      跑的口径一致：只比对 cfg 原始值的话，用户删键或填 0 就会绕过这条断言。
    _hp = home_params(cfg)
    _rearm = _hp["home_rearm_timeout"]
    _lock_eff = _hp["home_reenter_lock"]
    print(f"       home_rearm_timeout={_rearm}s（去武装兜底）"
          f"  vs  home_reenter_lock={_lock_eff}s（冷静期）")
    check("★ 去武装兜底重新武装 **晚于** 冷静期（否则 QA-20 死锁复活）",
          _rearm > _lock_eff, True)
    # ── D4：这几个"防死锁"参数**关不掉**（填 0 / 负数要回落成默认值）────────────
    # ⚠️ 为什么硬拦：8s 超时、10s 冷静期、30s 兜底这三个是防死锁的，
    #    用户嫌慢填 0 想"关掉" → 死锁原样回来（v1 已验证过一次）。
    #    所以 home_params() 必须把 0 / 负数回落成默认，这里把回落结果验出来。
    _probe = {"route": dict(route_cfg)}
    for _k, _v in (("home_timeout", 0), ("home_reenter_lock", -1),
                   ("home_rearm_timeout", 0)):
        _probe["route"][_k] = _v
        check(f"★ D4：{_k} 填 {_v} 会回落成默认 "
              f"{DEFAULT_HOME_PARAMS[_k]}（关不掉）",
              home_params(_probe)[_k], DEFAULT_HOME_PARAMS[_k])
    check("★ D4：home_dist_tol 填负数也回落成默认 2",
          home_params({"route": dict(route_cfg, home_dist_tol=-9)})["home_dist_tol"],
          DEFAULT_HOME_PARAMS["home_dist_tol"])
    check("★ home_dist_tol ≥ 0（相等时主线优先，不能是负的）",
          _hp["home_dist_tol"] >= 0, True)
    # ── v0.9 第二版：回正线解析成"线"，不再是"锚点" ──────────────────────────
    check("★ 引擎字段已从 home_anchors 换成 home_lines",
          isinstance(getattr(b, "home_lines", None), list), True)
    check("★ 旧字段 home_anchors / home_anchor 已不存在",
          (hasattr(b, "home_anchors") or hasattr(b, "home_anchor")), False)
    print(f"       本图回正线 {len(getattr(b, 'home_lines', []) or [])} 条"
          f"（废都南方工地现网那条是废线 → 期望 0）")
    for _ln in getattr(b, "home_lines", []) or []:
        print(f"         #{_ln['id']} 起点 {_ln['start']} → 终点 {_ln['goal']}，"
              f"{_ln['n_pts']} px，{'含跳跃' if _ln['has_jump'] else '无跳跃'}，"
              f"落点覆盖 x{_ln['cover_xs'][0]}~{_ln['cover_xs'][1]}")

    print("\n【6】★ 同进程热重载 + 段数变少 ⇒ 段号必须被钳回（2026-10-14 真机停摆回归）")
    # ⚠️ 复现的是这条真机链路（**两次**停摆：累计失败 149 帧 / 296 帧，角色彻底不动）：
    #    界面【开始】(src/ui/AutoBotController.py:109) 在**同一个进程里**重跑
    #    load_config → img_routes 被换成更短的列表（重录成更少段 / 有段被判废）
    #    → idx_routes 还停在上一轮的值 → run_once 里按下标取 route ⇒ IndexError
    #    ⇒ 崩溃点排在「路线推进」之前 ⇒ **每帧都炸**。
    #    tools/verify_route_index_bounds.py 只能钉住单元级的钳位判据，真资源这一层
    #    （load_config 真的会重建列表 + 真的会调钳位）只能在这里补。
    if len(b.img_routes) >= 3:
        import glob as _glob

        import src.engine.MapleStoryAutoLevelUp as _E
        _files = sorted(_glob.glob(os.path.join("minimaps", map_name, "route*.png")))
        _files = [p for p in _files
                  if not p.endswith("route_rest.png") and not p.endswith("route_home.png")]
        _keep = _files[:2]
        _real_glob = _glob.glob

        def _fake_glob(pattern, *a, **k):
            if "route" in str(pattern).replace("\\", "/"):
                return list(_keep)
            return _real_glob(pattern, *a, **k)

        _old_idx = len(b.img_routes) - 1
        b.idx_routes = _old_idx
        _E.glob.glob = _fake_glob
        try:
            _ret = b.load_config(cfg)
        finally:
            _E.glob.glob = _real_glob
        print(f"       热重载：{_old_idx + 1} 段 → {len(b.img_routes)} 段，"
              f"段号 {_old_idx} → {b.idx_routes}")
        check("★ 热重载没抛异常", _ret, 0)
        check("★ 段号被钳回合法范围（否则主循环每帧 IndexError）",
              0 <= b.idx_routes < len(b.img_routes), True)
        # 反证：修复前那一行在同样状态下**必须**抛 —— 不抛说明夹具不成立（用例是假的）
        try:
            _ = b.img_routes[_old_idx]
            check("★ 反证：旧段号在新列表上越界", "没抛", "IndexError")
        except IndexError:
            check("★ 反证：旧段号在新列表上越界", "IndexError", "IndexError")
        # 还原成真实资源：下面收尾与人工排查看到的状态必须是真实的
        b.load_config(cfg)
    else:
        print(f"       ⚠️ 跳过：本图只有 {len(b.img_routes)} 段，做不了「段数变少」的复现"
              f"（数据不足，不是代码问题）")

    print("\n" + "=" * 60)
    if FAIL:
        print(f"❌ 自检失败 {len(FAIL)} 项：")
        for n in FAIL:
            print(f"   - {n}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
