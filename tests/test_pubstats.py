"""All names, titles and numbers below are fictional."""
from __future__ import annotations

import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

import pubstats as ps  # noqa: E402

RULES = ps.load_rules(SKILL / "references" / "venue_rules.csv")


def _item(title, venue="", year="2022", **kw):
    return dict(title=title, venue=venue, year=year, orcid_type=kw.pop("orcid_type", ""), doi=kw.pop("doi", ""),
                sources={kw.pop("src", "orcid")}, gs_cites=kw.pop("gs_cites", None), **kw)


def test_name_matches_variants_but_not_other_people():
    for n in ["Wei Zhang", "Zhang, Wei", "W. Zhang", "Wei Ming Zhang", "ZHANG WEI"]:
        assert ps.name_matches(n, "Wei", "Zhang"), n
    for n in ["Lei Zhang", "Weiming Zhang", "Wei Zheng", ""]:
        assert not ps.name_matches(n, "Wei", "Zhang"), n


def test_merge_short_title_page_twin_preprint_and_thesis():
    orcid = [_item("FooNet: Distilling Optical Knowledge for SAR Segmentation", "IEEE TGRS", "2022")]
    gs = [dict(title="FooNet", venue="IEEE TGRS 60, 1-15, 2022", year="2022", gs_cites=90, gs_cites_id="1"),
          dict(title="Fusing radar and street images for city models", venue="2018 21st FUSION, 1601-1606, 2018",
               year="2018", gs_cites=6, gs_cites_id="2"),
          dict(title="Fusion of radar and social media images for city models", venue="2018 21st FUSION, 1601-1606, 2018",
               year="2018", gs_cites=1, gs_cites_id="3"),
          dict(title="A benchmark data set with a new nomenclature", venue="arXiv preprint arXiv:2001.00001", year="2020",
               gs_cites=35, gs_cites_id="4"),
          dict(title="Robust multibaseline interferometry", venue="Example University of Technology, 2019",
               year="2019", gs_cites=0, gs_cites_id="5")]
    items, log = ps.merge(orcid, gs)
    assert len(items) == 2
    assert items[0]["gs_cites"] == 90 and items[0]["sources"] == {"orcid", "scholar"}
    assert items[1]["gs_cites"] == 6
    assert sum("剔除" in x for x in log) == 2


def test_classify_rules_and_ei_only_count():
    rows = []
    for title, venue, t in [("A", "IEEE Transactions on Geoscience and Remote Sensing", "journal-article"),
                            ("基于对比学习的方法", "Journal of Radars", ""),
                            ("B", "Journal of Systems Engineering and Electronics", "journal-article"),
                            ("C", "IGARSS 2020-2020 IEEE International Geoscience and Remote Sensing Symposium", ""),
                            ("D", "FRINGE 2017, 1-6, 2017", ""),
                            ("E", "Remote Sensing", "journal-article")]:
        c = ps.classify(_item(title, venue, orcid_type=t), RULES, set(), set())
        rows.append(dict(c, 年份="2022"))
    assert [r["类型"] for r in rows] == ["期刊", "期刊", "期刊", "会议", "会议", "期刊"]
    assert [r["国内外"] for r in rows] == ["国外", "国内", "国内", "国外", "国外", "国外"]
    n = ps.count(rows)
    assert (n["journal"], n["domestic"], n["foreign"]) == (4, 2, 2)
    assert n["sci"] == 3                    # TGRS, JSEE, MDPI RS
    assert n["ei_only"] == 2                # Journal of Radars + IGARSS; JSEE/TGRS are SCI, FRINGE not EI


def test_official_list_overrides_rule():
    it = _item("E", "Remote Sensing", orcid_type="journal-article", issn=["2072-4292"])
    c = ps.classify(it, RULES, sci={"2072-4292"}, ei={"2072-4292"})
    assert c["EI_规则"] == "待核" and c["EI_名单"] == "是" and ps.final(c["EI_规则"], c["EI_名单"]) == "是"


def test_plausible_rejects_inflated_openalex_count():
    assert not ps.plausible(dict(oa_cites=400, gs_cites=15, cr_cites=14))
    assert ps.plausible(dict(oa_cites=300, gs_cites=400, cr_cites=280))


def test_parse_scholar_row_and_totals():
    page = ('<td class="gsc_rsb_std">1234</td><td class="gsc_rsb_std">987</td>'
            '<tr class="gsc_a_tr"><td class="gsc_a_t"><a href="/x" class="gsc_a_at">Radar &amp; optics</a>'
            '<div class="gs_gray">W Zhang, L Li</div><div class="gs_gray">Some Journal 12, 1-9<span>, 2018</span></div></td>'
            '<td class="gsc_a_c"><a href="https://scholar.google.com/scholar?cites=1111" class="gsc_a_ac gs_ibl">321</a></td>'
            '<td class="gsc_a_y"><span class="gsc_a_h gsc_a_hc gs_ibl">2018</span></td></tr>')
    rows, totals = ps.parse_scholar(page)
    assert totals == [1234, 987]
    assert rows[0]["title"] == "Radar & optics" and rows[0]["gs_cites"] == 321 and rows[0]["year"] == "2018"
    assert rows[0]["gs_cites_id"] == "1111"


def test_regressions():
    # 'IGARSS 2020-2020 ...' is a year range, not pages: two different IGARSS papers must stay apart
    gs = [dict(title=f"Paper {k}", venue="IGARSS 2020-2020 IEEE International Geoscience and Remote Sensing Symposium",
               year="2020", gs_cites=1, gs_cites_id=k) for k in "ab"]
    assert len(ps.merge([], gs)[0]) == 2
    # pages live on the Scholar venue even when the ORCID venue has none
    orcid = [_item("Fusing radar and street images for city models", "FUSION", "2018")]
    gs = [dict(title="Fusing radar and street images for city models", venue="21st FUSION, 1601-1606, 2018",
               year="2018", gs_cites=6, gs_cites_id="1"),
          dict(title="Fusion of radar and social media images for city models", venue="21st FUSION, 1601-1606, 2018",
               year="2018", gs_cites=1, gs_cites_id="2")]
    assert len(ps.merge(orcid, gs)[0]) == 1
    # OpenAlex files IET proceedings under a 'journal' source; the work type wins
    c = ps.classify(_item("X", "IET conference proceedings.", oa_type="conference-paper", oa_source_type="journal"),
                    RULES, set(), set())
    assert c["类型"] == "会议"
    # OpenAlex type 'other' + Crossref 'book-chapter', or a Wiley chapter DOI
    assert ps.classify(_item("Y", "A handbook", oa_type="other", cr_type="book-chapter"),
                       RULES, set(), set())["类型"] == "书章节"
    assert ps.classify(_item("Z", "", oa_type="other", doi="10.1002/0000000000000.ch11"), RULES, set(), set())["类型"] == "书章节"
    # a DOI cited by mistake inflates Crossref too; Scholar is the reference
    assert not ps.plausible(dict(oa_cites=400, gs_cites=15, cr_cites=380))
