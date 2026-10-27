#!/usr/bin/env python3
"""Publication statistics for Chinese form filling, with cross-checked numbers.

Sources: ORCID + Google Scholar profile (lists), OpenAlex + Crossref + OpenCitations
(metadata, citation graphs). Stdlib only. Every HTTP response is cached under OUT/cache,
so reruns are cheap and offline-repeatable.

    python3 pubstats.py --orcid 0000-0000-0000-0000 --scholar-user XXXX --name "Given Family" --out DIR
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import difflib
import hashlib
import html
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
CACHE: Path | None = None
MAILTO = ""


# ---------- helpers ----------

def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]", "", (s or "").lower())


def same_title(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a[:60], b[:60]).ratio() > 0.85


def get(url: str, *, as_json: bool = True, headers: dict | None = None):
    """GET with retry and on-disk cache. Returns None on persistent failure."""
    key = CACHE / (hashlib.sha1(url.encode()).hexdigest() + (".json" if as_json else ".html")) if CACHE else None
    if key and key.exists():
        text = key.read_text(encoding="utf-8")
        return json.loads(text) if as_json else text
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode("utf-8", errors="ignore")
            if key:
                key.write_text(text, encoding="utf-8")
            return json.loads(text) if as_json else text
        except Exception as e:  # noqa: BLE001 - network flakiness, retried
            if getattr(e, "code", None) in (401, 403, 404):
                return None
            time.sleep(2 + 3 * i)
    return None


def name_matches(name: str, given: str, family: str) -> bool:
    """'Wei Zhang', 'Zhang, Wei', 'W. Zhang', 'Wei Ming Zhang' all match given=Wei family=Zhang."""
    toks = [t for t in re.split(r"[\s,.\-]+", (name or "").lower()) if t]
    if family.lower() not in toks:
        return False
    rest = [t for t in toks if t != family.lower()]
    g = given.lower()
    return any(t == g or t.replace("-", "") == g or (len(t) == 1 and t == g[0]) for t in rest)


# ---------- stage 1: collect lists ----------

def fetch_orcid(orcid: str) -> list[dict]:
    d = get(f"https://pub.orcid.org/v3.0/{orcid}/works")
    out = []
    for g in (d or {}).get("group", []):
        w = g["work-summary"][0]
        doi = [e["external-id-value"] for e in g["external-ids"]["external-id"] if e["external-id-type"] == "doi"]
        pd = w.get("publication-date") or {}
        out.append(dict(title=w["title"]["title"]["value"], venue=(w.get("journal-title") or {}).get("value", ""),
                        year=((pd.get("year") or {}).get("value") or ""), orcid_type=w["type"],
                        doi=doi[0].lower() if doi else "", sources={"orcid"}, gs_cites=None))
    return out


def parse_scholar(page: str) -> tuple[list[dict], list[int]]:
    rows = []
    for tr in re.findall(r'<tr class="gsc_a_tr">(.*?)</tr>', page, re.S):
        m = re.search(r'class="gsc_a_at"[^>]*>(.*?)</', tr, re.S)
        if not m:
            continue
        grays = [html.unescape(re.sub("<.*?>", "", g)) for g in re.findall(r'<div class="gs_gray">(.*?)</div>', tr, re.S)]
        c = re.search(r'gsc_a_ac gs_ibl">(\d*)<', tr)
        y = re.search(r'gsc_a_hc gs_ibl">(\d*)<', tr)
        cid = re.search(r"cites=([\d,]+)", tr)
        rows.append(dict(title=html.unescape(re.sub("<.*?>", "", m.group(1))), authors=grays[0] if grays else "",
                         venue=grays[1] if len(grays) > 1 else "", year=y.group(1) if y else "",
                         gs_cites=int(c.group(1)) if c and c.group(1) else 0, gs_cites_id=cid.group(1) if cid else ""))
    totals = [int(x) for x in re.findall(r'gsc_rsb_std">(\d+)<', page)]
    return rows, totals


def fetch_scholar(user: str, html_files: list[str]) -> tuple[list[dict], list[int]]:
    pages = [Path(f).read_text(encoding="utf-8", errors="ignore") for f in html_files]
    if not pages:
        for start in range(0, 1000, 100):
            p = get(f"https://scholar.google.com/citations?user={user}&hl=en&cstart={start}&pagesize=100", as_json=False)
            if not p or "gsc_a_tr" not in p:
                if start == 0:
                    sys.exit("Scholar profile blocked or empty: save the profile page(s) from a browser "
                             "(click 'Show more' until done) and pass them with --scholar-html")
                break
            pages.append(p)
            if p.count('class="gsc_a_tr"') < 100:
                break
            time.sleep(3)
    rows, totals, seen = [], [], set()
    for p in pages:
        r, t = parse_scholar(p)
        totals = totals or t
        for x in r:
            k = (norm(x["title"]), x["year"])
            if k not in seen:
                seen.add(k)
                rows.append(x)
    return rows, totals


# ---------- stage 2: merge, drop, dedupe ----------

DROP_VENUE = re.compile(r"arxiv|preprint|biorxiv|ssrn|research ?square", re.I)
THESIS = re.compile(r"universit(y|ät|at)|dissertation|thesis|institut", re.I)
PAGES = re.compile(r"\b(?!(?:19|20)\d\d\s*-\s*(?:19|20)\d\d\b)(\d{2,6})\s*-\s*(\d{2,6})\b")  # not '2020-2020'


def merge(orcid_items: list[dict], gs_rows: list[dict]) -> tuple[list[dict], list[str]]:
    items, log = list(orcid_items), []
    for r in gs_rows:
        hit = next((it for it in items if same_title(it["title"], r["title"])), None)
        if hit is None:  # Scholar short-title entry ('FooNet') that is a prefix of a full title, same year
            n = norm(r["title"])
            hit = next((it for it in items if len(n) >= 5 and norm(it["title"]).startswith(n)
                        and it["year"] == r["year"]), None)
            if hit:
                log.append(f"合并短标题: 「{r['title']}」→「{hit['title']}」")
        if hit:
            hit["sources"].add("scholar")
            hit["gs_cites"] = max(hit["gs_cites"] or 0, r["gs_cites"])
            hit.setdefault("gs_cites_id", r["gs_cites_id"])
            hit["venue"] = hit["venue"] or r["venue"]
            hit["gs_venue"] = r["venue"]
            continue
        items.append(dict(title=r["title"], venue=r["venue"], year=r["year"], orcid_type="", doi="",
                          sources={"scholar"}, gs_cites=r["gs_cites"], gs_cites_id=r["gs_cites_id"]))
    kept = []
    for it in items:
        if DROP_VENUE.search(it["venue"]):
            log.append(f"剔除预印本: 「{it['title']}」({it['venue']})")
            continue
        if THESIS.search(it["venue"]) and not re.search(r"journal|proceedings|conference|transactions", it["venue"], re.I):
            log.append(f"剔除学位论文: 「{it['title']}」({it['venue']})")
            continue
        # same year + same page range in the same venue = one paper listed twice under two titles
        v = it.get("gs_venue") or it["venue"]
        pg = PAGES.search(v)
        twin = next((k for k in kept if pg and k["year"] == it["year"]
                     and (kp := PAGES.search(k.get("gs_venue") or k["venue"])) and kp.groups() == pg.groups()), None)
        if twin:
            log.append(f"合并同页码重复: 「{it['title']}」→「{twin['title']}」")
            twin["sources"] |= it["sources"]
            twin["gs_cites"] = max(twin["gs_cites"] or 0, it["gs_cites"] or 0)
            continue
        kept.append(it)
    return kept, log


# ---------- stage 3: resolve metadata ----------

def resolve(items: list[dict], given: str, family: str) -> None:
    m = f"mailto={MAILTO}" if MAILTO else ""
    for it in items:
        w = get(f"https://api.openalex.org/works/doi:{it['doi']}?{m}") if it["doi"] else None
        if not w:
            r = get(f"https://api.openalex.org/works?search={urllib.parse.quote(it['title'][:200])}&per_page=5&{m}")
            w = next((c for c in (r or {}).get("results", []) if same_title(c.get("title") or "", it["title"])
                      and any(name_matches(a.get("raw_author_name") or a["author"]["display_name"], given, family)
                              for a in c["authorships"])), None)
        if w:
            src = (w.get("primary_location") or {}).get("source") or {}
            it.update(oa=w["id"], oa_cites=w["cited_by_count"], oa_type=w["type"], oa_source_type=src.get("type", ""),
                      oa_venue=src.get("display_name") or "", issn=src.get("issn") or [],
                      authors=[dict(id=a["author"]["id"], name=a.get("raw_author_name") or a["author"]["display_name"],
                                    orcid=a["author"].get("orcid")) for a in w["authorships"]])
            if not it["doi"] and w.get("doi"):
                it["doi"] = w["doi"].replace("https://doi.org/", "").lower()
            it["sources"].add("openalex")
        if it["doi"]:
            cr = (get(f"https://api.crossref.org/works/{urllib.parse.quote(it['doi'])}?{m}") or {}).get("message")
            if cr:
                it["cr_cites"] = cr.get("is-referenced-by-count")
                it["cr_type"] = cr.get("type", "")
                it["cr_authors"] = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in cr.get("author", [])]
                it["doi_ok"] = same_title((cr.get("title") or [""])[0], it["title"]) and any(
                    name_matches(a, given, family) for a in it["cr_authors"])
                it["issn"] = list({*it.get("issn", []), *cr.get("ISSN", [])})


def my_ids(items: list[dict], given: str, family: str, orcid: str) -> dict[str, int]:
    ids: dict[str, int] = {}
    for it in items:
        for a in it.get("authors", []):
            if name_matches(a["name"], given, family) or (a["orcid"] or "").endswith(orcid):
                ids[a["id"]] = ids.get(a["id"], 0) + 1
    return ids


# ---------- stage 4: three citation routes ----------

def plausible(it: dict) -> bool:
    """A DOI that others cite by mistake inflates OpenAlex, Crossref and OpenCitations alike
    (hundreds everywhere vs a dozen on Scholar), so Scholar is the reference whenever the paper is on Scholar."""
    ref = it["gs_cites"] if it.get("gs_cites") is not None else (it.get("cr_cites") or 0)
    return bool(it.get("oa_cites")) and it["oa_cites"] <= 2 * ref + 20


def route_openalex(it: dict, me: set[str]) -> list[dict]:
    wid, m = it["oa"].rsplit("/", 1)[1], f"&mailto={MAILTO}" if MAILTO else ""
    co, out, page = {a["id"] for a in it["authors"]} | me, [], 1
    while page:  # cursor paging needs an API key; page paging works keyless up to 10k
        r = get(f"https://api.openalex.org/works?filter=cites:{wid}&per_page=200&page={page}&select=id,doi,authorships{m}")
        res = (r or {}).get("results", [])
        for w in res:
            ids = {a["author"]["id"] for a in w["authorships"]}
            out.append(dict(doi=(w.get("doi") or "").replace("https://doi.org/", "").lower(),
                            self_me=bool(ids & me), self_any=bool(ids & co)))
        page = page + 1 if len(res) == 200 else None
    return out


def route_opencitations(it: dict, given: str, family: str) -> list[dict]:
    r = get(f"https://api.opencitations.net/index/v2/citations/doi:{it['doi']}") or []
    cited_names = it.get("cr_authors") or [a["name"] for a in it.get("authors", [])]
    rows = []
    for c in r:
        doi = next((t[4:] for t in c["citing"].split() if t.startswith("doi:")), "")
        rows.append(dict(doi=doi.lower(), oc_sc=c.get("author_sc") == "yes"))
    m = f"?mailto={MAILTO}" if MAILTO else ""

    def names(doi: str) -> list[str]:
        if not doi:
            return []
        msg = (get(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}{m}") or {}).get("message") or {}
        return [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in msg.get("author", [])]

    with cf.ThreadPoolExecutor(6) as ex:
        for row, ns in zip(rows, ex.map(names, [x["doi"] for x in rows])):
            row["self_me"] = any(name_matches(n, given, family) for n in ns)
            # ponytail: co-author match by full-name string; initials-only bylines slip through
            row["self_any"] = row["self_me"] or bool({norm(n) for n in ns} & {norm(n) for n in cited_names if n})
    return rows


# ---------- stage 5: classify ----------

CJK = re.compile(r"[一-鿿]")
CONF = re.compile(r"conference|symposium|proceedings|workshop|congress|igarss|eusar|fringe|\bconf\b", re.I)


def load_rules(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["pattern"].strip() and not r["pattern"].startswith("#")]


def load_issns(path: str | None) -> set[str]:
    if not path:
        return set()
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    return {m.upper() for m in re.findall(r"\b\d{4}-\d{3}[\dXx]\b", text)}


def classify(it: dict, rules: list[dict], sci: set[str], ei: set[str]) -> dict:
    # a repository host (e.g. "elib (German Aerospace Center)") is not the venue; use the list venue
    venue = it["venue"] if it.get("oa_source_type") == "repository" else (it.get("oa_venue") or it["venue"])
    t = " ".join([it.get("oa_type") or "", it["orcid_type"], it.get("cr_type") or ""])
    # explicit work types first: OpenAlex files IET proceedings under a 'journal' source
    if "chapter" in t or re.search(r"\.ch\d+$", it["doi"]):  # Wiley chapters are typed "other" everywhere
        kind = "书章节"
    elif "conference" in t or "proceedings" in t or CONF.search(venue + " " + it["venue"]):
        kind = "会议"
    elif "journal" in t or it.get("oa_source_type") == "journal" or it.get("oa_type") == "article":
        kind = "期刊"
    else:
        kind = "期刊" if CJK.search(it["title"]) else "待定"
    rule = next((r for r in rules if re.search(r["pattern"], venue + " | " + it["venue"], re.I)), None)
    dom = (rule or {}).get("domestic") or ("国内" if CJK.search(it["title"] + venue) else "国外")
    sci_rule = (rule or {}).get("sci") or ("否" if kind != "期刊" else "待核")
    ei_rule = (rule or {}).get("ei") or ("否" if kind == "书章节" else "待核")
    issn = {x.upper() for x in it.get("issn", [])}
    sci_list = ("是" if issn & sci else "否") if sci and kind == "期刊" else ""
    ei_list = ("是" if issn & ei else "否") if ei and issn else ""
    return dict(类型=kind, 国内外=dom, SCI_规则=sci_rule, SCI_名单=sci_list, EI_规则=ei_rule, EI_名单=ei_list,
                规则=(rule or {}).get("pattern", ""))


def final(rule: str, listed: str) -> str:
    return listed or rule


# ---------- main ----------

def main() -> None:
    global CACHE, MAILTO
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orcid", required=True)
    ap.add_argument("--scholar-user", required=True)
    ap.add_argument("--name", required=True, help='"Given Family" in pinyin, e.g. "Wei Zhang"')
    ap.add_argument("--out", required=True)
    ap.add_argument("--scholar-html", nargs="*", default=[], help="saved Scholar profile pages if live fetch is blocked")
    ap.add_argument("--sci-list", help="SCIE journal list exported from Clarivate MJL (any text with ISSNs)")
    ap.add_argument("--ei-list", help="Ei Compendex source list (any text with ISSNs)")
    ap.add_argument("--mailto", default="", help="contact email for OpenAlex/Crossref polite pools")
    ap.add_argument("--exclude-from-year", type=int, help="also report counts without papers from this year on")
    a = ap.parse_args()
    out = Path(a.out)
    CACHE = out / "cache"
    CACHE.mkdir(parents=True, exist_ok=True)
    MAILTO = a.mailto
    given, family = a.name.split()[0], a.name.split()[-1]

    orc = fetch_orcid(a.orcid)
    gs, totals = fetch_scholar(a.scholar_user, a.scholar_html)
    items, log = merge(orc, gs)
    resolve(items, given, family)
    ids = my_ids(items, given, family, a.orcid)
    me = set(ids)

    own = {it["doi"] for it in items if it["doi"]}
    ra, rb, per_paper = [], [], []
    for it in items:
        row = dict(题目=it["title"][:80], Scholar=it.get("gs_cites"), OpenAlex=it.get("oa_cites"),
                   Crossref=it.get("cr_cites"), OpenCitations=None, 备注="")
        if it.get("oa") and plausible(it):
            ra += [dict(c, cited=it["title"]) for c in route_openalex(it, me)]
        elif it.get("oa_cites"):
            row["备注"] = "OpenAlex 计数异常，未计入路线A"
        if it["doi"] and plausible(dict(it, oa_cites=it.get("oa_cites") or 1)):
            b = route_opencitations(it, given, family)
            row["OpenCitations"] = len(b)
            rb += [dict(c, cited=it["title"]) for c in b]
        per_paper.append(row)

    def ratio(rows, k):
        return (sum(r[k] for r in rows) / len(rows)) if rows else 0.0

    gs_total = totals[0] if totals else sum(it.get("gs_cites") or 0 for it in items)
    routes = {
        "A OpenAlex 作者ID": (len(ra), ratio(ra, "self_me"), ratio(ra, "self_any")),
        "B OpenCitations+Crossref 姓名": (len(rb), ratio(rb, "self_me"), ratio(rb, "self_any")),
        "C OpenCitations author_sc(ORCID)": (len(rb), None, ratio(rb, "oc_sc")),
    }
    # invariant: a citing paper that is itself on the user's list is a self-citation in every route
    viol = [(r["doi"], route) for route, rows in (("A", ra), ("B", rb)) for r in rows
            if r["doi"] in own and not r["self_me"]]
    # disagreement between A and B on citing papers both routes saw
    amap = {(r["doi"], r["cited"]): r for r in ra if r["doi"]}
    dis = [(k[0], k[1][:50], amap[k]["self_me"], r["self_me"], amap[k]["self_any"], r["self_any"])
           for r in rb if (k := (r["doi"], r["cited"])) in amap
           and (amap[k]["self_me"] != r["self_me"] or amap[k]["self_any"] != r["self_any"])]

    rules = load_rules(Path(__file__).resolve().parents[1] / "references" / "venue_rules.csv")
    sci, ei = load_issns(a.sci_list), load_issns(a.ei_list)
    pubs = []
    for it in items:
        c = classify(it, rules, sci, ei)
        pubs.append(dict(年份=it["year"], **c, 题目=it["title"],
                         刊物会议=it["venue"] if it.get("oa_source_type") == "repository" else (it.get("oa_venue") or it["venue"]),
                         Scholar引用=it.get("gs_cites") if it.get("gs_cites") is not None else "", DOI=it["doi"],
                         DOI核验={True: "通过", False: "不符"}.get(it.get("doi_ok"), ""),
                         来源="+".join(sorted(it["sources"]))))
    pubs.sort(key=lambda r: (r["类型"], -int(r["年份"] or 0)))
    write_csv(out / "publications.csv", pubs)
    write_csv(out / "citations_by_paper.csv", per_paper)
    write_csv(out / "selfcite_disagreements.csv",
              [dict(施引DOI=d[0], 被引=d[1], A本人=d[2], B本人=d[3], A全作者=d[4], B全作者=d[5]) for d in dis])
    random.seed(0)
    sample = random.sample(ra, min(20, len(ra)))
    write_csv(out / "spot_check.csv", [dict(施引DOI=s["doi"], 被引=s["cited"][:60], 本人自引=s["self_me"],
                                            全作者自引=s["self_any"], 人工核对="") for s in sample])

    summary = report(pubs, totals, gs_total, routes, ids, viol, dis, per_paper, log, a.exclude_from_year,
                     bool(sci or ei))
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)


def count(pubs: list[dict]) -> dict:
    yes = lambda r, k: final(r[k + "_规则"], r[k + "_名单"]).startswith("是")  # noqa: E731
    sure = lambda r, k: final(r[k + "_规则"], r[k + "_名单"]) == "是"  # noqa: E731
    j = [r for r in pubs if r["类型"] == "期刊"]
    return dict(total=len(pubs), journal=len(j), conf=sum(r["类型"] == "会议" for r in pubs),
                chapter=sum(r["类型"] == "书章节" for r in pubs), undecided=sum(r["类型"] == "待定" for r in pubs),
                foreign=sum(r["国内外"] == "国外" for r in j), domestic=sum(r["国内外"] == "国内" for r in j),
                sci=sum(sure(r, "SCI") for r in pubs), sci_unsure=sum(yes(r, "SCI") or "待核" in final(r["SCI_规则"], r["SCI_名单"]) for r in pubs),
                ei_only=sum(sure(r, "EI") and not yes(r, "SCI") for r in pubs),
                ei_only_unsure=sum((yes(r, "EI") or "待核" in final(r["EI_规则"], r["EI_名单"])) and not yes(r, "SCI") for r in pubs),
                ei_all=sum(sure(r, "EI") for r in pubs))


def report(pubs, totals, gs_total, routes, ids, viol, dis, per_paper, log, cut, listed=False) -> str:
    c = count(pubs)
    basis = "已对照官方名单" if listed else "按刊物惯例判断，未对照官方名单"
    L = ["# 论文统计（交叉核验版）", "",
         "| 项目 | 数值 | 口径 |", "|---|---|---|",
         f"| 发表论文共计 | {c['total']} | 期刊 {c['journal']} + 会议 {c['conf']} + 书章节 {c['chapter']}"
         + (f" + 待定 {c['undecided']}" if c["undecided"] else "") + " |",
         f"| 国外刊物 | {c['foreign']} | 仅期刊 |", f"| 国内刊物 | {c['domestic']} | 仅期刊 |",
         f"| SCI 收录 | {c['sci']}（上限 {c['sci_unsure']}） | {basis} |",
         f"| EI 收录（不含 SCI） | {c['ei_only']}（上限 {c['ei_only_unsure']}） | 只收 EI、不收 SCI；{basis}；EI 全部 {c['ei_all']} |",
         f"| 总引用 | {gs_total} | Google Scholar"
         + (f"；2021 起 {totals[1]}；h={totals[2]}，i10={totals[4]}" if len(totals) >= 5 else "") + " |"]
    est = []
    for k, (n, me, anyr) in routes.items():
        if n:
            est.append((k, n, me, anyr))
    ab = [(me, anyr) for k, n, me, anyr in est if me is not None]
    if ab:
        me_m, any_m = sum(x for x, _ in ab) / len(ab), sum(y for _, y in ab) / len(ab)
        L.append(f"| 他引（去本人自引） | ≈{round(gs_total * (1 - me_m), -1):.0f} | 估算：Scholar 总引用 ×(1−自引率)，"
                 f"自引率取 A/B 两路均值 {me_m:.1%} |")
        L.append(f"| 他引（去全部作者自引） | ≈{round(gs_total * (1 - any_m), -1):.0f} | 同上，自引率 {any_m:.1%} |")
    if cut:
        c2 = count([r for r in pubs if not (r["年份"] or "0").isdigit() or int(r["年份"]) < cut])
        L.append(f"| 不含 {cut} 年起论文 | 共 {c2['total']}，SCI {c2['sci']}，EI(不含SCI) {c2['ei_only']} | 新刊出可能尚未被数据库收录 |")
    L += ["", "## 交叉核验", ""]
    for k, n, me, anyr in est:
        L.append(f"- 路线{k}：{n} 条施引；" + (f"本人自引 {me:.1%}，" if me is not None else "")
                 + f"全作者自引 {anyr:.1%}" + ("（下限）" if me is None else ""))
    mes = [me for _, _, me, _ in est if me is not None]
    anys = [a for *_, a in est]
    if len(mes) > 1:
        L.append(f"- A/B 两路：本人自引率差 {abs(mes[0] - mes[1]):.1%}，全作者自引率差 {abs(anys[0] - anys[1]):.1%}。"
                 "差距 >3 个百分点时先看 selfcite_disagreements.csv，再定口径。"
                 "路线 C 只认 ORCID，是全作者自引的下限，不参与比较。")
    L.append(f"- 本人在 OpenAlex 的作者 ID（出现次数）：{ids}。次数很少的 ID 可能是同名他人，确认后再信路线 A。")
    L.append(f"- 自己的论文引用自己的论文却没被判为自引：{len(viol)} 条" + (f"，例：{viol[:5]}" if viol else "（不变式通过）"))
    L.append(f"- A/B 判定不一致的施引记录：{len(dis)} 条（selfcite_disagreements.csv）")
    odd = [p for p in per_paper if p["备注"]]
    L.append(f"- 单篇引用数异常被剔出路线 A：{len(odd)} 篇" + (f"：{[p['题目'][:40] for p in odd]}" if odd else ""))
    single = [r for r in pubs if r["来源"] in ("scholar", "orcid")]
    L.append(f"- 只有一个来源、且未被 OpenAlex 确认的条目：{len(single)} 篇，需本人确认：{[r['题目'][:40] for r in single]}")
    bad = [r for r in pubs if r["DOI核验"] == "不符"]
    L.append(f"- DOI 元数据与题目/作者不符：{len(bad)} 篇" + (f"：{[r['题目'][:40] for r in bad]}" if bad else ""))
    pend = [r for r in pubs if "待核" in r["SCI_规则"] + r["EI_规则"] and not (r["SCI_名单"] or r["EI_名单"])]
    L.append(f"- 收录情况待核（规则表未覆盖且未提供名单）：{len(pend)} 篇：{[r['刊物会议'][:30] for r in pend]}")
    L.append("- spot_check.csv：随机 20 条施引记录，人工打开核对作者，填「人工核对」列。")
    L += ["", "## 去重与剔除", ""] + [f"- {x}" for x in log]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
