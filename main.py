"""猜选手 · 多题库（AstrBot 插件）

一套对局骨架，三个题库：
  - 弗一把  CS:GO / CS2 Major 选手（8 项属性）
  - 足一把  足球球员（9 项属性）
  - 撸一把  LoL 职业选手（9 项属性）

判定口径分别对齐 shnlfriberg/csgofriberg 与 zuyiba 的页面逻辑；
LoL 站点的判定在服务端，这里是按其棋盘列的语义复刻（见 README 说明）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_friberg_guess"
PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"

GREEN, YELLOW, RED = "🟩", "🟨", "🟥"
LEVEL_EMOJI = {"correct": GREEN, "close": YELLOW, "wrong": RED}
LEGEND = "🟩 完全一致　🟨 接近　🟥 不符　↑/↓ 答案更大/更小"

_LEET_TABLE = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g"}
)
_NOISE_RE = re.compile(r"[\s\u3000,，.。!！?？~～、:：;；'\"“”‘’()（）\[\]【】]+")
_PAREN_RE = re.compile(r"^(.+?)\s*[（(]([^）)]{1,10})[）)]\s*$")


# ============================================================
# 通用工具
# ============================================================


def normalize_name(text: str) -> str:
    """归一化：全角转半角、去符号空格、小写、leet 归一。"""
    text = unicodedata.normalize("NFKC", text or "").strip().lower()
    text = _NOISE_RE.sub("", text)
    return text.translate(_LEET_TABLE)


def split_nickname(nickname: str) -> tuple[str, str]:
    """拆出主体名与括号后缀，例如 `NiKo（波黑）` → (`NiKo`, `波黑`)。"""
    matched = _PAREN_RE.match((nickname or "").strip())
    if matched:
        return matched.group(1).strip(), matched.group(2).strip()
    return (nickname or "").strip(), ""


@dataclass
class Cell:
    """棋盘上的一格。"""

    label: str
    value: str
    level: str  # correct / close / wrong


def num_cell(label: str, guess_val: Any, target_val: Any, tolerance: int, suffix: str = "") -> Cell:
    """数值型属性：一致 / 差在容差内 / 不符，并给出方向。"""
    if guess_val == target_val:
        return Cell(label, f"{guess_val}{suffix}", "correct")
    arrow = "↑" if target_val > guess_val else "↓"
    level = "close" if abs(target_val - guess_val) <= tolerance else "wrong"
    return Cell(label, f"{guess_val}{suffix}{arrow}", level)


def render_board(theme: "Theme", index: int, total: int, guess: dict, target: dict) -> str:
    """把一次猜测渲染成群聊可读的棋盘。"""
    cells = theme.judge(guess, target)
    hit = all(c.level == "correct" for c in cells)
    lines = []
    for row in theme.rows:
        parts = [f"{LEVEL_EMOJI[cells[i].level]}{cells[i].label} {cells[i].value}" for i in row if i < len(cells)]
        if parts:
            lines.append("　".join(parts))
    head = "🎯 命中" if hit else f"第 {index}/{total} 猜"
    return f"{head}｜{theme.title_of(guess)}\n" + "\n".join(lines)


# ============================================================
# 题库主题
# ============================================================


class Theme:
    """一个题库（一种游戏）的定义。"""

    key: str = ""
    command: str = ""
    aliases: tuple[str, ...] = ()
    label: str = ""  # 「CS Major 选手」
    icon: str = "🎮"
    db_file: str = ""
    attempts: int = 8
    rows: tuple[tuple[int, ...], ...] = ()
    pool_alias: dict[str, str] = {}
    default_pool: str = "normal"
    difficulty_hint: str = "普通"

    # -- 数据 --
    def load(self, raw: Any) -> list[dict]:
        raise NotImplementedError

    def keys_of(self, player: dict) -> set[str]:
        raise NotImplementedError

    def title_of(self, player: dict) -> str:
        raise NotImplementedError

    def pools(self, players: list[dict]) -> dict[str, list[dict]]:
        return {"normal": players}

    def pool_label(self, pool: str) -> str:
        return pool

    # -- 玩法 --
    def judge(self, guess: dict, target: dict) -> list[Cell]:
        raise NotImplementedError

    def card(self, player: dict) -> str:
        raise NotImplementedError

    def hints(self, player: dict) -> list[tuple[str, str]]:
        raise NotImplementedError

    # -- 组装 --
    def resolve_pool(self, raw: str, default: str) -> str | None:
        """把用户输入的难度词转成 pool key；无法识别返回 None。"""
        token = (raw or "").strip().split()[0] if (raw or "").strip() else ""
        if not token:
            return default if default in self.pool_alias else self.default_pool
        return self.pool_alias.get(token) or self.pool_alias.get(token.lower())


def _diff_cell(label: str, guess_val: int, target_val: int, tolerance: int, suffix: str = "") -> Cell:
    if guess_val == target_val:
        return Cell(label, f"{guess_val}{suffix}", "correct")
    arrow = "↑" if target_val > guess_val else "↓"
    level = "close" if abs(target_val - guess_val) <= tolerance else "wrong"
    return Cell(label, f"{guess_val}{suffix}{arrow}", level)


class CSTheme(Theme):
    """CS:GO / CS2 Major 选手。"""

    key = "cs"
    command = "弗一把"
    aliases = ("猜选手", "cs猜选手")
    label = "CS Major 选手"
    icon = "🔫"
    db_file = "players.json"
    attempts = 8
    rows = ((0, 1, 2), (3, 4), (5, 6, 7))
    pool_alias = {
        "易": "easy", "入门": "easy", "新手": "easy", "easy": "easy", "beginner": "easy",
        "中": "normal", "简单": "normal", "进阶": "normal", "normal": "normal",
        "难": "hard", "普通": "hard", "标准": "hard", "困难": "hard", "地狱": "hard", "hard": "hard",
    }
    difficulty_hint = "易 / 中 / 难"

    def load(self, raw: Any) -> list[dict]:
        players = raw.get("players") if isinstance(raw, dict) else raw
        out = []
        for p in players or []:
            if not p.get("nickname") or not p.get("is_enabled", True):
                continue
            p = dict(p)
            p["_base"] = split_nickname(p["nickname"])[0]
            out.append(p)
        return out

    def keys_of(self, player: dict) -> set[str]:
        return {player["nickname"], player.get("_base", "")}

    def title_of(self, player: dict) -> str:
        return player["nickname"]

    def pools(self, players: list[dict]) -> dict[str, list[dict]]:
        beginner = [p for p in players if "beginner" in (p.get("difficulties") or [])]
        mainstream = [p for p in players if "easy" in (p.get("difficulties") or [])]
        return {
            "easy": beginner or mainstream or players,
            "normal": mainstream or players,
            "hard": players,
        }

    def pool_label(self, pool: str) -> str:
        return {
            "easy": "易（知名选手）",
            "normal": "中（主流选手）",
            "hard": "难（全部选手）",
        }.get(pool, pool)

    def judge(self, guess: dict, target: dict) -> list[Cell]:
        cells: list[Cell] = []

        # 国家 / 地区：相同=一致；不同但同赛区=接近
        if guess.get("nationality") == target.get("nationality"):
            cells.append(Cell("国家", guess.get("nationality", "—"), "correct"))
        elif guess.get("region") and guess.get("region") == target.get("region"):
            cells.append(Cell("国家", guess.get("nationality", "—"), "close"))
        else:
            cells.append(Cell("国家", guess.get("nationality", "—"), "wrong"))

        same_region = guess.get("region") == target.get("region")
        cells.append(Cell("赛区", guess.get("region", "—"), "correct" if same_region else "wrong"))

        history = target.get("team_history") or []
        if guess.get("team") == target.get("team"):
            cells.append(Cell("战队", guess.get("team", "—"), "correct"))
        elif guess.get("team") and guess["team"] in history:
            cells.append(Cell("战队", guess["team"], "close"))
        else:
            cells.append(Cell("战队", guess.get("team", "—"), "wrong"))

        cells.append(_diff_cell("年龄", int(guess.get("age") or 0), int(target.get("age") or 0), 3))
        same_role = guess.get("role") == target.get("role")
        cells.append(Cell("位置", guess.get("role", "—"), "correct" if same_role else "wrong"))
        cells.append(
            _diff_cell("冠军", int(guess.get("major_championships") or 0), int(target.get("major_championships") or 0), 1)
        )
        cells.append(
            _diff_cell("出场", int(guess.get("major_appearances") or 0), int(target.get("major_appearances") or 0), 1)
        )
        same_active = bool(guess.get("is_active")) == bool(target.get("is_active"))
        cells.append(
            Cell("状态", "现役" if guess.get("is_active") else "已退役", "correct" if same_active else "wrong")
        )
        return cells

    def card(self, player: dict) -> str:
        status = "现役" if player.get("is_active") else "已退役"
        return (
            f"📇 {player['nickname']}（{status}）\n"
            f"{player.get('nationality', '—')} · {player.get('region', '—')} · {player.get('team', '—')} · "
            f"{player.get('role', '—')} · {player.get('age', '—')} 岁\n"
            f"🏆 Major 冠军 {player.get('major_championships', 0)} 次 · 出场 {player.get('major_appearances', 0)} 次"
        )

    def hints(self, player: dict) -> list[tuple[str, str]]:
        return [
            ("位置", f"打的是 {player.get('role', '—')}"),
            ("赛区", f"来自 {player.get('region', '—')} 赛区"),
            ("国家", f"{player.get('nationality', '—')} 人"),
            ("冠军", f"Major 冠军 {player.get('major_championships', 0)} 次"),
            ("出场", f"Major 出场 {player.get('major_appearances', 0)} 次"),
            ("状态", "现役选手" if player.get("is_active") else "已经退役"),
            ("年龄", f"{player.get('age', '—')} 岁"),
        ]


TOP5_LEAGUES = {"英超", "西甲", "德甲", "意甲", "法甲"}

# 「易」档用的热门球队（名单与题库里的队名一致）
BIG_CLUBS = {
    # 英超
    "曼城", "利物浦", "阿森纳", "曼联", "切尔西", "热刺", "纽卡斯尔联", "阿斯顿维拉",
    # 西甲
    "皇家马德里", "巴塞罗那", "马德里竞技", "塞维利亚", "皇家社会", "比利亚雷亚尔",
    "皇家贝蒂斯", "瓦伦西亚",
    # 德甲
    "拜仁慕尼黑", "多特蒙德", "勒沃库森", "莱比锡红牛", "法兰克福", "斯图加特",
    # 意甲
    "国际米兰", "AC米兰", "尤文图斯", "那不勒斯", "罗马", "拉齐奥", "亚特兰大",
    # 法甲
    "巴黎圣日耳曼", "马赛", "里昂", "摩纳哥", "里尔",
}

# 题目范围：单联赛 / 五大
LEAGUE_SCOPES = {
    "英超": "epl", "西甲": "laliga", "德甲": "bundesliga", "意甲": "seriea", "法甲": "ligue1",
    "五大": "top5", "五大联赛": "top5",
}
FAMOUS = {
    "梅西", "C罗", "姆巴佩", "哈兰德", "内马尔", "贝林厄姆",
    "维尼修斯", "萨拉赫", "德布劳内", "凯恩", "莱万多夫斯基", "莫德里奇",
    "克罗斯", "本泽马", "苏亚雷斯", "伊布拉希莫维奇", "贝尔", "阿扎尔",
    "卡卡", "罗纳尔迪尼奥", "大罗", "齐达内", "贝克汉姆", "亨利",
    "杰拉德", "兰帕德", "鲁尼", "马尔蒂尼", "托蒂", "布冯",
    "卡西利亚斯", "哈维", "伊涅斯塔", "皮尔洛", "罗本", "里贝里",
    "德罗巴", "埃托奥", "阿圭罗", "巴乔", "菲戈", "贝利",
    "马拉多纳", "克鲁伊夫", "范巴斯滕", "普拉蒂尼", "济科", "罗马里奥",
    "里瓦尔多", "卡福", "罗伯特·卡洛斯", "内斯塔", "卡纳瓦罗", "拉姆",
    "克洛泽", "施魏因施泰格", "巴拉克", "欧文", "劳尔", "范尼斯特鲁伊",
    "博格坎普", "皮克", "布斯克茨", "若尔迪·阿尔巴", "马塞洛", "佩佩",
    "瓦拉内", "亚马尔", "罗德里", "福登", "萨卡", "厄德高",
    "拉菲尼亚", "库尔图瓦", "阿利松", "范迪克", "吕迪格", "戴维斯",
    "基米希", "托马斯·穆勒", "维尔茨", "穆西亚拉", "劳塔罗·马丁内斯", "巴雷拉",
    "莱奥", "多纳鲁马", "格列兹曼", "奥布拉克", "特尔施特根", "诺伊尔",
    "迪马利亚", "孙兴慜", "郑智", "孙继海", "中田英寿", "中村俊辅",
    "朴智星", "本田圭佑", "范志毅", "保利尼奥", "孔卡", "马内",
    "马赫雷斯", "库利巴利", "米林科维奇-萨维奇", "菲利克斯", "布罗佐维奇", "纳乔",
    "鲁本·内维斯", "沃特金斯", "托尼", "坎特", "博格巴", "拉什福德",
    "布鲁诺·费尔南德斯", "麦迪逊", "凯尔·沃克", "戈登", "埃德森", "奥纳纳",
}

SCOPE_LABELS = {
    "all": "全部联赛", "top5": "五大联赛", "epl": "英超", "laliga": "西甲",
    "bundesliga": "德甲", "seriea": "意甲", "ligue1": "法甲",
}
DIFF_WORDS = {
    "易": "easy", "简单": "easy", "easy": "easy",
    "中": "normal", "普通": "normal", "标准": "normal", "normal": "normal",
    "难": "hard", "困难": "hard", "地狱": "hard", "hard": "hard",
}


def _eff_club(player: dict) -> str:
    if player.get("league") == "已退役":
        return "已退役"
    if player.get("league") not in TOP5_LEAGUES:
        return "非五大联赛"
    return player.get("club", "—")


class FootballTheme(Theme):
    """足球球员（复刻 zuyiba 的 9 项对比）。"""

    key = "football"
    command = "足一把"
    aliases = ("足球一把", "猜球员", "猜球星")
    label = "足球球员"
    icon = "⚽"
    db_file = "football_players.json"
    attempts = 10
    rows = ((0, 1), (2, 3), (4, 5), (6, 7), (8,))
    pool_alias = {**DIFF_WORDS, "核心": "normal", "全部": "hard", "全量": "hard", "full": "hard"}
    difficulty_hint = "易（默认，知名 200 人）/ 中（五大联赛）/ 难（全部），或指定联赛：英超 / 西甲 / 德甲 / 意甲 / 法甲 / 五大"

    def resolve_pool(self, raw: str, default: str) -> str | None:
        """支持「联赛 + 难度」组合，如 `英超 易`、`五大 难`。"""
        scope = ""
        diff = DIFF_WORDS.get(str(default).strip(), "normal")
        for token in (raw or "").split():
            if token in LEAGUE_SCOPES:
                scope = LEAGUE_SCOPES[token]
            elif token in DIFF_WORDS:
                diff = DIFF_WORDS[token]
            elif token in {"全部", "全量", "所有", "full", "all"}:
                scope, diff = "all", "hard"
            else:
                return None
        # 「易」（知名 200 人）和「难」都在全库取，「中」默认限定五大联赛
        if not scope:
            scope = "all" if diff in ("easy", "hard") else "top5"
        return f"{scope}:{diff}"

    def load(self, raw: Any) -> list[dict]:
        players = raw.get("players") if isinstance(raw, dict) else raw
        return [dict(p) for p in players or [] if p.get("name")]

    def keys_of(self, player: dict) -> set[str]:
        keys = {player.get("name", ""), player.get("cnName", "")}
        keys.update(player.get("aliases") or [])
        return {k for k in keys if k}

    def title_of(self, player: dict) -> str:
        cn = player.get("cnName")
        return f"{cn}（{player['name']}）" if cn and cn != player["name"] else player["name"]

    def pools(self, players: list[dict]) -> dict[str, list[dict]]:
        famous = _famous_pool(players)
        top5 = [p for p in players if p.get("league") in TOP5_LEAGUES]
        out = {
            # 全库：易=最出名的 200 人（含退役巨星与小联赛巨星），中=五大联赛，难=全部
            "all:easy": famous,
            "all:normal": top5 or players,
            "all:hard": players,
            "top5:easy": [p for p in famous if p.get("league") in TOP5_LEAGUES] or top5,
            "top5:normal": top5,
            "top5:hard": top5,
        }
        for league, key in LEAGUE_SCOPES.items():
            if key == "top5":
                continue
            pool = [p for p in players if p.get("league") == league]
            famous_league = [p for p in famous if p.get("league") == league]
            hot_league = [p for p in pool if p.get("club") in BIG_CLUBS]
            out[f"{key}:easy"] = famous_league if len(famous_league) >= 40 else (hot_league or pool)
            out[f"{key}:normal"] = pool
            out[f"{key}:hard"] = pool
        return out

    def pool_label(self, pool: str) -> str:
        scope, _, diff = pool.partition(":")
        scope_label = SCOPE_LABELS.get(scope, scope)
        diff_label = {
            "easy": "易（知名球员）",
            "normal": "中（联赛全部）",
            "hard": "难（全部，含扩容）",
        }.get(diff, diff)
        if scope == "all" and diff == "hard":
            return "全部（含非五大联赛与退役）"
        if scope == "all" and diff == "easy":
            return "知名 200 人（含退役与小联赛巨星）"
        if scope == "all" and diff == "normal":
            return "五大联赛 · 中（全部球员）"
        return f"{scope_label} · {diff_label}"

    def judge(self, guess: dict, target: dict) -> list[Cell]:
        cells: list[Cell] = []

        # 国籍：双国籍互认；否则同大洲算接近
        g_nat, t_nat = guess.get("nationality"), target.get("nationality")
        g_dual = guess.get("dualNationality") or []
        t_dual = target.get("dualNationality") or []
        if g_nat == t_nat or t_nat in g_dual or g_nat in t_dual:
            cells.append(Cell("国籍", g_nat or "—", "correct"))
        elif guess.get("continent") and guess.get("continent") == target.get("continent"):
            cells.append(Cell("国籍", g_nat or "—", "close"))
        else:
            cells.append(Cell("国籍", g_nat or "—", "wrong"))

        # 联赛
        cells.append(
            Cell("联赛", guess.get("league", "—"), "correct" if guess.get("league") == target.get("league") else "wrong")
        )
        # 俱乐部（非五大联赛统一显示）
        g_club, t_club = _eff_club(guess), _eff_club(target)
        cells.append(Cell("俱乐部", g_club, "correct" if g_club == t_club else "wrong"))
        # 位置
        cells.append(
            Cell("位置", guess.get("position", "—"), "correct" if guess.get("position") == target.get("position") else "wrong")
        )
        # 出生年 ±2
        cells.append(_diff_cell("出生年", int(guess.get("birthYear") or 0), int(target.get("birthYear") or 0), 2))
        # 球衣号 ±2
        cells.append(_diff_cell("号码", int(guess.get("number") or 0), int(target.get("number") or 0), 2, ""))
        # 欧冠冠军数 ±1
        cells.append(_diff_cell("欧冠", int(guess.get("ucl") or 0), int(target.get("ucl") or 0), 1))
        # 世界杯参赛届数 ±1
        cells.append(_diff_cell("世界杯", int(guess.get("wc") or 0), int(target.get("wc") or 0), 1))
        # 世界杯夺冠次数：必须一致
        cells.append(
            _diff_cell("世界杯冠军", int(guess.get("wcTitle") or 0), int(target.get("wcTitle") or 0), 0)
        )
        return cells

    def card(self, player: dict) -> str:
        return (
            f"📇 {player.get('cnName', player['name'])}（{player['name']}）\n"
            f"{player.get('nationality', '—')} · {player.get('league', '—')} · {_eff_club(player)} · "
            f"{player.get('position', '—')} · {player.get('birthYear', '—')} 年生 · {player.get('number', '—')} 号\n"
            f"🏆 欧冠 {player.get('ucl', 0)} 次 · 世界杯出场 {player.get('wc', 0)} 届 · 夺冠 {player.get('wcTitle', 0)} 次"
        )

    def hints(self, player: dict) -> list[tuple[str, str]]:
        return [
            ("国籍", f"{player.get('nationality', '—')}（{player.get('continent', '—')}）"),
            ("联赛", f"{player.get('league', '—')}"),
            ("俱乐部", f"{_eff_club(player)}"),
            ("位置", f"{player.get('position', '—')}"),
            ("出生年", f"{player.get('birthYear', '—')} 年"),
            ("号码", f"{player.get('number', '—')} 号"),
            ("欧冠", f"欧冠冠军 {player.get('ucl', 0)} 次"),
        ]


class LolTheme(Theme):
    """LoL 职业选手（数据来自 Liquipedia 的 Infobox player）。"""

    key = "lol"
    command = "撸一把"
    aliases = ("lol一把", "猜撸", "猜联盟")
    label = "LoL 职业选手"
    icon = "🖱"
    db_file = "lol_players.json"
    attempts = 8
    rows = ((0, 1), (2, 3), (4,))
    pool_alias = {
        "易": "easy", "简单": "easy", "easy": "easy",
        "中": "normal", "普通": "normal", "标准": "normal", "normal": "normal",
        "难": "hard", "全部": "hard", "地狱": "hard", "hard": "hard",
    }
    difficulty_hint = "易 / 中 / 难"

    def load(self, raw: Any) -> list[dict]:
        players = raw.get("players") if isinstance(raw, dict) else raw
        out = []
        for p in players or []:
            if not p.get("gameId"):
                continue
            p = dict(p)
            positions = p.get("positions")
            p["_positions"] = positions if isinstance(positions, list) else [str(positions or "—")]
            p["_team"] = (p.get("currentTeam") or "").strip()
            p["_country"] = (p.get("country") or "").strip()
            p["_continent"] = (p.get("continent") or "").strip()
            out.append(p)
        return out

    def keys_of(self, player: dict) -> set[str]:
        keys = {player.get("gameId", ""), player.get("realName", "")}
        keys.update(player.get("aliases") or [])
        return {k for k in keys if k}

    def title_of(self, player: dict) -> str:
        return player["gameId"]

    def pools(self, players: list[dict]) -> dict[str, list[dict]]:
        active = [p for p in players if str(p.get("status", "")).lower().startswith("act")]
        # 中韩选手是中文玩家最眼熟的一批，作为「易」档
        cnkr = [p for p in players if p.get("country") in {"韩国", "中国", "中国台湾", "中国香港", "中国澳门"}]
        return {
            "easy": cnkr or active or players,
            "normal": active or players,
            "hard": players,
        }

    def pool_label(self, pool: str) -> str:
        return {
            "easy": "易（中韩选手）",
            "normal": "中（现役选手）",
            "hard": "难（全部，含退役）",
        }.get(pool, pool)

    def judge(self, guess: dict, target: dict) -> list[Cell]:
        cells: list[Cell] = []

        # 位置（有交集就算一致）
        g_pos = set(guess.get("_positions") or [])
        same_pos = bool(g_pos & set(target.get("_positions") or []))
        cells.append(Cell("位置", "/".join(guess.get("_positions") or ["—"]), "correct" if same_pos else "wrong"))

        # 国家：相同=一致，同大洲=接近
        g_country, t_country = guess.get("_country", ""), target.get("_country", "")
        if g_country and g_country == t_country:
            cells.append(Cell("国家", g_country, "correct"))
        elif guess.get("_continent") and guess.get("_continent") == target.get("_continent"):
            cells.append(Cell("国家", g_country or "—", "close"))
        else:
            cells.append(Cell("国家", g_country or "—", "wrong"))

        # 大洲
        same_continent = bool(guess.get("_continent")) and guess.get("_continent") == target.get("_continent")
        cells.append(Cell("大洲", guess.get("_continent") or "—", "correct" if same_continent else "wrong"))

        # 年龄 ±2
        cells.append(_diff_cell("年龄", int(guess.get("age") or 0), int(target.get("age") or 0), 2))

        # 状态
        same_status = str(guess.get("status", "")).lower() == str(target.get("status", "")).lower()
        cells.append(
            Cell(
                "状态",
                "现役" if str(guess.get("status", "")).lower().startswith("act") else "退役",
                "correct" if same_status else "wrong",
            )
        )
        return cells

    def card(self, player: dict) -> str:
        status = "现役" if str(player.get("status", "")).lower().startswith("act") else "退役"
        where = player.get("_country") or "—"
        if player.get("_continent"):
            where += f"（{player['_continent']}）"
        parts = [
            player.get("_team") or "",
            where,
            "/".join(player.get("_positions") or ["—"]),
            f"{player.get('age', '—')} 岁",
        ]
        return f"📇 {player['gameId']}（{player.get('realName') or '—'}，{status}）\n" + " · ".join(p for p in parts if p)

    def hints(self, player: dict) -> list[tuple[str, str]]:
        return [
            ("位置", "/".join(player.get("_positions") or ["—"])),
            ("国家", player.get("_country") or "—"),
            ("大洲", player.get("_continent") or "—"),
            ("年龄", f"{player.get('age', '—')} 岁"),
            ("状态", "现役" if str(player.get("status", "")).lower().startswith("act") else "退役"),
        ]


def _fame_score(player: dict) -> tuple:
    """给「谁更容易被猜到」打分：传奇履历 + 豪门 + 退役身份。"""
    return (
        1 if (player.get("ucl") or 0) > 0 else 0,
        1 if (player.get("wcTitle") or 0) > 0 else 0,
        int(player.get("wc") or 0),
        1 if player.get("club") in BIG_CLUBS else 0,
        int(player.get("ucl") or 0),
        1 if player.get("league") == "已退役" else 0,
    )


def _famous_pool(players: list[dict], limit: int = 200) -> list[dict]:
    """最出名的 200 人：先取名单内的，不够再按履历补齐。"""
    picked = [p for p in players if p.get("cnName") in FAMOUS]
    if len(picked) < limit:
        chosen = {p["cnName"] for p in picked}
        rest = sorted(
            (p for p in players if p.get("cnName") not in chosen),
            key=_fame_score,
            reverse=True,
        )
        picked.extend(rest[: limit - len(picked)])
    return picked[:limit]


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str) and value:
        return [value]
    return []


THEMES: dict[str, Theme] = {t.key: t for t in (CSTheme(), FootballTheme(), LolTheme())}


# ============================================================
# 选手库
# ============================================================


class PlayerDB:
    """某个题库的选手数据：加载、检索、按难度取池。"""

    def __init__(self, theme: Theme, path: Path) -> None:
        self.theme = theme
        self.path = path
        self.version = ""
        self.players: list[dict] = []
        self._index: dict[str, list[dict]] = {}
        self._pools: dict[str, list[dict]] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 题库 {self.path.name} 读取失败：{exc}")
            self.players, self._index, self._pools = [], {}, {}
            return
        self.version = str(raw.get("version") or "") if isinstance(raw, dict) else ""
        self.players = self.theme.load(raw)
        self._index = {}
        for player in self.players:
            for key in self.theme.keys_of(player):
                normalized = normalize_name(key)
                if normalized:
                    self._index.setdefault(normalized, []).append(player)
        self._pools = self.theme.pools(self.players)
        logger.info(f"[{PLUGIN_NAME}] {self.theme.label}题库加载 {len(self.players)} 人（{self.version or '未知版本'}）")

    @staticmethod
    def _prominence(player: dict) -> tuple:
        """同名时优先现役、履历更厚的那个。"""
        return (
            0 if str(player.get("is_active", player.get("status", "Active"))).lower() not in ("false", "retired") else 1,
            -int(player.get("major_appearances") or player.get("worldsCount") or 0),
            len(str(player.get("nickname") or player.get("name") or player.get("gameId") or "")),
        )

    def pool(self, name: str) -> list[dict]:
        players = self._pools.get(name) or self.players
        return players or self.players

    def pool_size(self, name: str) -> int:
        return len(self.pool(name))

    def match(self, query: str) -> tuple[str, list[dict]]:
        """检索，返回 (匹配类型, 候选)。类型：exact / prefix / contains / none。"""
        key = normalize_name(query)
        if not key:
            return "none", []
        exact = self._index.get(key)
        if exact:
            return "exact", sorted(exact, key=self._prominence)[:5]

        starts, contains = [], []
        for player in self.players:
            names = [normalize_name(k) for k in self.theme.keys_of(player)]
            if any(n.startswith(key) for n in names if n):
                starts.append(player)
            elif any(key in n for n in names if n):
                contains.append(player)
        if starts:
            return "prefix", sorted(starts, key=self._prominence)[:5]
        if contains:
            return "contains", sorted(contains, key=self._prominence)[:5]
        return "none", []

    def find(self, query: str) -> list[dict]:
        return self.match(query)[1]

    def get(self, title: str) -> dict | None:
        for player in self.players:
            if self.theme.title_of(player) == title:
                return player
        return None


# ============================================================
# 对局
# ============================================================


@dataclass
class Game:
    key: str  # 会话 + 题库
    theme: str
    umo: str
    group_id: str
    user_id: str
    user_name: str
    target: str  # 答案的 title_of
    pool: str
    attempts: int = 8
    created_at: float = field(default_factory=time.time)
    guessed: list[str] = field(default_factory=list)
    revealed: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "theme": self.theme,
            "umo": self.umo,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "target": self.target,
            "pool": self.pool,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "guessed": self.guessed,
            "revealed": self.revealed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Game":
        return cls(
            key=data["key"],
            theme=data.get("theme", "cs"),
            umo=data["umo"],
            group_id=data.get("group_id", ""),
            user_id=data.get("user_id", ""),
            user_name=data.get("user_name", ""),
            target=data["target"],
            pool=data.get("pool", "normal"),
            attempts=int(data.get("attempts") or 8),
            created_at=data.get("created_at", time.time()),
            guessed=list(data.get("guessed") or []),
            revealed=list(data.get("revealed") or []),
        )


class SessionKey:
    """会话键：群内每人一份，私聊单独一份。"""

    @staticmethod
    def user(group_id: str, user_id: str) -> str:
        return f"{group_id or 'private'}:{user_id}"

    @staticmethod
    def game(group_id: str, user_id: str, theme: str) -> str:
        return f"{group_id or 'private'}:{user_id}:{theme}"

    @staticmethod
    def stat(group_id: str, user_id: str, theme: str) -> str:
        return f"{group_id or 'private'}:{user_id}:{theme}"

# ============================================================
# 文本入口解析
# ============================================================

_START_PATTERNS: list[tuple[re.Pattern[str], str]] = []
for _key, _theme in THEMES.items():
    _names = "|".join(re.escape(n) for n in (_theme.command, *_theme.aliases))
    _START_PATTERNS.append((re.compile(rf"^[/／]?(?:{_names})(?:\s+(.+))?$", re.I), _key))

_LOOKUP_RE = re.compile(r"^[/／]?(?:图鉴|查选手|查球员|查资料)\s*(\S+)?$")
_LEADING_RE = re.compile(r"^[/／]?(?:我猜|猜一下|猜|guess|answer|是)\s*", re.I)
_TRAILING_RE = re.compile(r"(?:吧|呢|啊|呀|吗|了)+$")

# 不带唤醒前缀时也能用的口令 → 插件方法名
_SILENT_WITHOUT_GAME = {"_hint_text", "_giveup_text"}
_BARE_COMMANDS = {
    "提示": "_hint_text",
    "线索": "_hint_text",
    "放弃": "_giveup_text",
    "结束": "_giveup_text",
    "认输": "_giveup_text",
    "战绩": "_stats_text",
    "排行": "_rank_text",
    "排行榜": "_rank_text",
}


# ============================================================
# 插件
# ============================================================


class GuessProPlugin(Star):
    """猜选手 · 多题库

    群里发「弗一把」/「足一把」/「撸一把」开局，然后直接发选手名开猜。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config or {}
        self.dbs: dict[str, PlayerDB] = {
            key: PlayerDB(theme, DATA_DIR / theme.db_file) for key, theme in THEMES.items()
        }
        self.games: dict[str, Game] = {}
        self.active: dict[str, str] = {}  # 会话键 → 题库_key
        self.stats: dict[str, dict[str, dict[str, Any]]] = {"users": {}, "groups": {}}
        self._lock = asyncio.Lock()
        self.data_dir = self._resolve_data_dir()
        self.state_file = self.data_dir / "state.json"
        self._load_state()
        self._restart_timers()

    # ---------------- 基础设施 ----------------

    def _cfg(self, key: str, default: Any) -> Any:
        value = self.config.get(key, default) if isinstance(self.config, dict) else default
        return default if value is None else value

    def _enabled(self, theme_key: str) -> bool:
        return bool(self._cfg(f"enable_{theme_key}", True))

    def _session_ok(self, event: AstrMessageEvent) -> bool:
        """配置了「仅私聊」时，群聊里完全不响应。"""
        if not self._cfg("private_only", False):
            return True
        return not bool(event.get_group_id())

    def _attempts(self, theme: Theme) -> int:
        configured = int(self._cfg("max_attempts", 0) or 0)
        return configured if configured > 0 else theme.attempts

    def _resolve_data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = Path(get_astrbot_data_path())
        except Exception:  # pragma: no cover
            import tempfile

            base = Path(tempfile.gettempdir()) / "astrbot"
        path = base / "plugin_data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取存档失败，已忽略：{exc}")
            return
        for item in data.get("games") or []:
            with contextlib.suppress(Exception):
                game = Game.from_dict(item)
                self.games[game.key] = game
        for ukey, theme_key in (data.get("active") or {}).items():
            if theme_key in THEMES:
                self.active[ukey] = theme_key
        stats = data.get("stats") or {}
        self.stats = {"users": stats.get("users") or {}, "groups": stats.get("groups") or {}}

    def _save_state(self) -> None:
        payload = {
            "games": [g.to_dict() for g in self.games.values()],
            "active": self.active,
            "stats": self.stats,
        }
        tmp = self.state_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 保存存档失败：{exc}")

    def _spawn_timer(self, game: Game) -> None:
        minutes = int(self._cfg("timeout_minutes", 30) or 0)
        if minutes <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 非事件循环上下文（例如单元测试）
            return
        game.task = loop.create_task(self._timeout_watch(game, minutes * 60))

    def _restart_timers(self) -> None:
        for game in list(self.games.values()):
            if self.games.get(game.key) is game:
                self._spawn_timer(game)

    async def _timeout_watch(self, game: Game, seconds: int) -> None:
        deadline = game.created_at + seconds
        try:
            await asyncio.sleep(max(1.0, deadline - time.time()))
        except asyncio.CancelledError:  # pragma: no cover
            return
        if self.games.get(game.key) is not game:
            return
        self.games.pop(game.key, None)
        self._settle_stats(game, won=False)
        self._save_state()
        theme = THEMES.get(game.theme)
        db = self.dbs.get(game.theme)
        target = db.get(game.target) if db else None
        title = theme.label if theme else "对局"
        text = (
            f"⏳ 时间到，{title}这一局结束。答案：{game.target}\n"
            + (theme.card(target) + "\n" if theme and target else "")
            + f"发送「{theme.command if theme else '弗一把'}」再来一局。"
        )
        with contextlib.suppress(Exception):
            await self.context.send_message(game.umo, MessageChain().message(text))

    # ---------------- 战绩 ----------------

    def _settle_stats(self, game: Game, won: bool) -> None:
        key = SessionKey.stat(game.group_id, game.user_id, game.theme)
        user = self.stats["users"].setdefault(
            key, {"name": game.user_name, "wins": 0, "plays": 0, "guesses": 0, "best": 0}
        )
        user["name"] = game.user_name or user.get("name", "")
        user["plays"] = int(user.get("plays", 0)) + 1
        user["guesses"] = int(user.get("guesses", 0)) + len(game.guessed)
        if won:
            user["wins"] = int(user.get("wins", 0)) + 1
            best = len(game.guessed)
            if not user.get("best") or best < int(user["best"]):
                user["best"] = best
        group = self.stats["groups"].setdefault(
            f"{game.group_id or 'private'}:{game.theme}", {"plays": 0, "wins": 0}
        )
        group["plays"] = int(group.get("plays", 0)) + 1
        if won:
            group["wins"] = int(group.get("wins", 0)) + 1

    def _user_stats_text(self, group_id: str, user_id: str, theme_key: str) -> str:
        theme = THEMES[theme_key]
        stat = self.stats["users"].get(SessionKey.stat(group_id, user_id, theme_key))
        if not stat or not stat.get("plays"):
            return f"还没有{theme.label}战绩，发送「{theme.command}」开一局。"
        plays = int(stat["plays"])
        wins = int(stat["wins"])
        avg = int(stat["guesses"]) / plays if plays else 0
        best = stat.get("best") or 0
        return (
            f"📊 {stat.get('name', '你')} · {theme.label}：{wins} 胜 / {plays} 局（胜率 {wins / plays:.0%}）\n"
            f"平均每次消耗 {avg:.1f} 猜" + (f"，最少 {best} 猜命中" if best else "")
        )

    def _rank_board(self, group_id: str, theme_key: str) -> str:
        theme = THEMES[theme_key]
        gid = group_id or "private"
        prefix = f"{gid}:"
        suffix = f":{theme_key}"
        rows = [
            (value, key)
            for key, value in self.stats["users"].items()
            if key.startswith(prefix) and key.endswith(suffix) and int(value.get("wins", 0)) > 0
        ]
        if not rows:
            return f"本群还没有人猜中过{theme.label}，来开个记录？发送「{theme.command}」。"
        rows.sort(key=lambda item: (-int(item[0].get("wins", 0)), int(item[0].get("guesses", 0))))
        lines = [f"🏆 本群战绩榜 · {theme.label}"]
        for i, (stat, _key) in enumerate(rows[:10], start=1):
            plays = int(stat.get("plays", 0)) or 1
            avg = int(stat.get("guesses", 0)) / plays
            lines.append(
                f"{i}. {stat.get('name', '匿名')}　{int(stat.get('wins', 0))} 胜 / {plays} 局　平均 {avg:.1f} 猜"
            )
        return "\n".join(lines)

    # ---------------- 开局 ----------------

    def _args_after_command(self, event: AstrMessageEvent, theme: Theme) -> str:
        """取命令词后面的全部文字，支持「英超 易」这种两个词。"""
        text = (event.message_str or "").strip()
        for name in sorted((theme.command, *theme.aliases), key=len, reverse=True):
            if text.startswith(name):
                return text[len(name) :].strip()
        return ""

    def _pk(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        gid, uid = event.get_group_id(), event.get_sender_id()
        return SessionKey.user(gid, uid), gid, uid

    def _game_of(self, event: AstrMessageEvent, theme_key: str) -> Game | None:
        _ukey, gid, uid = self._pk(event)
        return self.games.get(SessionKey.game(gid, uid, theme_key))

    def _current_theme(self, event: AstrMessageEvent) -> str:
        """当前会话正在玩的题库；没有进行中的对局时回落到默认题库。"""
        ukey, _gid, _uid = self._pk(event)
        theme_key = self.active.get(ukey)
        if theme_key and self._game_of(event, theme_key):
            return theme_key
        for key in THEMES:
            if self._game_of(event, key):
                self.active[ukey] = key
                return key
        return theme_key or "cs"

    def _begin(self, event: AstrMessageEvent, theme_key: str, raw_difficulty: str) -> str:
        theme = THEMES[theme_key]
        db = self.dbs[theme_key]
        if not self._enabled(theme_key):
            return f"{theme.label}题库已在配置里关闭。"
        if not db.players:
            if theme_key == "lol":
                return (
                    "撸一把的题库还没导入（data/lol_players.json）。\n"
                    "可以运行仓库里的 `python tools/crawl_lol.py` 抓一份，脚本支持中断续抓。"
                )
            return f"{theme.label}题库没加载成功，检查 data/{theme.db_file}。"

        ukey, gid, uid = self._pk(event)
        gkey = SessionKey.game(gid, uid, theme_key)
        if gkey in self.games:
            return f"这局{theme.label}还没结束呢，先猜，或者发「放弃」结束。"

        default_pool = str(self._cfg("difficulty", "") or self._cfg("default_difficulty", "易"))
        pool = theme.resolve_pool(raw_difficulty, default_pool)
        if not pool:
            return f"{theme.command}的难度只有：{theme.difficulty_hint}。"
        if not db.pool(pool):
            pool = theme.default_pool

        target = random.choice(db.pool(pool))
        attempts = self._attempts(theme)
        game = Game(
            key=gkey,
            theme=theme_key,
            umo=event.unified_msg_origin,
            group_id=gid,
            user_id=uid,
            user_name=event.get_sender_name(),
            target=theme.title_of(target),
            pool=pool,
            attempts=attempts,
        )
        self.games[gkey] = game
        self.active[ukey] = theme_key
        self._spawn_timer(game)
        self._save_state()
        return self._start_text(theme, db, game)

    def _start_text(self, theme: Theme, db: PlayerDB, game: Game) -> str:
        minutes = int(self._cfg("timeout_minutes", 30) or 0)
        limit = f"｜限时 {minutes} 分钟" if minutes > 0 else ""
        others = " · ".join(
            f"{t.command}"
            for k, t in THEMES.items()
            if k != theme.key and self._enabled(k) and self.dbs[k].players
        )
        tail = f"\n其它题库：{others}" if others else ""
        return (
            f"{theme.icon} {theme.command} · 猜{theme.label}\n"
            f"难度：{theme.pool_label(game.pool)}（{db.pool_size(game.pool)} 人）"
            f"｜机会：{game.attempts} 次{limit}\n"
            f"直接发选手名就能猜（大小写不限，可以只写前几个字母）\n"
            f"其它指令：提示 · 放弃 · 战绩 · 排行 · 图鉴 <名字>{tail}\n"
            f"{LEGEND}"
        )

    # ---------------- 猜测 ----------------

    def _apply_guess(self, game: Game, guess: dict[str, Any]) -> str:
        theme = THEMES[game.theme]
        db = self.dbs[game.theme]
        title = theme.title_of(guess)
        if title in game.guessed:
            return f"「{title}」已经猜过了，换一个。"
        target = db.get(game.target)
        if not target:
            self.games.pop(game.key, None)
            return "这一局的答案数据丢了，重新开一局吧。"

        game.guessed.append(title)
        cells = theme.judge(guess, target)
        board = render_board(theme, len(game.guessed), game.attempts, guess, target)

        if all(c.level == "correct" for c in cells):
            self.games.pop(game.key, None)
            if game.task:
                game.task.cancel()
            self._settle_stats(game, won=True)
            self._save_state()
            return (
                f"{board}\n\n🎉 猜中了！{len(game.guessed)} 猜命中 {title}\n{theme.card(target)}\n"
                + self._user_stats_text(game.group_id, game.user_id, game.theme)
            )

        if len(game.guessed) >= game.attempts:
            self.games.pop(game.key, None)
            if game.task:
                game.task.cancel()
            self._settle_stats(game, won=False)
            self._save_state()
            return (
                f"{board}\n\n💀 {game.attempts} 次机会用完了，答案是 {title}\n{theme.card(target)}\n"
                f"发送「{theme.command}」再来一局。"
            )

        self._save_state()
        text = board
        if self._cfg("show_legend", True) and len(game.guessed) == 1:
            text += f"\n{LEGEND}"
        if len(game.guessed) == game.attempts - 1:
            text += "\n⚠️ 最后一次机会了。"
        else:
            text += f"\n还剩 {game.attempts - len(game.guessed)} 次机会"
        return text

    def _with_at(self, event: AstrMessageEvent, text: str) -> list:
        if self._cfg("at_sender", True) and event.get_group_id():
            with contextlib.suppress(Exception):
                return [At(qq=event.get_sender_id()), Plain(f" {text}")]
        return [Plain(text)]

    # ---------------- 口令实现 ----------------

    def _hint_text(self, event: AstrMessageEvent) -> str:
        if not self._cfg("enable_hint", True):
            return "本插件已关闭提示功能。"
        theme_key = self._current_theme(event)
        theme = THEMES[theme_key]
        game = self._game_of(event, theme_key)
        if not game:
            return "现在没有进行中的对局，发送「弗一把」「足一把」或「撸一把」开局。"

        limit = int(self._cfg("hint_limit", 2) or 0)
        if len(game.revealed) >= limit:
            return f"提示已经用完了（每局 {limit} 次）。"
        target = self.dbs[theme_key].get(game.target)
        if not target:
            self.games.pop(game.key, None)
            return "这一局的数据丢了，重新开一局吧。"

        pool = [h for h in theme.hints(target) if h[0] not in game.revealed]
        if not pool:
            return "没什么可提示的了，直接猜吧。"
        label, value = random.choice(pool)
        game.revealed.append(label)
        self._save_state()
        return f"💡 线索：{value}（提示还剩 {limit - len(game.revealed)} 次）"

    def _giveup_text(self, event: AstrMessageEvent) -> str:
        theme_key = self._current_theme(event)
        theme = THEMES[theme_key]
        game = self._game_of(event, theme_key)
        if not game:
            return "现在没有进行中的对局。"
        self.games.pop(game.key, None)
        if game.task:
            game.task.cancel()
        self._settle_stats(game, won=False)
        self._save_state()
        target = self.dbs[theme_key].get(game.target)
        return f"🏳️ 已放弃。答案：{game.target}\n" + (theme.card(target) if target else "")

    def _stats_text(self, event: AstrMessageEvent) -> str:
        _ukey, gid, uid = self._pk(event)
        return self._user_stats_text(gid, uid, self._current_theme(event))

    def _rank_text(self, event: AstrMessageEvent) -> str:
        _ukey, gid, _uid = self._pk(event)
        return self._rank_board(gid, self._current_theme(event))

    def _lookup_text(self, event: AstrMessageEvent, query: str) -> str:
        if not query:
            return "用法：图鉴 <选手名>"
        theme_key = self._current_theme(event)
        order = [theme_key] + [k for k in THEMES if k != theme_key]
        for key in order:
            db = self.dbs[key]
            candidates = db.find(query)
            if len(candidates) == 1:
                return THEMES[key].card(candidates[0])
            if len(candidates) > 1:
                names = " / ".join(THEMES[key].title_of(p) for p in candidates)
                return f"{THEMES[key].label}里找到多个：{names}，换个更精确的名字。"
        return f"没找到「{query}」，试试英文名或别的写法。"

    # ---------------- 指令 ----------------

    @filter.command("弗一把", alias={"猜选手", "cs猜选手"})
    async def start_cs(self, event: AstrMessageEvent, difficulty: str = ""):
        """CS:GO / CS2 Major 选手猜谜"""
        if not self._session_ok(event):
            return
        args = self._args_after_command(event, THEMES["cs"]) or difficulty
        yield event.chain_result(self._with_at(event, self._begin(event, "cs", args)))
        event.stop_event()

    @filter.command("足一把", alias={"足球一把", "猜球员", "猜球星"})
    async def start_football(self, event: AstrMessageEvent, difficulty: str = ""):
        """足球球员猜谜"""
        if not self._session_ok(event):
            return
        args = self._args_after_command(event, THEMES["football"]) or difficulty
        yield event.chain_result(self._with_at(event, self._begin(event, "football", args)))
        event.stop_event()

    @filter.command("撸一把", alias={"lol一把", "猜撸", "猜联盟"})
    async def start_lol(self, event: AstrMessageEvent, difficulty: str = ""):
        """LoL 职业选手猜谜"""
        if not self._session_ok(event):
            return
        args = self._args_after_command(event, THEMES["lol"]) or difficulty
        yield event.chain_result(self._with_at(event, self._begin(event, "lol", args)))
        event.stop_event()

    @filter.command("提示", alias={"线索"})
    async def hint(self, event: AstrMessageEvent):
        """给一条线索，每局限用若干次"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._hint_text(event))
        event.stop_event()

    @filter.command("放弃", alias={"结束", "认输"})
    async def give_up(self, event: AstrMessageEvent):
        """放弃本局并公布答案"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._giveup_text(event))
        event.stop_event()

    @filter.command("战绩")
    async def my_stats(self, event: AstrMessageEvent):
        """查看当前题库的战绩"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._stats_text(event))
        event.stop_event()

    @filter.command("排行", alias={"排行榜"})
    async def leaderboard(self, event: AstrMessageEvent):
        """本群当前题库的胜场排行"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._rank_text(event))
        event.stop_event()

    @filter.command("图鉴", alias={"查选手", "查球员", "查资料"})
    async def lookup(self, event: AstrMessageEvent, query: str = ""):
        """查选手资料，例如 图鉴 哈兰德"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._lookup_text(event, query))
        event.stop_event()

    @filter.command("猜选手帮助", alias={"弗一把帮助", "足一把帮助", "撸一把帮助"})
    async def help(self, event: AstrMessageEvent):
        """玩法说明"""
        if not self._session_ok(event):
            return
        lines = ["🎮 猜选手 · 多题库"]
        for key, theme in THEMES.items():
            if not self._enabled(key) or not self.dbs[key].players:
                continue
            db = self.dbs[key]
            lines.append(
                f"{theme.icon} {theme.command}（{theme.label} {len(db.players)} 人，{self._attempts(theme)} 次机会）"
                f"　难度：{theme.difficulty_hint}"
            )
        attempts = self._attempts(THEMES["cs"])
        lines.append(f"1. 发「弗一把」「足一把」「撸一把」开局，默认 {attempts} 次机会，可用难度词")
        lines.append("2. 直接发选手名就能猜（也支持「猜 xxx」）")
        lines.append(f"3. 每次猜测按各题库的属性逐格反馈：{LEGEND}")
        lines.append("4. 指令：提示 · 放弃 · 战绩 · 排行 · 图鉴 <名字>")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ---------------- 自由文本 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """自由文本入口：裸口令开局、图鉴、常用口令、直接发选手名猜。"""
        if not self._session_ok(event):
            return
        raw = (event.message_str or "").strip()
        if not raw:
            return

        ukey, _gid, _uid = self._pk(event)

        # 1) 开局
        for pattern, theme_key in _START_PATTERNS:
            matched = pattern.match(raw)
            if matched:
                event.stop_event()
                text = self._begin(event, theme_key, (matched.group(1) or "").strip())
                await event.send(event.chain_result(self._with_at(event, text)))
                return

        # 2) 图鉴
        looked = _LOOKUP_RE.match(raw)
        if looked:
            event.stop_event()
            await event.send(event.plain_result(self._lookup_text(event, (looked.group(1) or "").strip())))
            return

        # 3) 其它裸口令
        method = _BARE_COMMANDS.get(raw.lstrip("/／").strip())
        if method:
            theme_key = self._current_theme(event)
            if method in _SILENT_WITHOUT_GAME and not self._game_of(event, theme_key):
                return
            event.stop_event()
            await event.send(event.plain_result(getattr(self, method)(event)))
            return

        # 4) 直接发选手名
        if not self._cfg("free_text_guess", True):
            return
        theme_key = self._current_theme(event)
        game = self._game_of(event, theme_key)
        if not game or len(raw) > 32:
            return
        query = _TRAILING_RE.sub("", _LEADING_RE.sub("", raw)).strip()
        if not query or re.search(r"[?？]$", raw):
            return

        db = self.dbs[theme_key]
        kind, candidates = db.match(query)
        if not candidates:
            return
        if len(candidates) > 1:
            if kind == "exact":
                candidates = candidates[:1]
            elif _LEADING_RE.match(raw):
                names = " / ".join(THEMES[theme_key].title_of(p) for p in candidates)
                event.stop_event()
                await event.send(event.plain_result(f"你想猜谁？{names}"))
                return
            else:
                return

        event.stop_event()
        async with self._lock:
            text = self._apply_guess(game, candidates[0])
        await event.send(event.chain_result(self._with_at(event, text)))

    async def terminate(self) -> None:
        for game in list(self.games.values()):
            if game.task:
                game.task.cancel()
        self._save_state()
        logger.info(f"[{PLUGIN_NAME}] 已卸载，状态已保存")
