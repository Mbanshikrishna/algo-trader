from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from zipfile import ZipFile

from config.instruments import symbols_from_xlsx, watchlist_from_xlsx


def _build_sample_workbook(path: Path) -> None:
    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Equity" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
"""
    shared_strings_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="8" uniqueCount="8">
  <si><t>Summary</t></si>
  <si><t>Symbol</t></si>
  <si><t>ISIN</t></si>
  <si><t>SBIN</t></si>
  <si><t>INE062A01020</t></si>
  <si><t>INFY</t></si>
  <si><t>INE009A01021</t></si>
  <si><t>SBIN</t></si>
</sst>
"""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="13"><c r="B13" t="s"><v>0</v></c></row>
    <row r="38">
      <c r="B38" t="s"><v>1</v></c>
      <c r="C38" t="s"><v>2</v></c>
    </row>
    <row r="39">
      <c r="B39" t="s"><v>3</v></c>
      <c r="C39" t="s"><v>4</v></c>
    </row>
    <row r="40">
      <c r="B40" t="s"><v>5</v></c>
      <c r="C40" t="s"><v>6</v></c>
    </row>
    <row r="41">
      <c r="B41" t="s"><v>7</v></c>
      <c r="C41" t="s"><v>4</v></c>
    </row>
  </sheetData>
</worksheet>
"""

    with ZipFile(path, "w") as workbook:
        workbook.writestr("xl/workbook.xml", workbook_xml)
        workbook.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        workbook.writestr("xl/sharedStrings.xml", shared_strings_xml)
        workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml)


class WorkbookWatchlistTests(unittest.TestCase):
    def test_symbols_from_xlsx_extracts_and_normalizes_unique_symbols(self) -> None:
        temp_dir = Path(__file__).resolve().parent.parent / ".tmp" / "test-excel-watchlist-1"
        temp_dir.mkdir(parents=True, exist_ok=True)
        workbook_path = temp_dir / "sample.xlsx"
        try:
            _build_sample_workbook(workbook_path)
            symbols = symbols_from_xlsx(workbook_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        self.assertEqual(symbols, ["SBIN.NS", "INFY.NS"])

    def test_watchlist_from_xlsx_builds_instruments(self) -> None:
        temp_dir = Path(__file__).resolve().parent.parent / ".tmp" / "test-excel-watchlist-2"
        temp_dir.mkdir(parents=True, exist_ok=True)
        workbook_path = temp_dir / "sample.xlsx"
        try:
            _build_sample_workbook(workbook_path)
            instruments = watchlist_from_xlsx(workbook_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        self.assertEqual([instrument.symbol for instrument in instruments], ["SBIN.NS", "INFY.NS"])
        self.assertEqual(instruments[0].tradingsymbol, "SBIN-EQ")


if __name__ == "__main__":
    unittest.main()
