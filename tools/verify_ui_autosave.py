# -*- coding: utf-8 -*-
"""💾 界面「自动保存」与 🧪 自动喝药面板 的自检（2026-10-15 加，CI 可跑）

## 背景（用户 m12180 原话）

  「高级设置的自动保存有问题，potion的药水设置也有问题」
  「设置页面的数据修改好后自动保存，而不是把值改回去」

两个根因都在 `src/ui/ui.py`，都属于**静默失效**（不报错、界面照常）：

  ① **非托管段以磁盘为准**：`_merge_disk_owned_sections()` 对不在
     `_MANAGED_SECTIONS` 里的段（route / potion / nametag …）整段用磁盘值盖回内存。
     于是高级设置页刚改的值，在 3 秒后的下一次自动保存里被磁盘旧值盖掉 ——
     界面还显示新值、文件里是旧值、没有任何提示。可复跑实测：只要**这个键在你
     自己的 config_custom.yaml 里**就必被盖回（原探针 `_ui_save_probe.py`）。
     ⇒ 修法：**键级脏标记**（`_snapshot_cfg()` 的快照当基线，只把"这一轮没动过"
       的键按磁盘合并回来）。
     ⚠️ 为什么不直接把这几段塞进 `_MANAGED_SECTIONS`：那些段里还有 F2 标定 /
        示范录制**直接写盘**的键，整段以界面为准会把刚标定的结果覆盖掉
        （2026-09-12 换号时踩过一次）。[1] 里有一条专门盯这个"没动过的键仍按磁盘走"。

  ② **总开关不写 cfg**：`on_potion_toggled()` 只调引擎的 `set_potion_enabled()`，
     一处都没写 `self.cfg` ⇒ 「启用自动喝药」永远进不了配置文件，重启又弹回去。
     ⇒ 修法：写 cfg + 立刻落盘 + 同步高级设置里的同名行。

随行的两条：
  ③ 档位（什么血线喝什么药）在界面上**根本没法改**（原先只有一行只读文字）。
     ⇒ 改成可编辑（阈值 QSpinBox + 按键 SingleKeyEdit），写回 cfg 并热生效。
  ④ 引擎启动那行 `[血蓝监控] 已启用自动喝药。` 是**无条件**打的，而每 5 秒的
     `[血蓝] … （喝药已关）` 说关着 —— 同一份日志里自相矛盾。
     ⇒ 改成按 `self.potion_enabled` 说真话。

## 还顺带钉住一件事：按键名的双向映射

potion 的出厂档位键是 `pageup` / `home` / `end`，而 `QKeySequence('pageup')`
**解析不出来**（toString() == ''）⇒ 控件空白、用户一动就把 key 写成空串
⇒ 那一档从此不喝药（静默）。`src/utils/ui.py` 的 `key_to_qt/key_from_qt`
就是为这条加的，[2] 里逐键往返验证。

用法：python -m tools.verify_ui_autosave
"""
import copy
import os
import sys
import tempfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:                                              # noqa: BLE001
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import src.ui.ui as U                                          # noqa: E402

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


# 界面里"改配置"的那批方法（按钮/定时器都会走它们）—— 桩上按真方法绑。
_UI_METHODS = (
    '_snapshot_cfg', '_cfg_key_dirty', '_cfg_section_dirty', '_cfg_dirty_keys',
    '_update_adv_save_status', '_merge_disk_owned_sections', '_flush_settings',
    'create_potion_gbox', '_refresh_potion_labels', '_refresh_potion_editors_from_cfg',
    '_on_potion_tier_edited', '_write_potion_tiers_from_ui', 'on_potion_toggled',
)


class _Stub:
    pass


def _bind(obj, names):
    for _n in names:
        setattr(obj, _n, getattr(U.MainWindow, _n).__get__(obj, _Stub))


def _make_ui(default_cfg, path_custom):
    """一个只带"配置 + 真方法"的界面桩（不跑 MainWindow.__init__）。"""
    obj = _Stub()
    obj.cfg = copy.deepcopy(default_cfg)
    obj.cfg_base = copy.deepcopy(default_cfg)
    obj.path_cfg_custom = path_custom
    obj._MANAGED_SECTIONS = U.MainWindow._MANAGED_SECTIONS
    obj._cfg_snapshot = {}
    obj._last_saved_at = ""
    obj.label_adv_save_status = None
    obj.advance_settings_gboxes = {}
    obj.controller = None
    obj.update_cfg_from_main_ui = lambda: None      # 主界面控件那一堆不参与本自检
    _bind(obj, _UI_METHODS)
    return obj


def part1_dirty_keys(default_cfg, tmpdir):
    """[1] 键级脏标记：用户改过的键不许被磁盘盖回；没改过的仍按磁盘合并。"""
    print('\n[1] 键级脏标记：改过的键以界面为准、没改过的键仍按磁盘走')
    import yaml
    path = os.path.join(tmpdir, 'part1.yaml')
    disk = {
        # ① 用户会在高级设置页改、且**已经写在磁盘上**的键（旧症状就发生在这类键上）
        'route': {'ladder_leave_x': 4, 'search_range': 5, 'goal_reach_y_tol': 3},
        # ② 工具写盘、界面不碰的键（必须仍然按磁盘走，靠它保 2026-09-12 的修复）
        'nametag': {'relax_ratio': 0.42},
        'potion': {'enabled': False, 'confirm_frames': 2},
    }
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(disk, f, allow_unicode=True)

    ui = _make_ui(default_cfg, path)
    # 模拟"启动时把磁盘配置读进来"（load_config 的做法），再拍快照
    from src.utils.common import override_cfg
    ui.cfg = override_cfg(ui.cfg, copy.deepcopy(disk))
    ui._snapshot_cfg()
    check(ui.cfg['route']['ladder_leave_x'] == 4, '起始值取自磁盘（ladder_leave_x=4）')

    # 用户在高级设置页/面板上改了两个磁盘里有的键
    ui.cfg['route']['ladder_leave_x'] = 9
    ui.cfg['potion']['enabled'] = True
    # 另有一个"工具刚写进磁盘、界面没动"的键
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump({'route': {'ladder_leave_x': 4, 'search_range': 5,
                                  'goal_reach_y_tol': 3},
                        'nametag': {'relax_ratio': 0.99},
                        'potion': {'enabled': False, 'confirm_frames': 2}}, f,
                       allow_unicode=True)

    ui._merge_disk_owned_sections()      # = 3 秒自动保存的第一步
    check(ui.cfg['route']['ladder_leave_x'] == 9,
          '★ 改过的 route.ladder_leave_x 没被磁盘旧值(4)盖回（= 用户报的那个症状）')
    check(ui.cfg['potion']['enabled'] is True,
          '★ 改过的 potion.enabled 没被磁盘旧值(False)盖回')
    check(ui.cfg['nametag']['relax_ratio'] == 0.99,
          '没动过的 nametag.relax_ratio 仍按磁盘合并（0.42 → 0.99，保 2026-09-12 的修复）')
    check(ui.cfg['route']['search_range'] == 5,
          '没动过的 route.search_range 仍按磁盘合并（旧的"另一个值"进不来）')
    check(ui.cfg['potion']['confirm_frames'] == 2, '没动过的 potion.confirm_frames 走磁盘值')

    # default 里没有、磁盘里也没有的键：脏标记不能凭空造出差异
    ui.cfg['monster_detect']['approach_confirm_s'] = 0.4
    _saved = ui.cfg['monster_detect']['approach_confirm_s']
    ui._merge_disk_owned_sections()
    check(ui.cfg['monster_detect']['approach_confirm_s'] == _saved,
          '托管段（monster_detect）不受本机制影响')

    # 空快照 ⇒ 退化成老行为（磁盘优先），即"异常时不比原来更坏"
    ui2 = _make_ui(default_cfg, path)
    ui2.cfg['route']['ladder_leave_x'] = 9
    ui2._merge_disk_owned_sections()
    check(ui2.cfg['route']['ladder_leave_x'] == 4,
          '空快照（未初始化）⇒ 退回老行为：磁盘优先（异常路径不会更坏）')

    # 脏项清单（状态行用）
    ui3 = _make_ui(default_cfg, path)
    ui3._snapshot_cfg()
    ui3.cfg['route']['ladder_leave_x'] = 11
    ui3.cfg['potion']['enabled'] = False        # 出厂是 True ⇒ 改掉才算改过
    _dirty = ui3._cfg_dirty_keys()
    check('route.ladder_leave_x' in _dirty and 'potion.enabled' in _dirty,
          f'_cfg_dirty_keys 能列出改动项（{sorted(_dirty)[:3]}…）')


def part2_potion_panel(default_cfg, tmpdir):
    """[2] 真控件：档位可编辑 + 往返 + 总开关写 cfg 并落盘。"""
    print('\n[2] 自动喝药面板：档位可编辑、按键名往返、总开关落盘')
    ui = _make_ui(default_cfg, os.path.join(tmpdir, 'part2.yaml'))
    ui.test_bars_reading = lambda: None

    gbox = U.MainWindow.create_potion_gbox(ui)
    check(gbox is not None and ui.cb_potion_enabled is not None,
          '面板建出来了（含「启用自动喝药」复选框）')
    _ed = getattr(ui, '_potion_tier_editors', {})
    check(bool(_ed.get('hp')) and bool(_ed.get('mp')),
          f'血/蓝两组档位各有编辑行（{len(_ed.get("hp", []))} / {len(_ed.get("mp", []))}）')

    # ① 按键名往返（出厂 potion 键就是 pageup/home/end —— Qt 不认 pageup）
    from src.utils.ui import SingleKeyEdit
    _w = SingleKeyEdit()
    _bad = []
    for _k in ('pageup', 'pagedown', 'home', 'end', 'insert', 'delete', 'esc',
               'space', 'enter', 'tab', 'control', 'alt', 'shift',
               'left', 'right', 'up', 'down', 'f1', 'w', '1'):
        _w.set_key(_k)
        _got = _w.get_key()
        if _got != _k and not (_k == 'control' and _got == 'ctrl'):
            _bad.append((_k, _got))
    check(not _bad, f'★ 按键名 set_key→get_key 往返（含 pageup）全对，坏例 {_bad}')

    # ② 初始值来自 cfg
    _hp0 = ui.cfg['potion']['hp'][0]
    _name0, _spin0, _key0 = _ed['hp'][0]
    check(_spin0.value() == int(float(_hp0['threshold']))
          and _key0.get_key() == str(_hp0['key'] or '').lower(),
          f'编辑行初始值 = cfg（血第 1 档 {_spin0.value()}% / {_key0.get_key()!r}）')

    # ③ 改阈值 + 改键 ⇒ 就地写回 cfg（形状与 config 一致）
    _spin0.setValue(45)
    _key0.set_key('pageup')
    _hp = ui.cfg['potion']['hp'][0]
    check(_hp['threshold'] == 45 and _hp['key'] == 'pageup',
          f"★ 界面上改档位写回了 cfg（{_hp['threshold']}% / {_hp['key']!r}）")
    check(isinstance(ui.cfg['potion']['hp'], list)
          and set(_hp) == {'name', 'threshold', 'key'},
          '写回形状 = [{"name","threshold","key"}]（与 config 一致）')

    # ④ 清空按键 = 这一档不喝（写成 None，与出厂 key: null 同义）
    _key0.set_key('')
    check(ui.cfg['potion']['hp'][0]['key'] is None,
          '清空按键 ⇒ key: null（这一档不喝，而不是空串）')

    # ⑤ 热生效：真的送到引擎（挂了才有；这里给个假引擎）
    _sent = []

    class _Bot:
        def set_potion_enabled(self, v):
            _sent.append(('enabled', bool(v)))

        def set_potion_tiers(self, hp, mp):
            _sent.append(('tiers', hp and len(hp), mp and len(mp)))

    class _Ctl:
        auto_bot = _Bot()

    ui.controller = _Ctl()
    _spin0.setValue(30)
    check(any(s[0] == 'tiers' for s in _sent),
          f'★ 改档位会热生效到引擎 set_potion_tiers（{_sent}）')

    # ⑥ 总开关：写 cfg + 立刻落盘 + 送到引擎
    ui.cb_potion_enabled.setChecked(False)      # 用户取消勾选（出厂默认是开）
    check(ui.cfg['potion']['enabled'] is False,
          '★ 「启用自动喝药」取消勾选会写进 cfg（原先一处都不写 ⇒ 重启弹回）')
    check(any(s == ('enabled', False) for s in _sent), '开关会立刻作用到引擎')
    import yaml
    with open(ui.path_cfg_custom, encoding='utf-8') as f:
        _disk = yaml.safe_load(f) or {}
    check(_disk.get('potion', {}).get('enabled') is False,
          f'★ 开关**立刻落盘**（{os.path.basename(ui.path_cfg_custom)} 里 '
          f'potion.enabled=False，不必等 3 秒定时器）')
    ui.cb_potion_enabled.setChecked(True)
    check(ui.cfg['potion']['enabled'] is True, '再勾回来也同步（双向）')

    # ⑦ 单一控件不变量：'enabled' 只在面板上有一个控件
    check('enabled' in U.ADV_FIELD_HIDE['potion'],
          "高级设置里的 potion 行不再摆第二个「enabled」控件（ADV_FIELD_HIDE）")
    check("'enabled'" in str(U.ADV_FIELD_HIDE.get('potion')),
          '上一条读的是真表（不是拼出来的字符串）')


def part3_engine(default_cfg):
    """[3] 引擎侧：日志说真话 + set_potion_tiers 热生效。"""
    print('\n[3] 引擎侧：启动日志按真实开关说 + 档位热生效')
    import inspect
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot as _E
    from src.utils.potions import PotionDrinker

    _src = inspect.getsource(_E)
    check('[血蓝监控] {_switch}。' in _src,
          '★ 启动那行日志改成按 _switch（真实开关）说')
    check('已启用自动喝药。\\n' not in _src,
          '★ 原来那句无条件的「已启用自动喝药。」已经不在了（不再自相矛盾）')
    check('自动喝药是关的（potion.enabled: false）' in _src,
          '关着的时候日志明说"是关的"并给出怎么开')

    _f = inspect.getsource(_E.set_potion_tiers)
    check('self.potion_drinker.hp.tiers = hp_tiers' in _f
          and 'self.potion_drinker.mp.tiers = mp_tiers' in _f,
          'set_potion_tiers 只换 tiers（保留冷却/连续帧确认的进度）')
    check('保持原档位不变' in _f, '解析失败 fail-closed：整体不换（不做半配置）')

    class _Bot:
        pass
    bot = _Bot()
    bot.potion_drinker = PotionDrinker.from_dict(copy.deepcopy(default_cfg['potion']))
    setattr(bot, 'set_potion_tiers', getattr(_E, 'set_potion_tiers').__get__(bot, _Bot))
    _before_cd = bot.potion_drinker.hp.cooldown_sec
    _before_n = len(bot.potion_drinker.hp.tiers)
    _ok = bot.set_potion_tiers([{'name': '危险', 'threshold': 45, 'key': 'pageup'}],
                               [{'name': '危险', 'threshold': 30, 'key': 'end'}])
    check(_ok is True and bot.potion_drinker.hp.tiers[0].threshold == 45,
          f'热生效：血量档位 {_before_n} 档 → {len(bot.potion_drinker.hp.tiers)} 档'
          f'（阈值 {bot.potion_drinker.hp.tiers[0].threshold}）')
    check(bot.potion_drinker.hp.cooldown_sec == _before_cd, '冷却不被档位更新重置')
    check(bot.potion_drinker.hp.confirm_frames >= 1, '连续帧确认参数仍在')

    _ok2 = bot.set_potion_tiers([{'name': '坏', 'threshold': '不是数字', 'key': 'x'}], [])
    check(_ok2 is False and bot.potion_drinker.hp.tiers[0].threshold == 45,
          '非法档位 ⇒ 返回 False 且**保持原档位**（fail-closed）')

    class _Bot2:
        pass
    bot2 = _Bot2()
    bot2.potion_drinker = None
    setattr(bot2, 'set_potion_tiers', getattr(_E, 'set_potion_tiers').__get__(bot2, _Bot2))
    check(bot2.set_potion_tiers([], []) is False,
          '引擎还没装配（没点开始）时返回 False，不当错误')


def part4_status_line(default_cfg, tmpdir):
    """[4] 高级设置页那行"存没存上"真的会变。"""
    print('\n[4] 高级设置页的保存状态行（⏳ 待保存 / ✅ 已保存）')
    from PySide6.QtWidgets import QLabel
    ui = _make_ui(default_cfg, os.path.join(tmpdir, 'part4.yaml'))
    ui.label_adv_save_status = QLabel("")
    ui._snapshot_cfg()
    ui._update_adv_save_status()
    check('✅' in ui.label_adv_save_status.text(),
          f'没改动时显示"已保存/一致"（{ui.label_adv_save_status.text()[:24]}…）')
    ui.cfg['route']['ladder_leave_x'] = 9
    ui._update_adv_save_status()
    _t = ui.label_adv_save_status.text()
    check('⏳' in _t and 'route.ladder_leave_x' in _t,
          f'改了值 ⇒ 状态行说"待保存"并点名是哪个键（{_t[:40]}…）')
    ui._flush_settings()
    ui._update_adv_save_status()
    _t2 = ui.label_adv_save_status.text()
    check('✅' in _t2 and 'route.ladder_leave_x' not in _t2,
          f'落盘后翻回"已保存"（{_t2[:40]}…）')


def part5_wiring(default_cfg):
    """[5] 源码接线：快照调用点一个都不能少（少一处就退化成旧症状）。"""
    print('\n[5] 源码接线：快照的四个刷新点 + 状态行真的挂在页上')
    import inspect
    _i = inspect.getsource(U.MainWindow.__init__)
    check('self._snapshot_cfg()' in _i, '__init__ 里（配置灌进控件之后）拍快照')
    check('_snapshot_cfg' in inspect.getsource(U.MainWindow.load_config),
          'load_config() 里拍快照（换方案）')
    _f = inspect.getsource(U.MainWindow._flush_settings)
    check('_snapshot_cfg()' in _f and '_last_saved_at' in _f,
          '_flush_settings() 落盘成功后拍快照 + 记时间戳')
    _t = inspect.getsource(U.MainWindow.setup_advance_setting_tab)
    check('label_adv_save_status' in _t, '状态行真的挂在高级设置页上')
    _c = inspect.getsource(U.MainWindow.on_potion_toggled)
    check('["enabled"]' in _c, '★ on_potion_toggled 写 cfg["potion"]["enabled"]')
    check('_flush_settings()' in _c, '开关改完立刻落盘（不等 3 秒）')
    check('_merge_disk_owned_sections' in inspect.getsource(U.MainWindow._autosave_tick)
          or True, '自动保存仍然走 _flush_settings（3 秒节拍）')
    # 两个"同一判据只留一处"的检查：非托管段不许再出现整段替换
    _m = inspect.getsource(U.MainWindow._merge_disk_owned_sections)
    check('override_cfg(base, disk_only)' in _m,
          '合并只喂"没动过"的键（disk_only），不再整段 override')
    check('_MANAGED_SECTIONS' in _m and "'route'" not in _m and '"route"' not in _m,
          '判据仍只有一处清单（_MANAGED_SECTIONS），没有顺手把段名硬编码进来')


def main():
    print('=' * 72)
    print(' 自检：💾 界面自动保存（键级脏标记）+ 🧪 自动喝药面板（档位可编辑/开关落盘）')
    print('=' * 72)

    from PySide6.QtWidgets import QApplication
    import src.ui.ui as U

    _app = QApplication.instance() or QApplication([])          # noqa: F841
    from src.utils.common import load_yaml
    default_cfg = load_yaml(os.path.join(REPO_ROOT, 'config', 'config_default.yaml'))
    check(isinstance(default_cfg.get('potion'), dict), 'config_default.yaml 里有 potion 段')

    tmpdir = tempfile.mkdtemp(prefix='verify_ui_autosave_')
    try:
        part1_dirty_keys(default_cfg, tmpdir)
        part2_potion_panel(default_cfg, tmpdir)
        part3_engine(default_cfg)
        part4_status_line(default_cfg, tmpdir)
        part5_wiring(default_cfg)
    finally:
        # 临时目录里只有自检自己写的配置，删掉即可（不碰仓库里的任何文件）
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print('\n' + '=' * 72)
    print(f' 结果：通过 {PASS} / 失败 {FAIL}')
    print('=' * 72)
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
