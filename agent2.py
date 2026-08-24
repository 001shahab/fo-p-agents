#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 2 - AI Purchase Group (Category L5).

Adds an analytical level below the Sievo L1-L4 taxonomy by grouping purchases
that are the same thing described differently.

    Background
    ----------
    Sievo's taxonomy stops at Category L4, which on this data is still broad
    enough to contain thousands of unrelated line items. Below it there is
    nothing but the individual purchase-order text, and that text is written
    freehand by whoever raised the requisition. "Asbestos removal service",
    "Asbestos demolition" and "Asbestipurku" are one purchasing behaviour
    recorded three ways, and no report built on the raw text will ever say so.

    Agent 1 has already reduced all three to the same English description. This
    agent takes that output and folds descriptions that mean the same thing into
    a single named group, producing the Category L5 level that the business
    questions in the planning workbook depend on.

    Approach
    --------
    Grouping happens inside a category bucket, never across one: two purchases
    filed under different L4 categories are different purchases whatever their
    wording, and letting the clustering cross that line would silently
    contradict the client's own taxonomy.

    Within a bucket, work is done on *distinct descriptions* rather than rows,
    and similarity is measured three ways at once - shared lemmas, character
    n-grams and, where the model is available, multilingual sentence embeddings.
    Blending them is what lets the agent see past both spelling variation and
    vocabulary variation without being fooled by either alone.

    Naming
    ------
    A group is named from the words its members actually agree on. Taking the
    tokens that appear in at least half the members of {asbestos removal
    service, asbestos demolition, asbestos removal} leaves "asbestos removal",
    which is exactly the label the specification asks for. No model is needed to
    produce it, the result is identical on every run, and the label is
    guaranteed to consist of words the members really used.

    Stability
    ---------
    The development plan asks whether group labels must survive a re-run. They
    must, so a registry of every signature ever assigned is persisted between
    runs. A description that has been seen before keeps its group and its label
    permanently, even as new data arrives and new groups form around it. Without
    that, next quarter's report would rename half the analysis.

    Output
    ------
    Written to the results folder:

        agent2_purchase_groups.csv      one row per input line, group appended
        agent2_purchase_groups.jsonl    the same rows with grouping evidence
        agent2_group_directory.csv      one row per group, with its members
        agent2_run_manifest.json        configuration, statistics and tokens

Usage:
    python agent2.py

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
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from runtime import (
    DEFAULT_AZURE_MODEL, DEFAULT_OPENAI_MODEL, DEFAULT_REASONING_EFFORT,
    chat_completion_body, configure_process_logging, load_sentence_transformer,
    retry_chat_body,
)

LOGGER = logging.getLogger("agent2")

AGENT_NAME = "Agent 2 - AI Purchase Group (Category L5)"
AGENT_VERSION = "1.2.0"

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))

# The label given to anything that cannot be grouped with confidence. Required
# explicitly by the development plan: "All unclear classifications to 'other'".
OTHER_GROUP_LABEL = "Other"
OTHER_GROUP_ID = "G-OTHER"


# ===========================================================================
# Optional dependencies
# ===========================================================================

def _import_optional(module_path: str) -> Optional[Any]:
    """Import a module by dotted path, returning None when it is unavailable."""
    try:
        return __import__(module_path, fromlist=["_"])
    except Exception:
        return None


_numpy = _import_optional("numpy")
_rapidfuzz = _import_optional("rapidfuzz.fuzz")
_requests = _import_optional("requests")
_spacy = _import_optional("spacy")
_nltk = _import_optional("nltk")
_sentence_transformers = _import_optional("sentence_transformers")
_sklearn_text = _import_optional("sklearn.feature_extraction.text")
_sklearn_cluster = _import_optional("sklearn.cluster")
_openpyxl = _import_optional("openpyxl")


def describe_environment() -> Dict[str, bool]:
    return {
        "numpy": _numpy is not None,
        "scikit-learn": _sklearn_cluster is not None and _sklearn_text is not None,
        "sentence-transformers": _sentence_transformers is not None,
        "spacy": _spacy is not None,
        "nltk": _nltk is not None,
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
    model: str = DEFAULT_OPENAI_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    batch_size: int = 25
    timeout: int = 120
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
    """Everything the pipeline needs, resolved from arguments and prompts."""

    input_path: Path
    results_dir: Path
    lexicon_path: Path
    registry_path: Path
    cache_dir: Path

    use_embeddings: bool = True
    use_llm: bool = False

    # Cosine *distance* at which two descriptions stop being the same purchase.
    # 0.35 was chosen so that wording and word-order variation merge while a
    # genuine change of subject does not; it is the starting point for the
    # adaptive search below rather than a fixed rule.
    distance_threshold: float = 0.35
    adaptive: bool = True
    min_groups_per_category: int = 10
    max_groups_per_category: int = 50

    # Below this, a group's members do not really agree and the group is not
    # trustworthy enough to name.
    min_cohesion: float = 0.45
    max_label_words: int = 5
    bucket_level: str = "auto"          # auto | l4 | l3 | l2 | l1
    max_bucket_size: int = 6000

    # Fortum's ceiling on the number of Category L5 names, counting "Other" as
    # one of them. Everything below the line is folded into "Other" rather than
    # dropped, so no row leaves the analysis.
    max_total_groups: int = 6000

    write_jsonl: bool = True
    verbose: bool = False

    # False under --non-interactive, where nothing may block waiting for input.
    interactive: bool = True

    model: ModelConfig = field(default_factory=ModelConfig)


# ---------------------------------------------------------------------------
# Environment handling
# ---------------------------------------------------------------------------

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
    """Select the language-model backend. Mirrors Agent 1 exactly."""
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
        config.api_key = (env.get("AZURE_OPENAI_API_KEY") or env.get("AZURE_API_KEY")
                          or env.get("OPENAI_API_KEY") or "")
        config.base_url = (env.get("AZURE_OPENAI_BASE_URL") or env.get("AZURE_BASE_URL")
                           or env.get("BASE_URL")
                           or "https://genai-sharedservice-emea.pwcinternal.com/v1/chat/completions")
        config.model = env.get("AZURE_OPENAI_MODEL") or env.get("MODEL_NAME") or DEFAULT_AZURE_MODEL
        config.reasoning_effort = (
            env.get("LLM_REASONING_EFFORT") or DEFAULT_REASONING_EFFORT).strip().lower()
    else:
        config.backend = "openai"
        config.api_key = env.get("OPENAI_API_KEY", "")
        # Never inherits BASE_URL; see the note in .env.example.
        config.base_url = env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        config.model = env.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
        config.reasoning_effort = (
            env.get("LLM_REASONING_EFFORT") or DEFAULT_REASONING_EFFORT).strip().lower()

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
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def normalise_text(value: Any) -> str:
    """Reduce any value to clean, single-spaced text."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_CHARS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def fold_accents(text: str) -> str:
    """Strip diacritics for comparison purposes only."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return (stripped.replace("ł", "l").replace("Ł", "L")
            .replace("ø", "o").replace("Ø", "O")
            .replace("æ", "ae").replace("ß", "ss"))


def lookup_key(text: str) -> str:
    """Canonical comparison key."""
    return _WHITESPACE.sub(" ", fold_accents(text).lower()).strip()


def tokenise(text: str) -> List[str]:
    return _TOKEN.findall(text)


def is_code_token(token: str) -> bool:
    """True for tokens that identify rather than describe."""
    if not token or len(token) <= 2:
        return True
    if any(ch.isdigit() for ch in token):
        return True
    return token.isupper() and len(token) >= 4


def sentence_case(text: str) -> str:
    text = text.strip()
    return text[0].upper() + text[1:] if text else ""


def stable_hash(*parts: str) -> str:
    """Short, content-derived identifier, stable across runs and machines."""
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
    """Normalised similarity in ``[0, 1]`` between two short strings."""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if _rapidfuzz is not None:
        return float(_rapidfuzz.token_set_ratio(left, right)) / 100.0
    from difflib import SequenceMatcher
    return SequenceMatcher(None, left, right).ratio()


# ===========================================================================
# Input
# ===========================================================================
#
# The expected input is the unified table written by Agent 1, but the reader is
# tolerant: the agent will run against any table that carries an enriched
# description and a category, so a hand-corrected extract or a re-exported sheet
# works without modification.

REQUIRED_COLUMN = "Enriched_Purchase_Description"

# Columns consulted when they are present. The development plan lists these as
# supporting evidence for grouping, and each one that is populated sharpens the
# signature the clustering works from.
SUPPORTING_COLUMNS = (
    "Enriched_Description_Short", "Item_Or_Service", "Material_Group_Name",
    "Material_Group_Number", "Item_Number", "Original_Description",
)

CATEGORY_COLUMNS = ("Category_L1", "Category_L2", "Category_L3", "Category_L4")


@dataclass
class InputTable:
    """A table of rows held as dictionaries, plus its original column order."""

    headers: List[str]
    rows: List[Dict[str, str]]
    path: Path


def _read_csv(path: Path) -> InputTable:
    """Read a delimited file, detecting encoding and delimiter."""
    encodings = ("utf-8-sig", "utf-8", "cp1252", "cp1250", "latin-1")
    encoding = "latin-1"
    sample_bytes = path.open("rb").read(1 << 18)
    for candidate in encodings:
        try:
            sample_bytes.decode(candidate)
        except UnicodeDecodeError:
            continue
        encoding = candidate
        break

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


def _read_xlsx(path: Path) -> InputTable:
    """Read the first worksheet of a workbook."""
    if _openpyxl is None:
        raise SystemExit(
            f"Reading {path.name} needs openpyxl. Install it with: pip install openpyxl")
    workbook = _openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        worksheet = workbook[workbook.sheetnames[0]]
        iterator = worksheet.iter_rows(values_only=True)
        headers = [normalise_text(cell) for cell in next(iterator, ())]
        rows = [dict(zip(headers, (normalise_text(cell) for cell in values)))
                for values in iterator]
    finally:
        workbook.close()
    return InputTable(headers=headers, rows=rows, path=path)


def read_input(path: Path) -> InputTable:
    """Load the agent's input table and confirm it carries what is needed."""
    if not path.is_file():
        raise SystemExit(
            f"Input file not found: {path}\n"
            "Agent 2 consumes the unified table written by Agent 1. Run agent1.py first, "
            "or point --input at an equivalent table.")

    table = (_read_csv(path) if path.suffix.lower() in {".csv", ".tsv", ".txt"}
             else _read_xlsx(path))

    if REQUIRED_COLUMN not in table.headers:
        raise SystemExit(
            f"{path.name} has no '{REQUIRED_COLUMN}' column.\n"
            "That column is produced by Agent 1 and is the input this agent groups on.")
    LOGGER.info("Loaded %d row(s) and %d column(s) from %s",
                len(table.rows), len(table.headers), path.name)
    return table


# ===========================================================================
# Controlled vocabulary
# ===========================================================================

class Lexicon:
    """The shared procurement vocabulary.

    Agent 2 uses far less of it than Agent 1 - it works on text that is already
    English - but the marker lists and the noise terms are the same knowledge and
    are worth reading from the same file rather than restating here.
    """

    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        payload = payload or {}
        self.version: str = str(payload.get("version", "0.0.0"))
        self.service_markers: Set[str] = {lookup_key(t) for t in payload.get("service_markers", [])}
        self.material_markers: Set[str] = {lookup_key(t) for t in payload.get("material_markers", [])}
        self.noise_terms: Set[str] = {lookup_key(t) for t in payload.get("noise_terms", [])}

    @classmethod
    def load(cls, path: Path) -> "Lexicon":
        if not path.is_file():
            LOGGER.warning("Vocabulary file %s not found; running without it.", path)
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            LOGGER.error("Vocabulary file %s could not be read (%s).", path, error)
            return cls()
        return cls(payload)

    def is_noise(self, text: str) -> bool:
        key = lookup_key(text)
        if not key or key in self.noise_terms:
            return True
        return not any(ch.isalpha() for ch in key)


# ===========================================================================
# Signatures
# ===========================================================================

class SignatureBuilder:
    """Reduces a description to the set of concepts it contains.

    The signature is the backbone of the whole agent. Two descriptions with the
    same signature are treated as the same purchase without any similarity
    calculation at all, which handles the large majority of cases for free and
    perfectly deterministically. Only descriptions with *different* signatures
    ever reach the clustering.

    Lemmatisation matters here more than it looks: "valve", "valves" and
    "valve's" must collapse, or the group directory fills with near-duplicates
    that a human reviewer will immediately and rightly complain about.
    """

    _EXTRA_STOPWORDS = {
        "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at", "by",
        "with", "from", "as", "is", "are", "was", "were", "be", "per", "pcs",
        "each", "new", "used", "other", "misc", "various", "general", "total",
        "no", "not", "incl", "excl", "etc", "pc", "set", "item", "items",
    }

    def __init__(self) -> None:
        self._pipeline: Optional[Any] = None
        self._lemmatiser: Optional[Any] = None
        self._loaded = False
        self.stopwords = self._load_stopwords()
        self._cache: Dict[str, Tuple[str, ...]] = {}

    def _load_stopwords(self) -> Set[str]:
        words = set(self._EXTRA_STOPWORDS)
        if _nltk is not None:
            try:
                from nltk.corpus import stopwords
                words |= set(stopwords.words("english"))
            except Exception:
                pass
        return words

    def _load_backend(self) -> None:
        """Choose the best available lemmatiser once."""
        if self._loaded:
            return
        self._loaded = True

        if _spacy is not None:
            for model_name in ("en_core_web_sm", "en_core_web_md"):
                try:
                    # Only the tagger and lemmatiser are needed; disabling the
                    # rest roughly triples throughput on a large vocabulary.
                    self._pipeline = _spacy.load(
                        model_name, disable=["ner", "parser", "textcat"])
                    LOGGER.info("Lemmatising with spaCy %s.", model_name)
                    return
                except Exception:
                    continue

        if _nltk is not None:
            try:
                from nltk.stem import WordNetLemmatizer
                lemmatiser = WordNetLemmatizer()
                lemmatiser.lemmatize("valves")  # forces the corpus check to happen now
                self._lemmatiser = lemmatiser
                LOGGER.info("Lemmatising with the NLTK WordNet lemmatiser.")
                return
            except Exception:
                pass

        LOGGER.info("No lemmatiser available; using the built-in suffix rules.")

    # Endings that look like a plural but are not. "asbestos" and "analysis"
    # are the ones that actually occur in this data, and stemming either of them
    # produces a token that matches nothing and names a group badly.
    _NOT_PLURAL_ENDINGS = ("ss", "us", "is", "os", "ys")

    # Endings where the plural is formed by adding "es" rather than "s", so the
    # whole "es" has to come off to recover the singular.
    _ES_PLURAL_ENDINGS = ("sses", "shes", "ches", "xes", "zes")

    @classmethod
    def _suffix_lemma(cls, token: str) -> str:
        """Minimal English lemmatiser used when no model is installed.

        Deliberately conservative, and noticeably more so than a Porter or
        Snowball stemmer would be. Over-stemming merges procurement terms that
        are genuinely different, and an analyst who finds two unrelated materials
        sharing a group loses confidence in the whole output; a stray plural
        costs far less. Every rule here is guarded so that it removes an ending
        only when a plausible word is left behind.
        """
        if len(token) <= 3:
            return token

        if token.endswith("ies") and len(token) > 4:
            return token[:-3] + "y"

        for ending in cls._ES_PLURAL_ENDINGS:
            if token.endswith(ending) and len(token) > len(ending) + 1:
                return token[:-2]

        if token.endswith(cls._NOT_PLURAL_ENDINGS):
            return token

        # A trailing "s" after a vowel-consonant-"e" is an ordinary plural:
        # "valves" is "valve", not "valf". The dedicated "ves" to "f" rule that
        # recovers "leaf" from "leaves" is omitted on purpose, because in this
        # domain it fires on valves and sleeves and gets both wrong.
        if token.endswith("s"):
            return token[:-1]
        return token

    # How a purchase was fulfilled rather than what was purchased. Fortum asked
    # that "Bat survey wind site delivery" group with "Bat survey wind site"
    # instead of becoming a category of its own, so these words are removed
    # before a signature is taken and therefore never split a group or reach a
    # label. They are removed only while something else remains: a line whose
    # only content word is "delivery" really did buy a delivery.
    _FULFILMENT_LEMMAS = frozenset({
        "delivery", "deliveries", "deliver", "delivered", "shipment",
        "shipping", "dispatch", "use", "usage", "replacement", "standard",
        "inclusive", "included",
    })

    # Fulfilment words that are only noise when they lead. "Supply of gaskets"
    # is a gasket purchase, but a "power supply" is a component, so this set is
    # stripped from the front of a description and nowhere else.
    _LEADING_FULFILMENT_LEMMAS = frozenset({"supply", "supplies", "provision"})

    @classmethod
    def drop_fulfilment(cls, lemmas: Sequence[str]) -> Tuple[str, ...]:
        """Remove words that describe fulfilment rather than the purchase."""
        kept = [lemma for lemma in lemmas if lemma not in cls._FULFILMENT_LEMMAS]
        if not kept:
            return tuple(lemmas)
        while len(kept) > 1 and kept[0] in cls._LEADING_FULFILMENT_LEMMAS:
            kept = kept[1:]
        return tuple(kept)

    def lemmas(self, text: str) -> Tuple[str, ...]:
        """Content lemmas of a description, in the order they appear.

        Order is preserved because the labeller needs it to reconstruct a
        readable name; the clustering itself ignores it.
        """
        key = lookup_key(text)
        if key in self._cache:
            return self._cache[key]
        if not key:
            return ()

        self._load_backend()
        tokens = [token for token in tokenise(key) if not is_code_token(token)]
        tokens = [token for token in tokens if token not in self.stopwords]

        if self._pipeline is not None:
            try:
                document = self._pipeline(" ".join(tokens))
                lemmas = [token.lemma_.lower() for token in document
                          if token.lemma_ and not token.is_punct]
            except Exception:
                lemmas = [self._suffix_lemma(token) for token in tokens]
        elif self._lemmatiser is not None:
            lemmas = [self._lemmatiser.lemmatize(token) for token in tokens]
        else:
            lemmas = [self._suffix_lemma(token) for token in tokens]

        lemmas = [lemma for lemma in lemmas
                  if lemma and lemma not in self.stopwords and len(lemma) > 2]

        # Deduplicate while preserving first appearance: a description that
        # repeats a word says no more than one that states it once.
        result = self.drop_fulfilment(list(dict.fromkeys(lemmas)))
        self._cache[key] = result
        return result

    def signature(self, text: str) -> str:
        """Order-independent identity of a description.

        Sorted, so that "removal of asbestos" and "asbestos removal" produce the
        same signature and are grouped without any similarity computation.
        """
        lemmas = self.lemmas(text)
        return "|".join(sorted(lemmas))


# ===========================================================================
# Similarity and clustering
# ===========================================================================

class SimilarityModel:
    """Blended similarity over a set of distinct descriptions.

    Three views of the same text, combined:

        lexical   TF-IDF over word unigrams and bigrams. Precise when two
                  descriptions genuinely share vocabulary.
        character TF-IDF over character 3- to 5-grams. Survives the spelling
                  variation, abbreviation and residual foreign-language spelling
                  that the word view cannot see past.
        semantic  Multilingual sentence embeddings. Connects descriptions with
                  no shared substring at all, which is the case the other two
                  are structurally unable to handle.

    They are blended rather than used in sequence because each is wrong in a
    different way, and the average of three partially wrong views is markedly
    better calibrated than any of them alone. The weights favour the lexical
    views: on text that Agent 1 has already normalised, shared vocabulary is
    strong evidence, and the embedding is there to rescue what the others miss
    rather than to overrule them.
    """

    LEXICAL_WEIGHT = 0.40
    CHARACTER_WEIGHT = 0.30
    SEMANTIC_WEIGHT = 0.30
    EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    def __init__(self, use_embeddings: bool = True) -> None:
        self.use_embeddings = use_embeddings and _sentence_transformers is not None and _numpy is not None
        self._embedder: Optional[Any] = None

    def _load_embedder(self) -> Optional[Any]:
        if not self.use_embeddings:
            return None
        if self._embedder is None:
            try:
                LOGGER.info("Loading embedding model %s ...", self.EMBEDDING_MODEL)
                self._embedder = load_sentence_transformer(
                    _sentence_transformers, self.EMBEDDING_MODEL)
            except Exception as error:
                LOGGER.warning("Embeddings unavailable (%s); continuing without them.", error)
                self.use_embeddings = False
        return self._embedder

    def similarity_matrix(self, texts: Sequence[str]) -> Any:
        """Dense square matrix of blended similarities in ``[0, 1]``."""
        count = len(texts)
        if _numpy is None:
            return self._pairwise_fallback(texts)

        matrices: List[Tuple[Any, float]] = []

        if _sklearn_text is not None:
            word_matrix = self._tfidf_similarity(texts, analyzer="word", ngram_range=(1, 2))
            if word_matrix is not None:
                matrices.append((word_matrix, self.LEXICAL_WEIGHT))
            char_matrix = self._tfidf_similarity(texts, analyzer="char_wb", ngram_range=(3, 5))
            if char_matrix is not None:
                matrices.append((char_matrix, self.CHARACTER_WEIGHT))

        embedder = self._load_embedder()
        if embedder is not None:
            try:
                vectors = embedder.encode(list(texts), batch_size=64, convert_to_numpy=True,
                                          normalize_embeddings=True, show_progress_bar=False)
                matrices.append((vectors @ vectors.T, self.SEMANTIC_WEIGHT))
            except Exception as error:
                LOGGER.warning("Embedding pass failed (%s).", error)

        if not matrices:
            return self._pairwise_fallback(texts)

        # Re-normalise so the blend still spans [0, 1] when a view is missing.
        total_weight = sum(weight for _, weight in matrices)
        blended = _numpy.zeros((count, count), dtype="float32")
        for matrix, weight in matrices:
            blended += matrix.astype("float32") * (weight / total_weight)
        return _numpy.clip(blended, 0.0, 1.0)

    @staticmethod
    def _tfidf_similarity(texts: Sequence[str], analyzer: str,
                          ngram_range: Tuple[int, int]) -> Optional[Any]:
        """Cosine similarity of TF-IDF vectors under one analyser."""
        try:
            vectoriser = _sklearn_text.TfidfVectorizer(
                analyzer=analyzer, ngram_range=ngram_range,
                lowercase=True, sublinear_tf=True, min_df=1,
            )
            matrix = vectoriser.fit_transform(texts)
        except ValueError:
            # Raised when every document is empty after tokenisation.
            return None
        # TfidfVectorizer L2-normalises its rows, so the inner product is the
        # cosine similarity directly.
        return (matrix @ matrix.T).toarray()

    @staticmethod
    def _pairwise_fallback(texts: Sequence[str]) -> Any:
        """All-pairs similarity without numpy or scikit-learn."""
        count = len(texts)
        keys = [lookup_key(text) for text in texts]
        matrix = [[0.0] * count for _ in range(count)]
        for i in range(count):
            matrix[i][i] = 1.0
            for j in range(i + 1, count):
                score = text_similarity(keys[i], keys[j])
                matrix[i][j] = matrix[j][i] = score
        return matrix


def agglomerative_labels(similarity: Any, distance_threshold: float) -> List[int]:
    """Cluster by average linkage, cutting at a distance threshold.

    Average linkage rather than single linkage because single linkage chains:
    one description that happens to sit between two unrelated groups drags them
    into one, and on procurement text that happens constantly. Ward is
    unavailable here because the input is a precomputed distance matrix.

    The number of clusters is not fixed in advance. That is the point - how many
    distinct things a category contains is exactly what the client said they do
    not know and want the data to tell them.
    """
    count = len(similarity)
    if count == 0:
        return []
    if count == 1:
        return [0]

    if _sklearn_cluster is not None and _numpy is not None:
        distances = _numpy.clip(1.0 - _numpy.asarray(similarity, dtype="float64"), 0.0, 1.0)
        # Force exact symmetry and a zero diagonal. Floating-point drift in the
        # blend leaves tiny asymmetries that scikit-learn rejects outright.
        distances = (distances + distances.T) / 2.0
        _numpy.fill_diagonal(distances, 0.0)
        try:
            model = _sklearn_cluster.AgglomerativeClustering(
                n_clusters=None, distance_threshold=distance_threshold,
                metric="precomputed", linkage="average",
            )
            return [int(label) for label in model.fit_predict(distances)]
        except TypeError:
            # scikit-learn renamed `affinity` to `metric` in 1.2.
            model = _sklearn_cluster.AgglomerativeClustering(
                n_clusters=None, distance_threshold=distance_threshold,
                affinity="precomputed", linkage="average",
            )
            return [int(label) for label in model.fit_predict(distances)]
        except Exception as error:
            LOGGER.debug("scikit-learn clustering failed (%s); using the built-in.", error)

    return _average_linkage_fallback(similarity, distance_threshold)


def _average_linkage_fallback(similarity: Any, distance_threshold: float) -> List[int]:
    """Average-linkage agglomerative clustering, implemented directly.

    Used when scikit-learn is not installed. O(n^3) in the worst case, which is
    acceptable only because it runs over the *distinct descriptions* of a single
    category bucket rather than over rows; the bucket size cap in Settings keeps
    it bounded. Merge order is fully determined by the similarity values and, on
    ties, by index, so the result does not depend on iteration order.
    """
    count = len(similarity)
    clusters: Dict[int, List[int]] = {index: [index] for index in range(count)}
    threshold_similarity = 1.0 - distance_threshold

    def cluster_similarity(left: List[int], right: List[int]) -> float:
        total = sum(similarity[i][j] for i in left for j in right)
        return total / (len(left) * len(right))

    while len(clusters) > 1:
        best_pair, best_score = None, threshold_similarity
        keys = sorted(clusters)
        for position, left_key in enumerate(keys):
            for right_key in keys[position + 1:]:
                score = cluster_similarity(clusters[left_key], clusters[right_key])
                if score > best_score:
                    best_pair, best_score = (left_key, right_key), score
        if best_pair is None:
            break
        left_key, right_key = best_pair
        clusters[left_key].extend(clusters.pop(right_key))

    labels = [0] * count
    for label, key in enumerate(sorted(clusters)):
        for index in clusters[key]:
            labels[index] = label
    return labels


def cohesion(similarity: Any, members: Sequence[int]) -> float:
    """Mean pairwise similarity within a cluster.

    Used both to decide whether a group is trustworthy enough to name and to
    derive its confidence. A single-member cluster is given the neutral value
    0.5: it is not incoherent, but nothing corroborates it either.
    """
    if len(members) <= 1:
        return 0.5
    total, pairs = 0.0, 0
    for position, left in enumerate(members):
        for right in members[position + 1:]:
            total += float(similarity[left][right])
            pairs += 1
    return total / pairs if pairs else 0.5


# ===========================================================================
# Group naming
# ===========================================================================

class GroupLabeller:
    """Derives a short standardised English name for a cluster.

    The rule is consensus: keep the lemmas that at least half the members use,
    and present them in the order the members most often put them in. Applied to
    {asbestos removal service, asbestos demolition, asbestos removal} this keeps
    "asbestos" (in all three) and "removal" (in two of three) and drops "service"
    and "demolition" (one each), producing "Asbestos removal" - the label the
    specification asks for, with no model involved.

    Two properties matter more than elegance here. The label is composed only of
    words the members actually used, so it cannot describe something that was
    never purchased. And it is a pure function of the cluster, so it is identical
    on every run over the same data.
    """

    # A lemma has to be used by a strict majority of the members to enter the
    # label. At exactly one half, a two-member cluster would admit every word
    # either member used and the label would just be both descriptions joined.
    CONSENSUS_SHARE = 0.5

    # A label of one word is usually too coarse to be an analysis level:
    # "Cleaning" tells a category manager nothing that Category L4 did not
    # already. Where the members offer a distinguishing word, one is added.
    MIN_LABEL_WORDS = 2

    def __init__(self, builder: SignatureBuilder, lexicon: Lexicon, max_words: int = 5) -> None:
        self.builder = builder
        self.lexicon = lexicon
        self.max_words = max_words

    def label(self, descriptions: Sequence[str], weights: Optional[Sequence[float]] = None) -> str:
        """Name a cluster from its member descriptions.

        ``weights`` are line counts, so that a description used on ten thousand
        rows counts for more than one used once. Without that, a single stray
        wording can name a group that it barely belongs to.
        """
        if not descriptions:
            return ""
        weights = list(weights) if weights else [1.0] * len(descriptions)
        total_weight = sum(weights) or 1.0

        # Weighted document frequency of each lemma, and the average position it
        # occupies, which is what restores a readable word order at the end.
        frequency: Dict[str, float] = defaultdict(float)
        position_total: Dict[str, float] = defaultdict(float)
        position_count: Dict[str, float] = defaultdict(float)

        for description, weight in zip(descriptions, weights):
            lemmas = self.builder.lemmas(description)
            for index, lemma in enumerate(lemmas):
                frequency[lemma] += weight
                position_total[lemma] += index * weight
                position_count[lemma] += weight

        if not frequency:
            return ""

        # Lemmas ranked by how much of the cluster uses them. Ties are broken
        # alphabetically so that the outcome cannot depend on iteration order.
        ranked = sorted(frequency.items(), key=lambda item: (-item[1], item[0]))
        consensus = [lemma for lemma, weight in ranked
                     if weight / total_weight > self.CONSENSUS_SHARE]

        # Nothing is agreed on by a majority, which happens in a loose cluster.
        # Fall back to the most common lemmas so that the group is still named
        # from its own members rather than given a generic label.
        if not consensus:
            consensus = [lemma for lemma, _ in ranked[:self.MIN_LABEL_WORDS]]

        # A single agreed word is usually the head noun with every distinguishing
        # modifier voted away: {tank cleaning, reactor cleaning, industrial
        # cleaning} agrees only on "cleaning". Promoting the strongest remaining
        # modifier recovers "Industrial cleaning", which is the level of
        # specificity the specification asks for. The added word is always one a
        # member actually used.
        if len(consensus) < self.MIN_LABEL_WORDS:
            already = set(consensus)
            for lemma, _ in ranked:
                if lemma in already:
                    continue
                consensus.append(lemma)
                if len(consensus) >= self.MIN_LABEL_WORDS:
                    break

        ordered = sorted(
            consensus,
            key=lambda lemma: (position_total[lemma] / max(position_count[lemma], 1e-9),
                               -frequency[lemma], lemma),
        )[: self.max_words]

        label = " ".join(ordered).strip()
        if not label or self.lexicon.is_noise(label):
            return ""
        return sentence_case(label)

    def is_weak(self, label: str) -> bool:
        """Whether a label is too vague to be useful as an analysis level.

        A group called "Service" or "Material" tells a category manager nothing
        they did not already know, so those are candidates for the optional
        naming pass rather than acceptable answers.
        """
        if not label:
            return True
        words = [word for word in tokenise(lookup_key(label)) if word not in self.builder.stopwords]
        if not words:
            return True
        generic = {"service", "services", "material", "materials", "equipment",
                   "supply", "supplies", "work", "works", "product", "products",
                   "goods", "item", "items", "other", "various", "general", "misc"}
        return all(word in generic for word in words)


# ===========================================================================
# Group registry
# ===========================================================================

class GroupRegistry:
    """Remembers which group every description signature was assigned to.

    This is what makes the answer stable over time. Column L7 of the development
    plan asks whether labels must survive a re-run; they must, because a spend
    report whose analysis level is renamed every quarter cannot be compared with
    itself, and the requirement in row 6 that a user "be able to find the same
    materials again" applies just as much here as it does to Agent 1.

    A signature that has been seen before is assigned to its existing group
    without any clustering, and that group keeps the name it was first given.
    Only genuinely new signatures are clustered, and they join an existing group
    when they are close enough to it. The consequence is that groups accrete
    rather than churn: adding a quarter of new data does not reshuffle the
    previous year's analysis.
    """

    FORMAT_VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        self.groups: Dict[str, Dict[str, Any]] = {}
        self.signature_index: Dict[str, str] = {}
        self.loaded_group_count = 0
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            LOGGER.info("No group registry at %s; this run will establish one.", self.path)
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            LOGGER.warning("Group registry %s unreadable (%s); starting fresh.", self.path, error)
            return
        if int(payload.get("format_version", 0)) != self.FORMAT_VERSION:
            LOGGER.warning("Group registry format has changed; starting fresh.")
            return

        self.groups = payload.get("groups", {}) or {}
        self.signature_index = payload.get("signature_index", {}) or {}
        self.loaded_group_count = len(self.groups)
        LOGGER.info("Group registry loaded: %d group(s), %d known signature(s).",
                    len(self.groups), len(self.signature_index))

    def lookup(self, signature: str) -> Optional[str]:
        """Group identifier previously assigned to a signature, if any."""
        return self.signature_index.get(signature)

    def label_of(self, group_id: str) -> str:
        return str(self.groups.get(group_id, {}).get("label", ""))

    def bucket_of(self, group_id: str) -> str:
        return str(self.groups.get(group_id, {}).get("category_bucket", ""))

    def register(self, group_id: str, label: str, bucket: str, signatures: Iterable[str]) -> None:
        """Record a group and bind its signatures to it.

        An existing group keeps its original label. That is the whole purpose of
        the registry, so the label is deliberately not refreshed even when a new
        run would have named the group differently.
        """
        entry = self.groups.setdefault(group_id, {
            "label": label,
            "category_bucket": bucket,
            "first_seen_run": "",
            "signatures": [],
        })
        known = set(entry.get("signatures", []))
        for signature in signatures:
            known.add(signature)
            self.signature_index[signature] = group_id
        entry["signatures"] = sorted(known)
        entry["signature_count"] = len(known)

    def stamp_run(self, group_id: str, run_id: str) -> None:
        """Record which run first created a group."""
        entry = self.groups.get(group_id)
        if entry is not None and not entry.get("first_seen_run"):
            entry["first_seen_run"] = run_id

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": self.FORMAT_VERSION,
            "agent": AGENT_NAME,
            "agent_version": AGENT_VERSION,
            "note": (
                "Binds description signatures to purchase groups so that labels stay "
                "stable across runs. Preserve this file between runs: deleting it "
                "re-derives every group from scratch and existing group identifiers "
                "will not be reproduced."
            ),
            "groups": {key: self.groups[key] for key in sorted(self.groups)},
            "signature_index": {key: self.signature_index[key]
                                for key in sorted(self.signature_index)},
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        LOGGER.info("Group registry saved: %d group(s), %d signature(s).",
                    len(self.groups), len(self.signature_index))


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
        self._reasoning_style = "effort"

    def _load_cache(self) -> Dict[str, str]:
        if not self.cache_path.is_file():
            return {}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload.get("entries", {})

    def save_cache(self) -> None:
        if not self._cache_dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps({
            "agent": AGENT_NAME,
            "model": self.config.model,
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
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
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
            retry = retry_chat_body(status, text, body)
            if retry is not None:
                if "temperature" in body and "temperature" not in retry:
                    self._omit_temperature = True
                if "reasoning_effort" in body and "reasoning_effort" not in retry:
                    self._reasoning_style = "nested" if "reasoning" in retry else "omit"
                elif "reasoning" in body and "reasoning" not in retry:
                    self._reasoning_style = "omit"
                return self._post(retry)
            LOGGER.warning("Language-model request returned HTTP %s: %s", status, summary)
            self.usage.failed_requests += 1
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            self.usage.failed_requests += 1
            return None

    def complete_json(self, system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
        """Request a JSON object; None on any failure."""
        if not self.config.enabled:
            return None
        if self.config.max_requests and self.usage.requests >= self.config.max_requests:
            LOGGER.warning("Language-model request cap (%d) reached.", self.config.max_requests)
            return None

        body: Dict[str, Any] = chat_completion_body(
            self.config.model, system_prompt, user_prompt,
            omit_temperature=self._omit_temperature,
            reasoning_effort=getattr(self.config, "reasoning_effort", DEFAULT_REASONING_EFFORT),
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
# Record model
# ===========================================================================

@dataclass
class DescriptionEntry:
    """One distinct enriched description within one category bucket.

    Clustering operates on these, not on rows. On a million-line extract the
    number of distinct descriptions inside a single L4 category is typically in
    the hundreds, which is what makes an O(n^2) similarity matrix per bucket a
    reasonable thing to compute.
    """

    text: str
    signature: str
    bucket: str
    row_count: int = 0
    spend: float = 0.0
    supporting: Counter = field(default_factory=Counter)
    confidence_total: float = 0.0

    @property
    def mean_confidence(self) -> float:
        return self.confidence_total / self.row_count if self.row_count else 0.0


@dataclass
class PurchaseGroup:
    """A named Category L5 group."""

    group_id: str
    label: str
    bucket: str
    signatures: Set[str] = field(default_factory=set)
    descriptions: List[str] = field(default_factory=list)
    row_count: int = 0
    spend: float = 0.0
    cohesion: float = 0.0
    is_new: bool = True
    naming_method: str = "consensus"     # consensus | registry | model | fallback


# ===========================================================================
# Pipeline
# ===========================================================================

class Agent2:
    """Orchestrates the run.

    The sequence is: read Agent 1's table, reduce it to distinct descriptions
    per category bucket, assign every description the registry already knows,
    cluster whatever is left, name the new clusters, then write every input row
    back out with its group attached.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lexicon = Lexicon.load(settings.lexicon_path)
        self.builder = SignatureBuilder()
        self.similarity = SimilarityModel(use_embeddings=settings.use_embeddings)
        self.labeller = GroupLabeller(self.builder, self.lexicon, settings.max_label_words)
        self.registry = GroupRegistry(settings.registry_path)

        self.model: Optional[LanguageModelClient] = None
        if settings.model.enabled:
            self.model = LanguageModelClient(
                settings.model, settings.cache_dir / "agent2_model_cache.json",
                interactive=settings.interactive)

        self.table: Optional[InputTable] = None
        self.run_id = ""
        self.entries: Dict[Tuple[str, str], DescriptionEntry] = {}
        self.groups: Dict[str, PurchaseGroup] = {}
        self.signature_to_group: Dict[str, str] = {}
        self.statistics: Counter = Counter()

    # -- bucketing ----------------------------------------------------------

    def _category_bucket(self, row: Dict[str, str]) -> str:
        """The category a description is grouped within.

        Groups sit below the Sievo taxonomy, so a bucket is a category path.
        Which level is used depends on what the row actually has: falling back
        to a shallower level keeps rows with incomplete categorisation in the
        analysis instead of stranding them, and using the *deepest populated*
        level by default keeps groups as specific as the data allows.
        """
        levels = [normalise_text(row.get(column, "")) for column in CATEGORY_COLUMNS]
        levels = ["" if self.lexicon.is_noise(value) else value for value in levels]

        if self.settings.bucket_level != "auto":
            depth = {"l1": 1, "l2": 2, "l3": 3, "l4": 4}[self.settings.bucket_level]
            path = [value for value in levels[:depth] if value]
            return " > ".join(path) if path else "Uncategorised"

        populated = [value for value in levels if value]
        return " > ".join(populated) if populated else "Uncategorised"

    # -- reduction ----------------------------------------------------------

    def collect(self) -> None:
        """Reduce the input rows to distinct descriptions per bucket."""
        assert self.table is not None
        skipped = 0

        for row in self.table.rows:
            description = normalise_text(row.get(REQUIRED_COLUMN, ""))
            if not description or self.lexicon.is_noise(description):
                skipped += 1
                continue

            bucket = self._category_bucket(row)
            signature = self.builder.signature(description)
            if not signature:
                skipped += 1
                continue

            key = (bucket, signature)
            entry = self.entries.get(key)
            if entry is None:
                entry = DescriptionEntry(text=description, signature=signature, bucket=bucket)
                self.entries[key] = entry
            elif len(description) < len(entry.text):
                # Keep the shortest surface form as the entry's representative
                # text. Shorter descriptions carry fewer incidental qualifiers,
                # which makes for a cleaner label.
                entry.text = description

            entry.row_count += 1
            spend = parse_amount(row.get("Spend_EUR", ""))
            if spend is not None:
                entry.spend += spend
            confidence = parse_amount(row.get("AI_Confidence", ""))
            if confidence is not None:
                entry.confidence_total += confidence

            for column in SUPPORTING_COLUMNS:
                value = normalise_text(row.get(column, ""))
                if value and not self.lexicon.is_noise(value):
                    entry.supporting[value] += 1

        self.statistics["rows_total"] = len(self.table.rows)
        self.statistics["rows_without_description"] = skipped
        self.statistics["distinct_descriptions"] = len(self.entries)

        buckets = {bucket for bucket, _ in self.entries}
        self.statistics["category_buckets"] = len(buckets)
        LOGGER.info("%d row(s) reduced to %d distinct description(s) across %d category bucket(s).",
                    len(self.table.rows), len(self.entries), len(buckets))

        self.run_id = stable_hash(
            "agent2-run",
            str(self.settings.input_path.name),
            str(len(self.table.rows)),
            str(sorted(buckets)),
            AGENT_VERSION,
        )

    # -- grouping -----------------------------------------------------------

    def group(self) -> None:
        """Assign every distinct description to a purchase group."""
        by_bucket: Dict[str, List[DescriptionEntry]] = defaultdict(list)
        for (bucket, _), entry in self.entries.items():
            by_bucket[bucket].append(entry)

        for bucket in sorted(by_bucket):
            # Sorted by signature so that the order of the similarity matrix,
            # and therefore every tie-break inside the clustering, is a function
            # of the data alone and not of dictionary iteration order.
            entries = sorted(by_bucket[bucket], key=lambda item: item.signature)
            self._group_bucket(bucket, entries)

        LOGGER.info("Formed %d group(s): %d new, %d carried over from the registry.",
                    len(self.groups),
                    sum(1 for group in self.groups.values() if group.is_new),
                    sum(1 for group in self.groups.values() if not group.is_new))

    def _group_bucket(self, bucket: str, entries: List[DescriptionEntry]) -> None:
        """Group the distinct descriptions of one category bucket."""
        # Anything the registry already knows keeps its group without being
        # reconsidered. This is both the stability guarantee and, on a re-run
        # over mostly unchanged data, an enormous saving: the similarity matrix
        # is only ever built for descriptions that are genuinely new.
        pending: List[DescriptionEntry] = []
        for entry in entries:
            existing = self.registry.lookup(entry.signature)
            if existing:
                self._attach_to_group(existing, entry, is_new=False)
                self.statistics["descriptions_from_registry"] += 1
            else:
                pending.append(entry)

        if not pending:
            return

        if len(pending) == 1:
            self._create_group(bucket, [pending[0]], cohesion_score=0.5)
            return

        if len(pending) > self.settings.max_bucket_size:
            # An all-pairs matrix over this many descriptions would exhaust
            # memory. Splitting the bucket by first lemma is crude but keeps the
            # run alive and only affects the largest, least specific categories.
            LOGGER.warning(
                "Category bucket %r holds %d new descriptions, above the cap of %d. "
                "Splitting it by leading term; raise --max-bucket-size to avoid this.",
                bucket, len(pending), self.settings.max_bucket_size)
            partitions: Dict[str, List[DescriptionEntry]] = defaultdict(list)
            for entry in pending:
                lemmas = self.builder.lemmas(entry.text)
                partitions[lemmas[0] if lemmas else ""].append(entry)
            for key in sorted(partitions):
                self._cluster_and_create(bucket, partitions[key])
            return

        self._cluster_and_create(bucket, pending)

    def _cluster_and_create(self, bucket: str, entries: List[DescriptionEntry]) -> None:
        """Cluster new descriptions and turn each cluster into a group."""
        if not entries:
            return
        if len(entries) == 1:
            self._create_group(bucket, entries, cohesion_score=0.5)
            return

        texts = [entry.text for entry in entries]
        matrix = self.similarity.similarity_matrix(texts)
        threshold = self._choose_threshold(matrix, len(entries))
        labels = agglomerative_labels(matrix, threshold)

        clusters: Dict[int, List[int]] = defaultdict(list)
        for index, label in enumerate(labels):
            clusters[label].append(index)

        for label in sorted(clusters):
            member_indices = clusters[label]
            members = [entries[index] for index in member_indices]
            self._create_group(bucket, members, cohesion(matrix, member_indices))

    def _choose_threshold(self, matrix: Any, count: int) -> float:
        """Pick the distance threshold for one bucket.

        The client asked whether to expect 10 to 50 groups per category and
        answered honestly that they do not know. Rather than impose a number,
        the threshold is searched so that the group count lands inside the
        requested window where the data allows it, and the configured default is
        used unchanged where it does not. Small buckets are left alone: forcing
        twelve descriptions into ten groups produces ten meaningless groups.
        """
        default = self.settings.distance_threshold
        if not self.settings.adaptive or count < self.settings.min_groups_per_category * 2:
            return default

        low, high = 0.10, 0.70
        best = default
        # Ten bisection steps resolve the threshold to about 0.0006, far finer
        # than the clustering is sensitive to.
        for _ in range(10):
            candidate = (low + high) / 2.0
            groups = len(set(agglomerative_labels(matrix, candidate)))
            best = candidate
            if groups > self.settings.max_groups_per_category:
                # Too many groups: merge more aggressively.
                low = candidate
            elif groups < self.settings.min_groups_per_category:
                high = candidate
            else:
                return candidate
        return best

    def _create_group(self, bucket: str, members: List[DescriptionEntry],
                      cohesion_score: float) -> None:
        """Name a cluster and record it as a group."""
        descriptions = [member.text for member in members]
        weights = [float(member.row_count) for member in members]

        label, naming_method = "", "consensus"
        if cohesion_score >= self.settings.min_cohesion:
            label = self.labeller.label(descriptions, weights)

        if not label:
            # Either the cluster does not agree with itself or its members carry
            # no content words. The specification is explicit about where such
            # lines go.
            label, naming_method = OTHER_GROUP_LABEL, "fallback"

        # The identifier is derived from the label and the bucket, so the same
        # group in the same category has the same identifier on any machine and
        # in any run, with no counter to keep in sync.
        group_id = (OTHER_GROUP_ID if label == OTHER_GROUP_LABEL
                    else f"G-{stable_hash(bucket, lookup_key(label))[:10].upper()}")

        group = self.groups.get(group_id)
        if group is None:
            group = PurchaseGroup(group_id=group_id, label=label, bucket=bucket,
                                  cohesion=cohesion_score, is_new=True,
                                  naming_method=naming_method)
            self.groups[group_id] = group
            self.statistics["groups_created"] += 1
        else:
            # Two clusters in the same bucket resolved to the same consensus
            # name, which means they were the same purchase all along.
            group.cohesion = min(group.cohesion, cohesion_score)

        for member in members:
            self._attach_to_group(group_id, member, is_new=True, group=group)

    def _attach_to_group(self, group_id: str, entry: DescriptionEntry,
                         is_new: bool, group: Optional[PurchaseGroup] = None) -> None:
        """Bind one description entry to a group, creating a shell if needed."""
        if group is None:
            group = self.groups.get(group_id)
        if group is None:
            # Carried over from the registry and not yet seen in this run.
            group = PurchaseGroup(
                group_id=group_id,
                label=self.registry.label_of(group_id) or OTHER_GROUP_LABEL,
                bucket=self.registry.bucket_of(group_id) or entry.bucket,
                is_new=False, naming_method="registry",
            )
            self.groups[group_id] = group

        group.signatures.add(entry.signature)
        group.descriptions.append(entry.text)
        group.row_count += entry.row_count
        group.spend += entry.spend
        if not is_new:
            group.is_new = False
        self.signature_to_group[entry.signature] = group_id

    # -- optional naming pass -----------------------------------------------

    def refine_labels(self) -> None:
        """Ask the model to name only the groups the consensus rule named badly.

        This is the sole use of a language model in Agent 2, and it is scoped as
        tightly as it can be: one request per batch of weak labels, one label per
        group, cached permanently, and only for groups the deterministic rule
        could not name usefully. A group already called "Asbestos removal" never
        reaches this method.
        """
        if self.model is None or not self.model.config.enabled:
            return

        weak = [group for group in self.groups.values()
                if group.is_new and group.group_id != OTHER_GROUP_ID
                and self.labeller.is_weak(group.label)]
        if not weak:
            LOGGER.info("Every group received a specific name; the model was not needed.")
            return

        weak.sort(key=lambda group: group.group_id)
        LOGGER.info("Asking %s to name %d weakly-labelled group(s).",
                    self.model.config.model, len(weak))

        system_prompt = (
            "You name groups of similar industrial and energy sector purchases. "
            "Each group is a set of purchase descriptions that have already been "
            "determined to describe the same kind of material or service.\n"
            "Rules:\n"
            "1. Return a short English noun phrase of two to four words.\n"
            "2. Use only concepts present in the descriptions given. Never "
            "introduce a material, service or brand that does not appear.\n"
            "3. Name the common purchase, not one specific example of it.\n"
            "4. Do not use the words 'group', 'category', 'various' or 'other'.\n"
            "5. If the descriptions have nothing in common, return exactly "
            f'"{OTHER_GROUP_LABEL}".\n'
            'Reply with JSON: {"labels": {"<group_id>": "<label>"}}.'
        )

        batch_size = max(1, self.model.config.batch_size)
        for start in range(0, len(weak), batch_size):
            batch = weak[start:start + batch_size]
            outstanding: List[PurchaseGroup] = []

            for group in batch:
                # The cache is keyed on the group's members, not on its
                # identifier, so a group whose membership is unchanged never
                # costs a second request even if it is renamed upstream.
                payload = json.dumps(sorted(set(group.descriptions))[:20], ensure_ascii=False)
                cached = self.model.cached(self.model.cache_key("label", payload))
                if cached:
                    group.label, group.naming_method = cached, "model"
                else:
                    outstanding.append(group)

            if not outstanding:
                continue

            request = {
                "groups": [
                    {
                        "group_id": group.group_id,
                        "category": group.bucket,
                        "descriptions": sorted(set(group.descriptions))[:20],
                    }
                    for group in outstanding
                ]
            }
            response = self.model.complete_json(system_prompt,
                                                json.dumps(request, ensure_ascii=False))
            if not response:
                continue

            labels = response.get("labels") or {}
            if not isinstance(labels, dict):
                continue
            for group in outstanding:
                proposed = normalise_text(labels.get(group.group_id, ""))
                if not proposed or self.lexicon.is_noise(proposed):
                    continue
                proposed = sentence_case(" ".join(tokenise(proposed)[: self.settings.max_label_words]))
                payload = json.dumps(sorted(set(group.descriptions))[:20], ensure_ascii=False)
                self.model.store(self.model.cache_key("label", payload), proposed)
                group.label, group.naming_method = proposed, "model"
                self.statistics["groups_named_by_model"] += 1

    # -- consolidation ------------------------------------------------------

    def merge_equivalent_labels(self) -> None:
        """Fold together groups whose names differ only by a fulfilment word.

        Signatures already ignore those words, so this catches the remaining
        route to a near-duplicate name: a label the model proposed after the
        groups were formed. The surviving group is the one with the most lines
        behind it, and it takes the shorter of the two names.
        """
        by_key: Dict[Tuple[str, Tuple[str, ...]], List[PurchaseGroup]] = defaultdict(list)
        for group in self.groups.values():
            if group.group_id == OTHER_GROUP_ID:
                continue
            key = self.builder.drop_fulfilment(self.builder.lemmas(group.label))
            if key:
                by_key[(group.bucket, key)].append(group)

        merged = 0
        for members in by_key.values():
            if len(members) < 2:
                continue
            members.sort(key=lambda g: (-g.row_count, -g.spend, len(g.label), g.group_id))
            survivor = members[0]
            for group in members[1:]:
                if len(group.label) < len(survivor.label):
                    survivor.label = group.label
                survivor.signatures |= group.signatures
                survivor.descriptions.extend(group.descriptions)
                survivor.row_count += group.row_count
                survivor.spend += group.spend
                survivor.cohesion = min(survivor.cohesion, group.cohesion)
                for signature in group.signatures:
                    self.signature_to_group[signature] = survivor.group_id
                del self.groups[group.group_id]
                merged += 1

        if merged:
            self.statistics["groups_merged_by_label"] = merged
            LOGGER.info("Merged %d group(s) whose name differed only by a "
                        "fulfilment word such as delivery or site use.", merged)

    def enforce_group_cap(self) -> None:
        """Hold the number of Category L5 names at Fortum's ceiling.

        Groups are ranked by the spend behind them, so the names that survive
        are the ones a category manager would act on first. Everything past the
        ceiling joins "Other", which occupies one of the places itself.
        """
        cap = self.settings.max_total_groups
        if cap <= 0:
            return

        named = [group for group in self.groups.values()
                 if group.group_id != OTHER_GROUP_ID]
        if len(named) <= cap - 1:
            return

        named.sort(key=lambda g: (-g.spend, -g.row_count, g.label, g.group_id))
        demoted = named[cap - 1:]

        other = self.groups.get(OTHER_GROUP_ID)
        if other is None:
            other = PurchaseGroup(group_id=OTHER_GROUP_ID, label=OTHER_GROUP_LABEL,
                                  bucket=demoted[0].bucket, is_new=True,
                                  naming_method="cap")
            self.groups[OTHER_GROUP_ID] = other

        for group in demoted:
            other.signatures |= group.signatures
            other.descriptions.extend(group.descriptions)
            other.row_count += group.row_count
            other.spend += group.spend
            for signature in group.signatures:
                self.signature_to_group[signature] = OTHER_GROUP_ID
            del self.groups[group.group_id]

        self.statistics["groups_folded_into_other"] = len(demoted)
        LOGGER.info(
            "Category L5 is capped at %d names including 'Other'; %d group(s) "
            "below the spend line were folded into 'Other'.", cap, len(demoted))

    # -- persistence --------------------------------------------------------

    def commit_registry(self) -> None:
        """Write every group formed in this run back into the registry."""
        for group_id in sorted(self.groups):
            group = self.groups[group_id]
            self.registry.register(group_id, group.label, group.bucket, group.signatures)
            self.registry.stamp_run(group_id, self.run_id)
        self.registry.save()

    def write(self) -> Dict[str, Any]:
        """Write every output file and return the run manifest."""
        assert self.table is not None
        results_dir = self.settings.results_dir
        results_dir.mkdir(parents=True, exist_ok=True)

        rows_path = results_dir / "agent2_purchase_groups.csv"
        jsonl_path = results_dir / "agent2_purchase_groups.jsonl"
        directory_path = results_dir / "agent2_group_directory.csv"

        appended = ["AI_Purchase_Group_L5", "AI_Purchase_Group_Id",
                    "AI_Purchase_Group_Confidence", "AI_Purchase_Group_Band",
                    "AI_Purchase_Group_Size", "AI_Purchase_Group_Category",
                    "AI_Purchase_Group_Cohesion", "AI_Purchase_Group_Naming",
                    "AI_Purchase_Group_Is_New", "Agent2_Run_Id"]
        # Columns Agent 1 already wrote are not duplicated.
        headers = list(self.table.headers) + [name for name in appended
                                              if name not in self.table.headers]

        band_counts: Counter = Counter()
        grouped_rows = 0

        with rows_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()

            jsonl_handle = jsonl_path.open("w", encoding="utf-8") if self.settings.write_jsonl else None
            try:
                for row in self.table.rows:
                    output = dict(row)
                    description = normalise_text(row.get(REQUIRED_COLUMN, ""))
                    signature = self.builder.signature(description) if description else ""
                    group_id = self.signature_to_group.get(signature, "")
                    group = self.groups.get(group_id)

                    if group is None:
                        # No description, or a description with no content
                        # words. Explicitly "Other" rather than blank, so the
                        # Power BI hierarchy has no holes in it.
                        confidence, band = 0, "Low"
                        output.update({
                            "AI_Purchase_Group_L5": OTHER_GROUP_LABEL,
                            "AI_Purchase_Group_Id": OTHER_GROUP_ID,
                            "AI_Purchase_Group_Confidence": confidence,
                            "AI_Purchase_Group_Band": band,
                            "AI_Purchase_Group_Size": 0,
                            "AI_Purchase_Group_Category": self._category_bucket(row),
                            "AI_Purchase_Group_Cohesion": 0.0,
                            "AI_Purchase_Group_Naming": "unassigned",
                            "AI_Purchase_Group_Is_New": "No",
                        })
                    else:
                        entry = self.entries.get((group.bucket, signature))
                        confidence, band = self._score(group, entry)
                        grouped_rows += 1
                        band_counts[band] += 1
                        output.update({
                            "AI_Purchase_Group_L5": group.label,
                            "AI_Purchase_Group_Id": group.group_id,
                            "AI_Purchase_Group_Confidence": confidence,
                            "AI_Purchase_Group_Band": band,
                            "AI_Purchase_Group_Size": group.row_count,
                            # "Other" spans every category, so the row keeps its
                            # own category path rather than the group's.
                            "AI_Purchase_Group_Category": (
                                self._category_bucket(row)
                                if group.group_id == OTHER_GROUP_ID else group.bucket),
                            "AI_Purchase_Group_Cohesion": round(group.cohesion, 3),
                            "AI_Purchase_Group_Naming": group.naming_method,
                            "AI_Purchase_Group_Is_New": "Yes" if group.is_new else "No",
                        })

                    output["Agent2_Run_Id"] = self.run_id
                    writer.writerow(output)

                    if jsonl_handle is not None:
                        payload = dict(output)
                        payload["Grouping_Evidence"] = {
                            "signature": signature,
                            "group_members": (sorted(set(group.descriptions))[:10]
                                              if group else []),
                            "distinct_descriptions_in_group": (len(set(group.descriptions))
                                                               if group else 0),
                        }
                        jsonl_handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            finally:
                if jsonl_handle is not None:
                    jsonl_handle.close()

        self._write_directory(directory_path)

        if self.model is not None:
            self.model.save_cache()

        statistics = dict(self.statistics)
        statistics.update({
            "rows_grouped": grouped_rows,
            "groups_total": len(self.groups),
            "groups_new": sum(1 for group in self.groups.values() if group.is_new),
            "groups_other": sum(1 for group in self.groups.values()
                                if group.group_id == OTHER_GROUP_ID),
            "confidence_bands": dict(band_counts),
            "groups_per_bucket": self._groups_per_bucket(),
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
                "input": str(self.settings.input_path),
                "results_dir": str(self.settings.results_dir),
                "registry": str(self.settings.registry_path),
                "embeddings": self.similarity.use_embeddings,
                "language_model": self.settings.model.enabled,
                "model": self.settings.model.model if self.settings.model.enabled else None,
                "distance_threshold": self.settings.distance_threshold,
                "adaptive_threshold": self.settings.adaptive,
                "groups_per_category_target": [self.settings.min_groups_per_category,
                                               self.settings.max_groups_per_category],
                "bucket_level": self.settings.bucket_level,
            },
            "outputs": [rows_path.name, directory_path.name]
                       + ([jsonl_path.name] if self.settings.write_jsonl else []),
            "statistics": statistics,
        }
        (results_dir / "agent2_run_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    def _score(self, group: PurchaseGroup, entry: Optional[DescriptionEntry]) -> Tuple[int, str]:
        """Confidence that a line belongs in the group it was assigned to.

        Built from how tightly the group agrees with itself, how much
        corroboration the group has, and how confident Agent 1 was about the
        description in the first place. A group with one member is not wrong,
        but nothing supports it either, and the score says so.
        """
        if group.group_id == OTHER_GROUP_ID:
            return 0, "Low"

        cohesion_factor = max(0.0, min(1.0, group.cohesion))
        # Corroboration saturates around eight distinct wordings; beyond that,
        # more variants say nothing further about whether the group is real.
        distinct = len(set(group.descriptions))
        support_factor = min(1.0, math.log1p(distinct) / math.log(9))
        upstream_factor = (entry.mean_confidence / 100.0) if entry and entry.row_count else 0.5
        specific_factor = 0.0 if self.labeller.is_weak(group.label) else 1.0

        raw = (cohesion_factor * 0.40 + support_factor * 0.20
               + upstream_factor * 0.25 + specific_factor * 0.15)
        score = int(round(max(0.0, min(1.0, raw)) * 100))
        band = "High" if score >= 75 else "Medium" if score >= 50 else "Low"
        return score, band

    def _groups_per_bucket(self) -> Dict[str, int]:
        """Group count for each category, for the closing report."""
        counts: Counter = Counter()
        for group in self.groups.values():
            counts[group.bucket] += 1
        return dict(sorted(counts.items()))

    def _write_directory(self, path: Path) -> None:
        """Write the group directory: one row per group, for expert review.

        This is the file a category manager actually reads. Validation of this
        agent means looking down this list and saying whether the groups are the
        right shape, so it is ordered by spend and carries example members.
        """
        columns = ["AI_Purchase_Group_Id", "AI_Purchase_Group_L5", "Category_Path",
                   "Line_Count", "Distinct_Descriptions", "Total_Spend_EUR",
                   "Cohesion", "Naming_Method", "Is_New_This_Run", "Example_Descriptions"]

        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            ordered = sorted(self.groups.values(),
                             key=lambda group: (-group.spend, -group.row_count, group.group_id))
            for group in ordered:
                examples = sorted(set(group.descriptions))[:8]
                writer.writerow([
                    group.group_id, group.label, group.bucket,
                    group.row_count, len(set(group.descriptions)), round(group.spend, 2),
                    round(group.cohesion, 3), group.naming_method,
                    "Yes" if group.is_new else "No", " | ".join(examples),
                ])

    def run(self) -> Dict[str, Any]:
        """Execute the full pipeline."""
        self.table = read_input(self.settings.input_path)
        self.collect()
        self.group()
        self.refine_labels()
        self.merge_equivalent_labels()
        self.enforce_group_cap()
        self.commit_registry()
        return self.write()


# ===========================================================================
# Command line interface
# ===========================================================================

BANNER = r"""
===============================================================================
 Fortum AI-Powered Procurement Analysis
 Agent 2 - AI Purchase Group (Category L5)
 Prof. Shahab Anbarjafari
===============================================================================
""".strip("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent2.py",
        description="Agent 2 - group similar purchases into a standardised Category L5.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python agent2.py\n"
            "      Prompt for each path in turn and run with the defaults.\n\n"
            "  python agent2.py --non-interactive --input results/agent1_unified_lines.csv\n"
            "      Run unattended against Agent 1's output.\n\n"
            "  python agent2.py --non-interactive --min-groups 15 --max-groups 40\n"
            "      Steer the adaptive threshold towards a narrower group count.\n"
        ),
    )
    paths = parser.add_argument_group("paths")
    paths.add_argument("--input", metavar="FILE", help="unified table written by Agent 1")
    paths.add_argument("--results", metavar="DIR", help="folder to write results into")
    paths.add_argument("--lexicon", metavar="FILE", help="controlled vocabulary JSON file")
    paths.add_argument("--registry", metavar="FILE", help="group registry that keeps labels stable")
    paths.add_argument("--cache", metavar="DIR", help="folder for the model response cache")

    grouping = parser.add_argument_group("grouping")
    grouping.add_argument("--threshold", type=float, default=0.35,
                          help="cosine distance at which a group is cut (default 0.35)")
    grouping.add_argument("--no-adaptive", action="store_true",
                          help="use the fixed threshold instead of searching per category")
    grouping.add_argument("--min-groups", type=int, default=10,
                          help="lower end of the group count sought per category (default 10)")
    grouping.add_argument("--max-groups", type=int, default=50,
                          help="upper end of the group count sought per category (default 50)")
    grouping.add_argument("--bucket-level", choices=("auto", "l1", "l2", "l3", "l4"),
                          default="auto",
                          help="category depth groups are formed within (default auto)")
    grouping.add_argument("--max-label-words", type=int, default=5,
                          help="word budget for a group label (default 5)")
    grouping.add_argument("--max-bucket-size", type=int, default=6000,
                          help="new descriptions per category before splitting (default 6000)")
    grouping.add_argument("--max-total-groups", type=int, default=6000,
                          help="ceiling on Category L5 names including 'Other'; "
                               "groups below the spend line join 'Other' "
                               "(default 6000, 0 disables the ceiling)")

    tiers = parser.add_argument_group("processing tiers")
    tiers.add_argument("--no-embeddings", action="store_true",
                       help="disable the multilingual sentence-embedding view")
    tiers.add_argument("--use-llm", action="store_true",
                       help="let the language model name groups the rules named badly")
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
    default_input = (Path(args.input) if args.input
                     else default_results / "agent1_unified_lines.csv")
    default_lexicon = Path(args.lexicon) if args.lexicon else here / "lexicon" / "procurement_lexicon.json"
    default_registry = (Path(args.registry) if args.registry
                        else here / "lexicon" / "agent2_group_registry.json")
    default_cache = Path(args.cache) if args.cache else here / "cache"

    use_embeddings = not args.no_embeddings
    use_llm = args.use_llm
    spend_limit = (args.llm_spend_limit if args.llm_spend_limit is not None
                   else _env_float(env.get("LLM_SPEND_LIMIT"), DEFAULT_SPEND_LIMIT))

    if not args.non_interactive:
        print(BANNER)
        print("\nPress Enter to accept the value shown in brackets.\n")
        default_input = Path(ask("Agent 1 unified table", str(default_input)))
        default_results = Path(ask("Results folder", str(default_results)))
        default_lexicon = Path(ask("Controlled vocabulary file", str(default_lexicon)))
        default_registry = Path(ask("Group registry (keeps labels stable across runs)",
                                    str(default_registry)))
        default_cache = Path(ask("Cache folder", str(default_cache)))

        print()
        if _sentence_transformers is None:
            print("  Sentence embeddings are not installed; that view will be skipped.")
            use_embeddings = False
        else:
            use_embeddings = ask_yes_no(
                "Use multilingual sentence embeddings (recommended, free)?", True)
        use_llm = ask_yes_no(
            "Let the language model name groups the rules could not name well?", use_llm)
        if use_llm:
            print()
            print(f"  Charged at ${INPUT_COST_PER_MTOK:,.2f} per million input tokens and "
                  f"${OUTPUT_COST_PER_MTOK:,.2f} per million output tokens.")
            print("  The run pauses at the figure below and asks before spending more.")
            spend_limit = ask_amount(
                "Alert when estimated language-model spend reaches (USD)", spend_limit)
        print()

    settings = Settings(
        input_path=default_input.expanduser().resolve(),
        results_dir=default_results.expanduser().resolve(),
        lexicon_path=default_lexicon.expanduser().resolve(),
        registry_path=default_registry.expanduser().resolve(),
        cache_dir=default_cache.expanduser().resolve(),
        use_embeddings=use_embeddings,
        use_llm=use_llm,
        distance_threshold=args.threshold,
        adaptive=not args.no_adaptive,
        min_groups_per_category=args.min_groups,
        max_groups_per_category=args.max_groups,
        max_label_words=args.max_label_words,
        bucket_level=args.bucket_level,
        max_bucket_size=args.max_bucket_size,
        max_total_groups=args.max_total_groups,
        write_jsonl=not args.no_jsonl,
        verbose=args.verbose,
        interactive=not args.non_interactive,
    )
    settings.model = resolve_model_config(env, use_llm, spend_limit)

    if settings.min_groups_per_category > settings.max_groups_per_category:
        raise SystemExit("--min-groups cannot exceed --max-groups")
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
    print(f"  Rows read            : {statistics.get('rows_total', 0):,}")
    print(f"  Rows grouped         : {statistics.get('rows_grouped', 0):,}")
    print(f"  Distinct descriptions: {statistics.get('distinct_descriptions', 0):,}")
    print(f"  Category buckets     : {statistics.get('category_buckets', 0):,}")
    print(f"  Purchase groups      : {statistics.get('groups_total', 0):,} "
          f"({statistics.get('groups_new', 0):,} new this run)")

    carried = statistics.get("descriptions_from_registry", 0)
    if carried:
        print(f"  Reused from registry : {carried:,} description(s) kept their group")

    named = statistics.get("groups_named_by_model", 0)
    if named:
        print(f"  Named by the model   : {named:,}")

    bands = statistics.get("confidence_bands", {})
    if bands:
        print("  Confidence bands     : "
              + "  ".join(f"{name} {count:,}" for name, count in sorted(bands.items())))

    per_bucket = statistics.get("groups_per_bucket", {})
    if per_bucket:
        counts = sorted(per_bucket.values())
        median = counts[len(counts) // 2]
        print(f"  Groups per category  : min {counts[0]}, median {median}, max {counts[-1]}")
        print("\n  Largest categories by group count")
        for bucket, count in sorted(per_bucket.items(), key=lambda item: -item[1])[:8]:
            display = bucket if len(bucket) <= 52 else bucket[:49] + "..."
            print(f"    {count:>4}  {display}")

    print(f"\n  Output folder        : {settings.results_dir}")
    for name in manifest["outputs"]:
        print(f"    {name}")
    print(f"  Group registry       : {settings.registry_path}")

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
        "LLM_REASONING_EFFORT",
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
        manifest = Agent2(settings).run()
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
