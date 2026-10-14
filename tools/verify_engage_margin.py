# -*- coding: utf-8 -*-
"""📏 宽判定余量自检（`monster_detect.mob_engage_margin`，CI 可跑，2026-10-14 加）。

## 为什么会有这个键

用户 m07843「已经录制完毕」→ 示范录制（6262 帧 / 226.3s）读数：
  · 人出手 2232 帧（35.6%） vs 引擎出招 579 帧（9.2%）；
  · 其中 **389 帧**是「引擎**看见了**怪、却判够不着」，最近那只怪
    **69% 只差在框外 ≤ 45px**（dx p50=6 / p90=45；dy p50=0 / p90=42）。

用户 m06810 原话：「人不能准确计算，只能预估大概在范围内就会攻击，属于宽判定」；
m08016 拍板 **B：够得着判据加余量 40px**（不选 C 抗丢帧）。本文件钉死这个键。

## 这个自检盯的是什么

  ① 取值真值表 —— `_engage_margin()` 是**全文件唯一读点**：
     缺键 ⇒ 40（老配置文件没有这个键时也照 40 走，否则"改了看不出变化"）；
     畸形 / 负数 / > 200 ⇒ 0（保守：绝不因为配置写坏而静默放宽）；
  ② `_attack_boxes()` 的**外扩几何**：aoe 一个框 / directional 两个框；
     `m=0` 时必须与 `get_attack_range()` 的输出**逐字节一致**（关掉 = 完全老行为）；
  ③ **只放大判定框**：屏幕上的框由 `get_attack_range()` 画，一个像素都不许动；
  ④ 三个判定/观测消费点（`_mobs_in_attack_box` / `_engage_gap` / `_log_monster_detect`）
     必须走**同一个框** —— 否则"日志说差 6px、判定却在拿加过余量的框算"；
  ⑤ 真行为：中心差 30px 的怪在 `m=0` 不算、`m=40` 算（这是用户能看到的那点收益）；
  ⑥ 配置实体 / 中文名 / 界面控件往返 / 明细行文案 —— 一个都不许静默降级。

用法：python -m tools.verify_engage_margin
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [OK]   {msg}')
    else:
        FAIL += 1
        print(f'  [FAIL] {msg}')


def check_eq(got, want, msg):
    check(got == want, f'{msg}：got={got!r} want={want!r}')


# ── 假引擎桩：把真方法绑上去，不跑 __init__（与 tools/verify_mob_sight.py 同一套思路）──
def make_stub(cfg, mode="center", need=868, attack_box=(400, 300, 460, 340),
              boxes=None):
    import src.engine.MapleStoryAutoLevelUp as _mod

    class _S:
        pass

    s = _S()
    for _n in ('_engage_margin', '_attack_boxes', '_mobs_in_attack_box',
               '_engage_mode', '_min_mob_area', '_log_monster_detect'):
        if hasattr(_mod.MapleStoryAutoBot, _n):
            setattr(s, _n,
                    getattr(_mod.MapleStoryAutoBot, _n).__get__(s, _S))
    s.cfg = cfg
    s.monsters = []
    s.loc_player = (100, 200)
    s._engage_mode = lambda: mode
    s._min_mob_area = lambda: need
    if boxes is None:
        s.get_attack_range = lambda is_left=True: attack_box
    else:
        s.get_attack_range = lambda is_left=True: boxes[0 if is_left else 1]
    # 日志类字段（`_log_monster_detect` 只有在被直接调用时才用得到）
    s.t_detect_log = 0.0
    s._det_log_sig = None
    s._det_log_failed = False
    s.loc_player_global = None
    return s


def mob(cx, cy, size=(40, 40), score=0.1):
    '''按引擎内的怪 dict 形状造一只怪（`size` 是 (宽,高)，见 2026-10-14 的真 bug 修复）。'''
    return {"position": (cx - size[0] // 2, cy - size[1] // 2),
            "size": size, "score": score}


print('=' * 72)
print(' 自检：📏 宽判定余量（mob_engage_margin）—— 只放大判定框，不动屏幕上的框')
print('=' * 72)

# ── ① 配置实体 ──────────────────────────────────────────────────────────────
print('\n[1] 配置实体：config/config_default.yaml 与中文名')
import yaml  # noqa: E402
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_root, "config", "config_default.yaml"),
          encoding="utf-8") as _f:
    _md = (yaml.safe_load(_f) or {}).get("monster_detect") or {}
check_eq(_md.get("mob_engage_margin"), 40,
         "★ 出厂默认 40（依据：示范录制 389 帧里 69% 只差 ≤ 45px）")
check_eq(_md.get("mob_engage_mode"), "center",
         "判据仍是 center（宽判定，2026-10-14 用户 m06810 拍板）")
from src.utils.cfg_names import cn_key  # noqa: E402
_cn = cn_key("monster_detect", "mob_engage_margin")
check(bool(_cn) and "余量" in str(_cn), f'中文名在（{_cn!r}）')

# ── ② `_engage_margin()` 取值真值表 ─────────────────────────────────────────
print('\n[2] `_engage_margin()` 真值表（唯一读点；缺键 ⇒ 40，畸形 ⇒ 0）')
_TABLE = [
    ({"monster_detect": {}}, 40, "缺键 ⇒ 40（老配置也照 40，免得「改了没变化」）"),
    ({"monster_detect": {"mob_engage_margin": None}}, 40, "None ⇒ 40"),
    ({"monster_detect": {"mob_engage_margin": 0}}, 0, "0 ⇒ 0（关掉 = 严格按技能框）"),
    ({"monster_detect": {"mob_engage_margin": 40}}, 40, "40 ⇒ 40"),
    ({"monster_detect": {"mob_engage_margin": 40.6}}, 40, "40.6 ⇒ 40（截断，不四舍五入）"),
    ({"monster_detect": {"mob_engage_margin": "40"}}, 40, '字符串 "40" ⇒ 40'),
    ({"monster_detect": {"mob_engage_margin": 200}}, 200, "200 ⇒ 200（上界）"),
    ({"monster_detect": {"mob_engage_margin": 201}}, 0, "201 ⇒ 0（保守，绝不放宽）"),
    ({"monster_detect": {"mob_engage_margin": -1}}, 0, "-1 ⇒ 0"),
    ({"monster_detect": {"mob_engage_margin": "abc"}}, 0, '畸形 "abc" ⇒ 0'),
    ({"monster_detect": {"mob_engage_margin": ""}}, 0, "空串 ⇒ 0"),
    ({}, 40, "整个 monster_detect 段都没有 ⇒ 40"),
]
for _cfg, _want, _why in _TABLE:
    _s = make_stub(_cfg)
    check_eq(_s._engage_margin(), _want, f'  {_why}')

# ── ③ `_attack_boxes()` 的外扩几何 ──────────────────────────────────────────
print('\n[3] `_attack_boxes()`：aoe 一框 / directional 两框；m=0 时逐字节等于原框')
_BOX_L = (400, 300, 460, 340)
_BOX_R = (140, 300, 200, 340)

_s = make_stub({"bot": {"attack": "aoe_skill"},
                "monster_detect": {"mob_engage_margin": 0}},
               attack_box=_BOX_L)
check_eq(_s._attack_boxes(), [_BOX_L], "aoe + m=0 ⇒ 与原框完全一致（关掉 = 老行为）")

_s = make_stub({"bot": {"attack": "aoe_skill"},
                "monster_detect": {"mob_engage_margin": 40}},
               attack_box=_BOX_L)
check_eq(_s._attack_boxes(), [(360, 260, 500, 380)],
         "★ aoe + m=40 ⇒ 四边各向外 40px")

_s = make_stub({"bot": {"attack": "directional"},
                "monster_detect": {"mob_engage_margin": 0}},
               boxes=(_BOX_L, _BOX_R))
check_eq(_s._attack_boxes(), [_BOX_L, _BOX_R],
         "directional + m=0 ⇒ 左右两框原样")

_s = make_stub({"bot": {"attack": "directional"},
                "monster_detect": {"mob_engage_margin": 40}},
               boxes=(_BOX_L, _BOX_R))
check_eq(_s._attack_boxes(), [(360, 260, 500, 380), (100, 260, 240, 380)],
         "directional + m=40 ⇒ 两个框各自外扩（不是取并集）")

_s = make_stub({"bot": {"attack": "directional"},
                "monster_detect": {"mob_engage_margin": "abc"}},
               boxes=(_BOX_L, _BOX_R))
check_eq(_s._attack_boxes(), [_BOX_L, _BOX_R],
         "配置写坏（畸形 ⇒ 0）时退回原框 —— 保守，绝不因为配置坏了而放宽")

# ── ④ 三个消费点走同一个框（源码级，防"日志和判定不一致"）────────────────────
print('\n[4] 接线白盒：判定 / 观测必须拿同一个框')
import inspect  # noqa: E402
import src.engine.MapleStoryAutoLevelUp as _mod  # noqa: E402

_ENG = os.path.join(_root, "src", "engine", "MapleStoryAutoLevelUp.py")
with open(_ENG, encoding="utf-8") as _f:
    _SRC = _f.read()

_src_mobs = inspect.getsource(_mod.MapleStoryAutoBot._mobs_in_attack_box)
_src_gap = inspect.getsource(_mod.MapleStoryAutoBot._engage_gap)
_src_log = inspect.getsource(_mod.MapleStoryAutoBot._log_monster_detect)
_src_boxes = inspect.getsource(_mod.MapleStoryAutoBot._attack_boxes)
_src_mar = inspect.getsource(_mod.MapleStoryAutoBot._engage_margin)

check("_attack_boxes()" in _src_mobs, "`_mobs_in_attack_box` 走 `_attack_boxes()`")
check("get_attack_range" not in _src_mobs,
      "★ `_mobs_in_attack_box` 里**不再**直接调 `get_attack_range`（否则会绕过余量）")
check("_attack_boxes()" in _src_gap, "`_engage_gap` 走 `_attack_boxes()`")
check("get_attack_range" not in _src_gap,
      "★ `_engage_gap` 里也不再直接调 `get_attack_range`（日志与判定同一框）")
check("_attack_boxes()[0]" in _src_log,
      "★ `_log_monster_detect` 拿的也是 `_attack_boxes()[0]`（明细行的框 = 判定的框）")
check("margin=self._engage_margin()" in _src_log,
      "明细行把余量数值一并打出来（用户能核对日志与界面旋钮一致）")
check("self._engage_margin()" in _src_boxes,
      "`_attack_boxes()` 是唯一拼框点（只读一次余量）")
check(_SRC.count("def _engage_margin(") == 1, "`_engage_margin` 只有一个定义")
# ⚠️ 别拿"键名出现次数"当判据：注释里也会提到它（本文件第一次写就踩了 —— 6 次里
#    5 次是注释）。真正要钉的是**读配置的那一句只有一处**。
check_eq(_SRC.count('"mob_engage_margin", 40'), 1,
         '★ 读配置的那一句（"mob_engage_margin", 40）全文件只有 1 处 = 唯一读点')

# ── ⑤ 真行为：中心差 30px 的怪 ───────────────────────────────────────────────
print('\n[5] 真行为：中心差 30px 的怪，m=0 不算、m=40 算（这就是那 69%）')
_BOX = (400, 300, 460, 340)               # 技能真实框：x 400~460, y 300~340
_MOB_FAR = mob(490, 320)                  # 中心 (490,320)：横向差 30px（贴边但没进框）
_MOB_IN = mob(430, 320)                   # 中心 (430,320)：本来就在框里

_s0 = make_stub({"bot": {"attack": "aoe_skill"},
                 "monster_detect": {"mob_engage_margin": 0}}, attack_box=_BOX)
_s0.monsters = [_MOB_FAR]
check_eq(len(_s0._mobs_in_attack_box()), 0,
         "m=0 时「中心差 30px」的怪**不算**够得着（老口径，看着在眼前却不打）")

_s40 = make_stub({"bot": {"attack": "aoe_skill"},
                  "monster_detect": {"mob_engage_margin": 40}}, attack_box=_BOX)
_s40.monsters = [_MOB_FAR]
check_eq(len(_s40._mobs_in_attack_box()), 1,
         "★ m=40 时同一只怪**算**够得着（宽判定生效）")

_s40.monsters = [_MOB_IN]
check_eq(len(_s40._mobs_in_attack_box()), 1, "本来就在框里的怪照旧算")

_s40.monsters = [mob(560, 320)]           # 中心差 100px：余量也救不了（该走的还是走）
check_eq(len(_s40._mobs_in_attack_box()), 0,
         "中心差 100px 的怪仍然不算 —— 余量不是无限放宽")

_s40.monsters = [_MOB_FAR, _MOB_IN]
check_eq(len(_s40._mobs_in_attack_box()), 2, "两只都算（不是只认第一只）")

# 边界：正好落在扩过之后的框边上 ⇒ 算（闭区间）
_s40.monsters = [mob(500, 320)]           # 中心 x = 500 = 扩后的右边
check_eq(len(_s40._mobs_in_attack_box()), 1,
         "中心正好落在扩后框边上 ⇒ 算（闭区间，差 1px 不再翻脸）")

# min_area 老口径下余量同样生效（有余量时交叠面积更大 ⇒ 更容易达标）
_s_min = make_stub({"bot": {"attack": "aoe_skill"},
                    "monster_detect": {"mob_engage_margin": 0}}, mode="min_area")
_s_min.monsters = [mob(490, 320)]
_n0 = len(_s_min._mobs_in_attack_box())
_s_min.cfg = {"bot": {"attack": "aoe_skill"},
              "monster_detect": {"mob_engage_margin": 40}}
_n40 = len(_s_min._mobs_in_attack_box())
check(_n0 == 0 and _n40 == 1,
      f"min_area 老口径下余量同样生效（m=0 ⇒ {_n0} 只 / m=40 ⇒ {_n40} 只）")

# ── ⑥ 界面控件 + 明细行文案 ─────────────────────────────────────────────────
print('\n[6] 界面控件（有旋钮才谈得上"用户能调"）与明细行文案')
from PySide6.QtWidgets import QApplication, QLineEdit  # noqa: E402
import src.ui.ui as U  # noqa: E402

_app = QApplication.instance() or QApplication([])   # noqa: F841  (offscreen)


class _Stub:
    pass


_obj = _Stub()
for _name in ('create_monster_gbox', '_apply_monster_ui', '_read_monster_ui'):
    setattr(_obj, _name, getattr(U.MainWindow, _name).__get__(_obj, _Stub))
_obj.cfg = {}
_gbox = U.MainWindow.create_monster_gbox(_obj)
check(hasattr(_obj, 'monster_engage_margin')
      and isinstance(_obj.monster_engage_margin, QLineEdit),
      "控件挂上了（monster_engage_margin 是 QLineEdit）")
_tip = _obj.monster_engage_margin.toolTip()
check("40" in _tip and "余量" in _tip,
      f"tooltip 写了默认值与含义（{_tip[:40]!r}…）")

_obj.cfg = {'monster_detect': {'mob_engage_margin': 0}}
U.MainWindow._apply_monster_ui(_obj)
check_eq(_obj.monster_engage_margin.text(), '0', "灌值：0（关掉）")
_obj.monster_engage_margin.setText('77')
_md_out = U.MainWindow._read_monster_ui(_obj)
check_eq(_md_out['mob_engage_margin'], 77, "读回：77（整数）")
_obj.monster_engage_margin.setText('77.0')
_md_out = U.MainWindow._read_monster_ui(_obj)
check_eq(_md_out['mob_engage_margin'], 77, '读回："77.0" ⇒ 77（允许小数点）')
_obj.monster_engage_margin.setText('999')
_md_out = U.MainWindow._read_monster_ui(_obj)
check_eq(_md_out['mob_engage_margin'], 200, "读回：999 ⇒ 夹到 200（与界面校验一致）")
_obj.monster_engage_margin.setText('abc')
_obj.cfg = {'monster_detect': {'mob_engage_margin': 55}}
_md_out = U.MainWindow._read_monster_ui(_obj)
check_eq(_md_out['mob_engage_margin'], 55,
         "非法输入 ⇒ 保留原值 55（绝不写 None / 绝不静默清零）")
_obj.cfg = {}
U.MainWindow._apply_monster_ui(_obj)
check_eq(_obj.monster_engage_margin.text(), '40',
         "缺键时界面兜底值 = 40（与出厂默认一致）")

with open(os.path.join(_root, "src", "ui", "ui.py"), encoding="utf-8") as _f:
    _UISRC = _f.read()
check("'mob_engage_margin'" not in _UISRC.split("ADV_FIELD_HIDE")[-1].split("}")[0]
      or "mob_engage_margin" not in _UISRC[
          _UISRC.find("ADV_FIELD_HIDE['monster_detect']"):
          _UISRC.find("ADV_FIELD_HIDE['monster_detect']") + 400],
      "★ `mob_engage_margin` **不在** ADV_FIELD_HIDE['monster_detect'] 里"
      "（有控件的键不许被收起 —— 收起 = 用户看不见自己的旋钮）")
check("validate_numerical_input" in _UISRC
      and "self.monster_engage_margin.text(), error_label, 0, 200" in _UISRC,
      "输入框挂了 0~200 校验（手填 999 会被界面挡下）")

from src.utils.common import monster_detect_snapshot  # noqa: E402
_line0 = monster_detect_snapshot([], [], box=_BOX, mode="center", need=868,
                                 margin=0, player=(100, 200))
check("宽判定余量" not in _line0, f'margin=0 时明细行不提余量（{_line0[:60]!r}…）')
_line40 = monster_detect_snapshot([], [], box=_BOX, mode="center", need=868,
                                  margin=40, player=(100, 200))
check("宽判定余量" in _line40 and "40" in _line40 and "屏幕上画的仍是技能真实框" in _line40,
      f'★ margin=40 时明细行写明"这个框已外扩 40px、屏幕上的框没变"：{_line40[:90]!r}…')

print('\n' + '=' * 72)
print(f' 结果：通过 {PASS} / 失败 {FAIL}')
print('=' * 72)
sys.exit(1 if FAIL else 0)
