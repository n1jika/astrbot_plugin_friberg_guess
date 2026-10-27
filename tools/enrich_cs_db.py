"""用 Liquipedia（Counter-Strike 2 维基）刷新弗一把题库的一线信息。

只更新「会随时间变」的字段：现队、在役状态、角色、年龄、国籍、历史战队。
Major 冠军 / 参赛数是站点用动态模板算出来的，抓不到，保留原值。

用法：
    python tools/enrich_cs_db.py               # 抓 + 写 data/players.json
    python tools/enrich_cs_db.py --probe       # 只看统计，不写文件
    python tools/enrich_cs_db.py --limit 60    # 只处理前 N 人（试跑）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "players.json"
CACHE = ROOT / "data" / ".cs_liquipedia_cache.json"

API = "https://liquipedia.net/counterstrike/api.php"
UA = "FribergGuessBot/1.0 (AstrBot plugin; contact via GitHub issues)"
BATCH = 40           # 一次请求多少个页面
PAUSE = 2.2          # Liquipedia 要求 api.php 至少 2 秒一个请求

# ---------------------------------------------------------------- 国家 / 大区

COUNTRY_CN = {
    "Russia": "俄罗斯", "Ukraine": "乌克兰", "Belarus": "白俄罗斯", "Kazakhstan": "哈萨克斯坦",
    "Kyrgyzstan": "吉尔吉斯斯坦", "Uzbekistan": "乌兹别克斯坦", "Moldova": "摩尔多瓦",
    "Georgia": "格鲁吉亚", "Armenia": "亚美尼亚", "Azerbaijan": "阿塞拜疆",
    "Estonia": "爱沙尼亚", "Latvia": "拉脱维亚", "Lithuania": "立陶宛",
    "Poland": "波兰", "Czech Republic": "捷克", "Czechia": "捷克", "Slovakia": "斯洛伐克",
    "Hungary": "匈牙利", "Romania": "罗马尼亚", "Bulgaria": "保加利亚",
    "Serbia": "塞尔维亚", "Croatia": "克罗地亚", "Slovenia": "斯洛文尼亚",
    "Bosnia and Herzegovina": "波黑", "North Macedonia": "北马其顿", "Montenegro": "黑山",
    "Kosovo": "科索沃", "Albania": "阿尔巴尼亚", "Greece": "希腊", "Turkey": "土耳其",
    "Germany": "德国", "Austria": "奥地利", "Switzerland": "瑞士", "France": "法国",
    "Netherlands": "荷兰", "Belgium": "比利时", "Luxembourg": "卢森堡",
    "Denmark": "丹麦", "Sweden": "瑞典", "Norway": "挪威", "Finland": "芬兰", "Iceland": "冰岛",
    "United Kingdom": "英国", "England": "英国", "Scotland": "英国", "Wales": "英国",
    "Ireland": "爱尔兰", "Spain": "西班牙", "Portugal": "葡萄牙", "Italy": "意大利",
    "Malta": "马耳他", "Cyprus": "塞浦路斯", "Israel": "以色列",
    "United States": "美国", "Canada": "加拿大", "Mexico": "墨西哥",
    "Brazil": "巴西", "Argentina": "阿根廷", "Chile": "智利", "Uruguay": "乌拉圭",
    "Colombia": "哥伦比亚", "Peru": "秘鲁", "Venezuela": "委内瑞拉", "Ecuador": "厄瓜多尔",
    "Bolivia": "玻利维亚", "Paraguay": "巴拉圭", "Costa Rica": "哥斯达黎加",
    "Guatemala": "危地马拉", "Panama": "巴拿马", "Dominican Republic": "多米尼加",
    "Puerto Rico": "波多黎各", "Cuba": "古巴", "Honduras": "洪都拉斯", "Jamaica": "牙买加",
    "China": "中国", "Hong Kong": "中国香港", "Taiwan": "中国台湾", "Macau": "中国澳门",
    "South Korea": "韩国", "Japan": "日本", "Mongolia": "蒙古",
    "Australia": "澳大利亚", "New Zealand": "新西兰",
    "Malaysia": "马来西亚", "Indonesia": "印度尼西亚", "Philippines": "菲律宾",
    "Singapore": "新加坡", "Thailand": "泰国", "Vietnam": "越南", "Cambodia": "柬埔寨",
    "Myanmar": "缅甸", "India": "印度", "Pakistan": "巴基斯坦", "Bangladesh": "孟加拉国",
    "Sri Lanka": "斯里兰卡", "Nepal": "尼泊尔", "Afghanistan": "阿富汗",
    "Saudi Arabia": "沙特阿拉伯", "United Arab Emirates": "阿联酋", "Qatar": "卡塔尔",
    "Kuwait": "科威特", "Bahrain": "巴林", "Oman": "阿曼", "Jordan": "约旦",
    "Lebanon": "黎巴嫩", "Iraq": "伊拉克", "Iran": "伊朗", "Syria": "叙利亚",
    "Egypt": "埃及", "Morocco": "摩洛哥", "Algeria": "阿尔及利亚", "Tunisia": "突尼斯",
    "South Africa": "南非", "Nigeria": "尼日利亚", "Kenya": "肯尼亚", "Ghana": "加纳",
    "Zimbabwe": "津巴布韦", "Uganda": "乌干达", "Ethiopia": "埃塞俄比亚",
}

# 兜底：国籍查不到大区时按这几组猜
REGION_FALLBACK = {
    "中国": "亚太", "韩国": "亚太", "日本": "亚太", "蒙古": "亚太",
    "中国香港": "亚太", "中国台湾": "亚太", "中国澳门": "亚太",
    "俄罗斯": "独联体", "乌克兰": "独联体", "白俄罗斯": "独联体", "哈萨克斯坦": "独联体",
    "美国": "北美洲", "加拿大": "北美洲", "墨西哥": "北美洲",
    "巴西": "南美洲", "阿根廷": "南美洲", "智利": "南美洲",
    "澳大利亚": "大洋洲", "新西兰": "大洋洲",
    "以色列": "非洲与以色列", "南非": "非洲与以色列",
}

DISAMBIG = re.compile(r"[（(]([^）)]+)[）)]\s*$")  # 原库给同名选手加的「adren（哈萨克斯坦）」后缀


def strip_wiki(text: str) -> str:
    """去掉 [[链接|显示]]、'''粗体''' 之类的标记。"""
    text = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", text or "")
    return text.replace("'''", "").replace("''", "").strip()


def split_nick(nickname: str) -> tuple[str, str]:
    """拆出「主体名」和原库标注的消歧义国籍。"""
    m = DISAMBIG.search(nickname)
    if not m:
        return nickname, ""
    return nickname[: m.start()].strip(), m.group(1).strip()


# ---------------------------------------------------------------- 抓取

def _get(params: dict, tries: int = 3) -> dict:
    url = f"{API}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept-Encoding": "gzip",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3 + i * 4)
    raise RuntimeError(f"请求失败：{url[:120]} -> {last}")


def fetch_raw(titles: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """一次拿一批页面的原始 wikitext。

    返回 (按规范化后的标题索引的内容, 请求名 → 实际标题)。
    必须走 normalized / redirects 映射：NiKo 和 niko 是两个人，
    按小写匹配会让它们互相覆盖。
    """
    if not titles:
        return {}, {}
    data = _get({
        "action": "query", "format": "json", "prop": "revisions",
        "rvprop": "content", "rvslots": "main",
        "titles": "|".join(titles), "redirects": "1",
    })
    q = data.get("query") or {}
    normalized = {n["from"]: n["to"] for n in (q.get("normalized") or [])}
    redirected = {r["from"]: r["to"] for r in (q.get("redirects") or [])}
    pages: dict[str, str] = {}
    for page in q.get("pages", {}).values():
        if "missing" in page:
            continue
        rev = (page.get("revisions") or [{}])[0]
        text = (rev.get("slots", {}).get("main", {}) or {}).get("*", "")
        pages[page.get("title", "")] = text or ""
    alias: dict[str, str] = {}
    for t in titles:
        real = normalized.get(t, t)
        real = redirected.get(real, real)
        alias[t] = real
    return pages, alias


def fetch_titles(titles: list[str]) -> dict[str, str]:
    """按「请求时的名字」取内容。"""
    pages, alias = fetch_raw(titles)
    return {t: pages.get(alias.get(t, t), "") for t in titles}


FOR_RE = re.compile(r"\s*\{\{for\|[^|]*\|([^}|]+)\}\}")  # 「{{for|the Israeli player|xertioN}}」

# 消歧义页通常指向「名字 (某某国籍 player)」，搜索不一定排得出来，直接把这些后缀也当候选
NAME_SUFFIXES = (
    "player", "American player", "Danish player", "Bosnian player", "British player",
    "Israeli player", "Canadian player", "German player", "Brazilian player",
    "Swedish player", "Russian player", "Ukrainian player", "French player",
    "Polish player", "Australian player", "Norwegian player",
)


def fetch_titles(titles: list[str]) -> dict[str, str]:
    """按请求时的名字取内容（标题被维基规范化也能对上）。"""
    pages, alias = fetch_raw(titles)
    return {t: pages.get(alias.get(t, t), "") for t in titles}


def search_candidates(name: str) -> list[str]:
    """搜出可能的页面标题（大小写差异、消歧义后缀都靠它兜底）。"""
    data = _get({
        "action": "query", "format": "json", "list": "search",
        "srsearch": name, "srnamespace": "0", "srlimit": "6",
    })
    out: list[str] = []
    for hit in (data.get("query") or {}).get("search", []):
        title = hit.get("title", "")
        if title and title not in out:
            out.append(title)
    return out


LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"})


def _flat(text: str) -> str:
    return (text or "").strip().lower().translate(LEET).replace(" ", "")


def looks_wrong(text: str) -> bool:
    """页面是消歧义页 / 只有 {{for}} 的短桩，说明抓的不是本人。"""
    if not text:
        return True
    head = text[:800]
    if "disambiguation" in head:
        return True
    return bool(FOR_RE.search(head)) and len(text) < 3000


def choose_page(name: str, texts: dict[str, str], old_team: str, today: date) -> str:
    """几个候选页面里挑最像同一个人的：ID 对得上加分，履历里有老东家再加分。"""
    best, best_score = "", 0
    for title, text in texts.items():
        if not text:
            continue
        info = parse_player(text, today)
        if not info:
            continue
        score = 0
        if info["id"] and _flat(info["id"]) == _flat(name):
            score += 2
        joined = " ".join(info["history"]).lower()
        team_now = (info["team"] or "").lower()
        if old_team and old_team not in ("退役", "未签约/已下放"):
            key = old_team.lower()[:6]
            if key and (key in joined or (team_now and key in team_now)):
                score += 3
        if score > best_score:
            best, best_score = title, score
    return best


# ---------------------------------------------------------------- 解析

INFOBOX_RE = re.compile(r"\{\{\s*Infobox\s+player", re.I)  # 页面里也写作 {{Infobox Player}}


def _infobox(text: str) -> dict[str, str]:
    """取出 {{Infobox player ...}} 里的一层字段（忽略嵌套模板里的 |）。"""
    m = INFOBOX_RE.search(text or "")
    if not m:
        return {}
    start = m.start()
    depth, i = 0, start
    while i < len(text):
        if text.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i):
            depth -= 1
            i += 2
            if depth == 0:
                end = i
                break
        else:
            i += 1
    else:
        end = len(text)
    body = text[start:end]
    fields: dict[str, str] = {}
    for line in body.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        key, sep, val = s[1:].partition("=")
        if not sep:
            continue
        fields[key.strip().lower()] = val.strip()
    return fields


def _team_history(text: str) -> list[str]:
    """从 team_history 里的 {{TH|日期|队名}} 抽队伍，按时间顺序去重。"""
    start = text.find("|team_history=")
    if start < 0:
        return []
    end = text.find("\n}}", start)
    seg = text[start:end if end > 0 else start + 4000]
    teams: list[str] = []
    for _date, name in re.findall(r"\{\{TH\|([^|]*)\|([^}|]+)\}\}", seg):
        name = strip_wiki(name)
        if not name or name.startswith("Counter-Strike") or "Present" in name:
            continue
        if name not in teams:
            teams.append(name)
    return teams


def _roles_to_role(roles: str) -> str | None:
    """按 Liquipedia 信息框里的顺序取第一个认得的角色。

    以前是「只要出现 awp 就算 AWPer」，结果 latto / dumau 这种同时标了
    Rifler+AWPer 的人全被判成狙击手，一支队冒出三个 AWPer。改成看顺序：
    页面把主角色写在前面，只有 awp 单独出现才算纯 AWPer。
    """
    parts = [x.strip().lower() for x in roles.split(",") if x.strip()]
    for part in parts:
        if "coach" in part:
            return "Coach"
        if "awp" in part:
            return "AWPer"
        if "rifl" in part:
            return "Rifler"
    return None


def _age_of(birth: str, today: date) -> int | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", birth or "")
    if not m:
        m2 = re.match(r"(\d{4})-(\d{2})", birth or "")
        if not m2:
            return None
        y, mo, d = int(m2.group(1)), int(m2.group(2)), 1
    else:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    age = today.year - y - ((today.month, today.day) < (mo, d))
    return age if 14 <= age <= 60 else None


def parse_player(text: str, today: date) -> dict | None:
    box = _infobox(text)
    if not box:
        return None
    status = strip_wiki(box.get("status", ""))
    team = strip_wiki(box.get("team", ""))
    country = strip_wiki(box.get("country", ""))
    return {
        "id": box.get("id", "").strip(),
        "ids": [x.strip() for x in (box.get("ids") or "").split(",") if x.strip()],
        "name": (box.get("romanized_name") or box.get("name") or "").strip(),
        "country": country,
        "birth": box.get("birth_date", "").strip(),
        "status": status,
        "roles": box.get("roles", "").strip(),
        "team": team,
        "history": _team_history(text),
        "age": _age_of(box.get("birth_date", ""), today),
    }


# ---------------------------------------------------------------- 主流程

def load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_cache(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="只看统计，不写文件")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 人")
    ap.add_argument("--fresh", action="store_true", help="忽略缓存重新抓")
    args = ap.parse_args()

    today = date.today()
    db = json.loads(DB.read_text(encoding="utf-8"))
    players = db["players"]
    if args.limit:
        players = players[: args.limit]
    # 同名条目带「（国籍）」后缀，查 Liquipedia 时用主体名
    subjects = {p["nickname"]: split_nick(p["nickname"])[0] for p in players}
    unique = sorted({s for s in subjects.values() if s})

    cache = {} if args.fresh else load_cache()
    todo = [s for s in unique if not cache.get(s)]
    log(f"共 {len(players)} 人，需抓取 {len(todo)} 个页面（缓存已有 {len(cache)} 条）")

    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        cache.update(fetch_titles(chunk))
        save_cache(cache)
        log(f"  {min(i + BATCH, len(todo))}/{len(todo)} 已抓")
        if i + BATCH < len(todo):
            time.sleep(PAUSE)

    # 没抓到、或抓到的是消歧义页（Niko / Grim / xertion 这类）→ 搜索候选 + 校验
    old_teams = {subjects[p["nickname"]]: p.get("team", "") for p in players}
    hard = [s for s in unique if looks_wrong(cache.get(s))]
    if hard:
        log(f"\n需要二次确认的页面 {len(hard)} 个（消歧义页 / 没抓到）…")
        fixed = 0
        for i, name in enumerate(hard, 1):
            cands = search_candidates(name)
            flat = _flat(name)
            # 大小写/leet 能对上的（m0nesy → M0NESY、device → Dev1ce）直接抓它，
            # 别把队伍页、赛事页一起拉下来——响应太大反而会被丢掉
            exact = [c for c in cands if c != name and _flat(c) == flat]
            texts: dict[str, str] = {}
            if exact:
                texts.update(fetch_titles(exact[:1]))
            if not any(texts.values()):
                more = [c for c in cands[:3] if c.lower() != name.lower()]
                more += [f"{name} ({s})" for s in NAME_SUFFIXES[:8]]
                for j in range(0, len(more), 6):
                    texts.update(fetch_titles(list(dict.fromkeys(more))[j : j + 6]))
            texts[name] = cache.get(name) or ""  # 原页面也参与比较
            best = choose_page(name, texts, old_teams.get(name, ""), today)
            if best and texts.get(best) and best != name:
                cache[name] = texts[best]
                fixed += 1
            if i % 20 == 0:
                log(f"  {i}/{len(hard)} 已修正 {fixed}")
                save_cache(cache)
            time.sleep(PAUSE)
        save_cache(cache)
        log(f"  修正/补抓 {fixed} 人")

    # 从原库学「中文国籍 → 大区」，新增国籍才有依据
    region_by_country: dict[str, str] = {}
    tally: dict[str, dict[str, int]] = {}
    for p in db["players"]:
        if p.get("nationality") and p.get("region"):
            tally.setdefault(p["nationality"], {})
            tally[p["nationality"]][p["region"]] = tally[p["nationality"]].get(p["region"], 0) + 1
    for country, votes in tally.items():
        region_by_country[country] = max(votes, key=votes.get)

    hit = miss = 0
    changed_team: list[tuple[str, str, str]] = []
    changed_status: list[tuple[str, bool, bool]] = []
    changed_role: list[tuple[str, str, str]] = []
    changed_country: list[tuple[str, str, str]] = []

    for p in players:
        nick, want_country = split_nick(p["nickname"])
        text = cache.get(nick) or ""
        info = parse_player(text, today) if text else None
        if not info:
            miss += 1
            continue
        # 抓错人就直接跳过：页面 ID 对不上，或同名条目的国籍和原库标注不符
        if info["id"] and info["id"].replace(" ", "").lower() != nick.replace(" ", "").lower():
            miss += 1
            continue
        got_country = COUNTRY_CN.get(info["country"], "")
        if want_country and got_country and got_country != want_country:
            miss += 1
            continue
        # 年龄差太离谱也是抓错人了（同名同 ID 的老将），宁可不更新
        if info["age"] and abs(info["age"] - int(p.get("age") or 0)) >= 4:
            miss += 1
            continue
        # 老东家都不在履历里 → 多半抓成了同名选手，跳过
        prev_team = p.get("team", "")
        if prev_team and prev_team not in ("退役", "未签约/已下放"):
            key = prev_team.lower()[:6]
            joined = " ".join(info["history"]).lower()
            if key and key not in joined and key not in (info["team"] or "").lower():
                miss += 1
                continue
        hit += 1

        status = (info["status"] or "").lower()
        retired = status.startswith("retire")
        old_active = bool(p.get("is_active"))
        old_team = p.get("team", "")

        # 现队只认 Infobox 的 team 字段；历史战队只用来填 team_history，
        # 否则退役老将（Infobox 没写 team）会被当成复出
        team = info["team"]
        if team.lower().startswith("ex-"):
            team = ""  # Liquipedia 用「ex-队名」表示已经离队

        # 口径保守：只有「明确退役」或「查不到现队且本来就是退役」才算退役；
        # Active 却没写队伍的老将（主播 / 半退役）保持原库的退役状态，不硬改成复出
        if retired or (not team and not old_active):
            new_team, new_active = "退役", False
        elif team and len(team) >= 3:
            # 只差大小写（BC.Game / bc.game）不算转会，保持原库的写法
            new_team, new_active = (old_team if team.lower() == old_team.lower() else team), True
        else:
            new_team, new_active = "未签约/已下放", old_active

        if new_team != old_team:
            changed_team.append((p["nickname"], old_team, new_team))
        p["team"] = new_team
        if new_active != old_active:
            changed_status.append((p["nickname"], old_active, new_active))
        p["is_active"] = new_active

        role = _roles_to_role(info["roles"])
        if role and role != p.get("role"):
            changed_role.append((p["nickname"], p.get("role", ""), role))
            p["role"] = role

        # 国籍原库基本是准的，只在缺失时补；大区跟着国籍走
        if not p.get("nationality") and got_country:
            changed_country.append((p["nickname"], "", got_country))
            p["nationality"] = got_country
        cn = p.get("nationality") or got_country
        if cn and cn not in region_by_country:
            region_by_country[cn] = REGION_FALLBACK.get(cn, "欧洲")
        if cn:
            p["region"] = region_by_country.get(cn, p.get("region", ""))

        if info["age"]:
            p["age"] = info["age"]
        if info["history"]:
            p["team_history"] = info["history"][-8:]  # 只留最近几段，别把文件撑大

    log(f"\n命中 {hit} / 未命中 {miss}")
    log(f"战队变动 {len(changed_team)} 人")
    for nick, old, new in changed_team[:40]:
        log(f"   {nick:16s} {old or '—':24s} → {new}")
    log(f"状态变动 {len(changed_status)} 人")
    for nick, old, new in changed_status[:30]:
        log(f"   {nick:16s} {'现役' if old else '退役'} → {'现役' if new else '退役'}")
    log(f"角色变动 {len(changed_role)} 人")
    for nick, old, new in changed_role[:20]:
        log(f"   {nick:16s} {old} → {new}")
    log(f"国籍修正 {len(changed_country)} 人")
    for nick, old, new in changed_country[:20]:
        log(f"   {nick:16s} {old} → {new}")

    if not args.probe:
        db["version"] = f"{today.isoformat()}-liquipedia"
        db["players"] = players if not args.limit else db["players"]
        DB.write_text(json.dumps(db, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        log(f"\n已写入 {DB}（{len(db['players'])} 人，version={db['version']}）")


if __name__ == "__main__":
    sys.exit(main())
