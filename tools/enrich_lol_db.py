"""重建撸一把题库：只留现役选手 + 知名退役，并补上「当前战队 / S 赛次数 / S 赛冠军」。

用法：
    python tools/enrich_lol_db.py --probe    # 只打印统计，不写文件
    python tools/enrich_lol_db.py            # 抓取并写入 data/lol_players.json

数据源（都是公开接口，只读）：
  - guessassin.xyz `/api/players/{CN_KR,GLOBAL}`：现役选手，带当前战队、历史战队、
    S 赛出场次数、S 赛冠军数、赛区、位置、年龄、出生地
  - `/api/feedback/search-players` + `/api/feedback/player-detail`：按 ID 查退役老将
  - 本地 `data/lol_players.json`（Liquipedia 基底）：真名、别名、中文国籍

请求间隔 0.25 秒，结果缓存在 data/.lol_guessassin_cache.json，可断点续跑。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_lol_db import COUNTRIES  # noqa: E402  国家 → (中文名, 大洲)

DB = ROOT / "data" / "lol_players.json"
SRC_DB = ROOT / "data" / "lol_players.liquipedia.json"  # Liquipedia 基底，只读
CACHE = ROOT / "data" / ".lol_guessassin_cache.json"
BASE = "https://guessassin.xyz"
UA = "AstrBotFribergGuess/1.3 (+https://github.com/weadee/astrbot_plugin_friberg_guess)"
DELAY = 0.25

POSITIONS = {
    "Top": "上单", "Jungle": "打野", "Mid": "中单", "ADC": "下路", "Bot": "下路",
    "Support": "辅助", "Coach": "教练",
}

# 知名退役：只查这些名字，查到且数据里有 S 赛履历才收录
RETIRED_STARS = [
    # 韩国
    "Bengi", "Bang", "Wolf", "PraY", "GorillA", "Smeb", "Score", "Ambition",
    "CuVee", "Mata", "PawN", "Imp", "Dandy", "Looper", "Marin", "Easyhoon",
    "Duke", "Blank", "Deft", "Khan", "Nuguri", "BeryL", "Ghost",
    # 中国
    "Uzi", "Clearlove", "Misaya", "Weixiao", "PDD", "Gogoing", "Cool",
    "Zzitai", "Namei", "Kid", "Letme", "Mlxg",
    # 欧美
    "xPeke", "Froggen", "Alex Ich", "Diamondprox", "sOAZ", "Yellowstar",
    "Jankos", "Doublelift", "Bjergsen", "Sneaky", "Aphromoo", "Zven",
    "Rekkles", "Perkz", "Trick", "Upset", "CoreJJ",
    # 台港澳 / 东南亚
    "Toyz", "Stanley", "BeBe", "Westdoor", "SwordArt", "Maple", "Karsa",
]

# 「易」档点名：中文玩家最眼熟的一批
HOT_NAMES = [
    "Faker", "Chovy", "Knight", "Ruler", "Zeus", "Oner", "Gumayusi", "Keria",
    "Canyon", "ShowMaker", "Viper", "Scout", "Meiko", "Bin", "Xun", "Elk",
    "JackeyLove", "Tian", "Rookie", "TheShy", "Doinb", "Crisp", "Xiaohu", "Wei",
    "Breathe", "Light", "Creme", "Kanavi", "369", "Missing", "Hope", "Tarzan",
    "Peanut", "Kiin", "Bdd", "Cuzz", "Lehends", "Doran", "Zeka", "Kingen",
    "Pyosik", "Caps", "Humanoid", "Hans Sama", "Elyoya", "Blaber", "Jojopyun",
    "Impact", "CoreJJ", "Uzi", "Clearlove", "Bengi", "Bang", "Wolf", "PraY",
    "Smeb", "Deft", "Mata", "PawN", "Imp", "xPeke", "Jankos", "Doublelift",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def get_json(url: str, tries: int = 3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            if attempt == tries - 1:
                log(f"  ! 请求失败 {url[:70]}：{exc}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def norm(text: str) -> str:
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def fetch_pools() -> list[dict]:
    out: dict[str, dict] = {}
    for pool in ("CN_KR", "GLOBAL"):
        data = get_json(f"{BASE}/api/players/{pool}")
        rows = (data or {}).get("data") or []
        log(f"  池 {pool}: {len(rows)} 人")
        for row in rows:
            key = norm(row.get("gameId"))
            if key and key not in out:
                out[key] = row
        time.sleep(DELAY)
    return list(out.values())


def lookup(game_id: str, cache: dict) -> dict | None:
    key = norm(game_id)
    if key in cache:
        return cache[key] or None
    found = get_json(f"{BASE}/api/feedback/search-players?pool=GLOBAL&keyword={urllib.parse.quote(game_id)}")
    time.sleep(DELAY)
    rows = (found or {}).get("data") or []
    match = next((r for r in rows if norm(r.get("gameId")) == key), None)
    if not match:
        cache[key] = None
        return None
    detail = get_json(f"{BASE}/api/feedback/player-detail/{match['id']}?pool=GLOBAL")
    time.sleep(DELAY)
    cache[key] = (detail or {}).get("data")
    return cache[key]


def fame(row: dict) -> int:
    """知名度粗排：S 赛冠军 > S 赛出场 > MSI 出场。"""
    return (
        int(row.get("worldsSChampionshipsCount") or 0) * 6
        + int(row.get("worldsSCount") or 0) * 2
        + int(row.get("msiCount") or 0)
    )


def best_base(rows: list[dict], row: dict) -> dict | None:
    """同名条目可能不止一个（如两个 Uzi），按真名挑最像的那个。"""
    if not rows:
        return None
    want = norm(row.get("realName"))
    if want:
        for cand in rows:
            got = norm(cand.get("realName"))
            if got and (got == want or got in want or want in got):
                return cand
    return rows[0]


def to_player(row: dict, base: dict | None, status: str) -> dict:
    team = (row.get("currentTeam") or "").strip()
    history = [t for t in (row.get("historicalTeams") or []) if t and t != "None"]
    country, continent = COUNTRIES.get(row.get("birthplace") or "", ("", ""))
    base = base or {}
    return {
        "gameId": row.get("gameId") or base.get("gameId"),
        "realName": row.get("realName") or base.get("realName") or "",
        "age": int(row.get("age") or base.get("age") or 0),
        "country": base.get("country") or country or "",
        "continent": base.get("continent") or continent or "",
        "region": row.get("region") or "",
        "currentTeam": "" if team in ("", "None") else team,
        "historicalTeams": history,
        "positions": base.get("positions") or [POSITIONS.get(x, x) for x in (row.get("positions") or [])],
        "status": status,
        "worldsApp": int(row.get("worldsSCount") or 0),
        "worldsTitle": int(row.get("worldsSChampionshipsCount") or 0),
        "lastPlayedYear": int(row.get("lastPlayedYear") or 0),
        "aliases": base.get("aliases") or [],
        "page": base.get("page") or "",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--min-fame", type=int, default=6, help="退役选手的 S 赛履历门槛")
    ap.add_argument("--active-year", type=int, default=2025, help="最后出场年 ≥ 这个年份算现役")
    args = ap.parse_args()

    base = json.loads((SRC_DB if SRC_DB.exists() else DB).read_text(encoding="utf-8"))
    base_players = base["players"] if isinstance(base, dict) else base
    by_id: dict[str, list[dict]] = {}
    for p in base_players:
        if p.get("gameId"):
            by_id.setdefault(norm(p["gameId"]), []).append(p)
    log(f"基底：{len(base_players)} 人（{(SRC_DB if SRC_DB.exists() else DB).name}）")

    cache = load_cache()
    log("拉取现役题库…")
    live = fetch_pools()
    log(f"  去重后 {len(live)} 人，能对上基底 {sum(1 for r in live if norm(r['gameId']) in by_id)} 人")

    log(f"查询点名退役 {len(RETIRED_STARS)} 人（缓存 {len(cache)} 条）…")
    live_ids = {norm(r["gameId"]) for r in live}
    extra_retired = []
    for i, name in enumerate(RETIRED_STARS, 1):
        row = lookup(name, cache)
        # 站点题库里没有的传奇，按 ID 补进来
        if row and norm(row.get("gameId")) not in live_ids and fame(row) >= args.min_fame:
            extra_retired.append(row)
        if i % 20 == 0:
            log(f"  {i}/{len(RETIRED_STARS)} 补录 {len(extra_retired)} 人")
            save_cache(cache)
    save_cache(cache)

    # 站点题库里混着退役选手：按最后出场年份区分，退役的只留 S 赛履历厚的
    def is_active(row: dict) -> bool:
        return int(row.get("lastPlayedYear") or 0) >= args.active_year

    live_active = [r for r in live if is_active(r)]
    live_retired = [r for r in live if not is_active(r) and fame(r) >= args.min_fame]
    retired_all = {(norm(r["gameId"])): r for r in live_retired + extra_retired}
    log(f"  现役 {len(live_active)} 人；退役保留 {len(retired_all)} 人"
        f"（站点内 {len(live_retired)} + 补录 {len(extra_retired)}）")

    players = [to_player(r, best_base(by_id.get(norm(r["gameId"])), r), "Active") for r in live_active]
    players += [
        to_player(r, best_base(by_id.get(norm(r["gameId"])), r), "Retired") for r in retired_all.values()
    ]
    # 只挂教练头衔的不算选手
    players = [p for p in players if p["positions"] and any(x != "教练" for x in p["positions"])]
    players.sort(
        key=lambda p: (
            p["status"] != "Active",
            -(p["worldsTitle"] * 6 + p["worldsApp"] * 2),
            p["gameId"].lower(),
        )
    )

    active = sum(1 for p in players if p["status"] == "Active")
    teams = sum(1 for p in players if p["currentTeam"])
    titled = sum(1 for p in players if p["worldsTitle"])
    log(f"总计 {len(players)} 人（现役 {active} / 退役 {len(players) - active}），"
        f"有战队 {teams}，有 S 赛冠军 {titled}")

    if args.probe:
        for p in players[:6]:
            log(f"  {p['gameId']:12s} {p['status']:8s} {p['currentTeam'] or '-':24s} "
                f"S{p['worldsApp']} 冠{p['worldsTitle']}")
        return

    payload = {
        "version": f"{time.strftime('%Y-%m-%d')}-guessassin",
        "source": "guessassin.xyz（战队 / S 赛数据）+ Liquipedia（真名 / 别名 / 国籍）",
        "snapshot": time.strftime("%Y-%m-%d"),
        "players": players,
    }
    DB.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"已写入 {DB.name}")


if __name__ == "__main__":
    main()
