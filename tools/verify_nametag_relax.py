# -*- coding: utf-8 -*-
'''名字定位「抗闪」自检（2026-10-14 加）。

背景（用户真机日志 `log/MSBot_2026-10-14_11-42-16.log`）：
    名字匹配分是**双峰**的 —— 认得出 0.0000~0.0099、认不出 0.0220~0.0240、
    已知假阳性 0.0333~0.0466（`config/config_default.yaml` 的标定注释）。
    0.022~0.024 这一档当时判为"名牌被挡了一部分"，本来无害；有害的是连锁反应：
    miss 计数涨过 `stale_pos_full_frame_after`(该键 2026-10-14 最终定为 **0 = 关**)
    ⇒ **索敌框放大到整帧** ⇒ 怪数虚高 + 去追画面外 550px 的目标。

★ 2026-10-14 重标模板后的复测（引擎自己的口径、真机抓帧；明细在
  config/config_default.yaml 的 nametag 注释里）：
    真位置分 **0.0000~0.0016**，而**同一张图上"另一处最像的地方" 0.0232~0.0265**。
    ⇒ 上面那档 0.022~0.024 与假阳性档重叠，多半本来就是**错的位置**（不是"被挡住的
      名牌"）；所以 `relax_ratio` 由 2.5 收紧到 **2.0**（放宽上限 0.020 < 0.0232），
      并把 `stale_pos_full_frame_after` 的临时护栏 **25 撤回 10**（模板贴合后
      miss 涨不到 10）；**当晚用户再拍板把它设成 0 = 彻底关掉整帧放大**
      （12:43 那轮 4.6 分钟名字告警 33 条、miss 最高 71 ⇒ 放大被反复触发，
       满屏怪 + 追 400~550px 外的目标；先关掉副作用，改用 `nametag.miss_dump`
       存画面去查"为什么丢名字"）。
    ⇒ 本文件的夹具分数随之改成新口径：**采信档用 0.0150**（落在 0.010~0.020 之间）、
      拒绝档用 0.0232 及以上（含原来的 0.0333 / 0.0466）。
      纯函数真值表仍用 ratio=2.5 做**显式传入**的用例（它测的是函数本身，
      不受配置默认值变化影响）。

做法：判据收在 `src/utils/common.py: nametag_relax_decision()` 一处，
      引擎只负责"用它 + 打日志"，且放宽采信的帧**不进锚点、不写存盘**。

本文件钉四件事：
  【1】纯函数真值表（含 fail-closed 边界）
  【2】真机分数回归：半峰档采信、**已知假阳性档仍然拒绝**
  【3】配置 / 中文名 / 界面隐藏清单都登记了
  【4】引擎接线白盒：判据只调一次、锚点只由严格命中更新、放宽不落盘
'''
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.utils.common import nametag_relax_decision   # noqa: E402

_FAIL = 0
_N = 0


def check(name, got, want):
    global _FAIL, _N
    _N += 1
    if got == want:
        print(f"  [OK ] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}: got={got!r} want={want!r}")


def check_true(name, cond):
    check(name, bool(cond), True)


def code_only(src):
    '''剥掉整行/行尾注释 —— 注释里引用函数名会把白盒计数顶高（踩过的坑）。'''
    out = []
    for ln in src.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        i = ln.find("#")
        if i >= 0:
            ln = ln[:i]
        out.append(ln)
    return "\n".join(out)


def _engine_src():
    p = os.path.join(_ROOT, "src", "engine", "MapleStoryAutoLevelUp.py")
    with open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _method_src(src, name):
    '''从 "def name(" 切到下一个同级 def（保持调用点白盒判据不受别处干扰）。'''
    i = src.find(f"    def {name}(")
    if i < 0:
        return ""
    j = src.find("\n    def ", i + 10)
    return src[i:j if j > 0 else len(src)]


def main():
    # ── 【1】纯函数真值表 ────────────────────────────────────────────────
    print("\n【1】nametag_relax_decision 真值表（thres=0.01 / ratio=2.5 / jump=12）")
    _ok, _why, _d = nametag_relax_decision(0.0220, 0.01, 2.5, 12, (500, 400), (500, 400))
    check("① 半峰 + 位置不动 ⇒ 采信", _ok, True)
    check("① 距离算出来是 0", _d, 0)
    check_true("① 理由里带了实测分数", "0.0220" in _why)
    _ok, _why, _d = nametag_relax_decision(0.0220, 0.01, 2.5, 12, (512, 400), (500, 400))
    check("② 偏 12px（正好在上限）⇒ 采信（≤ 不是 <）", _ok, True)
    check("② 距离 = 12", _d, 12)
    _ok, _why, _d = nametag_relax_decision(0.0220, 0.01, 2.5, 12, (513, 400), (500, 400))
    check("③ 偏 13px ⇒ 拒绝", _ok, False)
    check_true("③ 理由里写清了两个数（13 与 12）", "13px" in _why and "12" in _why)
    _ok, _why, _d = nametag_relax_decision(0.0300, 0.01, 2.5, 12, (500, 400), (500, 400))
    check("④ 0.0300 超过放宽上限 0.025 ⇒ 拒绝", _ok, False)
    check_true("④ 理由说清是「连上限都没过」", "上限" in _why)
    check("⑤ ratio=1.0 ⇒ 功能关闭", nametag_relax_decision(0.022, 0.01, 1.0, 12, (500, 400), (500, 400))[0], False)
    check("⑥ ratio=0.5（<1）⇒ 也是关闭", nametag_relax_decision(0.022, 0.01, 0.5, 12, (500, 400), (500, 400))[0], False)
    _ok, _why, _ = nametag_relax_decision(0.022, 0.01, 2.5, 12, (500, 400), None)
    check("⑦ 还没有严格命中（anchor=None）⇒ 拒绝", _ok, False)
    check_true("⑦ 理由说清是缺锚点", "锚点" in _why)
    check("⑧ cand=None ⇒ 拒绝", nametag_relax_decision(0.022, 0.01, 2.5, 12, None, (500, 400))[0], False)
    check("⑨ 分数正好等于上限 0.025 ⇒ 拒绝（fail-closed，要严格小于）",
          nametag_relax_decision(0.025, 0.01, 2.5, 12, (500, 400), (500, 400))[0], False)
    check("⑩ max_jump=0：位置相同 ⇒ 采信",
          nametag_relax_decision(0.022, 0.01, 2.5, 0, (500, 400), (500, 400))[0], True)
    check("⑩ max_jump=0：偏 1px ⇒ 拒绝",
          nametag_relax_decision(0.022, 0.01, 2.5, 0, (501, 400), (500, 400))[0], False)
    check("⑪ 分数/阈值给字符串也能用", nametag_relax_decision("0.022", "0.01", 2.5, 12, (500, 400), (500, 400))[0], True)
    check("⑫ max_jump 给负数 ⇒ 拒绝（不崩，fail-closed）",
          nametag_relax_decision(0.022, 0.01, 2.5, -1, (500, 400), (500, 400))[0], False)
    _ok, _why, _ = nametag_relax_decision(None, 0.01, 2.5, 12, (500, 400), (500, 400))
    check("⑬ 分数是 None ⇒ 拒绝", _ok, False)
    check_true("⑬ 理由说「不可用」而不是抛异常", "不可用" in _why)
    check("⑬ 浮点位置先取整再算距离（512.7→512，距锚点 12，仍在上限内）",
          nametag_relax_decision(0.022, 0.01, 2.5, 12, (512.7, 400.2), (500, 400))[2], 12)

    # ── 【2】真机分数回归 ───────────────────────────────────────────────
    print("\n【2】真机分数回归（2026-10-14 重标模板后：真位置 0.0000~0.0016、"
          "假阳性档 0.0232~0.0265）")
    _A = (500, 400)
    for _s, _want, _tag in ((0.0150, True, "落在放宽区间内（0.010~0.020）"),
                            (0.0232, False, "实测**假阳性档低端**"),
                            (0.0333, False, "配置注释里记录的已知假阳性低端"),
                            (0.0466, False, "已知假阳性高端")):
        _ok = nametag_relax_decision(_s, 0.01, 2.0, 12, _A, _A)[0]
        check(f"{_tag}：{_s:.4f} ⇒ {'采信' if _want else '拒绝'}", _ok, _want)
    check("边界：正好等于上限 0.0200 ⇒ 拒绝（判据是严格小于）",
          nametag_relax_decision(0.0200, 0.01, 2.0, 12, _A, _A)[0], False)
    check_true("★ 默认放宽上限（0.01×2.0=0.020）**低于**实测假阳性档 0.0232",
               not nametag_relax_decision(0.0232, 0.01, 2.0, 12, _A, _A)[0])
    check_true("★ 放宽**没有**把已知假阳性档（0.033~0.047）放进来",
               not nametag_relax_decision(0.0333, 0.01, 2.5, 12, _A, _A)[0]
               and not nametag_relax_decision(0.0466, 0.01, 2.5, 12, _A, _A)[0])
    check_true("严格命中档（0.0009 / 重标后实测 0.0000~0.0016）本来就走 diff_thres，"
               "不依赖放宽", 0.0016 < 0.01)

    # ── 【3】配置 / 中文名 / 界面登记 ────────────────────────────────────
    print("\n【3】配置、中文名与界面隐藏清单")
    import yaml
    with open(os.path.join(_ROOT, "config", "config_default.yaml"), encoding="utf-8") as fh:
        _cfg = yaml.safe_load(fh)
    _nt = (_cfg.get("nametag") or {})
    check("① nametag.relax_ratio 默认 2.0（2026-10-14 重标模板后由 2.5 收紧："
          "上限须低于实测假阳性档 0.0232）", _nt.get("relax_ratio"), 2.0)
    check("② nametag.relax_max_jump 默认 12", _nt.get("relax_max_jump"), 12)
    with open(os.path.join(_ROOT, "config", "config_default.yaml"), encoding="utf-8") as fh:
        _raw = fh.read()
    check_true("③ 注释里写明了怎么关（1.0 = 逐字退回旧行为）", "relax_ratio 设为 1.0" in _raw)
    check_true("③ 注释里写明了「放宽采信的帧不写存盘」", "不写** nametag_pos.json" in _raw)
    from src.utils.cfg_names import KEY_CN
    check_true("④ cfg_names 有 relax_ratio 的中文名", bool(KEY_CN.get("relax_ratio")))
    check_true("④ cfg_names 有 relax_max_jump 的中文名", bool(KEY_CN.get("relax_max_jump")))
    _ui = open(os.path.join(_ROOT, "src", "ui", "ui.py"), encoding="utf-8", errors="replace").read()
    _m = _ui[_ui.find("ADV_FIELD_HIDE = {"):]
    _m = _m[:_m.find("\n}")]
    _nt_hide = _m[_m.find("'nametag':"):]
    _nt_hide = _nt_hide[:_nt_hide.find("],")]
    check_true("⑤ ui.py ADV_FIELD_HIDE['nametag'] 收起了 relax_ratio", "relax_ratio" in _nt_hide)
    check_true("⑤ ui.py ADV_FIELD_HIDE['nametag'] 收起了 relax_max_jump", "relax_max_jump" in _nt_hide)

    # ── 【4】引擎接线白盒 ───────────────────────────────────────────────
    print("\n【4】引擎接线白盒（判据只一处、锚点只由严格命中更新）")
    _src = _engine_src()
    _code = code_only(_src)
    check_true("① 命令行 import 里加了 nametag_relax_decision",
               "    nametag_relax_decision," in _code)
    _init = _method_src(_code, "__init__")
    check_true("② __init__ 里有 self._nt_strict_pos = None", "self._nt_strict_pos = None" in _init)
    check_true("② __init__ 里有 self.nametag_relaxed = False", "self.nametag_relaxed = False" in _init)
    check_true("② __init__ 里有 self.nametag_relax_hits = 0", "self.nametag_relax_hits = 0" in _init)
    _load = _method_src(_code, "_load_nametag_pos")
    check_true("③ 存盘锚点直接当抗闪锚点（开局头几帧也能采信）",
               "self._nt_strict_pos = (int(cand[0]), int(cand[1]))" in _load)
    _loc = _method_src(_code, "get_player_location_by_nametag")
    check("④ 判据只调用一次", _loc.count("nametag_relax_decision("), 1)
    check_true("⑤ 只在「严格判定没过」时才问放宽", "if not _accept:" in _loc)
    check_true("⑥ fail-closed：放宽候选落在画面外也不采信",
               "if _relax and not _plausible:" in _loc)
    check_true("⑦ 每次调用都先把 nametag_relaxed 复位", "self.nametag_relaxed = False" in _loc)
    check_true("⑧ 放宽采信时计数 +1", "self.nametag_relax_hits += 1" in _loc)
    check_true("⑨ ★ 存盘被「非放宽」包住（放宽不进锚点、不落盘）",
               "if not self.nametag_relaxed:" in _loc and "_save_nametag_pos(" in _loc)
    _i_save = _loc.find("if not self.nametag_relaxed:")
    _i_anchor = _loc.find("self._nt_strict_pos = (int(_cand[0])")
    check_true("⑨ ★ 锚点更新也在那个分支里（在存盘前后 200 字符内）",
               _i_save > 0 and _i_anchor > _i_save and (_i_anchor - _i_save) < 200)
    check_true("⑩ 放宽采信走的还是同一个 accept 分支（miss 计数照样归零）",
               "self.nametag_miss_streak = 0" in _loc[_loc.find("if _accept:"):])
    check_true("⑪ 放宽分支自己打一条 INFO 日志（不许静默）", "名字定位·抗闪" in _loc)

    # ── 【5】锚点不漂移（组合语义）────────────────────────────────────────
    print("\n【5】锚点不漂移：放宽采信不更新锚点，一串小幅偏移不会把位置带跑")
    _anchor = (500, 400)
    _seq = [(500, 400), (508, 400), (516, 400), (520, 400)]
    _accepted = [nametag_relax_decision(0.022, 0.01, 2.5, 12, c, _anchor)[0] for c in _seq]
    check("① 从严格命中锚点出发，一路小幅偏移的采信序列", _accepted, [True, True, False, False])
    check_true("① 说明：第 3 帧离**锚点** 16px > 12 ⇒ 拒绝（位置链式带跑在这里被掐断）",
               _accepted[2] is False)
    if True:
        # 反例：如果错误地"用上一帧采信位置当锚点"，同一串会被全部采信 ⇒ 位置一路漂走
        _wrong = []
        _a = _anchor
        for _c in _seq:
            _ok = nametag_relax_decision(0.022, 0.01, 2.5, 12, _c, _a)[0]
            _wrong.append(_ok)
            if _ok:
                _a = _c
        check_true("② 反例（锚点跟着放宽帧走）会把 4 帧全部放行 —— 我们**不是**这么做的",
                   _wrong == [True, True, True, True] and _accepted != _wrong)

    # ── 【6】行为测试：真跑 get_player_location_by_nametag()（匹配器打桩）─────
    print("\n【6】行为测试：真跑 get_player_location_by_nametag()，看 miss 计数与落盘")
    import copy
    import numpy as np
    import src.engine.MapleStoryAutoLevelUp as eng
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

    with open(os.path.join(_ROOT, "config", "config_default.yaml"), encoding="utf-8") as fh:
        _cfg = copy.deepcopy(yaml.safe_load(fh))
    _cfg["nametag"]["split_width_fallback"] = 0      # 只跑一遍（整块），让分数可控
    # 匹配器返回的是"加了 pad 之后"的模板左上角；pad = 模板 h/w = (20,60)
    # ⇒ loc=(160,70) 对应 loc_nametag=(100,50)，角色位置 cand=(130,20)
    _state = {"score": 0.0020, "loc": (160, 70)}
    _real_fps = eng.find_pattern_sqdiff
    eng.find_pattern_sqdiff = lambda *a, **k: (_state["loc"], _state["score"], True)

    def _mk(anchor, streak=7):
        b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
        b.cfg = _cfg
        b.img_frame_gray = np.zeros((_cfg["ui_coords"]["ui_y_start"], 400), np.uint8)
        b.img_nametag_gray = np.zeros((20, 60), np.uint8)
        b.img_nametag = np.zeros((20, 60, 3), np.uint8)
        b.is_first_frame = True
        b.loc_nametag = (100, 50)
        b.nametag_miss_streak = streak
        b.nametag_hit = False
        b.nametag_ever_hit = True
        b._nt_strict_pos = anchor
        b.nametag_relaxed = False
        b.nametag_relax_hits = 0
        b._nt_score_hist = []
        b.saves = []
        b._save_nametag_pos = lambda **kw: (b.saves.append(kw), True)[1]
        return b

    try:
        _state["score"] = 0.0020
        _b1 = _mk(None)
        _b1.get_player_location_by_nametag()
        check("① 严格命中：miss 计数归零", _b1.nametag_miss_streak, 0)
        check("① 严格命中：锚点 = 角色位置 (130,20)", _b1._nt_strict_pos, (130, 20))
        check("① 严格命中：写了存盘", len(_b1.saves), 1)
        check("① 严格命中：不打放宽标记", _b1.nametag_relaxed, False)

        _state["score"] = 0.0150
        _b2 = _mk(_b1._nt_strict_pos)
        _b2.get_player_location_by_nametag()
        check("② 放宽档 + 锚点就在附近 ⇒ miss 计数照样归零", _b2.nametag_miss_streak, 0)
        check("② 放宽档：判为命中", _b2.nametag_hit, True)
        check("② 放宽档：打了放宽标记", _b2.nametag_relaxed, True)
        check("② 放宽档：放宽计数 +1", _b2.nametag_relax_hits, 1)
        check("② ★ 放宽档：**不写存盘**", len(_b2.saves), 0)
        check("② ★ 放宽档：锚点没被放宽帧改掉", _b2._nt_strict_pos, _b1._nt_strict_pos)

        _b3 = _mk((_b1._nt_strict_pos[0] + 40, _b1._nt_strict_pos[1]))
        _b3.get_player_location_by_nametag()
        check("③ 放宽档但位置偏 40px ⇒ 仍算未命中（miss 继续涨）", _b3.nametag_miss_streak, 8)
        check("③ 位置跳了就不放宽", _b3.nametag_relax_hits, 0)
        check("③ 位置跳了就不放宽", _b3.nametag_relax_hits, 0)

        _state["score"] = 0.0333
        _b4 = _mk(_b1._nt_strict_pos)
        _b4.get_player_location_by_nametag()
        check("④ 已知假阳性档 0.0333 ⇒ 仍算未命中", _b4.nametag_miss_streak, 8)
        check("④ 假阳性档不放宽", _b4.nametag_relax_hits, 0)

        _state["score"] = 0.0030
        _b2.get_player_location_by_nametag()
        check("⑤ 放宽之后再来一帧严格命中：锚点更新、存盘 1 次",
              (_b2._nt_strict_pos, len(_b2.saves)), ((130, 20), 1))

        _state["loc"] = (10, 10)        # 加 pad 之后算出画面外的位置
        _state["score"] = 0.0020
        _b5 = _mk(_b1._nt_strict_pos)
        _b5.get_player_location_by_nametag()
        check("⑥ 假命中护栏优先：位置落在画面外 ⇒ 分数再好也不算命中",
              _b5.nametag_miss_streak, 8)
        check("⑥ 画面外不写存盘", len(_b5.saves), 0)
    finally:
        eng.find_pattern_sqdiff = _real_fps

    print("\n" + "=" * 68)
    if _FAIL:
        print(f"结果：通过 {_N - _FAIL} / 失败 {_FAIL}")
        print("=" * 68)
        return 1
    print(f"✅ 全部通过（{_N} 项）")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
