"""自测脚本：用桩模块替掉 astrbot，验证三个题库的判定、检索、整局流程与存档。

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
    _mod("astrbot.api.event", AstrMessageEvent=_AstrMessageEvent, MessageChain=_MessageChain, filter=_Filter)
    _mod("astrbot.api.message_components", At=_Component, Plain=_Component)
    _mod("astrbot.api.star", Context=_Context, Star=_Star)
    _mod("astrbot.core")
    _mod("astrbot.core.utils")
    _mod("astrbot.core.utils.astrbot_path", get_astrbot_data_path=lambda: str(data_dir))


def _load_plugin():
    spec = importlib.util.spec_from_file_location("guess_main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["guess_main"] = module
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
    out = []
    result = handler(event, *args)
    if hasattr(result, "__aiter__"):
        async for item in result:
            out.append(item)
    else:
        await result
    return out


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="guess-selftest-"))
    _install_stubs(tmp)
    m = _load_plugin()

    print("[1] 题库加载")
    cs = m.PlayerDB(m.THEMES["cs"], ROOT / "data" / "players.json")
    fb = m.PlayerDB(m.THEMES["football"], ROOT / "data" / "football_players.json")
    check(len(cs.players) >= 600, f"CS 题库 {len(cs.players)} 人")
    check(len(fb.players) >= 1000, f"足球题库 {len(fb.players)} 人")
    lol_path = ROOT / "data" / "lol_players.json"
    if lol_path.exists():
        lol_db = m.PlayerDB(m.THEMES["lol"], lol_path)
        check(len(lol_db.players) >= 500, f"LoL 题库 {len(lol_db.players)} 人")
    else:
        print("  · LoL 题库未导入（data/lol_players.json 缺失），本次跳过")

    print("[2] 检索")
    check(cs.find("simple")[0]["nickname"] == "s1mple", "CS：simple → s1mple")
    check(cs.find("niko")[0]["nickname"].startswith("NiKo"), "CS：同名取知名度更高的 NiKo")
    check(fb.find("哈兰德")[0]["name"] == "Erling Haaland", "足球：中文名可检索")
    check(fb.find("haaland")[0]["cnName"] == "哈兰德", "足球：英文名可检索")
    check(fb.find("魔笛") or fb.find("莫德里奇"), "足球：别名可检索")

    print("[3] CS 判定")
    smpl, zywoo = cs.get("s1mple"), cs.get("ZywOo")
    cells = m.THEMES["cs"].judge(smpl, smpl)
    check(all(c.level == "correct" for c in cells), "CS：自己猜自己全绿")
    cells = {c.label: (c.value, c.level) for c in m.THEMES["cs"].judge(smpl, zywoo)}
    check(cells["国家"][1] == "close", "CS：不同国家同赛区 → 黄")
    check(cells["战队"][1] == "wrong", "CS：战队不同 → 红")
    check(cells["年龄"] == ("28↓", "close"), "CS：年龄差 3 → 黄 + 更小")
    check(cells["冠军"] == ("1↑", "wrong"), "CS：冠军差 2 → 红 + 更大")

    print("[4] 足球判定")
    fbt = m.THEMES["football"]
    haaland = fb.find("哈兰德")[0]
    rodri = fb.find("罗德里")[0]
    cells = {c.label: (c.value, c.level) for c in fbt.judge(haaland, rodri)}
    check(cells["国籍"][1] == "close", "足球：不同国籍同大洲 → 黄")
    check(cells["联赛"][1] == "wrong", "足球：联赛不同 → 红")
    check(cells["位置"][1] == "wrong", "足球：位置不同 → 红")
    check(cells["出生年"][1] == "wrong" and cells["出生年"][0].endswith("↓"), "足球：出生年差 4 → 红 + 更小")
    check(cells["世界杯"][1] == "close", "足球：世界杯届数差 1 → 黄")
    check(cells["世界杯冠军"][1] == "wrong", "足球：世界杯夺冠必须一致")
    same = fbt.judge(haaland, haaland)
    check(all(c.level == "correct" for c in same), "足球：自己猜自己全绿")

    print("[4b] LoL 判定")
    lol_theme = m.THEMES["lol"]
    raw = {
        "players": [
            {"gameId": "A", "realName": "a", "age": 30, "currentTeam": "T1", "country": "韩国",
             "continent": "亚洲", "positions": ["中单"], "status": "Active"},
            {"gameId": "B", "realName": "b", "age": 25, "currentTeam": "Gen.G", "country": "中国",
             "continent": "亚洲", "positions": ["上单"], "status": "Retired"},
        ]
    }
    ga, gb = lol_theme.load(raw)
    cells = {c.label: (c.value, c.level) for c in lol_theme.judge(ga, gb)}
    check(cells["位置"][1] == "wrong", "LoL：位置不同 → 红")
    check(cells["国家"][1] == "close", "LoL：不同国家同大洲 → 黄")
    check(cells["大洲"][1] == "correct", "LoL：同大洲 → 绿")
    check(cells["年龄"][1] == "wrong" and cells["年龄"][0].endswith("↓"), "LoL：年龄差 5 → 红 + 更小")
    check(cells["状态"][1] == "wrong", "LoL：现役 vs 退役 → 红")
    check(all(c.level == "correct" for c in lol_theme.judge(ga, ga)), "LoL：自己猜自己全绿")

    print("[5] 棋盘渲染")
    board = m.render_board(fbt, 1, 10, haaland, rodri)
    check(board.count("\n") == len(fbt.rows), f"足球棋盘 {len(fbt.rows) + 1} 行")
    check("🟨国籍" in board, "带颜色与属性名")
    board_cs = m.render_board(m.THEMES["cs"], 1, 8, smpl, zywoo)
    check(board_cs.startswith("第 1/8 猜｜s1mple"), "CS 棋盘标题")

    print("[6] CS 整局")
    plugin = m.GuessProPlugin(_Context(), {"timeout_minutes": 0})
    ev = FakeEvent()
    out = await run_commands(plugin.start_cs, ev, "")
    check(out and "机会：8 次" in _flatten(out[0]), "CS 开局提示次数")
    check(ev.stopped is True, "开局后终止事件传播")
    gkey = m.SessionKey.game("10086", "9527", "cs")
    target = plugin.dbs["cs"].get(plugin.games[gkey].target)
    wrong = cs.get("ZywOo") if target["nickname"] != "ZywOo" else cs.get("device")
    text = plugin._apply_guess(plugin.games[gkey], wrong)
    check("还剩 7 次机会" in text, "错误猜测扣减机会")
    check("已经猜过" in plugin._apply_guess(plugin.games[gkey], wrong), "重复猜测被拦下")
    text = plugin._apply_guess(plugin.games[gkey], target)
    check("猜中了" in text, "猜中即结束")
    check(gkey not in plugin.games, "对局已清理")

    print("[7] 足球整局与题库切换")
    ev2 = FakeEvent(text="足一把")
    await plugin.on_message(ev2)
    fkey = m.SessionKey.game("10086", "9527", "football")
    check(fkey in plugin.games, "裸口令能开足球局")
    check(_flatten(ev2.results[0]).count("机会：10 次") == 1, "足球默认 10 次机会")
    game = plugin.games[fkey]
    guess = plugin.dbs["football"].find("哈兰德")[0]
    target = plugin.dbs["football"].get(game.target)
    pick = guess if guess is not target else plugin.dbs["football"].find("罗德里")[0]
    text = plugin._apply_guess(game, pick)
    check("次机会" in text, "足球猜测有反馈")
    ev3 = FakeEvent(text="提示")
    await plugin.on_message(ev3)
    check(ev3.results and "线索" in _flatten(ev3.results[0]), "裸口令提示可用")
    check(plugin._current_theme(ev3) == "football", "当前题库跟随最近开局")
    ev4 = FakeEvent(text="放弃")
    await plugin.on_message(ev4)
    check("答案" in _flatten(ev4.results[0]), "放弃公布答案")

    print("[8] 图鉴与排行")
    ev5 = FakeEvent(text="图鉴 哈兰德")
    await plugin.on_message(ev5)
    check("欧冠" in _flatten(ev5.results[0]), "图鉴返回球员资料")
    ev6 = FakeEvent(text="排行")
    await plugin.on_message(ev6)
    rank_text = _flatten(ev6.results[0])
    check(("战绩榜" in rank_text) or ("还没有人猜中" in rank_text), "排行可用")

    print("[9] 私聊")
    pv = m.GuessProPlugin(_Context(), {"timeout_minutes": 0})
    synthetic = Path(tempfile.mkdtemp(prefix="lol-db-")) / "lol.json"
    synthetic.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    pv.dbs["lol"] = m.PlayerDB(m.THEMES["lol"], synthetic)
    pev = FakeEvent(group="", user="8888", name="私聊用户")
    pev.is_at_or_wake_command = True
    out = await run_commands(pv.start_lol, pev, "")
    check(out and "撸一把" in _flatten(out[0]), "私聊能开撸一把")

    async def say(text, wake=True):
        ev_ = FakeEvent(text=text, group="", user="8888", name="私聊用户")
        ev_.is_at_or_wake_command = wake
        await pv.on_message(ev_)
        return ev_

    lkey = m.SessionKey.game("", "8888", "lol")
    lol_target = pv.dbs["lol"].get(pv.games[lkey].target)
    ev_guess = await say(lol_target["gameId"])
    check(ev_guess.results and ("猜中了" in _flatten(ev_guess.results[0])), "私聊猜中 LoL 选手")

    pev2 = FakeEvent(text="/足一把", group="", user="9999")
    await pv.on_message(pev2)
    check(m.SessionKey.game("", "9999", "football") in pv.games, "私聊带斜杠也能开局")

    print("[10] 存档与超时")
    check(pv.state_file.exists(), "状态已落盘")
    reloaded = m.GuessProPlugin(_Context(), {"timeout_minutes": 0})
    check(len(reloaded.games) == len(pv.games), "重启后对局仍在")
    plugin2 = m.GuessProPlugin(_Context(), {"timeout_minutes": 1})
    ev7 = FakeEvent(group="20000", user="1")
    await run_commands(plugin2.start_football, ev7)
    game = plugin2.games[m.SessionKey.game("20000", "1", "football")]
    game.created_at -= 120
    await plugin2._timeout_watch(game, 60)
    check(m.SessionKey.game("20000", "1", "football") not in plugin2.games, "超时自动结算")
    check(plugin2.context.sent, "超时主动推送答案")

    print("[11] 难度分档与仅私聊")
    diff_plugin = m.GuessProPlugin(_Context(), {"timeout_minutes": 0})
    expect = {"易": "easy", "中": "normal", "难": "hard"}
    for word, pool_key in expect.items():
        evd = FakeEvent(group="30000", user=f"u-{word}", name=f"难度{word}")
        await run_commands(diff_plugin.start_cs, evd, word)
        game = diff_plugin.games[m.SessionKey.game("30000", f"u-{word}", "cs")]
        check(game.pool == pool_key, f"CS「{word}」→ {pool_key} 池")
        check(
            diff_plugin.dbs["cs"].pool_size(pool_key) == len(diff_plugin.dbs["cs"]._pools[pool_key]),
            f"「{word}」池人数与题库一致（{diff_plugin.dbs['cs'].pool_size(pool_key)} 人）",
        )
    ev_bad = FakeEvent(group="30000", user="u-bad", name="乱填")
    out = await run_commands(diff_plugin.start_cs, ev_bad, "超级难")
    check(out and "难度只有" in _flatten(out[0]), "无法识别的难度给出提示")

    strict = m.GuessProPlugin(_Context(), {"timeout_minutes": 0, "private_only": True})
    ev_group = FakeEvent(text="弗一把", group="40000", user="1", name="群里人")
    await strict.on_message(ev_group)
    check(not ev_group.results, "仅私聊：群里发开局不响应")
    check(m.SessionKey.game("40000", "1", "cs") not in strict.games, "仅私聊：群里不会创建对局")
    ev_priv = FakeEvent(text="弗一把", group="", user="1", name="私聊人")
    await strict.on_message(ev_priv)
    check(m.SessionKey.game("", "1", "cs") in strict.games, "仅私聊：私聊能正常开局")
    out = await run_commands(strict.start_football, FakeEvent(group="40000", user="9"), "")
    check(not out, "仅私聊：群里的指令处理器也不响应")

    print("[12] 足一把范围与难度")
    fb_plugin = m.GuessProPlugin(_Context(), {"timeout_minutes": 0})
    cases = [
        ("足一把", "all:easy", 600, 630),
        ("足一把 易", "all:easy", 600, 630),
        ("足一把 中", "top5:normal", 900, 950),
        ("足一把 难", "all:hard", 1100, 1200),
        ("足一把 英超", "epl:easy", 160, 175),
        ("足一把 英超 易", "epl:easy", 160, 175),
        ("足一把 五大 难", "top5:hard", 900, 950),
        ("足一把 法甲", "ligue1:easy", 25, 35),
    ]
    for i, (text, expect_pool, lo, hi) in enumerate(cases):
        ev_ = FakeEvent(text=text, group="50000", user=f"fb{i}", name="足球")
        await fb_plugin.on_message(ev_)
        gkey_ = m.SessionKey.game("50000", f"fb{i}", "football")
        game_ = fb_plugin.games.get(gkey_)
        check(game_ is not None and game_.pool == expect_pool, f"{text} → {expect_pool}")
        size_ = fb_plugin.dbs["football"].pool_size(expect_pool)
        check(lo <= size_ <= hi, f"{text} 池人数 {size_} 在合理区间")
        reply_ = _flatten(ev_.results[0]) if ev_.results else ""
        check("难度：" in reply_ and "人）" in reply_ and "（" in reply_, f"{text} 开局写明范围与人数")

    fam = fb_plugin.dbs["football"].pool("all:easy")
    names = {p["cnName"] for p in fam}
    check(600 <= len(fam) <= 630, f"默认池含豪门全员后 {len(fam)} 人")
    check(any(p["league"] == "已退役" for p in fam), "默认池包含退役巨星")
    check(any(p["league"] in {"沙特联", "美职联", "土超", "巴甲"} for p in fam), "默认池包含小联赛巨星")
    for star in ("梅西", "C罗", "姆巴佩", "哈兰德", "齐达内", "大罗", "马尔蒂尼"):
        check(star in names, f"默认池包含 {star}")
    for club in ("曼城", "皇家马德里", "拜仁慕尼黑", "巴黎圣日耳曼"):
        squad = [p["cnName"] for p in fb_plugin.dbs["football"].players if p["club"] == club]
        check(squad and all(n in names for n in squad), f"{club} 全员 {len(squad)} 人都在默认池")

    ev_bad_fb = FakeEvent(text="足一把 德乙", group="50000", user="fbx")
    await fb_plugin.on_message(ev_bad_fb)
    check("难度只有" in _flatten(ev_bad_fb.results[0]), "未知范围给出提示")

    print("\n全部自测通过 ✅")
    print("足球棋盘示例：\n" + m.render_board(fbt, 1, 10, haaland, rodri))


if __name__ == "__main__":
    asyncio.run(main())
