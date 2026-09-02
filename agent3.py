#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 3 - AI Material and Service Standardisation.

Identifies free-text purchases that could have been bought through an existing
catalogue item, price list or standard item, and identifies the recurring
free-text purchases that ought to become catalogue items in the first place.

    Background
    ----------
    Two questions sit behind this agent, and the planning workbook is explicit
    that they are different questions.

    The first is retrospective: of everything that was bought as free text, what
    was already available as a standard item? Those lines are the raw material
    for the catalogue-compliance and maverick-buying analysis, because each one
    is a purchase that bypassed a channel that existed.

    The second is prospective: which recurring free-text purchases are regular
    enough, and stable enough in price, that they should become catalogue items?
    That question is answered without any reference data at all - it is a
    property of the buying pattern - and it is what makes this agent useful even
    before a complete set of price lists has been supplied.

    Functional equivalence
    ----------------------
    The client's instruction on matching is unambiguous and is the hardest part
    of the design: a match must be based on functional equivalence, not textual
    similarity. Descriptions differ in wording and in language, including
    between one catalogue and the next, and the agent has to recognise that two
    texts denote the same item even when they share no words at all.

    That is approached in four layers rather than one similarity number:

        translation  reference items are rendered in English first, so that a
                     Polish catalogue and a Finnish purchase line are compared
                     on equal terms rather than through a multilingual model's
                     goodwill alone
        semantic     multilingual sentence embeddings, which connect wordings
                     with nothing lexical in common
        lexical      character n-grams, which anchor the semantic view and stop
                     it from drifting onto plausible but wrong neighbours
        constraints  a type gate and a specification comparison, which decide
                     whether two things are the *same kind of thing* at the same
                     *size and rating*, independently of how alike they read

    The last layer is what separates functional equivalence from resemblance. A
    DN50 valve and a DN200 valve read almost identically and are not
    substitutes; a "frequency converter" and a "taajuusmuuttaja" read nothing
    alike and are the same product.

    Thresholds
    ----------
    The plan leaves the similarity threshold to be defined and tested rather
    than assumed. Defaults are supplied and documented, and every run writes a
    calibration file giving the full distribution of best-match scores, so the
    thresholds can be set from the data instead of from taste. Results are
    opportunity indicators for review, never assertions.

    Output
    ------
    Written to the results folder:

        agent3_standardisation.csv          one row per input line
        agent3_standardisation.jsonl        the same rows with all candidates
        agent3_catalogue_candidates.csv     recurring free-text worth listing
        agent3_match_calibration.csv        score distribution for threshold work
        agent3_run_manifest.json            configuration, statistics and tokens

Usage:
    python agent3.py

Author:
    Prof. Shahab Anbarjafari
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import logging
import math
import os
import re
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from runtime import (
    DEFAULT_AZURE_MODEL, DEFAULT_OPENAI_MODEL, DEFAULT_REASONING_EFFORT,
    chat_completion_body, configure_process_logging, load_sentence_transformer,
    retry_chat_body,
)

LOGGER = logging.getLogger("agent3")

AGENT_NAME = "Agent 3 - AI Material and Service Standardisation"
AGENT_VERSION = "1.5.1"

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))


# ===========================================================================
# Optional dependencies
# ===========================================================================

def _import_optional(module_path: str) -> Optional[Any]:
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
_sklearn_neighbors = _import_optional("sklearn.neighbors")
_transformers = _import_optional("transformers")
_torch = _import_optional("torch")
_openpyxl = _import_optional("openpyxl")


def describe_environment() -> Dict[str, bool]:
    return {
        "numpy": _numpy is not None,
        "scikit-learn": _sklearn_text is not None and _sklearn_neighbors is not None,
        "sentence-transformers": _sentence_transformers is not None,
        "transformers": _transformers is not None,
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
    batch_size: int = 20
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
    reference_dir: Path
    results_dir: Path
    lexicon_path: Path
    cache_dir: Path

    # A folder holding item catalogues and nothing else. Fortum sends catalogues
    # separately from the master data, so they get an input of their own rather
    # than being fished out of the same tree as the purchase extracts.
    catalogue_dir: Optional[Path] = None

    use_embeddings: bool = True
    use_neural_translation: bool = True
    use_llm: bool = False

    # Match thresholds. The development plan leaves these to be defined and
    # tested rather than assumed, so they are exposed on the command line and
    # every run writes the score distribution that justifies changing them.
    # These starting values place a match in "High" only when the semantic and
    # lexical views agree and neither constraint layer objected.
    high_threshold: float = 0.80
    medium_threshold: float = 0.65
    minimum_threshold: float = 0.50

    # Score below which a candidate is not even reported as an alternative.
    report_threshold: float = 0.40
    top_k: int = 5

    # Penalty applied when the two texts are not the same kind of thing. A
    # multiplier rather than a veto, so an unusually strong match still appears
    # for review but cannot reach the High band on resemblance alone.
    type_mismatch_penalty: float = 0.55
    spec_conflict_penalty: float = 0.70

    # Catalogue-candidate rules. A purchase has to recur, and to matter, before
    # it is worth the effort of listing.
    candidate_min_occurrences: int = 3
    candidate_min_spend: float = 1000.0
    candidate_price_tolerance: float = 0.25

    # Band around the accept threshold where the model is asked to adjudicate.
    adjudication_margin: float = 0.10

    write_jsonl: bool = True
    verbose: bool = False

    # False under --non-interactive, where nothing may block waiting for input.
    interactive: bool = True

    model: ModelConfig = field(default_factory=ModelConfig)

    def catalogue_roots(self) -> Tuple[Path, ...]:
        """Where to look for catalogues, most specific folder first.

        When a catalogue folder is named it is the whole answer: the point of
        naming it is to stop the reference tree, which also holds the purchase
        extracts, being trawled for anything that resembles a price list.
        """
        if self.catalogue_dir is not None:
            return (self.catalogue_dir,)
        return (self.reference_dir,)


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
    """Select the language-model backend. Mirrors Agents 1 and 2 exactly."""
    config = ModelConfig(enabled=use_llm)
    config.batch_size = max(1, _env_int(env.get("LLM_BATCH_SIZE"), 20))
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

    Applies with force here. The supplied catalogue has been through at least
    one wrong code page - Polish diacritics arrive as literal question marks and
    replacement characters - and every one of those defeats both the lexical
    comparison and the vocabulary lookup.
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
    """Aggressive key for comparing item and part numbers across systems."""
    return re.sub(r"[^A-Z0-9]", "", normalise_text(value).upper())


def is_code_token(token: str) -> bool:
    if not token or len(token) <= 2:
        return True
    if any(ch.isdigit() for ch in token):
        return True
    return token.isupper() and len(token) >= 4


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


# ===========================================================================
# Controlled vocabulary
# ===========================================================================

class Lexicon:
    """The shared procurement vocabulary.

    Agent 3 uses it for three things: rendering reference items in English,
    recognising placeholder values, and telling a service apart from a material.
    """

    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        payload = payload or {}
        self.version: str = str(payload.get("version", "0.0.0"))

        self.phrases: Dict[str, Dict[str, str]] = {}
        self.terms: Dict[str, Dict[str, str]] = {}
        for language, entries in (payload.get("phrases") or {}).items():
            self.phrases[language] = {lookup_key(k): v for k, v in entries.items()}
        for language, entries in (payload.get("terms") or {}).items():
            self.terms[language] = {lookup_key(k): v for k, v in entries.items()}

        self.any_phrase: Dict[str, str] = {}
        for entries in self.phrases.values():
            self.any_phrase.update(entries)
        self.any_term: Dict[str, str] = {}
        for entries in self.terms.values():
            self.any_term.update(entries)
        self._phrase_order = sorted(self.any_phrase, key=lambda item: (-len(item), item))

        self.service_markers: Set[str] = {lookup_key(t) for t in payload.get("service_markers", [])}
        self.material_markers: Set[str] = {lookup_key(t) for t in payload.get("material_markers", [])}
        self.noise_terms: Set[str] = {lookup_key(t) for t in payload.get("noise_terms", [])}
        self.unit_terms: Dict[str, str] = {lookup_key(k): v
                                           for k, v in (payload.get("unit_terms") or {}).items()}

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
        LOGGER.info("Vocabulary loaded (version %s).", payload.get("version"))
        return cls(payload)

    def is_noise(self, text: str) -> bool:
        key = lookup_key(text)
        if not key or key in self.noise_terms:
            return True
        return not any(ch.isalpha() for ch in key)

    def translate_surface(self, text: str) -> Tuple[str, float]:
        """Render text in English using the vocabulary alone.

        Returns the rewritten text and the share of content tokens that were
        recognised. That share is what decides whether the neural translator is
        worth invoking for this string.
        """
        if not text:
            return "", 1.0

        working = lookup_key(text)
        substitutions = 0
        emitted = 0
        for phrase in self._phrase_order:
            if len(phrase) < 5 or phrase not in working:
                continue
            target = self.any_phrase[phrase]
            pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")
            working, count = pattern.subn(target, working)
            substitutions += count
            emitted += count * len(tokenise(target))

        if not substitutions:
            working = text

        rendered: List[str] = []
        content = resolved = 0
        for token in tokenise(working):
            key = lookup_key(token)
            known = self.unit_terms.get(key) or self.any_term.get(key)
            if known is None and is_code_token(token):
                rendered.append(token)
                continue
            content += 1
            if known:
                rendered.append(known)
                resolved += 1
            elif key in self.service_markers or key in self.material_markers:
                rendered.append(token)
                resolved += 1
            else:
                rendered.append(token)

        coverage = (min(content, resolved + emitted) / content) if content else 1.0
        return _WHITESPACE.sub(" ", " ".join(rendered)).strip(), coverage


class NeuralTranslator:
    """Local Helsinki-NLP opus-mt translation for reference item text.

    Only reference data needs translating: the purchase lines arrive from Agent
    1 already in English. Catalogues are the multilingual half of the problem,
    and rendering them in English before comparison is what lets the type gate
    and the lexical view work at all - both are meaningless across languages.
    """

    MODEL_TEMPLATE = "Helsinki-NLP/opus-mt-{source}-en"
    SUPPORTED = ("pl", "fi", "sv", "de", "da", "no", "nl", "et", "fr", "es", "it", "cs")
    # Norwegian is published only inside the North Germanic group model, so the
    # template above names a repository that does not exist. Kept in step with
    # agent1.py.
    MODEL_OVERRIDES = {"no": "Helsinki-NLP/opus-mt-gmq-en"}
    WINDOW = 32
    # Fixed beam count and no sampling, so a re-run reproduces the previous
    # output exactly.
    GENERATION = {"max_length": 256, "num_beams": 4, "do_sample": False}

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and _transformers is not None
        self._translators: Dict[str, Any] = {}
        self._unavailable: Set[str] = set()
        self.translated_count = 0

    def available_for(self, language: str) -> bool:
        return self.enabled and language in self.SUPPORTED and language not in self._unavailable

    def _build(self, model_name: str) -> Any:
        """Load one bilingual model, returning a callable that translates a list.

        Driven directly rather than through pipeline("translation"), which
        transformers 5 removed while keeping Marian itself, so that upgrading
        transformers cannot quietly disable this tier.
        """
        tokeniser = _transformers.AutoTokenizer.from_pretrained(model_name)
        model = _transformers.AutoModelForSeq2SeqLM.from_pretrained(model_name)
        model.eval()

        def translate(texts: Sequence[str]) -> List[str]:
            encoded = tokeniser(list(texts), return_tensors="pt", padding=True,
                                truncation=True, max_length=256)
            if _torch is not None:
                with _torch.no_grad():
                    produced = model.generate(**encoded, **self.GENERATION)
            else:
                produced = model.generate(**encoded, **self.GENERATION)
            return tokeniser.batch_decode(produced, skip_special_tokens=True)

        return translate

    def _translator(self, language: str) -> Optional[Any]:
        if language in self._translators:
            return self._translators[language]
        if not self.available_for(language):
            return None
        model_name = self.MODEL_OVERRIDES.get(
            language, self.MODEL_TEMPLATE.format(source=language))
        try:
            LOGGER.info("Loading offline translation model %s ...", model_name)
            translator = self._build(model_name)
        except Exception as error:
            LOGGER.warning("Translation model %s unavailable (%s).", model_name, error)
            self._unavailable.add(language)
            self._translators[language] = None
            return None
        self._translators[language] = translator
        return translator

    def translate_batch(self, texts: Sequence[str], language: str) -> Dict[str, str]:
        """Translate a batch into English, keyed by the input text."""
        translator = self._translator(language)
        if translator is None or not texts:
            return {}
        results: Dict[str, str] = {}
        for start in range(0, len(texts), self.WINDOW):
            window = list(texts[start:start + self.WINDOW])
            try:
                produced = translator(window)
            except Exception as error:
                LOGGER.warning("Offline translation failed for a batch (%s).", error)
                continue
            for source_text, output in zip(window, produced):
                translated = normalise_text(output)
                if translated:
                    results[source_text] = translated
                    self.translated_count += 1
        return results


_LANGUAGE_MARKERS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "pl": (("ą", "ć", "ę", "ł", "ń", "ś", "ź", "ż", "cz", "sz", "rz", "prz"),
           ("i", "w", "na", "do", "dla", "oraz", "szt", "nie", "sp")),
    "fi": (("ä", "ö", "yy", "kk", "tt", "uu"), ("ja", "sekä", "kpl", "tai", "oy")),
    "sv": (("å", "ä", "ö", "ck"), ("och", "av", "för", "med", "till", "st", "ab")),
    "de": (("ä", "ö", "ü", "ß", "sch"), ("und", "der", "die", "das", "für", "mit", "gmbh")),
    "en": ((), ("the", "and", "of", "for", "with", "wireless", "black", "service")),
}


def detect_language(text: str) -> str:
    """Identify the language of a reference item description.

    Only has to be good enough to route the string to the right translation
    model. A wrong answer costs a slightly worse rendering, not a wrong match,
    because every later stage validates its own result.
    """
    cleaned = normalise_text(text).lower()
    if not cleaned or not any(ch.isalpha() for ch in cleaned):
        return "und"

    tokens = set(tokenise(cleaned))
    scores: Dict[str, float] = defaultdict(float)
    for language, (character_markers, word_markers) in _LANGUAGE_MARKERS.items():
        for marker in character_markers:
            hits = cleaned.count(marker)
            if hits:
                scores[language] += min(hits, 4) * 0.7
        for marker in word_markers:
            if marker in tokens:
                scores[language] += 1.2
    if all(ord(ch) < 128 for ch in cleaned):
        scores["en"] += 0.6

    if not scores:
        return "und"
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[0][0]


# ===========================================================================
# Specification extraction
# ===========================================================================

class SpecificationReader:
    """Pulls the measurable properties out of an item description.

    This is the layer that turns resemblance into equivalence. Two valve
    descriptions can be textually near-identical and denote parts that will not
    fit the same pipe; two cable descriptions can differ in every word and be the
    same product. Neither case is decidable from similarity, and both are
    decidable from the numbers.

    Only patterns that carry a unit or a recognised engineering prefix are
    extracted. A bare number in a description is far more often an order
    quantity or a line reference than a specification, and treating those as
    specifications would reject correct matches.
    """

    # Nominal diameter, pressure rating and the common electrical designations.
    _DESIGNATIONS = re.compile(
        r"\b(dn|pn|nps|iso|din|ip|awg|m|g)\s?(\d{1,4})\b", re.IGNORECASE)

    # A magnitude with a unit attached, which is the usual way a rating is
    # written: "3 kW", "400V", "1.5mm2", "50 Hz".
    _MEASURES = re.compile(
        r"\b(\d+(?:[.,]\d+)?)\s?"
        r"(kw|mw|w|kva|va|kv|v|ma|a|hz|bar|mbar|pa|kpa|mpa|"
        r"mm2|mm|cm|m2|m3|m|km|kg|g|t|l|ml|mwh|kwh|ah|rpm|inch|\"|'|"
        r"gb|tb|mb)\b",
        re.IGNORECASE)

    # A candidate manufacturer designation: a run of letters and digits,
    # optionally hyphenated, such as "WH-CH520" or "MZK-135". The pattern is
    # deliberately loose and the letter and digit counts are checked afterwards,
    # because a lookahead that has to see past a hyphen is both unreadable and,
    # as written the obvious way, wrong for exactly the hyphenated codes it
    # exists to catch.
    _MODEL_CODE = re.compile(r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b")

    # Units that mean the same physical quantity, normalised so that "3 kW" and
    # "3000 W" are recognised as the same rating.
    _UNIT_SCALE: Dict[str, Tuple[str, float]] = {
        "kw": ("w", 1000.0), "mw": ("w", 1_000_000.0), "w": ("w", 1.0),
        "kv": ("v", 1000.0), "v": ("v", 1.0), "ma": ("a", 0.001), "a": ("a", 1.0),
        "kpa": ("pa", 1000.0), "mpa": ("pa", 1_000_000.0), "bar": ("pa", 100_000.0),
        "mbar": ("pa", 100.0), "pa": ("pa", 1.0),
        "km": ("m", 1000.0), "m": ("m", 1.0), "cm": ("m", 0.01), "mm": ("m", 0.001),
        "kg": ("kg", 1.0), "g": ("kg", 0.001), "t": ("kg", 1000.0),
        "l": ("l", 1.0), "ml": ("l", 0.001),
        "kwh": ("wh", 1000.0), "mwh": ("wh", 1_000_000.0),
        "mb": ("b", 1e6), "gb": ("b", 1e9), "tb": ("b", 1e12),
    }

    def read(self, text: str) -> Dict[str, float]:
        """Extract normalised specifications, keyed by physical quantity."""
        if not text:
            return {}
        specifications: Dict[str, float] = {}

        for prefix, number in self._DESIGNATIONS.findall(text):
            try:
                specifications[f"designation:{prefix.lower()}"] = float(number)
            except ValueError:
                continue

        for magnitude, unit in self._MEASURES.findall(text):
            value = parse_amount(magnitude)
            if value is None:
                continue
            unit_key = unit.lower().replace('"', "inch").replace("'", "ft")
            base, scale = self._UNIT_SCALE.get(unit_key, (unit_key, 1.0))
            key = f"measure:{base}"
            # Keep the largest reading for a quantity. Descriptions frequently
            # carry a packaging figure alongside the rating, and the rating is
            # almost always the larger of the two.
            specifications[key] = max(specifications.get(key, 0.0), value * scale)
        return specifications

    def model_codes(self, text: str) -> Set[str]:
        """Manufacturer model designations present in a description.

        Measurements are removed before the search rather than filtered
        afterwards. Left in, "DN50" and "1.5mm2" are picked up as model codes,
        and since a shared code lifts a match almost to certainty, two unrelated
        DN50 fittings would be declared the same part. A dimension is evidence
        about size, which the specification comparison already uses properly;
        it is not evidence of identity.
        """
        stripped = self._MEASURES.sub(" ", self._DESIGNATIONS.sub(" ", text))

        codes = set()
        for token in self._MODEL_CODE.findall(stripped):
            compact = compact_key(token)
            # A genuine designation carries both letters and digits and is long
            # enough to be distinctive: this admits "CH520" and "MZK135" while
            # rejecting the "3" of "Model 3" and the "A4" of a paper size.
            letters = sum(1 for char in compact if char.isalpha())
            digits = sum(1 for char in compact if char.isdigit())
            if len(compact) >= 4 and letters >= 2 and digits >= 2:
                codes.add(compact)
        return codes

    @classmethod
    def agreement(cls, left: Dict[str, float], right: Dict[str, float]) -> Optional[bool]:
        """Whether two specification sets agree.

        Returns None when they cannot be compared, which is the common case and
        must not be confused with disagreement. Only quantities that *both*
        descriptions state are examined; a catalogue that records a rating the
        purchase line omits says nothing either way.
        """
        shared = set(left) & set(right)
        if not shared:
            return None
        for key in shared:
            left_value, right_value = left[key], right[key]
            scale = max(abs(left_value), abs(right_value), 1e-9)
            # Five per cent absorbs rounding and unit conversion without
            # admitting a genuinely different size.
            if abs(left_value - right_value) / scale > 0.05:
                return False
        return True


# ===========================================================================
# Type analysis
# ===========================================================================

class TypeAnalyser:
    """Decides whether two descriptions denote the same kind of thing.

    Head-noun comparison, performed on English text only. English noun phrases
    are head-final, so the last content word of a phrase is the thing itself and
    everything before it qualifies: in "wireless bluetooth headphones" the item
    is headphones. That regularity does not survive translation - Polish is
    head-initial - which is precisely why reference descriptions are rendered
    into English before this class ever sees them.
    """

    _STOPWORDS = {
        "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at", "by",
        "with", "from", "as", "is", "are", "per", "pcs", "each", "new", "used",
        "other", "misc", "various", "general", "total", "incl", "excl", "set",
    }

    # Words that occupy the head position without naming anything. "Removal
    # service" is a removal, not a service, and taking "service" as its type
    # makes every service in the data look like the same kind of thing.
    _EMPTY_HEADS = frozenset({
        "service", "services", "work", "works", "job", "jobs", "supply",
        "supplies", "item", "items", "product", "products", "goods", "delivery",
        "cost", "costs", "fee", "fees", "charge", "charges", "material",
        "materials", "equipment", "unit", "units", "set", "sets", "package",
    })

    def __init__(self, lexicon: Optional[Lexicon] = None) -> None:
        self.lexicon = lexicon or Lexicon()
        self._pipeline: Optional[Any] = None
        self._loaded = False
        self._cache: Dict[str, Tuple[str, frozenset]] = {}
        self.stopwords = self._load_stopwords()

    def _load_stopwords(self) -> Set[str]:
        words = set(self._STOPWORDS)
        if _nltk is not None:
            try:
                from nltk.corpus import stopwords
                words |= set(stopwords.words("english"))
            except Exception:
                pass
        return words

    def _load_pipeline(self) -> Optional[Any]:
        if self._loaded:
            return self._pipeline
        self._loaded = True
        if _spacy is None:
            return None
        for model_name in ("en_core_web_sm", "en_core_web_md"):
            try:
                self._pipeline = _spacy.load(model_name, disable=["ner", "textcat"])
                LOGGER.info("Type analysis using spaCy %s.", model_name)
                return self._pipeline
            except Exception:
                continue
        LOGGER.info("No spaCy English model installed; using positional head detection.")
        return None

    def analyse(self, text: str) -> Tuple[str, frozenset]:
        """Return the head noun and the full set of content words."""
        key = lookup_key(text)
        if key in self._cache:
            return self._cache[key]
        if not key:
            return "", frozenset()

        words = [word for word in tokenise(key)
                 if word not in self.stopwords and not is_code_token(word)]
        content = frozenset(words)

        head = ""
        pipeline = self._load_pipeline()
        if pipeline is not None:
            try:
                document = pipeline(text)
                chunks = list(document.noun_chunks)
                if chunks:
                    head = chunks[0].root.lemma_.lower() or chunks[0].root.text.lower()
                else:
                    nouns = [token for token in document if token.pos_ in {"NOUN", "PROPN"}]
                    if nouns:
                        head = (nouns[-1].lemma_ or nouns[-1].text).lower()
            except Exception:
                head = ""

        if not head and words:
            # Positional fallback: the last content word of an English phrase.
            head = words[-1]

        # A head that names no thing is stepped over in favour of the word
        # before it, so that "asbestos removal service" is typed as a removal
        # rather than as a service. This has to come after the fallback above,
        # since that is what supplies the head when no parser is installed.
        if head in self._EMPTY_HEADS:
            substantive = [word for word in words if word not in self._EMPTY_HEADS]
            if substantive:
                head = substantive[-1]

        result = (head, content)
        self._cache[key] = result
        return result

    def _is_service(self, content: frozenset) -> bool:
        """Whether a description names an activity rather than an object."""
        return bool(content & self.lexicon.service_markers)

    def compatible(self, left: str, right: str) -> bool:
        """Whether two descriptions could denote the same kind of thing.

        Deliberately permissive about *which* word carries the type, because
        head detection is imperfect: agreement is accepted when the heads match
        outright, when either head appears anywhere in the other description, or
        when the two share enough content words that the disagreement is more
        likely a parsing artefact than a real difference.

        Services are held to a different rule, and that distinction is the point
        of this method rather than an exception to it. An object is identified by
        what it *is*, so "wireless headphones" and "wireless keyboard" share a
        modifier and are still not substitutes. An activity is identified by what
        it is performed *on*, and the verb naming it varies freely: "asbestos
        removal", "asbestos demolition" and "asbestos disposal" are one service.
        Applying the object rule to services rejects exactly the equivalences
        this agent exists to find.
        """
        left_head, left_content = self.analyse(left)
        right_head, right_content = self.analyse(right)

        if not left_head or not right_head:
            return True  # nothing to disagree about
        if left_head == right_head:
            return True
        if left_head in right_content or right_head in left_content:
            return True

        overlap = left_content & right_content

        if self._is_service(left_content) and self._is_service(right_content):
            # Both name activities, so the comparison is made on what each is
            # performed on rather than on the words as a whole. Tank cleaning
            # and pipe cleaning share two of three words and are not the same
            # service; asbestos removal and asbestos demolition share one of
            # three and are.
            left_subject = left_content - self.lexicon.service_markers
            right_subject = right_content - self.lexicon.service_markers

            if left_subject and right_subject:
                return bool(left_subject & right_subject)

            # Neither states a subject, so both are bare activity names. Compare
            # the activities themselves, ignoring the words that merely mark
            # something as a service at all.
            if not left_subject and not right_subject:
                return bool(overlap - self._EMPTY_HEADS)
            return False

        smaller = min(len(left_content), len(right_content))
        return bool(smaller) and len(overlap) / smaller >= 0.6


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
        self._reasoning_style = "effort"

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
# Input
# ===========================================================================

PURCHASE_DESCRIPTION_COLUMN = "Enriched_Purchase_Description"
PURCHASE_GROUP_COLUMN = "AI_Purchase_Group_L5"

# Fortum asked that a catalogue match be made against the best original
# description rather than the generalised English sentence, because it is the
# original that carries the specifics identifying the exact catalogue product.
# Tried in order; the enriched sentence is the fallback when none is populated.
MATCH_TEXT_COLUMNS: Tuple[str, ...] = (
    "Original_Description",
    "PO_Line_Description",
    "Invoice_Line_Text",
    "Invoice_Article_Name",
    PURCHASE_DESCRIPTION_COLUMN,
)

# Placeholders that mean the field is empty, whatever the source system wrote.
EMPTY_MARKERS = frozenset({
    "n/a", "na", "n.a.", "none", "null", "nil", "nan", "-", "--", "?",
    "unknown", "not defined", "no description", "tbd",
})

ITEM_TYPE_COLUMN = "Item_Type"
# Maximo's ITEMNUM, isolated by Agent 1 from the general-purpose item key. A
# Basware supplier product code is an item number too and must not be read as
# evidence that a line came off a catalogue.
STOCK_NUMBER_COLUMN = "Stock_Item_Number"
ITEM_NUMBER_COLUMN = "Item_Number"

# Basware item types that mean the line was raised against a catalogue and is
# therefore already a standard purchase.
CATALOGUE_ITEM_TYPES = frozenset({
    "external webshop", "externalwebshop", "external web shop",
    "market place", "marketplace",
})

# The third value Fortum asked for on Potential_Standard_Match. A line that is
# already a catalogue purchase is neither a standardisation opportunity nor a
# failed match, so it says so rather than answering Yes or No.
ALREADY_STANDARD = "Already standard/catalogue purchase"

# Everything that describes a proposed catalogue match. Fortum asked that these
# be blank whenever Potential_Standard_Match is not Yes, so that the file cannot
# be read as proposing a match it did not make.
MATCHED_COLUMNS: Tuple[str, ...] = (
    "Matched_Item_ID", "Matched_Item_Description", "Matched_Item_Supplier",
    "Matched_Item_Source", "Matched_Item_Unit_Price", "Similarity_Score",
    "AI_Confidence", "Match_Band", "Match_Method", "Match_Rationale",
    "Type_Compatible", "Specification_Agreement", "Price_Difference_Percent",
    "Alternative_Matches",
)


def is_populated(value: str) -> bool:
    """True when a field carries a real value rather than a placeholder."""
    cleaned = normalise_text(value)
    return bool(cleaned) and cleaned.lower().strip(" .") not in EMPTY_MARKERS


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


def _read_xlsx(path: Path) -> List[InputTable]:
    if _openpyxl is None:
        LOGGER.error("Reading %s needs openpyxl (pip install openpyxl).", path.name)
        return []
    tables: List[InputTable] = []
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
            tables.append(InputTable(headers=headers, rows=rows, path=path))
    finally:
        workbook.close()
    return tables


def read_purchase_table(path: Path) -> InputTable:
    """Load the purchase lines produced by Agents 1 and 2."""
    if not path.is_file():
        raise SystemExit(
            f"Input file not found: {path}\n"
            "Agent 3 consumes the table written by Agent 2 (or, failing that, Agent 1). "
            "Run those first, or point --input at an equivalent table.")

    if path.suffix.lower() in {".csv", ".tsv", ".txt"}:
        table = _read_csv(path)
    else:
        tables = _read_xlsx(path)
        if not tables:
            raise SystemExit(f"No readable sheet in {path}")
        table = tables[0]

    if PURCHASE_DESCRIPTION_COLUMN not in table.headers:
        raise SystemExit(
            f"{path.name} has no '{PURCHASE_DESCRIPTION_COLUMN}' column.\n"
            "That column is produced by Agent 1 and is what this agent matches on.")
    LOGGER.info("Loaded %d purchase line(s) from %s", len(table.rows), path.name)
    return table


# ===========================================================================
# Reference data
# ===========================================================================

@dataclass
class ReferenceItem:
    """One catalogue, price-list or standard-item entry."""

    item_id: str
    description: str
    english_description: str
    supplier: str = ""
    unit_price: Optional[float] = None
    currency: str = ""
    source_file: str = ""
    language: str = "und"
    codes: Set[str] = field(default_factory=set)
    specifications: Dict[str, float] = field(default_factory=dict)

    @property
    def comparison_text(self) -> str:
        """The text every similarity view is computed over."""
        return self.english_description or self.description


# Header spellings that identify each field of a reference file. Reference data
# is supplied by third parties in whatever shape they keep it, so this is matched
# loosely and backed by a content-based fallback.
_REFERENCE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "item_id": ("itemcode", "itemid", "itemnumber", "articleid", "articlenumber",
                "productcode", "materialnumber", "sku", "code", "partnumber", "id"),
    "description": ("itemdescription", "description", "itemname", "articlename",
                    "productname", "name", "text", "materialdescription", "servicename"),
    "supplier": ("supplier", "suppliername", "vendor", "vendorname", "manufacturer",
                 "brand", "contractpartner"),
    "unit_price": ("unitprice", "price", "listprice", "netprice", "contractprice",
                   "priceexclvat", "unitcost"),
    "currency": ("currency", "currencycode", "priceccy"),
}


def normalise_column(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold_accents(normalise_text(name)).lower())


# A catalogue lists what may be bought. A transaction records what was bought,
# and so carries the identity of a document and the amounts and dates of one
# event. Those columns never appear in a catalogue, and finding them is how a
# purchase extract is told apart from the price list it should be matched
# against. Fortum's own extracts sit in the same folder tree as the catalogues,
# and matching purchases against other purchases produced confident nonsense:
# "Int UK Delivery Costs" was reported as matching a standard item called
# "Delivery", taken from a Basware purchase order file.
_TRANSACTION_MARKERS: Tuple[str, ...] = (
    # identity of a document or one of its lines
    "sourcerowid", "datasource", "documentnumber", "documentlinenumber",
    "documentlinedesc", "documentidentifier", "ordernumber", "ponum", "polinenum",
    "polinenumber", "polinedesc", "requisitionnumber", "invoicenumber",
    "invoicekey", "invoiceid", "invoicelink",
    # when the event happened
    "postingdate", "invoicedate", "duedate", "paymentdate", "orderdate",
    "pocreationdate", "prcreationdate", "exchangedate",
    # what the event cost, as opposed to what the item lists at
    "glaccount", "spendin", "spendineur", "rowtotal", "vatamount", "vatrate",
    "linecost", "loadedcost", "totalcost", "quantitycharged", "quantitydelivered",
    "orderqty", "receipts", "exchangerate",
    # who handled the event, and what state it reached
    "orderstatus", "orderlinestatus", "requisitionstatus", "costcenter",
    "pocreator", "prcreator", "purchaseagent", "prowner",
)

# One marker could be a coincidence in a catalogue that happens to carry, say, a
# validity date. Two is a purchase extract. Every file Fortum has sent so far
# carries far more than two, and the demo catalogue carries none.
_TRANSACTION_MARKER_LIMIT = 2

# Words that make a "supplier" column the name of a product rather than the name
# of a company: "Supplier product name" describes an item, "ERP supplier name"
# describes a party.
_PRODUCT_WORDS: Tuple[str, ...] = ("product", "item", "article", "material",
                                   "service", "goods", "part")


def transaction_markers(headers: Sequence[str]) -> List[str]:
    """Which purchase-transaction columns a table carries."""
    keys = {normalise_column(header) for header in headers}
    keys.discard("")
    found = [marker for marker in _TRANSACTION_MARKERS
             if any(marker in key for key in keys)]
    return found


def names_a_party(header: str) -> bool:
    """Whether a column holds a company name rather than an item description."""
    key = normalise_column(header)
    if not any(token in key for token in ("supplier", "vendor", "creditor",
                                          "manufacturer", "customer")):
        return False
    return not any(word in key for word in _PRODUCT_WORDS)


def _resolve_reference_columns(headers: Sequence[str]) -> Dict[str, str]:
    """Map reference fields onto the columns of one file."""
    by_key = {}
    for header in headers:
        key = normalise_column(header)
        if key and key not in by_key:
            by_key[key] = header

    resolved: Dict[str, str] = {}
    for field_name, aliases in _REFERENCE_ALIASES.items():
        for alias in aliases:
            if alias in by_key:
                resolved[field_name] = by_key[alias]
                break
        if field_name in resolved:
            continue
        # Substring match, longest alias first so that "unitprice" is preferred
        # over "price" when a file happens to carry both.
        for alias in sorted(aliases, key=len, reverse=True):
            match = next((header for key, header in sorted(by_key.items())
                          if alias in key), None)
            if match:
                resolved[field_name] = match
                break
    return resolved


class ReferenceLibrary:
    """Every catalogue, price list and standard-item file, loaded and prepared.

    Reference files are discovered recursively and read whatever their format,
    because the client has said there will be several of them, in several
    languages, and no fixed layout was ever agreed.
    """

    def __init__(self, lexicon: Lexicon, translator: NeuralTranslator,
                 reader: SpecificationReader) -> None:
        self.lexicon = lexicon
        self.translator = translator
        self.reader = reader
        self.items: List[ReferenceItem] = []
        self.by_code: Dict[str, List[ReferenceItem]] = defaultdict(list)
        self.files_read: List[str] = []
        self.skipped_files: List[str] = []
        self.rejected_files: List[str] = []
        self.unreadable_files: List[str] = []

        # What each catalogue actually was, file by file. Fortum's instruction is
        # to check the catalogue is the client's latest before a run, and a name
        # and an item count cannot answer that: two files with the same name and
        # the same number of rows can differ. The modification date says whether
        # the file was refreshed, and the digest says whether the contents moved.
        self.sources: List[Dict[str, Any]] = []

    def load(self, *roots: Path) -> None:
        """Read every usable catalogue beneath one or more folders."""
        seen_paths: Set[Path] = set()
        paths: List[Path] = []
        for root in roots:
            if not root.exists():
                LOGGER.warning("Catalogue folder %s does not exist.", root)
                continue
            found = ([root] if root.is_file() else
                     sorted(path for path in root.rglob("*")
                            if path.is_file()
                            and not path.name.startswith((".", "~$"))
                            and path.suffix.lower() in {".csv", ".tsv", ".txt",
                                                        ".xlsx", ".xlsm"}))
            for path in found:
                resolved = path.resolve()
                if resolved not in seen_paths:
                    seen_paths.add(resolved)
                    paths.append(path)

        if not paths:
            LOGGER.warning("No catalogue files were found; matching will be skipped.")

        for path in paths:
            spreadsheet = path.suffix.lower() not in {".csv", ".tsv", ".txt"}
            if spreadsheet and _openpyxl is None:
                # Silence here is dangerous: the file is a catalogue the client
                # sent, and dropping it without a word makes an incomplete run
                # look like a complete one.
                self.unreadable_files.append(
                    f"{path.name} (needs openpyxl; install it and run again)")
                LOGGER.error("Cannot read %s: openpyxl is not installed.", path.name)
                continue
            try:
                tables = [_read_csv(path)] if not spreadsheet else _read_xlsx(path)
            except Exception as error:                      # noqa: BLE001
                self.unreadable_files.append(f"{path.name} ({type(error).__name__})")
                LOGGER.error("Cannot read %s (%s).", path.name, error)
                continue
            if not tables:
                self.unreadable_files.append(f"{path.name} (no readable sheet)")
                LOGGER.error("No readable sheet in %s.", path.name)
                continue

            for table in tables:
                refusal = self._refuse(table)
                if refusal:
                    self.rejected_files.append(f"{path.name} ({refusal})")
                    LOGGER.warning("Not treating %s as a catalogue: %s.", path.name, refusal)
                    continue
                added = self._absorb(table)
                if added:
                    self.files_read.append(f"{path.name} ({added} item(s))")
                    self.sources.append(self._describe(path, added))
                else:
                    self.skipped_files.append(path.name)

        self._render_english()
        self._index()
        LOGGER.info("Catalogue holds %d item(s) from %d file(s); "
                    "%d file(s) refused, %d unreadable.",
                    len(self.items), len(self.files_read),
                    len(self.rejected_files), len(self.unreadable_files))
        if not self.items:
            # Not fatal - the agent still reports what is already standard and
            # still nominates candidates - but every proposed match is now
            # impossible, so the reason has to be unmissable.
            LOGGER.error("No catalogue item was loaded, so no match can be "
                         "proposed. Check --catalogues points at the client's "
                         "item catalogue.")

    @staticmethod
    def _describe(path: Path, items: int) -> Dict[str, Any]:
        """Identify one catalogue file well enough to tell two versions apart."""
        try:
            stat = path.stat()
            modified = datetime.datetime.fromtimestamp(
                stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            size = stat.st_size
        except OSError:
            modified, size = "", 0
        digest = ""
        try:
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    hasher.update(block)
            digest = hasher.hexdigest()[:12]
        except OSError:
            pass
        return {"file": path.name, "items": items, "modified": modified,
                "bytes": size, "sha256": digest}

    def _refuse(self, table: InputTable) -> str:
        """Why this table is not a catalogue, or an empty string if it is one.

        Refusing is safer than absorbing. An item wrongly left out of the
        catalogue costs a match that could have been proposed; a purchase
        extract wrongly absorbed proposes matches that do not exist and reports
        them with a confidence score.
        """
        markers = transaction_markers(table.headers)
        if len(markers) >= _TRANSACTION_MARKER_LIMIT:
            shown = ", ".join(markers[:4])
            more = f" and {len(markers) - 4} more" if len(markers) > 4 else ""
            return f"purchase transactions, not a catalogue: carries {shown}{more}"

        columns = _resolve_reference_columns(table.headers)
        description = columns.get("description")
        if description and names_a_party(description):
            return (f"its only description column, {description!r}, names a company "
                    f"rather than an item")
        return ""

    def _absorb(self, table: InputTable) -> int:
        """Turn one reference table into items, if it looks like reference data."""
        columns = _resolve_reference_columns(table.headers)
        if "description" not in columns:
            return 0

        added = 0
        seen: Set[str] = set()
        for index, row in enumerate(table.rows, start=2):
            description = normalise_text(row.get(columns["description"], ""))
            if not description or self.lexicon.is_noise(description):
                continue

            # Catalogue descriptions frequently carry a paragraph of delivery
            # boilerplate after the item name. Only the first line names the
            # item, and keeping the rest would swamp every similarity measure.
            description = description.split(" | ")[0].strip()
            if len(description) > 200:
                description = description[:200].rsplit(" ", 1)[0]

            item_id = normalise_text(row.get(columns.get("item_id", ""), "")) or f"ROW{index}"
            supplier = normalise_text(row.get(columns.get("supplier", ""), ""))

            identity = stable_hash(table.path.name, supplier, item_id, lookup_key(description))
            if identity in seen:
                continue
            seen.add(identity)

            self.items.append(ReferenceItem(
                item_id=item_id,
                description=description,
                english_description="",
                supplier=supplier,
                unit_price=parse_amount(row.get(columns.get("unit_price", ""), "")),
                currency=normalise_text(row.get(columns.get("currency", ""), "")),
                source_file=table.path.name,
            ))
            added += 1
        return added

    def _render_english(self) -> None:
        """Translate every reference description into English.

        Batched by language so that each translation model is loaded once, and
        applied only where the vocabulary alone left too much unresolved. The
        purchase side is already English, so this is what puts both sides of
        every comparison into the same language.
        """
        pending: Dict[str, List[ReferenceItem]] = defaultdict(list)

        for item in self.items:
            item.language = detect_language(item.description)
            rendered, coverage = self.lexicon.translate_surface(item.description)
            item.english_description = rendered

            if item.language not in {"en", "und"} and coverage < 0.85:
                pending[item.language].append(item)

        if self.translator.enabled:
            for language in sorted(pending):
                items = pending[language]
                if not self.translator.available_for(language):
                    continue
                texts = sorted({item.description for item in items})
                LOGGER.info("Translating %d reference description(s) from %s.",
                            len(texts), language)
                translations = self.translator.translate_batch(texts, language)
                for item in items:
                    translated = translations.get(item.description)
                    if translated:
                        item.english_description = translated

        for item in self.items:
            if not item.english_description:
                item.english_description = item.description
            item.specifications = self.reader.read(item.english_description or item.description)
            item.codes = (self.reader.model_codes(item.description)
                          | ({compact_key(item.item_id)} if len(compact_key(item.item_id)) >= 4
                             else set()))

    def _index(self) -> None:
        """Index items by every code they can be found by."""
        for item in self.items:
            for code in item.codes:
                self.by_code[code].append(item)


# ===========================================================================
# Matching
# ===========================================================================

@dataclass
class MatchCandidate:
    """One possible standard item for one purchase description."""

    item: ReferenceItem
    score: float
    semantic: float = 0.0
    lexical: float = 0.0
    code_match: bool = False
    type_compatible: bool = True
    spec_agreement: Optional[bool] = None
    method: str = "similarity"
    rationale: str = ""


class MatchEngine:
    """Scores purchase descriptions against the reference library.

    Retrieval and scoring are separated. Retrieval narrows a hundred thousand
    catalogue items to a handful of plausible ones cheaply, using embeddings
    where they are available and character n-grams where they are not. Scoring
    then examines those few closely, applying the constraints that decide
    equivalence rather than resemblance.

    Doing it in that order is what makes the agent tractable: the expensive
    reasoning runs on a handful of pairs per description rather than on the
    Cartesian product.
    """

    EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    SEMANTIC_WEIGHT = 0.60
    LEXICAL_WEIGHT = 0.40
    # Retrieval casts a wider net than scoring keeps, because the constraint
    # layers can only reject candidates, never introduce them.
    RETRIEVAL_MULTIPLIER = 4

    def __init__(self, library: ReferenceLibrary, settings: Settings,
                 types: TypeAnalyser, reader: SpecificationReader) -> None:
        self.library = library
        self.settings = settings
        self.types = types
        self.reader = reader

        self.use_embeddings = (settings.use_embeddings
                               and _sentence_transformers is not None
                               and _numpy is not None)
        self._embedder: Optional[Any] = None
        self._reference_vectors: Optional[Any] = None
        self._neighbours: Optional[Any] = None

        self._lexical_vectoriser: Optional[Any] = None
        self._lexical_matrix: Optional[Any] = None

    # -- index construction -------------------------------------------------

    def build(self) -> None:
        """Prepare the retrieval structures over the reference library."""
        if not self.library.items:
            return
        texts = [item.comparison_text for item in self.library.items]

        if self.use_embeddings:
            try:
                LOGGER.info("Loading embedding model %s ...", self.EMBEDDING_MODEL)
                self._embedder = load_sentence_transformer(
                    _sentence_transformers, self.EMBEDDING_MODEL)
                LOGGER.info("Embedding %d reference item(s) ...", len(texts))
                self._reference_vectors = self._embedder.encode(
                    texts, batch_size=64, convert_to_numpy=True,
                    normalize_embeddings=True, show_progress_bar=False)
                if _sklearn_neighbors is not None:
                    # Brute force over unit vectors is exact and, at catalogue
                    # scale, faster than building a tree.
                    self._neighbours = _sklearn_neighbors.NearestNeighbors(
                        n_neighbors=min(len(texts),
                                        self.settings.top_k * self.RETRIEVAL_MULTIPLIER),
                        metric="cosine", algorithm="brute")
                    self._neighbours.fit(self._reference_vectors)
            except Exception as error:
                LOGGER.warning("Embedding index unavailable (%s).", error)
                self.use_embeddings = False
                self._embedder = self._reference_vectors = self._neighbours = None

        if _sklearn_text is not None:
            try:
                self._lexical_vectoriser = _sklearn_text.TfidfVectorizer(
                    analyzer="char_wb", ngram_range=(3, 5), lowercase=True, sublinear_tf=True)
                self._lexical_matrix = self._lexical_vectoriser.fit_transform(
                    [lookup_key(text) for text in texts])
            except ValueError:
                self._lexical_vectoriser = self._lexical_matrix = None

    # -- retrieval ----------------------------------------------------------

    def _retrieve(self, description: str, item_number: str) -> List[Tuple[int, float, float]]:
        """Shortlist reference items as ``(index, semantic, lexical)``."""
        shortlist: Dict[int, Tuple[float, float]] = {}
        limit = self.settings.top_k * self.RETRIEVAL_MULTIPLIER

        # An item number that appears in the catalogue is the strongest possible
        # signal and is never allowed to be crowded out by similarity.
        code = compact_key(item_number)
        if len(code) >= 4:
            for item in self.library.by_code.get(code, []):
                shortlist[id(item)] = shortlist.get(id(item), (0.0, 0.0))

        semantic_scores: Dict[int, float] = {}
        if self.use_embeddings and self._reference_vectors is not None:
            try:
                vector = self._embedder.encode([description], convert_to_numpy=True,
                                               normalize_embeddings=True,
                                               show_progress_bar=False)[0]
                if self._neighbours is not None:
                    distances, indices = self._neighbours.kneighbors(
                        vector.reshape(1, -1),
                        n_neighbors=min(limit, len(self.library.items)))
                    for distance, index in zip(distances[0], indices[0]):
                        semantic_scores[int(index)] = 1.0 - float(distance)
                else:
                    similarities = self._reference_vectors @ vector
                    top = _numpy.argsort(-similarities)[:limit]
                    for index in top:
                        semantic_scores[int(index)] = float(similarities[int(index)])
            except Exception:
                pass

        lexical_scores: Dict[int, float] = {}
        if self._lexical_matrix is not None:
            try:
                query = self._lexical_vectoriser.transform([lookup_key(description)])
                similarities = (self._lexical_matrix @ query.T).toarray().ravel()
                order = _numpy.argsort(-similarities)[:limit] if _numpy is not None else \
                    sorted(range(len(similarities)), key=lambda i: -similarities[i])[:limit]
                for index in order:
                    lexical_scores[int(index)] = float(similarities[int(index)])
            except Exception:
                pass

        if not semantic_scores and not lexical_scores:
            return self._retrieve_by_scan(description, limit)

        candidates = set(semantic_scores) | set(lexical_scores)
        results = [(index, semantic_scores.get(index, 0.0), lexical_scores.get(index, 0.0))
                   for index in sorted(candidates)]
        # Sorted by the blended value so that the caller sees the best first,
        # and by index on ties so the order is reproducible.
        results.sort(key=lambda entry: (-(entry[1] * self.SEMANTIC_WEIGHT
                                          + entry[2] * self.LEXICAL_WEIGHT), entry[0]))
        return results[:limit]

    def _retrieve_by_scan(self, description: str, limit: int) -> List[Tuple[int, float, float]]:
        """Shortlist by direct comparison, when no index could be built.

        Linear in the catalogue size for every distinct description. Acceptable
        only as a fallback, and warned about at start-up.
        """
        key = lookup_key(description)
        scored = [(index, 0.0, text_similarity(key, lookup_key(item.comparison_text)))
                  for index, item in enumerate(self.library.items)]
        scored.sort(key=lambda entry: (-entry[2], entry[0]))
        return scored[:limit]

    # -- scoring ------------------------------------------------------------

    def match(self, description: str, item_number: str = "",
              unit_price: Optional[float] = None) -> List[MatchCandidate]:
        """Return the best reference items for one purchase description."""
        if not description or not self.library.items:
            return []

        purchase_specs = self.reader.read(description)
        purchase_codes = self.reader.model_codes(description)
        if len(compact_key(item_number)) >= 4:
            purchase_codes.add(compact_key(item_number))

        candidates: List[MatchCandidate] = []
        for index, semantic, lexical in self._retrieve(description, item_number):
            item = self.library.items[index]

            # Re-normalise the blend when one view is missing, so that a
            # catalogue without embeddings is not systematically scored lower.
            weights = []
            if semantic > 0.0:
                weights.append((semantic, self.SEMANTIC_WEIGHT))
            if lexical > 0.0:
                weights.append((lexical, self.LEXICAL_WEIGHT))
            if weights:
                total = sum(weight for _, weight in weights)
                base = sum(value * weight for value, weight in weights) / total
            else:
                base = 0.0

            shared_codes = purchase_codes & item.codes
            type_compatible = self.types.compatible(description, item.comparison_text)
            spec_agreement = SpecificationReader.agreement(purchase_specs, item.specifications)

            score = base
            method = "similarity"
            reasons: List[str] = []

            if shared_codes:
                # A shared manufacturer or item code is close to proof. The
                # score is lifted to the accept band rather than set to 1.0, so
                # a coincidental code collision still has to survive the
                # constraints below.
                score = max(score, 0.92)
                method = "code"
                reasons.append(f"shared item code {sorted(shared_codes)[0]}")

            if not type_compatible:
                score *= self.settings.type_mismatch_penalty
                reasons.append("different type of item")

            if spec_agreement is False:
                score *= self.settings.spec_conflict_penalty
                reasons.append("conflicting specification")
            elif spec_agreement is True:
                # Agreement on a stated rating is real evidence, but it can only
                # confirm a match the text already supports.
                score = min(1.0, score + 0.05)
                reasons.append("matching specification")

            if score < self.settings.report_threshold:
                continue

            if not reasons:
                reasons.append("wording and meaning align")

            candidates.append(MatchCandidate(
                item=item, score=round(min(1.0, max(0.0, score)), 4),
                semantic=round(semantic, 4), lexical=round(lexical, 4),
                code_match=bool(shared_codes), type_compatible=type_compatible,
                spec_agreement=spec_agreement, method=method,
                rationale="; ".join(reasons),
            ))

        candidates.sort(key=lambda candidate: (-candidate.score, candidate.item.item_id))
        return candidates[: self.settings.top_k]

    def band(self, score: float) -> str:
        if score >= self.settings.high_threshold:
            return "High"
        if score >= self.settings.medium_threshold:
            return "Medium"
        return "Low" if score >= self.settings.minimum_threshold else "None"


# ===========================================================================
# Catalogue candidates
# ===========================================================================

@dataclass
class CandidateProfile:
    """The buying pattern of one recurring free-text purchase."""

    description: str
    purchase_group: str
    category: str
    occurrences: int = 0
    total_spend: float = 0.0
    suppliers: Set[str] = field(default_factory=set)
    unit_prices: List[float] = field(default_factory=list)
    quantities: List[float] = field(default_factory=list)
    periods: Set[str] = field(default_factory=set)
    already_standard: bool = False

    @property
    def price_stability(self) -> Optional[float]:
        """How tightly the unit price clusters, in ``[0, 1]``.

        A stable price is the clearest sign that a purchase is a defined item
        rather than a bespoke job, which is exactly what makes it listable.
        Returns None when too few prices are recorded to say anything.
        """
        prices = [price for price in self.unit_prices if price and price > 0]
        if len(prices) < 2:
            return None
        mean = statistics.fmean(prices)
        if mean <= 0:
            return None
        spread = statistics.pstdev(prices) / mean
        return round(max(0.0, 1.0 - spread), 3)


class CatalogueCandidateDetector:
    """Finds recurring free-text purchases that ought to be catalogue items.

    This answers the forward-looking half of the specification, and it needs no
    reference data: whether something should be listed is a property of how it
    is bought, not of whether it already appears somewhere. Frequency,
    materiality and price stability are combined into a single score, and every
    contributing factor is reported so that the judgement can be argued with.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.profiles: Dict[str, CandidateProfile] = {}

    def observe(self, description: str, purchase_group: str, category: str,
                supplier: str, spend: Optional[float], unit_price: Optional[float],
                quantity: Optional[float], period: str, already_standard: bool) -> None:
        """Fold one purchase line into its profile."""
        if not description:
            return
        key = lookup_key(description)
        profile = self.profiles.get(key)
        if profile is None:
            profile = CandidateProfile(description=description,
                                       purchase_group=purchase_group, category=category)
            self.profiles[key] = profile

        profile.occurrences += 1
        if spend is not None:
            profile.total_spend += spend
        if supplier:
            profile.suppliers.add(supplier)
        if unit_price is not None and unit_price > 0:
            profile.unit_prices.append(unit_price)
        if quantity is not None and quantity > 0:
            profile.quantities.append(quantity)
        if period:
            profile.periods.add(period)
        if already_standard:
            profile.already_standard = True

    def score(self, profile: CandidateProfile) -> Tuple[int, str, List[str]]:
        """Rate one profile as a catalogue candidate."""
        reasons: List[str] = []

        if profile.occurrences < self.settings.candidate_min_occurrences:
            return 0, "No", ["bought too rarely to justify a catalogue entry"]

        # Frequency saturates: after about twenty repeats, buying it more often
        # says nothing further about whether it belongs in a catalogue.
        frequency_factor = min(1.0, math.log1p(profile.occurrences) / math.log(21))
        reasons.append(f"bought {profile.occurrences} times")

        spend_factor = 0.0
        if profile.total_spend >= self.settings.candidate_min_spend:
            spend_factor = min(1.0, math.log1p(profile.total_spend)
                               / math.log1p(self.settings.candidate_min_spend * 50))
            reasons.append(f"total spend {profile.total_spend:,.0f}")

        stability = profile.price_stability
        stability_factor = 0.5 if stability is None else stability
        if stability is not None:
            if stability >= (1.0 - self.settings.candidate_price_tolerance):
                reasons.append(f"stable unit price (stability {stability:.2f})")
            else:
                reasons.append(f"variable unit price (stability {stability:.2f})")

        # Repeat purchasing across several periods separates a standing need
        # from a single project that happened to be split over many lines.
        recurrence_factor = min(1.0, len(profile.periods) / 4.0)
        if len(profile.periods) > 1:
            reasons.append(f"recurs across {len(profile.periods)} periods")

        if len(profile.suppliers) > 1:
            reasons.append(f"sourced from {len(profile.suppliers)} suppliers")

        raw = (frequency_factor * 0.35 + spend_factor * 0.25
               + stability_factor * 0.25 + recurrence_factor * 0.15)
        score = int(round(max(0.0, min(1.0, raw)) * 100))

        if profile.already_standard:
            # A purchase that already matches a catalogue item is a compliance
            # question, not a catalogue-design question.
            reasons.append("an equivalent standard item already exists")
            score = min(score, 40)

        band = "Yes" if score >= 60 else "Review" if score >= 40 else "No"
        return score, band, reasons


# ===========================================================================
# Pipeline
# ===========================================================================

class Agent3:
    """Orchestrates the run."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lexicon = Lexicon.load(settings.lexicon_path)
        self.translator = NeuralTranslator(enabled=settings.use_neural_translation)
        self.reader = SpecificationReader()
        self.types = TypeAnalyser(self.lexicon)
        self.library = ReferenceLibrary(self.lexicon, self.translator, self.reader)
        self.engine = MatchEngine(self.library, settings, self.types, self.reader)
        self.detector = CatalogueCandidateDetector(settings)

        self.model: Optional[LanguageModelClient] = None
        if settings.model.enabled:
            self.model = LanguageModelClient(
                settings.model, settings.cache_dir / "agent3_model_cache.json",
                interactive=settings.interactive)

        self.table: Optional[InputTable] = None
        self.run_id = ""
        # Matching is done once per distinct description, then applied to every
        # row that carries it. On a million lines this is the difference between
        # minutes and days.
        self.matches: Dict[str, List[MatchCandidate]] = {}
        self.statistics: Counter = Counter()

    # -- matching -----------------------------------------------------------

    def match_descriptions(self) -> None:
        """Match every distinct purchase description against the library."""
        assert self.table is not None
        if not self.library.items:
            LOGGER.warning(
                "No reference items were loaded, so no matches can be proposed. "
                "The catalogue-candidate analysis below does not depend on them "
                "and will still be produced.")
            return

        distinct: Dict[str, str] = {}
        for row in self.table.rows:
            description, _ = self.match_text(row)
            if not description or self.lexicon.is_noise(description):
                continue
            key = lookup_key(description)
            if key not in distinct:
                distinct[key] = description

        LOGGER.info("Matching %d distinct purchase description(s) against %d reference item(s).",
                    len(distinct), len(self.library.items))
        self.engine.build()

        if not self.engine.use_embeddings and self.engine._lexical_matrix is None:
            LOGGER.warning(
                "Neither embeddings nor scikit-learn is available; matching will fall "
                "back to a direct scan of the catalogue for every description. Install "
                "sentence-transformers and scikit-learn before running at full volume.")

        for position, (key, description) in enumerate(sorted(distinct.items()), start=1):
            self.matches[key] = self.engine.match(description)
            if position % 2000 == 0:
                LOGGER.info("  matched %d / %d description(s)", position, len(distinct))

        self.statistics["distinct_descriptions"] = len(distinct)

    def adjudicate(self) -> None:
        """Ask the model about matches that sit on the accept threshold.

        Scoped to the band immediately around the decision point, because that
        is the only place an opinion changes an outcome. A match at 0.95 and a
        match at 0.30 are both already decided; spending a token on either would
        buy nothing. Answers are cached on the pair, so the cost is paid once
        for the life of the data.
        """
        if self.model is None or not self.model.config.enabled or not self.matches:
            return

        margin = self.settings.adjudication_margin
        low = self.settings.medium_threshold - margin
        high = self.settings.medium_threshold + margin

        borderline: List[Tuple[str, MatchCandidate]] = []
        for key in sorted(self.matches):
            candidates = self.matches[key]
            if candidates and low <= candidates[0].score <= high:
                borderline.append((key, candidates[0]))

        if not borderline:
            LOGGER.info("No matches fell in the adjudication band; the model was not needed.")
            return

        LOGGER.info("Asking %s to adjudicate %d borderline match(es).",
                    self.model.config.model, len(borderline))

        system_prompt = (
            "You judge whether an industrial or energy sector purchase could have "
            "been fulfilled by a catalogue item. The question is functional "
            "equivalence, not similar wording.\n"
            "Answer yes only when the catalogue item would genuinely serve the same "
            "purpose. Differences in wording, language or brand do not matter. "
            "Differences in size, rating, capacity or in what the item actually does "
            "matter absolutely.\n"
            'Reply with JSON: {"verdicts": {"<id>": {"equivalent": true|false, '
            '"reason": "<at most 12 words>"}}}.'
        )

        batch_size = max(1, self.model.config.batch_size)
        for start in range(0, len(borderline), batch_size):
            batch = borderline[start:start + batch_size]
            outstanding: List[Tuple[str, str, MatchCandidate]] = []

            for key, candidate in batch:
                pair = json.dumps([key, candidate.item.comparison_text], ensure_ascii=False)
                cache_key = self.model.cache_key("adjudicate", pair)
                cached = self.model.cached(cache_key)
                if cached is not None:
                    self._apply_verdict(candidate, cached)
                else:
                    outstanding.append((cache_key, key, candidate))

            if not outstanding:
                continue

            request = {"pairs": [
                {"id": str(index),
                 "purchase": key,
                 "catalogue_item": candidate.item.comparison_text,
                 "catalogue_supplier": candidate.item.supplier}
                for index, (_, key, candidate) in enumerate(outstanding)
            ]}
            response = self.model.complete_json(system_prompt,
                                                json.dumps(request, ensure_ascii=False))
            if not response:
                continue

            verdicts = response.get("verdicts") or {}
            if not isinstance(verdicts, dict):
                continue
            for index, (cache_key, _, candidate) in enumerate(outstanding):
                verdict = verdicts.get(str(index))
                if not isinstance(verdict, dict):
                    continue
                payload = json.dumps({
                    "equivalent": bool(verdict.get("equivalent")),
                    "reason": normalise_text(verdict.get("reason", ""))[:80],
                }, sort_keys=True)
                self.model.store(cache_key, payload)
                self._apply_verdict(candidate, payload)
                self.statistics["matches_adjudicated"] += 1

    def _apply_verdict(self, candidate: MatchCandidate, payload: str) -> None:
        """Apply a stored adjudication to a candidate.

        The verdict nudges the score across or away from the decision boundary
        rather than replacing it. Keeping the measured score visible underneath
        means the calibration file still reflects what the local stack computed,
        which is what the threshold work depends on.
        """
        try:
            verdict = json.loads(payload)
        except json.JSONDecodeError:
            return
        reason = normalise_text(verdict.get("reason", ""))
        if verdict.get("equivalent"):
            candidate.score = round(min(1.0, max(candidate.score,
                                                 self.settings.medium_threshold + 0.02)), 4)
            candidate.rationale = f"{candidate.rationale}; reviewed as equivalent"
        else:
            candidate.score = round(min(candidate.score,
                                        self.settings.medium_threshold - 0.02), 4)
            candidate.rationale = f"{candidate.rationale}; reviewed as not equivalent"
        if reason:
            candidate.rationale = f"{candidate.rationale} ({reason})"
        candidate.method = "adjudicated"

    # -- candidate profiling ------------------------------------------------

    def profile_candidates(self) -> None:
        """Build the buying profile of every distinct purchase description."""
        assert self.table is not None
        for row in self.table.rows:
            description = normalise_text(row.get(PURCHASE_DESCRIPTION_COLUMN, ""))
            if not description or self.lexicon.is_noise(description):
                continue

            # Profiling groups on the enriched English description, so that the
            # same purchase written five ways is recognised as recurring. The
            # match itself is looked up under the text it was made against.
            match_key, _ = self.match_text(row)
            best = self.matches.get(lookup_key(match_key), []) if match_key else []
            already_standard = (
                self.standard_item(row) == "Y"
                or (bool(best) and best[0].score >= self.settings.medium_threshold))

            posting_date = normalise_text(row.get("Posting_Date", ""))
            period = posting_date[:7] if len(posting_date) >= 7 else ""

            self.detector.observe(
                description=description,
                purchase_group=normalise_text(row.get(PURCHASE_GROUP_COLUMN, "")),
                category=normalise_text(row.get("Category_L2", ""))
                         or normalise_text(row.get("Category_L1", "")),
                supplier=normalise_text(row.get("Supplier_Name", "")),
                spend=parse_amount(row.get("Spend_EUR", "")),
                unit_price=parse_amount(row.get("Unit_Price", "")),
                quantity=parse_amount(row.get("Quantity", "")),
                period=period,
                already_standard=already_standard,
            )

    # -- row classification -------------------------------------------------

    def match_text(self, row: Dict[str, str]) -> Tuple[str, str]:
        """The text this row is matched against, and the column it came from.

        The original invoice, PO or Sievo description is preferred, because it
        names the specific product a catalogue entry has to be recognised as.
        The enriched English sentence is the fallback for a row whose original
        text is missing or a placeholder.
        """
        for column in MATCH_TEXT_COLUMNS:
            value = normalise_text(row.get(column, ""))
            if is_populated(value) and not self.lexicon.is_noise(value):
                return value, column
        return "", ""

    def standard_item(self, row: Dict[str, str]) -> str:
        """Whether the line was already raised as a standard catalogue purchase.

        The two source systems say so differently. Basware says it through the
        item type: a line raised from an external webshop or a marketplace came
        off a catalogue, and a free-text line did not, whatever supplier product
        code it happens to carry. Maximo has no item type and says it by
        carrying an ITEMNUM at all, because only a stocked item has one. The
        merged master table carries both and can be told either way.
        """
        source = normalise_text(row.get("Source_System", "")).lower()
        item_type = normalise_text(row.get(ITEM_TYPE_COLUMN, "")).lower().strip(" .")
        catalogue_type = item_type in CATALOGUE_ITEM_TYPES

        # On a Maximo extract the general item key *is* the ITEMNUM, so it is
        # accepted there and nowhere else.
        stock_number = row.get(STOCK_NUMBER_COLUMN, "")
        if not is_populated(stock_number) and "maximo" in source:
            stock_number = row.get(ITEM_NUMBER_COLUMN, "")
        has_stock_number = is_populated(stock_number)

        if "basware" in source:
            return "Y" if catalogue_type else "N"
        if "maximo" in source:
            return "Y" if has_stock_number else "N"
        return "Y" if catalogue_type or has_stock_number else "N"

    # -- output -------------------------------------------------------------

    def write(self) -> Dict[str, Any]:
        """Write every output file and return the run manifest."""
        assert self.table is not None
        results_dir = self.settings.results_dir
        results_dir.mkdir(parents=True, exist_ok=True)

        rows_path = results_dir / "agent3_standardisation.csv"
        jsonl_path = results_dir / "agent3_standardisation.jsonl"
        candidates_path = results_dir / "agent3_catalogue_candidates.csv"
        calibration_path = results_dir / "agent3_match_calibration.csv"

        appended = [
            "Standard_item", "Potential_Standard_Match", "Match_Source_Column",
            "Matched_Item_ID", "Matched_Item_Description",
            "Matched_Item_Supplier", "Matched_Item_Source", "Matched_Item_Unit_Price",
            "Similarity_Score", "AI_Confidence", "Match_Band", "Match_Method",
            "Match_Rationale", "Type_Compatible", "Specification_Agreement",
            "Price_Difference_Percent", "Alternative_Matches", "No_Match_Reason",
            "Agent3_Run_Id",
        ]
        headers = list(self.table.headers) + [name for name in appended
                                              if name not in self.table.headers]

        band_counts: Counter = Counter()
        best_scores: List[float] = []
        matched_rows = 0
        standard_rows = 0

        with rows_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            jsonl_handle = jsonl_path.open("w", encoding="utf-8") if self.settings.write_jsonl else None

            try:
                for row in self.table.rows:
                    output = dict(row)
                    description, match_column = self.match_text(row)
                    candidates = self.matches.get(lookup_key(description), []) if description else []
                    best = candidates[0] if candidates else None
                    standard = self.standard_item(row)
                    standard_rows += 1 if standard == "Y" else 0

                    band = "None"
                    if standard == "Y":
                        # Already bought off a catalogue, so there is nothing to
                        # standardise and no match to propose.
                        verdict = ALREADY_STANDARD
                    elif best is None or best.score < self.settings.minimum_threshold:
                        verdict = "No"
                    else:
                        band = self.engine.band(best.score)
                        verdict = ("Yes" if best.score >= self.settings.medium_threshold
                                   else "No")

                    output["Standard_item"] = standard
                    output["Potential_Standard_Match"] = verdict
                    output["Match_Source_Column"] = match_column

                    if verdict != "Yes":
                        # Fortum's rule: a row that is not proposing a match must
                        # not carry the traces of one, so every matched column is
                        # null. Why there is no match is still worth reading, and
                        # it goes in a column of its own rather than in a matched
                        # column that would then read as a match after all.
                        for column in MATCHED_COLUMNS:
                            output[column] = ""
                        output["No_Match_Reason"] = (
                            "already a standard catalogue purchase"
                            if standard == "Y" else
                            "best candidate below the reporting threshold" if best
                            else "no comparable standard item found")
                    else:
                        output["No_Match_Reason"] = ""
                        confidence = self._confidence(best)
                        purchase_price = parse_amount(row.get("Unit_Price", ""))
                        difference = self._price_difference(purchase_price, best.item.unit_price)

                        matched_rows += 1
                        band_counts[band] += 1
                        output.update({
                            "Matched_Item_ID": best.item.item_id,
                            "Matched_Item_Description": best.item.description,
                            "Matched_Item_Supplier": best.item.supplier,
                            "Matched_Item_Source": best.item.source_file,
                            "Matched_Item_Unit_Price": ("" if best.item.unit_price is None
                                                        else round(best.item.unit_price, 2)),
                            "Similarity_Score": round(best.score, 4),
                            "AI_Confidence": confidence,
                            "Match_Band": band,
                            "Match_Method": best.method,
                            "Match_Rationale": best.rationale,
                            "Type_Compatible": "Yes" if best.type_compatible else "No",
                            "Specification_Agreement": {True: "Agree", False: "Conflict",
                                                        None: "Not stated"}[best.spec_agreement],
                            "Price_Difference_Percent": "" if difference is None else difference,
                            "Alternative_Matches": " | ".join(
                                f"{candidate.item.item_id}:{candidate.score:.2f}"
                                for candidate in candidates[1:]),
                        })

                    if best is not None:
                        best_scores.append(best.score)

                    output["Agent3_Run_Id"] = self.run_id
                    writer.writerow(output)

                    if jsonl_handle is not None:
                        payload = dict(output)
                        payload["Match_Evidence"] = [
                            {
                                "item_id": candidate.item.item_id,
                                "item_description": candidate.item.description,
                                "item_description_english": candidate.item.english_description,
                                "supplier": candidate.item.supplier,
                                "source_file": candidate.item.source_file,
                                "score": candidate.score,
                                "semantic": candidate.semantic,
                                "lexical": candidate.lexical,
                                "code_match": candidate.code_match,
                                "type_compatible": candidate.type_compatible,
                                "specification_agreement": candidate.spec_agreement,
                                "rationale": candidate.rationale,
                            }
                            for candidate in candidates
                        ]
                        jsonl_handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            finally:
                if jsonl_handle is not None:
                    jsonl_handle.close()

        candidate_count = self._write_candidates(candidates_path)
        self._write_calibration(calibration_path, best_scores)

        if self.model is not None:
            self.model.save_cache()

        statistics = dict(self.statistics)
        statistics.update({
            "rows_total": len(self.table.rows),
            "reference_items": len(self.library.items),
            "reference_files": len(self.library.files_read),
            "rows_with_match": matched_rows,
            "rows_already_standard": standard_rows,
            "match_bands": dict(band_counts),
            "catalogue_candidates": candidate_count,
            "score_percentiles": self._percentiles(best_scores),
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
                "reference_dir": str(self.settings.reference_dir),
                "catalogue_dir": (str(self.settings.catalogue_dir)
                                  if self.settings.catalogue_dir else None),
                "results_dir": str(self.settings.results_dir),
                "thresholds": {
                    "high": self.settings.high_threshold,
                    "medium_accept": self.settings.medium_threshold,
                    "minimum_report": self.settings.minimum_threshold,
                },
                "type_mismatch_penalty": self.settings.type_mismatch_penalty,
                "spec_conflict_penalty": self.settings.spec_conflict_penalty,
                "embeddings": self.engine.use_embeddings,
                "neural_translation": self.translator.enabled,
                "language_model": self.settings.model.enabled,
                "model": self.settings.model.model if self.settings.model.enabled else None,
            },
            "reference_files": self.library.files_read,
            # Enough to prove which version of the client's catalogue was used.
            "catalogue_sources": self.library.sources,
            "skipped_files": self.library.skipped_files,
            "refused_files": self.library.rejected_files,
            "unreadable_files": self.library.unreadable_files,
            "outputs": [rows_path.name, candidates_path.name, calibration_path.name]
                       + ([jsonl_path.name] if self.settings.write_jsonl else []),
            "statistics": statistics,
        }
        (results_dir / "agent3_run_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    def _confidence(self, candidate: MatchCandidate) -> int:
        """Confidence that a proposed match is genuinely equivalent.

        Distinct from the similarity score. The score says how alike the two
        descriptions are; the confidence says how much that similarity should be
        believed, given whether the constraint layers were able to corroborate
        it. A high similarity between two items whose types disagree is exactly
        the case where confidence should fall while the score stays high.
        """
        confidence = candidate.score
        if candidate.code_match:
            confidence = min(1.0, confidence + 0.08)
        if not candidate.type_compatible:
            confidence *= 0.6
        if candidate.spec_agreement is True:
            confidence = min(1.0, confidence + 0.06)
        elif candidate.spec_agreement is False:
            confidence *= 0.65
        # Agreement between two independent views is worth more than a strong
        # reading from either one alone.
        if candidate.semantic >= 0.7 and candidate.lexical >= 0.5:
            confidence = min(1.0, confidence + 0.05)
        return int(round(max(0.0, min(1.0, confidence)) * 100))

    @staticmethod
    def _price_difference(purchase_price: Optional[float],
                          catalogue_price: Optional[float]) -> Optional[float]:
        """Percentage by which the purchase exceeded the catalogue price.

        Positive means the free-text purchase cost more than the standard item,
        which is the direction that matters for the maverick-buying analysis.
        """
        if purchase_price is None or catalogue_price is None or catalogue_price <= 0:
            return None
        return round((purchase_price - catalogue_price) / catalogue_price * 100.0, 1)

    def _write_candidates(self, path: Path) -> int:
        """Write the catalogue-candidate list, highest scoring first."""
        columns = ["Catalogue_Candidate", "Candidate_Score", "Purchase_Description",
                   "AI_Purchase_Group_L5", "Category", "Occurrences", "Total_Spend_EUR",
                   "Distinct_Suppliers", "Periods", "Mean_Unit_Price", "Price_Stability",
                   "Already_Has_Standard_Item", "Reasoning"]

        rows: List[Tuple[int, List[Any]]] = []
        for profile in self.detector.profiles.values():
            score, band, reasons = self.detector.score(profile)
            if band == "No" and score == 0:
                continue
            prices = [price for price in profile.unit_prices if price > 0]
            rows.append((score, [
                band, score, profile.description, profile.purchase_group, profile.category,
                profile.occurrences, round(profile.total_spend, 2), len(profile.suppliers),
                len(profile.periods),
                round(statistics.fmean(prices), 2) if prices else "",
                "" if profile.price_stability is None else profile.price_stability,
                "Yes" if profile.already_standard else "No",
                "; ".join(reasons),
            ]))

        rows.sort(key=lambda entry: (-entry[0], entry[1][2]))
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for _, values in rows:
                writer.writerow(values)
        return sum(1 for score, values in rows if values[0] == "Yes")

    def _write_calibration(self, path: Path, scores: Sequence[float]) -> None:
        """Write the distribution of best-match scores.

        The development plan leaves the similarity threshold to be defined and
        tested. This file is how that is done: it shows how many lines would be
        accepted at each possible cut-off, so the threshold can be argued from
        the shape of the data rather than picked and defended afterwards.
        """
        total = len(scores)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Threshold", "Lines_At_Or_Above", "Share_Of_Scored_Lines",
                             "Band_At_This_Threshold"])
            if not total:
                return
            for step in range(20, 101, 5):
                threshold = step / 100.0
                count = sum(1 for score in scores if score >= threshold)
                writer.writerow([f"{threshold:.2f}", count, f"{count / total:.4f}",
                                 self.engine.band(threshold)])

    @staticmethod
    def _percentiles(scores: Sequence[float]) -> Dict[str, float]:
        """Key percentiles of the best-match score distribution."""
        if not scores:
            return {}
        ordered = sorted(scores)

        def at(fraction: float) -> float:
            index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
            return round(ordered[index], 4)

        return {"p10": at(0.10), "p25": at(0.25), "p50": at(0.50),
                "p75": at(0.75), "p90": at(0.90), "max": round(ordered[-1], 4)}

    def run(self) -> Dict[str, Any]:
        """Execute the full pipeline."""
        self.table = read_purchase_table(self.settings.input_path)
        self.run_id = stable_hash("agent3-run", self.settings.input_path.name,
                                  str(len(self.table.rows)), AGENT_VERSION)
        self.library.load(*self.settings.catalogue_roots())
        self.match_descriptions()
        self.adjudicate()
        self.profile_candidates()
        return self.write()


# ===========================================================================
# Command line interface
# ===========================================================================

BANNER = r"""
===============================================================================
 Fortum AI-Powered Procurement Analysis
 Agent 3 - AI Material and Service Standardisation
 Prof. Shahab Anbarjafari
===============================================================================
""".strip("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent3.py",
        description="Agent 3 - match purchases to standard items and find catalogue candidates.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python agent3.py\n"
            "      Prompt for each path in turn and run with the defaults.\n\n"
            "  python agent3.py --non-interactive --input results/agent2_purchase_groups.csv \\\n"
            "                   --catalogues './catalogues'\n"
            "      Run unattended against the item catalogues the client sent.\n\n"
            "  python agent3.py --non-interactive --medium-threshold 0.70\n"
            "      Tighten the accept threshold after reading the calibration file.\n"
        ),
    )
    paths = parser.add_argument_group("paths")
    paths.add_argument("--input", metavar="FILE", help="purchase table from Agent 2 or Agent 1")
    paths.add_argument("--reference", metavar="DIR",
                       help="folder holding catalogues, price lists and standard items")
    paths.add_argument("--catalogues", metavar="DIR",
                       help="folder holding item catalogues only; use this when the "
                            "catalogues are kept apart from the purchase extracts")
    paths.add_argument("--results", metavar="DIR", help="folder to write results into")
    paths.add_argument("--lexicon", metavar="FILE", help="controlled vocabulary JSON file")
    paths.add_argument("--cache", metavar="DIR", help="folder for the model response cache")

    thresholds = parser.add_argument_group("match thresholds")
    thresholds.add_argument("--high-threshold", type=float, default=0.80,
                            help="score at or above which a match is High (default 0.80)")
    thresholds.add_argument("--medium-threshold", type=float, default=0.65,
                            help="score at or above which a match is accepted (default 0.65)")
    thresholds.add_argument("--minimum-threshold", type=float, default=0.50,
                            help="score below which no match is reported (default 0.50)")
    thresholds.add_argument("--top-k", type=int, default=5,
                            help="candidate standard items kept per description (default 5)")

    candidates = parser.add_argument_group("catalogue candidates")
    candidates.add_argument("--candidate-min-occurrences", type=int, default=3,
                            help="repeats before a purchase can be a candidate (default 3)")
    candidates.add_argument("--candidate-min-spend", type=float, default=1000.0,
                            help="total spend before a purchase counts (default 1000)")

    tiers = parser.add_argument_group("processing tiers")
    tiers.add_argument("--no-embeddings", action="store_true",
                       help="disable multilingual sentence embeddings")
    tiers.add_argument("--no-neural", action="store_true",
                       help="disable offline translation of reference descriptions")
    tiers.add_argument("--use-llm", action="store_true",
                       help="let the language model adjudicate borderline matches")
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

    # Agent 2's output is preferred because it carries the purchase group, which
    # makes the candidate list far more readable. Agent 1's output works too.
    if args.input:
        default_input = Path(args.input)
    elif (default_results / "agent2_purchase_groups.csv").is_file():
        default_input = default_results / "agent2_purchase_groups.csv"
    else:
        default_input = default_results / "agent1_unified_lines.csv"

    default_reference = Path(args.reference) if args.reference else here / "sources"
    default_lexicon = Path(args.lexicon) if args.lexicon else here / "lexicon" / "procurement_lexicon.json"
    default_cache = Path(args.cache) if args.cache else here / "cache"

    # A catalogues folder is used when one is named, and also when the standard
    # one exists, so that dropping the client's catalogues into ./catalogues is
    # all it takes to keep them apart from the purchase extracts.
    #
    # An explicitly named --reference is never overridden this way. Naming a
    # reference folder is an instruction about where to read, and since a named
    # catalogue folder becomes the whole answer, letting ./catalogues win would
    # silently redirect the run somewhere the caller did not ask for and report a
    # complete run against the wrong catalogue.
    if args.catalogues:
        default_catalogues: Optional[Path] = Path(args.catalogues)
    elif not args.reference and (here / "catalogues").is_dir():
        default_catalogues = here / "catalogues"
    else:
        default_catalogues = None

    use_embeddings = not args.no_embeddings
    use_neural = not args.no_neural
    use_llm = args.use_llm
    spend_limit = (args.llm_spend_limit if args.llm_spend_limit is not None
                   else _env_float(env.get("LLM_SPEND_LIMIT"), DEFAULT_SPEND_LIMIT))

    if not args.non_interactive:
        print(BANNER)
        print("\nPress Enter to accept the value shown in brackets.\n")
        default_input = Path(ask("Purchase table (from Agent 2, or Agent 1)", str(default_input)))
        default_catalogues = Path(ask("Item catalogue folder (catalogues only)",
                                      str(default_catalogues or (here / "catalogues"))))
        if not default_catalogues.is_dir():
            print(f"  {default_catalogues} does not exist yet; "
                  f"falling back to the reference folder.")
            default_catalogues = None
        default_reference = Path(ask("Reference data folder (catalogues and price lists)",
                                     str(default_reference)))
        default_results = Path(ask("Results folder", str(default_results)))
        default_lexicon = Path(ask("Controlled vocabulary file", str(default_lexicon)))
        default_cache = Path(ask("Cache folder", str(default_cache)))

        print()
        if _sentence_transformers is None:
            print("  Sentence embeddings are not installed; that view will be skipped.")
            use_embeddings = False
        else:
            use_embeddings = ask_yes_no(
                "Use multilingual sentence embeddings (recommended, free)?", True)
        if _transformers is None:
            use_neural = False
        else:
            use_neural = ask_yes_no(
                "Translate reference descriptions with the offline models?", use_neural)
        use_llm = ask_yes_no(
            "Let the language model adjudicate borderline matches?", use_llm)
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
        reference_dir=default_reference.expanduser().resolve(),
        catalogue_dir=(default_catalogues.expanduser().resolve()
                       if default_catalogues is not None else None),
        results_dir=default_results.expanduser().resolve(),
        lexicon_path=default_lexicon.expanduser().resolve(),
        cache_dir=default_cache.expanduser().resolve(),
        use_embeddings=use_embeddings,
        use_neural_translation=use_neural,
        use_llm=use_llm,
        high_threshold=args.high_threshold,
        medium_threshold=args.medium_threshold,
        minimum_threshold=args.minimum_threshold,
        top_k=args.top_k,
        candidate_min_occurrences=args.candidate_min_occurrences,
        candidate_min_spend=args.candidate_min_spend,
        write_jsonl=not args.no_jsonl,
        verbose=args.verbose,
        interactive=not args.non_interactive,
    )
    settings.model = resolve_model_config(env, use_llm, spend_limit)

    if not (settings.minimum_threshold <= settings.medium_threshold <= settings.high_threshold):
        raise SystemExit("Thresholds must satisfy minimum <= medium <= high")
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
    print(f"  Distinct descriptions: {statistics.get('distinct_descriptions', 0):,}")
    print(f"  Reference items      : {statistics.get('reference_items', 0):,} "
          f"from {statistics.get('reference_files', 0)} file(s)")

    # Named with its date and digest, so "is this the client's latest catalogue?"
    # can be answered from the run output before the numbers below are trusted.
    sources = manifest.get("catalogue_sources") or []
    if sources:
        for entry in sources:
            print(f"    {entry['file']}")
            print(f"      {entry['items']:,} item(s), modified "
                  f"{entry['modified'] or 'unknown'}, "
                  f"{entry['bytes']:,} bytes, sha256 {entry['sha256'] or 'unknown'}")
    else:
        for entry in manifest["reference_files"]:
            print(f"    {entry}")

    if not statistics.get("reference_items"):
        print("  NO CATALOGUE WAS LOADED - no match can be proposed.")
        print("    Point --catalogues at the folder holding the client's item")
        print("    catalogue, and check the file is their latest.")

    if manifest["skipped_files"]:
        print("  Files with no recognisable item description (skipped):")
        for name in manifest["skipped_files"]:
            print(f"    {name}")
    # Loud, because a refused file is a deliberate decision the reader should see
    # and an unreadable one is a catalogue that silently did not arrive.
    if manifest.get("refused_files"):
        print("  Not treated as catalogues:")
        for name in manifest["refused_files"]:
            print(f"    {name}")
    if manifest.get("unreadable_files"):
        print("  COULD NOT BE READ - these catalogues were left out of the run:")
        for name in manifest["unreadable_files"]:
            print(f"    {name}")

    print(f"\n  Already standard     : {statistics.get('rows_already_standard', 0):,} "
          f"(catalogue or stocked item)")
    print(f"  Lines with a match   : {statistics.get('rows_with_match', 0):,} "
          f"(at or above {settings.medium_threshold:.2f})")
    bands = statistics.get("match_bands", {})
    if bands:
        print("  Match bands          : "
              + "  ".join(f"{name} {count:,}" for name, count in sorted(bands.items())))

    percentiles = statistics.get("score_percentiles", {})
    if percentiles:
        print("\n  Best-match score distribution")
        for name in ("p10", "p25", "p50", "p75", "p90", "max"):
            if name in percentiles:
                print(f"    {name:<5} {percentiles[name]:.3f}")
        print("    See agent3_match_calibration.csv before fixing the thresholds.")

    adjudicated = statistics.get("matches_adjudicated", 0)
    if adjudicated:
        print(f"\n  Adjudicated by model : {adjudicated:,}")

    print(f"\n  Catalogue candidates : {statistics.get('catalogue_candidates', 0):,}")
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
        manifest = Agent3(settings).run()
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
