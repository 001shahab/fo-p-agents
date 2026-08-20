#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 1 - Improved Purchase Description.

Reads procurement line data from every available source system, resolves each line
against the other sources, and appends a clear, standardised English description of
what was actually purchased. The original source text is never modified or discarded.

Design notes
------------
Repeatability is a hard requirement: if the agent is re-run on unchanged inputs it
must produce byte-identical output, so that a user can find the same material again
in a later period. Three things enforce that:

  * A controlled vocabulary (``lexicon/procurement_lexicon.json``) does the bulk of the
    translation work through deterministic lookup rather than generation.
  * The language model is a last-resort fallback only, invoked at temperature 0, on
    *unique* phrases, with every answer written to an on-disk cache.
  * No timestamps, random seeds or hash-order-dependent iteration appear in row output.
    The run identifier is derived from the input file contents and the configuration.

The enrichment never states anything that is not present in the source data. Every
content word in a generated description must trace back either to a source token or to
an explicit vocabulary mapping; fragments that fail that check are dropped rather than
emitted.

Only the standard library is required. Optional packages (rapidfuzz, langdetect,
requests, argostranslate) are used automatically when installed and are transparently
substituted otherwise.

Usage
-----
    python agent1.py                 # interactive, prompts for every path
    python agent1.py --help          # full non-interactive option list
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import importlib
import json
import logging
import math
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

__author__ = "Shahab Anbarjafari"
__version__ = "1.0.0"

LOGGER = logging.getLogger("agent1")

AGENT_NAME = "Agent 1 - Improved Purchase Description"
AGENT_ID = "agent1"

# Used when the environment does not name a model explicitly. Both backends run the
# same generation of model so that output is comparable between local development and
# the shared service; only the deployment prefix differs.
DEFAULT_OPENAI_MODEL = "gpt-5.1"
DEFAULT_AZURE_MODEL = "azure.gpt-5.1"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


# ===========================================================================
# Optional dependencies
# ===========================================================================

def _optional(module_name: str) -> Any:
    """Import a module if it is installed, otherwise return None."""
    try:
        return importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 - a broken optional package must not stop the run
        return None


_rapidfuzz = _optional("rapidfuzz.fuzz")
_langdetect = _optional("langdetect")
_requests = _optional("requests")
_argos = _optional("argostranslate.translate")

if _langdetect is not None:
    # langdetect is probabilistic by default; fixing the seed makes it reproducible.
    try:
        _langdetect.DetectorFactory.seed = 0
    except Exception:  # noqa: BLE001
        _langdetect = None


# ===========================================================================
# Output schema
# ===========================================================================

# Appended to the right of the original columns of every source file.
ENRICHMENT_COLUMNS: Tuple[str, ...] = (
    "Row_Id",
    "Row_Type",
    "Is_Duplicate",
    "Duplicate_Of",
    "Source_Description_Raw",
    "Source_Description_Normalized",
    "Detected_Language",
    "Language_Confidence",
    "Enriched_Purchase_Description",
    "Enriched_Description_Short",
    "Item_Or_Service",
    "Translation_Method",
    "Translation_Coverage",
    "Unresolved_Tokens",
    "Evidence_Sources",
    "Matched_Source_System",
    "Matched_Row_Id",
    "Matched_PO_Number",
    "Matched_PO_Line",
    "Matched_Supplier",
    "Match_Tier",
    "Match_Method",
    "Match_Score",
    "Enrichment_Method",
    "AI_Confidence",
    "Confidence_Band",
    "Agent_Version",
    "Lexicon_Version",
    "Run_Id",
)

# Common core of the unified table consumed by Agents 2 to 4.
UNIFIED_CORE_COLUMNS: Tuple[str, ...] = (
    "Source_System",
    "Source_File",
    "Source_Sheet",
    "Source_Row_Index",
    "Document_Id",
    "Line_Number",
    "PO_Number",
    "PO_Line_Number",
    "Item_Code",
    "Supplier_Name",
    "Supplier_Code",
    "Quantity",
    "Unit_Price",
    "Amount",
    "Currency",
    "Document_Date",
    "Category_L1",
    "Category_L2",
    "Category_L3",
    "Category_L4",
    "Material_Group",
    "Account_Name",
)

CONFIDENCE_BANDS: Tuple[Tuple[int, str], ...] = ((80, "High"), (50, "Medium"), (0, "Low"))


# ===========================================================================
# Configuration
# ===========================================================================

@dataclass
class ModelConfig:
    """Resolved language-model endpoint settings."""

    enabled: bool = False
    provider: str = "openai"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    batch_size: int = 20
    timeout: int = 60
    request_json_mode: bool = False

    def endpoint(self) -> str:
        """Return the chat-completions URL.

        Accepts either a bare API root (``https://host/v1``) or a fully qualified
        endpoint (``https://host/v1/chat/completions``); the Azure shared service is
        configured with the latter form.
        """
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"


@dataclass
class Settings:
    """Everything the pipeline needs in order to run."""

    sources_root: Path
    invoice_dir: Optional[Path]
    po_dir: Optional[Path]
    transaction_dir: Optional[Path]
    catalogue_file: Optional[Path]
    results_dir: Path
    lexicon_file: Optional[Path]
    cache_dir: Path
    use_llm: bool = False
    use_machine_translation: bool = False
    top_k_matches: int = 5
    fuzzy_threshold: float = 0.62
    semantic_threshold: float = 0.45
    max_description_words: int = 12
    model: ModelConfig = field(default_factory=ModelConfig)


# ===========================================================================
# Environment handling
# ===========================================================================

def load_dotenv(path: Path) -> Dict[str, str]:
    """Parse a ``.env`` file into a dictionary without overwriting real env vars.

    Implemented locally so the agent has no hard dependency on python-dotenv.
    Values may be quoted, and ``#`` starts a comment when it begins a line.
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
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _env_flag(value: Optional[str], default: bool = False) -> bool:
    """Interpret a textual environment value as a boolean."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def resolve_model_config(env: Dict[str, str], use_llm: bool) -> ModelConfig:
    """Choose between the Azure shared service and the direct OpenAI API.

    ``AZURE_ENABLE`` is the single switch. Legacy variable names (``BASE_URL``,
    ``MODEL_NAME``) are still honoured so existing environments keep working.
    """
    def get(name: str, fallback: str = "") -> str:
        return (os.environ.get(name) or env.get(name) or fallback).strip()

    azure = _env_flag(os.environ.get("AZURE_ENABLE") or env.get("AZURE_ENABLE"), False)

    if azure:
        config = ModelConfig(
            provider="azure",
            api_key=get("AZURE_OPENAI_API_KEY") or get("OPENAI_API_KEY"),
            base_url=get("AZURE_OPENAI_BASE_URL") or get("BASE_URL"),
            model=get("AZURE_OPENAI_MODEL") or get("MODEL_NAME") or DEFAULT_AZURE_MODEL,
        )
    else:
        config = ModelConfig(
            provider="openai",
            api_key=get("OPENAI_API_KEY"),
            base_url=get("OPENAI_BASE_URL") or get("BASE_URL", DEFAULT_OPENAI_BASE_URL),
            model=get("OPENAI_MODEL") or get("MODEL_NAME") or DEFAULT_OPENAI_MODEL,
        )

    config.batch_size = _safe_int(get("LLM_BATCH_SIZE"), 20)
    config.timeout = _safe_int(get("LLM_TIMEOUT"), 60)
    config.request_json_mode = _env_flag(get("LLM_JSON_MODE"), False)
    config.enabled = bool(use_llm and config.api_key and config.base_url and config.model)
    return config


def _safe_int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ===========================================================================
# Text utilities
# ===========================================================================

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_CODE_LIKE = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9\-_/\.]{1,}$")
_MOJIBAKE_HINT = re.compile(r"Ã[\x80-\xbf]|Â[\x80-\xbf]|ï¿½")
_EXCEL_LINEBREAK = re.compile(r"_x000D_")

NULL_TOKENS = {"", "n/a", "na", "null", "none", "-", "--", "nan", "#n/a"}


def repair_mojibake(text: str) -> str:
    """Undo the common UTF-8-read-as-cp1252 corruption seen in exported files."""
    if not text or not _MOJIBAKE_HINT.search(text):
        return text
    try:
        repaired = text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return repaired


def normalise_text(value: Any) -> str:
    """Return a clean, single-line rendering of a raw cell value."""
    if value is None:
        return ""
    text = str(value)
    text = _EXCEL_LINEBREAK.sub(" ", text)
    text = repair_mojibake(text)
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_CHARS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def is_blank(value: Any) -> bool:
    """True when a value carries no information, including source ``n/a`` markers."""
    return normalise_text(value).lower() in NULL_TOKENS


def lookup_key(text: str) -> str:
    """Lower-cased, punctuation-flattened form used for vocabulary lookups."""
    text = normalise_text(text).lower()
    text = re.sub(r"[^\w\s\-]", " ", text, flags=re.UNICODE)
    return _WHITESPACE.sub(" ", text).strip()


def tokenise(text: str) -> List[str]:
    """Split text into lower-cased alphabetic tokens."""
    return [match.group(0).lower() for match in _WORD.finditer(normalise_text(text))]


def is_code_token(token: str) -> bool:
    """Identify part numbers, account codes and similar non-descriptive tokens."""
    return bool(_CODE_LIKE.match(token))


def compact_key(value: Any) -> str:
    """Aggressive normalisation for identifier comparison across systems."""
    return re.sub(r"[^A-Z0-9]", "", normalise_text(value).upper())


def parse_amount(value: Any) -> Optional[float]:
    """Parse a monetary value tolerating both European and Anglo number formats."""
    text = normalise_text(value)
    if text.lower() in NULL_TOKENS:
        return None
    text = text.replace("\u00a0", "").replace(" ", "")
    text = re.sub(r"[^\d,\.\-]", "", text)
    if not text or text in {"-", ".", ","}:
        return None

    if "," in text and "." in text:
        # Whichever separator appears last is the decimal separator.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        decimals = len(text.split(",")[-1])
        text = text.replace(",", "." if decimals in (1, 2) else "")

    try:
        return float(text)
    except ValueError:
        return None


_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%m/%d/%Y")


def parse_date(value: Any) -> Optional[str]:
    """Return an ISO date string, or None when the value is not a date."""
    text = normalise_text(value)
    if text.lower() in NULL_TOKENS:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[: len(fmt) + 4], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    return match.group(0) if match else None


def sha256_file(path: Path) -> str:
    """Content hash of a file, used to make the run identifier reproducible."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_similarity(left: str, right: str) -> float:
    """Order-insensitive similarity in the range 0..1.

    Uses rapidfuzz when available; the standard-library fallback blends a token
    Jaccard overlap with a sequence ratio so both give comparable magnitudes.
    """
    if not left or not right:
        return 0.0
    if _rapidfuzz is not None:
        return float(_rapidfuzz.token_set_ratio(left, right)) / 100.0

    left_tokens = set(tokenise(left))
    right_tokens = set(tokenise(right))
    if not left_tokens or not right_tokens:
        return 0.0
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = difflib.SequenceMatcher(
        None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))
    ).ratio()
    return round(0.6 * sequence + 0.4 * jaccard, 6)


def amounts_agree(left: Optional[float], right: Optional[float]) -> bool:
    """Compare two amounts with a tolerance that scales with magnitude."""
    if left is None or right is None:
        return False
    return abs(left - right) <= max(0.01, 0.005 * max(abs(left), abs(right)))


def sentence_case(text: str) -> str:
    """Capitalise the first letter only, preserving existing internal casing."""
    text = text.strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


# ===========================================================================
# Character n-gram index (semantic retrieval without external dependencies)
# ===========================================================================

class CharNgramIndex:
    """TF-IDF cosine retrieval over character n-grams.

    Character n-grams are deliberate: they tolerate inflection, compounding and
    spelling drift across systems far better than word tokens, which matters when the
    same item is written in Finnish in one system and English in another.
    """

    def __init__(
        self,
        documents: Dict[str, str],
        n_min: int = 3,
        n_max: int = 5,
        df_ceiling: float = 0.5,
    ) -> None:
        self._n_min = n_min
        self._n_max = n_max
        self._vectors: Dict[str, Dict[str, float]] = {}
        self._postings: Dict[str, List[Tuple[str, float]]] = defaultdict(list)

        grams_per_doc: Dict[str, Counter] = {}
        document_frequency: Counter = Counter()
        for doc_id, text in sorted(documents.items()):
            grams = self._grams(text)
            if not grams:
                continue
            grams_per_doc[doc_id] = grams
            document_frequency.update(grams.keys())

        total = max(1, len(grams_per_doc))
        ceiling = max(2, int(df_ceiling * total))
        self._idf = {
            gram: math.log((1.0 + total) / (1.0 + count)) + 1.0
            for gram, count in document_frequency.items()
            if count <= ceiling
        }

        for doc_id, grams in grams_per_doc.items():
            vector = self._weight(grams)
            if not vector:
                continue
            self._vectors[doc_id] = vector
            for gram, weight in vector.items():
                self._postings[gram].append((doc_id, weight))

    def _grams(self, text: str) -> Counter:
        cleaned = _WHITESPACE.sub(" ", normalise_text(text).lower()).strip()
        if not cleaned:
            return Counter()
        padded = f" {cleaned} "
        counter: Counter = Counter()
        for size in range(self._n_min, self._n_max + 1):
            if len(padded) < size:
                break
            for start in range(len(padded) - size + 1):
                counter[padded[start : start + size]] += 1
        return counter

    def _weight(self, grams: Counter) -> Dict[str, float]:
        """Sub-linear term frequency times inverse document frequency, L2-normalised."""
        vector: Dict[str, float] = {}
        for gram, frequency in grams.items():
            idf = self._idf.get(gram)
            if idf is None:
                continue
            vector[gram] = (1.0 + math.log(frequency)) * idf
        norm = math.sqrt(sum(value * value for value in vector.values()))
        if norm == 0.0:
            return {}
        return {gram: value / norm for gram, value in vector.items()}

    def query(self, text: str, top_k: int = 5, min_score: float = 0.0) -> List[Tuple[str, float]]:
        """Return the highest scoring documents, ties broken by identifier."""
        vector = self._weight(self._grams(text))
        if not vector:
            return []
        scores: Dict[str, float] = defaultdict(float)
        for gram, weight in vector.items():
            for doc_id, doc_weight in self._postings.get(gram, ()):
                scores[doc_id] += weight * doc_weight
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [(doc_id, round(score, 6)) for doc_id, score in ranked[:top_k] if score >= min_score]


# ===========================================================================
# Spreadsheet and delimited-file readers
# ===========================================================================

_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Built-in Excel number formats that represent dates or times.
_BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(45, 48))
_EXCEL_EPOCH = datetime(1899, 12, 30)


@dataclass
class Table:
    """A rectangular block of source data with its provenance."""

    path: Path
    sheet: str
    columns: List[str]
    rows: List[Dict[str, str]]


def _column_index(reference: str) -> int:
    """Convert an Excel column reference such as ``AB12`` into a zero-based index."""
    letters = "".join(char for char in reference if char.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - 64)
    return index - 1


def _read_xlsx(path: Path) -> List[Table]:
    """Read an ``.xlsx`` workbook using only the standard library.

    Shared strings, inline strings and date-formatted numbers are all resolved. Every
    worksheet is returned; the first row containing at least two populated cells is
    treated as the header.
    """
    tables: List[Table] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())

        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                shared.append("".join(node.text or "" for node in item.iter(_MAIN_NS + "t")))

        date_styles = _read_date_styles(archive, names)

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relations: Dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for relation in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels")):
                relations[relation.get("Id", "")] = relation.get("Target", "")

        sheets_node = workbook.find(_MAIN_NS + "sheets")
        if sheets_node is None:
            return tables

        for sheet in sheets_node:
            target = relations.get(sheet.get(_REL_NS + "id", ""), "")
            if not target:
                continue
            member = target.lstrip("/")
            if not member.startswith("xl/"):
                member = "xl/" + member
            if member not in names:
                continue
            grid = _read_worksheet(archive.read(member), shared, date_styles)
            table = _grid_to_table(path, sheet.get("name", "Sheet"), grid)
            if table is not None:
                tables.append(table)
    return tables


def _read_date_styles(archive: zipfile.ZipFile, names: Iterable[str]) -> set:
    """Return the set of cell-style indices whose number format is a date."""
    if "xl/styles.xml" not in set(names):
        return set()
    try:
        styles = ET.fromstring(archive.read("xl/styles.xml"))
    except ET.ParseError:
        return set()

    custom_date_formats = set()
    for number_format in styles.iter(_MAIN_NS + "numFmt"):
        code = (number_format.get("formatCode") or "").lower()
        stripped = re.sub(r"\[[^\]]*\]|\"[^\"]*\"", "", code)
        if re.search(r"[ymd]", stripped) and "e+" not in stripped:
            custom_date_formats.add(number_format.get("numFmtId", ""))

    date_styles = set()
    cell_formats = styles.find(_MAIN_NS + "cellXfs")
    if cell_formats is None:
        return date_styles
    for position, cell_format in enumerate(cell_formats):
        format_id = cell_format.get("numFmtId", "0")
        if format_id in custom_date_formats or _safe_int(format_id, -1) in _BUILTIN_DATE_FORMATS:
            date_styles.add(position)
    return date_styles


def _read_worksheet(payload: bytes, shared: List[str], date_styles: set) -> List[Dict[int, str]]:
    """Convert a worksheet part into a list of ``{column index: value}`` rows."""
    sheet = ET.fromstring(payload)
    grid: List[Dict[int, str]] = []
    for row in sheet.iter(_MAIN_NS + "row"):
        cells: Dict[int, str] = {}
        for cell in row:
            reference = cell.get("r")
            if not reference:
                continue
            cell_type = cell.get("t")
            value_node = cell.find(_MAIN_NS + "v")
            inline_node = cell.find(_MAIN_NS + "is")

            if inline_node is not None:
                value: Any = "".join(node.text or "" for node in inline_node.iter(_MAIN_NS + "t"))
            elif value_node is None or value_node.text is None:
                continue
            elif cell_type == "s":
                index = _safe_int(value_node.text, -1)
                value = shared[index] if 0 <= index < len(shared) else ""
            elif cell_type == "b":
                value = "TRUE" if value_node.text.strip() == "1" else "FALSE"
            else:
                value = value_node.text
                style = _safe_int(cell.get("s", "-1"), -1)
                if style in date_styles:
                    value = _excel_serial_to_date(value)

            text = normalise_text(value)
            if text:
                cells[_column_index(reference)] = text
        grid.append(cells)
    return grid


def _excel_serial_to_date(value: Any) -> str:
    """Convert an Excel serial number to an ISO date, leaving other values untouched."""
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return str(value)
    if serial <= 0:
        return str(value)
    moment = _EXCEL_EPOCH + timedelta(days=serial)
    if abs(serial - round(serial)) < 1e-9:
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _grid_to_table(path: Path, sheet_name: str, grid: List[Dict[int, str]]) -> Optional[Table]:
    """Locate the header row and materialise the remaining rows as dictionaries."""
    header_position = None
    for position, cells in enumerate(grid):
        if len([value for value in cells.values() if value]) >= 2:
            header_position = position
            break
    if header_position is None:
        return None

    header_cells = grid[header_position]
    columns_by_index: Dict[int, str] = {}
    seen: Counter = Counter()
    for index in sorted(header_cells):
        name = header_cells[index] or f"Column_{index + 1}"
        seen[name] += 1
        if seen[name] > 1:
            name = f"{name}_{seen[name]}"
        columns_by_index[index] = name

    columns = [columns_by_index[index] for index in sorted(columns_by_index)]
    rows: List[Dict[str, str]] = []
    for cells in grid[header_position + 1 :]:
        row = {name: cells.get(index, "") for index, name in columns_by_index.items()}
        if any(value for value in row.values()):
            rows.append(row)
    return Table(path=path, sheet=sheet_name, columns=columns, rows=rows)


def _read_delimited(path: Path) -> List[Table]:
    """Read a CSV or TSV file, detecting both the encoding and the delimiter."""
    raw = path.read_bytes()
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = raw.decode("utf-8", errors="replace")

    sample = text[:8192]
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        pass

    reader = csv.DictReader(text.splitlines(True), delimiter=delimiter)
    columns = [name for name in (reader.fieldnames or []) if name is not None]
    rows: List[Dict[str, str]] = []
    for record in reader:
        row = {name: normalise_text(record.get(name)) for name in columns}
        if any(value for value in row.values()):
            rows.append(row)
    return [Table(path=path, sheet=path.stem, columns=columns, rows=rows)]


def read_table_file(path: Path) -> List[Table]:
    """Dispatch to the appropriate reader based on the file extension."""
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return _read_xlsx(path)
    if suffix in {".csv", ".tsv", ".txt"}:
        return _read_delimited(path)
    LOGGER.warning("Skipping unsupported file type: %s", path.name)
    return []


# ===========================================================================
# Source profiles
# ===========================================================================

@dataclass(frozen=True)
class SourceProfile:
    """Declarative description of one source system's line-level layout."""

    key: str
    label: str
    signature: Tuple[str, ...]
    primary_text: Tuple[str, ...]
    support_text: Tuple[str, ...] = ()
    aliases: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    structural_rows: bool = False
    volatile_columns: Tuple[str, ...] = ()
    is_target: bool = True


SOURCE_PROFILES: Tuple[SourceProfile, ...] = (
    SourceProfile(
        key="invoice",
        label="Invoice lines",
        signature=("invoicekey", "articlename", "rowtotalexclvat"),
        primary_text=("article_name",),
        support_text=("free_text",),
        aliases={
            "document_id": ("invoice_key", "invoice_id"),
            "line_number": ("row_number",),
            "item_code": ("article_id",),
            "quantity": ("quantity_charged", "quantity_delivered"),
            "unit_price": ("unit_price_excl_vat", "unit_price_net"),
            "amount": ("row_total_excl_vat", "row_total_incl_vat"),
            "account_name": ("free_text",),
        },
        structural_rows=True,
        volatile_columns=("xml_file_name",),
    ),
    SourceProfile(
        key="basware",
        label="Basware purchase orders",
        signature=("ordernumber", "polinenumber", "supplierproductname"),
        primary_text=("Supplier product name",),
        support_text=("Main category", "Sub category", "Account name", "Project name"),
        aliases={
            "document_id": ("Order number",),
            "po_number": ("Order number",),
            "po_line_number": ("PO line number",),
            "line_number": ("PO line number",),
            "item_code": ("Supplier product code", "Item ID"),
            "supplier_name": ("Supplier name",),
            "supplier_code": ("Supplier code",),
            "quantity": ("PO line quantity",),
            "amount": ("PO net sum company",),
            "currency": ("PO currency company", "PO currency organization"),
            "document_date": ("PO creation date", "PR creation date"),
            "category_l1": ("Main category",),
            "category_l2": ("Sub category",),
            "material_group": ("Category code",),
            "account_name": ("Account name",),
        },
    ),
    SourceProfile(
        key="maximo",
        label="Maximo purchase orders",
        signature=("ponum", "linedescription", "itemnum"),
        primary_text=("LINE_DESCRIPTION", "DESCRIPTION"),
        support_text=("COMMODITY", "COMMODITYGROUP", "XPOINTERNALNOTE"),
        aliases={
            "document_id": ("PONUM",),
            "po_number": ("PONUM",),
            "po_line_number": ("POLINENUM",),
            "line_number": ("POLINENUM",),
            "item_code": ("ITEMNUM",),
            "supplier_name": ("VENDOR",),
            "quantity": ("ORDERQTY",),
            "unit_price": ("UNITCOST",),
            "amount": ("LINECOST", "TOTALCOST", "LOADEDCOST"),
            "currency": ("CURRENCYCODE",),
            "document_date": ("ORDERDATE",),
            "category_l1": ("COMMODITYGROUP",),
            "category_l2": ("COMMODITY",),
            "material_group": ("COMMODITYGROUP",),
        },
    ),
    SourceProfile(
        key="sievo",
        label="Sievo transactions",
        signature=("sourcerowid", "datasource", "categoryl1"),
        primary_text=("PO line desc", "Document line desc"),
        support_text=("MaterialGroupName", "Category L4", "Project"),
        aliases={
            "document_id": ("Document number", "Invoice number"),
            "po_number": ("PO number",),
            "po_line_number": ("PO line number",),
            "line_number": ("Document line number",),
            "supplier_name": ("ERP supplier name", "Supplier"),
            "supplier_code": ("ERP supplier number",),
            "quantity": ("Quantity",),
            "amount": ("Spend in purchase currency", "Spend in EUR"),
            "currency": ("Purchase currency",),
            "document_date": ("Posting date", "Invoice date"),
            "category_l1": ("Category L1",),
            "category_l2": ("Category L2",),
            "category_l3": ("Category L3",),
            "category_l4": ("Category L4",),
            "material_group": ("MaterialGroupName",),
            "account_name": ("GLAccountNumber",),
        },
    ),
    SourceProfile(
        key="catalogue",
        label="Supplier catalogues",
        signature=("supplier", "itemname", "itemcode"),
        primary_text=("Item_Name", "Item_Description"),
        support_text=(),
        aliases={
            "item_code": ("Item_Code",),
            "supplier_name": ("Supplier",),
            "unit_price": ("Unit_Price",),
        },
        is_target=False,
    ),
)


def _normalise_column(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalise_text(name).lower())


def detect_profile(table: Table) -> Tuple[SourceProfile, float]:
    """Identify which source system a table came from.

    Matching is done on normalised column names rather than file names or folders, so
    a file still resolves correctly when it is delivered as CSV instead of Excel or
    placed in an unexpected directory.
    """
    present = {_normalise_column(column) for column in table.columns}
    best_profile: Optional[SourceProfile] = None
    best_score = 0.0
    for profile in SOURCE_PROFILES:
        overlap = len(present & set(profile.signature)) / len(profile.signature)
        if overlap > best_score:
            best_profile, best_score = profile, overlap
    if best_profile is not None and best_score >= 0.6:
        return best_profile, best_score
    return _generic_profile(table), 0.0


def _generic_profile(table: Table) -> SourceProfile:
    """Build a best-effort profile for a file that matches no known layout.

    The descriptive column is taken to be the textual column with the highest average
    word count, which is a reliable proxy for a free-text description field.
    """
    scores: List[Tuple[float, str]] = []
    sample = table.rows[:200]
    for column in table.columns:
        values = [normalise_text(row.get(column)) for row in sample]
        words = [len(tokenise(value)) for value in values if value]
        if not words:
            continue
        alphabetic = sum(1 for value in values if _WORD.search(value))
        if alphabetic < max(1, len(values) // 4):
            continue
        scores.append((sum(words) / len(words), column))
    scores.sort(key=lambda item: (-item[0], item[1]))
    primary = tuple(column for _, column in scores[:1])
    support = tuple(column for _, column in scores[1:3])

    def find(*needles: str) -> Tuple[str, ...]:
        matched = []
        for column in table.columns:
            key = _normalise_column(column)
            if any(needle in key for needle in needles):
                matched.append(column)
        return tuple(matched[:2])

    return SourceProfile(
        key=f"generic:{table.path.stem}",
        label=f"Unrecognised layout ({table.path.name})",
        signature=(),
        primary_text=primary,
        support_text=support,
        aliases={
            "document_id": find("documentnumber", "invoicenumber", "ordernumber"),
            "po_number": find("ponumber", "ponum", "ordernumber"),
            "po_line_number": find("polinenumber", "polinenum"),
            "item_code": find("itemcode", "itemnum", "articleid", "materialnumber"),
            "supplier_name": find("suppliername", "vendor", "supplier"),
            "amount": find("amount", "netsum", "linecost", "spend", "total"),
            "currency": find("currency",),
            "document_date": find("date",),
        },
    )


# ===========================================================================
# Record model
# ===========================================================================

@dataclass
class MatchCandidate:
    """One resolved link between a line and a line in another source system."""

    row_id: str
    source_key: str
    tier: str
    method: str
    score: float
    po_number: str = ""
    po_line: str = ""
    supplier: str = ""
    text: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "row_id": self.row_id,
            "source": self.source_key,
            "tier": self.tier,
            "method": self.method,
            "score": round(self.score, 4),
            "po_number": self.po_number,
            "po_line": self.po_line,
            "supplier": self.supplier,
            "text": self.text,
        }


@dataclass
class TranslationResult:
    """Outcome of rendering one source-language phrase in English."""

    english: str = ""
    method: str = "none"
    coverage: float = 0.0
    unresolved: List[str] = field(default_factory=list)


@dataclass
class LineRecord:
    """A single source line together with everything the agent derives from it."""

    row_id: str
    source_key: str
    source_label: str
    source_file: str
    source_sheet: str
    table_key: str
    row_index: int
    raw: Dict[str, str]
    logical: Dict[str, str]
    profile: SourceProfile
    content_hash: str = ""

    row_type: str = "LINE"
    duplicate_of: str = ""

    raw_description: str = ""
    normalised_description: str = ""
    support_description: str = ""
    language: str = "und"
    language_confidence: float = 0.0

    translation: TranslationResult = field(default_factory=TranslationResult)
    support_translation: TranslationResult = field(default_factory=TranslationResult)
    matches: List[MatchCandidate] = field(default_factory=list)

    description: str = ""
    description_short: str = ""
    item_or_service: str = "Unknown"
    enrichment_method: str = "none"
    confidence: int = 0
    confidence_band: str = "Low"
    confidence_components: Dict[str, float] = field(default_factory=dict)

    @property
    def is_line(self) -> bool:
        return self.row_type == "LINE"

    @property
    def search_text(self) -> str:
        """Concatenated source and English text used for similarity retrieval."""
        parts = [self.normalised_description, self.translation.english, self.support_description]
        return " ".join(part for part in parts if part)


# ===========================================================================
# Controlled vocabulary
# ===========================================================================

DEFAULT_LEXICON: Dict[str, Any] = {
    "version": "builtin",
    "phrases": {
        "kortti- ja latauspalvelu": "card and charging service",
        "renkaiden säilytys": "tyre storage",
        "kaikki yhteensä": "grand total",
        "ajoneuvovero": "vehicle tax",
        "asbestipurku": "asbestos removal",
    },
    "terms": {
        "vuokra": "rental",
        "hallinnointi": "administration",
        "palvelumaksu": "service fee",
        "maksukorttiostot": "payment card purchases",
        "vero": "tax",
        "renkaat": "tyres",
    },
    "compound_parts": ["ajoneuvo", "maksu", "kortti", "osto", "vero", "palvelu"],
    "inflection_suffixes": ["ssa", "ssä", "lla", "llä", "jen", "ien", "t", "n", "a", "ä"],
    "concepts": {},
    "service_markers": ["service", "fee", "rental", "tax", "storage"],
    "material_markers": ["tyre", "material", "part"],
    "total_markers": ["yhteensä", "total", "totalt", "summa"],
    "stopwords": ["ja", "och", "the", "and", "of", "for"],
}


class Lexicon:
    """Controlled procurement vocabulary and the rules that apply it."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.version: str = str(data.get("version", "builtin"))
        self.phrases: Dict[str, str] = {
            lookup_key(key): value for key, value in data.get("phrases", {}).items()
        }
        self.terms: Dict[str, str] = {
            lookup_key(key): value for key, value in data.get("terms", {}).items()
        }
        self.concepts: Dict[str, Dict[str, str]] = {
            lookup_key(key): value for key, value in data.get("concepts", {}).items()
        }
        self.compound_parts: List[str] = sorted(
            {lookup_key(part) for part in data.get("compound_parts", [])}, key=len, reverse=True
        )
        self.inflection_suffixes: List[str] = sorted(
            {str(item).lower() for item in data.get("inflection_suffixes", [])},
            key=len,
            reverse=True,
        )
        self.service_markers = {str(item).lower() for item in data.get("service_markers", [])}
        self.material_markers = {str(item).lower() for item in data.get("material_markers", [])}
        self.total_markers = {lookup_key(item) for item in data.get("total_markers", [])}
        self.stopwords = {str(item).lower() for item in data.get("stopwords", [])}

        # Longest phrases first so that specific expressions win over their components.
        self.phrase_order = sorted(self.phrases, key=len, reverse=True)

        self.english_vocabulary = set()
        for value in list(self.terms.values()) + list(self.phrases.values()):
            self.english_vocabulary.update(tokenise(value))
        for concept in self.concepts.values():
            self.english_vocabulary.update(tokenise(str(concept.get("label", ""))))

    @classmethod
    def load(cls, path: Optional[Path]) -> "Lexicon":
        """Load the vocabulary file, falling back to the built-in minimal set."""
        data = dict(DEFAULT_LEXICON)
        if path and path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                LOGGER.warning("Could not read lexicon %s (%s); using built-in set", path, error)
            else:
                for key, value in loaded.items():
                    if isinstance(value, dict) and isinstance(data.get(key), dict):
                        merged = dict(data[key])
                        merged.update(value)
                        data[key] = merged
                    else:
                        data[key] = value
                LOGGER.info("Loaded vocabulary %s (%s)", path.name, loaded.get("version", "?"))
        return cls(data)

    def strip_inflection(self, token: str) -> Optional[str]:
        """Remove a case ending only when doing so yields a known base form."""
        for suffix in self.inflection_suffixes:
            if len(token) > len(suffix) + 2 and token.endswith(suffix):
                base = token[: -len(suffix)]
                if base in self.terms or base in self.compound_parts:
                    return base
        return None

    def resolve_token(self, token: str) -> Optional[str]:
        """Translate a single token via direct lookup, inflection stripping or compounding."""
        if token in self.terms:
            return self.terms[token]
        base = self.strip_inflection(token)
        if base and base in self.terms:
            return self.terms[base]
        return self.split_compound(token)

    def split_compound(self, token: str) -> Optional[str]:
        """Decompose a Nordic compound word into known parts and translate each.

        The whole token must be consumed by known parts; a partial decomposition is
        rejected rather than guessed at, which keeps output trustworthy.
        """
        if len(token) < 6:
            return None
        parts: List[str] = []
        position = 0
        length = len(token)
        while position < length:
            for end in range(length, position + 2, -1):
                candidate = token[position:end]
                translated = self.terms.get(candidate)
                if translated is None:
                    base = self.strip_inflection(candidate)
                    translated = self.terms.get(base) if base else None
                if translated:
                    parts.append(translated)
                    position = end
                    break
            else:
                return None
        return " ".join(parts) if len(parts) >= 2 else None

    def concept_for(self, english_phrase: str) -> Optional[Dict[str, str]]:
        return self.concepts.get(lookup_key(english_phrase))

    def classify_kind(self, english_phrase: str) -> str:
        """Label a phrase as a service or a material from its marker words."""
        concept = self.concept_for(english_phrase)
        if concept and concept.get("kind"):
            return str(concept["kind"]).capitalize()
        tokens = set(tokenise(english_phrase))
        service_hits = len(tokens & self.service_markers)
        material_hits = len(tokens & self.material_markers)
        if service_hits > material_hits:
            return "Service"
        if material_hits > service_hits:
            return "Material"
        return "Unknown"

    def looks_like_total(self, text: str) -> bool:
        key = lookup_key(text)
        if not key:
            return False
        return any(marker and marker in key for marker in self.total_markers)


# ===========================================================================
# Language identification
# ===========================================================================

_LANGUAGE_MARKERS: Dict[str, Tuple[set, Tuple[str, ...], str]] = {
    "fi": (
        {"ja", "on", "ei", "että", "sekä", "kun", "myös", "tai", "yhteensä", "kpl", "mukaan"},
        ("nen", "inen", "ssa", "ssä", "lla", "llä", "ksi", "uus", "ys", "ös", "ista", "istä"),
        "",
    ),
    "sv": (
        {"och", "för", "med", "av", "till", "är", "den", "det", "ett", "en", "på"},
        ("ning", "het", "else", "ande", "aren"),
        "",
    ),
    "pl": (
        {"i", "w", "na", "do", "oraz", "nie", "z", "dla", "przez"},
        ("owy", "owa", "ych", "ami", "nie"),
        "łżśąęćńźż",
    ),
    "en": (
        {"the", "and", "of", "for", "with", "to", "in", "on", "service", "order", "total"},
        ("tion", "ment", "ing", "ance", "ence"),
        "",
    ),
}


def detect_language(text: str, lexicon: Optional["Lexicon"] = None) -> Tuple[str, float]:
    """Identify the language of a short phrase.

    Short procurement strings defeat general-purpose detectors: a single upper-case
    word such as "VUOKRA" carries none of the statistical signal they rely on. A
    marker-based heuristic tuned to the languages actually present in this data runs
    first, the controlled vocabulary is consulted as corroborating evidence, and the
    optional langdetect package only ever breaks genuine ties.
    """
    tokens = tokenise(text)
    if not tokens:
        return "und", 0.0

    lowered = normalise_text(text).lower()
    scores: Dict[str, float] = {}
    for language, (markers, suffixes, characters) in _LANGUAGE_MARKERS.items():
        score = 0.0
        score += 2.0 * sum(1 for token in tokens if token in markers)
        score += 1.0 * sum(1 for token in tokens if token.endswith(suffixes))
        if characters:
            score += 2.0 * sum(1 for char in lowered if char in characters)
        scores[language] = score

    # Scandinavian diacritics separate Finnish and Swedish from English and Polish.
    if any(char in lowered for char in "äöå"):
        scores["fi"] += 1.0
        scores["sv"] += 1.0

    ascii_ratio = sum(1 for token in tokens if token.isascii()) / len(tokens)
    scores["en"] += ascii_ratio

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    best_language, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

    # A vocabulary hit is strong evidence the text is not English, but the vocabulary
    # does not record which source language each term belongs to. Reporting
    # "undetermined" is more honest than guessing, and the translation cascade does not
    # depend on the answer.
    if lexicon is not None and best_score <= 1.0:
        if lookup_key(text) in lexicon.phrases or any(
            token in lexicon.terms for token in tokens
        ):
            return "und", 0.3

    if best_score <= 0.0:
        if _langdetect is not None:
            try:
                return str(_langdetect.detect(text)), 0.4
            except Exception:  # noqa: BLE001 - detector raises on very short input
                pass
        return "und", 0.0

    confidence = min(1.0, (best_score - runner_up + 1.0) / (best_score + 1.0))
    return best_language, round(confidence, 3)


# ===========================================================================
# Machine translation (optional, offline)
# ===========================================================================

class MachineTranslator:
    """Thin wrapper over an offline translation engine.

    Argos Translate is used when it is installed and the relevant language pair has
    been downloaded. It runs entirely locally, so enabling it adds no per-row cost.
    """

    def __init__(self, enabled: bool) -> None:
        self.available = False
        self._pairs: Dict[str, Any] = {}
        self._cache: Dict[Tuple[str, str], str] = {}
        if not enabled or _argos is None:
            return
        try:
            installed = _argos.get_installed_languages()
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Offline translation unavailable: %s", error)
            return

        english = next((language for language in installed if language.code == "en"), None)
        if english is None:
            LOGGER.warning("Offline translation needs an installed English target model")
            return
        for language in installed:
            if language.code == "en":
                continue
            try:
                self._pairs[language.code] = language.get_translation(english)
            except Exception:  # noqa: BLE001
                continue
        self.available = bool(self._pairs)
        if self.available:
            LOGGER.info("Offline translation enabled for: %s", ", ".join(sorted(self._pairs)))

    def translate(self, text: str, language: str) -> Optional[str]:
        """Translate a phrase into English, or return None when unsupported."""
        if not self.available or not text:
            return None
        engine = self._pairs.get(language)
        if engine is None:
            return None
        key = (language, text)
        if key in self._cache:
            return self._cache[key]
        try:
            result = normalise_text(engine.translate(text))
        except Exception:  # noqa: BLE001
            return None
        self._cache[key] = result
        return result


# ===========================================================================
# Language model client
# ===========================================================================

@dataclass
class TokenUsage:
    """Running total of language-model consumption for one run.

    Reasoning tokens are tracked separately because reasoning models bill for output
    that never appears in the response, so completion tokens alone understate what a
    run actually cost.
    """

    requests: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reported_total_tokens: int = 0
    reasoning_tokens: int = 0
    cached_prompt_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Total billed tokens, accumulated per response rather than derived at the end."""
        return self.reported_total_tokens or (self.prompt_tokens + self.completion_tokens)

    @property
    def any_recorded(self) -> bool:
        return bool(self.requests or self.cache_hits)

    def as_dict(self) -> Dict[str, int]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "input_tokens": self.prompt_tokens,
            "output_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_input_tokens": self.cached_prompt_tokens,
            "total_tokens": self.total_tokens,
        }


class LanguageModelClient:
    """Chat-completions client used only for phrases the vocabulary cannot resolve.

    Answers are cached on disk by model and phrase, so repeated runs cost nothing and
    return identical text. Requests are issued at temperature 0 for the same reason.
    """

    _SYSTEM_PROMPT = (
        "You standardise procurement line-item text for spend analysis. "
        "For each supplied phrase, return a concise English noun phrase describing what "
        "was purchased. Use only information contained in the phrase itself. Never add "
        "product names, brands, suppliers, quantities, specifications or assumptions that "
        "are not present in the input. If a phrase carries no purchasing meaning, return "
        "it unchanged. Reply with JSON only, in the form "
        '{"translations": [{"id": <integer>, "english": "<text>"}]}.'
    )

    def __init__(self, config: ModelConfig, cache_path: Path) -> None:
        self.config = config
        self.available = config.enabled
        self.calls = 0
        self.usage = TokenUsage()
        self._cache_path = cache_path
        self._cache: Dict[str, str] = {}
        self._dirty = False
        if cache_path.is_file():
            try:
                self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._cache = {}

    def _cache_key(self, phrase: str, language: str) -> str:
        payload = f"{self.config.model}|{language}|{phrase}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def translate_phrases(self, phrases: Sequence[Tuple[str, str]]) -> Dict[str, str]:
        """Translate ``(phrase, language)`` pairs, returning a phrase to English map."""
        results: Dict[str, str] = {}
        outstanding: List[Tuple[str, str]] = []
        for phrase, language in phrases:
            cached = self._cache.get(self._cache_key(phrase, language))
            if cached is not None:
                results[phrase] = cached
                self.usage.cache_hits += 1
            else:
                outstanding.append((phrase, language))

        if not outstanding or not self.available:
            return results

        size = max(1, self.config.batch_size)
        for start in range(0, len(outstanding), size):
            batch = outstanding[start : start + size]
            answers = self._request_batch(batch)
            for (phrase, language), english in answers.items():
                results[phrase] = english
                self._cache[self._cache_key(phrase, language)] = english
                self._dirty = True
        return results

    def _request_batch(self, batch: Sequence[Tuple[str, str]]) -> Dict[Tuple[str, str], str]:
        items = [
            {"id": index, "text": phrase, "language": language}
            for index, (phrase, language) in enumerate(batch)
        ]
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"phrases": items}, ensure_ascii=False)},
            ],
        }
        if self.config.request_json_mode:
            payload["response_format"] = {"type": "json_object"}

        content = self._post(payload)
        if content is None:
            return {}

        parsed = _extract_json_object(content)
        if not parsed:
            LOGGER.warning("Model returned an unparseable response; keeping source text")
            return {}

        answers: Dict[Tuple[str, str], str] = {}
        for entry in parsed.get("translations", []):
            index = _safe_int(str(entry.get("id", -1)), -1)
            english = normalise_text(entry.get("english"))
            if 0 <= index < len(batch) and english:
                answers[batch[index]] = english
        return answers

    def _post(self, payload: Dict[str, Any]) -> Optional[str]:
        """Issue the request, degrading unsupported parameters and backing off on errors.

        Model generations differ in which sampling parameters they accept: newer models
        reject an explicit ``temperature`` outright. Rather than fail the run, a rejected
        parameter is dropped and the request is reissued immediately. Determinism is
        preserved wherever the parameter is honoured, and the on-disk cache keeps output
        stable across runs where it is not.
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        if self.config.provider == "azure":
            headers["api-key"] = self.config.api_key

        request_payload = dict(payload)
        url = self.config.endpoint()
        attempt = 0

        while attempt < 3:
            body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
            self.calls += 1
            status, raw = self._send(url, headers, body)

            if status == 200:
                try:
                    data = json.loads(raw)
                    content = data["choices"][0]["message"]["content"]
                except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
                    LOGGER.warning("Unexpected response shape from the model: %s", error)
                    return None
                self._record_usage(data.get("usage"))
                return content

            if status == 400 and self._degrade(request_payload, raw):
                # A parameter was removed; retry at once rather than burning an attempt.
                continue

            attempt += 1
            if attempt >= 3 or status in {401, 403, 404}:
                LOGGER.warning(
                    "Model request failed (HTTP %s): %s", status, _summarise_error(raw)
                )
                return None

            wait = 2**attempt
            LOGGER.debug("Model request failed (HTTP %s); retrying in %ss", status, wait)
            time.sleep(wait)
        return None

    def _record_usage(self, usage: Any) -> None:
        """Accumulate the token counts reported alongside a successful response.

        Field names differ between API generations, so both the chat-completions
        (``prompt_tokens``) and the newer (``input_tokens``) spellings are accepted.
        A response without a usage block is counted as a request but no tokens.
        """
        self.usage.requests += 1
        if not isinstance(usage, dict):
            return

        def count(*names: str) -> int:
            for name in names:
                if usage.get(name) is not None:
                    return _safe_int(str(usage[name]), 0)
            return 0

        prompt = count("prompt_tokens", "input_tokens")
        completion = count("completion_tokens", "output_tokens")
        self.usage.prompt_tokens += prompt
        self.usage.completion_tokens += completion
        # Accumulate the total per response. Deriving it at the end would understate the
        # figure if any single response omitted its usage block.
        self.usage.reported_total_tokens += count("total_tokens") or (prompt + completion)

        output_details = usage.get("completion_tokens_details") or usage.get(
            "output_tokens_details"
        )
        if isinstance(output_details, dict):
            self.usage.reasoning_tokens += _safe_int(
                str(output_details.get("reasoning_tokens", 0)), 0
            )

        input_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
        if isinstance(input_details, dict):
            self.usage.cached_prompt_tokens += _safe_int(
                str(input_details.get("cached_tokens", 0)), 0
            )

    # Parameters that may be dropped if the model rejects them, in removal order.
    _OPTIONAL_PARAMETERS = ("temperature", "response_format", "top_p", "seed")

    def _degrade(self, payload: Dict[str, Any], error_body: str) -> bool:
        """Remove a parameter the model rejected. True when the request is worth retrying."""
        message = error_body.lower()
        if not any(
            hint in message
            for hint in ("unsupported", "not supported", "unrecognized", "invalid_request", "does not support")
        ):
            return False
        for parameter in self._OPTIONAL_PARAMETERS:
            if parameter in payload and parameter in message:
                payload.pop(parameter)
                LOGGER.info(
                    "Model %s rejected '%s'; reissuing the request without it",
                    self.config.model,
                    parameter,
                )
                return True
        return False

    def _send(self, url: str, headers: Dict[str, str], body: bytes) -> Tuple[int, str]:
        """Perform one HTTP request, returning the status code and the raw body.

        Transport failures are reported as status 0 so the caller can treat them as
        retryable without distinguishing exception types across the two backends.
        """
        if _requests is not None:
            try:
                response = _requests.post(
                    url, headers=headers, data=body, timeout=self.config.timeout
                )
            except Exception as error:  # noqa: BLE001 - connection and timeout errors
                return 0, str(error)
            return response.status_code, response.text

        from urllib import error as urllib_error
        from urllib import request as urllib_request

        req = urllib_request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib_request.urlopen(req, timeout=self.config.timeout) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as error:
            return error.code, error.read().decode("utf-8", errors="replace")
        except Exception as error:  # noqa: BLE001 - connection and timeout errors
            return 0, str(error)

    def close(self) -> None:
        """Persist the cache so a repeated run makes no further requests."""
        if not self._dirty:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _summarise_error(body: str) -> str:
    """Reduce an API error body to its message, falling back to a truncated payload."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return normalise_text(body)[:300]
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict) and error.get("message"):
            return normalise_text(str(error["message"]))[:300]
        if isinstance(error, str):
            return normalise_text(error)[:300]
    return normalise_text(body)[:300]


def _extract_json_object(content: str) -> Dict[str, Any]:
    """Pull a JSON object out of a model reply that may be wrapped in code fences."""
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ===========================================================================
# Translation engine
# ===========================================================================

class TranslationEngine:
    """Renders source-language purchase text in English.

    The cascade is deliberate and ordered by cost and determinism: controlled
    vocabulary first, offline machine translation second, language model last.
    """

    def __init__(
        self,
        lexicon: Lexicon,
        translator: Optional[MachineTranslator] = None,
        coverage_floor: float = 0.6,
    ) -> None:
        self.lexicon = lexicon
        self.translator = translator
        self.coverage_floor = coverage_floor
        self._llm_overrides: Dict[str, str] = {}

    def register_overrides(self, overrides: Dict[str, str]) -> None:
        """Supply model answers gathered in the batched top-up pass."""
        self._llm_overrides.update(overrides)

    def translate(self, text: str, language: str) -> TranslationResult:
        """Translate a phrase, reporting how much of it the vocabulary could resolve.

        The vocabulary pass always runs, even for text identified as English. Language
        identification on a single upper-case word is unreliable, and short-circuiting
        on a wrong answer would silently leave source-language text untranslated. Text
        that the vocabulary genuinely cannot touch is returned unchanged, with its
        original casing intact.
        """
        cleaned = normalise_text(text)
        if not cleaned:
            return TranslationResult()

        override = self._llm_overrides.get(cleaned)
        if override:
            return TranslationResult(english=override, method="language_model", coverage=1.0)

        key = lookup_key(cleaned)
        if key in self.lexicon.phrases:
            return TranslationResult(
                english=self.lexicon.phrases[key], method="vocabulary_phrase", coverage=1.0
            )

        original_case = self._case_map(cleaned)
        segments, matched_phrase = self._apply_phrases(key)

        fragments: List[Tuple[List[str], bool]] = []
        unresolved: List[str] = []
        content_total = 0
        content_resolved = 0
        used_translator = False

        for segment, already_english in segments:
            if already_english:
                fragments.append((segment.split(), True))
                continue

            words: List[str] = []
            for token in segment.split():
                if not _WORD.fullmatch(token):
                    if not is_code_token(token):
                        words.append(original_case.get(token, token))
                    continue
                if token in self.lexicon.stopwords:
                    continue
                content_total += 1
                resolved = self.lexicon.resolve_token(token)
                if resolved is None and self.translator is not None:
                    resolved = self.translator.translate(token, language)
                    if resolved:
                        used_translator = True
                if resolved:
                    content_resolved += 1
                    words.append(resolved)
                else:
                    unresolved.append(token)
                    words.append(original_case.get(token, token))
            if words:
                fragments.append((words, False))

        english = self._join(self._drop_redundant(fragments))
        coverage = 1.0 if content_total == 0 else content_resolved / content_total

        if content_resolved == 0 and not matched_phrase:
            # Nothing in the vocabulary applied. Return the source text untouched rather
            # than a lower-cased approximation of it.
            is_english = language == "en"
            return TranslationResult(
                english=cleaned,
                method="passthrough" if is_english else "source_text",
                coverage=1.0 if is_english else 0.0,
                unresolved=sorted(set(unresolved)),
            )

        if used_translator:
            method = "vocabulary+machine_translation"
        elif not unresolved:
            method = "vocabulary"
        else:
            method = "vocabulary_partial"

        return TranslationResult(
            english=english,
            method=method,
            coverage=round(coverage, 3),
            unresolved=sorted(set(unresolved)),
        )

    @staticmethod
    def _case_map(text: str) -> Dict[str, str]:
        """Map lower-cased tokens back to their original casing.

        Lookup is case-insensitive, but anything the vocabulary cannot translate should
        reach the output exactly as the source system wrote it.
        """
        mapping: Dict[str, str] = {}
        for token in text.split():
            stripped = token.strip(",.;:()[]\"'")
            if stripped:
                mapping.setdefault(stripped.lower(), stripped)
        return mapping

    @staticmethod
    def _singular(word: str) -> str:
        """Crude singular form, sufficient for comparing English fragments."""
        lowered = word.lower()
        if len(lowered) > 3 and lowered.endswith("s") and not lowered.endswith("ss"):
            return lowered[:-1]
        return lowered

    def _drop_redundant(self, fragments: Sequence[Tuple[List[str], bool]]) -> List[str]:
        """Remove word-level fragments already covered by a matched phrase.

        Source systems commonly prefix a line with its own category, as in
        "VERO Ajoneuvovero" or "RENKAAT Renkaiden sailytys". Translating both parts
        yields "tax vehicle tax"; dropping the prefix once the phrase already conveys it
        yields "vehicle tax".
        """
        phrase_words = {
            self._singular(word)
            for words, is_phrase in fragments
            if is_phrase
            for word in words
        }
        kept: List[str] = []
        for words, is_phrase in fragments:
            if not is_phrase and phrase_words:
                if {self._singular(word) for word in words} <= phrase_words:
                    continue
            kept.append(" ".join(words))
        return kept

    def _apply_phrases(self, key: str) -> Tuple[List[Tuple[str, bool]], bool]:
        """Replace known multi-word expressions, returning ordered text segments.

        Each segment is flagged to record whether it is already English, so the
        token pass does not attempt to translate vocabulary output a second time.
        """
        segments: List[Tuple[str, bool]] = [(key, False)]
        matched = False
        for phrase in self.lexicon.phrase_order:
            if not phrase or phrase not in key:
                continue
            english = self.lexicon.phrases[phrase]
            rebuilt: List[Tuple[str, bool]] = []
            for segment, already_english in segments:
                if already_english or phrase not in segment:
                    rebuilt.append((segment, already_english))
                    continue
                matched = True
                head, _, tail = segment.partition(phrase)
                if head.strip():
                    rebuilt.append((head.strip(), False))
                rebuilt.append((english, True))
                if tail.strip():
                    rebuilt.append((tail.strip(), False))
            segments = rebuilt
        return segments, matched

    @staticmethod
    def _join(parts: Sequence[str]) -> str:
        """Concatenate fragments, collapsing immediate repetitions."""
        words: List[str] = []
        for part in parts:
            for word in part.split():
                if words and words[-1].lower() == word.lower():
                    continue
                words.append(word)
        return " ".join(words).strip()

    def needs_model(self, result: TranslationResult) -> bool:
        """True when a phrase is weak enough to justify a model call."""
        return bool(result.unresolved) and result.coverage < self.coverage_floor


# ===========================================================================
# Matching engine
# ===========================================================================

class MatchingEngine:
    """Links a line to comparable lines in the other source systems.

    Three tiers run in order and the first that produces a confident result wins.
    Every candidate keeps its tier and score so a reviewer can tell an exact key match
    apart from a statistical one.
    """

    def __init__(self, records: Sequence[LineRecord], settings: Settings) -> None:
        self.settings = settings
        self._records = {record.row_id: record for record in records}

        self._by_document: Dict[str, List[str]] = defaultdict(list)
        self._by_item: Dict[str, List[str]] = defaultdict(list)
        self._by_po: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        self._by_supplier: Dict[str, List[str]] = defaultdict(list)
        self._by_amount: Dict[str, List[str]] = defaultdict(list)
        self._by_month: Dict[str, List[str]] = defaultdict(list)

        semantic_documents: Dict[str, str] = {}
        for record in records:
            if not record.is_line:
                continue
            logical = record.logical
            row_id = record.row_id

            for field_name in ("document_id", "po_number"):
                key = compact_key(logical.get(field_name))
                if key:
                    self._by_document[key].append(row_id)

            item_code = compact_key(logical.get("item_code"))
            if item_code:
                self._by_item[item_code].append(row_id)

            po_number = compact_key(logical.get("po_number"))
            po_line = compact_key(logical.get("po_line_number"))
            if po_number and po_line:
                self._by_po[(po_number, po_line)].append(row_id)

            supplier = compact_key(logical.get("supplier_name"))
            if supplier:
                self._by_supplier[supplier].append(row_id)

            amount = parse_amount(logical.get("amount"))
            if amount is not None:
                self._by_amount[f"{amount:.2f}"].append(row_id)

            date = parse_date(logical.get("document_date"))
            if date:
                self._by_month[date[:7]].append(row_id)

            text = record.search_text
            if text:
                semantic_documents[row_id] = text

        self._semantic = CharNgramIndex(semantic_documents)
        LOGGER.info("Reference index built over %d searchable lines", len(semantic_documents))

    def match(self, record: LineRecord) -> List[MatchCandidate]:
        """Return the best cross-system candidates for one line, strongest first."""
        if not record.is_line:
            return []
        candidates: Dict[str, MatchCandidate] = {}

        for candidate in self._tier_a(record):
            candidates.setdefault(candidate.row_id, candidate)
        for candidate in self._tier_b(record):
            candidates.setdefault(candidate.row_id, candidate)
        for candidate in self._tier_c(record):
            candidates.setdefault(candidate.row_id, candidate)

        ranked = sorted(
            candidates.values(),
            key=lambda item: (-item.score, item.tier, item.row_id),
        )
        return ranked[: self.settings.top_k_matches]

    def _eligible(self, record: LineRecord, row_id: str) -> Optional[LineRecord]:
        """Reject self-matches and same-system matches, which add no information."""
        other = self._records.get(row_id)
        if other is None or other.row_id == record.row_id:
            return None
        if other.source_key == record.source_key:
            return None
        return other if other.is_line else None

    def _describe(self, other: LineRecord, tier: str, method: str, score: float) -> MatchCandidate:
        return MatchCandidate(
            row_id=other.row_id,
            source_key=other.source_key,
            tier=tier,
            method=method,
            score=score,
            po_number=other.logical.get("po_number", ""),
            po_line=other.logical.get("po_line_number", ""),
            supplier=other.logical.get("supplier_name", ""),
            text=other.translation.english or other.normalised_description,
        )

    def _tier_a(self, record: LineRecord) -> List[MatchCandidate]:
        """Deterministic joins on shared identifiers."""
        found: List[MatchCandidate] = []
        logical = record.logical

        po_number = compact_key(logical.get("po_number"))
        po_line = compact_key(logical.get("po_line_number"))
        if po_number and po_line:
            for row_id in self._by_po.get((po_number, po_line), ()):
                other = self._eligible(record, row_id)
                if other is not None:
                    found.append(self._describe(other, "A", "po_line_key", 1.0))

        for field_name in ("document_id", "po_number"):
            key = compact_key(logical.get(field_name))
            if not key:
                continue
            for row_id in self._by_document.get(key, ()):
                other = self._eligible(record, row_id)
                if other is not None:
                    found.append(self._describe(other, "A", "document_key", 0.95))

        item_code = compact_key(logical.get("item_code"))
        if item_code and len(item_code) >= 3:
            for row_id in self._by_item.get(item_code, ()):
                other = self._eligible(record, row_id)
                if other is not None:
                    found.append(self._describe(other, "A", "item_code", 0.9))
        return found

    def _tier_b(self, record: LineRecord) -> List[MatchCandidate]:
        """Blocked fuzzy matching on supplier, amount, period and text."""
        pool: set = set()
        logical = record.logical

        supplier = compact_key(logical.get("supplier_name"))
        if supplier:
            pool.update(self._by_supplier.get(supplier, ()))

        amount = parse_amount(logical.get("amount"))
        if amount is not None:
            pool.update(self._by_amount.get(f"{amount:.2f}", ()))

        date = parse_date(logical.get("document_date"))
        if date and len(pool) < 400:
            pool.update(self._by_month.get(date[:7], ()))

        found: List[MatchCandidate] = []
        for row_id in sorted(pool):
            other = self._eligible(record, row_id)
            if other is None:
                continue
            score = self._score_pair(record, other)
            if score >= self.settings.fuzzy_threshold:
                found.append(self._describe(other, "B", "blocked_fuzzy", score))
        return found

    def _tier_c(self, record: LineRecord) -> List[MatchCandidate]:
        """Character n-gram retrieval for wording that differs across systems."""
        text = record.search_text
        if not text:
            return []
        found: List[MatchCandidate] = []
        for row_id, score in self._semantic.query(
            text, top_k=self.settings.top_k_matches * 3, min_score=self.settings.semantic_threshold
        ):
            other = self._eligible(record, row_id)
            if other is not None:
                found.append(self._describe(other, "C", "semantic_ngram", round(score, 4)))
        return found

    def _score_pair(self, left: LineRecord, right: LineRecord) -> float:
        """Blend the available comparison signals, renormalised over what exists."""
        signals: List[Tuple[float, float]] = []

        text = max(
            text_similarity(left.normalised_description, right.normalised_description),
            text_similarity(left.translation.english, right.translation.english),
        )
        signals.append((0.55, text))

        left_amount = parse_amount(left.logical.get("amount"))
        right_amount = parse_amount(right.logical.get("amount"))
        if left_amount is not None and right_amount is not None:
            signals.append((0.25, 1.0 if amounts_agree(left_amount, right_amount) else 0.0))

        left_supplier = normalise_text(left.logical.get("supplier_name"))
        right_supplier = normalise_text(right.logical.get("supplier_name"))
        if left_supplier and right_supplier:
            supplier_score = (
                1.0
                if compact_key(left_supplier) == compact_key(right_supplier)
                else text_similarity(left_supplier, right_supplier)
            )
            signals.append((0.20, supplier_score))

        total_weight = sum(weight for weight, _ in signals)
        if total_weight == 0.0:
            return 0.0
        return round(sum(weight * value for weight, value in signals) / total_weight, 4)


# ===========================================================================
# Description synthesis
# ===========================================================================

_CONNECTIVES = {"and", "or", "of", "for", "with", "per", "the", "a", "an", "in", "on", "to"}


@dataclass
class DescriptionResult:
    description: str = ""
    short: str = ""
    kind: str = "Unknown"
    method: str = "none"
    evidence: List[str] = field(default_factory=list)


class DescriptionSynthesiser:
    """Assembles the final English description from validated evidence.

    Composition is templated rather than generated. Every content word in the result
    must be present in the evidence or in the controlled vocabulary; anything else is
    discarded, which is what makes the "no invented information" guarantee hold.
    """

    def __init__(self, lexicon: Lexicon, max_words: int = 12) -> None:
        self.lexicon = lexicon
        self.max_words = max_words

    def build(
        self,
        record: LineRecord,
        document_context: str = "",
    ) -> DescriptionResult:
        evidence: List[str] = []
        head = record.translation.english.strip()
        if head:
            evidence.append(f"{record.source_key}:{record.profile.primary_text[0]}"
                            if record.profile.primary_text else record.source_key)

        best_match = record.matches[0] if record.matches else None
        if not head and best_match is not None and best_match.text:
            head = best_match.text.strip()
            evidence.append(f"{best_match.source_key}:matched_line")

        category = self._category_leaf(record)
        material_group = normalise_text(record.logical.get("material_group"))
        account = normalise_text(record.logical.get("account_name"))

        if not head:
            head = category or material_group or account
            if head:
                evidence.append("classification")

        if not head:
            return DescriptionResult(method="insufficient_evidence")

        concept = self.lexicon.concept_for(head)
        label = str(concept["label"]) if concept and concept.get("label") else sentence_case(head)
        if concept:
            evidence.append("vocabulary:concept")

        qualifier = ""
        for candidate in (category, material_group):
            if candidate and self._adds_information(candidate, label):
                qualifier = candidate.strip()
                evidence.append("classification")
                break

        context = ""
        if document_context and self._adds_information(document_context, f"{label} {qualifier}"):
            context = document_context.strip()
            evidence.append("document_header")

        if (
            not qualifier
            and best_match is not None
            and best_match.text
            and self._adds_information(best_match.text, label)
        ):
            qualifier = best_match.text.strip()
            evidence.append(f"{best_match.source_key}:matched_line")

        description = self._compose(label, qualifier, context)
        description = self._enforce_vocabulary(description, record, document_context)
        description = self._trim(description)

        short = str(concept["label"]) if concept and concept.get("label") else sentence_case(
            self._trim(label, max_words=5)
        )
        kind = self.lexicon.classify_kind(f"{label} {qualifier}")
        if kind == "Unknown":
            kind = self._infer_kind_from_structure(record)

        method = record.translation.method
        if best_match is not None and f"{best_match.source_key}:matched_line" in evidence:
            method = f"{method}+cross_source"

        return DescriptionResult(
            description=description,
            short=short,
            kind=kind,
            method=method,
            evidence=sorted(set(evidence)),
        )

    @staticmethod
    def _category_leaf(record: LineRecord) -> str:
        """Return the most specific populated classification level for a line."""
        for level in ("category_l4", "category_l3", "category_l2", "category_l1"):
            value = normalise_text(record.logical.get(level))
            if value and value.lower() not in NULL_TOKENS:
                return value
        return ""

    @staticmethod
    def _compose(label: str, qualifier: str, context: str) -> str:
        text = sentence_case(label)
        if qualifier:
            text = f"{text} - {qualifier}"
        if context:
            text = f"{text} ({context})"
        return _WHITESPACE.sub(" ", text).strip()

    @staticmethod
    def _adds_information(candidate: str, existing: str) -> bool:
        """True when a fragment contributes words that are not already present."""
        candidate_tokens = {token for token in tokenise(candidate) if len(token) > 2}
        if not candidate_tokens:
            return False
        existing_tokens = set(tokenise(existing))
        return bool(candidate_tokens - existing_tokens)

    def _enforce_vocabulary(
        self, description: str, record: LineRecord, document_context: str
    ) -> str:
        """Drop any word that cannot be traced to the evidence or the vocabulary."""
        allowed = set(_CONNECTIVES) | self.lexicon.english_vocabulary
        allowed.update(tokenise(record.normalised_description))
        allowed.update(tokenise(record.translation.english))
        allowed.update(tokenise(record.support_description))
        allowed.update(tokenise(record.support_translation.english))
        allowed.update(tokenise(document_context))
        for field_name in (
            "category_l1",
            "category_l2",
            "category_l3",
            "category_l4",
            "material_group",
            "account_name",
            "supplier_name",
        ):
            allowed.update(tokenise(record.logical.get(field_name, "")))
        for match in record.matches:
            allowed.update(tokenise(match.text))

        kept: List[str] = []
        for word in description.split():
            core = "".join(char for char in word if char.isalpha() or char in "-").lower()
            if not core or core in allowed or not _WORD.search(core):
                kept.append(word)
                continue
            if any(part in allowed for part in core.split("-") if part):
                kept.append(word)
        rebuilt = " ".join(kept)
        rebuilt = re.sub(r"\(\s*\)", "", rebuilt)
        rebuilt = re.sub(r"\s*-\s*$", "", rebuilt.strip())
        return _WHITESPACE.sub(" ", rebuilt).strip()

    def _trim(self, text: str, max_words: Optional[int] = None) -> str:
        """Shorten to the configured word budget, dropping trailing detail first."""
        limit = max_words or self.max_words
        words = text.split()
        if len(words) <= limit:
            return text
        trimmed = " ".join(words[:limit])
        # Never leave an unbalanced parenthesis behind.
        if trimmed.count("(") > trimmed.count(")"):
            trimmed = trimmed[: trimmed.rfind("(")].strip()
        return re.sub(r"\s*[-,]\s*$", "", trimmed).strip()

    @staticmethod
    def _infer_kind_from_structure(record: LineRecord) -> str:
        """Fall back to structural cues when the wording carries no marker."""
        item_code = normalise_text(record.logical.get("item_code"))
        quantity = parse_amount(record.logical.get("quantity"))
        unit_price = parse_amount(record.logical.get("unit_price"))
        if item_code and quantity is not None and quantity > 1:
            return "Material"
        if unit_price is not None and quantity == 1 and not item_code:
            return "Service"
        return "Unknown"


# ===========================================================================
# Confidence scoring
# ===========================================================================

_INFORMATIVE_FIELDS = (
    "supplier_name",
    "item_code",
    "amount",
    "document_date",
    "category_l1",
    "material_group",
    "account_name",
)


def score_confidence(record: LineRecord, description: DescriptionResult) -> Tuple[int, str, Dict[str, float]]:
    """Combine the independent quality signals into a 0-100 score.

    The score is derived from measurable properties of the pipeline rather than from a
    model's self-assessment, so it can be audited and reproduced.
    """
    components: Dict[str, float] = {}

    components["lexical_coverage"] = round(record.translation.coverage, 3)

    tier_strength = {"A": 1.0, "B": 0.7, "C": 0.45}
    best = record.matches[0] if record.matches else None
    components["evidence_strength"] = tier_strength.get(best.tier, 0.25) if best else 0.25

    distinct_sources = {match.source_key for match in record.matches}
    components["cross_source_agreement"] = min(1.0, len(distinct_sources) / 2.0)

    populated = sum(1 for name in _INFORMATIVE_FIELDS if not is_blank(record.logical.get(name)))
    components["field_completeness"] = round(populated / len(_INFORMATIVE_FIELDS), 3)

    if len(record.matches) >= 2:
        margin = record.matches[0].score - record.matches[1].score
        components["decisiveness"] = round(min(1.0, max(0.0, margin * 2.0)), 3)
    elif record.matches:
        components["decisiveness"] = 1.0
    else:
        components["decisiveness"] = 0.5

    weights = {
        "lexical_coverage": 0.35,
        "evidence_strength": 0.20,
        "cross_source_agreement": 0.15,
        "field_completeness": 0.15,
        "decisiveness": 0.15,
    }
    score = sum(weights[name] * components[name] for name in weights)

    if not description.description:
        score *= 0.3
    elif description.method == "insufficient_evidence":
        score *= 0.4

    value = int(round(max(0.0, min(1.0, score)) * 100))
    band = next(name for threshold, name in CONFIDENCE_BANDS if value >= threshold)
    return value, band, components


# ===========================================================================
# Pipeline
# ===========================================================================

class Agent1:
    """Orchestrates ingestion, enrichment and output for the purchase-description agent."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lexicon = Lexicon.load(settings.lexicon_file)
        self.translator = MachineTranslator(settings.use_machine_translation)
        self.engine = TranslationEngine(self.lexicon, self.translator)
        self.synthesiser = DescriptionSynthesiser(self.lexicon, settings.max_description_words)
        self.model = LanguageModelClient(
            settings.model, settings.cache_dir / "agent1_model_cache.json"
        )
        self.tables: List[Tuple[Table, SourceProfile]] = []
        self.records: List[LineRecord] = []
        self.run_id = ""
        self.stats: Dict[str, Any] = {}

    # -- orchestration ------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        """Execute the full pipeline and return the run manifest."""
        files = self._discover_files()
        if not files:
            raise SystemExit("No readable source files were found. Check the paths and try again.")

        self.run_id = self._compute_run_id(files)
        LOGGER.info("Run identifier %s", self.run_id)

        self._ingest(files)
        self._classify_rows()
        self._deduplicate()
        self._prepare_text()
        self._translate()
        self._top_up_with_model()
        self._match()
        self._synthesise()
        return self._write_outputs(files)

    # -- discovery and ingestion -------------------------------------------

    def _discover_files(self) -> List[Path]:
        """Collect every readable source file from the configured locations."""
        candidates: List[Path] = []
        directories = [
            directory
            for directory in (
                self.settings.invoice_dir,
                self.settings.po_dir,
                self.settings.transaction_dir,
            )
            if directory and directory.is_dir()
        ]

        if not directories and self.settings.sources_root.is_dir():
            LOGGER.info("Named folders not found; scanning %s", self.settings.sources_root)
            directories = [self.settings.sources_root]

        for directory in directories:
            for path in sorted(directory.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".xlsx", ".csv", ".tsv"}:
                    if not path.name.startswith((".", "~$")):
                        candidates.append(path)

        catalogue = self.settings.catalogue_file
        if catalogue and catalogue.is_file():
            candidates.append(catalogue)

        unique: List[Path] = []
        seen: set = set()
        for path in candidates:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(path)
        return sorted(unique, key=lambda item: str(item).lower())

    def _compute_run_id(self, files: Sequence[Path]) -> str:
        """Derive a stable identifier from input contents and configuration."""
        digest = hashlib.sha256()
        digest.update(f"{__version__}|{self.lexicon.version}".encode("utf-8"))
        digest.update(
            f"{self.settings.fuzzy_threshold}|{self.settings.semantic_threshold}"
            f"|{self.settings.top_k_matches}|{self.settings.max_description_words}"
            f"|llm={self.settings.model.enabled}|mt={self.translator.available}".encode("utf-8")
        )
        for path in files:
            digest.update(path.name.encode("utf-8"))
            digest.update(sha256_file(path).encode("utf-8"))
        return digest.hexdigest()[:16]

    def _ingest(self, files: Sequence[Path]) -> None:
        """Read every file, identify its source system and build line records."""
        for path in files:
            for table in read_table_file(path):
                if not table.rows:
                    LOGGER.warning("No data rows in %s [%s]", path.name, table.sheet)
                    continue
                profile, confidence = detect_profile(table)
                self.tables.append((table, profile))
                LOGGER.info(
                    "%-34s %-26s %4d rows (layout match %.0f%%)",
                    path.name,
                    profile.label,
                    len(table.rows),
                    confidence * 100,
                )
                self._build_records(table, profile)

        if not self.records:
            raise SystemExit("Source files were read but contained no usable rows.")
        LOGGER.info("Ingested %d rows in total", len(self.records))

    def _build_records(self, table: Table, profile: SourceProfile) -> None:
        """Materialise one LineRecord per source row."""
        volatile = {_normalise_column(name) for name in profile.volatile_columns}
        table_key = f"{table.path.resolve()}::{table.sheet}"
        for index, row in enumerate(table.rows, start=1):
            logical = self._map_logical_fields(row, profile)
            content_hash = self._content_hash(row, volatile)
            row_id = f"{profile.key}:{table.path.stem}:{table.sheet}:{index:06d}"
            self.records.append(
                LineRecord(
                    row_id=row_id,
                    source_key=profile.key,
                    source_label=profile.label,
                    source_file=table.path.name,
                    source_sheet=table.sheet,
                    table_key=table_key,
                    row_index=index,
                    raw=row,
                    logical=logical,
                    profile=profile,
                    content_hash=content_hash,
                )
            )

    @staticmethod
    def _map_logical_fields(row: Dict[str, str], profile: SourceProfile) -> Dict[str, str]:
        """Project source columns onto the common logical schema.

        Column names are compared case- and punctuation-insensitively so that minor
        header drift between deliveries does not break the mapping.
        """
        normalised_row = {_normalise_column(name): value for name, value in row.items()}
        logical: Dict[str, str] = {}
        for logical_name, columns in profile.aliases.items():
            for column in columns:
                value = normalised_row.get(_normalise_column(column), "")
                if not is_blank(value):
                    logical[logical_name] = normalise_text(value)
                    break
            logical.setdefault(logical_name, "")

        for level in ("category_l1", "category_l2", "category_l3", "category_l4"):
            logical.setdefault(level, "")
        for name in ("po_number", "po_line_number", "supplier_name", "supplier_code",
                     "quantity", "unit_price", "amount", "currency", "document_date",
                     "material_group", "account_name", "item_code", "document_id",
                     "line_number"):
            logical.setdefault(name, "")
        return logical

    @staticmethod
    def _content_hash(row: Dict[str, str], volatile: set) -> str:
        """Hash a row excluding volatile columns such as the source file name.

        This is what allows the same invoice delivered under two file names to be
        recognised as one logical line.
        """
        payload = {
            name: normalise_text(value)
            for name, value in sorted(row.items())
            if _normalise_column(name) not in volatile
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]

    # -- structural analysis ------------------------------------------------

    @staticmethod
    def _document_key(record: LineRecord) -> Tuple[str, ...]:
        """Identify the physical document block a row belongs to.

        The document identifier alone is not sufficient: the same invoice is delivered
        under two file names in the same extract, and both blocks carry their own
        header and total rows. Including the volatile columns keeps the blocks apart
        during structural analysis, while the content hash still recognises them as
        duplicates afterwards.
        """
        normalised_row = {_normalise_column(name): value for name, value in record.raw.items()}
        volatile = tuple(
            normalise_text(normalised_row.get(_normalise_column(column), ""))
            for column in record.profile.volatile_columns
        )
        return (record.table_key, record.logical.get("document_id", "")) + volatile

    def _classify_rows(self) -> None:
        """Separate genuine purchase lines from headers, subtotals and totals.

        Invoice extracts interleave a document header, the priced lines, a grand total
        and per-article subtotals. Enriching all of them would inflate every downstream
        count, so each row is typed and only true lines are described.
        """
        counts: Counter = Counter()
        grouped: Dict[Tuple[str, ...], List[LineRecord]] = defaultdict(list)
        for record in self.records:
            if record.profile.structural_rows:
                grouped[self._document_key(record)].append(record)
            else:
                counts[record.row_type] += 1

        for group in grouped.values():
            seen_lines: Dict[Tuple[str, str], LineRecord] = {}
            header_assigned = False
            for record in group:
                text = self._primary_text(record)
                amount = parse_amount(record.logical.get("amount"))
                signature = (lookup_key(text), f"{amount:.2f}" if amount is not None else "")

                if not is_blank(record.logical.get("line_number")):
                    record.row_type = "LINE"
                    seen_lines.setdefault(signature, record)
                elif self.lexicon.looks_like_total(text):
                    record.row_type = "TOTAL"
                elif not header_assigned:
                    record.row_type = "HEADER"
                    header_assigned = True
                elif signature in seen_lines:
                    record.row_type = "SUBTOTAL"
                else:
                    record.row_type = "LINE"
                    seen_lines.setdefault(signature, record)
                counts[record.row_type] += 1

        self.stats["row_types"] = dict(sorted(counts.items()))
        LOGGER.info("Row types: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    def _deduplicate(self) -> None:
        """Mark rows that repeat an earlier row's content under a different file name."""
        canonical: Dict[Tuple[str, str], str] = {}
        duplicates = 0
        for record in self.records:
            key = (record.source_key, record.content_hash)
            existing = canonical.get(key)
            if existing is None:
                canonical[key] = record.row_id
            else:
                record.duplicate_of = existing
                duplicates += 1
        self.stats["duplicate_rows"] = duplicates
        if duplicates:
            LOGGER.info("Flagged %d duplicate rows (content identical to an earlier row)", duplicates)

    def _primary_text(self, record: LineRecord) -> str:
        """Return the descriptive text for a row, honouring the profile's field order."""
        normalised_row = {_normalise_column(name): value for name, value in record.raw.items()}
        for column in record.profile.primary_text:
            value = normalised_row.get(_normalise_column(column), "")
            if not is_blank(value):
                return normalise_text(value)
        return ""

    def _support_text(self, record: LineRecord) -> str:
        """Concatenate the secondary descriptive fields declared by the profile."""
        normalised_row = {_normalise_column(name): value for name, value in record.raw.items()}
        parts: List[str] = []
        for column in record.profile.support_text:
            value = normalised_row.get(_normalise_column(column), "")
            if not is_blank(value):
                parts.append(normalise_text(value))
        return " ".join(parts)

    # -- text preparation ---------------------------------------------------

    def _prepare_text(self) -> None:
        """Extract, clean and language-tag the descriptive text of every row."""
        for record in self.records:
            record.raw_description = self._primary_text(record)
            record.support_description = self._support_text(record)
            record.normalised_description = normalise_text(record.raw_description)
            combined = f"{record.normalised_description} {record.support_description}".strip()
            record.language, record.language_confidence = detect_language(combined, self.lexicon)

        languages = Counter(record.language for record in self.records if record.is_line)
        self.stats["languages"] = dict(sorted(languages.items()))
        LOGGER.info("Languages detected: %s", ", ".join(f"{k}={v}" for k, v in sorted(languages.items())))

    def _translate(self) -> None:
        """Render every descriptive field in English using the deterministic cascade."""
        for record in self.records:
            record.translation = self.engine.translate(
                record.normalised_description, record.language
            )
            if record.support_description:
                record.support_translation = self.engine.translate(
                    record.support_description, record.language
                )

        methods = Counter(record.translation.method for record in self.records if record.is_line)
        self.stats["translation_methods"] = dict(sorted(methods.items()))

    def _top_up_with_model(self) -> None:
        """Send only the phrases the vocabulary could not resolve to the model.

        Batching over unique phrases means cost scales with vocabulary gaps rather than
        with row count, and the on-disk cache makes any repeat run free.
        """
        if not self.model.available:
            if self.settings.use_llm:
                LOGGER.warning(
                    "Language-model fallback requested but not configured; "
                    "continuing with vocabulary and machine translation only"
                )
            self.stats["model_phrases"] = 0
            return

        pending: Dict[str, str] = {}
        for record in self.records:
            if not record.is_line or not record.normalised_description:
                continue
            if self.engine.needs_model(record.translation):
                pending.setdefault(record.normalised_description, record.language)

        if not pending:
            LOGGER.info("Vocabulary resolved every phrase; no model calls required")
            self.stats["model_phrases"] = 0
            return

        LOGGER.info("Resolving %d unique phrases with the language model", len(pending))
        answers = self.model.translate_phrases(sorted(pending.items()))
        if answers:
            self.engine.register_overrides(answers)
            for record in self.records:
                if record.normalised_description in answers:
                    record.translation = self.engine.translate(
                        record.normalised_description, record.language
                    )
        self.model.close()
        self.stats["model_phrases"] = len(pending)
        self.stats["model_attempts"] = self.model.calls
        self.stats["token_usage"] = self.model.usage.as_dict()

        usage = self.model.usage
        LOGGER.info(
            "Token usage: input=%s output=%s (reasoning=%s) total=%s across %s request(s), "
            "%s phrase(s) served from cache",
            f"{usage.prompt_tokens:,}",
            f"{usage.completion_tokens:,}",
            f"{usage.reasoning_tokens:,}",
            f"{usage.total_tokens:,}",
            usage.requests,
            usage.cache_hits,
        )

    # -- matching and synthesis --------------------------------------------

    def _match(self) -> None:
        """Link each line to comparable lines in the other source systems."""
        matcher = MatchingEngine(self.records, self.settings)
        tiers: Counter = Counter()
        for record in self.records:
            record.matches = matcher.match(record)
            tiers[record.matches[0].tier if record.matches else "none"] += 1
        self.stats["match_tiers"] = dict(sorted(tiers.items()))
        LOGGER.info("Match tiers: %s", ", ".join(f"{k}={v}" for k, v in sorted(tiers.items())))

    def _document_contexts(self) -> Dict[Tuple[str, ...], str]:
        """Map each document block to the English text of its header row.

        The header of a vehicle-leasing invoice names the asset the priced lines relate
        to, which is exactly the context a line such as "VUOKRA" is missing.
        """
        contexts: Dict[Tuple[str, ...], str] = {}
        for record in self.records:
            if record.row_type != "HEADER":
                continue
            text = record.translation.english or record.normalised_description
            if text:
                contexts[self._document_key(record)] = text
        return contexts

    def _synthesise(self) -> None:
        """Produce the final description and confidence score for every row."""
        contexts = self._document_contexts()
        by_id = {record.row_id: record for record in self.records}
        bands: Counter = Counter()

        for record in self.records:
            if record.duplicate_of:
                source = by_id.get(record.duplicate_of)
                if source is not None and source.description:
                    record.description = source.description
                    record.description_short = source.description_short
                    record.item_or_service = source.item_or_service
                    record.confidence = source.confidence
                    record.confidence_band = source.confidence_band
                    record.confidence_components = dict(source.confidence_components)
                    record.enrichment_method = "inherited_from_duplicate"
                    bands[record.confidence_band] += 1
                    continue

            if not record.is_line:
                record.enrichment_method = f"skipped_{record.row_type.lower()}"
                continue

            context = contexts.get(self._document_key(record), "")
            result = self.synthesiser.build(record, context)

            record.description = result.description
            record.description_short = result.short
            record.item_or_service = result.kind
            record.enrichment_method = result.method
            confidence, band, components = score_confidence(record, result)
            record.confidence = confidence
            record.confidence_band = band
            record.confidence_components = components
            bands[band] += 1

        self.stats["confidence_bands"] = dict(sorted(bands.items()))
        LOGGER.info("Confidence: %s", ", ".join(f"{k}={v}" for k, v in sorted(bands.items())))

    # -- output -------------------------------------------------------------

    def _enrichment_row(self, record: LineRecord) -> Dict[str, str]:
        """Flatten a record's derived fields into the appended output columns."""
        best = record.matches[0] if record.matches else None
        return {
            "Row_Id": record.row_id,
            "Row_Type": record.row_type,
            "Is_Duplicate": "Yes" if record.duplicate_of else "No",
            "Duplicate_Of": record.duplicate_of,
            "Source_Description_Raw": record.raw_description,
            "Source_Description_Normalized": record.normalised_description,
            "Detected_Language": record.language,
            "Language_Confidence": f"{record.language_confidence:.3f}",
            "Enriched_Purchase_Description": record.description,
            "Enriched_Description_Short": record.description_short,
            "Item_Or_Service": record.item_or_service,
            "Translation_Method": record.translation.method,
            "Translation_Coverage": f"{record.translation.coverage:.3f}",
            "Unresolved_Tokens": "; ".join(record.translation.unresolved),
            "Evidence_Sources": "; ".join(
                sorted({match.source_key for match in record.matches})
            ),
            "Matched_Source_System": best.source_key if best else "",
            "Matched_Row_Id": best.row_id if best else "",
            "Matched_PO_Number": best.po_number if best else "",
            "Matched_PO_Line": best.po_line if best else "",
            "Matched_Supplier": best.supplier if best else "",
            "Match_Tier": best.tier if best else "",
            "Match_Method": best.method if best else "",
            "Match_Score": f"{best.score:.4f}" if best else "",
            "Enrichment_Method": record.enrichment_method,
            "AI_Confidence": str(record.confidence),
            "Confidence_Band": record.confidence_band,
            "Agent_Version": __version__,
            "Lexicon_Version": self.lexicon.version,
            "Run_Id": self.run_id,
        }

    def _unified_row(self, record: LineRecord) -> Dict[str, str]:
        """Build one row of the common table consumed by the downstream agents."""
        logical = record.logical
        row = {
            "Source_System": record.source_key,
            "Source_File": record.source_file,
            "Source_Sheet": record.source_sheet,
            "Source_Row_Index": str(record.row_index),
            "Document_Id": logical.get("document_id", ""),
            "Line_Number": logical.get("line_number", ""),
            "PO_Number": logical.get("po_number", ""),
            "PO_Line_Number": logical.get("po_line_number", ""),
            "Item_Code": logical.get("item_code", ""),
            "Supplier_Name": logical.get("supplier_name", ""),
            "Supplier_Code": logical.get("supplier_code", ""),
            "Quantity": logical.get("quantity", ""),
            "Unit_Price": logical.get("unit_price", ""),
            "Amount": logical.get("amount", ""),
            "Currency": logical.get("currency", ""),
            "Document_Date": logical.get("document_date", ""),
            "Category_L1": logical.get("category_l1", ""),
            "Category_L2": logical.get("category_l2", ""),
            "Category_L3": logical.get("category_l3", ""),
            "Category_L4": logical.get("category_l4", ""),
            "Material_Group": logical.get("material_group", ""),
            "Account_Name": logical.get("account_name", ""),
        }
        row.update(self._enrichment_row(record))
        return row

    def _write_outputs(self, files: Sequence[Path]) -> Dict[str, Any]:
        """Write per-source files, the unified table, the JSONL export and the manifest."""
        results = self.settings.results_dir
        results.mkdir(parents=True, exist_ok=True)
        written: List[str] = []

        by_table: Dict[str, List[LineRecord]] = defaultdict(list)
        for record in self.records:
            by_table[record.table_key].append(record)

        for table, profile in self.tables:
            if not profile.is_target:
                continue
            records = by_table.get(f"{table.path.resolve()}::{table.sheet}", [])
            if not records:
                continue
            stem = re.sub(r"[^A-Za-z0-9]+", "_", table.path.stem).strip("_").lower()
            sheet_key = re.sub(r"[^a-z0-9]+", "_", table.sheet.lower()).strip("_")
            if sheet_key and sheet_key not in {"sheet1", stem}:
                stem = f"{stem}_{sheet_key}"
            path = results / f"{AGENT_ID}_{stem}.csv"
            self._write_csv(
                path,
                list(table.columns) + list(ENRICHMENT_COLUMNS),
                [
                    {**record.raw, **self._enrichment_row(record)}
                    for record in sorted(records, key=lambda item: item.row_index)
                ],
            )
            written.append(path.name)

        # Reference-only sources (supplier catalogues) take part in matching but are not
        # purchase lines, so they stay out of the table the downstream agents consume.
        ordered = sorted(
            (record for record in self.records if record.profile.is_target),
            key=lambda item: (item.source_key, item.source_file, item.row_index),
        )

        unified_path = results / f"{AGENT_ID}_unified_lines.csv"
        self._write_csv(
            unified_path,
            list(UNIFIED_CORE_COLUMNS) + list(ENRICHMENT_COLUMNS),
            [self._unified_row(record) for record in ordered],
        )
        written.append(unified_path.name)

        jsonl_path = results / f"{AGENT_ID}_unified_lines.jsonl"
        with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
            for record in ordered:
                handle.write(json.dumps(self._json_record(record), ensure_ascii=False) + "\n")
        written.append(jsonl_path.name)

        manifest_path = results / f"{AGENT_ID}_run_manifest.json"
        written.append(manifest_path.name)

        manifest = {
            "agent": AGENT_NAME,
            "agent_version": __version__,
            "lexicon_version": self.lexicon.version,
            "run_id": self.run_id,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "configuration": {
                "language_model_enabled": self.settings.model.enabled,
                "language_model_provider": self.settings.model.provider if self.settings.model.enabled else None,
                "language_model_name": self.settings.model.model if self.settings.model.enabled else None,
                "machine_translation_enabled": self.translator.available,
                "fuzzy_threshold": self.settings.fuzzy_threshold,
                "semantic_threshold": self.settings.semantic_threshold,
                "top_k_matches": self.settings.top_k_matches,
                "max_description_words": self.settings.max_description_words,
            },
            "inputs": [
                {
                    "file": str(path),
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
                for path in files
            ],
            "row_counts": {
                "total": len(self.records),
                "purchase_lines": len(ordered),
                "reference_lines": len(self.records) - len(ordered),
                "enriched": sum(1 for record in self.records if record.description),
            },
            "statistics": self.stats,
            "outputs": written,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest

    @staticmethod
    def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[Dict[str, str]]) -> None:
        """Write a CSV that Excel opens correctly regardless of locale."""
        seen: set = set()
        ordered_columns: List[str] = []
        for column in columns:
            if column not in seen:
                seen.add(column)
                ordered_columns.append(column)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=ordered_columns, extrasaction="ignore", lineterminator="\r\n"
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in ordered_columns})

    def _json_record(self, record: LineRecord) -> Dict[str, Any]:
        """Full record including the evidence bundle that does not fit a CSV cell."""
        return {
            "row_id": record.row_id,
            "run_id": self.run_id,
            "source": {
                "system": record.source_key,
                "label": record.source_label,
                "file": record.source_file,
                "sheet": record.source_sheet,
                "row_index": record.row_index,
            },
            "row_type": record.row_type,
            "duplicate_of": record.duplicate_of or None,
            "original": record.raw,
            "logical": record.logical,
            "enrichment": {
                "enriched_purchase_description": record.description,
                "enriched_description_short": record.description_short,
                "item_or_service": record.item_or_service,
                "method": record.enrichment_method,
                "ai_confidence": record.confidence,
                "confidence_band": record.confidence_band,
                "confidence_components": record.confidence_components,
            },
            "language": {
                "detected": record.language,
                "confidence": record.language_confidence,
            },
            "translation": {
                "source_text": record.raw_description,
                "english": record.translation.english,
                "method": record.translation.method,
                "coverage": record.translation.coverage,
                "unresolved_tokens": record.translation.unresolved,
                "support_english": record.support_translation.english,
            },
            "matches": [match.as_dict() for match in record.matches],
            "provenance": {
                "agent_version": __version__,
                "lexicon_version": self.lexicon.version,
            },
        }


# ===========================================================================
# Command line interface
# ===========================================================================

BANNER = f"""
===============================================================================
 {AGENT_NAME}
 Version {__version__}
===============================================================================
""".strip("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent1.py",
        description=AGENT_NAME,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Run without arguments for an interactive session; every prompt offers a\n"
            "default that can be accepted by pressing Enter."
        ),
    )
    parser.add_argument("--sources", type=str, help="Root folder holding the source data")
    parser.add_argument("--invoice-dir", type=str, help="Folder containing invoice line data")
    parser.add_argument("--po-dir", type=str, help="Folder containing purchase order data")
    parser.add_argument("--transaction-dir", type=str, help="Folder containing transaction data")
    parser.add_argument("--catalogue", type=str, help="Optional supplier catalogue file")
    parser.add_argument("--results", type=str, help="Folder for the generated output")
    parser.add_argument("--lexicon", type=str, help="Path to the controlled vocabulary file")
    parser.add_argument("--use-llm", action="store_true", help="Enable the language-model fallback")
    parser.add_argument(
        "--use-mt", action="store_true", help="Enable offline machine translation (Argos)"
    )
    parser.add_argument("--top-k", type=int, default=5, help="Matches retained per line")
    parser.add_argument(
        "--fuzzy-threshold", type=float, default=0.62, help="Minimum score for a fuzzy match"
    )
    parser.add_argument(
        "--semantic-threshold", type=float, default=0.45, help="Minimum score for a semantic match"
    )
    parser.add_argument(
        "--max-words", type=int, default=12, help="Word budget for a generated description"
    )
    parser.add_argument(
        "--non-interactive", action="store_true", help="Never prompt; use defaults and arguments"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug logging")
    return parser


def _clean_path_input(value: str) -> str:
    """Tolerate quoted paths and shell escaping when pasted into a prompt."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text.replace("\\ ", " ").strip()


def ask(question: str, default: str) -> str:
    """Prompt for a value, returning the default when the answer is empty."""
    try:
        answer = input(f"{question}\n  [{default}]: ")
    except EOFError:
        return default
    answer = _clean_path_input(answer)
    return answer or default


def ask_yes_no(question: str, default: bool = False) -> bool:
    """Prompt for a yes or no answer."""
    suffix = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{suffix}]: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in {"y", "yes"}


def resolve_settings(args: argparse.Namespace, env: Dict[str, str]) -> Settings:
    """Merge command-line arguments, prompted answers and sensible defaults."""
    project_root = Path(__file__).resolve().parent
    interactive = not args.non_interactive and sys.stdin.isatty()

    default_sources = args.sources or str(project_root / "sources")
    if interactive:
        print(BANNER)
        print("\nPress Enter to accept the value shown in brackets.\n")
        sources_root = Path(ask("Source data folder", default_sources)).expanduser()
    else:
        sources_root = Path(default_sources).expanduser()

    def resolve_directory(argument: Optional[str], label: str, folder: str) -> Optional[Path]:
        default = argument or str(sources_root / folder)
        if interactive:
            default = ask(f"{label} folder", default)
        path = Path(default).expanduser()
        if not path.is_dir():
            LOGGER.debug("Folder not present: %s", path)
        return path

    invoice_dir = resolve_directory(args.invoice_dir, "Invoice data", "invoice data")
    po_dir = resolve_directory(args.po_dir, "Purchase order data", "po data")
    transaction_dir = resolve_directory(args.transaction_dir, "Transaction data", "transaction data")

    default_catalogue = args.catalogue or str(sources_root / "Demo - Item Catalogues.csv")
    if interactive:
        default_catalogue = ask("Supplier catalogue file (optional, '-' to skip)", default_catalogue)
    catalogue = None if default_catalogue.strip() in {"", "-"} else Path(default_catalogue).expanduser()

    default_results = args.results or str(project_root / "results")
    if interactive:
        default_results = ask("Results folder", default_results)
    results_dir = Path(default_results).expanduser()

    lexicon_file = Path(
        args.lexicon or str(project_root / "lexicon" / "procurement_lexicon.json")
    ).expanduser()

    use_mt = args.use_mt
    use_llm = args.use_llm
    if interactive:
        if _argos is not None:
            use_mt = ask_yes_no("Enable offline machine translation (Argos)?", use_mt)
        use_llm = ask_yes_no(
            "Enable the language-model fallback for unresolved phrases?", use_llm
        )

    model = resolve_model_config(env, use_llm)
    if use_llm and not model.enabled:
        LOGGER.warning(
            "Language-model fallback could not be configured; check AZURE_ENABLE and the "
            "matching key, base URL and model name in .env"
        )

    return Settings(
        sources_root=sources_root,
        invoice_dir=invoice_dir,
        po_dir=po_dir,
        transaction_dir=transaction_dir,
        catalogue_file=catalogue,
        results_dir=results_dir,
        lexicon_file=lexicon_file if lexicon_file.is_file() else None,
        cache_dir=project_root / "cache",
        use_llm=use_llm,
        use_machine_translation=use_mt,
        top_k_matches=max(1, args.top_k),
        fuzzy_threshold=args.fuzzy_threshold,
        semantic_threshold=args.semantic_threshold,
        max_description_words=max(4, args.max_words),
        model=model,
    )


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def _print_token_usage(statistics: Dict[str, Any], settings: Settings) -> None:
    """Report language-model consumption, but only when a model was actually used."""
    usage = statistics.get("token_usage")
    if not usage:
        return

    print("\n" + "-" * 79)
    print("Language model usage")
    print("-" * 79)
    print(f"  {'Model':<20}: {settings.model.model} ({settings.model.provider})")
    print(f"  {'Phrases resolved':<20}: {statistics.get('model_phrases', 0)}")
    print(f"  {'Requests sent':<20}: {usage.get('requests', 0)}")
    if usage.get("cache_hits"):
        print(f"  {'Served from cache':<20}: {usage['cache_hits']} (no tokens consumed)")
    print(f"  {'Input tokens':<20}: {usage.get('input_tokens', 0):,}")
    if usage.get("cached_input_tokens"):
        print(f"  {'  of which cached':<20}: {usage['cached_input_tokens']:,}")
    print(f"  {'Output tokens':<20}: {usage.get('output_tokens', 0):,}")
    if usage.get("reasoning_tokens"):
        print(f"  {'  of which reasoning':<20}: {usage['reasoning_tokens']:,}")
    print(f"  {'Total tokens':<20}: {usage.get('total_tokens', 0):,}")


def print_summary(manifest: Dict[str, Any], settings: Settings) -> None:
    """Print a short operator-facing report of what the run produced."""
    statistics = manifest.get("statistics", {})
    counts = manifest.get("row_counts", {})

    print("\n" + "=" * 79)
    print("Run complete")
    print("=" * 79)
    print(f"  Run identifier      : {manifest['run_id']}")
    print(f"  Vocabulary version  : {manifest['lexicon_version']}")
    print(f"  Rows processed      : {counts.get('total', 0)}")
    print(f"  Rows enriched       : {counts.get('enriched', 0)}")

    for label, key in (
        ("Row types", "row_types"),
        ("Languages", "languages"),
        ("Match tiers", "match_tiers"),
        ("Confidence", "confidence_bands"),
    ):
        values = statistics.get(key)
        if values:
            rendered = ", ".join(f"{name}={value}" for name, value in values.items())
            print(f"  {label:<20}: {rendered}")

    if statistics.get("duplicate_rows"):
        print(f"  {'Duplicate rows':<20}: {statistics['duplicate_rows']}")

    _print_token_usage(statistics, settings)

    print(f"\n  Output folder: {settings.results_dir}")
    for name in manifest.get("outputs", []):
        print(f"    - {name}")
    print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    env = load_dotenv(Path(__file__).resolve().parent / ".env")
    settings = resolve_settings(args, env)

    if settings.model.enabled:
        LOGGER.info(
            "Language model enabled: %s via %s", settings.model.model, settings.model.provider
        )
    else:
        LOGGER.info("Running without a language model (vocabulary and rules only)")

    try:
        manifest = Agent1(settings).run()
    except SystemExit as error:
        LOGGER.error("%s", error)
        return 1
    except KeyboardInterrupt:
        LOGGER.error("Interrupted by user")
        return 130

    print_summary(manifest, settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
