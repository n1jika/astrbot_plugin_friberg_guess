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
        check(len(lol_db.players) >= 350, f"LoL 题库 {len(lol_db.players)} 人")
        act = [p for p in lol_db.players if p["status"] == "Active"]
        ret = [p for p in lol_db.players if p["status"] == "Retired"]
        check(len(act) + len(ret) == len(lol_db.players), f"LoL 只有现役与退役（现役 {len(act)} / 退役 {len(ret)}）")
        check(ret and all(p["_worlds_title"] or p["_worlds_app"] for p in ret), "LoL 退役只留 S 赛履历厚的")
        check(all("_team" in p and "_worlds_app" in p for p in lol_db.players), "LoL 每条都带战队与 S 赛数据")
        check(sum(1 for p in lol_db.players if p["_team"]) >= 150, "LoL 战队覆盖够用")
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
    # 年龄随年份变，别写死：只要 s1mple 比 ZywOo 大，方向就是 ↓
    check(
        smpl["age"] > zywoo["age"] and cells["年龄"][0] == f"{smpl['age']}↓",
        f"CS：年龄 s1mple {smpl['age']} vs ZywOo {zywoo['age']} → 更小 {cells['年龄']}",
    )
    check(cells["冠军"] == ("1↑", "wrong"), "CS：冠军差 2 → 红 + 更大")

    # 战队：同队绿、效力过对方现队黄（历史战队来自 Liquipedia，转会后才判得出来）
    raw_cs = {
        "players": [
            {"nickname": "A", "nationality": "法国", "region": "欧洲", "team": "Team Vitality",
             "team_history": ["Team Liquid"], "age": 25, "role": "Rifler",
             "major_championships": 1, "major_appearances": 5, "is_active": True},
            {"nickname": "B", "nationality": "法国", "region": "欧洲", "team": "FaZe Clan",
             "team_history": ["Team Vitality", "G2 Esports"], "age": 25, "role": "AWPer",
             "major_championships": 1, "major_appearances": 5, "is_active": True},
        ]
    }
    ca, cb = m.THEMES["cs"].load(raw_cs)
    tc = {c.label: (c.value, c.level) for c in m.THEMES["cs"].judge(ca, cb)}
    check(tc["战队"] == ("Team Vitality", "close"), f"CS：效力过对方现队 → 黄（{tc['战队']}）")
    check(tc["国家"][1] == "correct" and tc["位置"][1] == "wrong", "CS：同国家绿 / 不同位置红")
    check(sum(1 for p in cs.players if p.get("team_history")) >= 500,
          f"CS 题库带历史战队（{sum(1 for p in cs.players if p.get('team_history'))} 人）")

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
            {"gameId": "A", "realName": "a", "age": 30, "currentTeam": "T1", "region": "LCK",
             "historicalTeams": ["Gen.G"], "country": "韩国", "continent": "亚洲",
             "positions": ["中单"], "status": "Active", "worldsApp": 6, "worldsTitle": 3},
            {"gameId": "B", "realName": "b", "age": 25, "currentTeam": "Gen.G", "region": "LCK",
             "historicalTeams": ["T1"], "country": "中国", "continent": "亚洲",
             "positions": ["上单"], "status": "Retired", "worldsApp": 3, "worldsTitle": 0},
            {"gameId": "C", "realName": "c", "age": 27, "currentTeam": "Team Liquid", "region": "LCS",
             "historicalTeams": [], "country": "美国", "continent": "北美洲",
             "positions": ["下路"], "status": "Active", "worldsApp": 5, "worldsTitle": 1},
        ]
    }
    ga, gb, gc = lol_theme.load(raw)
    cells = {c.label: (c.value, c.level) for c in lol_theme.judge(ga, gb)}
    check(len(cells) == 8, f"LoL 棋盘 8 项（{sorted(cells)}）")
    check(cells["位置"][1] == "wrong", "LoL：位置不同 → 红")
    check(cells["国家"][1] == "close", "LoL：不同国家同大洲 → 黄")
    check(cells["大洲"][1] == "correct", "LoL：同大洲 → 绿")
    check(cells["战队"][1] == "close" and cells["战队"][0] == "T1", "LoL：效力过对方现队 → 黄")
    check(cells["年龄"][1] == "wrong" and cells["年龄"][0].endswith("↓"), "LoL：年龄差 5 → 红 + 更小")
    check(cells["S赛参赛"][1] == "wrong" and cells["S赛参赛"][0].endswith("↓"), "LoL：S 赛次数差 3 → 红 + 更小")
    check(cells["S赛冠军"][1] == "wrong", "LoL：S 赛冠军必须一致")
    check(cells["状态"][1] == "wrong", "LoL：现役 vs 退役 → 红")
    cross = {c.label: (c.value, c.level) for c in lol_theme.judge(ga, gc)}
    check(cross["战队"][1] == "wrong", "LoL：不同赛区不同队 → 红")
    check(cross["国家"][1] == "wrong", "LoL：不同大洲 → 红")
    same = {c.label: (c.value, c.level) for c in lol_theme.judge(ga, ga)}
    check(all(level == "correct" for _v, level in same.values()), "LoL：自己猜自己全绿")

    print("[5] 棋盘渲染")
    board = m.render_board(fbt, 1, 10, haaland, rodri)
    lines = board.splitlines()
    cells = fbt.judge(haaland, rodri)
    body = lines[1:]
    counts = [ln.count("🟩") + ln.count("🟨") + ln.count("🟥") for ln in body]
    check(all(c <= 2 for c in counts), f"每行最多两项（{counts}）")
    check(len(body) >= (len(cells) + 1) // 2, f"确实两项一行地排（{len(cells)} 项排了 {len(body)} 行）")
    check(sum(counts) == len(cells), "一项都没漏")
    check(max(m._disp_width(ln) for ln in lines) <= m.BOARD_LINE_BUDGET, "每行都不超过行宽预算（不会折行）")
    check("🟨国籍" in board and "🟥联赛" in board, "带颜色与属性名")
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
        ("足一把", "all:easy", 298, 302),
        ("足一把 易", "all:easy", 298, 302),
        ("足一把 中", "top5:normal", 900, 950),
        ("足一把 难", "all:hard", 1100, 1200),
        ("足一把 英超", "epl:easy", 145, 155),
        ("足一把 英超 易", "epl:easy", 145, 155),
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
        check("难度：" in reply_ and "人" in reply_, f"{text} 开局写明范围与人数")

    fam = fb_plugin.dbs["football"].pool("all:easy")
    names = {p["cnName"] for p in fam}
    check(len(fam) == 300, f"默认池正好 300 人（{len(fam)}）")
    check(any(p["league"] == "已退役" for p in fam), "默认池包含退役巨星")
    check(any(p["league"] in {"沙特联", "美职联", "土超", "巴甲"} for p in fam), "默认池包含小联赛巨星")
    for star in ("梅西", "C罗", "姆巴佩", "哈兰德", "齐达内", "大罗", "马尔蒂尼"):
        check(star in names, f"默认池包含 {star}")

    # 不再整队照收：豪门只有部分球员进池，但主力基本都在
    all_players = fb_plugin.dbs["football"].players
    for club, star in (("曼城", "哈兰德"), ("皇家马德里", "姆巴佩"), ("拜仁慕尼黑", "凯恩")):
        squad = [p["cnName"] for p in all_players if p["club"] == club]
        inside = [n for n in squad if n in names]
        check(0 < len(inside) < len(squad), f"{club} 只部分入池（{len(inside)}/{len(squad)}）")
        check(star in names, f"{club} 的 {star} 仍在默认池")
    check(not any(p["cnName"] == "" for p in fam), "默认池无空名")

    ev_bad_fb = FakeEvent(text="足一把 德乙", group="50000", user="fbx")
    await fb_plugin.on_message(ev_bad_fb)
    check("难度只有" in _flatten(ev_bad_fb.results[0]), "未知范围给出提示")

    print("[13] 弗一把默认池 = 最火 200 人")
    cs_db = fb_plugin.dbs["cs"]
    cs_theme = m.THEMES["cs"]
    hot = cs_db.pool("easy")
    check(len(hot) == 200, f"CS 易档正好 200 人（{len(hot)}）")
    check(cs_theme.pool_label("easy") == "易（最火 200 人）", "CS 易档标签写明最火 200 人")
    check(all(p["role"] != "Coach" for p in hot), "默认池不含教练")
    names_cs = {p["_base"].lower() for p in hot}
    for star in ("s1mple", "zywoo", "niko", "device", "donk", "m0nesy", "coldzera", "fallen"):
        check(star in names_cs, f"默认池包含 {star}")
    check(any(p["major_championships"] >= 3 for p in hot), "默认池有多届 Major 冠军")
    check(any(not p["is_active"] for p in hot), "默认池包含退役传奇")
    check(cs_db.pool_size("normal") == 384 and cs_db.pool_size("hard") == 646, "中/难两档人数未变")

    # 名人名单不能有拼错的孤立项（命中不到任何选手说明写错了）
    bases = {p["_base"].lower() for p in cs_db.players}
    dead = sorted(n for n in m.CS_FAMOUS if n not in bases)
    check(not dead, f"CS_FAMOUS 无死项（误写：{dead[:5]}）")

    print("[14] 默认难度必须落到真实池（回归：曾经默认值被当成池名）")
    for key_, theme_ in m.THEMES.items():
        db_ = fb_plugin.dbs[key_]
        for word in ("易", "中", "难"):
            pool_key = theme_.resolve_pool(word, "易")
            check(pool_key in db_._pools, f"{key_}「{word}」得到合法池名 {pool_key}")
        default_pool = theme_.resolve_pool("", "易")
        check(default_pool in db_._pools, f"{key_} 默认池 {default_pool} 存在")
        check(
            db_.pool_size(default_pool) < len(db_.players) or default_pool == "hard",
            f"{key_} 默认池不是全库（{db_.pool_size(default_pool)}/{len(db_.players)}）",
        )

    cs_start = await run_commands(fb_plugin.start_cs, FakeEvent(group="60000", user="cs-default"), "")
    cs_text = _flatten(cs_start[0])
    check("最火 200 人" in cs_text, f"弗一把开局写明 200 人：{cs_text.splitlines()[1]}")

    print("[15] 答案是 200 人之一，但题库仍是 646 人（可猜任何人）")
    cs_all = fb_plugin.dbs["cs"]
    check(len(cs_all.players) == 646, f"预选题库仍是 646 人（{len(cs_all.players)}）")
    hot_names = {p["nickname"] for p in cs_all.pool("easy")}

    # 抽 30 局，答案必须全部落在 200 人池内
    targets = set()
    for i in range(30):
        uid = f"pool-{i}"
        await run_commands(fb_plugin.start_cs, FakeEvent(group="70000", user=uid), "")
        game_ = fb_plugin.games.get(m.SessionKey.game("70000", uid, "cs"))
        if game_:
            targets.add(game_.target)
    check(targets and targets <= hot_names, f"30 局答案都来自 200 人池（取了 {len(targets)} 个不同答案）")

    # 池外选手照样能猜，并给出棋盘
    outside = sorted(p["nickname"] for p in cs_all.players if p["nickname"] not in hot_names)
    check(len(outside) == 446, f"池外仍有 446 人可猜（{len(outside)}）")
    ev_out = FakeEvent(text=outside[0], group="70000", user="outside", name="池外人")
    await run_commands(fb_plugin.start_cs, FakeEvent(group="70000", user="outside"), "")
    await fb_plugin.on_message(ev_out)
    check(ev_out.results and "第 1/" in _flatten(ev_out.results[0]), f"池外选手 {outside[0]} 能正常猜")

    print("[16] 足一把 / 撸一把 同样是「答案在池内、题库全可猜」")
    fb2 = m.GuessProPlugin(_Context(), {"timeout_minutes": 0})
    fb2.dbs["lol"] = m.PlayerDB(m.THEMES["lol"], Path(__file__).resolve().parent.parent / "data" / "lol_players.json")

    # 足一把：默认答案池 300，题库 1141
    fdb = fb2.dbs["football"]
    check(len(fdb.players) == 1141, f"足球题库仍是 1141 人（{len(fdb.players)}）")
    fb_targets = set()
    for i in range(20):
        uid = f"fb-pool-{i}"
        await run_commands(fb2.start_football, FakeEvent(group="71000", user=uid), "")
        g_ = fb2.games.get(m.SessionKey.game("71000", uid, "football"))
        if g_:
            fb_targets.add(g_.target)
    fb_pool = {fdb.theme.title_of(p) for p in fdb.answer_pool("all:easy")}
    check(len(fb_pool) == 300 and fb_targets <= fb_pool, f"足球 20 局答案都来自 300 人池（{len(fb_pool)}）")

    fb_out = [p for p in fdb.players if fdb.theme.title_of(p) not in fb_pool]
    check(len(fb_out) == 841, f"足球池外仍有 841 人可猜（{len(fb_out)}）")
    ev_fb_out = FakeEvent(text=fb_out[0]["name"], group="71000", user="fb-out", name="池外")
    await run_commands(fb2.start_football, FakeEvent(group="71000", user="fb-out"), "")
    await fb2.on_message(ev_fb_out)
    check(ev_fb_out.results and "第 1/" in _flatten(ev_fb_out.results[0]), f"足球池外 {fb_out[0]['cnName']} 能正常猜")

    # 开局文案里的人数必须是「真正能出题」的数量
    for key_, db_ in (("cs", fb2.dbs["cs"]), ("football", fdb)):
        check(db_.pool_size("easy") == len(db_.answer_pool("easy")), f"{key_} 显示人数=可出题数")
    # 撸一把：题库 2427，同名选手不作为答案
    ldb = fb2.dbs["lol"]
    if ldb.players:
        check(ldb.pool_size("easy") == len(ldb.answer_pool("easy")), "LoL 显示人数=可出题数")
        check(len(ldb.players) == 386, f"LoL 题库 386 人（{len(ldb.players)}）")
        check(ldb.pool_size("easy") == 200, f"LoL 易档 200 人（{ldb.pool_size('easy')}）")
        check(ldb.pool_size("normal") == 304, f"LoL 中档=现役 304 人（{ldb.pool_size('normal')}）")
        check(ldb.pool_size("hard") == 386, f"LoL 难档=全库（{ldb.pool_size('hard')}）")
        lol_pool = {ldb.theme.title_of(p) for p in ldb.answer_pool("easy")}
        check(not (lol_pool & ldb._ambiguous), "同名选手不会成为答案")
        lol_targets = set()
        for i in range(20):
            uid = f"lol-pool-{i}"
            await run_commands(fb2.start_lol, FakeEvent(group="71000", user=uid), "")
            g_ = fb2.games.get(m.SessionKey.game("71000", uid, "lol"))
            if g_:
                lol_targets.add(g_.target)
        check(lol_targets <= lol_pool, f"LoL 20 局答案都来自可出题池（{len(lol_pool)}）")
        lol_out = [p for p in ldb.players if ldb.theme.title_of(p) not in lol_pool]
        ev_lol_out = FakeEvent(text=lol_out[0]["gameId"], group="71000", user="lol-out", name="池外")
        await run_commands(fb2.start_lol, FakeEvent(group="71000", user="lol-out"), "")
        await fb2.on_message(ev_lol_out)
        check(ev_lol_out.results and "第 1/" in _flatten(ev_lol_out.results[0]), "LoL 池外选手能正常猜")

    print("[17] 瓦一把（wayiba）")
    v_theme = m.THEMES["valorant"]
    vdb = m.PlayerDB(v_theme, ROOT / "data" / "valorant_players.json")
    check(len(vdb.players) == 543, f"瓦一把题库 543 人（{len(vdb.players)}）")
    check(v_theme.pool_label("easy").startswith("易（热门 150 人"), "瓦一把易档写明热门 150 人")
    hot_v = vdb.pool("easy")
    check(len(hot_v) == 150, f"瓦一把易档正好 150 人（{len(hot_v)}）")
    check(vdb.pool_size("normal") == 314 and vdb.pool_size("hard") == 541, "瓦一把中/难两档人数")

    statuses = [m._valorant_status(p) for p in hot_v]
    check(statuses.count("退役") >= 15, f"易档含退役 {statuses.count('退役')} 人")
    check(statuses.count("自由人") >= 5, f"易档含自由人 {statuses.count('自由人')} 人")
    hot_names_v = {p["gameId"] for p in hot_v}
    for star in ("TenZ", "ZmjjKK", "Boaster", "Derke", "aspas"):
        check(star in hot_names_v, f"易档包含 {star}")

    check([p["gameId"] for p in vdb.find("郑永康")] == ["ZmjjKK"], "中文名能查到 ZmjjKK")
    check([p["gameId"] for p in vdb.find("KK")] == ["ZmjjKK"], "别名 KK 能查到 ZmjjKK")

    # 判定规则逐条核对
    raw_v = {
        "players": [
            {"gameId": "A", "nationality": "中国", "continent": "亚洲", "region": "VCT CN",
             "team": "EDward Gaming", "teamTier": "一线", "status": "现役", "roles": ["决斗"],
             "age": 22, "mastersApp": 4, "mastersTitle": 1, "championsApp": 2, "championsTitle": 1},
            {"gameId": "B", "nationality": "韩国", "continent": "亚洲", "region": "VCT CN",
             "team": "Trace Esports", "teamTier": "一线", "status": "现役", "roles": ["决斗", "先锋"],
             "age": 25, "mastersApp": 6, "mastersTitle": 0, "championsApp": 3, "championsTitle": 0},
            {"gameId": "C", "nationality": "巴西", "continent": "南美洲", "region": "VCT Americas",
             "team": "LOUD", "teamTier": "非一线", "status": "退役", "roles": ["控场"],
             "age": 30, "mastersApp": 1, "mastersTitle": 0, "championsApp": 1, "championsTitle": 0},
        ]
    }
    va, vb, vc = v_theme.load(raw_v)
    cells = {c.label: (c.value, c.level) for c in v_theme.judge(va, vb)}
    check(cells["国籍"][1] == "close", "同大洲不同国籍 → 黄")
    check(cells["赛区"][1] == "correct", "同赛区 → 绿")
    check(cells["战队"][1] == "close", "不同队但同赛区 → 黄")
    check(cells["身份"][1] == "correct", "现役 + 一线队都相同 → 绿")
    check(cells["职责"][1] == "close", "职责有交集 → 黄")
    check(cells["年龄"][1] == "wrong" and cells["年龄"][0].endswith("↑"), "年龄不同 → 红 + 更大")
    check(cells["大师赛参赛"][1] == "close", "大师赛参赛差 2 → 黄")
    check(cells["冠军赛参赛"][1] == "close", "冠军赛参赛差 1 → 黄")
    check(cells["大师赛冠军"][1] == "wrong", "冠军数不同 → 红")
    cross = {c.label: (c.value, c.level) for c in v_theme.judge(va, vc)}
    check(cross["国籍"][1] == "wrong", "不同大洲 → 红")
    check(cross["赛区"][1] == "wrong", "不同赛区 → 红")
    check(cross["战队"][1] == "wrong", "不同赛区不同队 → 红")
    check(cross["身份"][1] == "wrong", "现役 vs 退役 → 红")
    check(all(c.level == "correct" for c in v_theme.judge(va, va)), "自己猜自己全绿")

    # 整局：答案来自 150 人池，池外选手照样能猜
    vb = m.render_board(v_theme, 1, 8, vdb.get("ZmjjKK"), vdb.get("TenZ"))
    vb_body = vb.splitlines()[1:]
    check(len(vb_body) <= 10 and len(vb_body) >= 5, f"瓦一把 10 项排了 {len(vb_body)} 行（两项一行）")
    check(all(m._disp_width(ln) <= m.BOARD_LINE_BUDGET for ln in vb.splitlines()), "瓦一把棋盘每行都不折行")
    v_plugin = m.GuessProPlugin(_Context(), {"timeout_minutes": 0})
    v_targets = set()
    for i in range(15):
        uid = f"v-{i}"
        await run_commands(v_plugin.start_valorant, FakeEvent(group="72000", user=uid), "")
        g_ = v_plugin.games.get(m.SessionKey.game("72000", uid, "valorant"))
        if g_:
            v_targets.add(g_.target)
    check(v_targets <= hot_names_v, f"瓦一把 15 局答案都来自 150 人池（{len(v_targets)} 个不同答案）")

    v_out = [p for p in vdb.players if p["gameId"] not in hot_names_v]
    check(len(v_out) == 393, f"瓦一把池外仍有 393 人可猜（{len(v_out)}）")
    ev_v_out = FakeEvent(text=v_out[0]["gameId"], group="72000", user="v-out", name="池外")
    await run_commands(v_plugin.start_valorant, FakeEvent(group="72000", user="v-out"), "")
    await v_plugin.on_message(ev_v_out)
    check(ev_v_out.results and "第 1/8" in _flatten(ev_v_out.results[0]), "瓦一把池外选手能正常猜")

    ev_v_direct = FakeEvent(text="瓦一把 难", group="72001", user="v2")
    await v_plugin.on_message(ev_v_direct)
    g_v = v_plugin.games.get(m.SessionKey.game("72001", "v2", "valorant"))
    check(g_v is not None and g_v.pool == "hard", "瓦一把 难 切到全库")

    # 未知题库文件缺失时不影响其它题库
    check("valorant" in m.THEMES and m.THEMES["valorant"].command == "瓦一把", "主题已注册")

    print("\n全部自测通过 ✅")
    print("足球棋盘示例：\n" + m.render_board(fbt, 1, 10, haaland, rodri))


if __name__ == "__main__":
    asyncio.run(main())
