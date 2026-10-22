"""从 wayiba.com（瓦一把）的公开数据生成插件用的选手库。

用法：
    python tools/build_valorant_db.py            # 下载并生成 data/valorant_players.json
    python tools/build_valorant_db.py --cache    # 只用本地缓存重新生成
    python tools/build_valorant_db.py --out x.json

数据源：https://wayiba.com/data/players.generated.json（站点公开静态资源）
站点自带的 popular_pool / popular_score 用来挑「热门」档，插件不自行判断谁有名。
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

SOURCE = "https://wayiba.com/data/players.generated.json"
UA = "AstrBotGuessPro/1.0 (+https://github.com/weadee/astrbot_plugin_friberg_guess)"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "valorant_players.json"
CACHE = ROOT / "data" / ".valorant_source_cache.json"

# 国家代码 → 大洲（与站点 bundle 里的 we 表一致）
CONTINENTS = {
    "ae": "亚洲", "al": "欧洲", "ar": "南美洲", "at": "欧洲", "au": "大洋洲", "az": "亚洲",
    "ba": "欧洲", "be": "欧洲", "bg": "欧洲", "bh": "亚洲", "bm": "北美洲", "bo": "南美洲",
    "br": "南美洲", "by": "欧洲", "ca": "北美洲", "ch": "欧洲", "cl": "南美洲", "cn": "亚洲",
    "co": "南美洲", "cr": "北美洲", "cz": "欧洲", "de": "欧洲", "dk": "欧洲", "do": "北美洲",
    "dz": "非洲", "ec": "南美洲", "ee": "欧洲", "eg": "非洲", "es": "欧洲", "fi": "欧洲",
    "fr": "欧洲", "gb": "欧洲", "ge": "亚洲", "gr": "欧洲", "hk": "亚洲", "hr": "欧洲",
    "hu": "欧洲", "id": "亚洲", "ie": "欧洲", "il": "亚洲", "in": "亚洲", "iq": "亚洲",
    "ir": "亚洲", "is": "欧洲", "it": "欧洲", "jo": "亚洲", "jp": "亚洲", "ke": "非洲",
    "kg": "亚洲", "kr": "亚洲", "kw": "亚洲", "kz": "亚洲", "lb": "亚洲", "lt": "欧洲",
    "lu": "欧洲", "lv": "欧洲", "ma": "非洲", "md": "欧洲", "me": "欧洲", "mk": "欧洲",
    "mn": "亚洲", "mo": "亚洲", "mx": "北美洲", "my": "亚洲", "ng": "非洲", "nl": "欧洲",
    "no": "欧洲", "nz": "大洋洲", "pe": "南美洲", "ph": "亚洲", "pk": "亚洲", "pl": "欧洲",
    "pt": "欧洲", "py": "南美洲", "qa": "亚洲", "ro": "欧洲", "rs": "欧洲", "ru": "欧洲",
    "sa": "亚洲", "se": "欧洲", "sg": "亚洲", "si": "欧洲", "sk": "欧洲", "th": "亚洲",
    "tn": "非洲", "tr": "亚洲", "tw": "亚洲", "ua": "欧洲", "us": "北美洲", "uy": "南美洲",
    "uz": "亚洲", "ve": "南美洲", "vn": "亚洲", "za": "非洲",
}

DOWN_STATUS = {"下放", "替补", "第六人"}
DOWN_FLAGS = {"下放", "替补", "第六人", "自由人"}


def fetch(use_cache: bool) -> dict:
    if use_cache and CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    req = urllib.request.Request(SOURCE, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(raw.decode("utf-8"), encoding="utf-8")
    return json.loads(raw.decode("utf-8"))


def status_of(player: dict) -> str:
    """复刻站点的身份判定：退役 → 下放或替补 → 现役。"""
    flags = player.get("status_flags") or []
    if player.get("career_status") == "退役" or "退役" in flags:
        return "退役"
    if player.get("roster_status") in DOWN_STATUS or player.get("contract_status") == "自由人":
        return "下放或替补"
    if any(flag in DOWN_FLAGS for flag in flags):
        return "下放或替补"
    if (
        player.get("roster_status") == "首发"
        or player.get("career_status") == "职业中"
        or "现役" in flags
    ):
        return "现役"
    return "未知"


def roles_of(player: dict) -> list[str]:
    raw = str(player.get("role_label") or player.get("primary_role") or "").strip()
    return [part.strip() for part in raw.split("/") if part.strip()]


def to_player(player: dict) -> dict | None:
    game_id = str(player.get("game_id") or "").strip()
    if not game_id:
        return None
    status = status_of(player)
    free_agent = player.get("contract_status") == "自由人"
    # 退役与自由人展示上一支队伍（与站点一致）
    team = (
        player.get("last_team_name")
        if (status == "退役" or free_agent)
        else player.get("current_team_name")
    ) or ""
    code = str(player.get("nationality_code") or "").lower()
    return {
        "gameId": game_id,
        "cnName": str(player.get("chinese_name") or "").strip(),
        "realName": str(player.get("real_name") or "").strip(),
        "aliases": [a for a in (player.get("aliases") or []) if a][:4],
        "nationality": str(player.get("display_nationality") or "未知"),
        "continent": CONTINENTS.get(code, ""),
        "region": str(player.get("vct_region") or "未知"),
        "team": team.strip() or "无队伍",
        "teamTier": str(player.get("display_team_level") or ""),
        "status": status,
        "freeAgent": free_agent,
        "roles": roles_of(player),
        "age": player.get("current_age"),
        "mastersApp": player.get("masters_appearances") or 0,
        "mastersTitle": player.get("masters_titles") or 0,
        "championsApp": player.get("champions_appearances") or 0,
        "championsTitle": player.get("champions_titles") or 0,
        "popular": bool(player.get("popular_pool")),
        "popularScore": player.get("popular_score") or 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", action="store_true", help="只用本地缓存，不联网")
    parser.add_argument("--out", default=str(DATA))
    args = parser.parse_args()

    raw = fetch(args.cache)
    meta = raw.get("metadata") or {}
    players = [x for x in (to_player(p) for p in raw.get("players") or []) if x]
    payload = {
        "version": f"{meta.get('snapshot_date', 'unknown')}-wayiba",
        "source": "wayiba.com/data/players.generated.json",
        "snapshot": meta.get("snapshot_date", ""),
        "players": players,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    counts = {s: sum(1 for p in players if p["status"] == s) for s in ("现役", "下放或替补", "退役")}
    print(f"共 {len(players)} 人，热门池 {sum(1 for p in players if p['popular'])} 人 → {out}")
    print("  状态分布：", counts)
    print("  无年龄：", sum(1 for p in players if not p.get("age")))


if __name__ == "__main__":
    main()
