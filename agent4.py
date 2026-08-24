#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 4 - AI Supplier Consolidation.

Finds materials and services that Fortum buys from more than one supplier, and
ranks the resulting consolidation opportunities.

    The question
    ------------
    Consolidation is not a question about descriptions; it is a question about
    portfolios. Two purchase lines being alike proves nothing on its own, since
    every supplier of any size sells something that some other supplier also
    sells. What a category manager needs to know is different and specific: of
    everything we buy from this supplier, how much of it could this other
    supplier have delivered instead?

    That framing decides the whole design. The unit of comparison is a supplier
    within a scope, not a purchase line, and the answer is a share of that
    supplier's portfolio rather than a resemblance score.

    Direction matters
    -----------------
    The measure is deliberately asymmetric, and this is the single most
    important thing about the output. A specialist supplier can be entirely
    covered by a full-range distributor while the distributor is barely covered
    by the specialist. Reported symmetrically, that pair looks like a weak
    opportunity in both directions; reported directionally it says exactly what
    to do, which is to move the specialist's volume and leave the distributor
    alone. Every row therefore carries the share of *this* supplier that the
    partner covers, the reverse share, and the smaller of the two.

    Same thing, different words
    ---------------------------
    Descriptions differ between suppliers, between source systems and between
    languages, so the comparison is not made on them. It is made on the AI
    Purchase Group that Agent 2 assigned, which has already collapsed the
    wording. Two suppliers who sell the same thing land in the same group even
    when nothing they wrote about it agrees. Where Agent 2 split near-identical
    things into neighbouring groups, sentence embeddings recover the connection
    with a partial credit, so a split does not silently destroy an overlap.

    Scope
    -----
    The plan asks for the primary comparison at Category L2, and for the same
    computation to be available at the other category levels and at Business
    Area and Division. All six are produced in one run: a supplier that looks
    interchangeable at L2 often is not at L4, and the pair of numbers is more
    informative than either alone.

    Materiality
    -----------
    A 90% overlap on eight thousand euro is a curiosity, not an opportunity, so
    similarity alone does not set the headline. The output carries a pure
    similarity band, which is what was asked for, alongside a consolidation
    rating that also weighs the spend actually at stake. The two are separate
    columns so that neither judgement is hidden inside the other.

    Supplier identity
    -----------------
    There is no supplier master to normalise against, so one is derived. Vendor
    names are stripped of legal forms and punctuation and folded together, which
    both prevents "Siemens Oy" and "SIEMENS OY AB" from being offered as a
    consolidation opportunity and surfaces them as the duplicate vendor records
    they are. The derived master is written out and can be corrected by hand;
    corrections are respected on the next run.

    Repeatability
    -------------
    The plan requires that a future production run find the same suppliers
    again. Supplier keys are content-derived rather than positional, the
    registry preserves both the keys and any manual corrections, iteration order
    is sorted throughout, and no step depends on the order in which rows arrive.

    Output
    ------
    Written to the results folder:

        agent4_supplier_consolidation.csv    one row per supplier per scope
        agent4_supplier_consolidation.jsonl  the same rows with every partner
        agent4_supplier_pairs.csv            one row per compared supplier pair
        agent4_supplier_master.csv           derived vendor master and duplicates
        agent4_run_manifest.json             configuration, statistics and tokens

Usage:
    python agent4.py

Author:
    Prof. Shahab Anbarjafari
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from runtime import configure_process_logging, load_sentence_transformer

LOGGER = logging.getLogger("agent4")

AGENT_NAME = "Agent 4 - AI Supplier Consolidation"
AGENT_VERSION = "1.0.0"

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))


# ===========================================================================
# Optional dependencies
# ===========================================================================
#
# Everything below degrades rather than fails. The agent produces a complete
# and defensible answer with none of these installed; each one present widens
# the set of overlaps it can see.

def _import_optional(module_path: str) -> Optional[Any]:
    try:
        return __import__(module_path, fromlist=["_"])
    except Exception:
        return None


_numpy = _import_optional("numpy")
_rapidfuzz = _import_optional("rapidfuzz.fuzz")
_requests = _import_optional("requests")
_sentence_transformers = _import_optional("sentence_transformers")
_openpyxl = _import_optional("openpyxl")


def describe_environment() -> Dict[str, bool]:
    return {
        "numpy": _numpy is not None,
        "sentence-transformers": _sentence_transformers is not None,
        "rapidfuzz": _rapidfuzz is not None,
        "openpyxl": _openpyxl is not None,
        "requests": _requests is not None,
    }


# ===========================================================================
# Configuration
# ===========================================================================

# List price for the default model, in dollars per million tokens. Both figures
# are overridable from the environment because prices are revised from time to
# time and the shared service does not have to quote the same rate as the public
# API. They estimate spend during a run; the invoice is the authority.
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


# The scopes the comparison is run within. The plan names Category L2 as the
# primary level and asks for the other category levels and the two
# organisational dimensions to be available alongside it.
DEFAULT_SCOPE_LEVELS = ("Category_L1", "Category_L2", "Category_L3", "Category_L4",
                        "Business_Area", "Division")

PRIMARY_SCOPE_LEVEL = "Category_L2"


@dataclass
class Settings:
    """Everything the run needs, resolved from arguments and prompts."""

    input_path: Path
    results_dir: Path
    lexicon_path: Path
    registry_path: Path
    cache_dir: Path

    scope_levels: Tuple[str, ...] = DEFAULT_SCOPE_LEVELS
    primary_scope: str = PRIMARY_SCOPE_LEVEL

    use_embeddings: bool = True
    use_llm: bool = False

    # Similarity bands. The client asked the agent to propose the ranges rather
    # than be given them, and these are that proposal. They are set where the
    # statement they license is defensible: at 60% of a supplier's portfolio
    # being available elsewhere, a sourcing conversation is warranted; below
    # 30%, the overlap is the incidental one that any two suppliers in the same
    # category will show. Both are exposed so the bands can be moved once the
    # distribution written by the first run has been read.
    high_similarity: float = 0.60
    medium_similarity: float = 0.30

    # Below this, a partner is not reported at all.
    report_similarity: float = 0.10

    # Spend that has to be at stake before an overlap is rated a consolidation
    # opportunity rather than merely a similarity. Similarity is reported
    # regardless; this only governs the consolidation rating.
    min_addressable_spend: float = 10_000.0

    # Lines a side must have before the overlap can be rated High. Applied to
    # both suppliers: a portfolio of two lines cannot evidence a portfolio
    # claim, and neither can a partner who bought the shared item twice.
    min_evidence_lines: int = 3

    # Partial credit between two distinct purchase groups. Below this the two
    # are treated as unrelated; the value is deliberately high because a false
    # overlap here inflates every number downstream of it.
    item_similarity_floor: float = 0.75
    item_neighbours: int = 8

    top_partners: int = 5
    max_partners_retained: int = 50

    # Guard rails for the pathological input distribution. A purchase group
    # bought by every supplier in a category contributes nothing to telling them
    # apart and costs a pass over the whole supplier list for each holder.
    max_item_fanout: int = 2000
    item_cap: int = 60_000

    # Suppliers in one scope before the comparison is refused. The work grows
    # with the square of this number, and a scope holding several thousand
    # suppliers is normally one that is too broad to yield a useful answer
    # anyway - every supplier in the business is "similar" to some other one
    # somewhere. Refusing loudly beats running for hours.
    max_scope_suppliers: int = 5000

    # Band around the High boundary where a model opinion can change the rating.
    adjudication_margin: float = 0.08
    max_adjudications: int = 200

    write_jsonl: bool = True
    verbose: bool = False

    # False under --non-interactive, where nothing may block waiting for input.
    interactive: bool = True

    model: ModelConfig = field(default_factory=ModelConfig)


def load_dotenv(path: Path) -> Dict[str, str]:
    """Parse a ``.env`` file into a dictionary; later assignments win."""
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
    """Select the language-model backend. Mirrors Agents 1 to 3 exactly."""
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
        config.model = env.get("AZURE_OPENAI_MODEL") or env.get("MODEL_NAME") or "azure.gpt-5.1"
    else:
        config.backend = "openai"
        config.api_key = env.get("OPENAI_API_KEY", "")
        config.base_url = env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        config.model = env.get("OPENAI_MODEL") or "gpt-5.1"

    if config.enabled and not config.api_key:
        LOGGER.warning("Language-model tier requested but no API key was found for the "
                       "%s backend; continuing without it.", config.backend)
        config.enabled = False
    return config


# ===========================================================================
# Text utilities
# ===========================================================================

_WHITESPACE = re.compile(r"\s+")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_XML_ESCAPES = re.compile(r"_x00[0-9A-Fa-f]{2}_")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "Å¡", "Ð")
_MOJIBAKE_LITERAL = {
    "Ã¤": "ä", "Ã¶": "ö", "Ã¥": "å", "Ã„": "Ä", "Ã–": "Ö", "Ã…": "Å",
    "Ã©": "é", "Ã¼": "ü", "Ã¸": "ø", "Ã¦": "æ", "Ã³": "ó",
    "â€™": "'", "â€œ": '"', "â€“": "-", "â€”": "-",
    "Å‚": "ł", "Å„": "ń", "Å›": "ś", "Å¼": "ż", "Åº": "ź",
    "Ä…": "ą", "Ä‡": "ć", "Ä™": "ę", "Â ": " ", "Â": "",
}


def repair_mojibake(text: str) -> str:
    """Undo double-encoding damage.

    Matters for supplier names above all else. A vendor whose name arrives
    mangled in one extract and intact in another becomes two suppliers, and two
    halves of one portfolio are compared against each other as though they were
    a consolidation opportunity.
    """
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
        text = str(int(value)) if value.is_integer() else repr(value)
    elif not isinstance(value, str):
        text = str(value)
    else:
        text = value
    text = _XML_ESCAPES.sub(" ", text)
    text = repair_mojibake(text)
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_CHARS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def fold_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return (stripped.replace("ł", "l").replace("Ł", "L")
            .replace("ø", "o").replace("Ø", "O")
            .replace("æ", "ae").replace("ß", "ss"))


def lookup_key(text: str) -> str:
    return _WHITESPACE.sub(" ", fold_accents(text).lower()).strip()


def tokenise(text: str) -> List[str]:
    return _TOKEN.findall(text)


def compact_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalise_text(value).upper())


def sentence_case(text: str) -> str:
    text = text.strip()
    return text[0].upper() + text[1:] if text else ""


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def parse_amount(value: Any) -> Optional[float]:
    """Parse a numeric cell written in any European convention."""
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
    if "," in text and "." in text:
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal_sep == "," else ",", "").replace(decimal_sep, ".")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = head + tail if (len(tail) == 3 and head) else text.replace(",", ".")
    try:
        amount = float(text)
    except ValueError:
        return None
    return -amount if negative else amount


def text_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if _rapidfuzz is not None:
        return float(_rapidfuzz.token_set_ratio(left, right)) / 100.0
    from difflib import SequenceMatcher
    return SequenceMatcher(None, left, right).ratio()


def format_percent(fraction: float) -> int:
    """A share expressed as whole percent, which is how the output reads."""
    return int(round(max(0.0, min(1.0, fraction)) * 100))


# ===========================================================================
# Controlled vocabulary
# ===========================================================================

class Lexicon:
    """The shared procurement vocabulary.

    Agent 4 uses only three parts of it: the legal forms, which drive supplier
    name normalisation, the noise terms, which identify placeholder values, and
    the service markers, which distinguish a portfolio of work from a portfolio
    of goods when the rating is explained.
    """

    # Used when no vocabulary file is available. Kept deliberately short: the
    # curated list in the JSON file is the one that should be maintained.
    _FALLBACK_LEGAL_FORMS = (
        "oy", "oyj", "ab", "abp", "as", "asa", "aps", "ltd", "limited", "plc",
        "llc", "inc", "corp", "gmbh", "ag", "bv", "nv", "sa", "srl", "spa",
        "sp z o o", "sp z oo", "spzoo", "kft", "doo", "ou", "ky", "ry", "co",
    )

    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        payload = payload or {}
        self.version: str = str(payload.get("version", "0.0.0"))

        legal_forms = payload.get("legal_forms") or self._FALLBACK_LEGAL_FORMS
        self.legal_forms: Set[str] = {lookup_key(form) for form in legal_forms}
        # Multi-word forms cannot be removed token by token, so they are held
        # separately and stripped from the string first, longest first.
        self.legal_phrases: List[str] = sorted(
            (form for form in self.legal_forms if " " in form),
            key=lambda form: (-len(form), form))

        # Words like "Nordic" and "International" sit between a legal form and a
        # real name. Removing them merges companies that are genuinely distinct;
        # keeping them hides that one vendor is recorded both with and without
        # one. They are therefore kept in the identity key and ignored only when
        # two vendors are being tested for being the same company.
        self.geographic_qualifiers: Set[str] = {
            lookup_key(term) for term in payload.get("geographic_qualifiers", [])}

        self.noise_terms: Set[str] = {lookup_key(t) for t in payload.get("noise_terms", [])}
        self.service_markers: Set[str] = {lookup_key(t) for t in payload.get("service_markers", [])}
        self.material_markers: Set[str] = {lookup_key(t) for t in payload.get("material_markers", [])}

    @classmethod
    def load(cls, path: Path) -> "Lexicon":
        if not path.is_file():
            LOGGER.warning("Vocabulary file %s not found; using the built-in fallback.", path)
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            LOGGER.error("Vocabulary file %s could not be read (%s).", path, error)
            return cls()
        LOGGER.info("Vocabulary loaded (version %s).", payload.get("version"))
        return cls(payload)

    def is_noise(self, text: str) -> bool:
        key = lookup_key(text)
        if not key or key in self.noise_terms:
            return True
        return not any(ch.isalpha() for ch in key)


# ===========================================================================
# Input
# ===========================================================================
#
# Agent 4 consumes the table Agent 2 writes, because the AI Purchase Group is
# what makes a cross-supplier comparison possible at all. It will run against
# Agent 1's output as well, falling back to comparing enriched descriptions
# directly, but that path is slower and blunter and the log says so.

SUPPLIER_NAME_COLUMN = "Supplier_Name"
SUPPLIER_ID_COLUMN = "Supplier_Id"
DESCRIPTION_COLUMN = "Enriched_Purchase_Description"
GROUP_LABEL_COLUMN = "AI_Purchase_Group_L5"
GROUP_ID_COLUMN = "AI_Purchase_Group_Id"
SPEND_COLUMN = "Spend_EUR"

OTHER_GROUP_LABEL = "Other"
OTHER_GROUP_ID = "G-OTHER"

# Where every line lands that carries no purchase group and no usable
# description. It counts towards a supplier's size and never towards an overlap.
UNIDENTIFIED_ITEM_KEY = "U:unidentified"
UNIDENTIFIED_ITEM_LABEL = "Unidentified purchases"

# Consulted when present, reported when populated, never required.
CONTEXT_COLUMNS = ("Country", "Business_Area", "Division", "Item_Or_Service",
                   "Item_Number", "Material_Group_Name", "Source_System")


@dataclass
class InputTable:
    headers: List[str]
    rows: List[Dict[str, str]]
    path: Path


def _detect_encoding(path: Path) -> str:
    sample = path.open("rb").read(1 << 18)
    for candidate in ("utf-8-sig", "utf-8", "cp1252", "cp1250", "latin-1"):
        try:
            sample.decode(candidate)
        except UnicodeDecodeError:
            continue
        return candidate
    return "latin-1"


def _read_csv(path: Path) -> InputTable:
    encoding = _detect_encoding(path)
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        sample = handle.read(1 << 16)
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        headers = [normalise_text(name) for name in (reader.fieldnames or [])]
        rows = [{normalise_text(key): normalise_text(value)
                 for key, value in record.items() if key is not None}
                for record in reader]
    return InputTable(headers=headers, rows=rows, path=path)


def _read_xlsx(path: Path) -> Optional[InputTable]:
    if _openpyxl is None:
        LOGGER.error("Reading %s needs openpyxl (pip install openpyxl).", path.name)
        return None
    workbook = _openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet_name in workbook.sheetnames:
            worksheet = workbook[sheet_name]
            iterator = worksheet.iter_rows(values_only=True)
            headers = [normalise_text(cell) for cell in next(iterator, ())]
            if not any(headers):
                continue
            rows = [dict(zip(headers, (normalise_text(cell) for cell in values)))
                    for values in iterator]
            return InputTable(headers=headers, rows=rows, path=path)
    finally:
        workbook.close()
    return None


def read_purchase_table(path: Path) -> InputTable:
    """Load the purchase lines produced by Agents 1 and 2."""
    if not path.is_file():
        raise SystemExit(
            f"Input file not found: {path}\n"
            "Agent 4 consumes the table written by Agent 2, which carries the AI "
            "Purchase Group the supplier comparison is built on. Run Agent 2 first, "
            "or point --input at an equivalent table.")

    if path.suffix.lower() in {".csv", ".tsv", ".txt"}:
        table = _read_csv(path)
    else:
        table = _read_xlsx(path)
    if table is None:
        raise SystemExit(f"No readable sheet in {path}")

    if SUPPLIER_NAME_COLUMN not in table.headers and SUPPLIER_ID_COLUMN not in table.headers:
        raise SystemExit(
            f"{path.name} carries neither '{SUPPLIER_NAME_COLUMN}' nor "
            f"'{SUPPLIER_ID_COLUMN}'.\nA supplier comparison needs a supplier.")

    if GROUP_LABEL_COLUMN not in table.headers:
        if DESCRIPTION_COLUMN not in table.headers:
            raise SystemExit(
                f"{path.name} carries neither '{GROUP_LABEL_COLUMN}' nor "
                f"'{DESCRIPTION_COLUMN}'.\n"
                "Run Agent 1 and Agent 2 first, or supply a table that has one of them.")
        LOGGER.warning(
            "No '%s' column, so enriched descriptions will be compared directly. "
            "Running Agent 2 first gives a markedly better comparison, because the "
            "purchase group has already collapsed the wording that differs between "
            "suppliers.", GROUP_LABEL_COLUMN)

    LOGGER.info("Loaded %d purchase line(s) from %s", len(table.rows), path.name)
    return table


# ===========================================================================
# Supplier identity
# ===========================================================================

class SupplierNormaliser:
    """Reduces a vendor name to a comparable form.

    The open question in the plan was whether a supplier master exists to
    normalise against. It does not, so this derives one, and the derivation is
    the first thing that has to be right: a supplier split across two spellings
    has half a portfolio in each, and the two halves will then be offered to the
    category manager as a consolidation opportunity with a very high similarity.
    That is the most embarrassing failure this agent could produce, and it is
    also the easiest to prevent.

    Legal forms are removed rather than kept because they are the least stable
    part of a vendor name across systems - one extract writes "Oy", the next
    writes "Oy Ab", a third writes nothing - while carrying no information about
    what is being bought.
    """

    _PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)

    def __init__(self, lexicon: Lexicon) -> None:
        self.lexicon = lexicon
        self._cache: Dict[str, str] = {}
        self._core_cache: Dict[str, str] = {}

    def normalise(self, name: str) -> str:
        """The identity key for a vendor name.

        Two vendors sharing this key are treated as one company. Everything that
        distinguishes one business from another is therefore preserved, and only
        the incorporation suffix is taken away.
        """
        if name in self._cache:
            return self._cache[name]

        working = lookup_key(name)
        working = self._PUNCTUATION.sub(" ", working)
        working = _WHITESPACE.sub(" ", working).strip()

        # Multi-word forms first: "sp z o o" has to go before the token pass
        # gets the chance to read it as three separate one-letter tokens.
        for phrase in self.lexicon.legal_phrases:
            working = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", working)

        tokens = [token for token in tokenise(working)
                  if token not in self.lexicon.legal_forms]

        # Everything was a legal form, which happens for a vendor recorded as
        # nothing but "Oy Ab". Keeping the original is the lesser evil: it may
        # merge two unrelated vendors, but discarding it merges them with every
        # other degenerate name in the file.
        if not tokens:
            tokens = tokenise(working) or tokenise(lookup_key(name))

        key = " ".join(tokens).strip()
        self._cache[name] = key
        return key

    def core(self, canonical_key: str) -> str:
        """The identity key with its geographic qualifiers removed.

        Used only to ask whether two vendors might be the same company. "Alpha
        Nordic" and "Alpha" are not merged on the strength of this - that would
        be a decision made for the wrong reason - but they are put in front of a
        reviewer, which is what the flag is for.
        """
        if canonical_key in self._core_cache:
            return self._core_cache[canonical_key]
        tokens = [token for token in tokenise(canonical_key)
                  if token not in self.lexicon.geographic_qualifiers]
        core = " ".join(tokens).strip() or canonical_key
        self._core_cache[canonical_key] = core
        return core


@dataclass
class Supplier:
    """One vendor as this agent understands it, across all its spellings."""

    key: str
    canonical_key: str
    display_name: str
    raw_names: Counter = field(default_factory=Counter)
    identifiers: Counter = field(default_factory=Counter)
    lines: int = 0
    spend: float = 0.0
    countries: Counter = field(default_factory=Counter)
    business_areas: Counter = field(default_factory=Counter)
    possible_duplicate_of: str = ""
    duplicate_evidence: str = ""

    def dominant_country(self) -> str:
        return self.countries.most_common(1)[0][0] if self.countries else ""

    def dominant_business_area(self) -> str:
        return self.business_areas.most_common(1)[0][0] if self.business_areas else ""


class SupplierRegistry:
    """Persists supplier keys, display names and manual corrections.

    Keys are content-derived, so they would be reproducible without this file.
    What the registry adds is the ability to overrule the derivation. Name
    normalisation cannot know that two unrelated vendors happen to normalise
    alike, or that a subsidiary should be folded into its parent for the purpose
    of a sourcing conversation; a procurement expert does know, and the
    ``overrides`` block is where that knowledge is recorded so that it survives
    the next run.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: Dict[str, Dict[str, str]] = {}
        self.overrides: Dict[str, str] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            LOGGER.warning("Supplier registry %s could not be read (%s); starting fresh.",
                           self.path, error)
            return
        self.entries = dict(payload.get("entries") or {})
        self.overrides = {lookup_key(k): lookup_key(v)
                          for k, v in (payload.get("overrides") or {}).items()}
        LOGGER.info("Supplier registry loaded: %d known supplier(s), %d override(s).",
                    len(self.entries), len(self.overrides))

    def resolve(self, canonical_key: str) -> str:
        """Apply any manual correction, following a short chain of them."""
        seen: Set[str] = set()
        current = canonical_key
        while current in self.overrides and current not in seen:
            seen.add(current)
            current = self.overrides[current]
        return current

    def key_for(self, canonical_key: str, display_name: str) -> str:
        """A stable identifier for a supplier, minted once and kept."""
        entry = self.entries.get(canonical_key)
        if entry:
            return entry["supplier_key"]
        supplier_key = f"S-{stable_hash('supplier', canonical_key)[:10].upper()}"
        self.entries[canonical_key] = {"supplier_key": supplier_key,
                                       "display_name": display_name}
        self._dirty = True
        return supplier_key

    def note_display_name(self, canonical_key: str, display_name: str) -> None:
        """Keep the registry's copy of the name in step with the data."""
        entry = self.entries.get(canonical_key)
        if entry is not None and entry.get("display_name") != display_name:
            entry["display_name"] = display_name
            self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "agent": AGENT_NAME,
            "version": AGENT_VERSION,
            "note": ("Machine-maintained. The 'overrides' block is the part meant to be "
                     "edited by hand: map one normalised supplier name onto another to "
                     "merge them permanently, for example a subsidiary onto its parent."),
            "overrides": dict(sorted(self.overrides.items())),
            "entries": dict(sorted(self.entries.items())),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                        sort_keys=True), encoding="utf-8")
        self._dirty = False


class SupplierResolver:
    """Builds the derived vendor master from the purchase lines."""

    # Two normalised names this alike, sharing a first token, are almost always
    # the same vendor recorded twice. Set high because the cost of a wrong merge
    # - a supplier's portfolio silently absorbing another's - is far worse than
    # the cost of a missed one, which is a duplicate pair that a reviewer sees.
    _DUPLICATE_THRESHOLD = 0.92

    def __init__(self, lexicon: Lexicon, registry: SupplierRegistry) -> None:
        self.lexicon = lexicon
        self.registry = registry
        self.normaliser = SupplierNormaliser(lexicon)
        self.suppliers: Dict[str, Supplier] = {}
        self.by_key: Dict[str, Supplier] = {}

    @staticmethod
    def _follow(alias: Dict[str, str], start: str) -> str:
        """Resolve a chain of aliases, which vendor numbers can create.

        One number merges A into B while another merges B into C, and a
        single-step lookup would leave A and C apart when both are the same
        vendor. The guard is for a cycle, which two numbers pointing at each
        other would otherwise produce.
        """
        seen: Set[str] = set()
        current = start
        while current in alias and current not in seen:
            seen.add(current)
            current = alias[current]
        return current

    def ingest(self, rows: Iterable[Dict[str, str]]) -> None:
        """Fold every line into a supplier profile."""
        pending: List[Tuple[str, str, str, float, str, str]] = []

        for row in rows:
            name = normalise_text(row.get(SUPPLIER_NAME_COLUMN, ""))
            identifier = normalise_text(row.get(SUPPLIER_ID_COLUMN, ""))
            if self.lexicon.is_noise(name):
                name = ""
            if not name and not identifier:
                continue
            spend = parse_amount(row.get(SPEND_COLUMN, "")) or 0.0
            country = normalise_text(row.get("Country", ""))
            business_area = normalise_text(row.get("Business_Area", ""))

            canonical = self.normaliser.normalise(name) if name else ""
            if not canonical:
                # Identified only by number. That is still a supplier, and
                # keeping it under its identifier is better than dropping the
                # spend it carries out of the analysis entirely.
                canonical = f"id:{compact_key(identifier)}"
            pending.append((canonical, name, identifier, spend, country, business_area))

        # A vendor number is stronger evidence of identity than a name, so a
        # second pass lets the numbers pull differently-spelled names together.
        # Sorted so that which spelling wins is decided by the data rather than
        # by the order the file happened to arrive in.
        identifier_to_canonical: Dict[str, Counter] = defaultdict(Counter)
        for canonical, _, identifier, _, _, _ in pending:
            if identifier:
                identifier_to_canonical[compact_key(identifier)][canonical] += 1

        alias: Dict[str, str] = {}
        for identifier in sorted(identifier_to_canonical):
            counts = identifier_to_canonical[identifier]
            if len(counts) < 2:
                continue
            winner = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
            for canonical in counts:
                if canonical != winner:
                    alias[canonical] = winner

        for canonical, name, identifier, spend, country, business_area in pending:
            canonical = self._follow(alias, canonical)
            canonical = self.registry.resolve(canonical)
            supplier = self.suppliers.get(canonical)
            if supplier is None:
                supplier = Supplier(
                    key=self.registry.key_for(canonical, name or identifier),
                    canonical_key=canonical,
                    display_name=name or identifier,
                )
                self.suppliers[canonical] = supplier
            if name:
                supplier.raw_names[name] += 1
            if identifier:
                supplier.identifiers[identifier] += 1
            supplier.lines += 1
            supplier.spend += spend
            if country:
                supplier.countries[country] += 1
            if business_area:
                supplier.business_areas[business_area] += 1

        for supplier in self.suppliers.values():
            if supplier.raw_names:
                # The spelling used on the most lines, alphabetical on a tie.
                # Derived from the data on every run rather than frozen at first
                # sight: it is the key that has to be stable, and pinning the
                # name as well would leave a supplier labelled for good by
                # whichever spelling happened to appear first.
                supplier.display_name = sorted(
                    supplier.raw_names.items(), key=lambda item: (-item[1], item[0]))[0][0]
            self.registry.note_display_name(supplier.canonical_key, supplier.display_name)

        self._flag_duplicates()
        self.by_key = {supplier.key: supplier for supplier in self.suppliers.values()}
        LOGGER.info("Resolved %d distinct supplier(s) from %d raw spelling(s).",
                    len(self.suppliers),
                    sum(len(s.raw_names) for s in self.suppliers.values()))

    def _flag_duplicates(self) -> None:
        """Mark vendors that look like the same legal entity recorded twice.

        Reported rather than merged. A merge that is wrong is invisible and
        corrupts every number in the run; a flag that is wrong is a line in a
        file that a reviewer dismisses in a second. Where the flag is right, the
        registry override block is how the merge is made permanent.
        """
        blocks: Dict[str, List[str]] = defaultdict(list)
        cores: Dict[str, str] = {}
        for canonical in sorted(self.suppliers):
            core = self.normaliser.core(canonical)
            cores[canonical] = core
            tokens = tokenise(core)
            if not tokens:
                continue
            # Blocked on the first four characters of the leading token, which
            # is what keeps this out of quadratic territory on a long vendor
            # list while still catching every realistic spelling variation.
            blocks[tokens[0][:4]].append(canonical)

        for _, members in sorted(blocks.items()):
            if len(members) < 2:
                continue
            for index, left in enumerate(members):
                for right in members[index + 1:]:
                    score = text_similarity(cores[left], cores[right])
                    if score < self._DUPLICATE_THRESHOLD:
                        continue
                    # The larger portfolio is treated as the survivor, so the
                    # suggestion reads as "fold the small one into the big one".
                    first, second = sorted(
                        (self.suppliers[left], self.suppliers[right]),
                        key=lambda supplier: (-supplier.spend, -supplier.lines, supplier.key))
                    if second.possible_duplicate_of:
                        continue
                    second.possible_duplicate_of = first.key
                    second.duplicate_evidence = (
                        f"name {format_percent(score)}% identical to "
                        f"{first.display_name} once legal forms and geographic "
                        f"qualifiers are set aside")


# ===========================================================================
# Purchase items
# ===========================================================================
#
# The unit of comparison. A supplier's portfolio is a weighted bag of these, and
# two suppliers overlap to the extent that their bags do.

@dataclass
class PurchaseItem:
    """One thing that gets bought, independent of who supplied it."""

    key: str
    label: str
    is_named: bool = True


class ItemSpace:
    """The catalogue of purchase items, and the similarity between them.

    Two suppliers who sell the same thing normally land in the same purchase
    group, and that identity is what most of the overlap is built from. The
    neighbour map exists for the residue: where Agent 2 drew a boundary through
    the middle of something - "Frequency converter" in one group, "Frequency
    converters and drives" in another - identity alone would report an overlap
    of zero between two suppliers who sell exactly the same equipment.

    Partial credit is capped by a high floor. Every point of credit awarded here
    is added to a percentage that a category manager will act on, so the bias is
    firmly towards understating the overlap.
    """

    MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.items: Dict[str, PurchaseItem] = {}
        self.neighbours: Dict[str, List[Tuple[str, float]]] = {}
        self.method = "identity only"

    def add(self, key: str, label: str, named: bool) -> None:
        if key not in self.items:
            self.items[key] = PurchaseItem(key=key, label=label, is_named=named)

    def build_neighbours(self) -> None:
        """Relate purchase items that are near-duplicates of each other."""
        # An item only one supplier ever bought cannot create an overlap by
        # itself, but it can bridge to one held by others, so nothing is
        # excluded here beyond the placeholder group.
        keys = sorted(key for key, item in self.items.items() if item.is_named)
        if len(keys) < 2:
            return

        if len(keys) > self.settings.item_cap:
            LOGGER.warning(
                "%d distinct purchase items exceeds the cap of %d, so overlap will be "
                "measured on exact group identity alone. Running Agent 2 first collapses "
                "descriptions into groups and normally brings this well under the cap; "
                "raise --item-cap if the machine has the memory for it.",
                len(keys), self.settings.item_cap)
            return

        if self.settings.use_embeddings and _sentence_transformers is not None and _numpy is not None:
            if self._build_semantic(keys):
                return
        self._build_lexical(keys)

    def _build_semantic(self, keys: Sequence[str]) -> bool:
        """Nearest neighbours by multilingual sentence embedding."""
        labels = [self.items[key].label for key in keys]
        try:
            LOGGER.info("Loading embedding model %s ...", self.MODEL_NAME)
            model = load_sentence_transformer(_sentence_transformers, self.MODEL_NAME)
            LOGGER.info("Embedding %d purchase item label(s) ...", len(labels))
            matrix = model.encode(labels, batch_size=64, convert_to_numpy=True,
                                  normalize_embeddings=True, show_progress_bar=False)
        except Exception as error:
            LOGGER.warning("Embeddings unavailable (%s); falling back to lexical comparison.",
                           error)
            return False

        floor = self.settings.item_similarity_floor
        top_k = self.settings.item_neighbours
        # Chunked so that the full square of similarities is never held at once;
        # at the cap that matrix would be several terabytes.
        chunk_size = 512
        for start in range(0, len(keys), chunk_size):
            block = matrix[start:start + chunk_size]
            # Vectors are unit length, so the dot product is the cosine.
            similarities = block @ matrix.T
            for offset in range(block.shape[0]):
                row = similarities[offset]
                position = start + offset
                row[position] = -1.0  # never a neighbour of itself
                count = min(top_k, len(keys) - 1)
                if count <= 0:
                    continue
                candidates = _numpy.argpartition(-row, count - 1)[:count]
                ranked = sorted(candidates,
                                key=lambda index: (-float(row[index]), keys[index]))
                found = [(keys[index], round(float(row[index]), 4))
                         for index in ranked if float(row[index]) >= floor]
                if found:
                    self.neighbours[keys[position]] = found
            if start and start % (chunk_size * 20) == 0:
                LOGGER.info("  related %d / %d purchase item(s)", start, len(keys))

        self.method = "sentence embeddings"
        LOGGER.info("Purchase items related by embedding: %d of %d have a near neighbour.",
                    len(self.neighbours), len(keys))
        return True

    def _build_lexical(self, keys: Sequence[str]) -> None:
        """Nearest neighbours by shared tokens, used when embeddings are absent.

        Blocked on tokens rather than compared exhaustively. Labels are short
        procurement phrases, so a shared content word is a cheap and effective
        prefilter, and the common words that would defeat it are exactly the
        ones dropped below.
        """
        postings: Dict[str, List[str]] = defaultdict(list)
        for key in keys:
            for token in set(tokenise(lookup_key(self.items[key].label))):
                if len(token) >= 4:
                    postings[token].append(key)

        # A token held by a large share of all items is a stop word in this
        # corpus whatever the dictionary says, and blocking on it would compare
        # everything with everything.
        ceiling = max(50, len(keys) // 20)
        floor = self.settings.item_similarity_floor
        top_k = self.settings.item_neighbours

        for key in keys:
            label = lookup_key(self.items[key].label)
            candidates: Set[str] = set()
            for token in set(tokenise(label)):
                if len(token) < 4:
                    continue
                holders = postings.get(token, ())
                if len(holders) > ceiling:
                    continue
                candidates.update(holders)
            candidates.discard(key)
            if not candidates:
                continue
            scored = [(other, round(text_similarity(label, lookup_key(self.items[other].label)), 4))
                      for other in sorted(candidates)]
            found = sorted((entry for entry in scored if entry[1] >= floor),
                           key=lambda entry: (-entry[1], entry[0]))[:top_k]
            if found:
                self.neighbours[key] = found

        self.method = "lexical similarity"
        LOGGER.info("Purchase items related lexically: %d of %d have a near neighbour.",
                    len(self.neighbours), len(keys))


# ===========================================================================
# Scopes and portfolios
# ===========================================================================

@dataclass(frozen=True)
class Scope:
    """The slice of the business a comparison is made within.

    Comparing supplier portfolios across the whole of spend answers a question
    nobody asked: of course a cable distributor and a cleaning contractor have
    nothing in common. The comparison is only meaningful inside a category or an
    organisational unit, and which one is the right choice is itself a finding,
    which is why all six are produced.
    """

    level: str
    value: str

    @property
    def key(self) -> str:
        return f"{self.level}={self.value}"


@dataclass
class Portfolio:
    """What one supplier sells inside one scope."""

    supplier_key: str
    scope: Scope
    weights: Dict[str, float] = field(default_factory=dict)
    spend: Dict[str, float] = field(default_factory=dict)
    lines: Dict[str, int] = field(default_factory=dict)
    countries: Counter = field(default_factory=Counter)

    total_weight: float = 0.0
    total_spend: float = 0.0
    total_lines: int = 0
    named_weight: float = 0.0

    @property
    def named_share(self) -> float:
        """How much of the portfolio sits in a properly named purchase group.

        Weight parked in the unnamed residue is weight the comparison cannot
        reason about, so this is the honest ceiling on how much any statement
        about this supplier is worth.
        """
        return self.named_weight / self.total_weight if self.total_weight else 0.0

    def top_items(self, space: ItemSpace, count: int = 8) -> List[str]:
        ranked = sorted(self.weights.items(), key=lambda item: (-item[1], item[0]))
        return [space.items[key].label for key, _ in ranked[:count]]


class PortfolioBuilder:
    """Aggregates purchase lines into supplier portfolios, one set per scope.

    This is also the step that makes the run tractable at a million lines. Once
    the lines have been folded into (scope, supplier, item) totals, nothing
    downstream ever touches a line again, and the cost of the comparison is set
    by the number of suppliers rather than by the size of the input.
    """

    def __init__(self, settings: Settings, lexicon: Lexicon,
                 resolver: SupplierResolver, space: ItemSpace) -> None:
        self.settings = settings
        self.lexicon = lexicon
        self.resolver = resolver
        self.space = space
        self.portfolios: Dict[str, Dict[str, Portfolio]] = defaultdict(dict)
        self.scopes: Dict[str, Scope] = {}
        self.weight_basis: Dict[str, str] = {}
        self.statistics: Counter = Counter()
        self._use_groups = True

    def build(self, table: InputTable) -> None:
        self._use_groups = GROUP_LABEL_COLUMN in table.headers
        available = [level for level in self.settings.scope_levels if level in table.headers]
        missing = [level for level in self.settings.scope_levels if level not in table.headers]
        if missing:
            LOGGER.warning("Scope level(s) absent from the input and skipped: %s",
                           ", ".join(missing))
        if not available:
            raise SystemExit(
                "None of the requested scope levels is present in the input.\n"
                f"Looked for: {', '.join(self.settings.scope_levels)}")

        # Spend is the natural weight, but it is not always populated. The basis
        # is decided per scope rather than globally, because one badly exported
        # source system should not force the whole run onto line counts.
        #
        # That decision has to be made before any weight can be assigned, so the
        # rows are walked twice. Holding the intermediate result instead would
        # mean one record per line per scope level, which at a million lines and
        # six levels is six million records and several gigabytes; the rows are
        # already in memory, so reading them again is close to free.
        spend_lines: Counter = Counter()
        scope_lines: Counter = Counter()

        for row in table.rows:
            spend = parse_amount(row.get(SPEND_COLUMN, "")) or 0.0
            for level in available:
                value = normalise_text(row.get(level, ""))
                if not value or self.lexicon.is_noise(value):
                    continue
                scope_key = f"{level}={value}"
                self.scopes.setdefault(scope_key, Scope(level=level, value=value))
                scope_lines[scope_key] += 1
                if spend > 0:
                    spend_lines[scope_key] += 1

        for scope_key, lines in scope_lines.items():
            covered = spend_lines[scope_key] / lines if lines else 0.0
            self.weight_basis[scope_key] = "spend" if covered >= 0.5 else "lines"

        for row in table.rows:
            supplier_key = self._supplier_key(row)
            if not supplier_key:
                self.statistics["lines_without_supplier"] += 1
                continue

            item_key, item_label, named = self._item(row)
            spend = parse_amount(row.get(SPEND_COLUMN, "")) or 0.0
            country = normalise_text(row.get("Country", ""))
            self.space.add(item_key, item_label, named)

            for level in available:
                value = normalise_text(row.get(level, ""))
                if not value or self.lexicon.is_noise(value):
                    continue
                scope_key = f"{level}={value}"
                # In a spend-weighted scope a line with no amount contributes
                # nothing to the share, which is correct: an unpriced line is no
                # evidence about how much of the portfolio it represents.
                weight = spend if self.weight_basis[scope_key] == "spend" else 1.0

                bucket = self.portfolios[scope_key]
                portfolio = bucket.get(supplier_key)
                if portfolio is None:
                    portfolio = Portfolio(supplier_key=supplier_key,
                                          scope=self.scopes[scope_key])
                    bucket[supplier_key] = portfolio

                portfolio.weights[item_key] = portfolio.weights.get(item_key, 0.0) + weight
                portfolio.spend[item_key] = portfolio.spend.get(item_key, 0.0) + spend
                portfolio.lines[item_key] = portfolio.lines.get(item_key, 0) + 1
                portfolio.total_weight += weight
                portfolio.total_spend += spend
                portfolio.total_lines += 1
                if named:
                    portfolio.named_weight += weight
                if country:
                    portfolio.countries[country] += 1

        self._repair_empty_weights()
        self.statistics["scopes"] = len(self.portfolios)
        self.statistics["purchase_items"] = len(self.space.items)
        LOGGER.info("Built %d supplier portfolio(s) across %d scope(s).",
                    sum(len(bucket) for bucket in self.portfolios.values()),
                    len(self.portfolios))

    def _repair_empty_weights(self) -> None:
        """Fall back to line counts for a portfolio that carries no spend.

        A supplier whose every line in a spend-weighted scope has a blank amount
        would otherwise have a total weight of zero and drop silently out of the
        comparison. Weighting that one portfolio by lines keeps it visible; the
        shares it produces are still shares of itself, which is what the
        directional measure needs.
        """
        for bucket in self.portfolios.values():
            for portfolio in bucket.values():
                if portfolio.total_weight > 0:
                    continue
                portfolio.weights = {key: float(count)
                                     for key, count in portfolio.lines.items()}
                portfolio.total_weight = float(portfolio.total_lines)
                portfolio.named_weight = sum(
                    float(count) for key, count in portfolio.lines.items()
                    if self.space.items[key].is_named)
                self.statistics["portfolios_weighted_by_lines"] += 1

    def _supplier_key(self, row: Dict[str, str]) -> str:
        name = normalise_text(row.get(SUPPLIER_NAME_COLUMN, ""))
        identifier = normalise_text(row.get(SUPPLIER_ID_COLUMN, ""))
        if self.lexicon.is_noise(name):
            name = ""
        if not name and not identifier:
            return ""
        canonical = (self.resolver.normaliser.normalise(name) if name
                     else f"id:{compact_key(identifier)}")
        canonical = self.resolver.registry.resolve(canonical)
        supplier = self.resolver.suppliers.get(canonical)
        return supplier.key if supplier else ""

    def _item(self, row: Dict[str, str]) -> Tuple[str, str, bool]:
        """The purchase item a line represents, and whether it is a named one.

        Everything that cannot be identified lands in a single bucket. It is
        kept rather than discarded so that the denominator stays honest: a
        supplier two thirds of whose spend is unidentifiable should not be
        described as confidently comparable to anyone. The bucket never matches
        anything, including another supplier's copy of itself.
        """
        if self._use_groups:
            label = normalise_text(row.get(GROUP_LABEL_COLUMN, ""))
            identifier = normalise_text(row.get(GROUP_ID_COLUMN, "")) or label
            if label and label != OTHER_GROUP_LABEL and identifier != OTHER_GROUP_ID:
                return f"G:{lookup_key(identifier)}", label, True
            return UNIDENTIFIED_ITEM_KEY, UNIDENTIFIED_ITEM_LABEL, False

        description = normalise_text(row.get(DESCRIPTION_COLUMN, ""))
        if not description or self.lexicon.is_noise(description):
            return UNIDENTIFIED_ITEM_KEY, UNIDENTIFIED_ITEM_LABEL, False
        return f"D:{lookup_key(description)}", description, True


# ===========================================================================
# Overlap
# ===========================================================================

@dataclass
class Reach:
    """One supplier's coverage of another, measured from one side only."""

    coverage: float = 0.0
    matched_weight: float = 0.0
    exact_weight: float = 0.0
    addressable_spend: float = 0.0
    exact_items: int = 0
    similar_items: int = 0
    # What the partner itself has behind the overlap. A supplier who bought a
    # frequency converter once is not an alternative source of frequency
    # converters, however cleanly the group matches.
    partner_lines: int = 0
    labels: List[str] = field(default_factory=list)


@dataclass
class Overlap:
    """How much of one supplier's portfolio another supplier also covers."""

    partner_key: str
    coverage: float = 0.0            # share of this supplier covered by the partner
    reverse_coverage: float = 0.0    # share of the partner covered by this supplier
    addressable_spend: float = 0.0   # this supplier's spend the partner could serve
    partner_spend: float = 0.0       # the partner's spend this supplier could serve
    exact_items: int = 0
    similar_items: int = 0
    exact_weight: float = 0.0        # matched weight from identical items only
    matched_weight: float = 0.0
    partner_lines: int = 0           # the partner's own lines on the shared items
    shared_labels: List[str] = field(default_factory=list)
    confidence: int = 0
    similarity_band: str = "Low"
    consolidation: str = "Low"
    adjudication: str = ""

    @property
    def mutual(self) -> float:
        return min(self.coverage, self.reverse_coverage)

    @property
    def exact_share(self) -> float:
        return self.exact_weight / self.matched_weight if self.matched_weight else 0.0


class OverlapEngine:
    """Computes directional portfolio coverage between suppliers in a scope.

    The measure is:

        coverage(A -> B) = sum over items g of A of w(g) * m(g, B) / total w(A)

    where m(g, B) is 1 when B also buys g, the best neighbour similarity when B
    buys something near-identical to g, and zero otherwise. It reads as the
    share of A's spend that B could have served, which is the number the plan
    asks for and the number a sourcing decision actually turns on.

    Pairs are generated from an inverted index rather than enumerated. Comparing
    every supplier with every other is quadratic and, on real data, almost
    entirely wasted: the overwhelming majority of pairs in a category have no
    item in common at all and would score zero. Walking outwards from the items
    each supplier actually buys visits only the pairs that can be non-zero, and
    computes their exact coverage on the same pass.
    """

    def __init__(self, settings: Settings, space: ItemSpace) -> None:
        self.settings = settings
        self.space = space
        self.statistics: Counter = Counter()
        # Coverage below this is not retained. It is set well under the
        # reporting threshold because these values are also what the opposite
        # direction of a reported pair is read from, and a pair worth reporting
        # one way round can be very weak the other way round - which is the
        # asymmetry the output exists to show.
        self._retention_floor = min(0.02, settings.report_similarity / 2.0)

    def compare(self, portfolios: Dict[str, Portfolio]) -> Dict[str, List[Overlap]]:
        """Rank every supplier's partners within one scope."""
        if len(portfolios) < 2:
            return {}

        index: Dict[str, List[str]] = defaultdict(list)
        for supplier_key in sorted(portfolios):
            for item_key in portfolios[supplier_key].weights:
                if self.space.items[item_key].is_named:
                    index[item_key].append(supplier_key)

        # An item bought by an implausible number of suppliers separates none of
        # them and costs a pass over that whole list for every holder.
        crowded = {item_key for item_key, holders in index.items()
                   if len(holders) > self.settings.max_item_fanout}
        if crowded:
            self.statistics["items_skipped_as_crowded"] += len(crowded)

        # Both directions are measured from first principles, each from its own
        # side. Deriving one from the other is tempting and wrong: the neighbour
        # relation between purchase items is a top-k list and is therefore not
        # symmetric, so an inferred reverse figure would disagree with the one
        # computed when the partner's own turn came round.
        reaches: Dict[str, Dict[str, Reach]] = {}
        for supplier_key in sorted(portfolios):
            reaches[supplier_key] = self._reach(supplier_key, portfolios, index, crowded)

        results: Dict[str, List[Overlap]] = {}
        for supplier_key in sorted(reaches):
            overlaps: List[Overlap] = []
            for partner_key in sorted(reaches[supplier_key]):
                forward = reaches[supplier_key][partner_key]
                if forward.coverage < self.settings.report_similarity:
                    continue
                backward = reaches.get(partner_key, {}).get(supplier_key)
                overlaps.append(Overlap(
                    partner_key=partner_key,
                    coverage=forward.coverage,
                    reverse_coverage=backward.coverage if backward else 0.0,
                    addressable_spend=forward.addressable_spend,
                    partner_spend=backward.addressable_spend if backward else 0.0,
                    exact_items=forward.exact_items,
                    similar_items=forward.similar_items,
                    exact_weight=forward.exact_weight,
                    matched_weight=forward.matched_weight,
                    partner_lines=forward.partner_lines,
                    shared_labels=forward.labels,
                ))
            if not overlaps:
                continue
            # Coverage first, because that is what the row reports and a ranking
            # that disagreed with its own headline number would be unreadable.
            # Where two partners cover the same share, the one with more of its
            # own trade behind the overlap is the better answer.
            overlaps.sort(key=lambda entry: (-entry.coverage, -entry.partner_lines,
                                             -entry.addressable_spend, entry.partner_key))
            results[supplier_key] = overlaps[:self.settings.max_partners_retained]
        return results

    def _reach(self, supplier_key: str, portfolios: Dict[str, Portfolio],
               index: Dict[str, List[str]], crowded: Set[str]) -> Dict[str, Reach]:
        """How far each other supplier reaches into this one's portfolio."""
        portfolio = portfolios[supplier_key]
        if portfolio.total_weight <= 0:
            return {}

        # For each candidate partner, the best match found for each of this
        # supplier's items. Held as a maximum rather than a sum because an item
        # is either covered or it is not; a partner who stocks three things all
        # resembling one of ours has still only covered that one thing.
        best: Dict[str, Dict[str, float]] = defaultdict(dict)
        # The partner's own items that took part in the overlap, kept so that
        # the partner's standing in them can be weighed alongside our share.
        engaged: Dict[str, Set[str]] = defaultdict(set)

        for item_key in sorted(portfolio.weights):
            if not self.space.items[item_key].is_named or item_key in crowded:
                continue
            for partner_key in index.get(item_key, ()):
                if partner_key != supplier_key:
                    best[partner_key][item_key] = 1.0
                    engaged[partner_key].add(item_key)
            for neighbour_key, score in self.space.neighbours.get(item_key, ()):
                if neighbour_key in crowded:
                    continue
                for partner_key in index.get(neighbour_key, ()):
                    if partner_key == supplier_key:
                        continue
                    matches = best[partner_key]
                    if matches.get(item_key, 0.0) < score:
                        matches[item_key] = score
                    engaged[partner_key].add(neighbour_key)

        reaches: Dict[str, Reach] = {}
        for partner_key, matches in best.items():
            partner = portfolios[partner_key]
            reach = Reach(partner_lines=sum(partner.lines.get(key, 0)
                                            for key in engaged[partner_key]))
            contributions: List[Tuple[float, str]] = []
            for item_key, score in matches.items():
                weight = portfolio.weights.get(item_key, 0.0)
                reach.matched_weight += weight * score
                reach.addressable_spend += portfolio.spend.get(item_key, 0.0) * score
                if score >= 1.0:
                    reach.exact_items += 1
                    reach.exact_weight += weight
                else:
                    reach.similar_items += 1
                contributions.append((weight * score, self.space.items[item_key].label))

            reach.coverage = reach.matched_weight / portfolio.total_weight
            if reach.coverage < self._retention_floor:
                continue
            reach.labels = [label for _, label in
                            sorted(contributions, key=lambda entry: (-entry[0], entry[1]))[:3]]
            reaches[partner_key] = reach
        return reaches


# ===========================================================================
# Rating
# ===========================================================================

class Rater:
    """Turns an overlap into a band, a confidence and a sentence.

    Three separate judgements, kept in three separate columns because they
    answer three different questions and collapsing them loses the ones a
    reviewer needs. The similarity band says how alike the portfolios are. The
    consolidation rating says whether that likeness is worth acting on. The
    confidence says how much evidence stands behind either statement.
    """

    # Lines behind a portfolio at which the evidence stops being the limiting
    # factor. Below it, confidence is reduced in proportion.
    _EVIDENCE_SATURATION = 25

    def __init__(self, settings: Settings, space: ItemSpace) -> None:
        self.settings = settings
        self.space = space

    def similarity_band(self, coverage: float) -> str:
        if coverage >= self.settings.high_similarity:
            return "High"
        if coverage >= self.settings.medium_similarity:
            return "Medium"
        return "Low"

    def consolidation_band(self, overlap: Overlap, portfolio: Portfolio) -> str:
        """The band the similarity earns once materiality is taken into account.

        Demotion only, never promotion. A pair that is not similar does not
        become an opportunity because a lot of money passes through it, but a
        pair that is similar stops being one when the money at stake would not
        pay for the sourcing exercise, or when the whole finding rests on a
        handful of lines.
        """
        band = self.similarity_band(overlap.coverage)
        if band == "Low":
            return "Low"

        # Where the scope is weighted by lines, no spend figure is available to
        # test, so the materiality gate is skipped rather than failed.
        spend_known = portfolio.total_spend > 0
        if band == "High":
            if spend_known and overlap.addressable_spend < self.settings.min_addressable_spend:
                band = "Medium"
            elif portfolio.total_lines < self.settings.min_evidence_lines:
                band = "Medium"
            elif overlap.partner_lines < self.settings.min_evidence_lines:
                # The partner barely trades in the shared items, so it is not
                # yet an alternative source however cleanly the groups match.
                band = "Medium"
            elif overlap.confidence < 50:
                band = "Medium"
        if band == "Medium" and portfolio.total_lines < 2:
            band = "Low"
        return band

    def confidence(self, overlap: Overlap, portfolio: Portfolio) -> int:
        """How much the reported similarity should be believed.

        Distinct from the similarity itself. A supplier with four lines can show
        a coverage of 100% and it means very little; the same figure over four
        hundred lines is close to a fact. Confidence carries that difference so
        that the similarity column can stay a clean measurement.
        """
        # Evidence: how much of the supplier there is to reason about.
        evidence = self._sufficiency(portfolio.total_lines)

        # Interpretability: weight sitting in the unidentified residue supports
        # no statement about what this supplier sells.
        named = portfolio.named_share

        # Directness: an overlap built on identical purchase groups is a
        # stronger claim than one assembled from near neighbours.
        direct = overlap.exact_share

        # The other side of the same question. Coverage is measured entirely
        # from this supplier's portfolio, so nothing in it notices that the
        # partner may have bought the shared item once by accident.
        support = self._sufficiency(overlap.partner_lines)

        score = ((0.45 + 0.55 * evidence) * (0.55 + 0.45 * named)
                 * (0.80 + 0.20 * direct) * (0.55 + 0.45 * support))
        return format_percent(score)

    def _sufficiency(self, lines: int) -> float:
        """How close a line count comes to being enough to argue from.

        Logarithmic because the first few lines carry most of the information:
        the step from one line to five settles whether a supplier trades in
        something at all, while the step from a hundred to five hundred only
        refines a figure that was already sound.
        """
        return min(1.0, math.log1p(max(0, lines)) / math.log1p(self._EVIDENCE_SATURATION))

    def reason(self, overlap: Overlap, portfolio: Portfolio, supplier: Supplier,
               partner: Supplier, basis: str) -> str:
        """The finding in plain words, for a reader who will not open the code."""
        measure = "spend" if basis == "spend" else "purchase lines"
        text = (f"{format_percent(overlap.coverage)}% of {supplier.display_name} "
                f"{measure} in {portfolio.scope.value} is on items "
                f"{partner.display_name} also supplies")
        if overlap.shared_labels:
            text += ", chiefly " + ", ".join(overlap.shared_labels[:3])

        counts = []
        if overlap.exact_items:
            counts.append(f"{overlap.exact_items} purchase "
                          f"{'group' if overlap.exact_items == 1 else 'groups'} shared exactly")
        if overlap.similar_items:
            counts.append(f"{overlap.similar_items} matched by similarity")
        if counts:
            text += " (" + ", ".join(counts) + ")"
        text += "."

        if overlap.reverse_coverage >= overlap.coverage + 0.15:
            text += (f" {partner.display_name} has the narrower portfolio, so it is the "
                     f"likelier candidate to be consolidated away.")
        elif overlap.coverage >= overlap.reverse_coverage + 0.15:
            text += (f" {partner.display_name} has the broader portfolio and could "
                     f"absorb this volume.")

        if overlap.partner_lines < self.settings.min_evidence_lines:
            text += (f" Note that {partner.display_name} has only "
                     f"{overlap.partner_lines} line(s) of its own on those items.")
        return text


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
    model off for the rest of the run: nothing is lost, because every call site
    already has a deterministic path to fall back on, and the work completed so
    far is kept.

    The estimate is derived from the token counts the API reports, so it lags
    the true figure by at most one request and cannot account for discounts
    applied at invoicing. It is a guard rail, not an accounting record.
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
        """Guard state for the run manifest.

        A run whose model was switched off part way through is not comparable
        with one that had it throughout, and the manifest is where that
        difference has to be visible.
        """
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
        print("  Answering no finishes the run on the local stack alone.")
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
            "The run continues on the local stack; cached answers are still used.",
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
            return json.loads(self.cache_path.read_text(encoding="utf-8")).get("entries", {})
        except (OSError, json.JSONDecodeError):
            return {}

    def save_cache(self) -> None:
        if not self._cache_dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps({
            "agent": AGENT_NAME, "model": self.config.model,
            "backend": self.config.backend,
            "entries": dict(sorted(self._cache.items())),
        }, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        self._cache_dirty = False

    def cache_key(self, task: str, payload: str) -> str:
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
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.config.api_key}",
                   "api-key": self.config.api_key}
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
            self.usage.failed_requests += 1
            return None

    def complete_json(self, system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
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
    """Recover a JSON object from a model reply by brace balance."""
    content = content.strip()
    if not content:
        return None
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = content.find("{")
    if start < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for position in range(start, len(content)):
        char = content[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
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
                    parsed = json.loads(content[start:position + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


# ===========================================================================
# Orchestration
# ===========================================================================

class Agent4:
    """Runs the supplier consolidation analysis end to end."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lexicon = Lexicon.load(settings.lexicon_path)
        self.registry = SupplierRegistry(settings.registry_path)
        self.resolver = SupplierResolver(self.lexicon, self.registry)
        self.space = ItemSpace(settings)
        self.builder = PortfolioBuilder(settings, self.lexicon, self.resolver, self.space)
        self.engine = OverlapEngine(settings, self.space)
        self.rater = Rater(settings, self.space)

        self.model: Optional[LanguageModelClient] = None
        if settings.model.enabled:
            self.model = LanguageModelClient(
                settings.model, settings.cache_dir / "agent4_model_cache.json",
                interactive=settings.interactive)

        self.table: Optional[InputTable] = None
        self.run_id = ""
        self.overlaps: Dict[str, Dict[str, List[Overlap]]] = {}
        self.statistics: Counter = Counter()

    # -- analysis -----------------------------------------------------------

    def compare(self) -> None:
        """Compare every supplier against every plausible partner, per scope."""
        for scope_key in sorted(self.builder.portfolios):
            portfolios = self.builder.portfolios[scope_key]
            if len(portfolios) < 2:
                self.statistics["scopes_with_one_supplier"] += 1
                continue
            if len(portfolios) > self.settings.max_scope_suppliers:
                LOGGER.warning(
                    "Skipping %s: %d suppliers exceeds the limit of %d. Compare within a "
                    "narrower level, or raise --max-scope-suppliers if the run time is "
                    "acceptable.", scope_key, len(portfolios),
                    self.settings.max_scope_suppliers)
                self.statistics["scopes_skipped_as_too_broad"] += 1
                continue
            results = self.engine.compare(portfolios)
            if not results:
                continue
            self.overlaps[scope_key] = results
            self.statistics["supplier_scope_rows"] += len(results)
            LOGGER.info("  %s: %d supplier(s), %d with at least one overlapping partner",
                        scope_key, len(portfolios), len(results))
        self.statistics.update(self.engine.statistics)

    def rate(self) -> None:
        """Attach a band, a confidence and a rationale to every overlap."""
        for scope_key in sorted(self.overlaps):
            portfolios = self.builder.portfolios[scope_key]
            for supplier_key in sorted(self.overlaps[scope_key]):
                portfolio = portfolios[supplier_key]
                for overlap in self.overlaps[scope_key][supplier_key]:
                    overlap.confidence = self.rater.confidence(overlap, portfolio)
                    overlap.similarity_band = self.rater.similarity_band(overlap.coverage)
                    overlap.consolidation = self.rater.consolidation_band(overlap, portfolio)
                    self.statistics[f"band_{overlap.consolidation.lower()}"] += 1

    def adjudicate(self) -> None:
        """Ask the model about the pairs sitting on the High boundary.

        Confined to the primary scope and to the band around the one threshold
        where an opinion changes a rating, then ordered by the money at stake
        and capped. A pair at 90% and a pair at 15% are both already decided, so
        a token spent on either buys nothing. Verdicts are cached on the pair,
        so the cost is paid once for the life of the data.
        """
        if self.model is None or not self.model.config.enabled or not self.overlaps:
            return

        margin = self.settings.adjudication_margin
        low = self.settings.high_similarity - margin
        high = self.settings.high_similarity + margin

        borderline: List[Tuple[float, str, str, Overlap]] = []
        for scope_key in sorted(self.overlaps):
            if self.builder.scopes[scope_key].level != self.settings.primary_scope:
                continue
            for supplier_key in sorted(self.overlaps[scope_key]):
                for overlap in self.overlaps[scope_key][supplier_key][:self.settings.top_partners]:
                    if low <= overlap.coverage <= high:
                        borderline.append((overlap.addressable_spend, scope_key,
                                           supplier_key, overlap))

        if not borderline:
            LOGGER.info("No pair fell in the adjudication band; the model was not needed.")
            return

        borderline.sort(key=lambda entry: (-entry[0], entry[1], entry[2], entry[3].partner_key))
        borderline = borderline[:self.settings.max_adjudications]
        LOGGER.info("Asking %s to adjudicate %d borderline supplier pair(s).",
                    self.model.config.model, len(borderline))

        system_prompt = (
            "You advise on procurement supplier consolidation for an energy company.\n"
            "You are given two suppliers and the things each of them was actually paid "
            "for within one category. Judge whether the second supplier is a credible "
            "alternative source for what the first one supplies.\n"
            "Say yes when the two sell the same kinds of goods or perform the same kinds "
            "of work, even if the wording differs. Say no when the apparent overlap comes "
            "from generic items that any supplier would carry, or when the two are "
            "plainly in different lines of business.\n"
            'Reply with JSON: {"verdicts": {"<id>": {"comparable": true|false, '
            '"reason": "<at most 14 words>"}}}.'
        )

        batch_size = max(1, self.model.config.batch_size)
        for start in range(0, len(borderline), batch_size):
            batch = borderline[start:start + batch_size]
            outstanding: List[Tuple[str, str, Overlap]] = []

            for _, scope_key, supplier_key, overlap in batch:
                payload = self._adjudication_payload(scope_key, supplier_key, overlap)
                cache_key = self.model.cache_key("consolidation", json.dumps(
                    payload, ensure_ascii=False, sort_keys=True))
                cached = self.model.cached(cache_key)
                if cached is not None:
                    self._apply_verdict(overlap, cached)
                else:
                    outstanding.append((cache_key, json.dumps(payload, ensure_ascii=False),
                                        overlap))

            if not outstanding:
                continue

            lines = []
            for position, (_, payload, _) in enumerate(outstanding):
                lines.append(f'"{position}": {payload}')
            user_prompt = "Supplier pairs to judge:\n{\n" + ",\n".join(lines) + "\n}"

            response = self.model.complete_json(system_prompt, user_prompt)
            verdicts = (response or {}).get("verdicts") or {}
            for position, (cache_key, _, overlap) in enumerate(outstanding):
                verdict = verdicts.get(str(position)) or {}
                if not isinstance(verdict, dict) or "comparable" not in verdict:
                    continue
                serialised = json.dumps({
                    "comparable": bool(verdict.get("comparable")),
                    "reason": normalise_text(verdict.get("reason", ""))[:120],
                }, ensure_ascii=False)
                self.model.store(cache_key, serialised)
                self._apply_verdict(overlap, serialised)
                self.statistics["pairs_adjudicated"] += 1

        self.model.save_cache()

    def _adjudication_payload(self, scope_key: str, supplier_key: str,
                              overlap: Overlap) -> Dict[str, Any]:
        portfolios = self.builder.portfolios[scope_key]
        portfolio = portfolios[supplier_key]
        partner = portfolios[overlap.partner_key]
        supplier = self.resolver.by_key.get(supplier_key)
        partner_supplier = self.resolver.by_key.get(overlap.partner_key)
        return {
            "category": self.builder.scopes[scope_key].value,
            "supplier": supplier.display_name if supplier else supplier_key,
            "supplier_buys": portfolio.top_items(self.space),
            "alternative": partner_supplier.display_name if partner_supplier else overlap.partner_key,
            "alternative_buys": partner.top_items(self.space),
        }

    def _apply_verdict(self, overlap: Overlap, serialised: str) -> None:
        """Record a model verdict without letting it overwrite the measurement.

        A verdict can only move the consolidation rating, never the similarity.
        The similarity is a computed share of a portfolio and stays what it was
        measured to be; whether that share represents a credible sourcing option
        is the judgement, and that is the only thing the model is asked for.
        """
        try:
            verdict = json.loads(serialised)
        except json.JSONDecodeError:
            return
        comparable = bool(verdict.get("comparable"))
        reason = normalise_text(verdict.get("reason", ""))
        overlap.adjudication = ("comparable" if comparable else "not comparable") + (
            f": {reason}" if reason else "")
        if not comparable and overlap.consolidation == "High":
            overlap.consolidation = "Medium"
        elif comparable and overlap.consolidation == "Medium" and \
                overlap.coverage >= self.settings.high_similarity:
            overlap.consolidation = "High"

    # -- output -------------------------------------------------------------

    def write(self) -> Dict[str, Any]:
        directory = self.settings.results_dir
        directory.mkdir(parents=True, exist_ok=True)
        outputs: List[str] = []

        by_key = self.resolver.by_key

        consolidation_path = directory / "agent4_supplier_consolidation.csv"
        rows = self._build_rows(by_key)
        self._write_consolidation(consolidation_path, rows)
        outputs.append(consolidation_path.name)

        if self.settings.write_jsonl:
            jsonl_path = directory / "agent4_supplier_consolidation.jsonl"
            self._write_jsonl(jsonl_path, by_key)
            outputs.append(jsonl_path.name)

        pairs_path = directory / "agent4_supplier_pairs.csv"
        pair_count = self._write_pairs(pairs_path, by_key)
        outputs.append(pairs_path.name)

        master_path = directory / "agent4_supplier_master.csv"
        self._write_master(master_path)
        outputs.append(master_path.name)

        self.registry.save()
        if self.model is not None:
            self.model.save_cache()

        statistics = dict(self.statistics)
        statistics.update({
            "rows_total": len(self.table.rows) if self.table else 0,
            "suppliers": len(self.resolver.suppliers),
            "purchase_items": len(self.space.items),
            "item_relations": len(self.space.neighbours),
            "supplier_pairs": pair_count,
            "duplicate_vendor_flags": sum(
                1 for supplier in self.resolver.suppliers.values()
                if supplier.possible_duplicate_of),
        })
        statistics.update(self.builder.statistics)
        if self.model is not None:
            statistics["token_usage"] = {
                **self.model.usage.as_dict(),
                **self.model.guard.as_dict(),
            }

        manifest = {
            "agent": AGENT_NAME,
            "version": AGENT_VERSION,
            "run_id": self.run_id,
            "input": str(self.settings.input_path),
            "lexicon_version": self.lexicon.version,
            "item_relation_method": self.space.method,
            "scopes": sorted(self.builder.portfolios),
            "primary_scope": self.settings.primary_scope,
            "weight_basis": dict(sorted(self.builder.weight_basis.items())),
            "thresholds": {
                "high_similarity": self.settings.high_similarity,
                "medium_similarity": self.settings.medium_similarity,
                "report_similarity": self.settings.report_similarity,
                "min_addressable_spend": self.settings.min_addressable_spend,
                "item_similarity_floor": self.settings.item_similarity_floor,
            },
            "environment": describe_environment(),
            "statistics": statistics,
            "outputs": outputs,
        }
        manifest_path = directory / "agent4_run_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2,
                                            sort_keys=True), encoding="utf-8")
        manifest["outputs"] = outputs + [manifest_path.name]
        return manifest

    CONSOLIDATION_COLUMNS = [
        "Scope_Level", "Scope_Value", "Is_Primary_Scope",
        "Supplier_Key", "Supplier_Name", "Supplier_Ids",
        "Supplier_Lines", "Supplier_Spend_EUR", "Distinct_Purchase_Items",
        "Weight_Basis", "Grouped_Share_Percent",
        "Consolidation_Potential", "Similarity_Band", "Similarity_Percent", "AI_Confidence",
        "Most_Similar_Supplier", "Most_Similar_Supplier_Key",
        "Reverse_Similarity_Percent", "Mutual_Similarity_Percent",
        "Addressable_Spend_EUR", "Shared_Items_Exact", "Shared_Items_Similar",
        "Partner_Lines_On_Shared_Items", "Same_Country", "Same_Business_Area",
        "Overlapping_Supplier_Count", "Top_5_Other_Similar_Suppliers",
        "Possible_Duplicate_Vendor", "Model_Verdict", "Reason", "Agent4_Run_Id",
    ]

    def _build_rows(self, by_key: Dict[str, Supplier]) -> List[List[Any]]:
        """One row per supplier per scope, best partner first."""
        rows: List[List[Any]] = []

        for scope_key in sorted(self.overlaps):
            scope = self.builder.scopes[scope_key]
            portfolios = self.builder.portfolios[scope_key]
            basis = self.builder.weight_basis.get(scope_key, "lines")

            for supplier_key in sorted(self.overlaps[scope_key]):
                overlaps = self.overlaps[scope_key][supplier_key]
                if not overlaps:
                    continue
                portfolio = portfolios[supplier_key]
                supplier = by_key.get(supplier_key)
                if supplier is None:
                    continue
                best = overlaps[0]
                partner = by_key.get(best.partner_key)
                if partner is None:
                    continue

                others = "; ".join(
                    f"{by_key[entry.partner_key].display_name} "
                    f"{format_percent(entry.coverage)}%"
                    for entry in overlaps[1:1 + self.settings.top_partners]
                    if entry.partner_key in by_key)

                supplier_country = portfolio.countries.most_common(1)
                partner_country = portfolios[best.partner_key].countries.most_common(1)

                rows.append([
                    scope.level, scope.value,
                    "Yes" if scope.level == self.settings.primary_scope else "No",
                    supplier.key, supplier.display_name,
                    "; ".join(sorted(supplier.identifiers)),
                    portfolio.total_lines, round(portfolio.total_spend, 2),
                    len(portfolio.weights), basis,
                    format_percent(portfolio.named_share),
                    best.consolidation, best.similarity_band,
                    format_percent(best.coverage), best.confidence,
                    partner.display_name, partner.key,
                    format_percent(best.reverse_coverage),
                    format_percent(best.mutual),
                    round(best.addressable_spend, 2),
                    best.exact_items, best.similar_items, best.partner_lines,
                    self._same(supplier_country, partner_country),
                    self._same_text(supplier.dominant_business_area(),
                                    partner.dominant_business_area()),
                    len(overlaps), others,
                    by_key[supplier.possible_duplicate_of].display_name
                    if supplier.possible_duplicate_of in by_key else "",
                    best.adjudication,
                    self.rater.reason(best, portfolio, supplier, partner, basis),
                    self.run_id,
                ])

        # Primary scope first, then the strongest opportunities, so that the
        # file opens on the rows a reviewer should read.
        band_order = {"High": 0, "Medium": 1, "Low": 2}
        rows.sort(key=lambda row: (
            0 if row[2] == "Yes" else 1,
            band_order.get(str(row[11]), 3),
            -float(row[13]),
            -float(row[19] or 0.0),
            str(row[4]),
        ))
        return rows

    @staticmethod
    def _same(left: Sequence[Tuple[str, int]], right: Sequence[Tuple[str, int]]) -> str:
        if not left or not right:
            return ""
        return "Yes" if left[0][0] == right[0][0] else "No"

    @staticmethod
    def _same_text(left: str, right: str) -> str:
        if not left or not right:
            return ""
        return "Yes" if lookup_key(left) == lookup_key(right) else "No"

    def _write_consolidation(self, path: Path, rows: Sequence[Sequence[Any]]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.CONSOLIDATION_COLUMNS)
            writer.writerows(rows)
        LOGGER.info("Wrote %d supplier-scope row(s) to %s", len(rows), path.name)

    def _write_jsonl(self, path: Path, by_key: Dict[str, Supplier]) -> None:
        """The same analysis with every retained partner, not only the top five.

        The CSV is what a category manager reads; this is what a later step
        joins against, so nothing that was computed is dropped from it.
        """
        with path.open("w", encoding="utf-8", newline="") as handle:
            for scope_key in sorted(self.overlaps):
                scope = self.builder.scopes[scope_key]
                portfolios = self.builder.portfolios[scope_key]
                for supplier_key in sorted(self.overlaps[scope_key]):
                    supplier = by_key.get(supplier_key)
                    if supplier is None:
                        continue
                    portfolio = portfolios[supplier_key]
                    record = {
                        "run_id": self.run_id,
                        "scope_level": scope.level,
                        "scope_value": scope.value,
                        "supplier_key": supplier.key,
                        "supplier_name": supplier.display_name,
                        "supplier_ids": sorted(supplier.identifiers),
                        "lines": portfolio.total_lines,
                        "spend_eur": round(portfolio.total_spend, 2),
                        "weight_basis": self.builder.weight_basis.get(scope_key, "lines"),
                        "grouped_share": round(portfolio.named_share, 4),
                        "top_purchase_items": portfolio.top_items(self.space),
                        "partners": [
                            {
                                "supplier_key": overlap.partner_key,
                                "supplier_name": by_key[overlap.partner_key].display_name
                                if overlap.partner_key in by_key else overlap.partner_key,
                                "similarity": round(overlap.coverage, 4),
                                "reverse_similarity": round(overlap.reverse_coverage, 4),
                                "mutual_similarity": round(overlap.mutual, 4),
                                "addressable_spend_eur": round(overlap.addressable_spend, 2),
                                "shared_items_exact": overlap.exact_items,
                                "shared_items_similar": overlap.similar_items,
                                "partner_lines_on_shared_items": overlap.partner_lines,
                                "shared_item_labels": overlap.shared_labels,
                                "confidence": overlap.confidence,
                                "similarity_band": overlap.similarity_band,
                                "consolidation_potential": overlap.consolidation,
                                "model_verdict": overlap.adjudication,
                            }
                            for overlap in self.overlaps[scope_key][supplier_key]
                        ],
                    }
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _write_pairs(self, path: Path, by_key: Dict[str, Supplier]) -> int:
        """One row per supplier pair, written once rather than once per side.

        Both directional shares are on the row, so the pair can be read without
        having to find its mirror image elsewhere in the file.
        """
        columns = ["Scope_Level", "Scope_Value", "Supplier_A_Key", "Supplier_A",
                   "Supplier_B_Key", "Supplier_B", "A_Covered_By_B_Percent",
                   "B_Covered_By_A_Percent", "Mutual_Similarity_Percent",
                   "Shared_Items_Exact", "Shared_Items_Similar",
                   "Addressable_Spend_A_EUR", "Addressable_Spend_B_EUR",
                   "Combined_Spend_EUR", "Similarity_Band", "Consolidation_Potential",
                   "AI_Confidence", "Top_Shared_Items", "Model_Verdict", "Agent4_Run_Id"]

        seen: Set[Tuple[str, str, str]] = set()
        rows: List[Tuple[float, List[Any]]] = []

        for scope_key in sorted(self.overlaps):
            scope = self.builder.scopes[scope_key]
            portfolios = self.builder.portfolios[scope_key]
            for supplier_key in sorted(self.overlaps[scope_key]):
                for overlap in self.overlaps[scope_key][supplier_key]:
                    first, second = sorted((supplier_key, overlap.partner_key))
                    identity = (scope_key, first, second)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if first not in by_key or second not in by_key:
                        continue

                    # The stored overlap is directional. When the pair is
                    # emitted under the other supplier's key, the two shares
                    # swap places so that column A always describes supplier A.
                    if first == supplier_key:
                        a_by_b, b_by_a = overlap.coverage, overlap.reverse_coverage
                        spend_a, spend_b = overlap.addressable_spend, overlap.partner_spend
                    else:
                        a_by_b, b_by_a = overlap.reverse_coverage, overlap.coverage
                        spend_a, spend_b = overlap.partner_spend, overlap.addressable_spend

                    combined = (portfolios[first].total_spend + portfolios[second].total_spend)
                    rows.append((max(a_by_b, b_by_a), [
                        scope.level, scope.value,
                        first, by_key[first].display_name,
                        second, by_key[second].display_name,
                        format_percent(a_by_b), format_percent(b_by_a),
                        format_percent(min(a_by_b, b_by_a)),
                        overlap.exact_items, overlap.similar_items,
                        round(spend_a, 2), round(spend_b, 2), round(combined, 2),
                        overlap.similarity_band, overlap.consolidation,
                        overlap.confidence, "; ".join(overlap.shared_labels),
                        overlap.adjudication, self.run_id,
                    ]))

        rows.sort(key=lambda entry: (-entry[0], -float(entry[1][13]), entry[1][3], entry[1][5]))
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for _, values in rows:
                writer.writerow(values)
        LOGGER.info("Wrote %d supplier pair(s) to %s", len(rows), path.name)
        return len(rows)

    def _write_master(self, path: Path) -> None:
        """The derived vendor master, with the duplicate suggestions.

        Written on every run because it is the assumption the rest of the output
        rests on. If two rows here are the same company, every consolidation
        figure involving either of them is wrong, and this is the one file where
        that can be seen at a glance and corrected.
        """
        columns = ["Supplier_Key", "Canonical_Supplier_Name", "Normalised_Key",
                   "Raw_Name_Variants", "Supplier_Ids", "Lines", "Spend_EUR",
                   "Countries", "Business_Areas", "Possible_Duplicate_Of",
                   "Duplicate_Evidence"]

        by_key = self.resolver.by_key
        suppliers = sorted(self.resolver.suppliers.values(),
                           key=lambda supplier: (-supplier.spend, -supplier.lines,
                                                 supplier.display_name))

        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for supplier in suppliers:
                duplicate = by_key.get(supplier.possible_duplicate_of)
                writer.writerow([
                    supplier.key, supplier.display_name, supplier.canonical_key,
                    "; ".join(name for name, _ in sorted(
                        supplier.raw_names.items(), key=lambda item: (-item[1], item[0]))),
                    "; ".join(sorted(supplier.identifiers)),
                    supplier.lines, round(supplier.spend, 2),
                    "; ".join(name for name, _ in supplier.countries.most_common(3)),
                    "; ".join(name for name, _ in supplier.business_areas.most_common(3)),
                    duplicate.display_name if duplicate else "",
                    supplier.duplicate_evidence,
                ])

    # -- entry point --------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        """Execute the full analysis."""
        self.table = read_purchase_table(self.settings.input_path)
        self.run_id = stable_hash("agent4-run", self.settings.input_path.name,
                                  str(len(self.table.rows)), AGENT_VERSION)

        self.resolver.ingest(self.table.rows)
        if len(self.resolver.suppliers) < 2:
            raise SystemExit(
                "Fewer than two distinct suppliers were found, so there is nothing to "
                "consolidate. Check that the supplier column is populated in the input.")

        self.builder.build(self.table)
        self.space.build_neighbours()
        self.compare()
        self.rate()
        self.adjudicate()
        return self.write()


# ===========================================================================
# Command line interface
# ===========================================================================

BANNER = r"""
===============================================================================
 Fortum AI-Powered Procurement Analysis
 Agent 4 - AI Supplier Consolidation
 Prof. Shahab Anbarjafari
===============================================================================
""".strip("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent4.py",
        description="Agent 4 - find supplier overlap and rank consolidation opportunities.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python agent4.py\n"
            "      Prompt for each path in turn and run with the defaults.\n\n"
            "  python agent4.py --non-interactive --input results/agent2_purchase_groups.csv\n"
            "      Run unattended over the six default scope levels.\n\n"
            "  python agent4.py --non-interactive --scopes Category_L2 Division\n"
            "      Restrict the comparison to two levels, which is much faster.\n\n"
            "  python agent4.py --non-interactive --high-similarity 0.70\n"
            "      Raise the bar for a High rating after reviewing the first run.\n"
        ),
    )
    paths = parser.add_argument_group("paths")
    paths.add_argument("--input", metavar="FILE", help="purchase table from Agent 2")
    paths.add_argument("--results", metavar="DIR", help="folder to write results into")
    paths.add_argument("--lexicon", metavar="FILE", help="controlled vocabulary JSON file")
    paths.add_argument("--registry", metavar="FILE", help="supplier registry JSON file")
    paths.add_argument("--cache", metavar="DIR", help="folder for the model response cache")

    scopes = parser.add_argument_group("scope")
    scopes.add_argument("--scopes", nargs="+", metavar="LEVEL", default=list(DEFAULT_SCOPE_LEVELS),
                        help="columns to compare within (default: the four category "
                             "levels plus Business_Area and Division)")
    scopes.add_argument("--primary-scope", default=PRIMARY_SCOPE_LEVEL,
                        help=f"level the headline ranking uses (default {PRIMARY_SCOPE_LEVEL})")

    bands = parser.add_argument_group("similarity bands")
    bands.add_argument("--high-similarity", type=float, default=0.60,
                       help="share of a portfolio covered for a High band (default 0.60)")
    bands.add_argument("--medium-similarity", type=float, default=0.30,
                       help="share of a portfolio covered for a Medium band (default 0.30)")
    bands.add_argument("--report-similarity", type=float, default=0.10,
                       help="share below which a partner is not reported (default 0.10)")
    bands.add_argument("--min-addressable-spend", type=float, default=10_000.0,
                       help="spend at stake before a High rating stands (default 10000)")
    bands.add_argument("--min-evidence-lines", type=int, default=3,
                       help="lines each side needs before a High rating stands "
                            "(default 3)")
    bands.add_argument("--item-similarity-floor", type=float, default=0.75,
                       help="similarity between two purchase groups for partial "
                            "credit (default 0.75)")
    bands.add_argument("--top-partners", type=int, default=5,
                       help="alternative suppliers listed per row (default 5)")

    limits = parser.add_argument_group("limits")
    limits.add_argument("--item-cap", type=int, default=60_000,
                        help="distinct purchase items beyond which only exact "
                             "matches are used (default 60000)")
    limits.add_argument("--max-item-fanout", type=int, default=2000,
                        help="suppliers holding an item before it is ignored as "
                             "undiscriminating (default 2000)")
    limits.add_argument("--max-scope-suppliers", type=int, default=5000,
                        help="suppliers in one scope before it is skipped as too "
                             "broad to compare (default 5000)")

    tiers = parser.add_argument_group("processing tiers")
    tiers.add_argument("--no-embeddings", action="store_true",
                       help="relate purchase items lexically instead of by embedding")
    tiers.add_argument("--use-llm", action="store_true",
                       help="let the language model adjudicate borderline supplier pairs")
    tiers.add_argument("--llm-spend-limit", metavar="USD", type=float, default=None,
                       help="pause and ask once estimated model spend reaches this "
                            f"figure (default {DEFAULT_SPEND_LIMIT:.2f}; 0 disables the alert)")

    parser.add_argument("--no-jsonl", action="store_true", help="skip the JSONL export")
    parser.add_argument("--non-interactive", action="store_true",
                        help="never prompt; use the supplied arguments and defaults")
    parser.add_argument("--verbose", action="store_true", help="emit debug-level logging")
    parser.add_argument("--version", action="version", version=f"{AGENT_NAME} {AGENT_VERSION}")
    return parser


def _clean_path_input(value: str) -> str:
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
    default_results = Path(args.results) if args.results else here / "results"

    if args.input:
        default_input = Path(args.input)
    else:
        default_input = default_results / "agent2_purchase_groups.csv"

    default_lexicon = Path(args.lexicon) if args.lexicon else here / "lexicon" / "procurement_lexicon.json"
    default_registry = (Path(args.registry) if args.registry
                        else here / "lexicon" / "agent4_supplier_registry.json")
    default_cache = Path(args.cache) if args.cache else here / "cache"

    scope_levels = tuple(dict.fromkeys(args.scopes)) or DEFAULT_SCOPE_LEVELS
    use_embeddings = not args.no_embeddings
    use_llm = args.use_llm
    spend_limit = (args.llm_spend_limit if args.llm_spend_limit is not None
                   else _env_float(env.get("LLM_SPEND_LIMIT"), DEFAULT_SPEND_LIMIT))

    if not args.non_interactive:
        print(BANNER)
        print("\nPress Enter to accept the value shown in brackets.\n")
        default_input = Path(ask("Purchase table (from Agent 2)", str(default_input)))
        default_results = Path(ask("Results folder", str(default_results)))
        default_lexicon = Path(ask("Controlled vocabulary file", str(default_lexicon)))
        default_registry = Path(ask("Supplier registry file", str(default_registry)))
        default_cache = Path(ask("Cache folder", str(default_cache)))

        print()
        answer = ask("Scope levels to compare within, separated by spaces",
                     " ".join(scope_levels))
        scope_levels = tuple(dict.fromkeys(answer.split())) or DEFAULT_SCOPE_LEVELS

        if _sentence_transformers is None:
            print("  Sentence embeddings are not installed; purchase items will be "
                  "related lexically.")
            use_embeddings = False
        else:
            use_embeddings = ask_yes_no(
                "Relate near-identical purchase groups with embeddings (recommended, free)?",
                True)
        use_llm = ask_yes_no(
            "Let the language model adjudicate borderline supplier pairs?", use_llm)
        if use_llm:
            print()
            print(f"  Charged at ${INPUT_COST_PER_MTOK:,.2f} per million input tokens and "
                  f"${OUTPUT_COST_PER_MTOK:,.2f} per million output tokens.")
            print("  The run pauses at the figure below and asks before spending more.")
            spend_limit = ask_amount(
                "Alert when estimated language-model spend reaches (USD)", spend_limit)
        print()

    primary = args.primary_scope if args.primary_scope in scope_levels else scope_levels[0]

    settings = Settings(
        input_path=default_input.expanduser().resolve(),
        results_dir=default_results.expanduser().resolve(),
        lexicon_path=default_lexicon.expanduser().resolve(),
        registry_path=default_registry.expanduser().resolve(),
        cache_dir=default_cache.expanduser().resolve(),
        scope_levels=scope_levels,
        primary_scope=primary,
        use_embeddings=use_embeddings,
        use_llm=use_llm,
        high_similarity=args.high_similarity,
        medium_similarity=args.medium_similarity,
        report_similarity=args.report_similarity,
        min_addressable_spend=args.min_addressable_spend,
        min_evidence_lines=args.min_evidence_lines,
        item_similarity_floor=args.item_similarity_floor,
        top_partners=args.top_partners,
        item_cap=args.item_cap,
        max_item_fanout=args.max_item_fanout,
        max_scope_suppliers=args.max_scope_suppliers,
        write_jsonl=not args.no_jsonl,
        verbose=args.verbose,
        interactive=not args.non_interactive,
    )
    settings.model = resolve_model_config(env, use_llm, spend_limit)

    if not (settings.report_similarity <= settings.medium_similarity
            <= settings.high_similarity):
        raise SystemExit("Similarity bands must satisfy report <= medium <= high")
    if not 0.0 < settings.item_similarity_floor <= 1.0:
        raise SystemExit("--item-similarity-floor must be between 0 and 1")
    return settings


def configure_logging(verbose: bool) -> None:
    configure_process_logging(verbose)


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
        print("  the remaining work was done on the local stack alone.")


def print_summary(manifest: Dict[str, Any], settings: Settings) -> None:
    statistics = manifest["statistics"]
    print()
    print("=" * 79)
    print(f"{AGENT_NAME} - complete")
    print("=" * 79)
    print(f"  Run id               : {manifest['run_id']}")
    print(f"  Purchase lines       : {statistics.get('rows_total', 0):,}")
    print(f"  Distinct suppliers   : {statistics.get('suppliers', 0):,}")
    print(f"  Purchase items       : {statistics.get('purchase_items', 0):,} "
          f"({statistics.get('item_relations', 0):,} with a near neighbour, "
          f"by {manifest['item_relation_method']})")

    duplicates = statistics.get("duplicate_vendor_flags", 0)
    if duplicates:
        print(f"  Duplicate vendors    : {duplicates:,} flagged in "
              f"agent4_supplier_master.csv")

    print(f"\n  Scopes compared      : {len(manifest['scopes']):,} "
          f"(primary level {settings.primary_scope})")
    print(f"  Supplier-scope rows  : {statistics.get('supplier_scope_rows', 0):,}")
    print(f"  Supplier pairs       : {statistics.get('supplier_pairs', 0):,}")

    bands = [(name, statistics.get(f"band_{name.lower()}", 0))
             for name in ("High", "Medium", "Low")]
    if any(count for _, count in bands):
        print("  Consolidation bands  : "
              + "  ".join(f"{name} {count:,}" for name, count in bands))
        print(f"    High is a partner covering at least "
              f"{format_percent(settings.high_similarity)}% of a supplier's portfolio "
              f"with at least {settings.min_addressable_spend:,.0f} EUR at stake.")

    adjudicated = statistics.get("pairs_adjudicated", 0)
    if adjudicated:
        print(f"  Adjudicated by model : {adjudicated:,}")

    print(f"\n  Output folder        : {settings.results_dir}")
    for name in manifest["outputs"]:
        print(f"    {name}")

    print_token_usage(statistics, settings)
    print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    env = load_dotenv(Path(__file__).resolve().parent / ".env")
    env.update({key: value for key, value in os.environ.items() if key in {
        "AZURE_ENABLE", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
        "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_MODEL",
        "BASE_URL", "MODEL_NAME", "LLM_BATCH_SIZE", "LLM_TIMEOUT", "LLM_MAX_REQUESTS",
    }})

    try:
        settings = resolve_settings(args, env)
    except SystemExit as error:
        print(f"\n{error}\n", file=sys.stderr)
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

    try:
        manifest = Agent4(settings).run()
    except SystemExit as error:
        print(f"\n{error}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.\n", file=sys.stderr)
        return 130

    print_summary(manifest, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
