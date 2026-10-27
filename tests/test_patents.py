"""All names, titles and patent numbers below are fictional."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import patents as pt  # noqa: E402


def test_kind_codes():
    assert pt.kind_of("CN100000001B") == ("发明", True)
    assert pt.kind_of("CN100000002A") == ("发明", False)
    assert pt.kind_of("CN200000003U") == ("实用新型", True)
    assert pt.kind_of("CN300000004S") == ("外观设计", True)
    assert pt.kind_of("WO2023000005A1") == ("PCT", False)
    assert pt.kind_of("US10000006B2") == ("发明", True)
    assert pt.kind_of("US20230000007A1") == ("发明", False)
    assert pt.kind_of("EP3000008B1") == ("发明", True)


def test_exact_name_not_substring():
    assert pt.same_person("张伟", ["张伟", "Wei Zhang"])
    assert pt.same_person("Zhang Wei", ["张伟", "Wei Zhang"])
    assert not pt.same_person("李张伟", ["张伟", "Wei Zhang"])
    assert not pt.same_person("张伟明", ["张伟"])


def test_snippet_prefilter():
    assert pt.snippet_inventors({"inventor": "李<b>张伟</b>"}) == ["李张伟"]
    assert pt.snippet_inventors({"inventor": "<b>Wei</b> <b>Zhang</b>, Lei Li"}) == ["Wei Zhang", "Lei Li"]


def test_google_patents_csv_export(tmp_path):
    f = tmp_path / "gp.csv"
    f.write_text('search URL:,"https://patents.google.com/?inventor=张伟&assignee=示例大学"\n'
                 'id,title,assignee,inventor/author,priority date,filing/creation date,publication date,grant date,result link,representative figure link\n'
                 'CN-100000001-B,一种示例雷达图像处理方法,示例大学,"张伟, 王芳",2022-10-17,2022-10-17,2024-09-06,2024-09-06,https://x,\n'
                 'CN-100000001-A,一种示例雷达图像处理方法,示例大学,"张伟, 王芳",2022-10-17,2022-10-17,2023-03-07,,https://x,\n'
                 'CN-100000009-A,Catalytic device,示例大学,李张伟,2021-08-13,2021-08-13,2021-10-29,,https://x,\n',
                 encoding="utf-8")
    rows = pt.load_gp_csv(str(f))
    assert [r["pub"] for r in rows] == ["CN100000001B", "CN100000001A", "CN100000009A"]
    assert rows[0]["inventors"] == ["张伟", "王芳"] and rows[0]["granted"] == "2024-09-06"
    out = tmp_path / "out"
    run = [sys.executable, "-I", str(Path(pt.__file__)), "--gp-csv", str(f), "--inventor", "张伟", "--out", str(out)]
    subprocess.run(run, check=True, capture_output=True)
    table = (out / "patents.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(table) == 2                       # A+B merged, 李张伟 dropped
    assert "已授权" in table[1] and "1/2" in table[1] and "2024-09-06" in table[1]
    subprocess.run(run + ["--exclude", "CN100000001"], check=True, capture_output=True)
    assert len((out / "patents.csv").read_text(encoding="utf-8-sig").splitlines()) <= 1   # A and B both excluded
