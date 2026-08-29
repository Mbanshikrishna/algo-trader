from __future__ import annotations

from dataclasses import dataclass  # Imports dataclass for compact immutable instrument records.
import os  # Imports os for platform-specific path guidance.
from pathlib import Path  # Imports Path for workbook-based watchlist loading.
from zipfile import ZipFile  # Imports ZipFile for reading xlsx workbooks without extra dependencies.
import xml.etree.ElementTree as ET  # Imports XML parsing for workbook sheet extraction.


@dataclass(frozen=True)
class Instrument:  # Defines the broker-specific metadata needed to request data and place trades.
    symbol: str  # Stores the user-facing symbol used throughout the app and logs.
    exchange: str = "NSE"  # Stores the exchange segment used by Angel One.
    tradingsymbol: str | None = None  # Stores the Angel One trading symbol such as SBIN-EQ.
    symboltoken: str | None = None  # Stores the Angel One numeric symbol token.

    def with_broker_fields(self, tradingsymbol: str, symboltoken: str) -> "Instrument":  # Returns a copy enriched with broker identifiers.
        return Instrument(
            symbol=self.symbol,
            exchange=self.exchange,
            tradingsymbol=tradingsymbol,
            symboltoken=str(symboltoken),
        )


DEFAULT_WATCHLIST = [  # Defines the default set of instruments to scan and trade.
    "RELIANCE.NS",
    "HDFCBANK.NS",
    "ICICIBANK.NS",
    "INFY.NS",
    "TCS.NS",
    "SBIN.NS",
    "AXISBANK.NS",
    "ITC.NS",
    "LT.NS",
    "JUBLFOOD.NS",
]

_XLSX_NS = {  # Defines the spreadsheet XML namespaces used while reading workbook rows.
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def project_symbol_for(symbol: str) -> str:  # Normalizes a broker-style symbol into the Yahoo/NSE format used across the project.
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("Stock symbol cannot be blank")
    if normalized.endswith(".NS") or normalized.endswith(".BO"):
        return normalized
    if normalized.endswith("-EQ"):
        normalized = normalized.removesuffix("-EQ")
    return f"{normalized}.NS"


def angel_tradingsymbol_for(symbol: str) -> str:  # Converts the project symbol format into an Angel One tradingsymbol guess.
    if symbol.endswith(".NS"):  # Maps Yahoo/NSE-style symbols into Angel One equity symbols.
        return f"{symbol.removesuffix('.NS')}-EQ"
    return symbol  # Leaves already normalized symbols untouched.


def watchlist_from_symbols(symbols: list[str]) -> list[Instrument]:  # Builds Instrument records from normalized project symbols.
    return [Instrument(symbol=symbol, tradingsymbol=angel_tradingsymbol_for(symbol)) for symbol in symbols]


def _load_shared_strings(workbook: ZipFile) -> list[str]:  # Extracts shared strings so cell references can be resolved into text.
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []

    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for item in root.findall("main:si", _XLSX_NS):
        values.append("".join(node.text or "" for node in item.iterfind(".//main:t", _XLSX_NS)))
    return values


def _sheet_target_for(workbook: ZipFile, sheet_name: str) -> str:  # Resolves a workbook sheet name into its worksheet xml target.
    workbook_root = ET.fromstring(workbook.read("xl/workbook.xml"))
    rels_root = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    rel_map = {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in rels_root.findall("rel:Relationship", _XLSX_NS)
    }

    for sheet in workbook_root.findall("main:sheets/main:sheet", _XLSX_NS):
        if sheet.attrib.get("name") != sheet_name:
            continue
        rel_id = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        return "xl/" + rel_map[rel_id]
    raise ValueError(f"Sheet '{sheet_name}' was not found in workbook")


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str:  # Converts an xlsx cell element into plain text.
    value = cell.find("main:v", _XLSX_NS)
    if value is None or value.text is None:
        return ""
    raw = value.text
    if cell.attrib.get("t") == "s":
        return shared_strings[int(raw)]
    return raw


def _resolve_workbook_path(workbook_path: str | Path) -> Path:  # Resolves and validates the workbook path before the xlsx is opened.
    path = Path(workbook_path).expanduser()
    if path.exists():
        return path

    path_text = str(workbook_path)
    if os.name != "nt" and len(path_text) >= 2 and path_text[1] == ":":
        raise FileNotFoundError(
            f"Workbook not found: {workbook_path}. This looks like a Windows path, but the current machine is not Windows. "
            "Copy the Excel file onto this server first and use its Linux path, for example /home/ubuntu/algo-trader/pnl-RX6263.xlsx."
        )

    raise FileNotFoundError(
        f"Workbook not found: {workbook_path}. Make sure the file exists on this machine and pass the local path to it."
    )


def symbols_from_xlsx(workbook_path: str | Path, sheet_name: str = "Equity") -> list[str]:  # Loads symbol values from the Zerodha-style equity P&L workbook.
    path = _resolve_workbook_path(workbook_path)
    with ZipFile(path) as workbook:
        shared_strings = _load_shared_strings(workbook)
        sheet_root = ET.fromstring(workbook.read(_sheet_target_for(workbook, sheet_name)))

    header_found = False
    symbols: list[str] = []
    seen: set[str] = set()
    for row in sheet_root.findall(".//main:sheetData/main:row", _XLSX_NS):
        values_by_column: dict[str, str] = {}
        for cell in row.findall("main:c", _XLSX_NS):
            reference = cell.attrib.get("r", "")
            column = "".join(character for character in reference if character.isalpha())
            values_by_column[column] = _cell_text(cell, shared_strings).strip()

        symbol_value = values_by_column.get("B", "")
        isin_value = values_by_column.get("C", "")
        if not header_found:
            header_found = symbol_value == "Symbol" and isin_value == "ISIN"
            continue

        if not symbol_value:
            continue

        normalized = project_symbol_for(symbol_value)
        if normalized in seen:
            continue
        seen.add(normalized)
        symbols.append(normalized)

    if not symbols:
        raise ValueError(f"No symbols were found in '{path}'")
    return symbols


def watchlist_from_xlsx(workbook_path: str | Path, sheet_name: str = "Equity") -> list[Instrument]:  # Builds an instrument watchlist from an xlsx workbook.
    return watchlist_from_symbols(symbols_from_xlsx(workbook_path=workbook_path, sheet_name=sheet_name))


def default_watchlist() -> list[Instrument]:  # Builds the default watchlist as Instrument records.
    return watchlist_from_symbols(DEFAULT_WATCHLIST)
