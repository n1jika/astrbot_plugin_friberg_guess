"""弗一把 · CS Major 选手竞猜（AstrBot 插件）

群聊版「猜选手」：每人独立对局，猜一位选手后按八项属性给出接近度反馈，
8 次内猜中即胜。判定规则与属性顺序对齐开源项目 shnlfriberg/csgofriberg，
选手库来自社区整理版本（见 README 的数据来源一节）。
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
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_friberg_guess"
PLUGIN_DIR = Path(__file__).resolve().parent
PLAYER_FILE = PLUGIN_DIR / "data" / "players.json"

# ---------- 玩法常量（与官方一致） ----------
DEFAULT_MAX_ATTEMPTS = 8
AGE_TOLERANCE = 3  # 年龄相差 ≤3 岁视为「接近」
MAJOR_TOLERANCE = 1  # Major 冠军 / 出场数相差 ≤1 视为「接近」

GREEN, YELLOW, RED = "🟩", "🟨", "🟥"
LEVEL_EMOJI = {"correct": GREEN, "close": YELLOW, "wrong": RED}

DIFFICULTIES = {
    "beginner": ("入门", "知名选手"),
    "easy": ("简单", "主流选手"),
    "normal": ("普通", "全部选手"),
}
DIFFICULTY_ALIASES = {
    "入门": "beginner",
    "新手": "beginner",
    "beginner": "beginner",
    "简单": "easy",
    "进阶": "easy",
    "easy": "easy",
    "普通": "normal",
    "标准": "normal",
    "normal": "normal",
    "困难": "normal",
    "地狱": "normal",
    "hard": "normal",
}

LEGEND = "🟩 完全一致　🟨 接近　🟥 不符　↑/↓ 答案更大/更小"

# 猜测消息里允许出现的前缀 / 标点
_LEET_TABLE = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g"}
)
_NOISE_RE = re.compile(r"[\s\u3000,，.。!！?？~～、:：;；'\"“”‘’()（）\[\]【】]+")
_LEADING_RE = re.compile(r"^[/／]?(?:我猜|猜一下|猜|guess|answer|是)\s*", re.I)
_TRAILING_RE = re.compile(r"(?:吧|呢|啊|呀|吗|了)+$")
_START_RE = re.compile(r"^[/／]?(?:弗一把|猜选手|来一把)(?:\s+(\S+))?$")
_LOOKUP_RE = re.compile(r"^[/／]?(?:图鉴|查选手)\s*(\S+)?$")
_PAREN_RE = re.compile(r"^(.+?)\s*[（(]([^）)]{1,8})[）)]\s*$")

# 不带唤醒前缀时也能用的口令 → 插件方法名
# 没有进行中对局时，这些裸口令保持沉默
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
# 选手库
# ============================================================


def normalize_name(text: str) -> str:
    """把昵称归一化：全角转半角、去掉符号空格、小写、leet 归一。

    这样 `s1mple` / `S1MPLE` / `simple` 都能落到同一个 key 上。
    """
    text = unicodedata.normalize("NFKC", text or "").strip().lower()
    text = _NOISE_RE.sub("", text)
    return text.translate(_LEET_TABLE)


def split_nickname(nickname: str) -> tuple[str, str]:
    """拆出昵称主体与括号里的消歧义后缀，例如 `NiKo（波黑）` → (`NiKo`, `波黑`)。"""
    matched = _PAREN_RE.match(nickname.strip())
    if matched:
        return matched.group(1).strip(), matched.group(2).strip()
    return nickname.strip(), ""


class PlayerDB:
    """选手库：加载、检索、按难度取选手池。"""

    def __init__(self, path: Path = PLAYER_FILE) -> None:
        self.path = path
        self.version: str = ""
        self.players: list[dict[str, Any]] = []
        self._index: dict[str, list[dict[str, Any]]] = {}
        self._pools: dict[str, list[dict[str, Any]]] = {}
        self.load()

    # -- 加载 --
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 选手库读取失败（{self.path}）：{exc}")
            self.players = []
            self._index = {}
            self._pools = {}
            return
        if isinstance(raw, dict):
            self.version = str(raw.get("version") or "")
            players = raw.get("players") or []
        else:
            players = raw
        self.players = [p for p in players if p.get("nickname") and p.get("is_enabled", True)]
        self._index = {}
        for p in self.players:
            # 括号后缀（如「NiKo（波黑）」）只用于区分同名选手，检索时按主体名索引
            p["_base"] = split_nickname(p["nickname"])[0]
            for key in {
                normalize_name(p["nickname"]),
                p["nickname"].strip().lower(),
                normalize_name(p["_base"]),
            }:
                if key:
                    self._index.setdefault(key, []).append(p)
        self._pools = {}
        logger.info(
            f"[{PLUGIN_NAME}] 选手库已加载 {len(self.players)} 人（version={self.version or '未知'}）"
        )

    # -- 池子 --
    def pool(self, difficulty: str) -> list[dict[str, Any]]:
        difficulty = DIFFICULTY_ALIASES.get(difficulty, "normal")
        if difficulty not in self._pools:
            if difficulty == "normal":
                pool = list(self.players)
            else:
                pool = [p for p in self.players if difficulty in (p.get("difficulties") or [])]
            if not pool:
                pool = list(self.players)
            self._pools[difficulty] = pool
        return self._pools[difficulty]

    # -- 检索 --
    @staticmethod
    def _prominence(player: dict[str, Any]) -> tuple:
        """同名/近名排序：现役优先、Major 出场多者优先、名字短的优先。"""
        return (
            0 if player.get("is_active") else 1,
            -int(player.get("major_appearances") or 0),
            -int(player.get("major_championships") or 0),
            len(player["nickname"]),
        )

    def match(self, query: str) -> tuple[str, list[dict[str, Any]]]:
        """检索，返回 (匹配类型, 候选列表)。类型：exact / prefix / contains / none。"""
        key = normalize_name(query)
        if not key:
            return "none", []

        exact = self._index.get(key)
        if exact:
            return "exact", sorted(exact, key=self._prominence)[:5]

        starts, contains = [], []
        for p in self.players:
            name = normalize_name(p["nickname"])
            if name.startswith(key):
                starts.append(p)
            elif key in name:
                contains.append(p)
        if starts:
            return "prefix", sorted(starts, key=self._prominence)[:5]
        if contains:
            return "contains", sorted(contains, key=self._prominence)[:5]
        return "none", []

    def find(self, query: str) -> list[dict[str, Any]]:
        return self.match(query)[1]

    def by_nickname(self, nickname: str) -> dict[str, Any] | None:
        for p in self.players:
            if p["nickname"] == nickname:
                return p
        return None


# ============================================================
# 判定
# ============================================================


def _numeric(g: int, t: int, tolerance: int) -> tuple[str, str]:
    """数值型属性：完全一致 / 接近 / 不符 + 方向箭头。"""
    if g == t:
        return "correct", ""
    arrow = "↑" if t > g else "↓"
    level = "close" if abs(t - g) <= tolerance else "wrong"
    return level, arrow


def judge(guess: dict[str, Any], target: dict[str, Any]) -> list[tuple[str, str, str]]:
    """对比两位选手，返回 [(标签, 展示值, level), ...]，顺序固定为八项。

    规则对齐官方 compareGuess：
      - 国家/地区：相同=一致；不同但同赛区=接近
      - 赛区、位置：相同才算一致
      - 战队：相同=一致；该战队在答案的战队履历里=接近
      - 年龄（±3）、冠军数（±1）、出场数（±1）：带高低提示
      - 现役状态：相同才算一致
    """
    cells: list[tuple[str, str, str]] = []

    # 国家 / 地区
    if guess["nationality"] == target["nationality"]:
        cells.append(("国家", guess["nationality"], "correct"))
    elif guess.get("region") and guess.get("region") == target.get("region"):
        cells.append(("国家", guess["nationality"], "close"))
    else:
        cells.append(("国家", guess["nationality"], "wrong"))

    # 赛区
    cells.append(
        ("赛区", guess.get("region", "—"), "correct" if guess.get("region") == target.get("region") else "wrong")
    )

    # 战队
    history = target.get("team_history") or []
    if guess.get("team") == target.get("team"):
        cells.append(("战队", guess.get("team", "—"), "correct"))
    elif guess.get("team") and guess["team"] in history:
        cells.append(("战队", guess["team"], "close"))
    else:
        cells.append(("战队", guess.get("team", "—"), "wrong"))

    # 年龄
    level, arrow = _numeric(guess.get("age") or 0, target.get("age") or 0, AGE_TOLERANCE)
    cells.append(("年龄", f"{guess.get('age') or '—'}{arrow}", level))

    # 位置
    cells.append(
        ("位置", guess.get("role", "—"), "correct" if guess.get("role") == target.get("role") else "wrong")
    )

    # Major 冠军数
    level, arrow = _numeric(
        guess.get("major_championships") or 0,
        target.get("major_championships") or 0,
        MAJOR_TOLERANCE,
    )
    cells.append(("冠军", f"{guess.get('major_championships') or 0}{arrow}", level))

    # Major 出场数
    level, arrow = _numeric(
        guess.get("major_appearances") or 0,
        target.get("major_appearances") or 0,
        MAJOR_TOLERANCE,
    )
    cells.append(("出场", f"{guess.get('major_appearances') or 0}{arrow}", level))

    # 现役状态
    active_same = bool(guess.get("is_active")) == bool(target.get("is_active"))
    cells.append(
        ("状态", "现役" if guess.get("is_active") else "已退役", "correct" if active_same else "wrong")
    )
    return cells


def render_guess(index: int, total: int, guess: dict[str, Any], target: dict[str, Any]) -> str:
    """把一次猜测渲染成群聊可读的棋盘卡。"""
    cells = judge(guess, target)
    hit = all(c[2] == "correct" for c in cells)

    def cell(item: tuple[str, str, str]) -> str:
        label, value, level = item
        return f"{LEVEL_EMOJI[level]}{label} {value}"

    line1 = "　".join(cell(c) for c in cells[0:3])
    line2 = "　".join(cell(c) for c in cells[3:5])
    line3 = "　".join(cell(c) for c in cells[5:8])

    title = "🎯 命中" if hit else f"第 {index}/{total} 猜"
    return f"{title}｜{guess['nickname']}\n{line1}\n{line2}\n{line3}"


def player_card(p: dict[str, Any]) -> str:
    """选手资料卡。"""
    status = "现役" if p.get("is_active") else "已退役"
    return (
        f"📇 {p['nickname']}（{status}）\n"
        f"{p.get('nationality', '—')} · {p.get('region', '—')} · {p.get('team', '—')} · "
        f"{p.get('role', '—')} · {p.get('age', '—')} 岁\n"
        f"🏆 Major 冠军 {p.get('major_championships', 0)} 次 · 出场 {p.get('major_appearances', 0)} 次"
    )


# ============================================================
# 对局
# ============================================================


@dataclass
class Game:
    key: str
    umo: str
    group_id: str
    user_id: str
    user_name: str
    target: str  # 答案选手昵称
    difficulty: str
    created_at: float = field(default_factory=time.time)
    guessed: list[str] = field(default_factory=list)  # 已猜选手昵称
    revealed: list[str] = field(default_factory=list)  # 已揭示的属性标签
    task: asyncio.Task | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "umo": self.umo,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "target": self.target,
            "difficulty": self.difficulty,
            "created_at": self.created_at,
            "guessed": self.guessed,
            "revealed": self.revealed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Game:
        return cls(
            key=data["key"],
            umo=data["umo"],
            group_id=data.get("group_id", ""),
            user_id=data.get("user_id", ""),
            user_name=data.get("user_name", ""),
            target=data["target"],
            difficulty=data.get("difficulty", "normal"),
            created_at=data.get("created_at", time.time()),
            guessed=list(data.get("guessed") or []),
            revealed=list(data.get("revealed") or []),
        )


class SessionKey:
    """对局键：群内每人独立一局，私聊单独一局。"""

    @staticmethod
    def of(group_id: str, user_id: str) -> str:
        return f"{group_id or 'private'}:{user_id}"


# ============================================================
# 插件
# ============================================================


class FribergGuessPlugin(Star):
    """弗一把 · 猜 CS Major 选手

    群里发「弗一把」开局，然后直接发选手昵称开猜。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config or {}
        self.db = PlayerDB()
        self.games: dict[str, Game] = {}
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

    def _resolve_data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = Path(get_astrbot_data_path())
        except Exception:  # pragma: no cover - 极端情况下退回系统临时目录
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
        stats = data.get("stats") or {}
        self.stats = {
            "users": stats.get("users") or {},
            "groups": stats.get("groups") or {},
        }

    def _save_state(self) -> None:
        payload = {
            "games": [g.to_dict() for g in self.games.values()],
            "stats": self.stats,
        }
        tmp = self.state_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 保存存档失败：{exc}")

    def _restart_timers(self) -> None:
        """插件重载后，为存量对局重建超时计时器。"""
        for game in list(self.games.values()):
            if self.games.get(game.key) is game:
                self._spawn_timer(game)

    def _spawn_timer(self, game: Game) -> None:
        minutes = int(self._cfg("timeout_minutes", 30) or 0)
        if minutes <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 非事件循环上下文（例如单元测试）直接跳过
            return
        game.task = loop.create_task(self._timeout_watch(game, minutes * 60))

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
        target = self.db.by_nickname(game.target)
        text = (
            f"⏳ 时间到，本局结束。答案：{game.target}\n"
            + (player_card(target) + "\n" if target else "")
            + "发送「弗一把」再来一局。"
        )
        with contextlib.suppress(Exception):
            await self.context.send_message(game.umo, MessageChain().message(text))

    # ---------------- 战绩 ----------------

    def _settle_stats(self, game: Game, won: bool) -> None:
        user = self.stats["users"].setdefault(
            game.key, {"name": game.user_name, "wins": 0, "plays": 0, "guesses": 0, "best": 0}
        )
        user["name"] = game.user_name or user.get("name", "")
        user["plays"] = int(user.get("plays", 0)) + 1
        user["guesses"] = int(user.get("guesses", 0)) + len(game.guessed)
        if won:
            user["wins"] = int(user.get("wins", 0)) + 1
            best = len(game.guessed)
            if not user.get("best") or best < int(user["best"]):
                user["best"] = best
        group = self.stats["groups"].setdefault(game.group_id or "private", {"plays": 0, "wins": 0})
        group["plays"] = int(group.get("plays", 0)) + 1
        if won:
            group["wins"] = int(group.get("wins", 0)) + 1

    def _user_stats_text(self, game_key: str) -> str:
        stat = self.stats["users"].get(game_key)
        if not stat or not stat.get("plays"):
            return "还没有战绩，发送「弗一把」开一局。"
        plays = int(stat["plays"])
        avg = int(stat["guesses"]) / plays if plays else 0
        best = stat.get("best") or 0
        return (
            f"📊 {stat.get('name', '你')}：{int(stat['wins'])} 胜 / {plays} 局"
            f"（胜率 {int(stat['wins']) / plays:.0%}）\n"
            f"平均每次消耗 {avg:.1f} 猜" + (f"，最少 {best} 猜命中" if best else "")
        )

    # ---------------- 开新局 ----------------

    def _pick_target(self, difficulty: str) -> dict[str, Any]:
        pool = self.db.pool(difficulty)
        return random.choice(pool)

    def _start_text(self, game: Game) -> str:
        attempts = int(self._cfg("max_attempts", DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS)
        minutes = int(self._cfg("timeout_minutes", 30) or 0)
        name, hint = DIFFICULTIES.get(game.difficulty, DIFFICULTIES["normal"])
        pool_size = len(self.db.pool(game.difficulty))
        limit = f"｜限时 {minutes} 分钟" if minutes > 0 else ""
        return (
            f"🎮 弗一把 · 猜 CS Major 选手\n"
            f"难度：{name}（{hint} {pool_size} 人）｜机会：{attempts} 次{limit}\n"
            f"直接发选手昵称就能猜（大小写不限，可以只写前几个字母）\n"
            f"其它指令：提示 · 放弃 · 战绩 · 排行 · 图鉴 <名字>\n"
            f"{LEGEND}"
        )

    def _begin(self, event: AstrMessageEvent, raw_difficulty: str) -> str:
        """开一局，返回要回复的文本（失败时返回提示文本）。"""
        key = SessionKey.of(event.get_group_id(), event.get_sender_id())
        if key in self.games:
            return "这局还没结束呢，先猜，或者发「放弃」结束。"
        if not self.db.players:
            return "选手库没加载成功，检查一下插件目录下的 data/players.json。"

        raw_diff = (raw_difficulty or "").strip()
        if raw_diff:
            diff = DIFFICULTY_ALIASES.get(raw_diff, "")
            if not diff:
                return "难度只有：入门 / 简单 / 普通。"
        else:
            configured = str(self._cfg("default_difficulty", "normal"))
            diff = DIFFICULTY_ALIASES.get(configured, "normal")

        target = self._pick_target(diff)
        game = Game(
            key=key,
            umo=event.unified_msg_origin,
            group_id=event.get_group_id(),
            user_id=event.get_sender_id(),
            user_name=event.get_sender_name(),
            target=target["nickname"],
            difficulty=diff,
        )
        self.games[key] = game
        self._spawn_timer(game)
        self._save_state()
        # 答案只留在插件内部，不进入任何发出去的文本
        return self._start_text(game)

    # ---------------- 事件入口 ----------------

    @filter.command("弗一把", alias={"猜选手", "cs猜选手"})
    async def start_game(self, event: AstrMessageEvent, difficulty: str = ""):
        """开一局猜选手。可带难度：入门 / 简单 / 普通"""
        yield event.chain_result(self._with_at(event, self._begin(event, difficulty)))
        event.stop_event()

    def _apply_guess(self, game: Game, guess: dict[str, Any]) -> str:
        """执行一次猜测，返回要发出的文本；对局结束时清理状态。"""
        if guess["nickname"] in game.guessed:
            return f"「{guess['nickname']}」已经猜过了，换一个。"
        target = self.db.by_nickname(game.target)
        if not target:
            self.games.pop(game.key, None)
            return "这一局的答案数据丢了，发送「弗一把」重新开一局吧。"

        game.guessed.append(guess["nickname"])
        attempts = int(self._cfg("max_attempts", DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS)
        cells = judge(guess, target)
        board = render_guess(len(game.guessed), attempts, guess, target)

        if all(c[2] == "correct" for c in cells):
            self.games.pop(game.key, None)
            if game.task:
                game.task.cancel()
            self._settle_stats(game, won=True)
            self._save_state()
            return (
                f"{board}\n\n🎉 猜中了！{len(game.guessed)} 猜命中 {target['nickname']}\n{player_card(target)}\n"
                + self._user_stats_text(game.key)
            )

        if len(game.guessed) >= attempts:
            self.games.pop(game.key, None)
            if game.task:
                game.task.cancel()
            self._settle_stats(game, won=False)
            self._save_state()
            return (
                f"{board}\n\n💀 {attempts} 次机会用完了，答案是 {target['nickname']}\n{player_card(target)}\n"
                "发送「弗一把」再来一局。"
            )

        self._save_state()
        left = attempts - len(game.guessed)
        text = board
        if self._cfg("show_legend", True) and len(game.guessed) == 1:
            text += f"\n{LEGEND}"
        if len(game.guessed) == attempts - 1:
            text += "\n⚠️ 最后一次机会了。"
        else:
            text += f"\n还剩 {left} 次机会"
        return text

    def _with_at(self, event: AstrMessageEvent, text: str) -> list:
        """群聊里带上 @，私聊或关闭配置时纯文本。"""
        if self._cfg("at_sender", True) and event.get_group_id():
            with contextlib.suppress(Exception):
                return [At(qq=event.get_sender_id()), Plain(f" {text}")]
        return [Plain(text)]

    # ---------------- 其它指令 ----------------

    def _hint_text(self, event: AstrMessageEvent) -> str:
        """给一条线索，每局限用若干次。"""
        if not self._cfg("enable_hint", True):
            return "本插件已关闭提示功能。"
        key = SessionKey.of(event.get_group_id(), event.get_sender_id())
        game = self.games.get(key)
        if not game:
            return "现在没有进行中的对局，发送「弗一把」开局。"

        limit = int(self._cfg("hint_limit", 2) or 0)
        if len(game.revealed) >= limit:
            return f"提示已经用完了（每局 {limit} 次）。"

        target = self.db.by_nickname(game.target)
        if not target:
            self.games.pop(key, None)
            return "这一局的数据丢了，重新开一局吧。"

        fields = [
            ("位置", f"打的是 {target.get('role', '—')}"),
            ("赛区", f"来自 {target.get('region', '—')} 赛区"),
            ("国家", f"{target.get('nationality', '—')} 人"),
            ("冠军", f"Major 冠军 {target.get('major_championships', 0)} 次"),
            ("出场", f"Major 出场 {target.get('major_appearances', 0)} 次"),
            ("状态", "现役选手" if target.get("is_active") else "已经退役"),
            ("年龄", f"{target.get('age', '—')} 岁"),
        ]
        pool = [f for f in fields if f[0] not in game.revealed]
        if not pool:
            return "没什么可提示的了，直接猜吧。"
        label, value = random.choice(pool)
        game.revealed.append(label)
        self._save_state()
        return f"💡 线索：{value}（提示还剩 {limit - len(game.revealed)} 次）"

    def _giveup_text(self, event: AstrMessageEvent) -> str:
        """放弃本局并公布答案。"""
        key = SessionKey.of(event.get_group_id(), event.get_sender_id())
        game = self.games.pop(key, None)
        if not game:
            return "现在没有进行中的对局。"
        if game.task:
            game.task.cancel()
        self._settle_stats(game, won=False)
        self._save_state()
        target = self.db.by_nickname(game.target)
        return f"🏳️ 已放弃。答案：{game.target}\n" + (player_card(target) if target else "")

    def _stats_text(self, event: AstrMessageEvent) -> str:
        key = SessionKey.of(event.get_group_id(), event.get_sender_id())
        return self._user_stats_text(key)

    def _rank_text(self, event: AstrMessageEvent) -> str:
        gid = event.get_group_id() or "private"
        prefix = f"{gid}:"
        rows = [
            (value, key)
            for key, value in self.stats["users"].items()
            if key.startswith(prefix) and int(value.get("wins", 0)) > 0
        ]
        if not rows:
            return "本群还没有人猜中过，来开个记录？发送「弗一把」。"
        rows.sort(key=lambda item: (-int(item[0].get("wins", 0)), int(item[0].get("guesses", 0))))
        lines = ["🏆 本群战绩榜"]
        for i, (stat, _key) in enumerate(rows[:10], start=1):
            plays = int(stat.get("plays", 0)) or 1
            avg = int(stat.get("guesses", 0)) / plays
            lines.append(
                f"{i}. {stat.get('name', '匿名')}　{int(stat.get('wins', 0))} 胜 / {plays} 局"
                f"　平均 {avg:.1f} 猜"
            )
        return "\n".join(lines)

    def _lookup_text(self, event: AstrMessageEvent, query: str) -> str:
        if not query:
            return "用法：图鉴 <选手昵称>"
        candidates = self.db.find(query)
        if not candidates:
            return f"没找到「{query}」，试试英文昵称。"
        if len(candidates) > 1:
            names = " / ".join(p["nickname"] for p in candidates)
            return f"找到多个：{names}，换个更精确的名字。"
        return player_card(candidates[0])

    @filter.command("提示", alias={"线索"})
    async def hint(self, event: AstrMessageEvent):
        """给一条线索，每局限用若干次。"""
        yield event.plain_result(self._hint_text(event))
        event.stop_event()

    @filter.command("放弃", alias={"结束", "认输"})
    async def give_up(self, event: AstrMessageEvent):
        """放弃本局并公布答案。"""
        yield event.plain_result(self._giveup_text(event))
        event.stop_event()

    @filter.command("战绩")
    async def my_stats(self, event: AstrMessageEvent):
        """查看自己的猜选手战绩。"""
        yield event.plain_result(self._stats_text(event))
        event.stop_event()

    @filter.command("排行", alias={"排行榜"})
    async def leaderboard(self, event: AstrMessageEvent):
        """本群胜场排行。"""
        yield event.plain_result(self._rank_text(event))
        event.stop_event()

    @filter.command("图鉴", alias={"查选手"})
    async def lookup(self, event: AstrMessageEvent, query: str = ""):
        """查选手资料，例如 图鉴 s1mple"""
        yield event.plain_result(self._lookup_text(event, query))
        event.stop_event()

    @filter.command("弗一把帮助", alias={"猜选手帮助"})
    async def help(self, event: AstrMessageEvent):
        """玩法说明。"""
        attempts = int(self._cfg("max_attempts", DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS)
        yield event.plain_result(
            "🎮 弗一把 · 猜 CS Major 选手\n"
            f"1. 发「弗一把」或「弗一把 简单」开局，共 {attempts} 次机会\n"
            "2. 直接发选手昵称就能猜（也支持「猜 niko」）\n"
            "3. 每次猜测按 国家/赛区/战队/年龄/位置/冠军/出场/状态 八项反馈\n"
            f"   {LEGEND}\n"
            "4. 指令：提示 · 放弃 · 战绩 · 排行 · 图鉴 <名字>\n"
            f"选手库：{len(self.db.players)} 人（社区数据，version {self.db.version or '未知'}）"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """自由文本入口：裸口令开局、常用口令、直接发昵称猜。"""
        raw = (event.message_str or "").strip()
        if not raw:
            return

        key = SessionKey.of(event.get_group_id(), event.get_sender_id())
        game = self.games.get(key)

        # 1) 开局（带唤醒前缀时上面的指令已经响应，这里只兜底无前缀/私聊直呼）
        matched = _START_RE.match(raw)
        if matched:
            event.stop_event()
            text = self._begin(event, (matched.group(1) or "").strip())
            await event.send(event.chain_result(self._with_at(event, text)))
            return

        # 2) 图鉴 xxx
        looked = _LOOKUP_RE.match(raw)
        if looked:
            event.stop_event()
            await event.send(
                event.plain_result(self._lookup_text(event, (looked.group(1) or "").strip()))
            )
            return

        # 3) 其它裸口令
        method = _BARE_COMMANDS.get(raw.lstrip("/／").strip())
        if method:
            # 没有对局时，「提示 / 放弃」保持沉默，避免在群里误触发
            if method in _SILENT_WITHOUT_GAME and not game:
                return
            event.stop_event()
            await event.send(event.plain_result(getattr(self, method)(event)))
            return

        # 4) 直接发选手昵称猜
        if not self._cfg("free_text_guess", True):
            return
        if not game or len(raw) > 32:
            return
        query = _TRAILING_RE.sub("", _LEADING_RE.sub("", raw)).strip()
        if not query or re.search(r"[?？]$", raw):
            return

        kind, candidates = self.db.match(query)
        if not candidates:
            return
        if len(candidates) > 1:
            if kind == "exact":
                # 数据里有「NiKo（波黑）」和「niko(丹麦)」这类同名条目，取最有名的那个
                candidates = candidates[:1]
            elif _LEADING_RE.match(raw):
                names = " / ".join(p["nickname"] for p in candidates)
                event.stop_event()
                await event.send(event.plain_result(f"你想猜谁？{names}"))
                return
            else:
                # 模糊命中多个，且没有「猜」的明确意图，不抢普通聊天
                return

        event.stop_event()
        async with self._lock:
            result = self._apply_guess(game, candidates[0])
        if result is not None:
            await event.send(event.chain_result(self._with_at(event, result)))

    async def terminate(self) -> None:
        """插件卸载 / 重载时取消计时器并落盘。"""
        for game in list(self.games.values()):
            if game.task:
                game.task.cancel()
        self._save_state()
        logger.info(f"[{PLUGIN_NAME}] 已卸载，状态已保存")
