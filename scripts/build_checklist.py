"""ISMS-P 세부점검항목 엑셀을 data/checklist.csv로 변환한다 (1회성 스크립트).

실행: python -m scripts.build_checklist [엑셀 경로]
  경로를 생략하면 data/raw/ 안의 .xlsx 파일 하나를 사용한다.

엑셀 구조 (시트 3개, 모두 같은 형식):
  1행 제목, 2행 머리글, 3행부터 데이터
  B 분야번호 | C 분야명 | D 항목 ID | E 항목명 | F 상세내용 | G 주요 확인사항
  항목 하나의 확인사항이 여러 행에 이어지며, 항목 ID·항목명·상세내용은 첫 행에만 있다.

출력 (SERVICE.md 7.2):
  항목당 1행, 컬럼 id, 분야, 점검항목, 점검내용, 확인사항
  확인사항은 "1) ... 2) ..." 형태로 한 칸에 합친다. UTF-8(BOM 포함)으로 저장한다.

openpyxl 같은 엑셀 라이브러리 없이 기본 라이브러리(zipfile, xml)로 읽는다.
"""

import csv
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from src import config

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
ITEM_ID = re.compile(r"^\d+\.\d+\.\d+$")  # 예: 2.9.2
COLUMNS = ["id", "분야", "점검항목", "점검내용", "확인사항"]


def clean(text: str) -> str:
    """셀 안 줄바꿈과 연속 공백을 공백 하나로 바꾸고 앞뒤 공백을 지운다."""
    return re.sub(r"\s+", " ", text).strip()


def read_shared_strings(z: zipfile.ZipFile) -> list[str]:
    """엑셀은 문자열을 sharedStrings.xml에 모아 두고 셀에는 번호만 저장한다."""
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return [
        "".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t"))
        for si in root.findall("m:si", NS)
    ]


def sheet_paths(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """통합문서에 정의된 순서대로 (시트 이름, 시트 XML 경로)를 돌려준다."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", NS)}
    result = []
    for s in wb.find("m:sheets", NS):
        path = target[s.get(f"{{{NS['r']}}}id")].lstrip("/")
        result.append((s.get("name"), path if path.startswith("xl/") else f"xl/{path}"))
    return result


def read_rows(z: zipfile.ZipFile, path: str, shared: list[str]) -> list[dict[str, str]]:
    """시트의 각 행을 {열 문자: 값} 사전으로 읽는다. 예: {"D": "2.9.2", "E": "백업 및 복구관리"}"""
    root = ET.fromstring(z.read(path))
    rows = []
    for r in root.findall(".//m:sheetData/m:row", NS):
        cells = {}
        for c in r.findall("m:c", NS):
            col = re.match(r"[A-Z]+", c.get("r")).group()
            if c.get("t") == "inlineStr":
                val = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
            else:
                v = c.find("m:v", NS)
                val = v.text if v is not None and v.text else ""
                if c.get("t") == "s" and val:
                    val = shared[int(val)]
            cells[col] = val
        rows.append(cells)
    return rows


def convert(xlsx_path: Path) -> list[dict[str, str]]:
    """엑셀을 읽어 항목당 1개의 사전 목록으로 만든다."""
    items: list[dict] = []
    with zipfile.ZipFile(xlsx_path) as z:
        shared = read_shared_strings(z)
        for sheet_name, path in sheet_paths(z):
            area = ""  # 분야명은 분야의 첫 행에만 있으므로 아래 행으로 이어 쓴다
            for cells in read_rows(z, path, shared)[2:]:  # 제목·머리글 2행 제외
                if clean(cells.get("C", "")):
                    area = clean(cells["C"])
                item_id = clean(cells.get("D", "")).rstrip(".")
                check = clean(cells.get("G", ""))
                if ITEM_ID.match(item_id):
                    items.append({
                        "id": item_id,
                        "분야": area,
                        "점검항목": clean(cells.get("E", "")),
                        "점검내용": clean(cells.get("F", "")),
                        "checks": [check] if check else [],
                    })
                elif check:
                    if not items:
                        raise ValueError(f"[{sheet_name}] 항목 ID보다 확인사항이 먼저 나왔습니다: {check[:30]}")
                    items[-1]["checks"].append(check)

    for item in items:
        item["확인사항"] = " ".join(f"{i}) {c}" for i, c in enumerate(item.pop("checks"), 1))
    return items


def validate(items: list[dict[str, str]]) -> None:
    """빈 칸과 중복 ID가 없는지 확인한다. 문제가 있으면 저장하지 않고 중단한다."""
    ids = [it["id"] for it in items]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"중복 ID: {sorted(dup)}")
    for it in items:
        empty = [k for k in COLUMNS if not it[k]]
        if empty:
            raise ValueError(f"{it['id']}: 빈 칸 {empty}")


def main() -> None:
    if len(sys.argv) > 1:
        xlsx_path = Path(sys.argv[1])
    else:
        found = sorted(config.RAW_DIR.glob("*.xlsx"))
        if len(found) != 1:
            sys.exit(f"data/raw/에 .xlsx 파일이 정확히 1개 있어야 합니다 (현재 {len(found)}개). 경로를 인자로 주세요.")
        xlsx_path = found[0]

    items = convert(xlsx_path)
    validate(items)

    # utf-8-sig: BOM을 붙여 한국어 윈도우 엑셀에서 열어도 한글이 깨지지 않게 한다
    with open(config.CHECKLIST_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(items)

    n_checks = sum(len(re.findall(r"(?:^| )\d+\) ", it["확인사항"])) for it in items)
    print(f"원본: {xlsx_path.name}")
    print(f"저장: {config.CHECKLIST_PATH} — 항목 {len(items)}개, 확인사항 {n_checks}개")


if __name__ == "__main__":
    main()
