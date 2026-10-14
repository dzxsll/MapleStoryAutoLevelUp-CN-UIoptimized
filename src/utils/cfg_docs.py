'''
界面用的「参数说明 / 数值参考」加载器（2026-10-14 加）

为什么不用 load_yaml_with_comments()
------------------------------------
`src/utils/common.py:load_yaml_with_comments()` 取的是 ruamel 的
`ca.items[key][2]` —— 那是写在键**后面**的那段注释。而本仓库
`config/config_default.yaml` 的一贯写法是把说明写在键**上方**，于是 ruamel
把它挂到**前一个键**的 `[2]` 槽里，`field_comments[section][key]` 拿到的其实是
**下一个键**的说明：整体错位一格，而且完全静默（页面照渲染，只是说的是别人）。

2026-10-14 实测（`tools/verify_ui_tabs.py` 钉住三条）：
  · `config/config_default.yaml:464` 的 `detect_log: true`，说明写在 `:456-463`；
    但解析结果里 detect_log 装的是 `:465-474`（那段属于 `every_n_frames`，键在 `:475`）
  · `route.goal_pixel_skip`（`:711`）只拿到空白，它的整段说明被
    `route.goal_reach_y_tol`（`:666`）拿走了

本模块改成**按行读原文**，两条来源（都可当场核对）：
  1. 键**自己那一行**的行尾注释（`confirm_frames: 2   # 🔁 连续几帧…`）—— 最可靠，
     它就写在这个键身上；
  2. 紧贴该键**上方**的那段连续 `#` 注释。

⚠️ 写在键**下方**的注释**不算**本键的说明：那种写法在本仓库里通常是在讲**下一个**
   键（也可能是在续写上一个键 —— 光看文本分不出来）。宁可这个键暂时没有说明，
   也不要张冠李戴：错位的说明比没有说明更坏（用户会照着别人的阈值调）。
'''

import re

# 段落标题：缩进 0、`名字:`、后面什么都没有（或只有注释）
_SECTION_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*):\s*(?:#.*)?$')
# 字段：有缩进、`名字:`、后面可以是值
_KEY_RE = re.compile(r'^(\s+)([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$')
_COMMENT_RE = re.compile(r'^\s*#(.*)$')
# 装饰线（===== / ----- / *****）不算说明
_DECOR_RE = re.compile(r'^[=\-*~_\s]{3,}$')

# 出现这些词（且同行有数字/引号）的行，归到「参考」而不是「说明」
_REF_KEYWORDS = (
    "默认", "出厂", "建议", "推荐", "范围", "取值", "可选", "上限", "下限",
    "填 0", "填0", "0 = ", "1 = ", "0=", "1=", "关掉", "开启",
)


def _clean(block):
    '''把一段 `#` 注释行整理成说明文本：去装饰线、去尾部空行、折叠连续空行'''
    out = []
    for raw in block:
        text = raw.strip()
        if _DECOR_RE.match(text):
            continue
        out.append(text)
    while out and not out[-1]:
        out.pop()
    collapsed = []
    for text in out:
        if not text and collapsed and not collapsed[-1]:
            continue
        collapsed.append(text)
    return "\n".join(collapsed)


def _trailing_comment(rest):
    '''
    从"值 # 注释"里取出注释（引号里的 `#` 不算注释，例如 mode: "#fff"）

    返回 (注释文本, 去掉注释后的值)
    '''
    out = []
    quote = None
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
            out.append(ch)
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            continue
        if ch == '#' and (i == 0 or rest[i - 1].isspace()):
            return rest[i + 1:].strip(), rest[:i].rstrip()
        out.append(ch)
    return "", rest.rstrip()


def load_field_docs(path):
    '''
    按行读配置文件，返回 (field_docs, section_docs)

    field_docs[section][key] = 这个键的说明（**键自己那行的行尾注释**优先；
                              没有才用紧贴它上方的那段连续注释）
    section_docs[section]    = 紧贴该段落标题上方的那段连续注释

    规则（四条，简单到可以当场核对）：
      1. 连续 `#` 行累积成"待认领的说明块"；
      2. 遇到空行（真的空行，不是 `#`）或列表项等其它行 ⇒ 说明块作废（不再属于任何键）；
      3. 说明块后面第一个"段落标题 / 带缩进的键"就认领它；
      4. 键自己那行有行尾注释 ⇒ 以它为准（说明块随后作废，不再传给下一个键）。
    '''
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    field_docs, section_docs = {}, {}
    section = None
    pending = []

    for line in lines:
        if not line.strip():
            pending = []
            continue

        m = _COMMENT_RE.match(line)
        if m:
            pending.append(m.group(1).strip())
            continue

        m = _KEY_RE.match(line)
        if m and section:
            key = m.group(2)
            trail, _val = _trailing_comment(m.group(3))
            doc = trail if trail else _clean(pending)
            if doc:
                field_docs.setdefault(section, {})[key] = doc
            pending = []
            continue

        m = _SECTION_RE.match(line)
        if m and not line[:1].isspace():
            section = m.group(1)
            section_docs[section] = _clean(pending)
            field_docs.setdefault(section, {})
            pending = []
            continue

        pending = []

    return field_docs, section_docs


def _clip(text, limit):
    text = " ".join(text.split())
    if limit and len(text) > limit:
        return text[:limit - 1].rstrip() + "…"
    return text


def split_doc(text, max_summary=160, max_reference=200):
    '''
    把一段说明拆成 (说明, 参考) 两行 —— 参考 = 带默认值/范围/建议的那几行。

    只做"分流"，不做改写：两行都可以截断（页面上空间有限），全文由调用方
    放进 tooltip，所以信息不会丢。
    '''
    if not text:
        return "", ""
    summary_lines, ref_lines = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        has_number = any(ch.isdigit() for ch in line)
        looks_ref = any(kw in line for kw in _REF_KEYWORDS) and (
            has_number or '"' in line or "'" in line)
        (ref_lines if looks_ref else summary_lines).append(line)
    return (_clip(" ".join(summary_lines), max_summary),
            _clip("；".join(ref_lines), max_reference))


def default_hint(value):
    '''把出厂值渲染成中文短语（界面上的「参考：默认 X」）'''
    if isinstance(value, bool):
        return "默认 " + ("开" if value else "关")
    if isinstance(value, (int, float)):
        return f"默认 {value}"
    if isinstance(value, str):
        return f'默认 "{value}"' if value else "默认 空"
    if isinstance(value, (list, tuple)):
        return "默认 [" + ", ".join(str(v) for v in value) + "]"
    if value is None:
        return "默认 无"
    return f"默认 {value}"


def field_doc_lines(field_docs, section, key, value,
                    max_summary=160, max_reference=200):
    '''
    给高级设置页用：返回 (说明, 参考) 两行。

    说明 = 配置注释里的正文；参考 = 出厂值 + 注释里带数字的建议。
    '''
    doc = (field_docs or {}).get(section, {}).get(key, "")
    summary, reference = split_doc(doc, max_summary, max_reference)
    if "默认" in reference:
        # 注释里已经写了默认值，就不要再拼一遍出厂值
        return summary, reference
    reference = "；".join(p for p in (default_hint(value), reference) if p)
    return summary, reference
