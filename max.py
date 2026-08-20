#!/usr/bin/env python3
"""
Max - wide procurement dataset builder
======================================

Author  : Prof. Shahab Anbarjafari
Purpose : Assemble one wide, analysis-ready table from the separate procurement
          extracts, in three stages, each written to CSV and JSONL.

    Stage 1   Sievo transactions widened with the matching invoice lines.
    Stage 2   The result widened again with the Maximo or Basware purchase
              order line, matched on PO number plus PO line number.
    Stage 3   The free text carried on every row read and turned into
              structured columns.

The governing rule
------------------
A join here *widens* the transaction table; it never lengthens it. The Sievo
extract is the spend record, so one Sievo row must remain one output row from
the first stage to the last. Where several invoice lines or several PO lines
answer to the same transaction, they are folded into summary columns and the
fan-out is recorded, rather than being emitted as extra rows. Any other choice
would multiply spend during a join, and a spend figure that changes because a
reference table was attached to it is worse than no figure at all. The row count
is asserted at the end of every stage.

A second rule follows from the first: on a header-level match, a column is
filled only when every candidate line agrees on its value. If two PO lines on
the same order name different suppliers, the supplier column is left empty and
the disagreement is reported, because a plausible-looking wrong value costs more
to find later than a blank does now.

What matches what
-----------------
Nothing is assumed about which file is which. Every table found under the source
folder is classified by its header signature, so the files can be renamed, split
or supplied several at a time. Each join then tries a ladder of key strategies
from the most specific to the least, and records which one fired for each row:

    Stage 1   invoice number + line -> document number + line -> document id +
              line -> the same three again at document level
    Stage 2   PO number + PO line -> PO number where the order has exactly one
              line -> PO number at header level

"Document id" is the opaque identifier Sievo carries in `DocumentIdentifier`,
which also appears as the `docid` parameter of `InvoiceLink` and as the leading
element of the invoice `xml_file_name`. It is worth trying because it survives
in extracts where the invoice number column was never populated.

Every stage writes a diagnostic block recording how many rows matched, by which
strategy, and how populated each candidate key was. When a join matches nothing
that block is the answer to why, and it is the thing to send back to whoever
produced the extract.

Language model
--------------
Stages 1 and 2 never use one. A join is a key operation with a right answer, and
a model can only introduce error into it.

Stage 3 resolves free text with a controlled vocabulary, compound splitting and
a set of extraction rules, all of which run locally at no cost. The model is
offered only for the residue those cannot read, is asked once per distinct
string rather than once per row, is cached on a hash of the request, and runs
under the same spend guard as the other agents: a limit in dollars is agreed up
front, reaching it pauses the run to ask, and declining finishes the work on the
local stack.

Usage
-----
    python max.py                 # prompts for each path in turn
    python max.py --non-interactive --sources ./sources --results ./results
    python max.py --help
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple
from xml.etree import ElementTree

# --- Optional components ---------------------------------------------------
# All of these are optional. Each improves speed or reach; none is required, and
# the tool reports at start-up which ones it found.

try:                                     # faster and stricter workbook reader
    import openpyxl as _openpyxl
except ImportError:
    _openpyxl = None

try:                                     # only used when the model tier is on
    import requests as _requests
except ImportError:
    _requests = None

try:                                     # widens the English word list
    import nltk as _nltk
except ImportError:
    _nltk = None


AGENT_NAME = "Max - wide procurement dataset builder"
AGENT_VERSION = "1.0.0"

BANNER = f"""
===============================================================================
 Fortum AI-Powered Procurement Analysis
 {AGENT_NAME}
 Prof. Shahab Anbarjafari
===============================================================================
"""

LOGGER = logging.getLogger("max")

# Written into every output row so a result can be traced to the run and to the
# vocabulary version that produced it.
CSV_ENCODING = "utf-8-sig"


def describe_environment() -> Dict[str, bool]:
    return {
        "openpyxl": _openpyxl is not None,
        "requests": _requests is not None,
        "nltk": _nltk is not None,
    }


# ===========================================================================
# Configuration
# ===========================================================================

# List price for the default model, in dollars per million tokens. Overridable
# from the environment because prices are revised and the shared service does
# not have to quote the same rate as the public API.
INPUT_COST_PER_MTOK = 1.25
OUTPUT_COST_PER_MTOK = 10.00

# Default alert threshold offered at the prompt, in dollars.
DEFAULT_SPEND_LIMIT = 25.00


@dataclass
class ModelConfig:
    """Resolved language-model connection details. See .env.example."""

    enabled: bool = False
    backend: str = "openai"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.1"
    batch_size: int = 20
    timeout: int = 90
    max_requests: int = 0
    spend_limit: float = 0.0         # dollars; 0 means no alert
    input_cost_per_mtok: float = INPUT_COST_PER_MTOK
    output_cost_per_mtok: float = OUTPUT_COST_PER_MTOK

    @property
    def endpoint(self) -> str:
        url = self.base_url.rstrip("/")
        return url if url.endswith("/chat/completions") else f"{url}/chat/completions"


@dataclass
class Settings:
    """Everything the build needs, resolved from arguments and prompts."""

    source_dir: Path
    results_dir: Path
    lexicon_path: Path
    cache_dir: Path

    # Carry every native Maximo and Basware column through to the wide table.
    # Switched off when only the harmonised PO block is wanted.
    native_po_columns: bool = True

    # Free text below this many word characters is not worth interpreting; it is
    # almost always a code or a fragment.
    min_text_length: int = 3

    # A row whose deterministic interpretation resolves less than this share of
    # its content words is a candidate for the model tier.
    interpretation_floor: float = 0.55

    use_llm: bool = False
    write_jsonl: bool = True
    verbose: bool = False

    # False under --non-interactive, where nothing may block waiting for input.
    interactive: bool = True

    model: ModelConfig = field(default_factory=ModelConfig)


# ---------------------------------------------------------------------------
# Environment handling
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> Dict[str, str]:
    """Parse a ``.env`` file into a dictionary.

    Written by hand rather than pulled from a package because the format is
    trivial and this runs on machines where an extra dependency needs approval.
    Later assignments win, matching the shell and python-dotenv.
    """
    values: Dict[str, str] = {}
    if not path.is_file():
        return values

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _env_flag(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}


def _env_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _env_float(value: Optional[str], default: float) -> float:
    """Read a decimal environment variable, tolerating a currency symbol."""
    try:
        return float(str(value).strip().lstrip("$").replace(",", ""))
    except (TypeError, ValueError):
        return default


def resolve_model_config(env: Dict[str, str], use_llm: bool,
                         spend_limit: Optional[float] = None) -> ModelConfig:
    """Select the language-model backend. Mirrors Agents 1 to 4 exactly."""
    config = ModelConfig(enabled=use_llm)
    config.batch_size = max(1, _env_int(env.get("LLM_BATCH_SIZE"), 20))
    config.timeout = max(5, _env_int(env.get("LLM_TIMEOUT"), 90))
    config.max_requests = max(0, _env_int(env.get("LLM_MAX_REQUESTS"), 0))

    if spend_limit is None:
        spend_limit = _env_float(env.get("LLM_SPEND_LIMIT"), DEFAULT_SPEND_LIMIT)
    config.spend_limit = max(0.0, spend_limit)
    config.input_cost_per_mtok = max(
        0.0, _env_float(env.get("LLM_INPUT_COST_PER_MTOK"), INPUT_COST_PER_MTOK))
    config.output_cost_per_mtok = max(
        0.0, _env_float(env.get("LLM_OUTPUT_COST_PER_MTOK"), OUTPUT_COST_PER_MTOK))

    if _env_flag(env.get("AZURE_ENABLE"), False):
        config.backend = "azure"
        config.api_key = (env.get("AZURE_OPENAI_API_KEY") or env.get("AZURE_API_KEY")
                          or env.get("OPENAI_API_KEY") or "")
        config.base_url = (env.get("AZURE_OPENAI_BASE_URL") or env.get("AZURE_BASE_URL")
                           or env.get("BASE_URL")
                           or "https://genai-sharedservice-emea.pwcinternal.com/v1/chat/completions")
        config.model = (env.get("AZURE_OPENAI_MODEL") or env.get("MODEL_NAME")
                        or "azure.gpt-5.1")
    else:
        # Deliberately does not inherit BASE_URL: that variable points at the
        # shared service on this project, and inheriting it would transmit a
        # personal OpenAI key to an internal endpoint.
        config.backend = "openai"
        config.api_key = env.get("OPENAI_API_KEY") or ""
        config.base_url = env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        config.model = env.get("OPENAI_MODEL") or "gpt-5.1"

    if config.enabled and not config.api_key:
        LOGGER.warning("Language-model tier requested but no API key was found "
                       "for the %s backend; continuing without it.", config.backend)
        config.enabled = False
    return config


# ===========================================================================
# Text utilities
# ===========================================================================

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_XML_ESCAPES = re.compile(r"_x00[0-9A-Fa-f]{2}_")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

# Mojibake repair table. A UTF-8 string decoded as cp1252 produces these
# sequences; the round trip through latin-1 restores the original bytes. The
# explicit table handles the cases where the round trip is itself lossy.
_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "Ä\x8d", "Å¡", "Ð")
_MOJIBAKE_LITERAL = {
    "Ã¤": "ä", "Ã¶": "ö", "Ã¥": "å", "Ã„": "Ä", "Ã–": "Ö", "Ã…": "Å",
    "Ã©": "é", "Ã¨": "è", "Ã¼": "ü", "Ã±": "ñ", "Ã¸": "ø", "Ã¦": "æ",
    "â€™": "'", "â€œ": '"', "â€\x9d": '"', "â€“": "-", "â€”": "-", "â€¦": "...",
    "Å‚": "ł", "Å„": "ń", "Å›": "ś", "Å¼": "ż", "Åº": "ź", "Å¾": "ž",
    "Ä…": "ą", "Ä‡": "ć", "Ä™": "ę", "Ã³": "ó", "Â ": " ", "Â": "",
}


def repair_mojibake(text: str) -> str:
    """Undo the common double-encoding damage seen in exported spreadsheets."""
    if not text or not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text
    try:
        repaired = text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
        if "\ufffd" not in repaired:
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    for damaged, correct in _MOJIBAKE_LITERAL.items():
        text = text.replace(damaged, correct)
    return text


def normalise_text(value: Any) -> str:
    """Reduce any cell value to clean, single-spaced text."""
    if value is None:
        return ""
    if isinstance(value, float):
        # Whole floats are almost always integer keys that a reader widened;
        # rendering 1.0 as "1" keeps join keys comparable across files.
        text = str(int(value)) if value.is_integer() else repr(value)
    elif not isinstance(value, str):
        text = str(value)
    else:
        text = value

    text = _XML_ESCAPES.sub(" ", text)
    text = repair_mojibake(text)
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_CHARS.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip()


def fold_accents(text: str) -> str:
    """Strip diacritics for lookup purposes only; never written to output."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return (stripped
            .replace("ł", "l").replace("Ł", "L")
            .replace("ø", "o").replace("Ø", "O")
            .replace("æ", "ae").replace("Æ", "Ae")
            .replace("ß", "ss").replace("đ", "d"))


def lookup_key(text: str) -> str:
    """Canonical key for vocabulary and cache lookups."""
    return _WHITESPACE.sub(" ", fold_accents(text).lower()).strip()


def compact_key(value: Any) -> str:
    """Aggressive key for joining identifiers across systems.

    Purchase order numbers are written as ``PO23983000004`` in one system and
    ``po-23983000004`` in another; both reduce to the same key here. Leading
    zeros are preserved because they are significant in ERP numbering.
    """
    return re.sub(r"[^A-Z0-9]", "", normalise_text(value).upper())


def tokenise(text: str) -> List[str]:
    """Split into word tokens, keeping digits attached to their word."""
    return _TOKEN.findall(text)


def sentence_case(text: str) -> str:
    """Capitalise the first letter and leave the rest alone.

    ``str.capitalize`` would lower-case the remainder and destroy acronyms such
    as PPE and VAT that legitimately appear inside a description.
    """
    text = text.strip()
    return text[0].upper() + text[1:] if text else ""


def stable_hash(*parts: str) -> str:
    """Short, stable identifier derived from content."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def parse_amount(value: Any) -> Optional[float]:
    """Parse a monetary or quantity cell written in any European convention.

    Returns None rather than zero for unparseable input, because zero is a
    legitimate value and conflating the two would corrupt every sum built on it.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    text = normalise_text(value)
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^\d,.\-]", "", text.strip("()"))
    if not text or text in {"-", ".", ","}:
        return None

    has_comma, has_dot = "," in text, "." in text
    if has_comma and has_dot:
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        text = text.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif has_comma:
        head, _, tail = text.rpartition(",")
        text = head + tail if (len(tail) == 3 and head) else text.replace(",", ".")

    try:
        amount = float(text)
    except ValueError:
        return None
    return -amount if negative else amount


def format_amount(value: Optional[float]) -> str:
    """Render a number for output without trailing float noise."""
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")


# Values that mean "this cell was not filled in". Treating them as data is the
# single most common cause of a join matching the wrong thing.
_NULL_TOKENS = {
    "", "n/a", "na", "n.a.", "none", "null", "nil", "-", "--", "0",
    "unknown", "not defined", "not applicable", "tbd", "#n/a", "nan",
}


def is_blank(value: Any) -> bool:
    """Whether a cell carries no usable value."""
    return normalise_text(value).strip().lower() in _NULL_TOKENS


def line_key(value: Any) -> str:
    """Canonical form of a line number.

    ``1``, ``01`` and ``1.0`` are the same line written by three systems, so all
    three reduce to ``1``. Anything that is not a plain number is compared on
    its compacted text instead.
    """
    text = normalise_text(value)
    if not text or is_blank(text):
        return ""
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return compact_key(text)
    return str(int(number)) if abs(number - int(number)) < 1e-9 else str(number)


_DOCID_IN_URL = re.compile(r"[?&]docid=([A-Za-z0-9]+)", re.IGNORECASE)
_LEADING_ID = re.compile(r"^([A-Za-z0-9]{16,64})(?:[_.\-]|$)")


def document_id_key(value: Any) -> str:
    """Reduce an opaque document identifier to a comparable form.

    Sievo carries the identifier bare in ``DocumentIdentifier`` and again inside
    the ``docid`` parameter of ``InvoiceLink``; the invoice extract carries it as
    the leading element of ``xml_file_name``. All three spellings are reduced
    here so the three can be compared to each other.
    """
    text = normalise_text(value)
    if not text or is_blank(text):
        return ""

    match = _DOCID_IN_URL.search(text)
    if match:
        return match.group(1).lower()

    # A file name of the form "<id>_15613_15788-1412014052-D.xml".
    stem = text.split("/")[-1].split("\\")[-1]
    match = _LEADING_ID.match(stem)
    if match:
        return match.group(1).lower()

    compact = re.sub(r"[^A-Za-z0-9]", "", text).lower()
    return compact if len(compact) >= 16 else ""


# ===========================================================================
# Table reading
# ===========================================================================
#
# Every reader exposes the same shape: a header list and a factory that yields a
# fresh iterator over the rows. The factory rather than a list keeps memory flat
# on a file of a million rows, and lets the build make more than one pass over a
# source without holding it.

@dataclass
class Table:
    """A single sheet or delimited file, streamed on demand."""

    path: Path
    sheet: str
    headers: List[str]
    open_rows: Callable[[], Iterator[Tuple[int, List[str]]]]

    def iter_rows(self) -> Iterator[Tuple[int, List[str]]]:
        """Yield ``(source_row_number, values)`` with values aligned to headers."""
        width = len(self.headers)
        for row_number, values in self.open_rows():
            if len(values) < width:
                values = values + [""] * (width - len(values))
            elif len(values) > width:
                values = values[:width]
            yield row_number, values

    def iter_records(self) -> Iterator[Tuple[int, Dict[str, str]]]:
        """Yield ``(source_row_number, {header: value})``."""
        for row_number, values in self.iter_rows():
            yield row_number, dict(zip(self.headers, values))

    @property
    def label(self) -> str:
        """Human-readable identity used in logs and diagnostics."""
        if self.sheet in {"", "Sheet1", self.path.stem}:
            return self.path.name
        return f"{self.path.name}:{self.sheet}"


# --- Delimited files -------------------------------------------------------

_ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "cp1252", "cp1250", "latin-1")


def _detect_encoding(path: Path) -> str:
    """Pick the first encoding that decodes a sample without error.

    The catalogues are Central European and the PO extracts are Nordic, so both
    cp1250 and cp1252 have to be candidates. ``latin-1`` is last because it
    decodes any byte sequence and would mask a better answer.
    """
    sample = path.open("rb").read(1 << 18)
    for encoding in _ENCODING_CANDIDATES:
        try:
            sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        return encoding
    return "latin-1"


def _detect_delimiter(sample: str) -> str:
    """Choose a delimiter by counting candidates outside quoted regions."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        pass
    counts = {candidate: sample.count(candidate) for candidate in ",;\t|"}
    best = max(counts, key=lambda key: counts[key])
    return best if counts[best] > 0 else ","


def _read_delimited(path: Path) -> List[Table]:
    """Wrap a CSV or TSV file as a single streaming table."""
    encoding = _detect_encoding(path)
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        sample = handle.read(1 << 16)
    delimiter = _detect_delimiter(sample)

    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        try:
            header_row = next(csv.reader(handle, delimiter=delimiter))
        except StopIteration:
            return []
    headers = [normalise_text(cell) for cell in header_row]
    if not any(headers):
        return []

    def open_rows() -> Iterator[Tuple[int, List[str]]]:
        with path.open("r", encoding=encoding, errors="replace", newline="") as stream:
            rows = csv.reader(stream, delimiter=delimiter)
            next(rows, None)                       # discard the header
            for index, values in enumerate(rows, start=2):
                yield index, [normalise_text(cell) for cell in values]

    return [Table(path=path, sheet=path.stem, headers=headers, open_rows=open_rows)]


# --- Excel workbooks -------------------------------------------------------

_SPREADSHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_PACKAGE_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DOCUMENT_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Built-in Excel number-format identifiers that denote a date or a time. A cell
# holding a date is stored as a serial number, so without the style lookup an
# order date is emitted as "45103" and every date-based check silently fails.
_BUILTIN_DATE_FORMATS = frozenset({14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47, 27, 30, 36, 50, 57})


def _column_index(cell_reference: str) -> int:
    """Convert an Excel cell reference such as ``AB12`` to a zero-based column."""
    index = 0
    for char in cell_reference:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - 64)
    return index - 1


def _excel_serial_to_iso(value: Any) -> str:
    """Convert an Excel date serial to an ISO date string.

    Excel's epoch is 1899-12-30 rather than 1900-01-01 because the format
    deliberately reproduces a leap-year bug from Lotus 1-2-3.
    """
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return normalise_text(value)
    if serial <= 0:
        return normalise_text(value)

    from datetime import datetime, timedelta
    moment = datetime(1899, 12, 30) + timedelta(days=serial)
    if abs(serial - int(serial)) < 1e-9:
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _read_xlsx_with_openpyxl(path: Path) -> List[Table]:
    """Stream a workbook through openpyxl in read-only mode."""
    tables: List[Table] = []
    workbook = _openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet_names = list(workbook.sheetnames)
    finally:
        workbook.close()

    for sheet_name in sheet_names:
        probe = _openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            header_values: List[str] = []
            for row in probe[sheet_name].iter_rows(min_row=1, max_row=1, values_only=True):
                header_values = [normalise_text(cell) for cell in row]
                break
        finally:
            probe.close()

        if not any(header_values):
            continue

        def open_rows(sheet: str = sheet_name) -> Iterator[Tuple[int, List[str]]]:
            book = _openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                for index, row in enumerate(book[sheet].iter_rows(min_row=2, values_only=True),
                                            start=2):
                    yield index, [normalise_text(cell) for cell in row]
            finally:
                book.close()

        tables.append(Table(path=path, sheet=sheet_name,
                            headers=header_values, open_rows=open_rows))
    return tables


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> List[str]:
    """Read the workbook's shared string table."""
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.iter(_SPREADSHEET_NS + "t"))
            for item in root.iter(_SPREADSHEET_NS + "si")]


def _xlsx_date_styles(archive: zipfile.ZipFile) -> Set[int]:
    """Identify which cell style indices represent dates."""
    if "xl/styles.xml" not in archive.namelist():
        return set()
    root = ElementTree.fromstring(archive.read("xl/styles.xml"))

    custom_date_formats: Set[int] = set()
    for number_format in root.iter(_SPREADSHEET_NS + "numFmt"):
        code = number_format.attrib.get("formatCode", "").lower()
        # A format is a date format when it references day, month or year
        # placeholders outside of a literal string.
        if re.search(r"(?<!\\)[dmyh]", re.sub(r'"[^"]*"', "", code)):
            custom_date_formats.add(int(number_format.attrib["numFmtId"]))

    date_styles: Set[int] = set()
    cell_formats = root.find(_SPREADSHEET_NS + "cellXfs")
    if cell_formats is None:
        return date_styles
    for style_index, cell_format in enumerate(cell_formats.iter(_SPREADSHEET_NS + "xf")):
        format_id = int(cell_format.attrib.get("numFmtId", 0))
        if format_id in _BUILTIN_DATE_FORMATS or format_id in custom_date_formats:
            date_styles.add(style_index)
    return date_styles


def _iter_xlsx_sheet(payload: bytes, shared: List[str], date_styles: Set[int]) -> Iterator[List[str]]:
    """Yield dense row values from a worksheet part.

    Uses ``iterparse`` and clears each element after use so memory stays flat
    across a very large sheet. Sparse rows are densified against the cell
    references, because a row that omits its empty trailing cells would
    otherwise misalign against the header.
    """
    for _, element in ElementTree.iterparse(io.BytesIO(payload), events=("end",)):
        if element.tag != _SPREADSHEET_NS + "row":
            continue

        cells: Dict[int, str] = {}
        for cell in element.iter(_SPREADSHEET_NS + "c"):
            reference = cell.attrib.get("r", "")
            cell_type = cell.attrib.get("t")
            value_node = cell.find(_SPREADSHEET_NS + "v")
            inline_node = cell.find(_SPREADSHEET_NS + "is")

            if cell_type == "s" and value_node is not None:
                try:
                    text = shared[int(value_node.text)]
                except (ValueError, IndexError, TypeError):
                    text = ""
            elif inline_node is not None:
                text = "".join(node.text or "" for node in inline_node.iter(_SPREADSHEET_NS + "t"))
            elif value_node is not None:
                text = value_node.text or ""
                style = cell.attrib.get("s")
                if cell_type is None and style is not None and int(style) in date_styles:
                    text = _excel_serial_to_iso(text)
            else:
                text = ""

            if reference:
                cells[_column_index(reference)] = normalise_text(text)

        width = max(cells) + 1 if cells else 0
        yield [cells.get(position, "") for position in range(width)]
        element.clear()


def _read_xlsx_with_stdlib(path: Path) -> List[Table]:
    """Read a workbook using only the standard library.

    An .xlsx file is a zip archive of XML parts, so this is entirely supportable
    without a dependency, and having it means a missing package downgrades
    performance rather than stopping the run.
    """
    tables: List[Table] = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "xl/workbook.xml" not in names:
            return []
        relationships = {
            node.attrib["Id"]: node.attrib["Target"]
            for node in ElementTree.fromstring(
                archive.read("xl/_rels/workbook.xml.rels")).iter(_PACKAGE_REL_NS + "Relationship")
        }
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        sheets = [(node.attrib.get("name", "Sheet"),
                   node.attrib.get(_DOCUMENT_REL_NS + "id", ""))
                  for node in workbook.iter(_SPREADSHEET_NS + "sheet")]
        shared = _xlsx_shared_strings(archive)
        date_styles = _xlsx_date_styles(archive)

        for sheet_name, relationship_id in sheets:
            target = relationships.get(relationship_id, "")
            if not target:
                continue
            part = target[1:] if target.startswith("/") else f"xl/{target}"
            if part not in names:
                continue

            headers: List[str] = []
            for candidate in _iter_xlsx_sheet(archive.read(part), shared, date_styles):
                if any(candidate):
                    headers = candidate
                    break
            if not headers:
                continue

            def open_rows(part_name: str = part) -> Iterator[Tuple[int, List[str]]]:
                with zipfile.ZipFile(path) as inner:
                    stream = _iter_xlsx_sheet(inner.read(part_name), shared, date_styles)
                    started = False
                    for index, values in enumerate(stream, start=1):
                        if not started:
                            if any(values):
                                started = True
                            continue
                        yield index, values

            tables.append(Table(path=path, sheet=sheet_name,
                                headers=headers, open_rows=open_rows))
    return tables


def read_table_file(path: Path) -> List[Table]:
    """Read any supported tabular file into one table per sheet."""
    suffix = path.suffix.lower()
    try:
        if suffix in {".csv", ".tsv", ".txt"}:
            return _read_delimited(path)
        if suffix in {".xlsx", ".xlsm"}:
            if _openpyxl is not None:
                return _read_xlsx_with_openpyxl(path)
            return _read_xlsx_with_stdlib(path)
        if suffix == ".xls":
            LOGGER.warning("Legacy .xls is not supported; convert %s to .xlsx", path.name)
            return []
    except Exception as error:
        LOGGER.warning("Could not read %s: %s", path.name, error)
    return []


# ===========================================================================
# Source classification
# ===========================================================================
#
# Which file is which is decided from the header signature rather than the file
# name, so the extracts can be renamed, re-foldered or delivered several at a
# time without touching this code. Each role names the headers that identify it;
# a table is assigned the best-scoring role that clears the threshold.

_ROLE_SIGNATURES: Dict[str, Tuple[str, ...]] = {
    "sievo": ("sourcerowid", "datasource", "spend in eur", "po number",
              "category l1", "erp supplier name", "document number"),
    "invoice": ("invoice_key", "invoice_id", "row_number", "article_name",
                "row_total_excl_vat", "vat_amount", "xml_file_name"),
    "maximo": ("ponum", "polinenum", "line_description", "xpointernalnote",
               "commoditygroup", "orderqty", "unitcost"),
    "basware": ("order number", "po line number", "requisition number",
                "supplier product name", "po net sum company", "main category"),
}

# Below this share of a role's signature the table is left unclassified rather
# than forced into the closest role; a wrong role is worse than none.
_ROLE_THRESHOLD = 0.34


def classify_table(table: Table) -> Tuple[str, float]:
    """Assign a table to a source role, with the score that earned it."""
    present = {lookup_key(header) for header in table.headers if header}
    best_role, best_score = "", 0.0
    for role, signature in _ROLE_SIGNATURES.items():
        hits = sum(1 for name in signature if name in present)
        score = hits / len(signature)
        if score > best_score:
            best_role, best_score = role, score
    if best_score < _ROLE_THRESHOLD:
        return "", best_score
    return best_role, best_score


def discover_tables(source_dir: Path) -> Dict[str, List[Table]]:
    """Read every supported file under the source folder and group by role."""
    grouped: Dict[str, List[Table]] = defaultdict(list)
    paths = sorted(path for path in source_dir.rglob("*")
                   if path.is_file()
                   and path.suffix.lower() in {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls"}
                   and not path.name.startswith("~$"))

    for path in paths:
        for table in read_table_file(path):
            role, score = classify_table(table)
            if not role:
                LOGGER.info("Skipping %s: does not match a known extract (best score %.2f)",
                            table.label, score)
                continue
            LOGGER.info("%-8s <- %s (%d columns, signature %.0f%%)",
                        role, table.label, len(table.headers), score * 100)
            grouped[role].append(table)
    return grouped


class ColumnMap:
    """Resolves logical field names to the headers a particular table uses.

    Extracts get re-cased, re-spaced and occasionally renamed between deliveries,
    so every field is looked up through a list of accepted spellings rather than
    a single literal. Lookup is on the folded, lower-cased header.
    """

    def __init__(self, headers: Sequence[str], synonyms: Dict[str, Sequence[str]]) -> None:
        index = {lookup_key(header): header for header in headers if header}
        self._resolved: Dict[str, str] = {}
        for field_name, candidates in synonyms.items():
            for candidate in candidates:
                header = index.get(lookup_key(candidate))
                if header:
                    self._resolved[field_name] = header
                    break

    def header(self, field_name: str) -> str:
        return self._resolved.get(field_name, "")

    def has(self, field_name: str) -> bool:
        return field_name in self._resolved

    def get(self, record: Dict[str, str], field_name: str) -> str:
        header = self._resolved.get(field_name)
        return normalise_text(record.get(header, "")) if header else ""

    @property
    def resolved(self) -> Dict[str, str]:
        return dict(self._resolved)


SIEVO_FIELDS: Dict[str, Sequence[str]] = {
    "row_id": ("SourceRowId", "Source Row Id", "RowId"),
    "data_source": ("DataSource", "Data source", "Source system"),
    "document_number": ("Document number", "DocumentNumber", "Document no"),
    "document_line": ("Document line number", "Document line no", "DocumentLineNumber"),
    "document_line_desc": ("Document line desc", "Document line description"),
    "po_number": ("PO number", "PONumber", "Purchase order number", "Order number"),
    "po_line": ("PO line number", "POLineNumber", "Purchase order line number"),
    "po_line_desc": ("PO line desc", "PO line description"),
    "invoice_number": ("Invoice number", "InvoiceNumber", "Invoice no"),
    "document_identifier": ("DocumentIdentifier", "Document identifier"),
    "invoice_link": ("InvoiceLink", "Invoice link"),
    "supplier_name": ("ERP supplier name", "Supplier name", "Supplier"),
    "supplier_number": ("ERP supplier number", "Supplier number", "Supplier code"),
    "spend_eur": ("Spend in EUR", "Spend EUR"),
    "quantity": ("Quantity", "Qty"),
    "material_group_name": ("MaterialGroupName", "Material group name"),
    "category_l4": ("Category L4", "CategoryL4"),
    "project": ("Project",),
}

INVOICE_FIELDS: Dict[str, Sequence[str]] = {
    "invoice_key": ("invoice_key", "invoice key"),
    "invoice_id": ("invoice_id", "invoice id", "invoice number"),
    "row_number": ("row_number", "row number", "line number"),
    "xml_file_name": ("xml_file_name", "xml file name", "file name"),
    "article_id": ("article_id", "article id", "item id"),
    "article_name": ("article_name", "article name", "item name", "description"),
    "free_text": ("free_text", "free text", "text"),
    "quantity_charged": ("quantity_charged", "quantity charged", "quantity"),
    "quantity_delivered": ("quantity_delivered", "quantity delivered"),
    "unit_price_excl_vat": ("unit_price_excl_vat", "unit price excl vat"),
    "unit_price_net": ("unit_price_net", "unit price net"),
    "row_total_excl_vat": ("row_total_excl_vat", "row total excl vat"),
    "row_total_incl_vat": ("row_total_incl_vat", "row total incl vat"),
    "vat_amount": ("vat_amount", "vat amount"),
    "vat_rate": ("vat_rate", "vat rate"),
}

# The two purchase-order systems are mapped onto one harmonised vocabulary so
# that downstream work does not have to branch on which system a row came from.
# Native columns are carried through alongside, unaltered.
MAXIMO_FIELDS: Dict[str, Sequence[str]] = {
    "po_number": ("PONUM",),
    "po_line_number": ("POLINENUM",),
    "header_description": ("DESCRIPTION",),
    "line_description": ("LINE_DESCRIPTION",),
    "item_code": ("ITEMNUM",),
    "category_main": ("COMMODITYGROUP",),
    "category_sub": ("COMMODITY",),
    "quantity": ("ORDERQTY",),
    "unit_cost": ("UNITCOST",),
    "line_cost": ("LINECOST",),
    "total_cost": ("TOTALCOST",),
    "currency": ("CURRENCYCODE",),
    "supplier_name": ("VENDOR",),
    "order_date": ("ORDERDATE",),
    "status": ("STATUS",),
    "order_type": ("POTYPE",),
    "buyer": ("PURCHASEAGENT",),
    "requester": ("XREQUESTEDBY",),
    "payment_terms": ("PAYMENTTERMS",),
    "delivery_terms": ("XDELIVERYTERMS",),
    "contract_reference": ("CONTRACTREFNUM",),
    "offer_reference": ("XOFFERNUM",),
    "frame_contract": ("XFRAMECONTRACT",),
    "internal_note": ("XPOINTERNALNOTE",),
    "terms_conditions": ("XCONTRACTTERMS",),
    "company_code": ("BUYERCOMPANY",),
    "site": ("SITEID",),
}

BASWARE_FIELDS: Dict[str, Sequence[str]] = {
    "po_number": ("Order number",),
    "po_line_number": ("PO line number",),
    "line_description": ("Supplier product name",),
    "item_code": ("Supplier product code", "Item ID"),
    "category_main": ("Main category",),
    "category_sub": ("Sub category",),
    "category_code": ("Category code",),
    "quantity": ("PO line quantity",),
    "line_cost": ("PO net sum company",),
    "currency": ("PO currency company", "PO currency organization"),
    "supplier_name": ("Supplier name",),
    "supplier_code": ("Supplier code",),
    "order_date": ("PO creation date",),
    "status": ("Order status",),
    "line_status": ("Order line status",),
    "order_type": ("Order type",),
    "buyer": ("PO creator",),
    "requester": ("PR owner name", "PR creator"),
    "payment_terms": ("Payment term code",),
    "contract_reference": ("Contract number",),
    "terms_conditions": ("PO terms and conditions",),
    "company_code": ("Company code",),
    "company_name": ("Company name",),
    "cost_center_code": ("Cost center code",),
    "cost_center_name": ("Cost center name",),
    "project_code": ("Project code",),
    "project_name": ("Project name",),
    "account_code": ("Account code",),
    "account_name": ("Account name",),
    "requisition_number": ("Requisition number",),
    "unspsc": ("UNSPSC",),
    "item_type": ("Item type",),
}

# The harmonised block written for whichever system matched.
PO_OUTPUT_FIELDS: Tuple[str, ...] = (
    "po_number", "po_line_number", "header_description", "line_description",
    "item_code", "item_type", "category_main", "category_sub", "category_code",
    "quantity", "unit_cost", "line_cost", "total_cost", "currency",
    "supplier_name", "supplier_code", "order_date", "status", "line_status",
    "order_type", "buyer", "requester", "payment_terms", "delivery_terms",
    "contract_reference", "offer_reference", "frame_contract", "terms_conditions",
    "internal_note", "company_code", "company_name", "cost_center_code",
    "cost_center_name", "project_code", "project_name", "account_code",
    "account_name", "requisition_number", "unspsc", "site",
)


def _title(field_name: str) -> str:
    """Turn a logical field name into an output column name.

    Word boundaries are kept, so ``article_name`` becomes ``Article_Name`` and
    the column reads as ``Invoice_Article_Name``. Running the words together
    would produce ``Invoice_ArticleName``, which is harder to read in a
    spreadsheet header and easy to mistype when referring to the column
    elsewhere in this file.
    """
    return "_".join(part.capitalize() for part in field_name.split("_"))


def _column(prefix: str, field_name: str) -> str:
    """Name an output column, without repeating the prefix inside it.

    The logical field is ``po_number`` and the block prefix is ``PO``, so the
    column is ``PO_Number`` rather than ``PO_Po_Number``.
    """
    lead = f"{prefix.lower()}_"
    if field_name.startswith(lead):
        field_name = field_name[len(lead):]
    return f"{prefix}_{_title(field_name)}"


# ===========================================================================
# Controlled vocabulary
# ===========================================================================

@dataclass
class Lexicon:
    """The shared procurement vocabulary, loaded once and read many times."""

    version: str = "0"
    phrases: Dict[str, Dict[str, str]] = field(default_factory=dict)
    terms: Dict[str, Dict[str, str]] = field(default_factory=dict)
    compound_parts: Dict[str, Dict[str, str]] = field(default_factory=dict)
    service_markers: Set[str] = field(default_factory=set)
    material_markers: Set[str] = field(default_factory=set)
    noise_terms: Set[str] = field(default_factory=set)
    unit_terms: Dict[str, str] = field(default_factory=dict)

    # Phrase lookup is ordered longest-first so that a specific phrase wins over
    # a shorter one contained inside it.
    ordered_phrases: List[Tuple[str, str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Lexicon":
        if not path.is_file():
            LOGGER.warning("No vocabulary at %s; free text will be read with rules only.", path)
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            LOGGER.warning("Vocabulary at %s is unreadable (%s); continuing without it.",
                           path, error)
            return cls()

        lexicon = cls(
            version=str(payload.get("version", "0")),
            phrases={language: {lookup_key(key): value for key, value in entries.items()}
                     for language, entries in (payload.get("phrases") or {}).items()},
            terms={language: {lookup_key(key): value for key, value in entries.items()}
                   for language, entries in (payload.get("terms") or {}).items()},
            compound_parts={language: {lookup_key(key): value for key, value in entries.items()}
                            for language, entries in (payload.get("compound_parts") or {}).items()},
            service_markers={lookup_key(term) for term in payload.get("service_markers") or []},
            material_markers={lookup_key(term) for term in payload.get("material_markers") or []},
            noise_terms={lookup_key(term) for term in payload.get("noise_terms") or []},
            unit_terms={lookup_key(key): value
                        for key, value in (payload.get("unit_terms") or {}).items()},
        )

        ordered: List[Tuple[str, str, str]] = []
        for language, entries in lexicon.phrases.items():
            for key, value in entries.items():
                ordered.append((key, value, language))
        ordered.sort(key=lambda item: (-len(item[0]), item[0]))
        lexicon.ordered_phrases = ordered

        LOGGER.info("Vocabulary %s loaded: %d phrases, %d terms.",
                    lexicon.version, len(ordered),
                    sum(len(entries) for entries in lexicon.terms.values()))
        return lexicon

    def term(self, key: str) -> Tuple[str, str]:
        """Look a single token up across every language. Returns (english, language)."""
        for language, entries in self.terms.items():
            value = entries.get(key)
            if value:
                return value, language
        return "", ""


# ===========================================================================
# Free-text interpretation
# ===========================================================================
#
# Everything below runs locally. The extraction rules are deliberately
# conservative: each one either finds a well-formed value or reports nothing, so
# that a populated column can be trusted without checking it against the text.

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.IGNORECASE)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_EU_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_EU_DATE_SHORT = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(?!\d)")
_PERCENT = re.compile(r"\b\d+(?:[.,]\d+)?\s?%")

_CURRENCY_WORDS = "eur|sek|pln|nok|dkk|usd|gbp|czk|huf"
_MONEY = re.compile(
    rf"(?:(?P<symbol>[€$£])\s?(?P<after>\d[\d\s\u00a0.,]*)"
    rf"|(?P<before>\d[\d\s\u00a0.,]*)\s?(?P<word>[€$£]|{_CURRENCY_WORDS})\b)",
    re.IGNORECASE)

# Quantity units seen across the Nordic and Polish extracts, mapped to a single
# spelling so that "5 kpl", "5 st" and "5 pcs" become comparable.
_UNIT_CANON = {
    "kpl": "pcs", "st": "pcs", "stk": "pcs", "szt": "pcs", "pcs": "pcs",
    "pc": "pcs", "pce": "pcs", "ea": "pcs", "unit": "pcs", "units": "pcs",
    "kg": "kg", "g": "g", "t": "t", "ton": "t", "tn": "t",
    "m": "m", "mm": "mm", "cm": "cm", "km": "km",
    "m2": "m2", "m3": "m3", "l": "l", "ltr": "l", "litraa": "l",
    "h": "h", "hrs": "h", "hr": "h", "tuntia": "h", "timmar": "h", "godz": "h",
    "pv": "day", "paivaa": "day", "dagar": "day", "dni": "day",
}
_QUANTITY = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s?(" + "|".join(sorted(_UNIT_CANON, key=len, reverse=True)) + r")\b",
    re.IGNORECASE)

# Two or more numbers joined by x, as in "1250X2500" or "3/4,5 X 1250 X 2500".
_DIMENSION = re.compile(r"\b\d+(?:[.,/]\d+)*(?:\s?[x×]\s?\d+(?:[.,/]\d+)*){1,3}\b", re.IGNORECASE)

_LEAD_TIME_UNITS = {
    "viikko": 7, "viikkoa": 7, "vko": 7, "veckor": 7, "vecka": 7,
    "week": 7, "weeks": 7, "tydzien": 7, "tygodnie": 7,
    "paiva": 1, "paivaa": 1, "pv": 1, "dagar": 1, "dag": 1,
    "day": 1, "days": 1, "dni": 1, "dzien": 1, "arkipaivaa": 1,
    "kuukausi": 30, "kuukautta": 30, "manad": 30, "manader": 30,
    "month": 30, "months": 30, "miesiac": 30,
}
_LEAD_TIME = re.compile(
    r"\b(\d{1,3})\s?(" + "|".join(sorted(_LEAD_TIME_UNITS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE)

# A structured reference: at least two alphanumeric groups joined by hyphens,
# containing a digit. Catches LO1-K570-570-00083 and HAM-LS-661.
_STRUCTURED_REFERENCE = re.compile(r"\b(?=[\w-]*\d)[A-Za-z0-9]{1,10}(?:-[A-Za-z0-9]{1,10}){1,5}\b")

# An English date range such as "06th-12th" satisfies the reference pattern and
# is not a reference.
_ORDINAL_RANGE = re.compile(r"^\d{1,2}(?:st|nd|rd|th)(?:-\d{1,2}(?:st|nd|rd|th))+$",
                            re.IGNORECASE)

# A quotation or order reference introduced by a keyword, in any of the four
# languages the extracts arrive in.
_REFERENCE_KEYWORDS = re.compile(
    r"\b(tarjous|tarjouksen|tarjoukse\w*|offert\w*|offer|quote|quotation|"
    r"tilaus|tilauksen|order|bestallning|zamowienie|"
    r"sopimus|sopimuksen|contract|avtal|umowa)\b[^\w|]{0,4}(?:n[:o]{1,2}\.?\s*)?"
    r"([A-Za-z0-9][A-Za-z0-9/_-]{3,})",
    re.IGNORECASE)
# The gap between the keyword and the number may not contain a pipe: fields are
# joined with " | " before scanning, and without that exclusion the last word of
# one field binds to the first token of the next, reading "Valve order | IT-1"
# as order number IT-1.

# A model or part code: mixed letters and digits, long enough to be meaningful.
_MODEL_CODE = re.compile(r"\b(?=[A-Za-z0-9./-]*\d)(?=[A-Za-z0-9./-]*[A-Za-z])"
                         r"[A-Za-z0-9][A-Za-z0-9./-]{3,}\b")

# Stripped before model-code detection so that a measurement is not mistaken for
# a part number.
_MEASUREMENT_LIKE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s?(?:mm|cm|m|km|kg|g|t|l|ml|m2|m3|v|kv|kw|kwh|w|a|ma|"
    r"bar|pa|kpa|mpa|hz|khz|mhz|gb|tb|mb|%|°c|c)\b|"
    r"\b(?:dn|pn|iso|din|en|sfs)\s?\d+\b", re.IGNORECASE)

_STOPWORDS: Dict[str, Set[str]] = {
    "fi": {"ja", "on", "ei", "se", "etta", "kanssa", "mukaan", "seka", "tai",
           "kun", "myos", "ovat", "ole", "han", "sen", "joka", "vain", "noin"},
    "sv": {"och", "av", "for", "med", "till", "enligt", "samt", "eller", "den",
           "det", "som", "pa", "ar", "vid", "fran", "inte", "har"},
    "pl": {"i", "w", "na", "z", "do", "oraz", "lub", "sie", "jest", "nie",
           "dla", "od", "po", "przy", "za", "to"},
    "en": {"and", "the", "of", "for", "with", "to", "per", "from", "on", "in",
           "at", "by", "or", "as", "is", "are", "be"},
}

_ALL_STOPWORDS: Set[str] = set().union(*_STOPWORDS.values())

# A core English word list, used to tell text that is already English from text
# that is foreign and was not understood. The distinction decides whether a row
# is offered to the model tier, so getting it wrong is expensive in both
# directions: too narrow a list sends perfectly clear English lines to the
# model, and too broad a list lets untranslated Finnish through as though it had
# been read. The list is deliberately weighted towards the vocabulary of
# purchasing, invoicing and industrial maintenance rather than general prose,
# and it is extended at load time by every English target in the controlled
# vocabulary and, when the corpus is installed, by NLTK's word list.
_ENGLISH_CORE: Set[str] = set("""
a able about above accept access according account accounting accrual acquire
across activity actual adapter adaptor add additional address adjust administration
advance advisory after agreed agreement air alarm all allowance along also alternative
aluminium amount analysis annual another answer any application approval approved
april area arrange article assembly assessment asset assistance associated assurance
august authority automatic available average award back badge bag balance bank bar
base basic basis batch bathroom battery bearing before below belt bench between bid
bill black block blue board body bolt bonus book booking bottle bottom box bracket
brake branch brand breakdown bridge bring broadband budget build building bulk bundle
business but buy buyer cabin cabinet cable calculation calibration call camera cancel
cap capacity capital car carbon card care cargo carriage carrier case cash catalogue
category cell cement central centre certificate certification chain chair change
channel charge charger check chemical circuit civil claim clamp cleaning clear client
clip clothing coating code coil cold collection colour column combined commission
commissioning committee commodity communication company complete compliance component
composite compressor computer concrete condition conference configuration connection
consultancy consultant consulting consumable container content continuous contract
contractor control conversion conveyor cooling copper copy core corner corporate
correction cost council counter coupling cover crane credit crew cross current custom
customer cut cycle cylinder daily damage data date day dealer december decision deck
declaration decommissioning deduction deep defect definition delivery demand demolition
department deposit depot design desk detail detection development device diagnostic
diameter diesel digital dimension direct director disc discharge discount dismantling
dispatch display disposal distance distribution district diverse document domestic
door double down download drain drawing drilling drive driver drum dry due duct duty
early earth economy edge education effect efficiency electric electrical electricity
electronic element email emergency emission employee enclosure end energy engine
engineer engineering enquiry ensure entry environment environmental equipment
estimate european evaluation event examination exchange excluding execution exhaust
existing exit expense expenses expert export exposure express extended external extra
fabric fabrication facility factor factory failure fan february fee female fence field
file filter final finance financial finish fire firm first fitting fixed fixing flange
flat fleet flexible flight flights floor flow fluid foam follow food foot force
forecast form foundation frame framework free freight frequency fresh from fuel full
function furniture fuse gas gasket gate gauge gear general generation generator glass
glove goods grade graphic grease green grid grinding ground group guard guarantee
guide handle handling hardware hazardous head header health heat heater heating heavy
height help high hire hire holder hole holiday home hook hose hospital hotel hour
hourly house housing hydraulic ice identification image impact implementation import
improvement inch include including income increase independent index indirect
individual induction industrial industry information infrastructure initial injection
inspection install installation instrument insulation insurance integration interface
interim internal international internet interview inventory investigation invoice iron
issue item january job joint journal july june keep key keyboard kit knife label
labour laboratory ladder lamp land landfill language laptop large laser last late
layout lead leak lease leasing legal length lens level licence license lift light
lighting lime limit line liner link liquid list load loading loan local location lock
logistics long loss low lubricant machine machinery magnetic main maintenance major
male management manager manual manufacture manufacturer march marine mark market mass
master match material may measure measurement mechanical media medical medium meeting
member membrane metal meter method metre microphone middle mileage mill mineral
minimum mining minor mixer mobile model modification module monitor monitoring month
monthly mortar motor mount mounting mouse move multiple national natural network new
next night noise nominal north note notice november nozzle number nut object
observation october off offer office officer offshore oil online open operating
operation operator option order organisation original other outlet output outsourcing
overhaul overhead overtime package packaging pad paint painting pallet panel paper
parking part partial particle partner parts passenger patch payment peak penalty
pension per performance period permit person personal personnel petrol phase phone
photo physical pick picture piece pile pilot pin pipe pipeline piping piston place
plan planning plant plastic plate platform plug plumbing point pole police policy
polish pollution pool port portable position post power practice preparation pressure
prevention price primary print printer printing private procedure process procurement
product production professional profile programme project promotion property
protection provision public pull pump purchase purchasing pure push quality quantity
quarter quarterly quote quotation rack radio rail railway rain range rate rating raw
reactor real rebate receipt receiver recharge reclamation recommendation reconditioning
record recovery recruitment recycling reduction reference refrigeration refurbishment
refuse regional register regulation regulator reimbursement reinforcement relay release
remote removal renewal rent rental repair replacement report request requirement
rescue research reserve reset residual resin resource response rest restoration
result retail retrofit return review revision rig rig right ring risk river road
robot rock rod roll roof room rope rotor round rounding route routine rubber rubbish
running safety sale sample sand sanitary scaffold scaffolding scale scan scanner
schedule scheme school science scope scrap screen screw sea seal sealing search season
seat second secondary section sector security segment selection self seminar sensor
separate september series service servicing session set setting shaft share sheet
shelf shield shipping shop short shutdown side sign signal silicone simple single site
size skid skill sleeve slide small smart social socket software solar solid solution
solvent sound source south space spare special specialist specification speed spill
spindle spare split spool spray spreader spring stack staff stage stainless stair
stand standard start station steam steel step sterile stock stop storage store
straight strap strategy street stress strip structural structure study sub subcontract
subscription substation substitute supervision supplier supply support surface survey
suspension sweep switch switchgear system table tablet tank tape target task tax team
technical technician technology telecom telephone temporary terminal termination test
testing text thermal thermometer thickness third thread three through ticket tie tight
tile time tin tip tool toolkit top torque total tower town track traffic trailer
training transfer transformer transition transmission transport transportation travel
tray treatment trial trim trip truck tube tuning tunnel turbine turn twin two type
tyre unit universal upgrade urgent usage use used user utility vacuum valve van
variable vat vehicle vendor vent ventilation verification version vertical vessel
video view vinyl visit visual voltage volume voucher wage wall warehouse warranty
waste watch water weather web week weekly weight weld welding well west wheel white
wide width winch wind window wire wireless wood work worker working workshop workwear
wrap yard year yearly yellow zinc zone
""".split())

# A word shared by two languages says nothing about which one is in front of
# you. Swedish "for" and English "for", Polish "to" and English "to" were enough
# on their own to label an English line as Scandinavian, so only the words that
# belong to exactly one language are allowed to vote.
_DISCRIMINATING_STOPWORDS: Dict[str, Set[str]] = {
    language: {word for word in words
               if sum(word in other for other in _STOPWORDS.values()) == 1}
    for language, words in _STOPWORDS.items()
}

# Signals that place a purchase in a recognisable class. Ordered: the first
# group whose markers appear decides, so the more specific groups come first.
_INTENT_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Freight and delivery",
     ("freight", "rahti", "toimituskulu", "frakt", "delivery cost", "shipping",
      "kuljetus", "transport", "spedycja", "postage", "courier")),
    ("Travel and accommodation",
     ("travel", "matka", "flight", "flights", "hotel", "accommodation", "b&b",
      "car hire", "taxi", "resa", "podroz", "majoitus", "lento")),
    ("Rental and leasing",
     ("rental", "rent", "vuokra", "vuokraus", "hyra", "uthyrning", "lease",
      "leasing", "wynajem", "hire")),
    ("Training and certification",
     ("training", "koulutus", "utbildning", "kurs", "kurssi", "szkolenie",
      "certification", "course", "seminar", "workshop")),
    ("Consulting and advisory",
     ("consulting", "consultancy", "consultant", "konsultointi", "konsultation",
      "konsult", "doradztwo", "advisory", "audit", "study", "utredning",
      "inventering", "survey", "promemoria")),
    ("Maintenance and repair",
     ("maintenance", "huolto", "kunnossapito", "underhall", "konserwacja",
      "repair", "korjaus", "reparation", "naprawa", "service", "overhaul",
      "kalibrointi", "calibration", "besiktning", "inspection", "tarkastus")),
    ("Installation and construction",
     ("installation", "asennus", "montaz", "construction", "rakennus",
      "byggnad", "erection", "pystytys", "commissioning", "perustus")),
    ("Licences and subscriptions",
     ("licence", "license", "lisenssi", "licens", "licencja", "subscription",
      "tilaus", "software", "ohjelmisto", "programvara", "oprogramowanie",
      "saas", "support agreement")),
    ("Utilities and energy",
     ("electricity", "sahko", "el", "gas", "water", "vesi", "heat", "lampo",
      "district heating", "kaukolampo", "fjarrvarme", "energy")),
    ("Spare parts and materials",
     ("spare", "varaosa", "reservdel", "czesc", "part", "parts", "component",
      "material", "supplies", "tarvike", "consumable", "seal", "tiiviste",
      "valve", "venttiili", "bearing", "laakeri", "cable", "kaapeli")),
    ("Fees and charges",
     ("fee", "charge", "maksu", "avgift", "oplata", "booking fee", "rounding",
      "surcharge", "commission", "hallinnointi")),
)

_ATTACHMENT_MARKERS = ("liite", "liitteessa", "liitteena", "attached", "attachment",
                       "bilaga", "bifogad", "zalacznik", "enclosed", "ohessa")
_STOCK_MARKERS = ("varasto", "varastoon", "varastossa", "lager", "stock",
                  "magazyn", "warehouse", "inventory")
_URGENCY_MARKERS = ("kiireellinen", "urgent", "asap", "bradskande", "pilne",
                    "pikaisesti", "immediately", "heti")
_TAX_MARKERS = ("alv", "vat", "moms", "vat", "podatek", "mwst", "tax")


def _nltk_english_words() -> Set[str]:
    """NLTK's English word list, when the corpus has been downloaded.

    Optional. It widens the core list to ordinary English prose, which matters
    for the UK invoice descriptions. Absence costs a little recall on English
    text and nothing else, so a missing corpus is not worth a warning.
    """
    if _nltk is None:
        return set()
    try:
        from nltk.corpus import words as nltk_words
        return {word.lower() for word in nltk_words.words() if word.isalpha()}
    except Exception:
        return set()


@dataclass
class Interpretation:
    """Everything read from one row's free text."""

    text: str = ""
    sources: str = ""
    language: str = ""
    language_confidence: float = 0.0
    description: str = ""
    method: str = "none"
    confidence: float = 0.0
    item_or_service: str = ""
    intent: str = ""
    keywords: str = ""
    resolved_share: float = 0.0
    token_count: int = 0

    quantity: str = ""
    unit: str = ""
    amount: str = ""
    currency: str = ""
    dimensions: str = ""
    model_codes: str = ""
    references: str = ""
    dates: str = ""
    emails: str = ""
    domains: str = ""
    lead_time_days: str = ""
    percentages: str = ""

    mentions_tax: str = ""
    mentions_attachment: str = ""
    mentions_stock: str = ""
    mentions_urgency: str = ""

    def as_columns(self) -> Dict[str, str]:
        return {
            "Text_Sources": self.sources,
            "Text_Interpreted": self.text,
            "Text_Language": self.language,
            "Text_Language_Confidence": f"{self.language_confidence:.2f}" if self.language else "",
            "Text_Token_Count": str(self.token_count) if self.token_count else "",
            "Interpreted_Description": self.description,
            "Interpretation_Method": self.method,
            "Interpretation_Confidence": f"{self.confidence:.2f}" if self.text else "",
            "Interpretation_Resolved_Share": f"{self.resolved_share:.2f}" if self.text else "",
            "Item_Or_Service": self.item_or_service,
            "Purchase_Intent": self.intent,
            "Keywords": self.keywords,
            "Extracted_Quantity": self.quantity,
            "Extracted_Unit": self.unit,
            "Extracted_Amount": self.amount,
            "Extracted_Currency": self.currency,
            "Extracted_Dimensions": self.dimensions,
            "Extracted_Model_Codes": self.model_codes,
            "Extracted_References": self.references,
            "Extracted_Dates": self.dates,
            "Extracted_Emails": self.emails,
            "Extracted_Domains": self.domains,
            "Extracted_Lead_Time_Days": self.lead_time_days,
            "Extracted_Percentages": self.percentages,
            "Mentions_Tax": self.mentions_tax,
            "Mentions_Attachment": self.mentions_attachment,
            "Mentions_Stock": self.mentions_stock,
            "Mentions_Urgency": self.mentions_urgency,
        }

    @staticmethod
    def columns() -> List[str]:
        return list(Interpretation().as_columns().keys())


class TextInterpreter:
    """Reads a free-text field into structured columns without a model.

    The work splits in two. The extractors pull out values that have an
    unambiguous written form — amounts, quantities, dates, addresses, references
    — and are exact by construction. The reader then renders the remaining prose
    into English through the controlled vocabulary, reporting the share of
    content words it managed to resolve so that a weak reading is visible rather
    than merely wrong.
    """

    def __init__(self, lexicon: Lexicon, settings: Settings) -> None:
        self.lexicon = lexicon
        self.settings = settings
        self._cache: Dict[str, Interpretation] = {}
        self.english_words = self._english_vocabulary(lexicon)
        self.discriminating_terms = self._discriminating_terms(lexicon)

    @staticmethod
    def _english_vocabulary(lexicon: Lexicon) -> Set[str]:
        """The English side of the vocabulary, used to recognise English input.

        Every target in the vocabulary is by definition an English procurement
        word, so the file that translates into English also describes what
        English looks like. Without this, an English line resolves nothing and
        is scored as badly understood when in fact it needed no work at all.
        """
        words: Set[str] = set()
        for mapping in (lexicon.phrases, lexicon.terms, lexicon.compound_parts):
            for entries in mapping.values():
                for value in entries.values():
                    words.update(tokenise(lookup_key(value)))
        for marker in lexicon.service_markers | lexicon.material_markers:
            words.update(tokenise(marker))
        for value in lexicon.unit_terms.values():
            words.update(tokenise(lookup_key(value)))
        words |= _STOPWORDS["en"]
        words |= _ENGLISH_CORE
        words |= _nltk_english_words()
        return {word for word in words if word}

    @staticmethod
    def _discriminating_terms(lexicon: Lexicon) -> Dict[str, Set[str]]:
        """Vocabulary entries that belong to exactly one language.

        'transport', 'material' and 'system' are spelled the same in three of
        the four languages here and so identify none of them.
        """
        counts: Counter = Counter()
        for entries in lexicon.terms.values():
            counts.update(entries.keys())
        return {language: {term for term in entries if counts[term] == 1}
                for language, entries in lexicon.terms.items()}

    # -- entry point --------------------------------------------------------

    def interpret(self, describing: str, supporting: str, sources: str) -> Interpretation:
        """Interpret one row's free text, memoised on that text.

        Distinct strings are far fewer than rows in every extract seen so far,
        so caching here is what keeps a million-row run affordable.
        """
        describing = normalise_text(describing)
        supporting = normalise_text(supporting)
        combined = " | ".join(part for part in (describing, supporting) if part)
        if len(re.sub(r"\W", "", combined)) < self.settings.min_text_length:
            return Interpretation(text=combined, sources=sources)

        cached = self._cache.get((describing, supporting))
        if cached is not None:
            # Sources vary per row while the reading does not; copy and relabel.
            return Interpretation(**{**cached.__dict__, "sources": sources})

        result = self._interpret_uncached(describing, supporting)
        result.sources = sources
        self._cache[(describing, supporting)] = result
        return result

    def _interpret_uncached(self, describing: str, supporting: str) -> Interpretation:
        combined = " | ".join(part for part in (describing, supporting) if part)
        result = Interpretation(text=combined)

        # Values are drawn from everything the row carries: a lead time or a
        # quotation number is just as real in the buyer's note as in the line
        # description, and often only appears there.
        self._extract_values(combined, lookup_key(combined), result)

        # The description is built from the descriptive fields alone, and from
        # the prose within them. Codes, references and addresses have already
        # been captured in their own columns, and leaving them in produces a
        # description like "leasing contract ham".
        subject = describing or supporting
        language, confidence = self._detect_language(lookup_key(subject))
        result.language, result.language_confidence = language, confidence

        prose = self._prose(subject)
        english, resolved, total = self._render_english(
            lookup_key(prose), self._proper_nouns(prose) if language == "en" else set())
        result.resolved_share = resolved / total if total else 0.0
        result.token_count = total
        result.description = sentence_case(english)
        result.method = "vocabulary" if resolved else ("passthrough" if english else "none")

        # Type and intent are judged on everything the row carries, since a note
        # saying "to stock" or "urgent" bears on both. Keywords come from the
        # rendered description, so that the indexed terms are the English ones.
        folded_all = lookup_key(combined)
        result.item_or_service = self._classify_type(folded_all, english)
        result.intent = self._classify_intent(folded_all, english)
        result.keywords = self._keywords(english)
        result.confidence = self._confidence(result)
        return result

    # -- extraction ---------------------------------------------------------

    def _extract_values(self, text: str, folded: str, result: Interpretation) -> None:
        """Pull the values that have an unambiguous written form."""
        emails = sorted({match.group(0).lower() for match in _EMAIL.finditer(text)})
        result.emails = "; ".join(emails)
        result.domains = "; ".join(sorted({address.split("@")[-1] for address in emails}))

        # Addresses and links are removed before the remaining scans so that a
        # domain or a query string cannot be read as a code or a date.
        scrubbed = _URL.sub(" ", _EMAIL.sub(" ", text))

        quantities = _QUANTITY.findall(scrubbed)
        if quantities:
            amount, unit = quantities[0]
            result.quantity = format_amount(parse_amount(amount))
            result.unit = _UNIT_CANON.get(unit.lower(), unit.lower())

        money = self._extract_money(scrubbed)
        if money:
            result.amount, result.currency = money

        dimensions = sorted({match.group(0).replace(" ", "")
                             for match in _DIMENSION.finditer(scrubbed)})
        result.dimensions = "; ".join(dimensions[:5])

        result.dates = "; ".join(self._extract_dates(scrubbed)[:6])
        result.references = "; ".join(self._extract_references(scrubbed)[:6])
        result.model_codes = "; ".join(self._extract_model_codes(scrubbed)[:6])
        result.percentages = "; ".join(
            sorted({match.group(0).replace(" ", "") for match in _PERCENT.finditer(scrubbed)})[:4])

        lead_days = self._extract_lead_time(folded)
        result.lead_time_days = str(lead_days) if lead_days else ""

        result.mentions_tax = "Yes" if self._mentions(folded, _TAX_MARKERS) else ""
        result.mentions_attachment = "Yes" if self._mentions(folded, _ATTACHMENT_MARKERS) else ""
        result.mentions_stock = "Yes" if self._mentions(folded, _STOCK_MARKERS) else ""
        result.mentions_urgency = "Yes" if self._mentions(folded, _URGENCY_MARKERS) else ""

    @staticmethod
    def _mentions(folded: str, markers: Sequence[str]) -> bool:
        tokens = set(tokenise(folded))
        return any(marker in tokens or (" " in marker and marker in folded)
                   for marker in markers)

    @staticmethod
    def _extract_money(text: str) -> Optional[Tuple[str, str]]:
        """Return the first well-formed money value as (amount, currency)."""
        symbols = {"€": "EUR", "$": "USD", "£": "GBP"}
        for match in _MONEY.finditer(text):
            raw = match.group("after") or match.group("before") or ""
            token = match.group("symbol") or match.group("word") or ""
            amount = parse_amount(raw)
            if amount is None:
                continue
            currency = symbols.get(token, token.upper())
            return format_amount(amount), currency
        return None

    @staticmethod
    def _extract_dates(text: str) -> List[str]:
        """Collect dates, normalised to ISO where the year is known.

        Nordic internal notes routinely write a bare "11.3." meaning a day in the
        current working year. The day and month are kept as written in that case
        rather than guessed into a year that may be wrong.
        """
        found: List[str] = []
        for match in _ISO_DATE.finditer(text):
            year, month, day = match.groups()
            if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
                found.append(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
        for match in _EU_DATE.finditer(text):
            day, month, year = match.groups()
            if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
                found.append(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
        for match in _EU_DATE_SHORT.finditer(text):
            day, month = match.groups()
            if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
                found.append(f"--{int(month):02d}-{int(day):02d}")
        # Sorted for determinism, de-duplicated because a note often repeats one.
        return sorted(set(found))

    @staticmethod
    def _extract_references(text: str) -> List[str]:
        """Collect quotation, order and contract references.

        A reference introduced by a keyword is reported with that keyword, since
        knowing a number is a contract rather than a quotation is most of its
        value. The bare forms of those same numbers are then suppressed so the
        column does not carry each reference twice.
        """
        found: List[str] = []
        named: Set[str] = set()

        for match in _REFERENCE_KEYWORDS.finditer(text):
            candidate = match.group(2).strip(".,;:")
            if any(ch.isdigit() for ch in candidate) and len(candidate) >= 4:
                found.append(f"{match.group(1).lower()}:{candidate}")
                named.add(candidate)

        for match in _STRUCTURED_REFERENCE.finditer(text):
            candidate = match.group(0)
            if candidate in named or len(candidate) < 7:
                continue
            # A date written 12-05-2026, and an English date range written
            # 06th-12th, both satisfy the pattern and neither is a reference.
            if re.fullmatch(r"\d{1,4}(?:-\d{1,4}){1,2}", candidate):
                continue
            if _ORDINAL_RANGE.match(candidate):
                continue
            # At least one run of two or more letters, which excludes a number
            # broken up by hyphens.
            if not re.search(r"[A-Za-z]{2,}", candidate):
                continue
            found.append(candidate)
        return sorted(set(found))

    @staticmethod
    def _extract_model_codes(text: str) -> List[str]:
        """Collect part and model codes, with measurements set aside first.

        Measurements and dimensions are removed before the scan because both
        satisfy the pattern of a part number and neither is one; they are
        already reported in their own columns.
        """
        stripped = _DIMENSION.sub(" ", _MEASUREMENT_LIKE.sub(" ", text))
        found = set()
        for match in _MODEL_CODE.finditer(stripped):
            candidate = match.group(0).strip("./-")
            if len(candidate) < 4 or _ORDINAL_RANGE.match(candidate):
                continue
            digits = sum(ch.isdigit() for ch in candidate)
            letters = sum(ch.isalpha() for ch in candidate)
            # Both a letter and a digit are required, which excludes bare
            # quantities and bare words alike.
            if digits and letters:
                found.add(candidate)
        return sorted(found)

    @staticmethod
    def _extract_lead_time(folded: str) -> int:
        """Longest stated lead time in days, or zero when none is stated."""
        best = 0
        for match in _LEAD_TIME.finditer(folded):
            count = int(match.group(1))
            multiplier = _LEAD_TIME_UNITS.get(match.group(2).lower(), 0)
            best = max(best, count * multiplier)
        return best

    # -- language and rendering --------------------------------------------

    def _detect_language(self, folded: str) -> Tuple[str, float]:
        """Identify the language from vocabulary and stop-word hits.

        Scoring rather than a hard rule, because procurement free text mixes
        languages inside a single field far more often than prose does.
        """
        tokens = tokenise(folded)
        if not tokens:
            return "", 0.0

        scores: Dict[str, float] = defaultdict(float)
        for language, stopwords in _DISCRIMINATING_STOPWORDS.items():
            scores[language] += sum(1.5 for token in tokens if token in stopwords)
        for language, entries in self.discriminating_terms.items():
            scores[language] += sum(1.0 for token in tokens if token in entries)
        for key, _, language in self.lexicon.ordered_phrases:
            if key in folded:
                scores[language] += 2.0

        # English words carry less weight than a foreign term, because several
        # of them are also spelled that way in the other three languages.
        scores["en"] += sum(0.75 for token in tokens
                            if token in self.english_words and token not in _ALL_STOPWORDS)

        # Nothing voted. The honest answer is that the language is unknown, not
        # that it is English: the text handed in here has already been folded,
        # so a Swedish word is as plainly ASCII as an English one and an
        # alphabet test would call "Tillstandsansokan" English. Saying nothing
        # keeps the reading's confidence low, which is what routes the line to
        # the model tier where it belongs.
        if not scores or max(scores.values()) == 0:
            return "", 0.0

        best = max(scores, key=lambda language: scores[language])
        total = sum(scores.values()) or 1.0
        return best, min(1.0, scores[best] / total)

    @staticmethod
    def _prose(text: str) -> str:
        """Remove the parts of the text that are identifiers rather than words."""
        cleaned = _URL.sub(" ", _EMAIL.sub(" ", text))
        cleaned = _DIMENSION.sub(" ", cleaned)
        cleaned = _STRUCTURED_REFERENCE.sub(" ", cleaned)
        cleaned = _MODEL_CODE.sub(" ", cleaned)
        return cleaned

    @staticmethod
    def _proper_nouns(prose: str) -> Set[str]:
        """Capitalised words other than the first, taken to be names.

        Only consulted for text already identified as English, where an unknown
        capitalised word is a place, a brand or a person rather than a word that
        failed to translate. Without this, "Car Hire Charges for Teesside" is
        scored as one word short of understood and sent to the model to be told
        that Teesside is a place.
        """
        names: Set[str] = set()
        for position, match in enumerate(_TOKEN.finditer(prose)):
            word = match.group(0)
            if position and word[:1].isupper():
                names.add(lookup_key(word))
        return names

    def _render_english(self, folded: str, names: Set[str]) -> Tuple[str, int, int]:
        """Render the text in English. Returns (english, resolved, total)."""
        working = folded
        rendered: List[str] = []

        # Phrases first, longest first, so a multi-word entry is not broken up
        # by the single-word pass that follows.
        resolved_phrase_tokens = 0
        for key, value, _ in self.lexicon.ordered_phrases:
            if key and key in working:
                working = working.replace(key, " \u0000 ")
                rendered.append(value)
                resolved_phrase_tokens += len(key.split())

        resolved, total = resolved_phrase_tokens, resolved_phrase_tokens
        for token in tokenise(working):
            if token == "\u0000":
                continue
            if token in self.lexicon.noise_terms:
                continue
            if token.isdigit():
                continue
            total += 1

            english, _ = self.lexicon.term(token)
            if english:
                rendered.append(english)
                resolved += 1
                continue

            unit = self.lexicon.unit_terms.get(token)
            if unit:
                rendered.append(unit)
                resolved += 1
                continue

            decomposed = self._decompose(token)
            if decomposed:
                rendered.append(decomposed)
                resolved += 1
                continue

            # A word that is already English needs no translation, so it counts
            # as resolved. Judging otherwise would score a perfectly clear
            # English line as badly understood and send it to the model tier for
            # no reason, which is most of what the model tier costs. Membership
            # of the word list is required: inferring it from the detected
            # language would let a whole untranslated Finnish line through as
            # understood on the strength of one ambiguous token.
            if token in self.english_words or token in names:
                if len(token) > 2 or token in _STOPWORDS["en"]:
                    rendered.append(token)
                resolved += 1
                continue

            # Unknown. Kept rather than dropped so nothing is silently lost,
            # but not counted as understood.
            if len(token) > 2 and not any(ch.isdigit() for ch in token):
                rendered.append(token)

        english = " ".join(dict.fromkeys(word for word in " ".join(rendered).split() if word))
        return english, resolved, total

    def _decompose(self, token: str) -> str:
        """Split a Nordic compound into known parts.

        'asbestipurkutyo' is one token to a tokeniser and three ideas to a
        reader; splitting it is what lets the vocabulary cover a language that
        forms new words by joining old ones.
        """
        for language in ("fi", "sv"):
            parts = self.lexicon.compound_parts.get(language) or {}
            if not parts:
                continue
            pieces = self._split_compound(token, parts)
            if pieces:
                return " ".join(piece for piece in pieces if piece)
        return ""

    @staticmethod
    def _split_compound(token: str, parts: Dict[str, str], depth: int = 0) -> List[str]:
        """Greedy longest-prefix decomposition, at most three pieces deep."""
        if not token:
            return []
        if depth >= 3:
            return []
        for length in range(len(token), 2, -1):
            head = token[:length]
            if head not in parts:
                continue
            tail = token[length:]
            if not tail:
                return [parts[head]]
            rest = TextInterpreter._split_compound(tail, parts, depth + 1)
            if rest:
                return [parts[head]] + rest
        return []

    # -- classification -----------------------------------------------------

    def _classify_type(self, folded: str, english: str) -> str:
        """Decide whether the line buys a thing or an activity."""
        tokens = set(tokenise(english)) | set(tokenise(folded))
        service = len(tokens & self.lexicon.service_markers)
        material = len(tokens & self.lexicon.material_markers)
        if service > material:
            return "Service"
        if material > service:
            return "Material"
        return "Unclassified" if not tokens else ""

    @staticmethod
    def _classify_intent(folded: str, english: str) -> str:
        """Place the purchase in a recognisable class, or leave it unnamed."""
        haystack = f"{folded} {lookup_key(english)}"
        tokens = set(tokenise(haystack))
        for label, markers in _INTENT_RULES:
            for marker in markers:
                if " " in marker:
                    if marker in haystack:
                        return label
                elif marker in tokens:
                    return label
        return ""

    def _keywords(self, english: str) -> str:
        """The content words worth indexing, in a stable order."""
        words = [word for word in tokenise(lookup_key(english))
                 if len(word) > 2 and word not in _ALL_STOPWORDS
                 and word not in self.lexicon.noise_terms
                 and not word.isdigit()]
        return "; ".join(sorted(dict.fromkeys(words))[:12])

    @staticmethod
    def _confidence(result: Interpretation) -> float:
        """How much of the row's text was actually understood.

        Built from the share of content words resolved, lifted by each
        structured value that was extracted cleanly, because a row where the
        quantity, the amount and the reference all came out is well understood
        even when the prose around them did not resolve.
        """
        score = result.resolved_share * 0.7
        signals = sum(bool(value) for value in (
            result.quantity, result.amount, result.dimensions, result.references,
            result.model_codes, result.dates, result.emails, result.lead_time_days))
        score += min(0.25, signals * 0.05)
        if result.language:
            score += 0.05 * result.language_confidence
        return round(min(1.0, score), 4)


# ===========================================================================
# Language model client
# ===========================================================================

@dataclass
class TokenUsage:
    """Running total of language-model consumption."""

    requests: int = 0
    failed_requests: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    # Per-million-token prices, copied from the resolved model configuration so
    # that the running total can be valued without reaching back into it.
    input_cost_per_mtok: float = INPUT_COST_PER_MTOK
    output_cost_per_mtok: float = OUTPUT_COST_PER_MTOK

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def input_cost(self) -> float:
        """Dollar value of the input tokens consumed so far.

        Cached input is charged here at the full rate even though the provider
        discounts it heavily, so the figure leans high. That is the right
        direction for a number whose job is to stop a run becoming expensive.
        """
        return self.input_tokens / 1_000_000.0 * self.input_cost_per_mtok

    @property
    def output_cost(self) -> float:
        """Dollar value of the output tokens, reasoning tokens included."""
        return self.output_tokens / 1_000_000.0 * self.output_cost_per_mtok

    @property
    def estimated_cost(self) -> float:
        return self.input_cost + self.output_cost

    def record(self, usage: Dict[str, Any]) -> None:
        self.requests += 1
        if not usage:
            return
        self.input_tokens += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        self.cached_input_tokens += int(prompt_details.get("cached_tokens") or 0)
        completion_details = (usage.get("completion_tokens_details")
                              or usage.get("output_tokens_details") or {})
        self.reasoning_tokens += int(completion_details.get("reasoning_tokens") or 0)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requests": self.requests, "failed_requests": self.failed_requests,
            "cache_hits": self.cache_hits, "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens, "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "input_cost_per_mtok": self.input_cost_per_mtok,
            "output_cost_per_mtok": self.output_cost_per_mtok,
            "input_cost_usd": round(self.input_cost, 4),
            "output_cost_usd": round(self.output_cost, 4),
            "estimated_cost_usd": round(self.estimated_cost, 4),
        }


class SpendGuard:
    """Holds language-model spend to what the operator authorised.

    The operator names a figure when the model tier is switched on. After every
    billed response the running estimate is compared against it, and once the
    figure is reached the run pauses and asks whether to carry on. Agreeing
    raises the ceiling by the same amount again, so a limit of $25 becomes $50,
    then $75, and each further step needs its own answer. Declining switches the
    model off for the rest of the run: nothing is lost, because the local reader
    already produced an answer for every row, and the work done so far is kept.
    """

    def __init__(self, usage: TokenUsage, config: ModelConfig, interactive: bool) -> None:
        self.usage = usage
        self.config = config
        self.interactive = interactive
        self.step = max(0.0, config.spend_limit)
        self.limit = self.step
        self.extensions = 0
        self.declined = False

    @property
    def active(self) -> bool:
        """True when a limit is in force and has not been waived."""
        return bool(self.step) and not self.declined

    def as_dict(self) -> Dict[str, Any]:
        return {
            "spend_limit_usd": round(self.step, 4),
            "spend_limit_final_usd": round(self.limit, 4),
            "spend_limit_extensions": self.extensions,
            "spend_limit_stopped": self.declined,
        }

    def review(self) -> None:
        """Check the running total after a billed response and act on it."""
        if not self.active or not self.config.enabled:
            return

        # A loop rather than a single test: one costly response can overshoot
        # by more than a whole step, and each step still needs its own answer.
        while self.usage.estimated_cost >= self.limit:
            if not self.interactive:
                LOGGER.warning(
                    "Estimated language-model spend is $%.2f, at or above the $%.2f limit. "
                    "Continuing without the model; raise --llm-spend-limit to allow more.",
                    self.usage.estimated_cost, self.limit)
                self._stop()
                return
            if not self._ask():
                self._stop()
                return
            self.limit += self.step
            self.extensions += 1
            print(f"  Continuing. The limit is now ${self.limit:,.2f}.\n", flush=True)

    def _ask(self) -> bool:
        """Report the position and ask whether to keep using the model."""
        print(flush=True)
        print("=" * 79)
        print("  Language-model spend alert")
        print("=" * 79)
        print(f"  Estimated spend      : ${self.usage.estimated_cost:,.2f}")
        print(f"  Authorised so far    : ${self.limit:,.2f}")
        print(f"  Input tokens         : {self.usage.input_tokens:,} "
              f"at ${self.usage.input_cost_per_mtok:,.2f}/M = ${self.usage.input_cost:,.2f}")
        print(f"  Output tokens        : {self.usage.output_tokens:,} "
              f"at ${self.usage.output_cost_per_mtok:,.2f}/M = ${self.usage.output_cost:,.2f}")
        print(f"  Requests sent        : {self.usage.requests:,}")
        print()
        print(f"  Answering yes raises the limit to ${self.limit + self.step:,.2f}.")
        print("  Answering no finishes the run on the local reader alone.")
        try:
            answer = input("  Continue using the language model? [y/N]: ").strip().lower()
        except EOFError:
            # Input is piped and nobody is watching; the cautious reading of
            # silence is that no further spend was authorised.
            print()
            return False
        return answer[:1] == "y"

    def _stop(self) -> None:
        """Switch the model off for the remainder of the run."""
        self.declined = True
        self.config.enabled = False
        LOGGER.info(
            "Language model disabled after an estimated $%.2f of spend. "
            "The run continues on the local reader; cached answers are still used.",
            self.usage.estimated_cost)


class LanguageModelClient:
    """Minimal OpenAI-compatible chat client with disk caching."""

    def __init__(self, config: ModelConfig, cache_path: Path,
                 interactive: bool = False) -> None:
        self.config = config
        self.usage = TokenUsage(
            input_cost_per_mtok=config.input_cost_per_mtok,
            output_cost_per_mtok=config.output_cost_per_mtok,
        )
        self.guard = SpendGuard(self.usage, config, interactive)
        self.cache_path = cache_path
        self._cache: Dict[str, str] = self._load_cache()
        self._cache_dirty = False
        self._omit_temperature = False

    def _load_cache(self) -> Dict[str, str]:
        if not self.cache_path.is_file():
            return {}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Model cache %s unreadable; starting empty.", self.cache_path)
            return {}
        entries = payload.get("entries", {})
        LOGGER.info("Model cache loaded with %d entries.", len(entries))
        return entries

    def save_cache(self) -> None:
        if not self._cache_dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "agent": AGENT_NAME,
            "model": self.config.model,
            "backend": self.config.backend,
            "entries": dict(sorted(self._cache.items())),
        }
        self.cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        self._cache_dirty = False

    def cache_key(self, task: str, payload: str) -> str:
        """Content address for one unit of work.

        The model name is part of the key so that switching backend does not
        silently reuse answers produced by a different model.
        """
        return stable_hash(task, self.config.model, payload)

    def cached(self, key: str) -> Optional[str]:
        value = self._cache.get(key)
        if value is not None:
            self.usage.cache_hits += 1
        return value

    def store(self, key: str, value: str) -> None:
        self._cache[key] = value
        self._cache_dirty = True

    def _post(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Issue one request, returning the decoded response or None."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            # The shared service authenticates by api-key rather than by bearer
            # token. Sending both is accepted by each and saves branching.
            "api-key": self.config.api_key,
        }
        payload = json.dumps(body).encode("utf-8")

        try:
            if _requests is not None:
                response = _requests.post(self.config.endpoint, headers=headers,
                                          data=payload, timeout=self.config.timeout)
                status, text = response.status_code, response.text
            else:
                import urllib.error
                import urllib.request
                request = urllib.request.Request(self.config.endpoint, data=payload,
                                                 headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(request, timeout=self.config.timeout) as handle:
                        status, text = handle.status, handle.read().decode("utf-8", "replace")
                except urllib.error.HTTPError as error:
                    status, text = error.code, error.read().decode("utf-8", "replace")
        except Exception as error:
            LOGGER.warning("Language-model request failed: %s", error)
            self.usage.failed_requests += 1
            return None

        if status != 200:
            summary = text[:300].replace("\n", " ")
            if status == 400 and "temperature" in summary.lower():
                self._omit_temperature = True
                return self._post({k: v for k, v in body.items() if k != "temperature"})
            LOGGER.warning("Language-model request returned HTTP %s: %s", status, summary)
            self.usage.failed_requests += 1
            return None

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            LOGGER.warning("Language-model response was not valid JSON.")
            self.usage.failed_requests += 1
            return None

    def complete_json(self, system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
        """Request a JSON object; None on any failure."""
        if not self.config.enabled:
            return None
        if self.config.max_requests and self.usage.requests >= self.config.max_requests:
            LOGGER.warning("Language-model request cap (%d) reached.", self.config.max_requests)
            return None

        body: Dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_prompt}],
            "response_format": {"type": "json_object"},
        }
        if not self._omit_temperature:
            body["temperature"] = 0

        response = self._post(body)
        if response is None:
            return None
        self.usage.record(response.get("usage") or {})
        # Checked after every response, so a limit reached mid-batch takes
        # effect on the next call rather than at the end of the phase.
        self.guard.review()
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        return extract_json_object(content or "")


def extract_json_object(content: str) -> Optional[Dict[str, Any]]:
    """Recover a JSON object from a model reply.

    Even with a JSON response format enforced, replies occasionally arrive
    wrapped in a code fence or with a sentence in front of them, so the object
    is located by brace balance rather than assumed to be the whole string.
    """
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content).strip()
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = content.find("{")
    if start < 0:
        return None
    depth, in_string, escape = 0, False, False
    for index in range(start, len(content)):
        char = content[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(content[start:index + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


_MODEL_SYSTEM_PROMPT = (
    "You read short procurement free-text lines written in Finnish, Swedish, "
    "Polish, German or English and describe what was bought.\n"
    "Return JSON only, in the form {\"items\": [{\"id\": \"...\", "
    "\"description\": \"...\", \"type\": \"Material|Service\", "
    "\"intent\": \"...\"}]}.\n"
    "Rules:\n"
    "- description: plain English, at most twelve words, naming the goods or "
    "the activity. No invoice numbers, no supplier names, no prices.\n"
    "- type: Material for a physical item, Service for an activity.\n"
    "- intent: a short category such as Maintenance and repair, Spare parts and "
    "materials, Consulting and advisory, Travel and accommodation, Freight and "
    "delivery, Rental and leasing, Training and certification, Licences and "
    "subscriptions, Fees and charges. Use an empty string when unsure.\n"
    "- Return one entry for every id supplied, in the same order. Never invent "
    "detail that is not present in the text."
)


class ModelReader:
    """Asks the model to read the free text the local reader could not.

    Works on distinct strings rather than rows, in batches, with every answer
    cached on a hash of the text. A second run over the same data therefore
    costs nothing, and the answers cannot drift between runs.
    """

    def __init__(self, client: LanguageModelClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.resolved = 0

    def resolve(self, texts: Sequence[str]) -> Dict[str, Dict[str, str]]:
        """Read a list of distinct strings, returning text -> fields."""
        answers: Dict[str, Dict[str, str]] = {}
        pending: List[str] = []

        for text in texts:
            cached = self.client.cached(self.client.cache_key("read", text))
            if cached is not None:
                try:
                    answers[text] = json.loads(cached)
                except json.JSONDecodeError:
                    pending.append(text)
            else:
                pending.append(text)

        if not pending:
            return answers

        batch_size = max(1, self.settings.model.batch_size)
        # Sorted so that the batching, and therefore the cache keys of any
        # future run over the same data, are identical every time.
        pending.sort()
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            if not self.client.config.enabled:
                break
            resolved = self._ask(batch)
            for text, fields in resolved.items():
                answers[text] = fields
                self.client.store(self.client.cache_key("read", text),
                                  json.dumps(fields, ensure_ascii=False, sort_keys=True))
            self.resolved += len(resolved)
            LOGGER.info("Model read %d of %d unresolved descriptions.",
                        min(start + batch_size, len(pending)), len(pending))
        return answers

    def _ask(self, batch: Sequence[str]) -> Dict[str, Dict[str, str]]:
        identifiers = {stable_hash(text): text for text in batch}
        payload = {"items": [{"id": key, "text": value}
                             for key, value in sorted(identifiers.items())]}
        response = self.client.complete_json(
            _MODEL_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False))
        if not response:
            return {}

        results: Dict[str, Dict[str, str]] = {}
        for item in response.get("items") or []:
            if not isinstance(item, dict):
                continue
            text = identifiers.get(normalise_text(item.get("id")))
            if text is None:
                continue
            description = normalise_text(item.get("description"))
            if not description:
                continue
            item_type = normalise_text(item.get("type")).title()
            results[text] = {
                "description": sentence_case(description),
                "type": item_type if item_type in {"Material", "Service"} else "",
                "intent": normalise_text(item.get("intent")),
            }
        return results


# ===========================================================================
# Stage 1: invoice lines onto Sievo
# ===========================================================================

@dataclass
class InvoiceLine:
    """One invoice row, reduced to the fields the join and the output need."""

    values: Dict[str, str]
    is_line: bool
    row_total_excl: Optional[float]
    row_total_incl: Optional[float]
    vat_amount: Optional[float]


class InvoiceIndex:
    """Every invoice line, indexed by each key the join is willing to try.

    Duplicates are dropped on the way in. An invoice cannot legitimately carry
    the same line number twice, so a repeated pair is an artefact of the export
    and summing it would overstate the invoice.
    """

    # Ordered most specific first. Each entry is (name, is_line_level).
    STRATEGIES: Tuple[Tuple[str, bool], ...] = (
        ("invoice-number + line", True),
        ("document-number + line", True),
        ("document-id + line", True),
        ("invoice-number", False),
        ("document-number", False),
        ("document-id", False),
    )

    def __init__(self, tables: Sequence[Table]) -> None:
        self.lines: List[InvoiceLine] = []
        self.columns: Dict[str, str] = {}
        self.duplicates = 0
        self.summary_rows = 0

        self._by_line: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        self._by_document: Dict[str, List[int]] = defaultdict(list)
        self._document_totals: Dict[str, Tuple[int, float]] = {}

        seen: Set[Tuple[str, str, str]] = set()
        for table in tables:
            mapping = ColumnMap(table.headers, INVOICE_FIELDS)
            self.columns.update(mapping.resolved)
            for _, record in table.iter_records():
                self._ingest(mapping, record, seen)

        self._summarise()

    def _ingest(self, mapping: ColumnMap, record: Dict[str, str],
                seen: Set[Tuple[str, str, str]]) -> None:
        values = {field_name: mapping.get(record, field_name) for field_name in INVOICE_FIELDS}
        key = values.get("invoice_key") or values.get("invoice_id")
        if is_blank(key) and is_blank(values.get("xml_file_name")):
            return

        row = line_key(values.get("row_number"))
        signature = (compact_key(key), row, lookup_key(values.get("article_name", "")))
        if signature in seen:
            self.duplicates += 1
            return
        seen.add(signature)

        # A row without a line number is a header or a total, not a purchase
        # line; including it in a sum would double-count the invoice.
        is_line = bool(row) and row.isdigit()
        if not is_line:
            self.summary_rows += 1

        line = InvoiceLine(
            values=values,
            is_line=is_line,
            row_total_excl=parse_amount(values.get("row_total_excl_vat")),
            row_total_incl=parse_amount(values.get("row_total_incl_vat")),
            vat_amount=parse_amount(values.get("vat_amount")),
        )
        index = len(self.lines)
        self.lines.append(line)

        for document in self._document_keys(values):
            self._by_document[document].append(index)
            if row:
                self._by_line[(document, row)].append(index)

    @staticmethod
    def _document_keys(values: Dict[str, str]) -> Set[str]:
        """Every key under which this invoice line can be found."""
        keys: Set[str] = set()
        for field_name in ("invoice_key", "invoice_id"):
            value = values.get(field_name, "")
            if not is_blank(value):
                keys.add(compact_key(value))
        document = document_id_key(values.get("xml_file_name", ""))
        if document:
            keys.add(document)
        return keys

    def _summarise(self) -> None:
        """Pre-compute the line count and total for each invoice."""
        for document, indices in self._by_document.items():
            lines = [self.lines[index] for index in indices if self.lines[index].is_line]
            total = sum(line.row_total_excl or 0.0 for line in lines)
            self._document_totals[document] = (len(lines), total)

    @property
    def empty(self) -> bool:
        return not self.lines

    def lookup(self, sievo: Dict[str, str]) -> Tuple[str, List[InvoiceLine], str]:
        """Find the invoice lines for one Sievo row.

        Returns (strategy, lines, document_key). The ladder is walked in order
        and the first strategy that produces anything wins, so a line-level
        match is never displaced by a looser one.
        """
        invoice_number = "" if is_blank(sievo.get("invoice_number")) else compact_key(sievo["invoice_number"])
        document_number = "" if is_blank(sievo.get("document_number")) else compact_key(sievo["document_number"])
        document_id = document_id_key(sievo.get("document_identifier") or "") \
            or document_id_key(sievo.get("invoice_link") or "")
        row = line_key(sievo.get("document_line"))

        candidates = (
            ("invoice-number + line", invoice_number, True),
            ("document-number + line", document_number, True),
            ("document-id + line", document_id, True),
            ("invoice-number", invoice_number, False),
            ("document-number", document_number, False),
            ("document-id", document_id, False),
        )

        for name, key, line_level in candidates:
            if not key:
                continue
            if line_level:
                if not row:
                    continue
                indices = self._by_line.get((key, row))
            else:
                indices = self._by_document.get(key)
            if indices:
                return name, [self.lines[index] for index in indices], key
        return "", [], ""

    def document_summary(self, document: str) -> Tuple[int, float]:
        return self._document_totals.get(document, (0, 0.0))


# The invoice block appended to every row in stage 1.
INVOICE_OUTPUT_FIELDS: Tuple[str, ...] = (
    "invoice_key", "invoice_id", "row_number", "xml_file_name", "article_id",
    "article_name", "free_text", "quantity_charged", "quantity_delivered",
    "unit_price_excl_vat", "unit_price_net", "row_total_excl_vat",
    "row_total_incl_vat", "vat_amount", "vat_rate",
)

INVOICE_COLUMNS: List[str] = (
    ["Invoice_Match_Level", "Invoice_Match_Strategy", "Invoice_Match_Count"]
    + [_column("Invoice", name) for name in INVOICE_OUTPUT_FIELDS]
    + ["Invoice_Lines_On_Document", "Invoice_Document_Total_Excl_Vat",
       "Invoice_Matched_Total_Excl_Vat", "Invoice_Article_Names_All"]
)


def join_invoice(sievo: Dict[str, str], index: Optional[InvoiceIndex]) -> Dict[str, str]:
    """Produce the invoice block for one Sievo row.

    One row in, one block out. Where several invoice lines answer to the
    transaction, scalar columns are filled only where every candidate agrees,
    and the rest of the evidence is summarised.
    """
    block = {column: "" for column in INVOICE_COLUMNS}
    block["Invoice_Match_Level"] = "none"
    if index is None or index.empty:
        return block

    strategy, lines, document = index.lookup(sievo)
    if not lines:
        return block

    line_rows = [line for line in lines if line.is_line] or lines
    block["Invoice_Match_Strategy"] = strategy
    block["Invoice_Match_Count"] = str(len(line_rows))
    block["Invoice_Match_Level"] = "line" if strategy.endswith("+ line") else "header"

    if len(line_rows) == 1:
        for field_name in INVOICE_OUTPUT_FIELDS:
            block[_column("Invoice", field_name)] = line_rows[0].values.get(field_name, "")
    else:
        # Fill only where the candidates agree; a disagreement is left blank
        # rather than resolved arbitrarily.
        for field_name in INVOICE_OUTPUT_FIELDS:
            distinct = {line.values.get(field_name, "") for line in line_rows}
            distinct.discard("")
            if len(distinct) == 1:
                block[_column("Invoice", field_name)] = distinct.pop()

    names = sorted({line.values.get("article_name", "") for line in line_rows} - {""})
    block["Invoice_Article_Names_All"] = "; ".join(names[:12])
    block["Invoice_Matched_Total_Excl_Vat"] = format_amount(
        sum(line.row_total_excl or 0.0 for line in line_rows) or None)

    count, total = index.document_summary(document)
    block["Invoice_Lines_On_Document"] = str(count) if count else ""
    block["Invoice_Document_Total_Excl_Vat"] = format_amount(total or None)
    return block


# ===========================================================================
# Stage 2: purchase order lines onto the widened table
# ===========================================================================

class PurchaseOrderIndex:
    """Maximo and Basware lines, indexed by PO number and PO line number."""

    def __init__(self, system: str, tables: Sequence[Table],
                 synonyms: Dict[str, Sequence[str]]) -> None:
        self.system = system
        self.native_headers: List[str] = []
        self.records: List[Dict[str, str]] = []
        self.mapped: List[Dict[str, str]] = []

        self._by_line: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        self._by_order: Dict[str, List[int]] = defaultdict(list)

        seen_headers: Set[str] = set()
        for table in tables:
            mapping = ColumnMap(table.headers, synonyms)
            for header in table.headers:
                if header and header not in seen_headers:
                    seen_headers.add(header)
                    self.native_headers.append(header)

            for _, record in table.iter_records():
                values = {name: mapping.get(record, name) for name in synonyms}
                order = values.get("po_number", "")
                if is_blank(order):
                    continue
                order_key = compact_key(order)
                index = len(self.records)
                self.records.append({header: normalise_text(record.get(header, ""))
                                     for header in table.headers})
                self.mapped.append(values)
                self._by_order[order_key].append(index)
                row = line_key(values.get("po_line_number"))
                if row:
                    self._by_line[(order_key, row)].append(index)

    @property
    def empty(self) -> bool:
        return not self.records

    def lookup(self, order: str, row: str) -> Tuple[str, List[int]]:
        """Find the PO lines for one transaction.

        A missing line number is common in the transaction extract. When the
        order has exactly one line the match is still unambiguous and is taken;
        when it has several, only the header-level fields can be trusted.
        """
        order_key = compact_key(order)
        if not order_key or order_key not in self._by_order:
            return "", []

        if row:
            indices = self._by_line.get((order_key, row))
            if indices:
                return "po-number + line", indices

        indices = self._by_order[order_key]
        if len(indices) == 1:
            return "po-number, single-line order", indices
        return "po-number", indices

    def order_line_count(self, order: str) -> int:
        return len(self._by_order.get(compact_key(order), ()))


PO_CORE_COLUMNS: List[str] = (
    ["PO_Match_Level", "PO_Match_Strategy", "PO_Match_Count", "PO_System",
     "PO_Lines_On_Order"]
    + [_column("PO", name) for name in PO_OUTPUT_FIELDS]
)


class PurchaseOrderJoiner:
    """Attaches the PO line to a transaction, from whichever system holds it."""

    def __init__(self, indexes: Dict[str, PurchaseOrderIndex], native: bool) -> None:
        self.indexes = {name: index for name, index in indexes.items() if not index.empty}
        self.native = native
        self.native_columns: List[str] = []
        self._native_index: Dict[str, List[str]] = {}
        if native:
            for name, index in sorted(self.indexes.items()):
                prefix = name.capitalize()
                columns = [f"{prefix}_{header}" for header in index.native_headers]
                self._native_index[name] = columns
                self.native_columns.extend(columns)

    @property
    def columns(self) -> List[str]:
        return PO_CORE_COLUMNS + self.native_columns

    def join(self, sievo: Dict[str, str]) -> Dict[str, str]:
        block = {column: "" for column in self.columns}
        block["PO_Match_Level"] = "none"
        if not self.indexes:
            return block

        order = sievo.get("po_number", "")
        if is_blank(order):
            return block
        row = line_key(sievo.get("po_line"))

        best = self._select(order, row, sievo.get("data_source", ""))
        if best is None:
            return block

        system, strategy, indices = best
        index = self.indexes[system]
        block["PO_System"] = system.capitalize()
        block["PO_Match_Strategy"] = strategy
        block["PO_Match_Count"] = str(len(indices))
        block["PO_Match_Level"] = "line" if len(indices) == 1 else "header"
        block["PO_Lines_On_Order"] = str(index.order_line_count(order))

        if len(indices) == 1:
            values = index.mapped[indices[0]]
            for name in PO_OUTPUT_FIELDS:
                block[_column("PO", name)] = values.get(name, "")
        else:
            # Only unanimous values are trustworthy at header level.
            for name in PO_OUTPUT_FIELDS:
                distinct = {index.mapped[position].get(name, "") for position in indices}
                distinct.discard("")
                if len(distinct) == 1:
                    block[_column("PO", name)] = distinct.pop()

        if self.native:
            self._fill_native(block, system, index, indices)
        return block

    def _select(self, order: str, row: str,
                data_source: str) -> Optional[Tuple[str, str, List[int]]]:
        """Choose which PO system answers for this transaction.

        The transaction extract usually names its own source system, and that
        naming is trusted when it resolves. Otherwise both systems are probed
        and the more specific match wins; a tie is broken by system name so the
        result does not depend on dictionary order.
        """
        hint = lookup_key(data_source).replace("x.", "").strip()
        preferred = [name for name in self.indexes if name in hint]

        found: List[Tuple[int, str, str, List[int]]] = []
        for name in sorted(self.indexes):
            strategy, indices = self.indexes[name].lookup(order, row)
            if not indices:
                continue
            rank = 0 if strategy.startswith("po-number + line") else (
                1 if "single-line" in strategy else 2)
            if name in preferred:
                rank -= 1
            found.append((rank, name, strategy, indices))

        if not found:
            return None
        found.sort(key=lambda item: (item[0], item[1]))
        _, name, strategy, indices = found[0]
        return name, strategy, indices

    def _fill_native(self, block: Dict[str, str], system: str,
                     index: PurchaseOrderIndex, indices: List[int]) -> None:
        columns = self._native_index.get(system) or []
        headers = index.native_headers
        if len(indices) == 1:
            record = index.records[indices[0]]
            for column, header in zip(columns, headers):
                block[column] = record.get(header, "")
            return
        for column, header in zip(columns, headers):
            distinct = {index.records[position].get(header, "") for position in indices}
            distinct.discard("")
            if len(distinct) == 1:
                block[column] = distinct.pop()


# ===========================================================================
# Orchestration
# ===========================================================================

# The free-text fields read in stage 3, in the order they are consulted, each
# marked as describing the purchase or merely supporting it.
#
# Descriptive fields say what was bought and are the only ones allowed to shape
# the generated description. Supporting fields — the buyer's note to a
# colleague, the free-text column on the invoice line — are worth reading for
# the dates, references, quantities and lead times they carry, but folding them
# into the description turns "Bearing replacement" into "Bearing replacement
# order urgent to stock by link". They are scanned for values and then set
# aside.
TEXT_SOURCE_COLUMNS: Tuple[Tuple[str, str, bool], ...] = (
    ("Document line desc", "sievo.document-line", True),
    ("PO line desc", "sievo.po-line", True),
    ("Invoice_Article_Name", "invoice.article", True),
    ("Invoice_Article_Names_All", "invoice.articles", True),
    ("PO_Line_Description", "po.line", True),
    ("PO_Header_Description", "po.header", True),
    ("MaterialGroupName", "sievo.material-group", True),
    ("PO_Item_Code", "po.item", False),
    ("PO_Internal_Note", "po.note", False),
    ("Invoice_Free_Text", "invoice.free-text", False),
)


class Builder:
    """Runs the three stages and writes their outputs."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lexicon = Lexicon.load(settings.lexicon_path)
        self.interpreter = TextInterpreter(self.lexicon, settings)

        self.model: Optional[LanguageModelClient] = None
        if settings.model.enabled:
            self.model = LanguageModelClient(
                settings.model, settings.cache_dir / "max_model_cache.json",
                interactive=settings.interactive)

        self.run_id = ""
        self.sievo_tables: List[Table] = []
        self.invoice_index: Optional[InvoiceIndex] = None
        self.po_joiner: Optional[PurchaseOrderJoiner] = None
        self.sievo_headers: List[str] = []
        self.sievo_map: Optional[ColumnMap] = None
        self.outputs: List[str] = []
        self.statistics: Dict[str, Any] = {}
        self.diagnostics: Dict[str, Any] = {}

        # Purchase-order extracts that were read but cannot be joined. Held
        # separately because the joiner discards them, and the diagnostics file
        # is precisely where someone needs to see that a delivered file was
        # unusable.
        self.unusable_po_systems: List[str] = []

    # -- setup --------------------------------------------------------------

    def load(self) -> None:
        grouped = discover_tables(self.settings.source_dir)

        self.sievo_tables = grouped.get("sievo") or []
        if not self.sievo_tables:
            raise SystemExit(
                f"No transaction extract was found under {self.settings.source_dir}.\n"
                "Max builds outwards from the Sievo transaction table; without it there "
                "is nothing to widen. Expected a file carrying columns such as "
                "SourceRowId, DataSource, PO number and Spend in EUR.")

        # One header list covering every transaction table supplied, in the
        # order the columns were first seen.
        headers: List[str] = []
        for table in self.sievo_tables:
            for header in table.headers:
                if header and header not in headers:
                    headers.append(header)
        self.sievo_headers = headers
        self.sievo_map = ColumnMap(headers, SIEVO_FIELDS)

        invoice_tables = grouped.get("invoice") or []
        if invoice_tables:
            self.invoice_index = InvoiceIndex(invoice_tables)
            LOGGER.info("Invoice extract: %d rows (%d line rows, %d summary rows, "
                        "%d duplicates dropped).",
                        len(self.invoice_index.lines),
                        sum(1 for line in self.invoice_index.lines if line.is_line),
                        self.invoice_index.summary_rows, self.invoice_index.duplicates)
        else:
            LOGGER.warning("No invoice extract found; stage 1 columns will be empty.")

        indexes: Dict[str, PurchaseOrderIndex] = {}
        for name, synonyms in (("maximo", MAXIMO_FIELDS), ("basware", BASWARE_FIELDS)):
            tables = grouped.get(name) or []
            if not tables:
                continue
            index = PurchaseOrderIndex(name, tables, synonyms)
            if index.empty:
                self.unusable_po_systems.append(name)
                LOGGER.warning("%s extract carries no usable PO number; "
                               "it cannot take part in the join.", name.capitalize())
            else:
                LOGGER.info("%s extract: %d PO lines across %d orders.",
                            name.capitalize(), len(index.records), len(index._by_order))
            indexes[name] = index

        if not any(not index.empty for index in indexes.values()):
            LOGGER.warning("No purchase-order extract carries a usable PO number; "
                           "stage 2 columns will be empty.")
        self.po_joiner = PurchaseOrderJoiner(indexes, self.settings.native_po_columns)

        self.run_id = stable_hash(
            AGENT_VERSION, self.lexicon.version,
            *(table.label for table in self.sievo_tables))

    # -- stages 1 and 2 -----------------------------------------------------

    def build_wide(self) -> Tuple[Path, Path, List[str]]:
        """Stream the transactions, widening each row, writing stages 1 and 2.

        Both stages are produced in a single pass. Stage 1's file is the
        transaction plus the invoice block; stage 2's is that same row plus the
        purchase-order block, so writing them together costs one pass rather
        than two and cannot let the two files disagree.
        """
        results = self.settings.results_dir
        results.mkdir(parents=True, exist_ok=True)

        stage1_columns = ["Max_Row_Id"] + self.sievo_headers + INVOICE_COLUMNS
        stage2_columns = stage1_columns + self.po_joiner.columns

        stage1_csv = results / "max_stage1_sievo_invoice.csv"
        stage2_csv = results / "max_stage2_with_po.csv"
        stage1_jsonl = results / "max_stage1_sievo_invoice.jsonl"
        stage2_jsonl = results / "max_stage2_with_po.jsonl"

        counters: Counter = Counter()
        invoice_strategies: Counter = Counter()
        po_strategies: Counter = Counter()
        rows_in = 0

        handles = []
        try:
            stage1_handle = stage1_csv.open("w", encoding=CSV_ENCODING, newline="")
            stage2_handle = stage2_csv.open("w", encoding=CSV_ENCODING, newline="")
            handles += [stage1_handle, stage2_handle]
            stage1_writer = csv.DictWriter(stage1_handle, fieldnames=stage1_columns,
                                           extrasaction="ignore")
            stage2_writer = csv.DictWriter(stage2_handle, fieldnames=stage2_columns,
                                           extrasaction="ignore")
            stage1_writer.writeheader()
            stage2_writer.writeheader()

            stage1_lines = stage2_lines = None
            if self.settings.write_jsonl:
                stage1_lines = stage1_jsonl.open("w", encoding="utf-8")
                stage2_lines = stage2_jsonl.open("w", encoding="utf-8")
                handles += [stage1_lines, stage2_lines]

            for table in self.sievo_tables:
                for row_number, record in table.iter_records():
                    rows_in += 1
                    row = {header: normalise_text(record.get(header, ""))
                           for header in self.sievo_headers}
                    keys = {name: self.sievo_map.get(record, name) for name in SIEVO_FIELDS}

                    row_id = keys.get("row_id") or ""
                    row["Max_Row_Id"] = stable_hash(table.label, str(row_number), row_id)

                    invoice_block = join_invoice(keys, self.invoice_index)
                    counters[f"invoice_{invoice_block['Invoice_Match_Level']}"] += 1
                    if invoice_block["Invoice_Match_Strategy"]:
                        invoice_strategies[invoice_block["Invoice_Match_Strategy"]] += 1

                    stage1_row = {**row, **invoice_block}
                    stage1_writer.writerow(stage1_row)
                    if stage1_lines is not None:
                        stage1_lines.write(json.dumps(stage1_row, ensure_ascii=False) + "\n")

                    po_block = self.po_joiner.join(keys)
                    counters[f"po_{po_block['PO_Match_Level']}"] += 1
                    if po_block["PO_Match_Strategy"]:
                        po_strategies[po_block["PO_Match_Strategy"]] += 1

                    stage2_row = {**stage1_row, **po_block}
                    stage2_writer.writerow(stage2_row)
                    if stage2_lines is not None:
                        stage2_lines.write(json.dumps(stage2_row, ensure_ascii=False) + "\n")
        finally:
            for handle in handles:
                handle.close()

        self.statistics.update({
            "rows_in": rows_in,
            "invoice_matched_line": counters["invoice_line"],
            "invoice_matched_header": counters["invoice_header"],
            "invoice_unmatched": counters["invoice_none"],
            "po_matched_line": counters["po_line"],
            "po_matched_header": counters["po_header"],
            "po_unmatched": counters["po_none"],
        })
        self.diagnostics["invoice_strategies"] = dict(invoice_strategies)
        self.diagnostics["po_strategies"] = dict(po_strategies)

        self.outputs += [stage1_csv.name, stage2_csv.name]
        if self.settings.write_jsonl:
            self.outputs += [stage1_jsonl.name, stage2_jsonl.name]
        return stage2_csv, stage2_jsonl, stage2_columns

    # -- stage 3 ------------------------------------------------------------

    def interpret(self, stage2_csv: Path, stage2_columns: List[str]) -> Path:
        """Read the free text on every row of the wide table.

        Two passes over the file on disk rather than one over the rows in
        memory: the first collects the distinct strings so the model tier can be
        offered a de-duplicated worklist, the second writes the output. At a
        million rows the distinct strings number in the tens of thousands, which
        is the difference between a run that is affordable and one that is not.
        """
        results = self.settings.results_dir
        stage3_csv = results / "max_stage3_interpreted.csv"
        stage3_jsonl = results / "max_stage3_interpreted.jsonl"
        columns = stage2_columns + Interpretation.columns()

        distinct: Counter = Counter()
        for row in self._read_rows(stage2_csv):
            describing, supporting, _ = self._gather_text(row)
            if describing or supporting:
                distinct[(describing, supporting)] += 1
        LOGGER.info("Stage 3: %d distinct free-text combinations to read.", len(distinct))

        # Interpret every distinct combination once, then decide which readings
        # are weak enough to be worth a model call.
        readings: Dict[Tuple[str, str], Interpretation] = {
            key: self.interpreter.interpret(key[0], key[1], "")
            for key in sorted(distinct)
        }

        model_answers: Dict[str, Dict[str, str]] = {}
        if self.model is not None:
            # The model is asked about the descriptive text only, and asked once
            # per distinct string: two rows whose notes differ but whose
            # description is the same are one question, not two.
            residue = sorted({
                key[0] or key[1] for key, reading in readings.items()
                if reading.confidence < self.settings.interpretation_floor
                and reading.token_count >= 2
            } - {""})
            LOGGER.info("Stage 3: %d of %d readings fell below the confidence floor, "
                        "giving %d distinct strings to ask about.",
                        sum(1 for reading in readings.values()
                            if reading.confidence < self.settings.interpretation_floor),
                        len(readings), len(residue))
            if residue:
                model_answers = ModelReader(self.model, self.settings).resolve(residue)

        weak = sum(1 for reading in readings.values()
                   if reading.confidence < self.settings.interpretation_floor)
        rows_out = 0
        handles = []
        try:
            handle = stage3_csv.open("w", encoding=CSV_ENCODING, newline="")
            handles.append(handle)
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()

            lines = None
            if self.settings.write_jsonl:
                lines = stage3_jsonl.open("w", encoding="utf-8")
                handles.append(lines)

            for row in self._read_rows(stage2_csv):
                describing, supporting, sources = self._gather_text(row)
                reading = readings.get((describing, supporting))
                if reading is None:
                    block = Interpretation(sources=sources).as_columns()
                else:
                    block = self._apply(reading, sources,
                                        model_answers.get(describing or supporting))
                output = {**row, **block}
                writer.writerow(output)
                if lines is not None:
                    lines.write(json.dumps(output, ensure_ascii=False) + "\n")
                rows_out += 1
        finally:
            for opened in handles:
                opened.close()

        self.statistics.update({
            "distinct_texts": len(distinct),
            "texts_below_floor": weak,
            "texts_read_by_model": len(model_answers),
            "rows_out": rows_out,
        })
        self.outputs.append(stage3_csv.name)
        if self.settings.write_jsonl:
            self.outputs.append(stage3_jsonl.name)
        return stage3_csv

    def _apply(self, reading: Interpretation, sources: str,
               answer: Optional[Dict[str, str]]) -> Dict[str, str]:
        """Combine a local reading with the model's answer, if there is one.

        The model replaces only the prose: the description, and the two labels
        when the local reader had none. Every extracted value stays as the rules
        found it, because those are exact and the model's are not.
        """
        result = Interpretation(**{**reading.__dict__, "sources": sources})
        if answer:
            result.description = answer.get("description") or result.description
            result.item_or_service = result.item_or_service or answer.get("type", "")
            result.intent = result.intent or answer.get("intent", "")
            result.method = "model"
            result.confidence = max(result.confidence, 0.60)
        return result.as_columns()

    @staticmethod
    def _read_rows(path: Path) -> Iterator[Dict[str, str]]:
        with path.open("r", encoding=CSV_ENCODING, newline="") as handle:
            for record in csv.DictReader(handle):
                yield {key: (value or "") for key, value in record.items() if key is not None}

    @staticmethod
    def _gather_text(row: Dict[str, str]) -> Tuple[str, str, str]:
        """Collect the row's free text, in priority order, without repetition.

        Returns the descriptive text, the supporting text and the list of
        fields that contributed.
        """
        describing: List[str] = []
        supporting: List[str] = []
        used: List[str] = []
        seen: Set[str] = set()
        for column, label, is_descriptive in TEXT_SOURCE_COLUMNS:
            value = normalise_text(row.get(column, ""))
            if not value or is_blank(value):
                continue
            key = lookup_key(value)
            if key in seen:
                continue
            seen.add(key)
            (describing if is_descriptive else supporting).append(value)
            used.append(label)
        return " | ".join(describing), " | ".join(supporting), "; ".join(used)

    # -- reporting ----------------------------------------------------------

    def write_manifest(self) -> Dict[str, Any]:
        """Record what was read, what was produced and how the joins fared."""
        results = self.settings.results_dir

        if self.model is not None:
            self.statistics["token_usage"] = {
                **self.model.usage.as_dict(),
                **self.model.guard.as_dict(),
            }

        diagnostics = {
            "sources": {
                "transactions": [table.label for table in self.sievo_tables],
                "invoice_lines": len(self.invoice_index.lines) if self.invoice_index else 0,
                "invoice_duplicates_dropped": self.invoice_index.duplicates if self.invoice_index else 0,
                "purchase_order_systems": {
                    name: len(index.records)
                    for name, index in sorted((self.po_joiner.indexes if self.po_joiner else {}).items())
                },
                "purchase_order_systems_unusable": sorted(self.unusable_po_systems),
            },
            "key_population": self.diagnostics.get("key_population", {}),
            "invoice_strategies": self.diagnostics.get("invoice_strategies", {}),
            "po_strategies": self.diagnostics.get("po_strategies", {}),
            "advice": self.diagnostics.get("advice", []),
        }

        diagnostics_path = results / "max_join_diagnostics.json"
        diagnostics_path.write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")
        manifest_path = results / "max_run_manifest.json"
        self.outputs += [diagnostics_path.name, manifest_path.name]

        manifest = {
            "agent": AGENT_NAME,
            "version": AGENT_VERSION,
            "run_id": self.run_id,
            "lexicon_version": self.lexicon.version,
            "source_dir": str(self.settings.source_dir),
            "results_dir": str(results),
            "settings": {
                "native_po_columns": self.settings.native_po_columns,
                "interpretation_floor": self.settings.interpretation_floor,
                "use_llm": self.settings.model.enabled or bool(self.settings.use_llm),
                "model": self.settings.model.model if self.settings.model.enabled else "",
            },
            "statistics": self.statistics,
            "diagnostics": diagnostics,
            "outputs": sorted(set(self.outputs)),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return manifest

    def measure_keys(self) -> None:
        """Record how populated each candidate join key is.

        This is the block to read when a join matched nothing. It states, for
        each key the join would have used, how many transactions carried it and
        how many of those values were also present on the other side.
        """
        # Seeded with every candidate key so that a key which is never populated
        # is reported as zero rather than omitted. "invoice_number: 0" is the
        # single most useful line in this file when a join found nothing, and
        # leaving it out would hide it.
        tracked = ("invoice_number", "document_number", "document_line",
                   "po_number", "po_line", "document_identifier")
        population: Counter = Counter({name: 0 for name in tracked})
        total = 0
        invoice_keys: Set[str] = set()
        if self.invoice_index is not None:
            invoice_keys = set(self.invoice_index._by_document)

        po_keys: Set[str] = set()
        for index in (self.po_joiner.indexes if self.po_joiner else {}).values():
            po_keys |= set(index._by_order)

        matched_invoice: Set[str] = set()
        matched_po: Set[str] = set()

        for table in self.sievo_tables:
            for _, record in table.iter_records():
                total += 1
                keys = {name: self.sievo_map.get(record, name) for name in SIEVO_FIELDS}
                for name in tracked:
                    if not is_blank(keys.get(name)):
                        population[name] += 1

                for name in ("invoice_number", "document_number"):
                    value = keys.get(name, "")
                    if not is_blank(value) and compact_key(value) in invoice_keys:
                        matched_invoice.add(compact_key(value))
                document = document_id_key(keys.get("document_identifier") or "") \
                    or document_id_key(keys.get("invoice_link") or "")
                if document and document in invoice_keys:
                    matched_invoice.add(document)

                order = keys.get("po_number", "")
                if not is_blank(order) and compact_key(order) in po_keys:
                    matched_po.add(compact_key(order))

        self.diagnostics["key_population"] = {
            "transaction_rows": total,
            "populated": {name: count for name, count in sorted(population.items())},
            "invoice_documents_available": len(invoice_keys),
            "invoice_documents_hit": len(matched_invoice),
            "purchase_orders_available": len(po_keys),
            "purchase_orders_hit": len(matched_po),
        }

        advice: List[str] = []
        if self.invoice_index is not None and not matched_invoice:
            advice.append(
                "No transaction key matched any invoice document. Invoice number, "
                "document number and document identifier were all tried. Check that "
                "the invoice extract covers the same period and entities as the "
                "transaction extract, and that 'Invoice number' is populated.")
        if po_keys and not matched_po:
            advice.append(
                "No PO number on the transaction extract appears in the purchase-order "
                "extracts. Check that the PO extracts cover the same companies as the "
                "transactions; the two sets of order numbers do not currently intersect.")
        for name in self.unusable_po_systems:
            advice.append(
                f"The {name} extract was read but carries no PO number on any row, so it "
                f"cannot be joined at all. Re-export it with the key columns populated.")
        if population.get("po_line", 0) < population.get("po_number", 0):
            advice.append(
                "PO line number is less populated than PO number. Transactions without a "
                "line number can only be matched when the order has a single line; the "
                "rest fall back to header-level fields.")
        self.diagnostics["advice"] = advice

    def close(self) -> None:
        if self.model is not None:
            self.model.save_cache()


# ===========================================================================
# Command line
# ===========================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="max.py",
        description="Max - build one wide procurement table from the source extracts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python max.py\n"
            "      Prompt for each path in turn and run with the defaults.\n\n"
            "  python max.py --non-interactive --sources ./sources --results ./results\n"
            "      Run unattended with the local reader only.\n\n"
            "  python max.py --non-interactive --use-llm --llm-spend-limit 10\n"
            "      Add the model tier for free text the rules cannot read.\n"
        ),
    )

    paths = parser.add_argument_group("paths")
    paths.add_argument("--sources", metavar="DIR", help="folder holding the source extracts")
    paths.add_argument("--results", metavar="DIR", help="folder to write results into")
    paths.add_argument("--lexicon", metavar="FILE", help="controlled vocabulary JSON file")
    paths.add_argument("--cache", metavar="DIR", help="folder for the model response cache")

    output = parser.add_argument_group("output")
    output.add_argument("--no-native-po-columns", action="store_true",
                        help="omit the raw Maximo and Basware columns, keeping the "
                             "harmonised PO block only")
    output.add_argument("--no-jsonl", action="store_true", help="skip the JSONL exports")

    tiers = parser.add_argument_group("processing tiers")
    tiers.add_argument("--use-llm", action="store_true",
                       help="let the language model read free text the rules cannot")
    tiers.add_argument("--llm-spend-limit", metavar="USD", type=float, default=None,
                       help="pause and ask once estimated model spend reaches this "
                            f"figure (default {DEFAULT_SPEND_LIMIT:.2f}; 0 disables the alert)")
    tiers.add_argument("--interpretation-floor", type=float, default=0.55,
                       help="confidence below which a reading is offered to the model "
                            "(default 0.55)")

    parser.add_argument("--non-interactive", action="store_true",
                        help="never prompt; use the supplied arguments and defaults")
    parser.add_argument("--verbose", action="store_true", help="emit debug-level logging")
    parser.add_argument("--version", action="version", version=f"{AGENT_NAME} {AGENT_VERSION}")
    return parser


def _clean_path_input(value: str) -> str:
    """Tidy a path pasted into the terminal.

    Dragging a folder onto a terminal wraps it in quotes and escapes its spaces,
    and both would otherwise be taken as part of the name.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.replace("\\ ", " ").strip()


def ask(question: str, default: str) -> str:
    try:
        answer = input(f"{question}\n  [{default}]: ").strip()
    except EOFError:
        return default
    return _clean_path_input(answer) or default


def ask_yes_no(question: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    return default if not answer else answer[0] == "y"


def ask_amount(question: str, default: float) -> float:
    """Prompt for a sum of money, re-asking until the answer is usable."""
    while True:
        try:
            answer = input(f"{question}\n  [{default:.2f}]: ").strip()
        except EOFError:
            return default
        if not answer:
            return default
        try:
            value = float(answer.lstrip("$").replace(",", "").strip())
        except ValueError:
            print("  Enter an amount in dollars, for example 25 or 25.00.")
            continue
        if value < 0:
            print("  Enter zero or more; zero runs without a spend alert.")
            continue
        return value


def resolve_settings(args: argparse.Namespace, env: Dict[str, str]) -> Settings:
    here = Path(__file__).resolve().parent
    default_sources = Path(args.sources) if args.sources else here / "sources"
    default_results = Path(args.results) if args.results else here / "results"
    default_lexicon = (Path(args.lexicon) if args.lexicon
                       else here / "lexicon" / "procurement_lexicon.json")
    default_cache = Path(args.cache) if args.cache else here / "cache"

    native = not args.no_native_po_columns
    use_llm = args.use_llm
    spend_limit = (args.llm_spend_limit if args.llm_spend_limit is not None
                   else _env_float(env.get("LLM_SPEND_LIMIT"), DEFAULT_SPEND_LIMIT))

    if not args.non_interactive:
        print(BANNER)
        print("\nPress Enter to accept the value shown in brackets.\n")
        default_sources = Path(ask("Source data folder", str(default_sources)))
        default_results = Path(ask("Results folder", str(default_results)))
        default_lexicon = Path(ask("Controlled vocabulary file", str(default_lexicon)))
        default_cache = Path(ask("Cache folder", str(default_cache)))

        print()
        native = ask_yes_no(
            "Carry the raw Maximo and Basware columns into the wide table?", native)
        use_llm = ask_yes_no(
            "Let the language model read free text the rules cannot?", use_llm)
        if use_llm:
            print()
            print(f"  Charged at ${INPUT_COST_PER_MTOK:,.2f} per million input tokens and "
                  f"${OUTPUT_COST_PER_MTOK:,.2f} per million output tokens.")
            print("  The run pauses at the figure below and asks before spending more.")
            spend_limit = ask_amount(
                "Alert when estimated language-model spend reaches (USD)", spend_limit)
        print()

    settings = Settings(
        source_dir=default_sources.expanduser().resolve(),
        results_dir=default_results.expanduser().resolve(),
        lexicon_path=default_lexicon.expanduser().resolve(),
        cache_dir=default_cache.expanduser().resolve(),
        native_po_columns=native,
        interpretation_floor=args.interpretation_floor,
        use_llm=use_llm,
        write_jsonl=not args.no_jsonl,
        verbose=args.verbose,
        interactive=not args.non_interactive,
    )
    settings.model = resolve_model_config(env, use_llm, spend_limit)

    if not settings.source_dir.exists():
        raise SystemExit(f"Source folder does not exist: {settings.source_dir}")
    return settings


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S", stream=sys.stdout,
    )


def print_token_usage(statistics: Dict[str, Any], settings: Settings) -> None:
    """Report language-model consumption for the run."""
    usage = statistics.get("token_usage")
    if not usage:
        return
    print()
    print("-" * 79)
    print("Language model usage")
    print("-" * 79)
    print(f"  Model                : {settings.model.model} ({settings.model.backend})")
    print(f"  Requests sent        : {usage['requests']:,}")
    if usage["failed_requests"]:
        print(f"  Failed requests      : {usage['failed_requests']:,}")
    print(f"  Served from cache    : {usage['cache_hits']:,} (no tokens consumed)")
    print(f"  Input tokens         : {usage['input_tokens']:,}")
    if usage["cached_input_tokens"]:
        print(f"    of which cached    : {usage['cached_input_tokens']:,}")
    print(f"  Output tokens        : {usage['output_tokens']:,}")
    if usage["reasoning_tokens"]:
        print(f"    of which reasoning : {usage['reasoning_tokens']:,}")
    print(f"  Total tokens         : {usage['total_tokens']:,}")

    # Priced from the rates in force at configuration time. Cached input is
    # counted at the full rate, so the figure is an upper bound.
    print(f"  Input cost           : ${usage['input_cost_usd']:,.2f} "
          f"at ${usage['input_cost_per_mtok']:,.2f}/M")
    print(f"  Output cost          : ${usage['output_cost_usd']:,.2f} "
          f"at ${usage['output_cost_per_mtok']:,.2f}/M")
    print(f"  Estimated cost       : ${usage['estimated_cost_usd']:,.2f}")

    if usage.get("spend_limit_usd"):
        print(f"  Spend alert          : ${usage['spend_limit_usd']:,.2f}"
              + (f", raised {usage['spend_limit_extensions']} time(s) "
                 f"to ${usage['spend_limit_final_usd']:,.2f}"
                 if usage["spend_limit_extensions"] else ""))
    if usage.get("spend_limit_stopped"):
        print("  Model switched off part way through the run at the spend limit;")
        print("  the remaining text was read by the local reader alone.")


def _share(part: int, whole: int) -> str:
    return f"{part:,} ({part / whole * 100:5.1f}%)" if whole else f"{part:,}"


def print_summary(manifest: Dict[str, Any], settings: Settings) -> None:
    statistics = manifest["statistics"]
    rows = statistics.get("rows_in", 0)

    print()
    print("=" * 79)
    print(f"{AGENT_NAME} - complete")
    print("=" * 79)
    print(f"  Run id               : {manifest['run_id']}")
    print(f"  Vocabulary version   : {manifest['lexicon_version']}")
    print(f"  Transaction rows in  : {rows:,}")
    print(f"  Rows written         : {statistics.get('rows_out', 0):,}")

    print()
    print("  Stage 1  invoice lines onto the transaction")
    print(f"    Matched on a line  : {_share(statistics.get('invoice_matched_line', 0), rows)}")
    print(f"    Matched on document: {_share(statistics.get('invoice_matched_header', 0), rows)}")
    print(f"    Not matched        : {_share(statistics.get('invoice_unmatched', 0), rows)}")

    print()
    print("  Stage 2  purchase order onto the widened table")
    print(f"    Matched on a line  : {_share(statistics.get('po_matched_line', 0), rows)}")
    print(f"    Matched on an order: {_share(statistics.get('po_matched_header', 0), rows)}")
    print(f"    Not matched        : {_share(statistics.get('po_unmatched', 0), rows)}")

    print()
    print("  Stage 3  free text read into columns")
    print(f"    Distinct strings   : {statistics.get('distinct_texts', 0):,}")
    print(f"    Below the floor    : {statistics.get('texts_below_floor', 0):,}")
    if statistics.get("texts_read_by_model"):
        print(f"    Read by the model  : {statistics['texts_read_by_model']:,}")

    advice = manifest.get("diagnostics", {}).get("advice") or []
    if advice:
        print()
        print("  Worth knowing")
        for note in advice:
            wrapped = re.findall(r".{1,70}(?:\s|$)", note)
            print(f"    - {wrapped[0].strip()}")
            for line in wrapped[1:]:
                if line.strip():
                    print(f"      {line.strip()}")

    print()
    print(f"  Output folder        : {settings.results_dir}")
    for name in manifest["outputs"]:
        print(f"    {name}")

    print_token_usage(statistics, settings)
    print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    here = Path(__file__).resolve().parent
    env = {**load_dotenv(here / ".env"), **os.environ}

    try:
        settings = resolve_settings(args, env)
    except SystemExit as error:
        print(f"\n{error}\n")
        return 2

    LOGGER.info("Optional components: %s", ", ".join(
        f"{name}={'yes' if present else 'no'}"
        for name, present in sorted(describe_environment().items())))
    if settings.model.enabled:
        LOGGER.info("Language-model tier enabled: %s via %s",
                    settings.model.model, settings.model.backend)
        if settings.model.spend_limit:
            LOGGER.info("Spend alert set at $%.2f; the run will ask before going past it.",
                        settings.model.spend_limit)
        else:
            LOGGER.info("No spend alert set; the model tier will run unmetered.")

    builder = Builder(settings)
    try:
        builder.load()
    except SystemExit as error:
        print(f"\n{error}\n")
        return 2

    try:
        builder.measure_keys()
        stage2_csv, _, stage2_columns = builder.build_wide()
        builder.interpret(stage2_csv, stage2_columns)
        manifest = builder.write_manifest()
    finally:
        builder.close()

    rows_in = builder.statistics.get("rows_in", 0)
    rows_out = builder.statistics.get("rows_out", 0)
    if rows_in != rows_out:
        # The whole design rests on this holding. If it ever does not, the
        # output is not a widened transaction table and must not be used as one.
        LOGGER.error("Row count changed during the build: %d in, %d out. "
                     "The output is not a faithful widening of the transactions.",
                     rows_in, rows_out)
        print_summary(manifest, settings)
        return 1

    print_summary(manifest, settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
