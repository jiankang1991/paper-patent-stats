#!/usr/bin/env python3
"""Granted patents by inventor (country, title, number, grant date, inventor rank) from Google Patents.

    python3 patents.py --inventor 张伟 --inventor "Wei Zhang" --assignee 示例大学 --assignee "Example University" --out DIR

Search hits show only the matching inventor and match substrings (张伟 hits 李张伟), so every hit is
re-read from its detail page and kept only on an exact full-name match. Responses are cached in DIR/cache.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pubstats  # noqa: E402  (shared cached GET)

COUNTRY = {"CN": "中国", "US": "美国", "EP": "欧洲专利局", "WO": "PCT国际申请", "DE": "德国", "JP": "日本",
           "KR": "韩国", "GB": "英国", "FR": "法国", "TW": "中国台湾", "HK": "中国香港", "AU": "澳大利亚", "CA": "加拿大"}


class Blocked(RuntimeError):
    pass


def fetch(url: str, *, as_json: bool):
    """Cached, throttled GET. Google answers bursts with 503 'Sorry'; stop instead of hammering.
    ScraperAPI does not help here: Google Patents returns 404 to its proxies (25 credits each)."""
    key = pubstats.CACHE / (hashlib.sha1(url.encode()).hexdigest() + (".json" if as_json else ".html"))
    if key.exists():
        text = key.read_text(encoding="utf-8")
        return json.loads(text) if as_json else text
    time.sleep(2.5)  # ponytail: fixed throttle; Google's limit is undocumented
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": pubstats.UA}), timeout=90) as r:
            text = r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code in (429, 503):
            raise Blocked("Google Patents is rate-limiting this IP (503 Sorry). Wait 30-60 min and rerun; "
                          "the cache keeps finished requests.") from e
        raise
    key.write_text(text, encoding="utf-8")
    return json.loads(text) if as_json else text


def kind_of(pub: str) -> tuple[str, bool]:
    """Patent type and whether this publication itself is a grant, from the kind code."""
    m = re.match(r"([A-Z]{2})\d+([A-Z]\d?)$", pub)
    if not m:
        return "未知", False
    cc, k = m.groups()
    if cc == "CN":
        return {"B": ("发明", True), "C": ("发明", True), "U": ("实用新型", True), "Y": ("实用新型", True),
                "S": ("外观设计", True), "A": ("发明", False)}.get(k[0], ("未知", False))
    if cc == "WO":
        return "PCT", False
    if cc == "US":
        return ("外观设计", True) if k.startswith("S") else ("发明", k.startswith("B"))
    return "发明", k[0] in "BC"


def same_person(name: str, wanted: list[str]) -> bool:
    key = lambda s: "".join(sorted(re.split(r"[\s,.\-]+", s.lower())))  # noqa: E731 - 'Zhang Wei' == 'Wei Zhang'
    return any(key(name) == key(w) for w in wanted)


def snippet_inventors(hit: dict) -> list[str]:
    """Search hits list only the inventors that matched, with <b> highlighting."""
    return [x.strip() for x in re.sub(r"<[^>]+>", "", hit.get("inventor") or "").split(",") if x.strip()]


def search(name: str, assignee: str | None) -> list[str]:
    """Publication numbers whose matched inventor is exactly `name` (张伟 also hits 李张伟)."""
    pubs, page = [], 0
    while True:
        q = f"inventor={name}" + (f"&assignee={assignee}" if assignee else "") + f"&num=100&page={page}"
        d = fetch(f"https://patents.google.com/xhr/query?url={urllib.parse.quote(q)}&exp=", as_json=True)
        res = ((d or {}).get("results") or {})
        for cl in res.get("cluster", []):
            pubs += [r["patent"]["publication_number"] for r in cl.get("result", [])
                     if any(same_person(x, [name]) for x in snippet_inventors(r["patent"]))]
        page += 1
        if page >= (res.get("total_num_pages") or 0):
            return pubs


def detail(pub: str) -> dict | None:
    t = fetch(f"https://patents.google.com/patent/{pub}/zh", as_json=False)
    if not t:
        return None
    meta = lambda n, s=None: [html.unescape(v).strip() for v in re.findall(  # noqa: E731
        rf'<meta name="{n}" content="([^"]*)"' + (rf' scheme="{s}"' if s else ""), t)]
    ev = {}
    for e in re.findall(r'<dd itemprop="events"[^>]*>(.*?)</dd>', t, re.S):
        d = re.search(r'itemprop="date"[^>]*>([^<]*)', e)
        ty = re.search(r'itemprop="type"[^>]*>([^<]*)', e)
        ti = re.search(r'itemprop="title"[^>]*>([^<]*)', e)
        if ty and d:
            ev.setdefault(ty.group(1).strip(), (d.group(1).strip(), (ti.group(1).strip() if ti else "")))
    status = re.search(r'itemprop="legalStatusIfi"[^>]*>\s*<span[^>]*>([^<]*)', t) or \
        re.search(r'itemprop="ifiStatus"[^>]*>([^<]*)', t)
    app = (meta("citation_patent_application_number") or [""])[0].replace(":", "")
    return dict(pub=pub, title=(meta("DC.title") or [""])[0], inventors=meta("DC.contributor", "inventor"),
                assignees=meta("DC.contributor", "assignee"), app=app, filed=ev.get("filed", ("", ""))[0],
                granted=ev.get("granted", ("", ""))[0], status=status.group(1).strip() if status else "")


def load_gp_csv(path: str) -> list[dict]:
    """Google Patents 'Download (CSV)' export: row 1 is the search URL, row 2 the header.
    The inventor/author column holds the full inventor list in order, so no detail pages are needed."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    hdr = next(i for i, r in enumerate(rows) if r and r[0].strip().lower() == "id")
    cols = [c.strip().lower() for c in rows[hdr]]
    out = []
    for r in rows[hdr + 1:]:
        if not r or not r[0].strip():
            continue
        d = dict(zip(cols, r))
        pub = d["id"].replace("-", "")
        out.append(dict(pub=pub, title=d.get("title", "").strip(), app="",
                        inventors=[x.strip() for x in d.get("inventor/author", "").split(",") if x.strip()],
                        assignees=[x.strip() for x in d.get("assignee", "").split(",") if x.strip()],
                        filed=d.get("filing/creation date", ""), granted=d.get("grant date", ""), status="",
                        via={f"csv:{Path(path).name}"}))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gp-csv", nargs="*", default=[], help="Google Patents CSV exports saved from a browser; skips live search")
    ap.add_argument("--inventor", action="append", required=True, help="exact name; repeat for 中文名 and English name")
    ap.add_argument("--assignee", action="append", default=[], help="repeat per employer (中文 and English names)")
    ap.add_argument("--no-assignee", action="store_true", help="also search the name alone (slow for common names)")
    ap.add_argument("--exclude", nargs="*", default=[], help="publication numbers the user says are not theirs (kind code optional)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    excl = {re.sub(r"[A-Z]\d?$", "", x.upper()) for x in a.exclude}
    out = Path(a.out)
    pubstats.CACHE = out / "cache"
    pubstats.CACHE.mkdir(parents=True, exist_ok=True)

    found: dict[str, set[str]] = {}
    pre: dict[str, dict] = {}
    for f in a.gp_csv:
        for d in load_gp_csv(f):
            pre.setdefault(d["pub"], d)["via"] |= d["via"]
            found.setdefault(d["pub"], set()).update(d["via"])
    if not a.gp_csv:
        for n in a.inventor:
            for asg in a.assignee + [None] * (a.no_assignee or not a.assignee):
                for p in search(n, asg):
                    found.setdefault(p, set()).add(f"{n}" + (f"@{asg}" if asg else ""))
    recs, dropped = [], Counter()
    for p, via in found.items():
        if re.sub(r"[A-Z]\d?$", "", p) in excl:
            dropped["用户确认非本人"] += 1
            continue
        d = pre.get(p) or detail(p)
        if not d:
            dropped["详情页取不到"] += 1
            continue
        hit = [i for i, x in enumerate(d["inventors"]) if same_person(x, a.inventor)]
        if not hit:
            dropped["发明人非同一姓名(子串误中)"] += 1
            continue
        d.update(rank=hit[0] + 1, n_inv=len(d["inventors"]), via=via)
        recs.append(d)

    # one row per application: keep the grant over the application publication
    fams: dict[str, dict] = {}
    for d in recs:
        typ, is_grant = kind_of(d["pub"])
        d.update(type=typ, is_grant=is_grant)
        k = d["app"] or (re.sub(r"[A-Z]\d?$", "", d["pub"]) if d["pub"].startswith("CN") else d["pub"])  # CN A/B share a number
        if k not in fams or (is_grant and not fams[k]["is_grant"]):
            fams[k] = d
    rows = list(fams.values())

    # same-name check: patents sharing no co-inventor with the rest are flagged for the user
    co = Counter(x for d in rows for x in d["inventors"] if not same_person(x, a.inventor))
    for d in rows:
        others = [x for x in d["inventors"] if not same_person(x, a.inventor)]
        d["linked"] = not others or any(co[x] > 1 for x in others)

    checks = []
    for d in rows:
        if d["is_grant"] and not d["granted"] and d["type"] == "发明":
            checks.append(f"{d['pub']}：公告号为授权类型，但时间线没有 granted 事件")
        if d["granted"] and not d["is_grant"]:
            checks.append(f"{d['pub']}：时间线有授权事件，但保留的是申请公开号（授权公告号可能未被检索到）")
    rows.sort(key=lambda d: (not d["is_grant"], d["granted"] or d["filed"]), reverse=False)
    table = [dict(状态="已授权" if d["is_grant"] else "未授权/审查中", 国别=COUNTRY.get(d["pub"][:2], d["pub"][:2]),
                  专利类型=d["type"], 专利名称=d["title"], 公告号=d["pub"], 申请号=d["app"], 申请日=d["filed"],
                  授权日=d["granted"] if d["is_grant"] else "", 排名=f"{d['rank']}/{d['n_inv']}",
                  全部发明人="、".join(d["inventors"]), 专利权人="、".join(d["assignees"]), 法律状态=d["status"],
                  同名核查="" if d["linked"] else "与其余专利无共同发明人，请确认是否本人", 命中检索="; ".join(sorted(d["via"])))
             for d in rows]
    pubstats.write_csv(out / "patents.csv", table)

    g = [d for d in rows if d["is_grant"]]
    L = ["# 专利授权统计（Google Patents）", "", "| 项目 | 数值 |", "|---|---|",
         f"| 授权专利 | {len(g)}（" + "，".join(f"{COUNTRY.get(c, c)} {n}" for c, n in Counter(d['pub'][:2] for d in g).most_common()) + "） |",
         f"| 其中发明 / 实用新型 / 外观 | {sum(d['type'] == '发明' for d in g)} / {sum(d['type'] == '实用新型' for d in g)} / {sum(d['type'] == '外观设计' for d in g)} |",
         f"| 第一发明人（授权） | {sum(d['rank'] == 1 for d in g)} |",
         f"| 未授权/审查中（含 PCT） | {len(rows) - len(g)} |", "", "## 核验", "",
         f"- 检索命中 {len(found)} 条公开文本，剔除：{dict(dropped)}；按申请号合并后 {len(rows)} 件。",
         "- 各检索式单独命中的件数：" + str(Counter(v for d in rows for v in d["via"])),
         f"- 同名待确认：{sum(not d['linked'] for d in rows)} 件（patents.csv「同名核查」列）。",
         f"- 公告号类型与授权事件不一致：{len(checks)} 件" + ("".join(f"\n  - {c}" for c in checks) if checks else "。"),
         "- 法律状态（有效/失效）以 CNIPA 中国及多国专利审查信息查询为准；Google 标注为推定状态。"]
    (out / "patents_summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
