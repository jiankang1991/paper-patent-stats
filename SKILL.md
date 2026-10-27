---
name: paper-patent-stats
description: >-
  Count a researcher's papers, citations and granted patents for Chinese forms, with cross-checks.
  Covers 总数, 国内/国外, SCI, EI, 他引, and 授权专利 by country, grant date and inventor rank. 触发：“填表要论文篇数和引用”“SCI/EI 收录多少篇”“他引次数是多少”“查我的授权专利”“统计我的论文和专利”。
---

# Paper and patent statistics with cross-checked numbers

Forms such as 职称, 人才计划, 基金申请, 结题 and 年度考核 ask for the same numbers: papers in total,
foreign vs domestic journals, SCI, EI, total citations, citations by others (他引) and granted
patents. Google Scholar gives only the total. Every other number has to be assembled, and each
source has known failure modes. Report each number with its definition (口径) and the check that
backs it.

## Step 0: collect the user's own identifiers

This skill holds no personal data. Ask the user for the following before running anything, and use
only what they give:

| Item | Used for | Required |
|---|---|---|
| 中文姓名 | patent search (CN inventor field) | for patents |
| English name in pinyin, "Given Family" (e.g. "Wei Zhang") | self-citation matching, English patent search | yes |
| ORCID iD | paper list | yes |
| Google Scholar user ID (the `user=` value in the profile URL) | paper list, total citations | yes |
| Every employer, Chinese and English names (current and former) | patent search by assignee | for patents |
| Email (optional) | OpenAlex/Crossref polite pool, faster; passed as `--mailto` | no |
| Which 他引 definition the form uses | which number leads | ask; report both until answered |

Never fill any of these from account context, git config or memory of another person. Store the
user's identifiers only in the user's own memory/notes if they ask.

## Run: papers

```bash
SKILL_ROOT="/absolute/path/to/paper-patent-stats"
python3 -I "$SKILL_ROOT/scripts/pubstats.py" --orcid <ORCID> --scholar-user <Scholar user=> \
  --name "<Given> <Family>" --out <dir> --exclude-from-year <current year> \
  [--mailto <email>] [--scholar-html page1.html page2.html] [--sci-list mjl.csv] [--ei-list compendex.csv]
```

Standard library only. The run takes 5–20 minutes (Crossref is slow without `--mailto`); every
response is cached in `<dir>/cache`, so reruns are fast. Outputs: `summary.md` (form table plus the
cross-check section), `publications.csv` (one row per paper), `citations_by_paper.csv`,
`selfcite_disagreements.csv`, `spot_check.csv`.

If the live Scholar profile is blocked, have the user save the profile pages from a browser
(`cstart=0,100,…`, until a page has fewer than 100 rows) and pass them with `--scholar-html`.

## Definitions to state with every number

| 项目 | 口径 |
|---|---|
| 发表论文 | 期刊 + 会议 + 书章节；剔除预印本、学位论文；同一篇的重复条目合并 |
| 国外/国内刊物 | 只计期刊。按主办/出版地分：国内主办的英文刊（如 JSEE）也算国内 |
| SCI | SCIE 期刊；会议不计 |
| EI | 表格问的通常是“除 SCI 之外的 EI”：只收 EI、不收 SCI 的（EI 期刊 + EI 会议）。同时报 EI 全部 |
| 总引用 | Google Scholar profile 总数，附 h-index / i10 |
| 他引 | 两种口径：去本人自引；去该文任一作者的自引。按自引率换算到 Scholar 总数，并标为估算 |

The headline 他引 is the route A/B mean, rounded to tens; route C is only a lower bound. The
(上限 N) next to SCI/EI also counts the 待核 papers. Give the `--exclude-from-year` variant second.

## Cross-checks the summary must report

1. **List**: each paper's sources (ORCID / Scholar / OpenAlex) and DOI metadata check (Crossref title + author).
   Papers from a single source go to the user to confirm.
2. **Per-paper citations**: Scholar vs OpenAlex vs Crossref vs OpenCitations. A count far above Scholar's
   comes from merged records or a DOI cited by mistake, so that paper is excluded from the self-citation routes.
3. **Self-citation, three routes**: A = OpenAlex citing works × author IDs; B = OpenCitations citing
   DOIs × Crossref bylines (names); C = OpenCitations `author_sc` (ORCID-based, a lower bound). If
   A and B differ by more than 3 points, read `selfcite_disagreements.csv` before choosing a number.
4. **Invariant**: a citing paper that is on the user's own list must be a self-citation in A and B.
   A violation means the author-ID set is wrong.
5. **Indexing**: `venue_rules.csv` gives a rule-based guess, labelled 「按刊物惯例判断」. Only an official
   list confirms it: SCIE from Clarivate Master Journal List (free account, export CSV) and the Ei
   Compendex source list from Elsevier. Conference EI and the final word on any count come from the
   library's 查收查引 report.
6. **Spot check**: the user checks the 20 random citing records in `spot_check.csv` by hand.

## Source traps

| Source | Trap | Handling |
|---|---|---|
| Google Scholar | Profile pages fetch fine; `cites=` pages hit a CAPTCHA after one or two requests | Never crawl citing lists from Scholar. Proxy services (e.g. ScraperAPI) cost many credits per request and return truncated results |
| OpenAlex author | The `orcid:` filter can return a conflated same-name record; the user's own record may carry an unexpected display name | The author-ID set comes from the authorships of the user's own works; the summary prints it with counts. Rare IDs need confirmation |
| OpenAlex paging | `cursor=` paging returns 401 without an API key | `page=` paging (keyless, ≤10k results) |
| Scholar list | Short-title duplicates, one paper under two titles (same venue + pages), arXiv entries, the PhD thesis, recent papers missing | `merge()` handles these and logs each action. ORCID fills in recent papers |
| Recent papers | Indexed in WoS/EI only after formal publication | `--exclude-from-year` gives a conservative variant |

`references/venue_rules.csv` (first match wins) sets 国内/国外, SCI and EI per venue. When a paper lands in
「待核」, add a row after confirming the venue rather than hard-coding it in the script.

## Run: patents

```bash
python3 -I "$SKILL_ROOT/scripts/patents.py" --inventor <中文名> --inventor "<Given> <Family>" \
  --assignee <单位中文名> --assignee "<Institution English name>" [--assignee <former employer> ...] \
  --out <dir> [--gp-csv export1.csv ...] [--exclude <公告号> ...]
```

The source is Google Patents. Output is `patents.csv`, one row per application: 状态, 国别, 专利类型,
专利名称, 公告号, 申请号, 申请日, 授权日, 排名 k/N, 全部发明人, 专利权人, 法律状态, plus `patents_summary.md`.
Search by assignee: a common name alone returns thousands of hits.

| Trap | Handling |
|---|---|
| Hits list only the matched inventor, as a substring (张伟 also hits 李张伟) | Drop hits whose matched inventor is not an exact full-name match, then read each detail page; rank comes from its inventor order |
| The same application appears as A (公开) and B (授权) | Merge by application number and keep the grant. 授权日 comes from the timeline `granted` event |
| CN kinds: B/C 发明授权, U/Y 实用新型, S 外观 are granted; A is only published; WO is never a grant | `kind_of()` |
| Same name at the same employer | A patent sharing no co-inventor with the rest is flagged 「请确认是否本人」. Topic is not evidence. Rerun with `--exclude <公告号>` for those the user rejects |
| Google returns 503 "Sorry" after bursts; a block on an IP can last hours | 2.5 s throttle; stop on the first 503. Then use the CSV path: the user runs `https://patents.google.com/?inventor=<名>&assignee=<单位>` in their own browser (one search per name × employer), clicks Download (CSV), and passes the files with `--gp-csv`. The CSV has the ordered inventor list and grant date, so no detail pages are fetched. Never try to get past a CAPTCHA |
| Legal status | Google's status is an assumption. Point the user to CNIPA 中国及多国专利审查信息查询 for the authoritative status |

CNIPA 公布公告 sits behind anti-bot protection: an honest headless browser gets a blank page, and getting
through means disguising the browser, which this skill does not do. Authorised automatic routes need the
user's own credentials: an EPO OPS key + secret (free), BigQuery patents-public-data, a Lens API token, or an
institutional incoPat/智慧芽 key. Offer them only if the user asks for fully automatic CN patent search.

## Reporting

Lead with the form table, values only. Follow with the definitions, then the cross-check results:
what agrees, what differs and by how much, and what the user must confirm. Give absolute paths of
`publications.csv` and `patents.csv`.
