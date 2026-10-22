"""自测脚本：用桩模块替掉 astrbot，验证判定、检索、整局流程与存档。

用法：python tests/selftest.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------
# 桩：最小可用的 astrbot API
# ------------------------------------------------------------------


def _mod(name: str, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _EventMessageType:
    ALL = "all"
    GROUP_MESSAGE = "group"
    PRIVATE_MESSAGE = "private"


class _Filter:
    EventMessageType = _EventMessageType

    @staticmethod
    def _decorator(*_args, **_kwargs):
        def wrap(func):
            return func

        return wrap

    command = staticmethod(lambda *a, **k: _Filter._decorator())
    command_group = staticmethod(lambda *a, **k: _Filter._decorator())
    event_message_type = staticmethod(lambda *a, **k: _Filter._decorator())
    permission_type = staticmethod(lambda *a, **k: _Filter._decorator())
    regex = staticmethod(lambda *a, **k: _Filter._decorator())


Filter = _Filter


class _AstrMessageEvent:
    pass


class _MessageChain:
    def __init__(self):
        self.parts = []

    def message(self, text):
        self.parts.append(text)
        return self


class _Component:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        if args:
            self.kwargs.setdefault("text", args[0])


class _Context:
    def __init__(self):
        self.sent = []

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True


class _Star:
    def __init__(self, context=None, config=None):
        self.context = context
        self.name = "astrbot_plugin_friberg_guess"


def _install_stubs(data_dir: Path) -> None:
    import logging

    _mod("astrbot")
    _mod("astrbot.api", AstrBotConfig=dict, logger=logging.getLogger("stub"))
    _mod(
        "astrbot.api.event",
        AstrMessageEvent=_AstrMessageEvent,
        MessageChain=_MessageChain,
        filter=_Filter,
    )
    _mod(
        "astrbot.api.message_components",
        At=_Component,
        Plain=_Component,
    )
    _mod("astrbot.api.star", Context=_Context, Star=_Star)
    _mod("astrbot.core")
    _mod("astrbot.core.utils")
    _mod("astrbot.core.utils.astrbot_path", get_astrbot_data_path=lambda: str(data_dir))


def _load_plugin():
    spec = importlib.util.spec_from_file_location("friberg_main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["friberg_main"] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------
# 假事件
# ------------------------------------------------------------------


class FakeEvent:
    def __init__(self, text="", group="10086", user="9527", name="测试员"):
        self.message_str = text
        self._group = group
        self._user = user
        self._name = name
        self.unified_msg_origin = f"umo:{group}:{user}"
        self.is_at_or_wake_command = False
        self.results = []
        self.stopped = False

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._user

    def get_sender_name(self):
        return self._name

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    async def send(self, result):
        self.results.append(result)

    def stop_event(self):
        self.stopped = True


def _flatten(result) -> str:
    kind, payload = result
    if kind == "plain":
        return payload
    return "".join(str(getattr(p, "kwargs", {}).get("text", "")) for p in payload)


async def run_commands(handler, event, *args):
    """指令 handler 可能是 async generator，也可能是协程。"""
    out = []
    result = handler(event, *args)
    if hasattr(result, "__aiter__"):
        async for item in result:
            out.append(item)
    else:
        await result
    return out


# ------------------------------------------------------------------
# 断言
# ------------------------------------------------------------------


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="friberg-selftest-"))
    _install_stubs(tmp)
    m = _load_plugin()
    print("[1] 选手库")
    db = m.PlayerDB()
    check(len(db.players) >= 600, f"加载 {len(db.players)} 位选手")
    check(db.find("simple")[0]["nickname"] == "s1mple", "simple → s1mple（leet 归一）")
    check(db.find("S1MPLE")[0]["nickname"] == "s1mple", "大小写不敏感")
    check(db.find("zywo")[0]["nickname"] == "ZywOo", "前缀匹配 zywo → ZywOo")
    kind, cands = db.match("niko")
    check(kind == "exact" and cands[0]["nickname"].startswith("NiKo"), "同名条目按知名度取 NiKo")
    kind, _ = db.match("zywo")
    check(kind == "prefix", "匹配类型可区分 exact / prefix")
    check(db.find("绝对不存在的名字") == [], "无结果时返回空列表")
    check(len(db.pool("beginner")) < len(db.pool("normal")), "入门池小于普通池")

    print("[2] 判定规则")
    smpl = db.by_nickname("s1mple")
    zywoo = db.by_nickname("ZywOo")
    cells = m.judge(smpl, smpl)
    check(all(c[2] == "correct" for c in cells), "自己猜自己全绿")
    cells = dict((c[0], (c[1], c[2])) for c in m.judge(smpl, zywoo))
    check(cells["国家"] == ("乌克兰", "close"), "不同国家同赛区 → 黄")
    check(cells["赛区"] == ("欧洲", "correct"), "同赛区 → 绿")
    check(cells["战队"][1] == "wrong", "战队不同且不在履历 → 红")
    check(cells["年龄"] == ("28↓", "close"), "年龄差 3 岁 → 黄 + 答案更小")
    check(cells["冠军"] == ("1↑", "wrong"), "冠军差 2 → 红 + 答案更大")
    check(cells["状态"] == ("现役", "correct"), "同为现役 → 绿")

    print("[3] 棋盘渲染")
    board = m.render_guess(1, 8, smpl, zywoo)
    check(board.count("\n") == 3, "四行结构")
    check("🟨国家" in board and "🟩赛区" in board, "带颜色与属性名")

    print("[4] 整局流程")
    plugin = m.FribergGuessPlugin(_Context(), {"timeout_minutes": 0})
    plugin.db = db
    ev = FakeEvent()
    out = await run_commands(plugin.start_game, ev, "")
    check(out and "机会：8 次" in _flatten(out[0]), "开局提示包含机会次数")
    check(len(plugin.games) == 1, "对局已创建")
    key = m.SessionKey.of("10086", "9527")
    check(plugin.games[key].target not in _flatten(out[0]), "开局不泄露答案")
    target = plugin.db.by_nickname(plugin.games[key].target)

    wrong = db.by_nickname("ZywOo") if target["nickname"] != "ZywOo" else db.by_nickname("device")
    text = plugin._apply_guess(plugin.games[key], wrong)
    check("还剩 7 次机会" in text, "错误猜测扣减机会")
    text = plugin._apply_guess(plugin.games[key], wrong)
    check("已经猜过" in text, "重复猜测被拦下")

    text = plugin._apply_guess(plugin.games[key], target)
    check("猜中了" in text and "Major 冠军" in text, "猜中即结束并给出资料卡")
    check(key not in plugin.games, "结束后清理对局")
    check(plugin.stats["users"][key]["wins"] == 1, "战绩记录胜场")

    print("[5] 提示 / 图鉴 / 排行")
    ev2 = FakeEvent()
    await run_commands(plugin.start_game, ev2, "简单")
    hint_out = await run_commands(plugin.hint, ev2)
    check("线索" in _flatten(hint_out[0]), "提示可用")
    lookup_out = await run_commands(plugin.lookup, ev2, "ZywOo")
    check("Major 冠军" in _flatten(lookup_out[0]), "图鉴返回完整资料")
    rank_out = await run_commands(plugin.leaderboard, ev2)
    check("本群战绩榜" in _flatten(rank_out[0]), "排行可用")

    print("[6] 无指令猜测与存档")
    ev3 = FakeEvent(text="zywoo")
    await plugin.on_message(ev3)
    check(ev3.stopped is True and ev3.results, "自由文本猜测被识别并拦截事件传播")

    ev_np = FakeEvent(text="弗一把 入门", group="30000", user="7")
    await plugin.on_message(ev_np)
    check(
        m.SessionKey.of("30000", "7") in plugin.games,
        "没加唤醒前缀也能开局",
    )
    check(plugin.games[m.SessionKey.of("30000", "7")].difficulty == "beginner", "难度参数生效")

    ev_chat = FakeEvent(text="今天天气不错我们出去玩吧", group="30000", user="7")
    await plugin.on_message(ev_chat)
    check(not ev_chat.results, "普通聊天不会被误当成猜测")

    ev_hint = FakeEvent(text="提示", group="30000", user="7")
    await plugin.on_message(ev_hint)
    check(ev_hint.results and "线索" in _flatten(ev_hint.results[0]), "不带唤醒前缀也能用提示")

    ev_look = FakeEvent(text="图鉴 zywoo", group="30000", user="7")
    await plugin.on_message(ev_look)
    check(ev_look.results and "Major 冠军" in _flatten(ev_look.results[0]), "不带前缀也能查图鉴")

    check(plugin.state_file.exists(), "状态已落盘")
    reloaded = m.FribergGuessPlugin(_Context(), {"timeout_minutes": 0})
    check(len(reloaded.stats["users"]) == 1, "重启后战绩仍在")

    print("[7] 私聊")
    pv = m.FribergGuessPlugin(_Context(), {"timeout_minutes": 0})
    pv.db = db

    async def send(text, group="", user="8888", wake=False):
        ev = FakeEvent(text=text, group=group, user=user, name="私聊用户")
        ev.is_at_or_wake_command = wake
        await pv.on_message(ev)
        return ev

    # 私聊里消息本来就算已唤醒，指令路径可以直接命中
    ev_priv = FakeEvent(text="弗一把", group="", user="8888", name="私聊用户")
    ev_priv.is_at_or_wake_command = True
    out = await run_commands(pv.start_game, ev_priv, "")
    check(out and "机会" in _flatten(out[0]), "私聊能开局（指令路径）")
    check(ev_priv.stopped is True, "开局后终止事件传播，避免重复响应")
    priv_key = m.SessionKey.of("", "8888")
    check(priv_key in pv.games, "私聊对局用 private 键")

    # 私聊里直接发昵称
    ev_guess = await send("niko", wake=True)
    check(ev_guess.results and "猜" in _flatten(ev_guess.results[0]), "私聊能直接发昵称猜")

    # 私聊里裸口令
    ev_hint2 = await send("提示", wake=True)
    check(ev_hint2.results and "线索" in _flatten(ev_hint2.results[0]), "私聊裸口令「提示」可用")

    ev_stat = await send("战绩", wake=True)
    check(ev_stat.results and "战绩" in _flatten(ev_stat.results[0]), "私聊「战绩」可用")

    # 前缀没被剥掉的情况（自建前缀或私聊无需前缀时可能残留斜杠）
    ev_slash = await send("/弗一把", user="9999")
    check(m.SessionKey.of("", "9999") in pv.games, "私聊里带斜杠也能开局")

    # 没有对局时，裸口令不该在群里乱回
    ev_quiet = FakeEvent(text="提示", group="40000", user="1", name="路人")
    await pv.on_message(ev_quiet)
    check(not ev_quiet.results, "无对局时裸口令「提示」保持沉默")

    ev_end = await send("放弃", wake=True)
    check(ev_end.results and "答案" in _flatten(ev_end.results[0]), "私聊「放弃」公布答案")

    print("[8] 超时")
    plugin2 = m.FribergGuessPlugin(_Context(), {"timeout_minutes": 1})
    plugin2.db = db
    ev4 = FakeEvent(group="20000", user="1")
    await run_commands(plugin2.start_game, ev4)
    game = plugin2.games[m.SessionKey.of("20000", "1")]
    game.created_at -= 120  # 造一个过期的对局
    await plugin2._timeout_watch(game, 60)
    check(m.SessionKey.of("20000", "1") not in plugin2.games, "超时自动结算")
    check(plugin2.context.sent, "超时主动推送答案")

    print("\n全部自测通过 ✅")
    print("示例棋盘：\n" + m.render_guess(1, 8, smpl, zywoo))


if __name__ == "__main__":
    asyncio.run(main())
