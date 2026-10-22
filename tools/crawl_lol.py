"""抓 guessassin.xyz（撸一把）的选手库，写入 data/lol_players.json。

用法：
    python tools/crawl_lol.py            # 断点续抓（已抓过的关键词会跳过）
    python tools/crawl_lol.py --reset    # 清掉进度重头抓

说明：
  - 站点的搜索接口每个关键词最多返回 20 条，所以要靠「关键词逐层加长」把库挖出来；
  - 抓取过程每完成一层就落盘一次，随时 Ctrl+C 都不会白跑；
  - 只抓公开的选手资料，不碰账号与对局数据；请求间隔保持克制，别把人家接口打挂。
"""

from __future__ import annotations

import json
import string
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://guessassin.xyz"
POOLS = ["CN_KR", "GLOBAL"]
ALPHABET = string.ascii_lowercase + string.digits
MAX_DEPTH = 3
LIMIT = 20
WORKERS = 5
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "lol_players.json"
STATE = ROOT / "data" / ".lol_crawl_state.json"


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def get_json(url: str, tries: int = 3) -> dict:
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except Exception as exc:  # noqa: BLE001 - 网络抖动统一重试
            if attempt == tries - 1:
                log(f"  跳过（请求失败）：{exc}")
                return {}
            time.sleep(0.6 * (attempt + 1))
    return {}


def search(pool: str, keyword: str) -> list[dict]:
    url = f"{BASE}/api/feedback/search-players?pool={pool}&keyword={urllib.parse.quote(keyword)}"
    return get_json(url).get("data") or []


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"done": {pool: [] for pool in POOLS}}


def load_players() -> dict[str, dict]:
    if not OUT.exists():
        return {}
    try:
        raw = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    players = raw.get("players") if isinstance(raw, dict) else raw
    return {p["id"]: p for p in players or [] if p.get("id")}


def save(players: dict[str, dict], state: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "version": time.strftime("%Y-%m-%d"),
                "source": "guessassin.xyz",
                "players": list(players.values()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def crawl_pool(pool: str, players: dict[str, dict], state: dict) -> None:
    done = set(state["done"].get(pool) or [])
    frontier = [kw for kw in ALPHABET if kw not in done]
    depth = 1
    while frontier and depth <= MAX_DEPTH:
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            results = list(executor.map(lambda kw: (kw, search(pool, kw)), frontier))
        next_frontier: list[str] = []
        for keyword, rows in results:
            done.add(keyword)
            for row in rows:
                players.setdefault(row["id"], row).setdefault("_pools", [])
                if pool not in players[row["id"]]["_pools"]:
                    players[row["id"]]["_pools"].append(pool)
            if len(rows) >= LIMIT and len(keyword) < MAX_DEPTH:
                next_frontier.extend(keyword + ch for ch in ALPHABET if keyword + ch not in done)
        state["done"][pool] = sorted(done)
        save(players, state)
        log(f"[{pool}] 第 {depth} 层完成：本次查 {len(frontier)} 条，累计 {len(players)} 人，下一层 {len(next_frontier)}")
        frontier = next_frontier
        depth += 1


def fetch_details(players: dict[str, dict]) -> None:
    pending = [pid for pid, p in players.items() if "worldsCount" not in p]
    if not pending:
        return
    log(f"补齐详情：{len(pending)} 人")

    def worker(pid: str) -> tuple[str, dict | None]:
        pools = players[pid].get("_pools") or [POOLS[0]]
        data = get_json(f"{BASE}/api/feedback/player-detail/{pid}?pool={pools[0]}")
        return pid, (data.get("data") if data.get("success") else None)

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        for i, (pid, detail) in enumerate(executor.map(worker, pending), 1):
            if detail:
                pools = players[pid].get("_pools") or []
                players[pid].update(detail)
                players[pid]["_pools"] = pools
            if i % 100 == 0:
                log(f"  详情 {i}/{len(pending)}")


def main() -> None:
    if "--reset" in sys.argv:
        for path in (OUT, STATE):
            if path.exists():
                path.unlink()
        log("已清空旧进度")

    state = load_state()
    players = load_players()
    log(f"开始：已有 {len(players)} 人，已抓关键词 {sum(len(v) for v in state['done'].values())} 条")

    for pool in POOLS:
        crawl_pool(pool, players, state)
        fetch_details(players)
        save(players, state)

    log(f"完成：{len(players)} 人 → {OUT}")


if __name__ == "__main__":
    main()
