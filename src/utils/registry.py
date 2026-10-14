# -*- coding: utf-8 -*-
'''
config_data.yaml 地图/怪物登记表的自动维护（文本级编辑，保留注释）。

背景（2026-09-12）：录路线 / 截怪物模板后要求用户手编登记表，容易忘、容易拼错。
现在工具自动登记。为什么不用 yaml.safe_load + safe_dump 整文件重写：
登记表头部有使用说明注释，safe_dump 会把它们全部抹掉（用户指南就没了）。
登记表结构固定简单，文本级插入最稳。

所有函数都支持 path 参数：测试用临时文件，不碰用户的 config_data.yaml。
'''
import os
import re

REG_PATH = os.path.join('config', 'config_data.yaml')


def _read(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return ''   # 登记表还不存在：按空表处理，首次登记时创建


def _write(text, path):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)   # 写成功才覆盖，防半截文件


def list_maps(path=None):
    path = path or REG_PATH
    """返回 map_mobs_mapping 里已登记的地图名列表（支持块式和空 {} 两种形态）。"""
    text = _read(path)
    m = re.search(r'^map_mobs_mapping:[ \t]*\n((?:[ \t]+\S.*\n?)*)', text, re.M)
    if m:
        return re.findall(r'^[ \t]+([^\s:#][^:#]*?):', m.group(1), re.M)
    m = re.search(r'^map_mobs_mapping:[ \t]*\{[ \t]*([^}]*)\}', text, re.M)
    if m:
        return [k for k in (s.strip() for s in re.split(r'[,\s]+', m.group(1))) if k]
    return []


def _ensure_block(text, map_name):
    """确保 map_mobs_mapping 段存在且为块式，并把 map_name: [] 追加到段尾。返回新文本。"""
    m = re.search(r'^map_mobs_mapping:[ \t]*\{[ \t]*\}[ \t]*$', text, re.M)
    if m:
        # 空 {} 形态 → 改写为块式
        return text[:m.start()] + f'map_mobs_mapping:\n  {map_name}: []\n' + text[m.end():]
    m_head = re.search(r'^map_mobs_mapping:[ \t]*\n', text, re.M)
    if m_head:
        # 已有块式段 → 找段内连续缩进行块的结尾，追加到末尾
        pos = m_head.end()
        lines = text[pos:].splitlines(keepends=True)
        last = 0
        for i, line in enumerate(lines):
            if re.match(r'[ \t]+\S', line):
                last = i + 1
            elif line.strip():
                break
            else:
                break
        indent_m = re.match(r'([ \t]+)', lines[0]) if lines else None
        indent = indent_m.group(1) if indent_m else '  '
        insert_at = pos + sum(len(l) for l in lines[:last])
        return text[:insert_at] + f'{indent}{map_name}: []\n' + text[insert_at:]
    # 段缺失 → 文件末尾追加
    return text.rstrip('\n') + f'\n\nmap_mobs_mapping:\n  {map_name}: []\n'


def register_map(map_name, path=None):
    path = path or REG_PATH
    """把地图登记进 map_mobs_mapping（怪物列表留空，之后截怪时补充）。
    已登记则不动。返回 True = 本次新登记，False = 早已存在。"""
    if map_name in list_maps(path):
        return False
    text = _ensure_block(_read(path), map_name)
    _write(text, path)
    return True


def unregister_mob(map_name, mob_name, path=None):
    '''把怪物从指定地图的怪物列表里移除（取消勾选时用）。返回 True = 有修改。'''
    path = path or REG_PATH
    text = _read(path)
    m = re.search(rf'^([ \t]+){re.escape(map_name)}:[ \t]*\[([^\]]*)\][ \t]*$',
                  text, re.M)
    if not m:
        return False
    mobs = [s.strip() for s in m.group(2).split(',') if s.strip()]
    if mob_name not in mobs:
        return False
    mobs = [s for s in mobs if s != mob_name]
    new_line = f'{m.group(1)}{map_name}: [{", ".join(mobs)}]\n'
    text = text[:m.start()] + new_line + text[m.end():]
    _write(text, path)
    return True


def unregister_map(map_name, path=None):
    '''把地图从登记表里移除（map_mobs_mapping + eng_to_cn 两处都清）。

    用于「删除地图」功能（2026-09-12 用户需求）。同样走文本级编辑，
    保住文件头部的说明注释。返回 True = 有修改，False = 表里本来就没它。

    ⚠️ 只动登记表，**不碰** minimaps/<图>/ 目录 —— 删目录是调用方的事，
       分开是为了可测（离线单测只验证文本编辑）。
    '''
    path = path or REG_PATH
    text = _read(path)
    if not text:
        return False

    def _drop_line_in_block(text, header, map_name):
        m_head = re.search(rf'^{re.escape(header)}:[ \t]*\n', text, re.M)
        if not m_head:
            return text, False
        pos = m_head.end()
        lines = text[pos:].splitlines(keepends=True)
        kept, removed = [], False
        for line in lines:
            # 只删该段内（有缩进）的目标行；遇到无缩进的非空行说明这个段结束了
            if re.match(rf'[ \t]+{re.escape(map_name)}:', line):
                removed = True
                continue
            kept.append(line)
        if not removed:
            return text, False
        body = ''.join(kept)
        # ⚠️ 2026-10-13（真机事故：**界面再也打不开**）：删掉**最后一条**登记时，
        #    原写法会留下一个**裸头部**（`map_mobs_mapping:` 后面一行都没有）——
        #    yaml 把它解析成 None，界面的 refresh_map_list 一 `name in None` 就抛
        #    TypeError（那份函数在启动链上），窗口还没显示就死；引擎侧的直接下标
        #    同样会炸。⇒ 段里一条缩进项都不剩时，写成显式的空表 `header: {}`。
        first = next((ln for ln in body.splitlines() if ln.strip()), '')
        if not re.match(r'[ \t]+\S', first):
            return text[:m_head.start()] + f'{header}: {{}}\n' + body, True
        return text[:pos] + body, True

    text, r1 = _drop_line_in_block(text, 'map_mobs_mapping', map_name)
    text, r2 = _drop_line_in_block(text, 'eng_to_cn', map_name)
    if r1 or r2:
        _write(text, path)
    return bool(r1 or r2)


def register_mob(map_name, mob_name, path=None):
    path = path or REG_PATH
    """把怪物追加到指定地图的怪物列表（已存在则不重复）。返回 True = 有修改。
    地图未登记时先登记地图。"""
    register_map(map_name, path)
    text = _read(path)
    m = re.search(rf'^([ \t]+){re.escape(map_name)}:[ \t]*\[([^\]]*)\][ \t]*$', text, re.M)
    if not m:
        raise ValueError(f'登记表里找不到地图 {map_name!r} 的怪物列表行')
    mobs = [s.strip() for s in m.group(2).split(',') if s.strip()]
    if mob_name in mobs:
        return False
    mobs.append(mob_name)
    new_line = f'{m.group(1)}{map_name}: [{", ".join(mobs)}]\n'
    text = text[:m.start()] + new_line + text[m.end():]
    _write(text, path)
    return True
