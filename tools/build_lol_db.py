"""从 Liquipedia 生成撸一把（LoL）题库：data/lol_players.json

用法：
    python tools/build_lol_db.py            # 断点续抓（已解析的页面会跳过）
    python tools/build_lol_db.py --limit 300  # 只抓前 300 页，先看看效果
    python tools/build_lol_db.py --reset    # 清掉进度重头抓

数据源与合规：
  - Liquipedia 的 API 规则是「每次请求间隔 ≥2 秒、必须带可识别的 User-Agent」，脚本已遵守；
  - 只读公开的选手页 wikitext，取 Infobox player 里的字段；
  - 署名与许可见 README 的「数据」一节。
"""

from __future__ import annotations

import gzip
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://liquipedia.net/leagueoflegends/api.php"
UA = "AstrBotFribergGuess/1.2 (https://github.com/weadee/astrbot_plugin_friberg_guess)"
DELAY = 2.1  # Liquipedia 要求 ≥2 秒
BATCH = 50

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "lol_players.json"
CACHE = ROOT / "data" / ".lol_liquipedia_cache.json"

# 国家 → (中文名, 大洲)。只列 LoL 常见地区，未收录的保留英文名、大洲留空。
COUNTRIES: dict[str, tuple[str, str]] = {
    "South Korea": ("韩国", "亚洲"),
    "Korea": ("韩国", "亚洲"),
    "China": ("中国", "亚洲"),
    "Taiwan": ("中国台湾", "亚洲"),
    "Hong Kong": ("中国香港", "亚洲"),
    "Macau": ("中国澳门", "亚洲"),
    "Macao": ("中国澳门", "亚洲"),
    "Japan": ("日本", "亚洲"),
    "Vietnam": ("越南", "亚洲"),
    "Thailand": ("泰国", "亚洲"),
    "Singapore": ("新加坡", "亚洲"),
    "Malaysia": ("马来西亚", "亚洲"),
    "Philippines": ("菲律宾", "亚洲"),
    "Indonesia": ("印度尼西亚", "亚洲"),
    "India": ("印度", "亚洲"),
    "Mongolia": ("蒙古", "亚洲"),
    "Kazakhstan": ("哈萨克斯坦", "亚洲"),
    "United States": ("美国", "北美洲"),
    "USA": ("美国", "北美洲"),
    "Canada": ("加拿大", "北美洲"),
    "Mexico": ("墨西哥", "北美洲"),
    "Brazil": ("巴西", "南美洲"),
    "Argentina": ("阿根廷", "南美洲"),
    "Chile": ("智利", "南美洲"),
    "Peru": ("秘鲁", "南美洲"),
    "Colombia": ("哥伦比亚", "南美洲"),
    "Uruguay": ("乌拉圭", "南美洲"),
    "Venezuela": ("委内瑞拉", "南美洲"),
    "Bolivia": ("玻利维亚", "南美洲"),
    "Ecuador": ("厄瓜多尔", "南美洲"),
    "Paraguay": ("巴拉圭", "南美洲"),
    "Germany": ("德国", "欧洲"),
    "France": ("法国", "欧洲"),
    "Spain": ("西班牙", "欧洲"),
    "Portugal": ("葡萄牙", "欧洲"),
    "Italy": ("意大利", "欧洲"),
    "United Kingdom": ("英国", "欧洲"),
    "England": ("英国", "欧洲"),
    "Scotland": ("英国", "欧洲"),
    "Wales": ("英国", "欧洲"),
    "Ireland": ("爱尔兰", "欧洲"),
    "Netherlands": ("荷兰", "欧洲"),
    "Belgium": ("比利时", "欧洲"),
    "Denmark": ("丹麦", "欧洲"),
    "Sweden": ("瑞典", "欧洲"),
    "Norway": ("挪威", "欧洲"),
    "Finland": ("芬兰", "欧洲"),
    "Iceland": ("冰岛", "欧洲"),
    "Poland": ("波兰", "欧洲"),
    "Czech Republic": ("捷克", "欧洲"),
    "Slovakia": ("斯洛伐克", "欧洲"),
    "Hungary": ("匈牙利", "欧洲"),
    "Romania": ("罗马尼亚", "欧洲"),
    "Bulgaria": ("保加利亚", "欧洲"),
    "Serbia": ("塞尔维亚", "欧洲"),
    "Croatia": ("克罗地亚", "欧洲"),
    "Slovenia": ("斯洛文尼亚", "欧洲"),
    "Bosnia and Herzegovina": ("波黑", "欧洲"),
    "North Macedonia": ("北马其顿", "欧洲"),
    "Greece": ("希腊", "欧洲"),
    "Turkey": ("土耳其", "欧洲"),
    "Russia": ("俄罗斯", "欧洲"),
    "Ukraine": ("乌克兰", "欧洲"),
    "Belarus": ("白俄罗斯", "欧洲"),
    "Lithuania": ("立陶宛", "欧洲"),
    "Latvia": ("拉脱维亚", "欧洲"),
    "Estonia": ("爱沙尼亚", "欧洲"),
    "Austria": ("奥地利", "欧洲"),
    "Switzerland": ("瑞士", "欧洲"),
    "Israel": ("以色列", "亚洲"),
    "Australia": ("澳大利亚", "大洋洲"),
    "New Zealand": ("新西兰", "大洋洲"),
    "South Africa": ("南非", "非洲"),
    "Egypt": ("埃及", "非洲"),
    "Morocco": ("摩洛哥", "非洲"),
    "Tunisia": ("突尼斯", "非洲"),
    "Algeria": ("阿尔及利亚", "非洲"),
    "Nigeria": ("尼日利亚", "非洲"),
}

ROLE_CN = {
    "top": "上单",
    "jungle": "打野",
    "mid": "中单",
    "bot": "下路",
    "adc": "下路",
    "support": "辅助",
}

INFOBOX_RE = re.compile(r"\{\{\s*Infobox player", re.I)
PARAM_RE = re.compile(r"\|\s*([a-z_0-9]+)[ \t]*=[ \t]*([^\n]*)", re.I)


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def api_get(params: dict) -> dict:
    query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
    req = urllib.request.Request(
        f"{API}?{query}",
        headers={"User-Agent": UA, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
    time.sleep(DELAY)  # 遵守 Liquipedia 的 1 次/2 秒限制
    return data


def clean(value: str) -> str:
    """去掉 wikitext 里的链接、模板、注释，取出纯文本。"""
    value = re.sub(r"<ref[^>]*>.*?</ref>", "", value, flags=re.S)
    value = re.sub(r"<ref[^>]*/>", "", value)
    value = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    value = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", value)
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    return value.strip().strip("|").strip()


def parse_infobox(text: str) -> dict[str, str]:
    """从页面 wikitext 里取 Infobox player 的字段。"""
    matched = INFOBOX_RE.search(text)
    if not matched:
        return {}
    start = matched.start()
    depth = 0
    end = len(text)
    for i in range(start, len(text) - 1):
        if text[i : i + 2] == "{{":
            depth += 1
        elif text[i : i + 2] == "}}":
            depth -= 1
            if depth == 0:
                end = i + 2
                break
    block = text[start:end]
    fields: dict[str, str] = {}
    for key, value in PARAM_RE.findall(block):
        if key not in fields:
            fields[key.lower()] = clean(value)
    return fields


def age_from(birth: str) -> int | None:
    matched = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", birth or "")
    if not matched:
        return None
    year, month, day = (int(x) for x in matched.groups())
    today = time.localtime()
    age = today.tm_year - year - ((today.tm_mon, today.tm_mday) < (month, day))
    return age if 10 <= age <= 60 else None


def to_player(title: str, fields: dict[str, str]) -> dict | None:
    game_id = fields.get("id") or title
    role_text = fields.get("roles") or fields.get("role") or ""
    roles = [ROLE_CN[r.strip().lower()] for r in role_text.split(",") if r.strip().lower() in ROLE_CN]
    if not roles:  # 只收选手，跳过教练 / 解说等
        return None
    roles = list(dict.fromkeys(roles))
    status_raw = (fields.get("status") or "").strip().lower()
    retired_raw = (fields.get("retired") or "").strip().lower()
    status = "Retired" if ("retire" in status_raw or retired_raw) else "Active"
    country_raw = fields.get("country") or ""
    country, continent = COUNTRIES.get(country_raw, (country_raw, ""))
    team = fields.get("team") or ""
    if team.lower() in {"free agent", "retired", ""}:
        team = "" if team.lower() != "free agent" else "自由人"

    aliases = [
        x.strip()
        for x in re.split(r"[,、]", fields.get("ids") or "")
        if x.strip() and x.strip().lower() != game_id.lower()
    ]
    return {
        "gameId": game_id,
        "realName": fields.get("romanized_name") or fields.get("name") or "",
        "age": age_from(fields.get("birth_date", "")),
        "currentTeam": team,
        "country": country,
        "countryRaw": country_raw,
        "continent": continent,
        "positions": roles,
        "status": status,
        "aliases": aliases[:6],
        "page": title,
    }


def list_players(limit: int | None) -> list[str]:
    titles: list[str] = []
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:Players",
        "cmlimit": "500",
        "cmnamespace": "0",
    }
    while True:
        data = api_get(params)
        titles.extend(item["title"] for item in data.get("query", {}).get("categorymembers", []))
        cont = data.get("continue")
        log(f"  分类已取 {len(titles)} 页")
        if not cont or (limit and len(titles) >= limit):
            break
        params.update(cont)
    return titles[:limit] if limit else titles


def fetch_wikitext(titles: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(titles), BATCH):
        chunk = titles[i : i + BATCH]
        data = api_get(
            {
                "action": "query",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "titles": "|".join(chunk),
            }
        )
        for page in data.get("query", {}).get("pages", []):
            revs = page.get("revisions") or []
            if not revs:
                continue
            out[page["title"]] = revs[0].get("slots", {}).get("main", {}).get("content", "")
        log(f"  正文 {min(i + BATCH, len(titles))}/{len(titles)}")
    return out


def main() -> None:
    argv = sys.argv[1:]
    limit = None
    for i, arg in enumerate(argv):
        if arg == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
    if "--reset" in argv:
        for path in (OUT, CACHE):
            if path.exists():
                path.unlink()
        log("已清空旧数据")

    cache: dict[str, dict] = {}
    if CACHE.exists():
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        log(f"续抓：已有 {len(cache)} 人")

    titles = list_players(limit)
    log(f"分类共 {len(titles)} 页，开始取正文")
    pending = [t for t in titles if t not in cache]
    texts = fetch_wikitext(pending) if pending else {}

    for title, text in texts.items():
        fields = parse_infobox(text)
        player = to_player(title, fields) if fields else None
        if player and player.get("age"):
            cache[title] = player

    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    players = sorted(cache.values(), key=lambda p: (p["status"] != "Active", p["gameId"].lower()))
    OUT.write_text(
        json.dumps(
            {
                "version": time.strftime("%Y-%m-%d"),
                "source": "liquipedia.net/leagueoflegends",
                "players": players,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    active = sum(1 for p in players if p["status"] == "Active")
    log(f"完成：{len(players)} 人（现役 {active}）→ {OUT.name}")


if __name__ == "__main__":
    main()
