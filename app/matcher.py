"""软件名 / 文件夹名 / 注册表键名之间的启发式匹配。

设计原则：宁可漏报，不可误删。
- 候选强度：STRONG（很可能相关）> MEDIUM（可能相关）> WEAK（弱相关，默认不勾选）
- 所有候选最终都要经过用户人工勾选确认才会被删除。
- 命中通用词（microsoft/windows/update 等）的候选不会产生。
"""
from __future__ import annotations

import re

STRONG = "strong"
MEDIUM = "medium"
WEAK = "weak"

STRENGTH_RANK = {STRONG: 0, MEDIUM: 1, WEAK: 2}
STRENGTH_TEXT = {STRONG: "很可能相关", MEDIUM: "可能相关", WEAK: "弱相关"}

# 这些词没有区分度，不能作为删除依据
GENERIC_TOKENS = {
    "software", "microsoft", "windows", "corporation", "corp", "inc", "ltd",
    "llc", "gmbh", "co", "company", "update", "updates", "updater",
    "redistributable", "package", "packages", "setup", "install", "installer",
    "uninstall", "version", "build", "bit", "x64", "x86", "win32", "win64",
    "amd64", "runtime", "application", "app", "apps", "system", "tools",
    "tool", "suite", "client", "server", "service", "plugin", "plugins",
    "components", "component", "common", "shared", "files", "program",
    "programs", "data", "lib", "libs", "library", "support", "driver",
    "drivers", "edition", "language", "patch", "hotfix", "manager",
    "helper", "agent", "international", "intl", "user", "local", "temp",
}

_VERSION_RE = re.compile(r"^v?\d[\d._]*$", re.IGNORECASE)
_SPLIT_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _is_version_like(token: str) -> bool:
    # 只把纯数字/点号（可带 v 前缀）的词当版本号；"7zip" 这类不算
    return bool(_VERSION_RE.match(token))


def tokenize(text: str) -> list[str]:
    """按非字母数字（保留中文）切分。"""
    return [p for p in _SPLIT_RE.split(text or "") if p]


def normalize(text: str) -> str:
    """小写并去掉所有非字母数字（中文保留），用于无空格比较。"""
    return _SPLIT_RE.sub("", (text or "")).lower()


def distinctive_tokens(*texts: str) -> list[str]:
    """抽取有区分度的特征词：去通用词、去版本号、保序、去重。"""
    out: list[str] = []
    for text in texts:
        for tok in tokenize(text):
            low = tok.lower()
            if low in GENERIC_TOKENS or _is_version_like(low):
                continue
            if _has_cjk(low):
                if len(low) < 2:
                    continue
            elif len(low) < 3:
                continue
            if low not in out:
                out.append(low)
    return out


def name_core(name: str) -> str:
    """去掉括号内容与版本尾巴后的核心名（无空格小写）。

    例：'Mozilla Firefox 143.0 (x64 zh-CN)' -> 'mozillafirefox'
        '7-Zip 24.08 (x64)'                -> '7zip'
    """
    text = re.sub(r"(?<=\d)-(?=[A-Za-z\u4e00-\u9fff])", "", name or "")  # 7-Zip -> 7Zip
    text = re.sub(r"[（(][^（()）]*[)）]", " ", text)
    keep = []
    for tok in tokenize(text):
        low = tok.lower()
        if _is_version_like(low) or low in GENERIC_TOKENS:
            continue
        if not _has_cjk(low) and len(low) < 3:
            continue
        keep.append(low)
    return "".join(keep)


def match_candidate(candidate: str, app):
    """把文件夹名/注册表键名 candidate 与软件 app 匹配。

    返回 (强度, 匹配原因) 或 None。匹配依据：
    - 与完整软件名 / 核心名完全一致 -> STRONG
    - 等于一个高权重特征词（安装目录组件权重最高） -> MEDIUM/STRONG
    - 只是包含某个特征词 -> WEAK
    """
    cand = normalize(candidate)
    if not cand:
        return None
    if candidate.strip().lower() in GENERIC_TOKENS:
        return None  # 通用词（microsoft/windows/update…）永远不作为删除依据
    if app.name and cand == normalize(app.name):
        return STRONG, f"与软件名称完全匹配（{candidate}）"
    core = app.name_core
    if core and len(core) >= 4 and cand == core:
        return STRONG, f"与软件核心名称匹配（{candidate} ≈ {app.name}）"
    name_norm = normalize(app.name) if app.name else ""
    if len(cand) >= 4 and name_norm.startswith(cand):
        return STRONG, f"软件名称以「{candidate}」开头"

    score = 0
    hits: list[str] = []
    for tok, weight in app.match_tokens():
        if tok == cand:
            score += weight
            hits.append(tok)
        elif len(tok) >= 4 and tok in cand:
            score += 1
            hits.append(tok)

    if not hits:
        return None
    shown = "」「".join(dict.fromkeys(hits))
    if score >= 3:
        return STRONG, f"匹配特征词「{shown}」"
    if score == 2:
        return MEDIUM, f"匹配特征词「{shown}」"
    return WEAK, f"部分匹配特征词「{hits[0]}」（可能相关，请人工确认）"
