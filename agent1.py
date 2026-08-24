#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 1 - Improved Purchase Description.

Turns free-text procurement lines into a clear, standardised English description
of what was actually bought, without discarding or overwriting anything the
source systems recorded.

    Background
    ----------
    Procurement spend arrives from four systems that describe the same purchase
    in different ways and in different languages. Sievo is the analytical master
    but its line description is frequently ``n/a``; the readable text for that
    same purchase often sits in the Maximo or Basware row that Sievo was derived
    from, or in the invoice line that settled it. A description written in
    Finnish, Swedish or Polish is invisible to an English-language analyst, and
    an entry such as ``157238asbestipurku - tuntiveloitus`` is close to useless
    even to a native speaker.

    Approach
    --------
    The agent gathers evidence for each purchase line from every system that
    says anything about it, renders that evidence in English, and composes a
    description from it. Composition starts from validated fragments. When the
    language model is on, each line is then read carefully and rewritten as one
    or two English sentences that still contain only facts from the source.
    Any word that cannot be traced back to the source data or to the controlled
    vocabulary is dropped before output.

    Cost
    ----
    The design target is high quality at negligible marginal cost. Work is done
    on the space of *distinct phrases* rather than the space of rows, which on
    real spend data is one to two orders of magnitude smaller. Within that
    space, a phrase is resolved by the controlled vocabulary first, then by a
    local neural translation model, and only then by a language model. Every
    resolution is cached on disk, so the second run of a data set consumes no
    tokens at all. When a language model is used, consumption is reported in
    full at the end of the run, including the reasoning tokens that are billed
    but never appear in the response.

    Repeatability
    -------------
    Row 6 of the AI development plan requires that a user be able to find the
    same material again in a later run. Every stage is therefore deterministic:
    lookups and templates rather than generation, sorted iteration order, a
    content-addressed cache, temperature 0 where a model is unavoidable, and no
    timestamps or random identifiers anywhere in the row output. Re-running on
    unchanged input reproduces the previous output byte for byte.

    Output
    ------
    Written to the results folder:

        agent1_<source>.csv         the original sheet with enrichment appended
        agent1_unified_lines.csv    common schema across all sources
        agent1_unified_lines.jsonl  the same rows with the full evidence bundle
        agent1_run_manifest.json    input hashes, configuration and statistics

    The unified table is the input contract for Agents 2, 3 and 4. For the
    production run, point ``--input`` at Max's ``max_stage3_interpreted.csv``
    (the master wide table) rather than at the raw extracts.

Usage:
    python agent1.py

Author:
    Prof. Shahab Anbarjafari
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
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple
from xml.etree import ElementTree

from runtime import (
    DEFAULT_AZURE_MODEL, DEFAULT_OPENAI_MODEL, DEFAULT_REASONING_EFFORT,
    chat_completion_body, configure_process_logging, load_sentence_transformer,
    retry_chat_body,
)

LOGGER = logging.getLogger("agent1")

AGENT_NAME = "Agent 1 - Improved Purchase Description"
AGENT_VERSION = "1.5.0"

# The CSV module refuses very long fields by default. Procurement free-text
# occasionally carries an entire pasted e-mail thread, and losing those rows to
# an exception would be worse than accepting the memory cost of reading them.
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))


# ===========================================================================
# Optional dependencies
# ===========================================================================
#
# Everything in this block improves quality or speed but nothing in it is
# mandatory. The agent inspects what is installed at start-up, reports it, and
# selects the best available implementation for each task. This keeps the agent
# runnable on a locked-down machine where only the standard library is present,
# which matters because the deployment target is not the development machine.

def _import_optional(module_path: str) -> Optional[Any]:
    """Import a module by dotted path, returning None when it is unavailable."""
    try:
        module = __import__(module_path, fromlist=["_"])
    except Exception:  # ImportError, but also the runtime errors that torch and
        return None    # its friends raise on an incompatible platform.
    return module


_rapidfuzz = _import_optional("rapidfuzz.fuzz")
_openpyxl = _import_optional("openpyxl")
_langdetect = _import_optional("langdetect")
_requests = _import_optional("requests")
_numpy = _import_optional("numpy")
_spacy = _import_optional("spacy")
_nltk = _import_optional("nltk")
_sentence_transformers = _import_optional("sentence_transformers")
_transformers = _import_optional("transformers")
_sklearn_neighbors = _import_optional("sklearn.neighbors")


def describe_environment() -> Dict[str, bool]:
    """Return the availability map that is printed at start-up and archived."""
    return {
        "openpyxl": _openpyxl is not None,
        "rapidfuzz": _rapidfuzz is not None,
        "numpy": _numpy is not None,
        "spacy": _spacy is not None,
        "nltk": _nltk is not None,
        "sentence-transformers": _sentence_transformers is not None,
        "transformers": _transformers is not None,
        "scikit-learn": _sklearn_neighbors is not None,
        "langdetect": _langdetect is not None,
        "requests": _requests is not None,
    }


# ===========================================================================
# Output schema
# ===========================================================================
#
# The unified record carries the answer first, then the evidence behind it, then
# the provenance needed to audit it. Agents 2 to 4 read this table, so the
# column names are part of the interface between the agents and are not to be
# renamed without updating the downstream readers.

UNIFIED_COLUMNS: Tuple[str, ...] = (
    # --- the deliverable ---------------------------------------------------
    "Enriched_Purchase_Description",
    "Enriched_Description_Short",
    "Item_Or_Service",
    "AI_Confidence",
    "Confidence_Band",
    # --- what the description was built from --------------------------------
    "Original_Description",
    "Original_Description_Fields",
    "Detected_Language",
    "Language_Confidence",
    "Translated_Description",
    "Translation_Method",
    "Translation_Coverage",
    "Unresolved_Tokens",
    "Evidence_Sources",
    "Evidence_Field_Count",
    "Match_Tier",
    "Match_Score",
    "Matched_Source_Systems",
    "Confidence_Factors",
    # --- business keys, carried through for the downstream agents -----------
    "Source_System",
    "Row_Type",
    "Document_Number",
    "Document_Line_Number",
    "PO_Number",
    "PO_Line_Number",
    "Invoice_Number",
    "Item_Number",
    "Item_Type",
    "Supplier_Id",
    "Supplier_Name",
    "Category_L1",
    "Category_L2",
    "Category_L3",
    "Category_L4",
    "Material_Group_Number",
    "Material_Group_Name",
    "Business_Area",
    "Division",
    "Company_Code",
    "Company_Name",
    "Country",
    "Quantity",
    "Unit",
    "Unit_Price",
    "Spend_EUR",
    "Currency",
    "Posting_Date",
    # --- provenance ---------------------------------------------------------
    "Is_Duplicate",
    "Duplicate_Of",
    "Source_File",
    "Source_Sheet",
    "Source_Row_Number",
    "Row_Id",
    "Run_Id",
    "Lexicon_Version",
    "Agent_Version",
)

# Appended to each per-source file by default. The deliverable is the
# description itself, so the default view stays narrow enough to drop into the
# client's own spreadsheet without burying their columns. The full audit trail
# is always present in the unified table, so nothing is lost by this choice;
# --full-columns switches the per-source files to the complete set.
PER_SOURCE_COLUMNS: Tuple[str, ...] = (
    "Enriched_Purchase_Description",
    "Enriched_Description_Short",
    "Item_Or_Service",
    "AI_Confidence",
    "Detected_Language",
    "Row_Id",
)

# Confidence band boundaries. Exposed as constants because Agents 3 and 4 apply
# the same thresholds to their own scores, and the bands must agree across the
# suite for the Power BI filters to behave consistently.
CONFIDENCE_HIGH = 75
CONFIDENCE_MEDIUM = 50


# ===========================================================================
# Configuration
# ===========================================================================

# List price for the default model, in dollars per million tokens. Both figures
# are overridable from the environment because prices are revised from time to
# time and the shared service does not have to quote the same rate as the public
# API. They are used only to estimate spend during a run; the invoice is the
# authority.
INPUT_COST_PER_MTOK = 1.25
OUTPUT_COST_PER_MTOK = 10.00

# Default alert threshold offered at the prompt, in dollars.
DEFAULT_SPEND_LIMIT = 25.00


@dataclass
class ModelConfig:
    """Resolved language-model connection details.

    Populated from the environment by :func:`resolve_model_config`. ``enabled``
    is false whenever the model tier is switched off or no usable credential was
    found, and every call site checks it rather than probing for a key.
    """

    enabled: bool = False
    backend: str = "openai"          # "openai" or "azure"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = DEFAULT_OPENAI_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    batch_size: int = 25
    timeout: int = 120
    max_requests: int = 0            # 0 means no cap
    spend_limit: float = 0.0         # dollars; 0 means no alert
    input_cost_per_mtok: float = INPUT_COST_PER_MTOK
    output_cost_per_mtok: float = OUTPUT_COST_PER_MTOK

    @property
    def endpoint(self) -> str:
        """Full chat-completions URL.

        The shared service is usually configured with the complete endpoint
        while the public API is configured with the ``/v1`` root, so both spellings
        are accepted and normalised here rather than at each call site.
        """
        url = self.base_url.rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        return f"{url}/chat/completions"


@dataclass
class Settings:
    """Everything the pipeline needs to run, resolved from CLI and prompts."""

    source_dir: Path
    results_dir: Path
    lexicon_path: Path
    cache_dir: Path

    use_neural_translation: bool = True
    use_semantic_matching: bool = True
    use_llm: bool = False

    fuzzy_threshold: float = 0.86
    semantic_threshold: float = 0.72
    top_k: int = 5
    max_words: int = 40
    max_short_words: int = 12
    semantic_phrase_cap: int = 200_000

    full_columns: bool = False
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
    trivial and the agents are expected to run on machines where installing an
    extra dependency needs an approval. Later assignments win, matching the
    behaviour of the shell and of python-dotenv, which matters because the
    working ``.env`` on this project defines one key twice.
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
        # Strip one matched pair of quotes; anything else is part of the value.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _env_flag(value: Optional[str], default: bool = False) -> bool:
    """Interpret the usual spellings of a boolean environment variable."""
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
    """Select and validate the language-model backend.

    ``AZURE_ENABLE`` chooses between the PwC GenAI shared service and the public
    OpenAI API. The two credential sets are read from separate variables so both
    can be configured at once. The direct-OpenAI branch deliberately refuses to
    inherit ``BASE_URL``: that variable points at the shared service on this
    project, and inheriting it would transmit a personal OpenAI key to an
    internal endpoint.
    """
    config = ModelConfig(enabled=use_llm)
    config.batch_size = max(1, _env_int(env.get("LLM_BATCH_SIZE"), 25))
    config.timeout = max(5, _env_int(env.get("LLM_TIMEOUT"), 120))
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
        config.api_key = (
            env.get("AZURE_OPENAI_API_KEY")
            or env.get("AZURE_API_KEY")
            or env.get("OPENAI_API_KEY")
            or ""
        )
        config.base_url = (
            env.get("AZURE_OPENAI_BASE_URL")
            or env.get("AZURE_BASE_URL")
            or env.get("BASE_URL")
            or "https://genai-sharedservice-emea.pwcinternal.com/v1/chat/completions"
        )
        config.model = (
            env.get("AZURE_OPENAI_MODEL")
            or env.get("MODEL_NAME")
            or DEFAULT_AZURE_MODEL
        )
        config.reasoning_effort = (
            env.get("LLM_REASONING_EFFORT") or DEFAULT_REASONING_EFFORT).strip().lower()
    else:
        config.backend = "openai"
        config.api_key = env.get("OPENAI_API_KEY", "")
        config.base_url = env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        config.model = env.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
        config.reasoning_effort = (
            env.get("LLM_REASONING_EFFORT") or DEFAULT_REASONING_EFFORT).strip().lower()

    if config.enabled and not config.api_key:
        LOGGER.warning(
            "Language-model tier requested but no API key was found for the %s "
            "backend; continuing without it.", config.backend,
        )
        config.enabled = False
    return config


# ===========================================================================
# Text utilities
# ===========================================================================
#
# Procurement free-text is among the dirtiest text in an enterprise. It carries
# the residue of every system it passed through: Windows line endings encoded as
# literal XML escapes, characters mangled by a round trip through the wrong code
# page, order numbers welded onto the front of a word, and the occasional pasted
# e-mail signature. Everything below exists because it was observed in the data.

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_XML_ESCAPES = re.compile(r"_x00[0-9A-Fa-f]{2}_")

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.IGNORECASE)
_DATE_LIKE = re.compile(r"\b\d{1,4}[./-]\d{1,2}([./-]\d{2,4})?\.?\b")
_LONG_DIGITS = re.compile(r"\b\d{4,}\b")
_ALPHANUM_CODE = re.compile(r"\b(?=\S*\d)(?=\S*[A-Za-z])[A-Za-z0-9][A-Za-z0-9._/-]{3,}\b")
_LEADING_CODE = re.compile(r"^\s*[\dA-Z]{3,}[-_ ]?(?=[a-zA-ZäöåÄÖÅøæØÆłąćęńóśźż])")
_MONEY = re.compile(r"\b\d+[.,]\d{2}\s*(eur|sek|pln|nok|dkk|usd|€|\$)\b", re.IGNORECASE)
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
    """Undo the common double-encoding damage seen in exported spreadsheets.

    Applied before anything else, because a mangled character defeats language
    identification, lexicon lookup and fuzzy matching simultaneously.
    """
    if not text or not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text

    try:
        repaired = text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
        # Only accept the round trip if it did not introduce replacement noise.
        if "\ufffd" not in repaired:
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    for damaged, correct in _MOJIBAKE_LITERAL.items():
        text = text.replace(damaged, correct)
    return text


def normalise_text(value: Any) -> str:
    """Reduce any cell value to clean, single-spaced text.

    Excel stores hard line breaks inside a cell as the literal token ``_x000D_``
    when the sheet is written by certain exporters, which is why that is stripped
    here rather than treated as data.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        # Whole floats are almost always integer keys that pandas or openpyxl
        # widened; rendering 1.0 as "1" keeps join keys comparable across files.
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
    """Strip diacritics for lookup purposes only.

    The lexicon is keyed on the folded form so that a single entry covers text
    that arrives with correct diacritics, without them, or with them mangled.
    Folded text is never written to output.
    """
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # Characters that do not decompose have to be mapped explicitly.
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
    text = normalise_text(value).upper()
    return re.sub(r"[^A-Z0-9]", "", text)


def tokenise(text: str) -> List[str]:
    """Split into word tokens, keeping digits attached to their word."""
    return _TOKEN.findall(text)


# Units that sit next to a number and describe the purchase (wattage, size,
# quantity). Kept in the enriched description; invoice-style identifiers are not.
_MEASUREMENT_UNITS = frozenset({
    "kw", "kwh", "mw", "gw", "w", "v", "kv", "a", "ma", "amp", "amps",
    "mm", "cm", "m", "km", "kg", "g", "t", "ton", "tons",
    "bar", "pa", "kpa", "mpa", "dn", "pn", "np", "nb",
    "pcs", "pc", "kpl", "stk", "st", "l", "ml", "h", "hz", "rpm",
})
_MEASUREMENT_TOKEN = re.compile(
    r"^(\d+[.,]?\d*)(" + "|".join(sorted(_MEASUREMENT_UNITS, key=len, reverse=True)) + r")$",
    re.IGNORECASE,
)
_DN_PN_TOKEN = re.compile(r"^(dn|pn)\d+[a-z]?$", re.IGNORECASE)


def is_measurement_token(token: str) -> bool:
    """True for a quantity or rating that belongs in the purchase description."""
    if not token:
        return False
    key = lookup_key(token)
    if key in _MEASUREMENT_UNITS:
        return True
    if token.isdigit() and 1 <= len(token) <= 4:
        return True
    if _MEASUREMENT_TOKEN.match(key) or _DN_PN_TOKEN.match(key):
        return True
    return False


def is_code_token(token: str) -> bool:
    """True for opaque identifiers, not for words or purchase specifications.

    Long digit runs and document-style hashes are dropped from the composed
    description. Wattage, quantity, DN/PN size and short item numbers are kept:
    Fortum asked that numbers which describe the purchase survive enrichment.
    """
    if not token:
        return True
    if is_measurement_token(token):
        return False
    if token.isdigit():
        return len(token) >= 6
    if len(token) <= 1:
        return True
    # Opaque document or XML identifiers, not catalogue item numbers.
    if len(token) >= 16 and any(ch.isdigit() for ch in token):
        return True
    return False


# Words that say what the number after them identifies. Fortum asked that an
# item number survive enrichment; a bare long digit run is still an invoice or
# document reference, so the number is kept only where the text labels it.
_ITEM_NUMBER_MARKERS = frozenset({
    "item", "items", "itemnum", "itemno", "itemnumber", "article", "articleno",
    "art", "code", "part", "partno", "pos", "position", "ref", "nro", "nr",
    "no", "nimike", "tuote", "artikel", "artikelnr", "pozycja", "numer",
})


def keep_purchase_tokens(tokens: Sequence[str]) -> List[str]:
    """Drop opaque codes, keeping numbers that describe the purchase.

    Wattage, sizes and short quantities are kept by ``is_measurement_token``.
    A longer number is kept when a preceding word names it, as in "item 970094".
    """
    kept: List[str] = []
    for index, token in enumerate(tokens):
        if is_measurement_token(token) or not is_code_token(token):
            kept.append(token)
            continue
        previous = lookup_key(tokens[index - 1]) if index else ""
        if token.isdigit() and previous in _ITEM_NUMBER_MARKERS:
            kept.append(token)
    return kept


# Nordic/Polish/German letters that never belong in the enriched columns.
_FOREIGN_LETTERS = re.compile(r"[äöåÄÖÅąćęłńóśźżĄĆĘŁŃÓŚŹŻõüÜßøæØÆ]")

# Long morphological endings. Short ones such as "ning" are omitted on purpose:
# they would flag English "cleaning" and "training".
_FOREIGN_ENDINGS = (
    "ukset", "uksen", "uksia", "uksella", "ukseen", "uksineen",
    "minen", "mista", "mistä", "ainen", "oinen", "ellinen",
    "työ", "työt", "tyota", "työtä",
    "lle", "ssa", "ssä", "sta", "stä", "lla", "llä",
    "ointi", "ointi", "tusten", "piston", "puistoon", "pumpun",
    "sailion", "kattilan", "paatteiden",
    "ningar", "heter", "elser", "ande", "ende", "arna", "orna",
    "arbete", "anlaggning", "besiktning", "inventering",
    "anie", "enia", "owych", "owie", "osci", "ości",
    "owy", "owa", "owe", "ami", "ach", "owi",
    "ungen", "keiten", "schaft",
)

_POLISH_SHAPE = re.compile(
    r"(cz|sz|rz|dz|prz|sci|ych|owi|ami|ach|enie|anie)", re.IGNORECASE)
_SWEDISH_SHAPE = re.compile(
    r"(hj|sj|skj|lj|ggning|hammande|hjalm|avslut)", re.IGNORECASE)
_FINNISH_SHAPE = re.compile(
    r"(kk|pp|tt|aa|ee|ii|uu|yy)", re.IGNORECASE)
_ENGLISH_SUFFIX = (
    "tion", "sion", "ment", "ness", "ity", "ies", "ing", "ed", "ly",
    "ous", "ful", "less", "able", "ible", "ence", "ance", "est",
    "ive", "ize", "ise", "ers",
)
_FOREIGN_FUNCTION = frozenset("""
och ja tai sekae sekä enligt med till oraz dla
""".split())

# English words that must never be stripped, even if an ending overlaps.
_ENGLISH_KEEP = frozenset("""
a an the and or of for to in on at by with from as
service services maintenance repair repairs work works
inspection inspections cleaning removal installation replacement
pump pumps motor motors cable cables tank tanks boiler plant site
wind bat survey overall visor helmet safety flame retardant fire
frequency converter meter meters flow transmitter module modules
calibration annual periodic industrial environmental impact
assessment termination underground delivery freight transport
confirmed plant engineer district heating digital input output
coverall protective mechanical seal seals gasket gaskets impeller
centrifugal variable speed drive power according quote offer
attached number weeks stock warehouse items parts spare
english polish finnish swedish german
""".split())

_FOREIGN_TERMS = frozenset("""
kuljetukset kuljetus kuljetuspalvelu huolto huoltotyo huoltotyö kunnossapito
vuokra vuokraus vuokran purku purkutyo purkutyö asbestipurku palvelu palvelut
palvelua korjaus asennus siivous konsultointi koulutus tarkastus hankinta
varaosa varaosat sopimus lasku tilaus työ tyot työt laite laitteet urakka
mittaus kaytto käyttö toimituskulut toimitus toimitusaika varastoon
lepakkoselvitys lepakkokartoitus tuulipuistoon tuulipuisto taajuusmuuttaja
vaihto vanhan tilalle mekaaninen tiiviste pumpulle polttimen liekinkestava
suojahaalari tarjous liitteena tarjousnumero viikkoa keskipakopumpun
polttoainesailion kaapelipaatteiden ymparistovaikutusten arviointi
suojakypara tyokypara visiirilla palosuojattu haalari
arbete underhall underhåll reparation tjanst tjänst tjanster tjänster
hyra uthyrning avtal faktura bestallning beställning brandhammande
skyddshjalm arbetshjalm kabelavslutningsarbete pannanlaggning periodisk
utbytesmodul digitala enligt offert leverans veckor
inventering fladdermus vindpark miljoteknisk utredning flodesgivare
flödesgivare genomgang genomgång justering
usluga uslugi usługa usługi usuwanie azbestu konserwacja naprawa
uszczelka mechaniczna pompy srodowiskowa środowiskowa srodowisko
nietoperzy wiatrowa przeplywomierz zgodnie oferta oddzialywania
falownik urzadzen wynajem zamowienie zamówienie instalacja
dienstleistung reparatur wartung
""".split())


def is_foreign_common_noun(token: str) -> bool:
    """True when a token is a Finnish/Swedish/Polish/German common noun.

    ASCII Nordic and Polish words were previously accepted as English because
    they carry no umlaut. That is how ``Lepakkoselvitys`` and ``Brandhammande``
    reached the published columns.
    """
    if not token:
        return False
    key = lookup_key(token)
    if not key or any(ch.isdigit() for ch in key):
        return False
    if key in _ENGLISH_KEEP:
        return False
    if key in _FOREIGN_FUNCTION:
        return True
    if key in _FOREIGN_TERMS:
        return True
    if _FOREIGN_LETTERS.search(token) and len(key) > 2:
        return True
    if any(key.endswith(ending) and len(key) > len(ending) + 1
           for ending in _FOREIGN_ENDINGS):
        return True
    if len(key) >= 6 and _POLISH_SHAPE.search(key):
        return True
    if len(key) >= 8 and _SWEDISH_SHAPE.search(key):
        return True
    if (len(key) >= 8 and _FINNISH_SHAPE.search(key)
            and not any(key.endswith(suffix) for suffix in _ENGLISH_SUFFIX)):
        return True
    return False


def foreign_tokens_in(text: str) -> List[str]:
    """Common nouns in ``text`` that are not English."""
    return [token for token in tokenise(text) if is_foreign_common_noun(token)]


def has_non_english(text: str) -> bool:
    """True when a published description still carries a source-language word."""
    return bool(foreign_tokens_in(text))


def drop_foreign_common_nouns(text: str) -> str:
    """Strip leftover foreign common nouns so an enriched column stays English."""
    kept = [token for token in tokenise(text) if not is_foreign_common_noun(token)]
    return sentence_case(" ".join(kept)) if kept else ""


def keep_published_english(text: str) -> str:
    """Keep punctuation when the string is already English.

    ``drop_foreign_common_nouns`` tokenises, which would turn a polished
    sentence back into a noun phrase.
    """
    text = normalise_text(text)
    if not text:
        return ""
    if has_non_english(text):
        return drop_foreign_common_nouns(text)
    return text


def tidy_published_english(text: str) -> str:
    """Drop truncated leftovers and repeated words from a published description."""
    words: List[str] = []
    seen: Set[str] = set()
    for token in tokenise(text):
        key = lookup_key(token)
        if is_foreign_common_noun(token):
            continue
        if (2 <= len(token) <= 3 and key not in _ENGLISH_KEEP
                and not is_measurement_token(token) and not token.isdigit()
                and not token.isupper()):
            continue
        if key in seen and key not in {"and", "or", "of", "for", "to", "with"}:
            continue
        seen.add(key)
        words.append(token)
    return sentence_case(" ".join(words)) if words else ""


# Maximo's buyer-to-buyer scratchpad. Fortum forbade using it as a description
# source: it carries lead times, stock instructions and "confirmed with site
# manager", none of which is what was purchased.
_INTERNAL_NOTE_HEADER = re.compile(
    r"xpointernalnote|pointernalnote|internal.?note", re.IGNORECASE)

_NON_PURCHASE = re.compile(
    r"("
    r"confirmed with(?: the)? (?:site manager|plant engineer)(?: on [\d./-]+)?"
    r"|quote attached|offer attached|see attachment|see enclosure"
    r"|according to (?:the )?(?:quote|offer|offert|contract)"
    r"|enligt offert(?:\s+\d+)?(?:\s*,?\s*leverans(?:\s+\d+)?(?:\s+veckor)?)?"
    r"|tarjous liitteena(?:\s*,?\s*tarjousnumero\s+\d+)?"
    r"|zgodnie z oferta(?:\s+\d+)?"
    r"|zgodnie oferta(?:\s+\d+)?"
    r"|\d+\s*kpl\s+varastoon"
    r"|varastoon(?:\s+toimitusaika(?:\s+\d+)?(?:\s+viikkoa)?)?"
    r"|toimitusaika(?:\s+\d+)?(?:\s+viikkoa)?"
    r"|delivery time(?:\s+\d+)?(?:\s+weeks)?"
    r"|lead time"
    r"|leverans(?:\s+\d+)?(?:\s+veckor)?"
    r")",
    re.IGNORECASE,
)


def is_internal_note_header(header: str) -> bool:
    """True for Maximo/Basware internal-note columns that must not be read."""
    return bool(_INTERNAL_NOTE_HEADER.search(normalise_column(header)))


def is_non_purchase_text(text: str) -> bool:
    """True when the text is a note or instruction, not an item or a service.

    "Confirmed with site manager" is the example Fortum gave: fluent, and still
    not a purchase.
    """
    if not text:
        return True
    remainder = strip_non_purchase_text(text)
    if not remainder:
        return True
    return bool(_NON_PURCHASE.search(text)) and not remainder


def strip_non_purchase_text(text: str) -> str:
    """Remove buyer notes, quote references and stock instructions.

    What remains, if anything, is the actual purchase wording.
    """
    if not text:
        return ""
    cleaned = _NON_PURCHASE.sub(" ", text)
    cleaned = re.sub(
        r"\b(tarjousnumero|offert|oferta|quote number|offer number)\b(?:\s+\d+)?",
        " ", cleaned, flags=re.I)
    return _WHITESPACE.sub(" ", cleaned).strip(" -,;:|")


# A reference number welded onto the front of a word, or a long number welded
# onto the back of one. Both are routine in ERP free-text and both hide the only
# meaningful word in the field from every downstream stage.
_WELDED_PREFIX = re.compile(r"(?<=\d)(?=[^\W\d_]{4,})", re.UNICODE)
_WELDED_SUFFIX = re.compile(r"(?<=[^\W\d_])(?=\d{4,})", re.UNICODE)


def separate_welded_codes(text: str) -> str:
    """Insert a break between a run of digits and an adjacent word.

    The specification's own worked example is ``157238asbestipurku`` becoming
    "Asbestos removal", which is only reachable if the reference number is
    separated from the word first. Both patterns require at least four
    characters on the numeric side so that genuine part designations such as
    ``WH-CH520`` and ``MODEL 3`` are left intact.
    """
    text = _WELDED_PREFIX.sub(" ", text)
    return _WELDED_SUFFIX.sub(" ", text)


def soften_caps(text: str) -> str:
    """Lower-case a fragment that was written entirely in capitals.

    Several of the source fields are shouted: invoice ``article_name`` arrives
    as ``VUOKRA`` and Maximo line descriptions are frequently all upper case.
    Left alone, every one of those words is indistinguishable from a part number
    to the code detector below, and a genuine Finnish word gets discarded as an
    identifier. Mixed-case text is untouched, so an acronym sitting inside a
    normal sentence still survives.
    """
    letters = [char for char in text if char.isalpha()]
    if len(letters) < 2:
        return text
    upper_share = sum(1 for char in letters if char.isupper()) / len(letters)
    return text.lower() if upper_share > 0.85 else text


def sentence_case(text: str) -> str:
    """Capitalise the first letter and leave the rest alone.

    ``str.capitalize`` would lower-case the remainder and destroy acronyms such
    as ``PPE`` and ``UNSPSC`` that legitimately appear inside a description.
    """
    text = text.strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def parse_amount(value: Any) -> Optional[float]:
    """Parse a monetary or quantity cell written in any European convention.

    Handles ``1 234,56``, ``1,234.56``, ``1.234,56``, parentheses for negatives
    and a trailing or leading currency symbol. Returns None rather than zero for
    unparseable input, because zero is a legitimate value and conflating the two
    would corrupt the amount-agreement check used during matching.
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
        # Whichever separator appears last is the decimal separator.
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        text = text.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif has_comma:
        # A single comma with exactly three trailing digits is a thousands
        # separator; anything else is a decimal comma.
        head, _, tail = text.rpartition(",")
        text = head + tail if (len(tail) == 3 and head) else text.replace(",", ".")

    try:
        amount = float(text)
    except ValueError:
        return None
    return -amount if negative else amount


def parse_date(value: Any) -> str:
    """Normalise a date cell to ISO ``YYYY-MM-DD``, or return it unchanged."""
    text = normalise_text(value)
    if not text:
        return ""

    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return iso.group(0)

    european = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
    if european:
        day, month, year = european.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return text


def sha256_file(path: Path) -> str:
    """Content hash of an input file, read in blocks so size does not matter."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_hash(*parts: str) -> str:
    """Short, stable identifier derived from content.

    Used for row identifiers so that the same input row carries the same
    identifier in every run, which is what lets a downstream agent or a Power BI
    model join to a previous export.
    """
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8"))
    return digest.hexdigest()[:16]


def text_similarity(left: str, right: str) -> float:
    """Normalised similarity in ``[0, 1]`` between two short strings."""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if _rapidfuzz is not None:
        return float(_rapidfuzz.token_set_ratio(left, right)) / 100.0

    # Standard-library fallback. Slower, and slightly less forgiving of word
    # order, but it keeps the agent working without third-party packages.
    from difflib import SequenceMatcher
    left_tokens, right_tokens = set(tokenise(left)), set(tokenise(right))
    if left_tokens and right_tokens:
        overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    else:
        overlap = 0.0
    return max(overlap, SequenceMatcher(None, left, right).ratio())


def amounts_agree(left: Optional[float], right: Optional[float], tolerance: float = 0.02) -> bool:
    """Whether two amounts are close enough to describe the same transaction.

    A relative tolerance absorbs rounding and currency conversion; the absolute
    floor stops the relative test from being meaninglessly strict near zero.
    """
    if left is None or right is None:
        return False
    if abs(left - right) <= 0.01:
        return True
    scale = max(abs(left), abs(right))
    return scale > 0 and abs(left - right) / scale <= tolerance


# ===========================================================================
# Tabular input
# ===========================================================================
#
# Two properties drive this section. First, the same logical extract may arrive
# as .xlsx or .csv, so the reader must not decide anything based on the file
# extension beyond how to parse it. Second, the production data set is around a
# million lines, so nothing may assume the file fits in memory. Every table
# therefore exposes a factory that yields a *fresh* iterator on demand, and the
# pipeline makes two streaming passes over the input instead of one buffered
# pass.

@dataclass
class Table:
    """A single sheet or delimited file, streamed on demand."""

    path: Path
    sheet: str
    headers: List[str]
    open_rows: Callable[[], Iterator[Tuple[int, List[str]]]]

    def iter_rows(self) -> Iterator[Tuple[int, List[str]]]:
        """Yield ``(source_row_number, values)`` with values aligned to headers."""
        for row_number, values in self.open_rows():
            if len(values) < len(self.headers):
                values = values + [""] * (len(self.headers) - len(values))
            elif len(values) > len(self.headers):
                values = values[: len(self.headers)]
            yield row_number, values

    @property
    def label(self) -> str:
        """Human-readable identity used in logs and output file names."""
        return f"{self.path.name}" if self.sheet in {"", "Sheet1"} else f"{self.path.name}:{self.sheet}"


# --- Delimited files -------------------------------------------------------

_ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "cp1252", "cp1250", "latin-1")


def _detect_encoding(path: Path) -> str:
    """Pick the first encoding that decodes a sample without error.

    The supplied catalogue is Central European and the PO extracts are Nordic,
    so both cp1250 and cp1252 have to be candidates. ``latin-1`` is last because
    it decodes any byte sequence and would mask a better answer.
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
        reader = csv.reader(handle, delimiter=delimiter)
        try:
            header_row = next(reader)
        except StopIteration:
            return []
    headers = [normalise_text(cell) for cell in header_row]
    if not any(headers):
        return []

    def open_rows() -> Iterator[Tuple[int, List[str]]]:
        with path.open("r", encoding=encoding, errors="replace", newline="") as stream:
            rows = csv.reader(stream, delimiter=delimiter)
            next(rows, None)  # discard the header
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
            worksheet = probe[sheet_name]
            header_values: List[str] = []
            for row in worksheet.iter_rows(min_row=1, max_row=1, values_only=True):
                header_values = [normalise_text(cell) for cell in row]
                break
        finally:
            probe.close()

        if not any(header_values):
            continue

        def open_rows(sheet: str = sheet_name) -> Iterator[Tuple[int, List[str]]]:
            book = _openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                target = book[sheet]
                for index, row in enumerate(target.iter_rows(min_row=2, values_only=True), start=2):
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
    return [
        "".join(node.text or "" for node in item.iter(_SPREADSHEET_NS + "t"))
        for item in root.iter(_SPREADSHEET_NS + "si")
    ]


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

    Uses ``iterparse`` and clears each element after use so that memory stays
    flat across a very large sheet. Sparse rows are densified against the cell
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
                except (ValueError, IndexError):
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

    A fallback for environments where openpyxl cannot be installed. An .xlsx
    file is a zip archive of XML parts, so this is entirely supportable without
    a dependency, and having it means a missing package downgrades performance
    rather than stopping the run.
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
        sheets = [
            (node.attrib.get("name", "Sheet"), node.attrib.get(_DOCUMENT_REL_NS + "id", ""))
            for node in workbook.iter(_SPREADSHEET_NS + "sheet")
        ]
        shared = _xlsx_shared_strings(archive)
        date_styles = _xlsx_date_styles(archive)

        for sheet_name, relationship_id in sheets:
            target = relationships.get(relationship_id, "")
            if not target:
                continue
            part = target[1:] if target.startswith("/") else f"xl/{target}"
            if part not in names:
                continue

            payload = archive.read(part)
            rows = _iter_xlsx_sheet(payload, shared, date_styles)
            headers: List[str] = []
            for candidate in rows:
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
        LOGGER.error("Could not read %s: %s", path.name, error)
        return []
    return []


def discover_input_files(root: Path) -> List[Path]:
    """Find every readable table beneath a folder.

    The client's folder layout is not fixed and subfolders are expected, so the
    walk is recursive. Excel lock files and hidden files are skipped, and the
    result is sorted so that run-to-run behaviour does not depend on the order
    the file system happens to return entries in.
    """
    if root.is_file():
        return [root]

    candidates: List[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith((".", "~$")):
            continue
        if path.suffix.lower() in {".csv", ".tsv", ".txt", ".xlsx", ".xlsm"}:
            candidates.append(path)
    return sorted(candidates)


# ===========================================================================
# Source profiling
# ===========================================================================
#
# The development plan is explicit that column names may change and that the
# script must not depend on an exact-name search. Profiling therefore works in
# two stages: a fingerprint identifies which system a file came from, and a
# resolver maps each logical field onto whichever column actually carries it.
# When no profile matches, a generic profile is inferred from the data itself.

def normalise_column(name: str) -> str:
    """Reduce a header to a comparison key, ignoring spacing and punctuation."""
    return re.sub(r"[^a-z0-9]", "", fold_accents(normalise_text(name)).lower())


# Logical field -> candidate header spellings, in preference order. Matching is
# performed on the normalised key, so "PO line desc", "po_line_desc" and
# "PO Line Description" all collapse onto the same candidate.
FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "document_number": ("documentnumber", "docnumber", "documentno", "invoicekey"),
    "document_line_number": ("documentlinenumber", "doclinenumber", "rownumber", "lineno"),
    "po_number": ("ponumber", "ordernumber", "ponum", "purchaseordernumber", "poid"),
    "po_line_number": ("polinenumber", "polinenum", "polinenumber", "polinenr"),
    "invoice_number": ("invoicenumber", "invoiceid", "invoiceno", "invoicekey"),
    "item_number": ("itemnum", "itemid", "articleid", "materialnumber",
                    "supplierproductcode", "itemcode", "itemnumber"),
    # Basware records how a line was raised. Agent 3 turns "External webshop"
    # and "Market place" into Standard_item = Yes, so the value has to survive
    # the unified table rather than being read from the extract a second time.
    "item_type": ("itemtype", "itemtypename", "lineitemtype", "baswareitemtype",
                  "purchasingtype", "requisitiontype"),
    "supplier_id": ("erpsuppliernumber", "suppliercode", "vendorid", "suppliernumber",
                    "vendornum", "supplierid"),
    "supplier_name": ("erpsuppliername", "suppliername", "supplier", "vendor",
                      "vendorname", "creditor"),
    "category_l1": ("categoryl1", "category1", "maincategory"),
    "category_l2": ("categoryl2", "category2", "subcategory"),
    "category_l3": ("categoryl3", "category3"),
    "category_l4": ("categoryl4", "category4"),
    "material_group_number": ("materialgroupnumber", "commoditygroup", "categorycode",
                              "unspsc", "materialgroup"),
    "material_group_name": ("materialgroupname", "commodity", "accountname",
                            "materialgroupdescription"),
    "business_area": ("businessarea", "ba", "businessunit"),
    "division": ("division", "divisionname"),
    "company_code": ("legalcompanynumber", "companycode", "buyercompany", "orgid", "organizationcode"),
    "company_name": ("legalcompanyname", "companyname", "organizationname"),
    "country": ("country", "suppliercountry", "organizationcountry", "purchasingcountry"),
    "quantity": ("quantity", "orderqty", "polinequantity", "quantitycharged",
                 "quantitydelivered", "qty"),
    "unit": ("unit", "uom", "unitofmeasure", "orderunit"),
    "unit_price": ("unitcost", "unitprice", "unitpriceexclvat", "unitpricenet", "price"),
    "spend": ("spendineur", "linecost", "ponetsumcompany", "rowtotalexclvat",
              "loadedcost", "spend", "netamount", "amount"),
    "currency": ("purchasecurrency", "currencycode", "pocurrencycompany", "currency"),
    "posting_date": ("postingdate", "orderdate", "invoicedate", "pocreationdate", "documentdate"),
}


@dataclass
class SourceProfile:
    """How to interpret the columns of one recognised source system."""

    name: str
    fingerprint: Tuple[str, ...]
    description_fields: Tuple[str, ...]
    context_fields: Tuple[str, ...] = ()
    code_fields: Tuple[str, ...] = ()

    def match_score(self, headers: Sequence[str]) -> float:
        """Share of the fingerprint columns present in this header row."""
        if not self.fingerprint:
            return 0.0
        present = {normalise_column(header) for header in headers}
        hits = sum(1 for column in self.fingerprint if column in present)
        return hits / len(self.fingerprint)


# Ordered by specificity: the invoice profile has to be tested before the more
# permissive ones, because its fingerprint is small.
KNOWN_PROFILES: Tuple[SourceProfile, ...] = (
    SourceProfile(
        name="master",
        fingerprint=("maxrowid", "interpreteddescription", "pomatchlevel",
                     "invoicematchlevel", "textinterpreted"),
        description_fields=(
            "Document line desc", "PO line desc",
            "PO_Line_Description", "Maximo_LINE_DESCRIPTION", "Maximo_DESCRIPTION",
            "Basware_Supplier product name",
        ),
        context_fields=(
            "MaterialGroupName", "Category L4", "Category L3", "Category L2",
            "Category L1", "PO_Category_Sub", "PO_Category_Main",
            "Invoice_Article_Name",
        ),
        code_fields=("PO_Item_Code", "Maximo_ITEMNUM", "MaterialGroupNumber"),
    ),
    SourceProfile(
        name="sievo",
        fingerprint=("sourcerowid", "datasource", "documentlinedesc", "categoryl1",
                     "materialgroupnumber", "spendineur"),
        description_fields=("Document line desc", "PO line desc"),
        # A supplier name is who was paid, never what was bought, so it is not
        # offered as evidence for the description.
        context_fields=("MaterialGroupName", "Category L4", "Category L3",
                        "Category L2", "Category L1"),
        code_fields=("MaterialGroupNumber", "GLAccountNumber"),
    ),
    SourceProfile(
        name="maximo",
        fingerprint=("ponum", "linedescription", "itemnum", "commoditygroup",
                     "commodity", "orderqty"),
        description_fields=("LINE_DESCRIPTION", "DESCRIPTION"),
        context_fields=("COMMODITY", "COMMODITYGROUP"),
        code_fields=("ITEMNUM", "COMMODITYGROUP", "CONTRACTREFNUM"),
    ),
    SourceProfile(
        name="basware",
        fingerprint=("ordernumber", "supplierproductname", "supplierproductcode",
                     "unspsc", "maincategory", "subcategory"),
        description_fields=("Supplier product name", "PO line text1", "PO line text2",
                            "PO line text3", "PO line text4", "PO line text5"),
        # "Item type" is deliberately absent: it says how the line was raised,
        # not what was bought, and it now travels as its own column instead.
        context_fields=("Sub category", "Main category", "Account name",
                        "Project name"),
        code_fields=("Supplier product code", "Item ID", "UNSPSC", "Category code"),
    ),
    SourceProfile(
        name="invoice",
        fingerprint=("xmlfilename", "invoicekey", "articlename", "freetext",
                     "rowtotalexclvat"),
        description_fields=("article_name",),
        context_fields=("article_id",),
        code_fields=("article_id",),
    ),
    SourceProfile(
        name="catalogue",
        fingerprint=("supplier", "itemname", "itemcode", "itemdescription", "unitprice"),
        description_fields=("Item_Name", "Item_Description"),
        context_fields=("Supplier",),
        code_fields=("Item_Code",),
    ),
)

# Header substrings that suggest free text, used only by the generic profile.
_DESCRIPTIVE_HINTS = ("desc", "description", "text", "name", "note", "comment",
                      "narrative", "article", "material", "service", "item")
# Header substrings that disqualify a column from being treated as free text
# even when it also matches a descriptive hint, e.g. "Supplier name".
_DESCRIPTIVE_BLOCKERS = ("supplier", "vendor", "company", "creditor", "buyer",
                         "creator", "owner", "agent", "requester", "person",
                         "country", "currency", "status", "file", "user",
                         "xpointernal", "internalnote", "itemtype")


def profile_table(table: Table, sample_rows: List[List[str]]) -> Tuple[SourceProfile, float]:
    """Identify which system a table came from, inferring a profile if unknown."""
    best_profile, best_score = None, 0.0
    for profile in KNOWN_PROFILES:
        score = profile.match_score(table.headers)
        if score > best_score:
            best_profile, best_score = profile, score

    # Half the fingerprint present is a comfortable margin: the profiles share
    # almost no fingerprint columns, so a genuine match scores far higher.
    if best_profile is not None and best_score >= 0.5:
        return best_profile, best_score
    return infer_profile(table, sample_rows), 0.0


def infer_profile(table: Table, sample_rows: List[List[str]]) -> SourceProfile:
    """Build a profile for an unrecognised table from its content.

    A column is treated as free text when its values look like language rather
    than like identifiers: several words on average, mostly alphabetic, and not
    drawn from a tiny set of repeated codes. Header wording is used as a tie
    breaker rather than as the primary signal, because header wording is exactly
    what cannot be relied upon.
    """
    scores: Dict[int, float] = {}
    for position, header in enumerate(table.headers):
        values = [row[position] for row in sample_rows
                  if position < len(row) and row[position]]
        if len(values) < 3:
            continue

        average_tokens = sum(len(tokenise(value)) for value in values) / len(values)
        alphabetic = sum(
            sum(1 for ch in value if ch.isalpha()) / max(1, len(value)) for value in values
        ) / len(values)
        distinct_ratio = len(set(values)) / len(values)

        score = 0.0
        score += min(average_tokens / 4.0, 1.0) * 0.45
        score += alphabetic * 0.30
        score += distinct_ratio * 0.25

        key = normalise_column(header)
        if is_internal_note_header(header):
            continue
        if any(hint in key for hint in _DESCRIPTIVE_HINTS):
            score += 0.25
        if any(blocker in key for blocker in _DESCRIPTIVE_BLOCKERS):
            score -= 0.45
        scores[position] = score

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    description_fields = tuple(table.headers[position] for position, score in ranked[:3] if score >= 0.5)
    context_fields = tuple(table.headers[position] for position, score in ranked[3:7] if score >= 0.35)

    return SourceProfile(
        name=f"generic:{table.path.stem.lower().replace(' ', '_')}",
        fingerprint=(),
        description_fields=description_fields,
        context_fields=context_fields,
    )


class ColumnResolver:
    """Maps logical field names onto the actual columns of one table.

    Resolution is by alias first and by fuzzy header comparison second. The
    fuzzy stage is what absorbs the renaming the development plan warns about:
    a column that arrives as ``PO_Line_Description`` rather than ``PO line desc``
    still resolves, without anybody editing this file.
    """

    def __init__(self, headers: Sequence[str]) -> None:
        self.headers = list(headers)
        self._by_key: Dict[str, int] = {}
        for position, header in enumerate(headers):
            key = normalise_column(header)
            # First occurrence wins; duplicated headers are common in exports
            # and the leftmost is invariably the populated one.
            if key and key not in self._by_key:
                self._by_key[key] = position
        self._resolved: Dict[str, Optional[int]] = {}

    def position_of(self, header_name: str) -> Optional[int]:
        """Index of a column addressed by its literal name."""
        return self._by_key.get(normalise_column(header_name))

    def resolve(self, logical_field: str) -> Optional[int]:
        """Index of the column carrying a logical field, or None."""
        if logical_field in self._resolved:
            return self._resolved[logical_field]

        position: Optional[int] = None
        for alias in FIELD_ALIASES.get(logical_field, ()):
            if alias in self._by_key:
                position = self._by_key[alias]
                break

        if position is None:
            position = self._fuzzy_resolve(logical_field)

        self._resolved[logical_field] = position
        return position

    def _fuzzy_resolve(self, logical_field: str) -> Optional[int]:
        """Last-resort header match, requiring a high similarity to accept."""
        aliases = FIELD_ALIASES.get(logical_field, ())
        if not aliases:
            return None

        best_position, best_score = None, 0.0
        for key, position in self._by_key.items():
            for alias in aliases:
                score = text_similarity(key, alias)
                if score > best_score:
                    best_position, best_score = position, score
        # 0.92 is deliberately strict. A wrong mapping here silently corrupts a
        # business key, which is far more damaging than leaving it unmapped.
        return best_position if best_score >= 0.92 else None

    def value(self, row: Sequence[str], logical_field: str) -> str:
        """Value of a logical field in one row, or an empty string."""
        position = self.resolve(logical_field)
        if position is None or position >= len(row):
            return ""
        return row[position]

    def value_at(self, row: Sequence[str], header_name: str) -> str:
        """Value of a literally named column in one row, or an empty string."""
        position = self.position_of(header_name)
        if position is None or position >= len(row):
            return ""
        return row[position]


# ===========================================================================
# Row typing
# ===========================================================================
#
# Invoice extracts interleave document headers, purchase lines, subtotals and
# totals in a single sheet. Describing a total row as if it were a purchase
# would distort every count and every spend figure downstream. No row is ever
# dropped: the ones that are not purchase lines are flagged and left unenriched,
# so the client's row count is preserved exactly.

ROW_TYPE_LINE = "LINE"
ROW_TYPE_HEADER = "HEADER"
ROW_TYPE_SUBTOTAL = "SUBTOTAL"
ROW_TYPE_TOTAL = "TOTAL"
ROW_TYPE_EMPTY = "EMPTY"

_TOTAL_MARKERS = re.compile(
    r"^\s*(total|sum|subtotal|grand\s*total|yhteens[aä]|summa|delsumma|razem|suma|"
    r"gesamt|netto|brutto|vat|alv|moms|podatek)\b", re.IGNORECASE)


def classify_row(resolver: ColumnResolver, row: Sequence[str], description: str) -> str:
    """Decide whether a row is a purchase line or document furniture."""
    if not any(cell.strip() for cell in row):
        return ROW_TYPE_EMPTY

    if _TOTAL_MARKERS.match(description):
        # A total that also carries a line number is a subtotal within a
        # document rather than the closing total of the whole document.
        line_number = resolver.value(row, "document_line_number") or resolver.value(row, "po_line_number")
        return ROW_TYPE_SUBTOTAL if line_number.strip() else ROW_TYPE_TOTAL

    has_line_number = bool((resolver.value(row, "document_line_number")
                            or resolver.value(row, "po_line_number")).strip())
    has_quantity = parse_amount(resolver.value(row, "quantity")) is not None
    has_amount = parse_amount(resolver.value(row, "spend")) is not None
    has_item = bool(resolver.value(row, "item_number").strip())

    if has_line_number or has_quantity or has_item:
        return ROW_TYPE_LINE
    if description and has_amount:
        return ROW_TYPE_LINE
    if description:
        return ROW_TYPE_LINE

    # A row with a document number and money but neither a line number nor any
    # text is the document header that the following lines belong to.
    if resolver.value(row, "document_number").strip() and has_amount:
        return ROW_TYPE_HEADER
    return ROW_TYPE_EMPTY


# ===========================================================================
# Controlled vocabulary
# ===========================================================================

class Lexicon:
    """The curated procurement vocabulary.

    Everything resolved here is resolved deterministically, offline and for
    free. It is the first tier of the translation cascade and the single most
    cost-effective place to invest effort: one entry improves every future run
    of every agent, whereas one language-model call improves exactly one phrase
    on exactly one run.
    """

    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        payload = payload or {}
        self.version: str = str(payload.get("version", "0.0.0"))

        # Phrase tables are stored per language and also merged across
        # languages. The merged view is what a lookup uses when language
        # identification was inconclusive, which is common for two-word
        # fragments where there is simply not enough signal.
        self.phrases: Dict[str, Dict[str, str]] = {}
        self.terms: Dict[str, Dict[str, str]] = {}
        self.compound_parts: Dict[str, Dict[str, str]] = {}

        for language, entries in (payload.get("phrases") or {}).items():
            self.phrases[language] = {lookup_key(k): v for k, v in entries.items()}
        for language, entries in (payload.get("terms") or {}).items():
            self.terms[language] = {lookup_key(k): v for k, v in entries.items()}
        for language, entries in (payload.get("compound_parts") or {}).items():
            self.compound_parts[language] = {lookup_key(k): v for k, v in entries.items()}

        self.any_phrase: Dict[str, str] = {}
        for entries in self.phrases.values():
            self.any_phrase.update(entries)
        self.any_term: Dict[str, str] = {}
        for entries in self.terms.values():
            self.any_term.update(entries)
        self.any_compound: Dict[str, str] = {}
        for entries in self.compound_parts.values():
            self.any_compound.update(entries)

        # Longest first, so "asbestin purkutyo" is preferred over "asbesti".
        self._phrase_order: List[str] = sorted(
            self.any_phrase, key=lambda phrase: (-len(phrase), phrase))

        self.service_markers: Set[str] = {lookup_key(t) for t in payload.get("service_markers", [])}
        self.material_markers: Set[str] = {lookup_key(t) for t in payload.get("material_markers", [])}
        self.noise_terms: Set[str] = {lookup_key(t) for t in payload.get("noise_terms", [])}
        self.unit_terms: Dict[str, str] = {lookup_key(k): v
                                           for k, v in (payload.get("unit_terms") or {}).items()}
        self.legal_forms: Set[str] = {lookup_key(t) for t in payload.get("legal_forms", [])}

        # Vocabulary keys double as a language-identification signal: a token
        # present only in the Finnish tables is strong evidence for Finnish.
        self._language_tokens: Dict[str, Set[str]] = {}
        for language in set(self.terms) | set(self.phrases):
            tokens: Set[str] = set(self.terms.get(language, {}))
            for phrase in self.phrases.get(language, {}):
                tokens.update(phrase.split())
            self._language_tokens[language] = tokens

    @classmethod
    def load(cls, path: Path) -> "Lexicon":
        """Load the vocabulary, degrading to an empty one when absent."""
        if not path.is_file():
            LOGGER.warning("Vocabulary file %s not found; running without it.", path)
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            LOGGER.error("Vocabulary file %s could not be read (%s).", path, error)
            return cls()
        LOGGER.info("Vocabulary %s loaded (version %s).", path.name, payload.get("version"))
        return cls(payload)

    # -- lookups ------------------------------------------------------------

    def phrase(self, text: str, language: Optional[str] = None) -> Optional[str]:
        """Exact phrase translation, preferring the identified language."""
        key = lookup_key(text)
        if language and key in self.phrases.get(language, {}):
            return self.phrases[language][key]
        return self.any_phrase.get(key)

    def term(self, token: str, language: Optional[str] = None) -> Optional[str]:
        """Single-token translation, preferring the identified language."""
        key = lookup_key(token)
        if language and key in self.terms.get(language, {}):
            return self.terms[language][key]
        return self.any_term.get(key)

    def substitute_phrases(self, text: str, language: Optional[str]) -> Tuple[str, int, int]:
        """Replace every known multi-word expression, longest match first.

        Matching happens on the folded, lower-cased form so that one entry
        covers every spelling variant. The folded form is only *returned* when a
        substitution actually fired: text the vocabulary had nothing to say
        about is handed back untouched, so that casing and diacritics survive
        for the tiers further down the cascade.

        Returns the rewritten text, the number of substitutions made, and the
        number of English tokens those substitutions produced. The last of these
        is needed by the coverage measure: the words a phrase substitution emits
        are already translated, and counting them as unresolved content would
        make a perfectly translated phrase look like a failure.
        """
        if not self._phrase_order:
            return text, 0, 0

        working = lookup_key(text)
        replacements = 0
        emitted_tokens = 0
        for phrase in self._phrase_order:
            if len(phrase) < 5 or phrase not in working:
                continue
            target = (self.phrases.get(language, {}).get(phrase)
                      if language else None) or self.any_phrase[phrase]
            # Word-boundary guarded so that "el" does not fire inside "eldrift".
            pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")
            working, count = pattern.subn(target, working)
            replacements += count
            emitted_tokens += count * len(tokenise(target))

        return (working, replacements, emitted_tokens) if replacements else (text, 0, 0)

    def split_compound(self, token: str, language: Optional[str]) -> Optional[List[str]]:
        """Decompose a Nordic compound into known parts.

        Finnish and Swedish form compounds by concatenation, so ``asbestipurku``
        never appears in a dictionary while both of its parts do. A greedy
        longest-prefix walk recovers the parts. Two-part splits are required to
        cover the whole token, which keeps the method from inventing readings of
        words it does not actually know.
        """
        parts_table = self.compound_parts.get(language or "", {}) or self.any_compound
        if not parts_table:
            return None

        key = lookup_key(token)
        if len(key) < 8:
            return None

        pieces: List[str] = []
        cursor = 0
        while cursor < len(key):
            match = ""
            for candidate in parts_table:
                if len(candidate) > len(match) and key.startswith(candidate, cursor):
                    match = candidate
            if not match:
                # Finnish compounds often insert a linking vowel; skipping one
                # character and retrying recovers those without a rule table.
                if pieces and cursor + 1 < len(key):
                    cursor += 1
                    continue
                return None
            pieces.append(parts_table[match])
            cursor += len(match)

        meaningful = [piece for piece in pieces if piece]
        return meaningful if len(meaningful) >= 2 else None

    def language_affinity(self, tokens: Sequence[str]) -> Dict[str, int]:
        """Count how many tokens are known to each language's vocabulary."""
        counts: Dict[str, int] = {}
        folded = [lookup_key(token) for token in tokens]
        for language, vocabulary in self._language_tokens.items():
            hits = sum(1 for token in folded if token in vocabulary)
            if hits:
                counts[language] = hits
        return counts

    def is_noise(self, text: str) -> bool:
        """Whether a value is a placeholder rather than a description."""
        key = lookup_key(text)
        if not key:
            return True
        if key in self.noise_terms:
            return True
        # A value with no letters at all is a code or a number, not a description.
        return not any(ch.isalpha() for ch in key)


# ===========================================================================
# Language identification
# ===========================================================================
#
# Identification runs on distinct phrases, not on rows, and only has to be good
# enough to route a phrase to the right translation resource. Getting it wrong
# costs a slightly worse translation, not a wrong answer, because the downstream
# stages all validate their own output.

_LANGUAGE_MARKERS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    # language: (characteristic character sequences, characteristic words)
    "fi": (("ä", "ö", "yy", "ii", "kk", "tt", "uu", "ää"),
           ("ja", "ta", "sekä", "sekae", "kpl", "oy", "tai", "sekä", "vuoden", "mukaan")),
    "sv": (("å", "ä", "ö", "ck", "sk", "tj"),
           ("och", "av", "för", "med", "till", "st", "ab", "enligt", "per")),
    "pl": (("ą", "ć", "ę", "ł", "ń", "ó", "ś", "ź", "ż", "cz", "sz", "rz", "prz"),
           ("i", "w", "na", "do", "dla", "oraz", "szt", "sp", "usluga", "z")),
    "de": (("ä", "ö", "ü", "ß", "sch", "ung"),
           ("und", "der", "die", "das", "für", "mit", "von", "gmbh")),
    "et": (("õ", "ä", "ö", "ü"), ("ja", "ning", "tk", "ou")),
    "no": (("ø", "å", "æ"), ("og", "av", "for", "med", "til", "as")),
    "da": (("ø", "å", "æ"), ("og", "af", "for", "med", "til", "aps")),
    "en": ((), ("the", "and", "of", "for", "with", "service", "repair", "parts", "per")),
}

# Words spelled identically in several languages are useless as evidence and are
# excluded so that they do not decide a close contest.
_AMBIGUOUS_MARKERS = {"i", "in", "av", "og", "and", "st", "per", "z", "w"}


def detect_language(text: str, lexicon: Optional[Lexicon] = None) -> Tuple[str, float]:
    """Identify the language of a phrase.

    Returns the language code and a confidence in ``[0, 1]``. Very short input
    is reported as ``und`` rather than guessed, because a two-word fragment
    genuinely does not carry enough signal and a confident wrong answer is worse
    than an honest abstention.
    """
    cleaned = normalise_text(text)
    if not cleaned or not any(ch.isalpha() for ch in cleaned):
        return "und", 0.0

    tokens = [token.lower() for token in tokenise(cleaned)]
    letters = [ch for ch in cleaned.lower() if ch.isalpha()]
    if not tokens or not letters:
        return "und", 0.0

    scores: Dict[str, float] = defaultdict(float)

    for language, (character_markers, word_markers) in _LANGUAGE_MARKERS.items():
        for marker in character_markers:
            hits = cleaned.lower().count(marker)
            if hits:
                scores[language] += min(hits, 3) * 0.6
        for marker in word_markers:
            if marker in _AMBIGUOUS_MARKERS:
                continue
            if marker in tokens:
                scores[language] += 1.4

    # Vocabulary membership is the strongest single signal available, because
    # the vocabulary is domain-specific and these are domain phrases.
    if lexicon is not None:
        for language, hits in lexicon.language_affinity(tokens).items():
            scores[language] += hits * 2.0

    # An all-ASCII phrase built from common English words is English. ASCII
    # Finnish and Polish have no umlauts, so the bonus is withheld when the
    # tokens themselves look like source-language nouns.
    if (all(ord(ch) < 128 for ch in cleaned)
            and not any(is_foreign_common_noun(token) for token in tokens)):
        scores["en"] += 0.8

    if not scores:
        return ("en", 0.35) if all(ord(ch) < 128 for ch in cleaned) else ("und", 0.0)

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    best_language, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    # Confidence is the margin over the runner-up, so a language that wins
    # narrowly is reported as uncertain even when its absolute score is high.
    confidence = min(1.0, (best_score - runner_up) / max(best_score, 1.0) + 0.25)

    # langdetect is statistical rather than rule-based and is a useful tie
    # breaker, but it is unreliable on fragments so it only votes on longer text.
    if _langdetect is not None and confidence < 0.55 and len(cleaned) >= 25:
        try:
            _langdetect.DetectorFactory.seed = 0  # required for deterministic output
            statistical = _langdetect.detect(cleaned)
            if statistical == best_language:
                confidence = min(1.0, confidence + 0.25)
            elif statistical in _LANGUAGE_MARKERS:
                best_language, confidence = statistical, 0.5
        except Exception:
            pass

    if len(tokens) <= 1 and confidence < 0.5:
        return "und", confidence
    return best_language, round(confidence, 3)


# ===========================================================================
# Offline neural translation
# ===========================================================================

class NeuralTranslator:
    """Local Helsinki-NLP opus-mt translation.

    This is the component that removes the largest language-model cost from the
    pipeline. The models are small, run on CPU, and translate a domain phrase at
    roughly the quality a general-purpose language model would, at zero
    marginal cost. Models are loaded lazily and only for the languages actually
    present in the data, so a run over purely English input loads nothing.
    """

    MODEL_TEMPLATE = "Helsinki-NLP/opus-mt-{source}-en"
    # Languages served by a dedicated bilingual model. Others fall through to
    # the multilingual model or, failing that, to the language-model tier.
    SUPPORTED = ("fi", "sv", "pl", "de", "da", "no", "nl", "et", "fr", "es", "it", "cs")

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and _transformers is not None
        self._pipelines: Dict[str, Any] = {}
        self._unavailable: Set[str] = set()
        self.translated_count = 0

    def available_for(self, language: str) -> bool:
        return self.enabled and language in self.SUPPORTED and language not in self._unavailable

    def _pipeline(self, language: str) -> Optional[Any]:
        """Fetch or build the pipeline for one source language."""
        if language in self._pipelines:
            return self._pipelines[language]
        if not self.available_for(language):
            return None

        model_name = self.MODEL_TEMPLATE.format(source=language)
        try:
            LOGGER.info("Loading offline translation model %s ...", model_name)
            pipeline = _transformers.pipeline(
                "translation", model=model_name, device=-1,  # CPU keeps this portable
            )
        except Exception as error:
            LOGGER.warning("Translation model %s unavailable (%s).", model_name, error)
            self._unavailable.add(language)
            self._pipelines[language] = None
            return None

        self._pipelines[language] = pipeline
        return pipeline

    def translate_batch(self, texts: Sequence[str], language: str) -> Dict[str, str]:
        """Translate a batch of phrases from one language into English.

        Returns a mapping keyed by the input text so that callers do not have to
        rely on positional alignment, which a failed item would break.
        """
        pipeline = self._pipeline(language)
        if pipeline is None or not texts:
            return {}

        results: Dict[str, str] = {}
        # Beam search with a fixed beam count and no sampling, so the same input
        # always produces the same output. This is a hard requirement, not a
        # preference: the whole agent is specified to be repeatable.
        for start in range(0, len(texts), 32):
            window = list(texts[start:start + 32])
            try:
                outputs = pipeline(window, max_length=256, num_beams=4, do_sample=False)
            except Exception as error:
                LOGGER.warning("Offline translation failed for a batch (%s).", error)
                continue
            for source_text, output in zip(window, outputs):
                translated = normalise_text(output.get("translation_text", ""))
                if translated:
                    results[source_text] = translated
                    self.translated_count += 1
        return results


# ===========================================================================
# Language model client
# ===========================================================================

@dataclass
class TokenUsage:
    """Running total of language-model consumption.

    Reasoning tokens are tracked separately because reasoning models bill them
    as output but never return them in the message, so reporting only the
    visible output tokens would understate the cost of a run, sometimes by a
    large factor. Cached input tokens are tracked because they are billed at a
    reduced rate and their share is the clearest evidence that the caching
    strategy is working.
    """

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
        discounts it heavily. The figure therefore leans high, which is the
        right direction for a number that exists to stop a run before it
        becomes expensive.
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
        """Accumulate one API response's usage block, however it is spelled."""
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
            "requests": self.requests,
            "failed_requests": self.failed_requests,
            "cache_hits": self.cache_hits,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
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

        Worth recording: a run whose model was switched off part way through is
        not comparable with one that had it throughout, and the manifest is
        where that difference has to be visible.
        """
        return {
            "spend_limit_usd": round(self.step, 4),
            "spend_limit_final_usd": round(self.limit, 4),
            "spend_limit_extensions": self.extensions,
            "spend_limit_stopped": self.declined,
        }

    def review(self) -> None:
        """Check the running total after a billed response and act on it.

        Called once per response rather than per batch so that a single
        expensive reply cannot carry the run far past the figure that was
        authorised.
        """
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
    """Minimal OpenAI-compatible chat client with disk caching.

    Deliberately small. The agent needs one endpoint, one message shape and a
    JSON reply; an SDK would add a dependency, a release cadence and a set of
    behaviours that have to be pinned, in exchange for nothing that is needed
    here. Both supported backends speak the same protocol, so the only
    difference between them is the URL and the deployment name.
    """

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
        self._reasoning_style = "effort"

    # -- cache --------------------------------------------------------------

    def _load_cache(self) -> Dict[str, str]:
        """Load previously resolved phrases.

        The cache is the mechanism that makes repeated runs free and, just as
        importantly, makes them reproducible: a phrase resolved once keeps that
        resolution for every subsequent run regardless of model drift.
        """
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

    # -- transport ----------------------------------------------------------

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
                        status = handle.status
                        text = handle.read().decode("utf-8", errors="replace")
                except urllib.error.HTTPError as error:
                    status = error.code
                    text = error.read().decode("utf-8", errors="replace")
        except Exception as error:
            LOGGER.warning("Language-model request failed: %s", error)
            self.usage.failed_requests += 1
            return None

        if status != 200:
            summary = text[:300].replace("\n", " ")
            retry = retry_chat_body(status, text, body)
            if retry is not None:
                if "temperature" in body and "temperature" not in retry:
                    self._omit_temperature = True
                    LOGGER.info("Model rejected the temperature parameter; retrying without it.")
                if "reasoning_effort" in body and "reasoning_effort" not in retry:
                    self._reasoning_style = "nested" if "reasoning" in retry else "omit"
                    LOGGER.info("Retrying with the reasoning control this endpoint accepts.")
                elif "reasoning" in body and "reasoning" not in retry:
                    self._reasoning_style = "omit"
                return self._post(retry)
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
        """Request a JSON object from the model.

        Returns None on any failure. Every caller treats that as "the model had
        nothing to add" and falls back to its deterministic result, which is why
        an outage degrades quality slightly instead of stopping the run.
        """
        if not self.config.enabled:
            return None
        if self.config.max_requests and self.usage.requests >= self.config.max_requests:
            LOGGER.warning("Language-model request cap (%d) reached.", self.config.max_requests)
            return None

        body: Dict[str, Any] = chat_completion_body(
            self.config.model, system_prompt, user_prompt,
            omit_temperature=self._omit_temperature,
            reasoning_effort=self.config.reasoning_effort or DEFAULT_REASONING_EFFORT,
            reasoning_style=self._reasoning_style,
        )

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
# Translation engine
# ===========================================================================

@dataclass
class TranslationResult:
    """One phrase rendered in English, with the evidence for how it got there."""

    source_text: str
    english_text: str
    language: str
    language_confidence: float
    method: str                 # native | lexicon | compound | neural | model | passthrough
    coverage: float             # share of content tokens resolved, in [0, 1]
    unresolved: Tuple[str, ...] = ()


class TranslationEngine:
    """Renders distinct phrases in English through a four-tier cascade.

    The tiers are ordered by cost, cheapest first, and each one only sees what
    the tier above could not resolve:

        1. controlled vocabulary   free, deterministic, domain-accurate
        2. compound decomposition  free, deterministic, Nordic-specific
        3. offline neural model    free after download, high quality
        4. language model          paid, last resort, cached forever

    Working on distinct phrases rather than rows is what makes this affordable:
    on real spend data the same phrase recurs many times, so the number of
    phrases needing tier 3 or 4 is a small fraction of the row count.
    """

    def __init__(self, lexicon: Lexicon, settings: Settings,
                 neural: NeuralTranslator, model: Optional[LanguageModelClient]) -> None:
        self.lexicon = lexicon
        self.settings = settings
        self.neural = neural
        self.model = model
        self.results: Dict[str, TranslationResult] = {}
        self.method_counts: Counter = Counter()

    # -- tier 1 and 2: vocabulary -------------------------------------------

    def _resolve_with_vocabulary(self, text: str, language: str) -> Tuple[str, float, List[str], str]:
        """Translate token by token using the vocabulary and the compound splitter.

        Returns the rewritten text, the share of content tokens resolved, the
        tokens that remain unresolved, and which mechanism did the most work.
        """
        substituted, phrase_hits, phrase_tokens = self.lexicon.substitute_phrases(
            separate_welded_codes(text), language)

        rendered: List[str] = []
        content_tokens = 0
        resolved_tokens = 0
        unresolved: List[str] = []
        used_compound = False

        for token in tokenise(substituted):
            key = lookup_key(token)

            # The vocabulary is consulted before the code detector, not after.
            # A known word is a word whatever its casing, and checking in this
            # order is what stops a shouted term such as VUOKRA from being
            # written off as a part number.
            known = self.lexicon.unit_terms.get(key) or self.lexicon.term(token, language)

            if known is None and is_code_token(token):
                # Identifiers are preserved verbatim: they are meaningful to a
                # buyer and translating them would be nonsense.
                rendered.append(token)
                continue

            content_tokens += 1
            if known:
                rendered.append(known)
                resolved_tokens += 1
                continue

            # A token that survived phrase substitution and is already English
            # counts as resolved; this is what stops mixed-language text from
            # being penalised for its English half.
            if key in self.lexicon.service_markers or key in self.lexicon.material_markers:
                rendered.append(token)
                resolved_tokens += 1
                continue

            parts = self.lexicon.split_compound(token, language)
            if parts:
                rendered.append(" ".join(parts))
                resolved_tokens += 1
                used_compound = True
                continue

            rendered.append(token)
            unresolved.append(token)

        # Tokens emitted by a phrase substitution are already English, so they
        # count as resolved even though the loop above saw them as bare words it
        # did not recognise. Capping at the content total avoids double-counting
        # a token that the loop also resolved on its own.
        effective_resolved = min(content_tokens, resolved_tokens + phrase_tokens)
        coverage = effective_resolved / content_tokens if content_tokens else 1.0

        method = "compound" if used_compound and not phrase_hits else "lexicon"
        return _WHITESPACE.sub(" ", " ".join(rendered)).strip(), coverage, unresolved, method

    # -- orchestration ------------------------------------------------------

    def prepare(self, phrases: Iterable[str]) -> None:
        """Resolve a whole corpus of distinct phrases, cheapest tier first.

        Batched by tier rather than by phrase so that the neural models are
        loaded once per language and the language model is called with many
        phrases per request.
        """
        pending: Dict[str, Tuple[str, float]] = {}

        for phrase in sorted(set(phrases)):
            if not phrase or phrase in self.results:
                continue
            language, confidence = detect_language(phrase, self.lexicon)

            # The vocabulary pass runs for every phrase, including ones believed
            # to be English. Identification is fallible on single words -
            # "asbestipurku" contains no non-ASCII character and no Finnish
            # function word, so it reads as English - and short-circuiting on
            # that belief would leave the phrase untranslated. If the vocabulary
            # in fact changes nothing, the phrase really was English.
            english, coverage, unresolved, method = self._resolve_with_vocabulary(phrase, language)

            if (language == "en"
                    and lookup_key(english) == lookup_key(phrase)
                    and not has_non_english(phrase)
                    and not has_non_english(english)):
                self.results[phrase] = TranslationResult(
                    phrase, phrase, "en", confidence, "native", 1.0)
                continue

            # 0.85 is the point at which the remaining unresolved tokens are
            # typically proper nouns or place names, which a translator would
            # leave alone anyway. Below it, a real translation is worth paying
            # for. Leftover Finnish/Swedish/Polish common nouns are never
            # accepted as done, however high the coverage.
            if coverage >= 0.85 and not has_non_english(english):
                self.results[phrase] = TranslationResult(
                    phrase, english, language, confidence, method, coverage, tuple(unresolved))
                continue

            # Hold the partial result: if the richer tiers are unavailable or
            # fail, this is still a better answer than the untranslated source.
            self.results[phrase] = TranslationResult(
                phrase, english, language, confidence, method, coverage, tuple(unresolved))
            pending[phrase] = (language, coverage)

        self._run_neural_tier(pending)
        self._run_model_tier(pending)

        leftover = {
            phrase: (result.language, result.coverage)
            for phrase, result in self.results.items()
            if has_non_english(result.english_text)
        }
        if leftover:
            self._run_model_tier(leftover)

        for phrase, result in list(self.results.items()):
            cleaned = drop_foreign_common_nouns(result.english_text)
            if cleaned and cleaned != result.english_text and not has_non_english(cleaned):
                self.results[phrase] = TranslationResult(
                    result.source_text, cleaned, result.language, result.language_confidence,
                    result.method, result.coverage, result.unresolved)

        for result in self.results.values():
            self.method_counts[result.method] += 1

    def _run_neural_tier(self, pending: Dict[str, Tuple[str, float]]) -> None:
        """Translate everything the vocabulary could not, using local models."""
        if not self.settings.use_neural_translation or not self.neural.enabled or not pending:
            return

        by_language: Dict[str, List[str]] = defaultdict(list)
        for phrase, (language, _) in pending.items():
            if self.neural.available_for(language):
                by_language[language].append(phrase)

        for language in sorted(by_language):
            phrases = sorted(by_language[language])
            LOGGER.info("Translating %d phrase(s) from %s with the offline model.",
                        len(phrases), language)
            translations = self.neural.translate_batch(phrases, language)
            for phrase, english in translations.items():
                previous = self.results[phrase]
                self.results[phrase] = TranslationResult(
                    phrase, english, previous.language, previous.language_confidence,
                    "neural", max(previous.coverage, 0.9), previous.unresolved)
                if not has_non_english(english):
                    pending.pop(phrase, None)

    def _run_model_tier(self, pending: Dict[str, Tuple[str, float]]) -> None:
        """Send the residue to the language model, in batches, with caching."""
        if self.model is None or not self.model.config.enabled or not pending:
            return

        outstanding: List[str] = []
        for phrase in sorted(pending):
            key = self.model.cache_key("translate_en_v2", phrase)
            cached = self.model.cached(key)
            if cached:
                previous = self.results[phrase]
                cleaned = drop_foreign_common_nouns(cached) or cached
                self.results[phrase] = TranslationResult(
                    phrase, cleaned, previous.language, previous.language_confidence,
                    "model", 1.0, ())
            else:
                outstanding.append(phrase)

        if not outstanding:
            return

        LOGGER.info("Sending %d unresolved phrase(s) to %s.",
                    len(outstanding), self.model.config.model)

        system_prompt = (
            "You translate short procurement and purchase-order line texts into "
            "concise English. These are industrial and energy sector purchases: "
            "materials, spare parts, maintenance work and professional services.\n"
            "Rules:\n"
            "1. The translation MUST be English. Translate every Finnish, "
            "Swedish, Polish or German common noun. Never copy a source-language "
            "word into the translation. Keep part numbers, brand names, place "
            "names, person names, wattage, quantities and item numbers as they "
            "appear.\n"
            "2. Translate only what is written. Never add detail that is not in "
            "the source text.\n"
            "3. Keep part numbers, order references and measurements exactly as "
            "they appear.\n"
            "4. Return a noun phrase describing what was purchased, not a "
            "sentence and not an explanation.\n"
            "5. If the text carries no meaning, return it unchanged only when it "
            "is already English; otherwise translate it.\n"
            'Reply with JSON: {"translations": {"<source>": "<english>"}}. '
            "Every key must be reproduced exactly as given. Every value must be "
            "English."
        )

        batch_size = max(1, self.model.config.batch_size)
        for start in range(0, len(outstanding), batch_size):
            batch = outstanding[start:start + batch_size]
            user_prompt = json.dumps({"texts": batch}, ensure_ascii=False)
            response = self.model.complete_json(system_prompt, user_prompt)
            if not response:
                continue

            translations = response.get("translations") or {}
            if not isinstance(translations, dict):
                continue
            for phrase in batch:
                english = drop_foreign_common_nouns(
                    normalise_text(translations.get(phrase, ""))) or normalise_text(
                    translations.get(phrase, ""))
                if not english:
                    continue
                self.model.store(self.model.cache_key("translate_en_v2", phrase), english)
                previous = self.results[phrase]
                self.results[phrase] = TranslationResult(
                    phrase, english, previous.language, previous.language_confidence,
                    "model", 1.0, ())

    def translate(self, phrase: str) -> TranslationResult:
        """Look up an already-prepared phrase.

        Phrases that were never prepared are resolved inline. That only happens
        for text assembled after the preparation pass, and keeping the method
        total avoids a class of ordering bug that would otherwise be silent.
        """
        if phrase in self.results:
            return self.results[phrase]
        language, confidence = detect_language(phrase, self.lexicon)
        english, coverage, unresolved, method = self._resolve_with_vocabulary(phrase, language)
        cleaned = drop_foreign_common_nouns(english)
        if cleaned and not has_non_english(cleaned):
            english = cleaned
        result = TranslationResult(phrase, english, language, confidence,
                                   method, coverage, tuple(unresolved))
        self.results[phrase] = result
        return result

    def ensure_english(self, text: str) -> str:
        """Guarantee a published string contains no source-language nouns.

        Vocabulary, then the local neural model (trying Finnish/Swedish/Polish
        if identification was unsure), then the language model, then stripping
        leftover foreign tokens. An empty return means the line must be Unclear.
        """
        if not text:
            return ""
        language, _ = detect_language(text, self.lexicon)
        if not has_non_english(text) and language in {"en", "und"}:
            return text.strip()

        english, _, _, _ = self._resolve_with_vocabulary(text, language)
        if english and not has_non_english(english):
            return tidy_published_english(english)

        candidates = [language] if language in {"fi", "sv", "pl", "de"} else []
        for code in ("fi", "sv", "pl", "de"):
            if code not in candidates:
                candidates.append(code)
        if self.settings.use_neural_translation and self.neural.enabled:
            for code in candidates:
                if not self.neural.available_for(code):
                    continue
                translated = self.neural.translate_batch([text], code).get(text, "")
                if translated and not has_non_english(translated):
                    return tidy_published_english(translated)

        if self.model is not None and self.model.config.enabled:
            pending = {text: (language if language in {"fi", "sv", "pl", "de"} else "fi", 0.0)}
            self.results.setdefault(text, TranslationResult(
                text, english or text, language, 0.0, "lexicon", 0.0))
            self._run_model_tier(pending)
            forced = self.results.get(text)
            if forced and forced.english_text and not has_non_english(forced.english_text):
                return tidy_published_english(forced.english_text)

        cleaned = drop_foreign_common_nouns(english or text)
        if cleaned and not has_non_english(cleaned):
            return tidy_published_english(cleaned)
        return ""

    def polish_composed(self, original: str, draft: str, extra: str,
                        short: str, item_or_service: str) -> Tuple[str, str, str]:
        """Ask the model to confirm a composed description is English and relevant.

        Used for the three published columns. Cached on the original line plus
        the draft, so a second run over the same data costs nothing.
        """
        if self.model is None or not self.model.config.enabled:
            return draft, short, item_or_service
        if not draft:
            return draft, short, item_or_service

        payload = json.dumps({
            "original": original,
            "extra_evidence": extra,
            "draft_description": draft,
            "draft_short": short,
            "draft_item_or_service": item_or_service,
        }, ensure_ascii=False)
        cache_key = self.model.cache_key("polish_sentence_v1", payload)
        cached = self.model.cached(cache_key)
        if cached:
            try:
                parsed = json.loads(cached)
                return self._take_polish(parsed, draft, short, item_or_service)
            except json.JSONDecodeError:
                pass

        prompt = (
            "You write one clear English sentence that says what was purchased. "
            "Read the original line carefully. Do not rush.\n"
            "Return JSON: {\"description\": \"...\", \"short_description\": "
            "\"...\", \"item_or_service\": \"Material|Service|Unclear\"}.\n"
            "Rules:\n"
            "1. description is one or two complete English sentences with a verb "
            "(purchased, supplied, carried out, and so on), typically 12 to 30 "
            "words. Never return a title or a noun phrase. It must name the item "
            "or the service, keep wattage, quantities, sizes and item numbers, "
            "and be something a buyer can read without guessing. Never copy "
            "Finnish, Swedish, Polish or German.\n"
            "2. short_description is a compact English noun phrase of at most "
            "twelve words for the same purchase.\n"
            "3. 'original' is the authoritative purchase line. Ignore "
            "'extra_evidence' when it describes a different purchase, and "
            "ignore buyer internal notes (stock instructions, lead times, "
            "'confirmed with site manager').\n"
            "4. If original does not name an item or a service that was "
            "purchased, set item_or_service to Unclear and set description to "
            "an empty string. Do not invent a product from a note or a quote "
            "reference.\n"
            "5. If the draft is a fragment, expand it into a sentence using "
            "only facts in original. Do not invent detail that is not in the source.\n"
            "6. If original is empty or a placeholder, set item_or_service to "
            "Unclear rather than guessing from a category."
        )
        response = self.model.complete_json(prompt, payload)
        if not response:
            return (keep_published_english(draft),
                    keep_published_english(short),
                    item_or_service)
        self.model.store(cache_key, json.dumps(response, ensure_ascii=False, sort_keys=True))
        return self._take_polish(response, draft, short, item_or_service)

    @staticmethod
    def _take_polish(parsed: Dict[str, Any], draft: str, short: str,
                     item_or_service: str) -> Tuple[str, str, str]:
        item = normalise_text(parsed.get("item_or_service")).title()
        if item not in {"Material", "Service", "Unclear"}:
            item = item_or_service or "Unclear"
        raw_description = keep_published_english(parsed.get("description"))
        if item == "Unclear" and not raw_description:
            return "", "", "Unclear"
        description = raw_description or keep_published_english(draft)
        short_out = (keep_published_english(parsed.get("short_description"))
                     or keep_published_english(short))
        if not description:
            return "", "", item if item == "Unclear" else (item_or_service or "Unclear")
        return sentence_case(description), sentence_case(short_out), item


# ===========================================================================
# Linguistic analysis
# ===========================================================================

class EnglishAnalyser:
    """Noun-phrase extraction and entity filtering over English text.

    Analysis runs *after* translation rather than before it, which is a
    deliberate simplification: it means one well-supported English pipeline does
    the syntactic work instead of four uneven ones, and it means the same rules
    apply to every source language. The cost is that a translation error
    propagates, which the coverage measure already exposes.
    """

    # Entity labels whose text is a name rather than a thing that was purchased.
    _DROPPABLE_ENTITIES = frozenset({"PERSON", "GPE", "LOC", "DATE", "TIME",
                                     "MONEY", "CARDINAL", "ORDINAL", "PERCENT"})

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and _spacy is not None
        self._pipeline: Optional[Any] = None
        self._loaded = False
        self.stopwords: Set[str] = self._load_stopwords()

    @staticmethod
    def _load_stopwords() -> Set[str]:
        """English stop words, from NLTK when present and a core list otherwise."""
        core = {
            "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at",
            "by", "with", "from", "as", "is", "are", "was", "were", "be", "been",
            "this", "that", "these", "those", "it", "its", "per", "pcs", "each",
            "new", "old", "other", "misc", "various", "general", "total", "no",
        }
        if _nltk is None:
            return core
        try:
            from nltk.corpus import stopwords
            return core | set(stopwords.words("english"))
        except Exception:
            return core

    def _load_pipeline(self) -> Optional[Any]:
        """Load the English pipeline once, tolerating an absent model."""
        if self._loaded:
            return self._pipeline
        self._loaded = True
        if not self.enabled:
            return None
        for model_name in ("en_core_web_sm", "en_core_web_md", "xx_ent_wiki_sm"):
            try:
                self._pipeline = _spacy.load(model_name, disable=["lemmatizer"])
                LOGGER.info("Loaded spaCy pipeline %s.", model_name)
                return self._pipeline
            except Exception:
                continue
        LOGGER.info(
            "No spaCy English model is installed; using rule-based phrase extraction.")
        self.enabled = False
        return None

    def noun_phrases(self, text: str) -> List[str]:
        """Extract candidate noun phrases, longest and earliest first.

        Order matters: the first noun phrase of a procurement line is almost
        always the thing being bought, and everything after it qualifies.
        """
        if not text.strip():
            return []

        pipeline = self._load_pipeline()
        if pipeline is None:
            return self._rule_based_phrases(text)

        try:
            document = pipeline(text)
        except Exception:
            return self._rule_based_phrases(text)

        # Character spans covered by an entity that should not appear in the
        # description, so that a phrase overlapping one can be discarded whole.
        blocked_spans = [
            (entity.start_char, entity.end_char)
            for entity in document.ents if entity.label_ in self._DROPPABLE_ENTITIES
        ]

        phrases: List[str] = []
        for chunk in document.noun_chunks:
            if any(start < chunk.end_char and chunk.start_char < end
                   for start, end in blocked_spans):
                continue
            words = [token.text for token in chunk
                     if not token.is_punct and not token.is_space
                     and token.text.lower() not in self.stopwords]
            if words:
                phrases.append(" ".join(words))

        if not phrases:
            # Some lines are a bare verb phrase such as "removing asbestos",
            # which yields no noun chunk at all.
            phrases = [token.text for token in document
                       if token.pos_ in {"NOUN", "PROPN", "VERB", "ADJ"}
                       and token.text.lower() not in self.stopwords]
        return phrases or self._rule_based_phrases(text)

    def _rule_based_phrases(self, text: str) -> List[str]:
        """Phrase extraction without spaCy.

        Splits on punctuation and connectives, then keeps the fragments that
        still carry content words. Markedly worse than the parser, but it keeps
        the agent useful on a machine where no model could be installed.
        """
        fragments = re.split(r"[,;:/|]|\s+-\s+|\s+\u2013\s+", text)
        phrases: List[str] = []
        for fragment in fragments:
            words = [word for word in tokenise(fragment)
                     if word.lower() not in self.stopwords and not is_code_token(word)]
            if words:
                phrases.append(" ".join(words))
        return phrases

    def head_noun(self, text: str) -> str:
        """The single most representative word of a phrase.

        Used by the downstream agents to check that two descriptions are talking
        about the same kind of thing before believing a similarity score.
        """
        phrases = self.noun_phrases(text)
        if not phrases:
            return ""
        # English noun phrases are head-final, so the last word of the first
        # phrase is the head.
        words = [word for word in tokenise(phrases[0])
                 if word.lower() not in self.stopwords]
        return words[-1].lower() if words else ""


# ===========================================================================
# Semantic retrieval
# ===========================================================================

class SemanticIndex:
    """Nearest-neighbour search over sentence embeddings.

    The last matching tier, reached only for lines that no business key and no
    fuzzy comparison could resolve. It exists because the same purchase is
    routinely described with no words in common at all - "asbestipurku" and
    "rivning av asbest" share not one character sequence - and lexical methods
    are structurally incapable of connecting them.

    The model is multilingual and runs locally on CPU, so this costs nothing per
    query once the model is resident. The index is built over *distinct phrases*
    rather than rows; at a million lines that is the difference between a few
    seconds and an afternoon.
    """

    MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    def __init__(self, enabled: bool = True, threshold: float = 0.72,
                 top_k: int = 5, phrase_cap: int = 200_000) -> None:
        self.enabled = enabled and _sentence_transformers is not None and _numpy is not None
        self.threshold = threshold
        self.top_k = top_k
        self.phrase_cap = phrase_cap
        self.phrases: List[str] = []
        self._model: Optional[Any] = None
        self._matrix: Optional[Any] = None

    def build(self, phrases: Iterable[str]) -> None:
        """Embed a corpus of phrases and prepare it for querying."""
        if not self.enabled:
            return

        # Sorted so that the row order of the matrix, and therefore the
        # tie-breaking between equally similar neighbours, is identical on every
        # run over the same data.
        self.phrases = sorted({phrase for phrase in phrases if phrase and len(phrase) >= 4})
        if len(self.phrases) < 2:
            self.enabled = False
            return

        if len(self.phrases) > self.phrase_cap:
            LOGGER.warning(
                "Semantic index skipped: %d distinct phrases exceeds the cap of %d. "
                "Raise --semantic-cap if the machine has the memory for it.",
                len(self.phrases), self.phrase_cap)
            self.enabled = False
            return

        try:
            LOGGER.info("Loading embedding model %s ...", self.MODEL_NAME)
            self._model = load_sentence_transformer(_sentence_transformers, self.MODEL_NAME)
            LOGGER.info("Embedding %d distinct phrase(s) ...", len(self.phrases))
            self._matrix = self._model.encode(
                self.phrases, batch_size=64, convert_to_numpy=True,
                normalize_embeddings=True, show_progress_bar=False)
        except Exception as error:
            LOGGER.warning("Semantic index unavailable (%s).", error)
            self.enabled = False
            self._model = self._matrix = None

    def query(self, text: str) -> List[Tuple[str, float]]:
        """Return the nearest phrases above the threshold, best first.

        Vectors are unit-normalised at encoding time, so the dot product is the
        cosine similarity and no per-query normalisation is needed.
        """
        if not self.enabled or self._model is None or self._matrix is None or not text:
            return []

        try:
            vector = self._model.encode([text], convert_to_numpy=True,
                                        normalize_embeddings=True, show_progress_bar=False)[0]
        except Exception:
            return []

        similarities = self._matrix @ vector
        count = min(self.top_k + 1, len(self.phrases))
        # argpartition finds the top-k without sorting the whole array, which
        # matters when the phrase pool is large and this runs once per row.
        candidates = _numpy.argpartition(-similarities, count - 1)[:count]
        ranked = sorted(candidates, key=lambda index: (-float(similarities[index]),
                                                       self.phrases[index]))

        results: List[Tuple[str, float]] = []
        for index in ranked:
            phrase = self.phrases[index]
            score = float(similarities[index])
            if phrase == text or score < self.threshold:
                continue
            results.append((phrase, round(score, 3)))
            if len(results) >= self.top_k:
                break
        return results


# ===========================================================================
# Record model
# ===========================================================================

@dataclass
class EvidenceBundle:
    """Everything known about one purchase, pooled across source systems.

    Pooling into a bundle rather than keeping every contributing row is what
    holds memory flat at a million lines: the number of distinct purchases is
    bounded by the number of purchase-order lines, and each bundle stores sets of
    short strings rather than whole rows.
    """

    descriptions: Set[str] = field(default_factory=set)
    context: Set[str] = field(default_factory=set)
    codes: Set[str] = field(default_factory=set)
    suppliers: Set[str] = field(default_factory=set)
    categories: Set[str] = field(default_factory=set)
    systems: Set[str] = field(default_factory=set)
    amounts: Set[float] = field(default_factory=set)

    def absorb(self, other: "EvidenceBundle") -> None:
        self.descriptions |= other.descriptions
        self.context |= other.context
        self.codes |= other.codes
        self.suppliers |= other.suppliers
        self.categories |= other.categories
        self.systems |= other.systems
        self.amounts |= other.amounts

    def is_empty(self) -> bool:
        return not (self.descriptions or self.context or self.codes)


@dataclass
class LineRecord:
    """One row of one source, with its enrichment attached.

    Held only for the duration of writing that row, so the field count here does
    not constrain how large an input can be processed.
    """

    row_id: str
    source_system: str
    source_file: str
    source_sheet: str
    source_row: int
    row_type: str

    keys: Dict[str, str] = field(default_factory=dict)
    business: Dict[str, str] = field(default_factory=dict)

    # Processing text, softened for analysis, and the untouched source values.
    # Both are kept because the enrichment must be built from the former while
    # the audit trail has to show the latter exactly as the client supplied it.
    own_descriptions: List[str] = field(default_factory=list)
    own_descriptions_raw: List[str] = field(default_factory=list)
    own_description_fields: List[str] = field(default_factory=list)
    own_context: List[str] = field(default_factory=list)
    own_codes: List[str] = field(default_factory=list)

    # The row's free text was present and was entirely a buyer note, so the
    # line is Unclear rather than a guess from its category or its supplier.
    own_text_was_note: bool = False

    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)
    match_tier: str = "none"
    match_score: float = 0.0
    matched_systems: Tuple[str, ...] = ()

    is_duplicate: bool = False
    duplicate_of: str = ""

    @property
    def primary_text(self) -> str:
        """The best free-text value this row carries, exactly as supplied."""
        return self.own_descriptions_raw[0] if self.own_descriptions_raw else ""

    @property
    def primary_processing_text(self) -> str:
        """The same value, softened for analysis."""
        return self.own_descriptions[0] if self.own_descriptions else ""


# ===========================================================================
# Description synthesis
# ===========================================================================

@dataclass
class DescriptionResult:
    """A composed description and the record of how it was composed."""

    description: str = ""
    short_description: str = ""
    item_or_service: str = "Unclear"
    basis: str = "none"                 # description | context | taxonomy | none
    used_fragments: Tuple[str, ...] = ()
    specificity: float = 0.0


class DescriptionSynthesiser:
    """Composes the enriched description from validated fragments.

    The critical requirement from the validation column of the development plan
    is that the description must contain no invented information. That is
    enforced structurally rather than by instruction: the synthesiser can only
    emit words that appeared in the source data, in a vocabulary translation of
    the source data, or in the fixed connector set below. There is no path
    through this class by which a word can reach the output without having come
    from the input.
    """

    # The only words the synthesiser may add. Everything else must be traceable.
    _CONNECTORS = frozenset({"and", "for", "with", "service", "services", "work",
                             "item", "no", "nr", "ref", "part", "code"})

    _BOILERPLATE = re.compile(
        r"\b(according to contract|as per contract|per agreement|see attachment|"
        r"see enclosure|no description|not defined|account not defined|"
        r"terms and conditions|hourly rate|invoice|purchase order|order confirmation)\b",
        re.IGNORECASE)

    def __init__(self, lexicon: Lexicon, analyser: EnglishAnalyser, settings: Settings) -> None:
        self.lexicon = lexicon
        self.analyser = analyser
        self.settings = settings

    # -- cleaning -----------------------------------------------------------

    def _strip_noise(self, text: str) -> str:
        """Remove everything that is not part of what was purchased.

        Order references, dates, e-mail addresses and pasted correspondence are
        all common in these fields and none of them describe the purchase.
        """
        text = separate_welded_codes(text)
        text = _URL.sub(" ", text)
        text = _EMAIL.sub(" ", text)
        text = _MONEY.sub(" ", text)
        text = _DATE_LIKE.sub(" ", text)
        text = self._BOILERPLATE.sub(" ", text)
        text = _LEADING_CODE.sub("", text)
        # Long digit runs that are not a short quantity or a measurement stay
        # out; wattage and item numbers are kept by is_measurement_token.
        text = re.sub(r"[\"'`*_<>\[\]{}()]+", " ", text)
        text = re.sub(r"\s*[-–—/|:;,]\s*", " ", text)
        return _WHITESPACE.sub(" ", text).strip()

    def _is_meaningful(self, text: str) -> bool:
        """Whether a cleaned fragment still says anything useful."""
        if not text or self.lexicon.is_noise(text):
            return False
        words = [word for word in tokenise(text)
                 if not is_code_token(word) and word.lower() not in self.analyser.stopwords]
        return len(words) >= 1 and any(len(word) >= 3 for word in words)

    # -- classification -----------------------------------------------------

    # The line's own text is worth more than the category it was filed under.
    # A maintenance line inside the "Measurement equipment" category is still a
    # service, and weighting the two sources equally gets that backwards.
    _OWN_TEXT_WEIGHT = 2.0
    _CONTEXT_WEIGHT = 1.0

    def classify_item_or_service(self, text: str, context: Sequence[str]) -> str:
        """Decide whether a line bought a thing or an activity.

        Reported as Unclear rather than guessed when the evidence is balanced or
        absent. The development plan asks for this only where the data supports
        a reliable assessment, and a coin-flip label would be actively harmful
        in a spend analysis.
        """
        if not text and not context:
            return "Unclear"

        # The line's own text is consulted alone first. Only when it is silent
        # or genuinely balanced does the surrounding context get a vote. Letting
        # the two compete on a single tally lets a category with several marker
        # words outvote an unambiguous description, which is how "asbestos
        # removal" filed under "Measurement equipment" ends up labelled a
        # material.
        own = self._marker_tally(text)
        if own[0] != own[1]:
            return "Service" if own[0] > own[1] else "Material"

        surrounding = self._marker_tally(" ".join(context))
        if surrounding[0] != surrounding[1]:
            return "Service" if surrounding[0] > surrounding[1] else "Material"
        return "Unclear"

    def _marker_tally(self, source: str) -> Tuple[int, int]:
        """Count service and material marker words in one body of text."""
        haystack = lookup_key(source)
        if not haystack:
            return 0, 0
        tokens = set(tokenise(haystack))

        def hits(markers: Set[str]) -> int:
            return sum(1 for marker in markers
                       if marker in tokens or (" " in marker and marker in haystack))

        return hits(self.lexicon.service_markers), hits(self.lexicon.material_markers)

    # -- composition --------------------------------------------------------

    def compose(self, record: LineRecord, english_fragments: Sequence[str],
                context_fragments: Sequence[str]) -> DescriptionResult:
        """Build the description for one line.

        Three sources are tried in order of how directly they describe the
        purchase. The basis is recorded because a description derived from a
        category is a much weaker statement than one derived from the line's own
        text, and the confidence score has to know the difference.
        """
        cleaned_primary = [self._strip_noise(strip_non_purchase_text(fragment))
                           for fragment in english_fragments]
        cleaned_primary = [fragment for fragment in cleaned_primary
                           if self._is_meaningful(fragment) and not is_non_purchase_text(fragment)]

        cleaned_context = [self._strip_noise(strip_non_purchase_text(fragment))
                           for fragment in context_fragments]
        cleaned_context = [fragment for fragment in cleaned_context
                           if self._is_meaningful(fragment) and not is_non_purchase_text(fragment)]

        # A note that does not name an item or a service is Unclear, not a
        # guess from the category the line happened to be filed under. The same
        # applies when the note was stripped before it reached this method.
        if not cleaned_primary and record.own_text_was_note:
            return DescriptionResult(item_or_service="Unclear")
        if (not cleaned_primary and english_fragments
                and all(is_non_purchase_text(fragment) or not self._is_meaningful(self._strip_noise(fragment))
                        for fragment in english_fragments)):
            return DescriptionResult(item_or_service="Unclear")

        if cleaned_primary:
            description, used = self._from_fragments(cleaned_primary)
            basis = "description"
        elif cleaned_context:
            description, used = self._from_fragments(cleaned_context)
            basis = "context"
        else:
            taxonomy = [value for value in record.business.get("category_path", "").split(" > ")
                        if value and not self.lexicon.is_noise(value)]
            if taxonomy:
                # The most specific level available is the last populated one.
                description, used = sentence_case(taxonomy[-1]), (taxonomy[-1],)
                basis = "taxonomy"
            else:
                return DescriptionResult()

        if not description:
            return DescriptionResult()

        item_or_service = self.classify_item_or_service(
            description, list(cleaned_context) + list(record.evidence.categories))

        # "service" is appended only when the source text actually indicated a
        # service and did not already say so. This is the one place a word is
        # added, and it is licensed by evidence in the input.
        lowered = description.lower()
        if (item_or_service == "Service"
                and basis == "description"
                and not any(marker in lowered for marker in
                            ("service", "work", "maintenance", "repair", "consulting",
                             "rental", "training", "inspection", "cleaning", "removal"))
                and len(tokenise(description)) < self.settings.max_words):
            description = f"{description} service"

        short = self._shorten(description)
        polished = DescriptionResult(
            description=sentence_case(description),
            short_description=sentence_case(short),
            item_or_service=item_or_service,
            basis=basis,
            used_fragments=tuple(used),
            specificity=self._specificity(description),
        )
        return polished

    def _from_fragments(self, fragments: Sequence[str]) -> Tuple[str, Tuple[str, ...]]:
        """Choose one description fragment rather than concatenating several.

        Merging a Finnish line with a Polish invoice article and an English
        note is how ``Lepakkoselvitys tuulipuistoon Bat`` was published. Fully
        English fragments win; among those, the longest informative one is kept.
        """
        unique = [fragment for fragment in dict.fromkeys(fragments) if fragment]
        if not unique:
            return "", ()

        english = [fragment for fragment in unique if not has_non_english(fragment)]
        pool = english or unique
        chosen = max(pool, key=lambda item: (len(tokenise(item)), len(item)))

        words = keep_purchase_tokens(tokenise(chosen))
        words = [word for word in words
                 if word.isdigit()
                 or word.lower() not in self.analyser.stopwords
                 or word.lower() in self._CONNECTORS]
        if not words:
            return "", ()
        if len(words) > self.settings.max_words:
            words = words[: self.settings.max_words]
        return _WHITESPACE.sub(" ", " ".join(words)).strip(), (chosen,)

    def _shorten(self, description: str) -> str:
        """Reduce a description to its head phrase for compact display."""
        phrases = self.analyser.noun_phrases(description)
        candidate = phrases[0] if phrases else description
        words = tokenise(candidate)[: self.settings.max_short_words]
        # Truncation can leave "... item" with the number it introduced cut off.
        while words and lookup_key(words[-1]) in _ITEM_NUMBER_MARKERS:
            words.pop()
        return " ".join(words)

    @staticmethod
    def _specificity(description: str) -> float:
        """How informative a description is, in ``[0, 1]``.

        Rewards content words and distinct words, and penalises the generic
        vocabulary that indicates the pipeline had little to work with.
        """
        words = [word.lower() for word in tokenise(description)]
        if not words:
            return 0.0
        generic = {"other", "various", "general", "misc", "material", "materials",
                   "service", "services", "equipment", "supplies", "item", "items",
                   "work", "works", "product", "products", "goods"}
        informative = [word for word in words if word not in generic and len(word) > 2]
        distinctness = len(set(words)) / len(words)
        return round(min(1.0, (len(informative) / 4.0) * 0.7 + distinctness * 0.3), 3)


# ===========================================================================
# Confidence scoring
# ===========================================================================

def score_confidence(record: LineRecord, description: DescriptionResult,
                     translation: TranslationResult) -> Tuple[int, str, Dict[str, float]]:
    """Score how much the enriched description can be relied upon.

    The score is computed from measurable properties of the pipeline, never from
    a model's opinion of its own output. A model asked to rate its own answer
    reports its fluency, which on this task is uncorrelated with correctness:
    the most confident-sounding descriptions are precisely the invented ones.

    The factors are returned alongside the score so that a reviewer can see why
    a line scored as it did, which is what makes the sample-based validation in
    the development plan practical.
    """
    factors: Dict[str, float] = {}

    # How directly the description reflects the line's own text.
    factors["basis"] = {"description": 1.0, "context": 0.65,
                        "taxonomy": 0.3, "none": 0.0}.get(description.basis, 0.0)

    # How much of the source language was actually resolved.
    factors["translation"] = round(min(1.0, translation.coverage), 3)

    # How many independent fields contributed. Corroboration across systems is
    # the strongest evidence available that a description is right.
    field_count = len(record.evidence.descriptions) + len(record.evidence.context)
    factors["evidence"] = round(min(1.0, field_count / 4.0), 3)

    # Agreement between source systems, which only a successful match can give.
    factors["corroboration"] = round(min(1.0, max(0, len(record.evidence.systems) - 1) / 2.0), 3)

    # How informative the result is.
    factors["specificity"] = description.specificity

    # How reliable the link to the other systems was, if one was used.
    factors["match"] = {"key": 1.0, "fuzzy": 0.75, "semantic": 0.6,
                        "none": 0.5}.get(record.match_tier, 0.5)

    weights = {
        "basis": 0.28,
        "translation": 0.18,
        "evidence": 0.16,
        "corroboration": 0.10,
        "specificity": 0.20,
        "match": 0.08,
    }
    raw = sum(factors[name] * weight for name, weight in weights.items())
    score = int(round(max(0.0, min(1.0, raw)) * 100))

    # A description built purely from a category is a statement about the
    # category, not about the line, and must not reach the high band however
    # well the other factors score.
    if description.basis == "taxonomy":
        score = min(score, CONFIDENCE_MEDIUM - 1)
    if not description.description:
        score = 0

    band = ("High" if score >= CONFIDENCE_HIGH
            else "Medium" if score >= CONFIDENCE_MEDIUM else "Low")
    return score, band, factors


# ===========================================================================
# Pipeline
# ===========================================================================

class Agent1:
    """Orchestrates the run.

    Structured as two streaming passes over the input. The first pass reads
    every row to learn what is in the data: which distinct phrases occur, which
    business keys link the systems together, and which rows duplicate each other.
    Between the passes, all the expensive work happens once, on the distinct
    phrases. The second pass reads the rows again and writes the output.

    Two passes cost twice the I/O and save an unbounded amount of memory, which
    is the right trade at a million lines. Nothing is held between the passes
    except the phrase table and the evidence index, both of which are bounded by
    the number of *distinct* values rather than the number of rows.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lexicon = Lexicon.load(settings.lexicon_path)
        self.analyser = EnglishAnalyser()
        self.neural = NeuralTranslator(enabled=settings.use_neural_translation)

        self.model: Optional[LanguageModelClient] = None
        if settings.model.enabled:
            self.model = LanguageModelClient(
                settings.model, settings.cache_dir / "agent1_model_cache.json",
                interactive=settings.interactive)

        self.translator = TranslationEngine(self.lexicon, settings, self.neural, self.model)
        self.synthesiser = DescriptionSynthesiser(self.lexicon, self.analyser, settings)
        self.semantic = SemanticIndex(
            enabled=settings.use_semantic_matching,
            threshold=settings.semantic_threshold,
            top_k=settings.top_k,
            phrase_cap=settings.semantic_phrase_cap,
        )

        self.tables: List[Tuple[Table, SourceProfile, ColumnResolver]] = []
        self.run_id = ""

        # Cross-source evidence, keyed by every identifier a row can be found by.
        self.evidence_by_key: Dict[str, EvidenceBundle] = defaultdict(EvidenceBundle)
        # Distinct phrases seen anywhere in the input.
        self.phrase_pool: Set[str] = set()
        # Content hash -> first row identifier carrying it, for duplicate marking.
        self.content_seen: Dict[str, str] = {}
        # Phrases indexed for the fuzzy and semantic matching tiers.
        self.phrase_index: Dict[str, Set[str]] = defaultdict(set)

        self.statistics: Dict[str, Any] = Counter()
        self.input_hashes: Dict[str, str] = {}

    # -- discovery ----------------------------------------------------------

    def discover(self) -> None:
        """Locate, read and profile every input table."""
        paths = discover_input_files(self.settings.source_dir)
        if not paths:
            raise SystemExit(f"No readable data files were found under {self.settings.source_dir}")

        LOGGER.info("Found %d candidate file(s) under %s", len(paths), self.settings.source_dir)

        for path in paths:
            for table in read_table_file(path):
                sample: List[List[str]] = []
                for _, values in table.iter_rows():
                    sample.append(values)
                    if len(sample) >= 200:
                        break
                if not sample:
                    LOGGER.info("Skipping %s: no data rows.", table.label)
                    continue

                profile, score = profile_table(table, sample)
                resolver = ColumnResolver(table.headers)
                self.tables.append((table, profile, resolver))
                LOGGER.info("%-46s -> %-22s (fingerprint match %.0f%%, %d columns)",
                            table.label, profile.name, score * 100, len(table.headers))

            if path.is_file():
                self.input_hashes[str(path.relative_to(self.settings.source_dir)
                                      if self.settings.source_dir in path.parents
                                      else path.name)] = sha256_file(path)

        if not self.tables:
            raise SystemExit("No table contained usable data.")

        # The run identifier is derived from the inputs and the configuration,
        # never from the clock, so an identical run produces an identical id and
        # an output file can always be traced to exactly what produced it.
        signature = json.dumps({
            "inputs": dict(sorted(self.input_hashes.items())),
            "agent": AGENT_VERSION,
            "lexicon": self.lexicon.version,
            "max_words": self.settings.max_words,
            "fuzzy": self.settings.fuzzy_threshold,
            "semantic": self.settings.semantic_threshold,
            "neural": self.settings.use_neural_translation,
            "llm": self.settings.model.enabled,
            "model": self.settings.model.model if self.settings.model.enabled else "",
        }, sort_keys=True)
        self.run_id = stable_hash("agent1-run", signature)

    # -- pass one -----------------------------------------------------------

    def _row_keys(self, resolver: ColumnResolver, row: Sequence[str]) -> Dict[str, str]:
        """Extract the identifiers a row can be joined on."""
        return {
            "document_number": resolver.value(row, "document_number"),
            "document_line_number": resolver.value(row, "document_line_number"),
            "po_number": resolver.value(row, "po_number"),
            "po_line_number": resolver.value(row, "po_line_number"),
            "invoice_number": resolver.value(row, "invoice_number"),
            "item_number": resolver.value(row, "item_number"),
        }

    @staticmethod
    def _join_keys(keys: Dict[str, str]) -> List[str]:
        """Build the join keys a row participates in, most specific first.

        A purchase-order line key is far stronger evidence of identity than a
        purchase-order header key, so they are namespaced separately and the
        matcher prefers the specific one. Item numbers are included because they
        link a catalogue or a repeat purchase across documents.
        """
        candidates: List[str] = []
        po = compact_key(keys.get("po_number"))
        po_line = compact_key(keys.get("po_line_number"))
        invoice = compact_key(keys.get("invoice_number"))
        document = compact_key(keys.get("document_number"))
        document_line = compact_key(keys.get("document_line_number"))
        item = compact_key(keys.get("item_number"))

        if po and po_line:
            candidates.append(f"POL:{po}:{po_line}")
        if po:
            candidates.append(f"PO:{po}")
        if invoice and document_line:
            candidates.append(f"INVL:{invoice}:{document_line}")
        if invoice:
            candidates.append(f"INV:{invoice}")
        if document and document_line and document.lower() not in {"NA", "N/A"}:
            candidates.append(f"DOCL:{document}:{document_line}")
        # An item number alone is a weak key: it identifies a product, not a
        # transaction. It is kept last so it only ever supplies context.
        if item and len(item) >= 4:
            candidates.append(f"ITEM:{item}")
        return candidates

    def _extract_texts(self, profile: SourceProfile, resolver: ColumnResolver,
                       row: Sequence[str]) -> Tuple[List[str], List[str], List[str],
                                                    List[str], List[str], bool]:
        """Pull the descriptive, contextual and code values out of one row.

        Description fields are returned in the profile's declared order, which
        encodes which field is the better free-text source for that system. The
        descriptions come back twice: once softened for analysis and once
        exactly as the client wrote them, for the audit trail.

        The final flag says the row did carry free text and all of it was a
        buyer note. Fortum's rule is that such a line is Unclear, so the
        difference between "nothing was written" and "only a note was written"
        has to survive this method.
        """
        descriptions: List[str] = []
        descriptions_raw: List[str] = []
        description_fields: List[str] = []
        note_only = False
        for header in profile.description_fields:
            if is_internal_note_header(header):
                continue
            value = resolver.value_at(row, header)
            if value and not self.lexicon.is_noise(value):
                remainder = strip_non_purchase_text(value)
                if not remainder:
                    note_only = True
                    continue
                descriptions.append(soften_caps(remainder))
                descriptions_raw.append(value)
                description_fields.append(header)
                # One line-level field is the purchase. Concatenating the rest
                # mixes a joined invoice article or a second system's note into
                # the description, which is how Finnish leaked into English rows.
                break

        context: List[str] = []
        for header in profile.context_fields:
            value = resolver.value_at(row, header)
            if value and not self.lexicon.is_noise(value):
                context.append(soften_caps(value))

        codes: List[str] = []
        for header in profile.code_fields:
            value = resolver.value_at(row, header)
            if value and not self.lexicon.is_noise(value):
                codes.append(value)

        return (descriptions, descriptions_raw, description_fields, context,
                codes, note_only and not descriptions)

    def collect(self) -> None:
        """First pass: index phrases, evidence and duplicates."""
        LOGGER.info("Pass 1 of 2: reading input and indexing evidence")

        for table, profile, resolver in self.tables:
            rows_read = 0
            for row_number, row in table.iter_rows():
                rows_read += 1
                descriptions, _, _, context, codes, _ = self._extract_texts(
                    profile, resolver, row)
                primary = descriptions[0] if descriptions else ""
                row_type = classify_row(resolver, row, primary)

                self.statistics[f"rows_{row_type.lower()}"] += 1
                if row_type != ROW_TYPE_LINE:
                    continue

                self.phrase_pool.update(descriptions)
                self.phrase_pool.update(context)

                bundle = EvidenceBundle(
                    descriptions=set(descriptions),
                    context=set(context),
                    codes=set(codes),
                    systems={profile.name},
                )
                supplier = resolver.value(row, "supplier_name")
                if supplier:
                    bundle.suppliers.add(supplier)
                for level in ("category_l1", "category_l2", "category_l3", "category_l4"):
                    value = resolver.value(row, level)
                    if value and not self.lexicon.is_noise(value):
                        bundle.categories.add(value)
                amount = parse_amount(resolver.value(row, "spend"))
                if amount is not None:
                    bundle.amounts.add(round(amount, 2))

                for key in self._join_keys(self._row_keys(resolver, row)):
                    self.evidence_by_key[key].absorb(bundle)

                # Index each description under a blocking key so that the fuzzy
                # tier compares a phrase only against plausible neighbours rather
                # than against every phrase in the data set.
                for description in descriptions:
                    for block in self._blocking_keys(description):
                        self.phrase_index[block].add(description)

            self.statistics[f"rows_read:{profile.name}"] += rows_read
            LOGGER.info("  %-46s %8d row(s)", table.label, rows_read)

        LOGGER.info("Indexed %d distinct phrase(s) and %d join key(s).",
                    len(self.phrase_pool), len(self.evidence_by_key))

    @staticmethod
    def _blocking_keys(text: str) -> List[str]:
        """Cheap keys that group phrases likely to be comparable.

        Blocking converts an intractable all-pairs comparison into a set of
        small ones. The keys are the longest content words in the phrase: two
        descriptions of the same purchase almost always share at least one, and
        two unrelated descriptions almost never do.
        """
        words = [word.lower() for word in tokenise(fold_accents(text))
                 if len(word) >= 5 and not is_code_token(word)]
        return sorted(set(words), key=len, reverse=True)[:3]

    # -- between the passes -------------------------------------------------

    def resolve_language(self) -> None:
        """Translate every distinct phrase once, then index them for retrieval."""
        LOGGER.info("Resolving %d distinct phrase(s) into English ...", len(self.phrase_pool))
        self.translator.prepare(self.phrase_pool)
        for method, count in sorted(self.translator.method_counts.items()):
            LOGGER.info("  %-12s %6d phrase(s)", method, count)
            self.statistics[f"translation_{method}"] = count

        # Built after translation so that the index holds English text. A
        # multilingual model would place the source phrases near each other
        # anyway, but indexing the English keeps this consistent with what the
        # downstream agents embed and makes the neighbours legible in the audit
        # trail.
        self.semantic.build(
            self.translator.results[phrase].english_text
            for phrase in self.phrase_pool if phrase in self.translator.results
        )

    # -- matching -----------------------------------------------------------

    def _match_by_key(self, keys: Dict[str, str]) -> Tuple[EvidenceBundle, str, float]:
        """Look a row up by its business keys, preferring the most specific."""
        merged = EvidenceBundle()
        tier, score = "none", 0.0

        for candidate in self._join_keys(keys):
            bundle = self.evidence_by_key.get(candidate)
            if not bundle:
                continue
            merged.absorb(bundle)
            if tier == "none":
                tier = "key"
                # A line-level key is an exact identification; a header-level or
                # item-level key only places the row in the right neighbourhood.
                score = 1.0 if candidate.startswith(("POL:", "INVL:", "DOCL:")) else 0.8
        return merged, tier, score

    def _match_by_similarity(self, text: str, english_text: str) -> Tuple[Set[str], str, float]:
        """Find comparable phrases when no business key resolved the row.

        Two tiers, tried in order of how much they can be trusted. Lexical
        comparison runs against the *source* text, because the blocking index
        was built from source text and because a lexical match between two
        untranslated strings is the stronger signal. Embedding retrieval runs
        against the *English* text, because that is what the semantic index
        holds, and only when the lexical tier found nothing: it can connect
        phrases with no words in common, which is valuable and correspondingly
        easier to fool.

        The bar is set high on purpose. A false match here attaches another
        purchase's description to this line, which is precisely the invented
        information the specification forbids, so precision is favoured over
        recall throughout.
        """
        if not text:
            return set(), "none", 0.0

        candidates: Set[str] = set()
        for block in self._blocking_keys(text):
            candidates |= self.phrase_index.get(block, set())
        candidates.discard(text)

        if candidates:
            scored = sorted(
                ((text_similarity(lookup_key(text), lookup_key(candidate)), candidate)
                 for candidate in candidates),
                key=lambda item: (-item[0], item[1]),
            )[: self.settings.top_k]

            accepted = {candidate for score, candidate in scored
                        if score >= self.settings.fuzzy_threshold}
            if accepted:
                return accepted, "fuzzy", round(max(score for score, _ in scored), 3)

        neighbours = self.semantic.query(english_text)
        if neighbours:
            return ({phrase for phrase, _ in neighbours}, "semantic",
                    round(max(score for _, score in neighbours), 3))
        return set(), "none", 0.0

    # -- pass two -----------------------------------------------------------

    def enrich_row(self, profile: SourceProfile, resolver: ColumnResolver,
                   table: Table, row_number: int, row: Sequence[str]) -> LineRecord:
        """Build the complete enriched record for one input row."""
        (descriptions, descriptions_raw, description_fields,
         context, codes, note_only) = self._extract_texts(profile, resolver, row)
        primary = descriptions[0] if descriptions else ""
        row_type = classify_row(resolver, row, primary)

        keys = self._row_keys(resolver, row)
        category_values = [resolver.value(row, level)
                           for level in ("category_l1", "category_l2", "category_l3", "category_l4")]

        record = LineRecord(
            row_id=stable_hash(self.run_id, table.label, str(row_number)),
            source_system=profile.name,
            source_file=table.path.name,
            source_sheet=table.sheet,
            source_row=row_number,
            row_type=row_type,
            keys=keys,
            own_descriptions=descriptions,
            own_descriptions_raw=descriptions_raw,
            own_description_fields=description_fields,
            own_context=context,
            own_codes=codes,
            own_text_was_note=note_only,
        )

        record.business = {
            "supplier_id": resolver.value(row, "supplier_id"),
            "supplier_name": resolver.value(row, "supplier_name"),
            "category_l1": category_values[0],
            "category_l2": category_values[1],
            "category_l3": category_values[2],
            "category_l4": category_values[3],
            "category_path": " > ".join(value for value in category_values if value),
            "item_type": resolver.value(row, "item_type"),
            "material_group_number": resolver.value(row, "material_group_number"),
            "material_group_name": resolver.value(row, "material_group_name"),
            "business_area": resolver.value(row, "business_area"),
            "division": resolver.value(row, "division"),
            "company_code": resolver.value(row, "company_code"),
            "company_name": resolver.value(row, "company_name"),
            "country": resolver.value(row, "country"),
            "quantity": resolver.value(row, "quantity"),
            "unit": resolver.value(row, "unit"),
            "unit_price": resolver.value(row, "unit_price"),
            "spend": resolver.value(row, "spend"),
            "currency": resolver.value(row, "currency"),
            "posting_date": parse_date(resolver.value(row, "posting_date")),
        }

        if row_type != ROW_TYPE_LINE:
            return record

        # Duplicate detection ignores the file the row arrived in, so the same
        # invoice delivered twice is recognised as one purchase. The row is
        # flagged rather than removed: the client's row count must be preserved.
        content_signature = stable_hash(
            compact_key(keys.get("po_number")), compact_key(keys.get("po_line_number")),
            compact_key(keys.get("invoice_number")), compact_key(keys.get("document_line_number")),
            lookup_key(primary), lookup_key(record.business["supplier_name"]),
            record.business["spend"],
        )
        if content_signature in self.content_seen:
            record.is_duplicate = True
            record.duplicate_of = self.content_seen[content_signature]
        else:
            self.content_seen[content_signature] = record.row_id

        # Evidence: what this row says, plus what every other system says about
        # the same purchase.
        record.evidence = EvidenceBundle(
            descriptions=set(descriptions), context=set(context),
            codes=set(codes), systems={profile.name},
        )
        matched, tier, score = self._match_by_key(keys)
        if not matched.is_empty():
            record.evidence.absorb(matched)
            record.match_tier, record.match_score = tier, score
        elif not descriptions or all(self.lexicon.is_noise(value) for value in descriptions):
            # Only reach for similarity when the row has nothing of its own to
            # say. A row with a good description does not need a risky match.
            english_primary = self.translator.translate(primary).english_text if primary else ""
            similar, tier, score = self._match_by_similarity(primary, english_primary)
            if similar:
                record.evidence.descriptions |= similar
                record.match_tier, record.match_score = tier, score

        record.matched_systems = tuple(sorted(record.evidence.systems - {profile.name}))
        return record

    def build_description(self, record: LineRecord) -> Tuple[DescriptionResult, TranslationResult]:
        """Translate a record's evidence and compose its description."""
        if record.row_type != ROW_TYPE_LINE:
            return DescriptionResult(), TranslationResult("", "", "und", 0.0, "none", 0.0)

        # Own text first. Borrowed fragments from other systems are offered to
        # the model as extra evidence, not concatenated into the draft: a false
        # join is how a Finnish invoice article used to leak into an English
        # line's enriched description.
        own = [strip_non_purchase_text(value) for value in record.own_descriptions]
        own = [value for value in own if value]
        borrowed = sorted(record.evidence.descriptions - set(record.own_descriptions))
        borrowed = [strip_non_purchase_text(value) for value in borrowed]
        borrowed = [value for value in borrowed if value]
        composing = own[:1] or borrowed[:1]

        english_fragments: List[str] = []
        translations: List[TranslationResult] = []
        for fragment in composing:
            result = self.translator.translate(fragment)
            english_fragments.append(result.english_text)
            translations.append(result)

        extra_fragments = [self.translator.translate(value).english_text
                           for value in borrowed] if own else []
        context_fragments = [self.translator.translate(value).english_text
                             for value in sorted(record.evidence.context)]

        description = self.synthesiser.compose(record, english_fragments, context_fragments)
        extra = " | ".join(fragment for fragment in extra_fragments if fragment)
        source = record.primary_text or (own[0] if own else "")
        # Every line is read by the language model when that tier is on, so the
        # published description is a sentence rather than a leftover fragment.
        if (self.translator.model is not None and self.translator.model.config.enabled
                and (description.description or source)):
            polished, short, item = self.translator.polish_composed(
                source,
                description.description or source, extra,
                description.short_description, description.item_or_service)
            description.description = polished
            description.short_description = short
            description.item_or_service = item or "Unclear"
            if description.item_or_service == "Unclear" and not description.description:
                description.short_description = ""

        description.description = self.translator.ensure_english(description.description)
        description.short_description = self.translator.ensure_english(
            description.short_description)
        if has_non_english(description.description):
            description.description = ""
            description.short_description = ""
            description.item_or_service = "Unclear"
        elif description.description and not description.short_description:
            description.short_description = self.synthesiser._shorten(description.description)
        if description.item_or_service == "Unclear" and not description.description:
            description.short_description = ""

        # The reported translation is the one for the line's own primary text,
        # which is what a reviewer will want to check against the source cell.
        primary_translation = translations[0] if translations else TranslationResult(
            "", "", "und", 0.0, "none", 1.0)
        return description, primary_translation

    def unified_row(self, record: LineRecord, description: DescriptionResult,
                    translation: TranslationResult, confidence: int,
                    band: str, factors: Dict[str, float]) -> Dict[str, Any]:
        """Assemble one row of the unified table."""
        business = record.business
        return {
            "Enriched_Purchase_Description": (
                "" if has_non_english(description.description) else description.description),
            "Enriched_Description_Short": (
                "" if has_non_english(description.short_description)
                else description.short_description),
            "Item_Or_Service": (
                (description.item_or_service or "Unclear")
                if record.row_type == ROW_TYPE_LINE else ""
            ),
            "AI_Confidence": confidence if description.description else "",
            "Confidence_Band": band if description.description else "",

            "Original_Description": record.primary_text,
            "Original_Description_Fields": "; ".join(record.own_description_fields),
            "Detected_Language": translation.language,
            "Language_Confidence": translation.language_confidence,
            "Translated_Description": translation.english_text,
            "Translation_Method": translation.method,
            "Translation_Coverage": round(translation.coverage, 3),
            "Unresolved_Tokens": "; ".join(translation.unresolved),
            "Evidence_Sources": "; ".join(sorted(record.evidence.systems)),
            "Evidence_Field_Count": len(record.evidence.descriptions) + len(record.evidence.context),
            "Match_Tier": record.match_tier,
            "Match_Score": record.match_score,
            "Matched_Source_Systems": "; ".join(record.matched_systems),
            "Confidence_Factors": json.dumps(factors, sort_keys=True) if description.description else "",

            "Source_System": record.source_system,
            "Row_Type": record.row_type,
            "Document_Number": record.keys.get("document_number", ""),
            "Document_Line_Number": record.keys.get("document_line_number", ""),
            "PO_Number": record.keys.get("po_number", ""),
            "PO_Line_Number": record.keys.get("po_line_number", ""),
            "Invoice_Number": record.keys.get("invoice_number", ""),
            "Item_Number": record.keys.get("item_number", ""),
            "Item_Type": business.get("item_type", ""),
            "Supplier_Id": business.get("supplier_id", ""),
            "Supplier_Name": business.get("supplier_name", ""),
            "Category_L1": business.get("category_l1", ""),
            "Category_L2": business.get("category_l2", ""),
            "Category_L3": business.get("category_l3", ""),
            "Category_L4": business.get("category_l4", ""),
            "Material_Group_Number": business.get("material_group_number", ""),
            "Material_Group_Name": business.get("material_group_name", ""),
            "Business_Area": business.get("business_area", ""),
            "Division": business.get("division", ""),
            "Company_Code": business.get("company_code", ""),
            "Company_Name": business.get("company_name", ""),
            "Country": business.get("country", ""),
            "Quantity": business.get("quantity", ""),
            "Unit": business.get("unit", ""),
            "Unit_Price": business.get("unit_price", ""),
            "Spend_EUR": business.get("spend", ""),
            "Currency": business.get("currency", ""),
            "Posting_Date": business.get("posting_date", ""),

            "Is_Duplicate": "Yes" if record.is_duplicate else "No",
            "Duplicate_Of": record.duplicate_of,
            "Source_File": record.source_file,
            "Source_Sheet": record.source_sheet,
            "Source_Row_Number": record.source_row,
            "Row_Id": record.row_id,
            "Run_Id": self.run_id,
            "Lexicon_Version": self.lexicon.version,
            "Agent_Version": AGENT_VERSION,
        }

    def write(self) -> Dict[str, Any]:
        """Second pass: enrich every row and write all output files."""
        LOGGER.info("Pass 2 of 2: enriching rows and writing output")
        results_dir = self.settings.results_dir
        results_dir.mkdir(parents=True, exist_ok=True)

        unified_csv_path = results_dir / "agent1_unified_lines.csv"
        unified_jsonl_path = results_dir / "agent1_unified_lines.jsonl"
        written_files: List[str] = []

        per_source_columns = list(UNIFIED_COLUMNS if self.settings.full_columns
                                  else PER_SOURCE_COLUMNS)
        confidence_totals: List[int] = []
        band_counts: Counter = Counter()

        unified_handle = unified_csv_path.open("w", encoding="utf-8-sig", newline="")
        unified_writer = csv.DictWriter(unified_handle, fieldnames=list(UNIFIED_COLUMNS),
                                        extrasaction="ignore")
        unified_writer.writeheader()

        jsonl_handle = (unified_jsonl_path.open("w", encoding="utf-8")
                        if self.settings.write_jsonl else None)

        try:
            for table, profile, resolver in self.tables:
                output_path = results_dir / f"agent1_{self._output_stem(table, profile)}.csv"
                with output_path.open("w", encoding="utf-8-sig", newline="") as source_handle:
                    source_writer = csv.writer(source_handle)
                    source_writer.writerow(list(table.headers) + per_source_columns)

                    enriched_count = 0
                    for row_number, row in table.iter_rows():
                        record = self.enrich_row(profile, resolver, table, row_number, row)
                        description, translation = self.build_description(record)
                        confidence, band, factors = score_confidence(record, description, translation)

                        unified = self.unified_row(record, description, translation,
                                                   confidence, band, factors)
                        unified_writer.writerow(unified)

                        if jsonl_handle is not None:
                            payload = dict(unified)
                            # The JSONL carries the evidence that the flat table
                            # can only summarise, so that a reviewer can see
                            # every fragment a description was built from.
                            payload["Evidence"] = {
                                "own_descriptions": record.own_descriptions,
                                "own_context": record.own_context,
                                "own_codes": record.own_codes,
                                "pooled_descriptions": sorted(record.evidence.descriptions),
                                "pooled_context": sorted(record.evidence.context),
                                "used_fragments": list(description.used_fragments),
                                "description_basis": description.basis,
                                "specificity": description.specificity,
                            }
                            jsonl_handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

                        source_writer.writerow(
                            list(row) + [unified.get(column, "") for column in per_source_columns])

                        if description.description:
                            enriched_count += 1
                            confidence_totals.append(confidence)
                            band_counts[band] += 1

                self.statistics[f"enriched:{profile.name}"] += enriched_count
                written_files.append(output_path.name)
                LOGGER.info("  %-46s %8d description(s) -> %s",
                            table.label, enriched_count, output_path.name)
        finally:
            unified_handle.close()
            if jsonl_handle is not None:
                jsonl_handle.close()

        written_files.extend([unified_csv_path.name]
                             + ([unified_jsonl_path.name] if self.settings.write_jsonl else []))

        if self.model is not None:
            self.model.save_cache()

        statistics = {name: value for name, value in sorted(self.statistics.items())}
        statistics.update({
            "distinct_phrases": len(self.phrase_pool),
            "join_keys": len(self.evidence_by_key),
            "descriptions_written": len(confidence_totals),
            "mean_confidence": round(sum(confidence_totals) / len(confidence_totals), 1)
                               if confidence_totals else 0.0,
            "confidence_bands": dict(band_counts),
        })
        if self.model is not None:
            statistics["token_usage"] = {
                **self.model.usage.as_dict(),
                **self.model.guard.as_dict(),
            }

        manifest = {
            "agent": AGENT_NAME,
            "agent_version": AGENT_VERSION,
            "run_id": self.run_id,
            "lexicon_version": self.lexicon.version,
            "environment": describe_environment(),
            "configuration": {
                "source_dir": str(self.settings.source_dir),
                "results_dir": str(self.settings.results_dir),
                "neural_translation": self.settings.use_neural_translation,
                "semantic_matching": self.settings.use_semantic_matching,
                "language_model": self.settings.model.enabled,
                "model": self.settings.model.model if self.settings.model.enabled else None,
                "backend": self.settings.model.backend if self.settings.model.enabled else None,
                "fuzzy_threshold": self.settings.fuzzy_threshold,
                "semantic_threshold": self.settings.semantic_threshold,
                "max_words": self.settings.max_words,
            },
            "inputs": dict(sorted(self.input_hashes.items())),
            "sources": [
                {"file": table.path.name, "sheet": table.sheet,
                 "profile": profile.name, "columns": len(table.headers)}
                for table, profile, _ in self.tables
            ],
            "outputs": written_files,
            "statistics": statistics,
        }

        manifest_path = results_dir / "agent1_run_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    @staticmethod
    def _output_stem(table: Table, profile: SourceProfile) -> str:
        """Safe, stable file-name stem for a per-source output file."""
        base = f"{table.path.stem}_{table.sheet}" if table.sheet not in {"", table.path.stem} else table.path.stem
        stem = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_").lower()
        return stem or profile.name

    def run(self) -> Dict[str, Any]:
        """Execute the full pipeline."""
        self.discover()
        self.collect()
        self.resolve_language()
        return self.write()


# ===========================================================================
# Command line interface
# ===========================================================================

BANNER = r"""
===============================================================================
 Fortum AI-Powered Procurement Analysis
 Agent 1 - Improved Purchase Description
 Prof. Shahab Anbarjafari
===============================================================================
""".strip("\n")


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface.

    Interactive prompting is the primary way this agent is run, so every option
    here has a sensible default and the parser exists mainly so that a scheduled
    run can bypass the prompts entirely.
    """
    parser = argparse.ArgumentParser(
        prog="agent1.py",
        description="Agent 1 - generate standardised English purchase descriptions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python agent1.py\n"
            "      Prompt for each path in turn and run with the defaults.\n\n"
            "  python agent1.py --non-interactive --sources ./sources --results ./results\n"
            "      Run unattended with the local NLP stack only.\n\n"
            "  python agent1.py --non-interactive --input ./results/max_stage3_interpreted.csv --use-llm\n"
            "      Run against the Stage 3 master table with the language-model tier.\n"
        ),
    )
    paths = parser.add_argument_group("paths")
    paths.add_argument("--sources", metavar="DIR", help="folder holding the source data")
    paths.add_argument("--input", metavar="PATH",
                       help="Stage 3 master file (or folder) to enrich; overrides --sources")
    paths.add_argument("--results", metavar="DIR", help="folder to write results into")
    paths.add_argument("--lexicon", metavar="FILE", help="controlled vocabulary JSON file")
    paths.add_argument("--cache", metavar="DIR", help="folder for the model response cache")

    tiers = parser.add_argument_group("processing tiers")
    tiers.add_argument("--use-llm", action="store_true",
                       help="enable the language-model tier for unresolved phrases")
    tiers.add_argument("--llm-spend-limit", metavar="USD", type=float, default=None,
                       help="pause and ask once estimated model spend reaches this "
                            f"figure (default {DEFAULT_SPEND_LIMIT:.2f}; 0 disables the alert)")
    tiers.add_argument("--no-neural", action="store_true",
                       help="disable the offline neural translation models")
    tiers.add_argument("--no-semantic", action="store_true",
                       help="disable embedding-based matching")

    tuning = parser.add_argument_group("tuning")
    tuning.add_argument("--fuzzy-threshold", type=float, default=0.86,
                        help="minimum similarity to accept a fuzzy match (default 0.86)")
    tuning.add_argument("--semantic-threshold", type=float, default=0.72,
                        help="minimum cosine similarity for a semantic match (default 0.72)")
    tuning.add_argument("--top-k", type=int, default=5,
                        help="candidate matches retained per line (default 5)")
    tuning.add_argument("--max-words", type=int, default=40,
                        help="word budget for a generated description (default 40)")

    output = parser.add_argument_group("output")
    output.add_argument("--full-columns", action="store_true",
                        help="append the complete audit trail to the per-source files")
    output.add_argument("--no-jsonl", action="store_true", help="skip the JSONL export")

    parser.add_argument("--non-interactive", action="store_true",
                        help="never prompt; use the supplied arguments and defaults")
    parser.add_argument("--verbose", action="store_true", help="emit debug-level logging")
    parser.add_argument("--version", action="version",
                        version=f"{AGENT_NAME} {AGENT_VERSION}")
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
    """Prompt for a value, offering a default that Enter accepts."""
    try:
        answer = input(f"{question}\n  [{default}]: ").strip()
    except EOFError:
        # Reached when input is piped rather than typed; the default is correct.
        return default
    return _clean_path_input(answer) or default


def ask_yes_no(question: str, default: bool) -> bool:
    """Prompt for a yes or no answer."""
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer[0] == "y"


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
    """Combine defaults, command-line arguments and prompts into a settings object."""
    here = Path(__file__).resolve().parent
    default_sources = Path(args.input or args.sources) if (args.input or args.sources) else here / "sources"
    default_results = Path(args.results) if args.results else here / "results"
    default_lexicon = Path(args.lexicon) if args.lexicon else here / "lexicon" / "procurement_lexicon.json"
    default_cache = Path(args.cache) if args.cache else here / "cache"

    use_neural = not args.no_neural
    use_semantic = not args.no_semantic
    use_llm = args.use_llm
    spend_limit = (args.llm_spend_limit if args.llm_spend_limit is not None
                   else _env_float(env.get("LLM_SPEND_LIMIT"), DEFAULT_SPEND_LIMIT))

    if not args.non_interactive:
        print(BANNER)
        print("\nPress Enter to accept the value shown in brackets.\n")
        default_sources = Path(ask(
            "Source data folder or Stage 3 master file", str(default_sources)))
        default_results = Path(ask("Results folder", str(default_results)))
        default_lexicon = Path(ask("Controlled vocabulary file", str(default_lexicon)))
        default_cache = Path(ask("Cache folder", str(default_cache)))

        print()
        if _transformers is None:
            print("  Offline translation models are not installed; that tier will be skipped.")
            use_neural = False
        else:
            use_neural = ask_yes_no(
                "Use the offline neural translation models (recommended, free)?", True)

        if _sentence_transformers is None:
            use_semantic = False
        else:
            use_semantic = ask_yes_no(
                "Use embedding-based matching for unlinked lines?", use_semantic)

        use_llm = ask_yes_no(
            "Use the language model for phrases the local stack cannot resolve?", use_llm)
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
        use_neural_translation=use_neural,
        use_semantic_matching=use_semantic,
        use_llm=use_llm,
        fuzzy_threshold=args.fuzzy_threshold,
        semantic_threshold=args.semantic_threshold,
        top_k=args.top_k,
        max_words=args.max_words,
        full_columns=args.full_columns,
        write_jsonl=not args.no_jsonl,
        verbose=args.verbose,
        interactive=not args.non_interactive,
    )
    settings.model = resolve_model_config(env, use_llm, spend_limit)

    if not settings.source_dir.exists():
        raise SystemExit(f"Source folder does not exist: {settings.source_dir}")
    return settings


def configure_logging(verbose: bool) -> None:
    """Send progress to stdout in a format that reads well in a terminal."""
    configure_process_logging(verbose)


def print_token_usage(statistics: Dict[str, Any], settings: Settings) -> None:
    """Report language-model consumption for the run.

    Printed whenever the tier was enabled, including when it turned out that
    every phrase was served from cache, because "this run cost nothing" is
    exactly as useful a result as a token count.
    """
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
        # Billed as output but never returned in the message, so a report that
        # omitted them would understate the cost of the run.
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
        print("  the remaining lines were processed on the local stack alone.")


def print_summary(manifest: Dict[str, Any], settings: Settings) -> None:
    """Print the closing report."""
    statistics = manifest["statistics"]

    print()
    print("=" * 79)
    print(f"{AGENT_NAME} - complete")
    print("=" * 79)
    print(f"  Run id               : {manifest['run_id']}")
    print(f"  Vocabulary version   : {manifest['lexicon_version']}")
    print(f"  Sources processed    : {len(manifest['sources'])}")
    print(f"  Purchase lines       : {statistics.get('rows_line', 0):,}")

    for row_type in ("header", "subtotal", "total", "empty"):
        count = statistics.get(f"rows_{row_type}", 0)
        if count:
            print(f"  Rows typed {row_type:<10}: {count:,}")

    print(f"  Distinct phrases     : {statistics.get('distinct_phrases', 0):,}")
    print(f"  Descriptions written : {statistics.get('descriptions_written', 0):,}")
    print(f"  Mean confidence      : {statistics.get('mean_confidence', 0)}")

    bands = statistics.get("confidence_bands", {})
    if bands:
        band_summary = "  ".join(f"{name} {count:,}" for name, count in sorted(bands.items()))
        print(f"  Confidence bands     : {band_summary}")

    methods = [(name.replace("translation_", ""), value)
               for name, value in statistics.items() if name.startswith("translation_")]
    if methods:
        print("\n  Phrase resolution by tier")
        for name, value in sorted(methods, key=lambda item: -item[1]):
            print(f"    {name:<14} {value:,}")

    print(f"\n  Output folder        : {settings.results_dir}")
    for name in manifest["outputs"]:
        print(f"    {name}")

    print_token_usage(statistics, settings)
    print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    env = load_dotenv(Path(__file__).resolve().parent / ".env")
    # Real environment variables win over the file, which is what allows a
    # scheduled job to override a developer's local settings.
    env.update({key: value for key, value in os.environ.items() if key in {
        "AZURE_ENABLE", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
        "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_MODEL",
        "BASE_URL", "MODEL_NAME", "LLM_BATCH_SIZE", "LLM_TIMEOUT", "LLM_MAX_REQUESTS",
        "LLM_REASONING_EFFORT",
    }})

    try:
        settings = resolve_settings(args, env)
    except SystemExit as error:
        print(f"\n{error}\n", file=sys.stderr)
        return 2

    available = describe_environment()
    LOGGER.info("Optional components: %s", ", ".join(
        f"{name}={'yes' if present else 'no'}" for name, present in sorted(available.items())))
    if settings.model.enabled:
        LOGGER.info("Language-model tier enabled: %s via %s",
                    settings.model.model, settings.model.backend)
        if settings.model.spend_limit:
            LOGGER.info("Spend alert set at $%.2f; the run will ask before going past it.",
                        settings.model.spend_limit)
        else:
            LOGGER.info("No spend alert set; the model tier will run unmetered.")

    try:
        manifest = Agent1(settings).run()
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
