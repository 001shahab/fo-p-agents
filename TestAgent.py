#!/usr/bin/env python3
"""
TestAgent - synthetic-data test harness for the procurement agents
==================================================================

Author  : Prof. Shahab Anbarjafari
Purpose : Prove that each agent still does what it claims, on data built to
          make it prove it, without ever touching client information.

Why synthetic data rather than a sample of the real thing
---------------------------------------------------------
A test is only worth running if it can fail. Handing an agent a slice of real
data tells you that it produced output, not that the output was right, because
nobody knows what the right answer was. Here the answer is decided first and
the data is built backwards from it: the harness plants a supplier whose
portfolio is a strict subset of another's and then checks that Agent 4 finds
it, plants six ways of writing the same purchase and checks that Agent 2 puts
them in one group, plants a purchase order that a transaction can be joined to
and checks that Max joins it.

Everything generated here is invented. The company names, sites, suppliers and
purchase lines are written to look like Fortum's procurement estate — district
heating, hydro, wind, nuclear and the Polish and Swedish networks, in Finnish,
Swedish, Polish and English — because an agent tuned to that vocabulary should
be tested against it. None of it is real, and none of it leaves the machine
unless the language-model tier is switched on.

What a test consists of
-----------------------
    1. Build the input the chosen agent expects, with the properties that agent
       is supposed to detect deliberately planted in it.
    2. Run the agent exactly as an operator would, as a separate process, with
       its own results folder.
    3. Read what came back and check it against what was planted.

Checks are graded rather than binary. A failure means the agent broke a
contract: a missing output file, a lost row, an empty deliverable column. A
warning means it missed something the data was built to offer, which is worth
knowing but may be a matter of threshold rather than a defect.

The language model
------------------
Optional throughout, and never load-bearing. It writes extra phrasings of the
purchase descriptions so the vocabulary is not limited to what is spelled out
in this file, and it reads the agent's log back in plain English while the run
is in progress. The structure of the test — the planted subset, the planted
duplicate, the planted join key — is built deterministically and does not
depend on it, so a run without a key tests exactly the same things.

Usage
-----
    python app.py                       # the interface this was written for
    python TestAgent.py --list          # what can be tested
    python TestAgent.py --agent agent2  # run one test from the terminal
    python TestAgent.py --all
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / ".testruns"

HARNESS_NAME = "TestAgent"
HARNESS_VERSION = "1.0.0"

# Prices for the default model tier, in dollars per million tokens. Kept here so
# the harness can report what a run cost in the same terms the agents do.
INPUT_COST_PER_MTOK = 1.25
OUTPUT_COST_PER_MTOK = 10.00


# ===========================================================================
# Language model
# ===========================================================================

@dataclass
class ModelConfig:
    """Where the language model lives, if it is being used at all."""

    enabled: bool = False
    backend: str = "openai"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.1"
    timeout: int = 60

    @property
    def endpoint(self) -> str:
        url = self.base_url.rstrip("/")
        return url if url.endswith("/chat/completions") else f"{url}/chat/completions"

    def describe(self) -> str:
        if not self.enabled:
            return "Local rules only"
        label = "Azure OpenAI" if self.backend == "azure" else "OpenAI"
        return f"{self.model} via {label}"


def load_dotenv(path: Path) -> Dict[str, str]:
    """Parse a ``.env`` file into a dictionary."""
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


def _flag(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def resolve_model_config(env: Dict[str, str], enabled: bool) -> ModelConfig:
    """Select the backend, following the same rules as the agents."""
    config = ModelConfig(enabled=enabled)
    if _flag(env.get("AZURE_ENABLE")):
        config.backend = "azure"
        config.api_key = (env.get("AZURE_OPENAI_API_KEY") or env.get("AZURE_API_KEY")
                          or env.get("OPENAI_API_KEY") or "")
        config.base_url = (env.get("AZURE_OPENAI_BASE_URL") or env.get("AZURE_BASE_URL")
                           or env.get("BASE_URL")
                           or "https://genai-sharedservice-emea.pwcinternal.com/v1/chat/completions")
        config.model = env.get("AZURE_OPENAI_MODEL") or env.get("MODEL_NAME") or "azure.gpt-5.1"
    else:
        # Deliberately does not inherit BASE_URL: on this project that variable
        # points at the shared service, and inheriting it would send a personal
        # OpenAI key to an internal endpoint.
        config.backend = "openai"
        config.api_key = env.get("OPENAI_API_KEY") or ""
        config.base_url = env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        config.model = env.get("OPENAI_MODEL") or "gpt-5.1"

    if config.enabled and not config.api_key:
        config.enabled = False
    return config


@dataclass
class TokenUsage:
    """What the harness itself spent, reported in the same terms as the agents."""

    requests: int = 0
    failures: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def estimated_cost(self) -> float:
        return (self.input_tokens / 1_000_000.0 * INPUT_COST_PER_MTOK
                + self.output_tokens / 1_000_000.0 * OUTPUT_COST_PER_MTOK)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "failures": self.failures,
            "cache_hits": self.cache_hits,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "estimated_cost_usd": round(self.estimated_cost, 4),
        }


class LanguageModel:
    """A small OpenAI-compatible client with an on-disk answer cache.

    Written against urllib so the harness has no dependency beyond the standard
    library: ``python app.py`` has to work on a machine where nothing has been
    installed yet.
    """

    def __init__(self, config: ModelConfig, cache_path: Path) -> None:
        self.config = config
        self.usage = TokenUsage()
        self.cache_path = cache_path
        self._cache = self._load()
        self._dirty = False
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, str]:
        if not self.cache_path.is_file():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8")).get("entries", {})
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self) -> None:
        if not self._dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps({"model": self.config.model, "entries": self._cache},
                       ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        self._dirty = False

    @property
    def available(self) -> bool:
        return self.config.enabled and bool(self.config.api_key)

    def ask(self, system: str, user: str, *, cache: bool = True) -> Optional[Dict[str, Any]]:
        """Ask for a JSON object. Returns None on any failure, never raises."""
        if not self.available:
            return None

        key = ""
        if cache:
            import hashlib
            key = hashlib.sha256(
                "\x1f".join((self.config.model, system, user)).encode("utf-8")).hexdigest()[:20]
            with self._lock:
                stored = self._cache.get(key)
            if stored is not None:
                self.usage.cache_hits += 1
                try:
                    return json.loads(stored)
                except json.JSONDecodeError:
                    pass

        body = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        answer = self._post(body)
        if answer is None:
            return None

        if cache and key:
            with self._lock:
                self._cache[key] = json.dumps(answer, ensure_ascii=False, sort_keys=True)
                self._dirty = True
        return answer

    def _post(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        import urllib.error
        import urllib.request

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            # The shared service authenticates by api-key rather than by bearer
            # token; sending both is accepted by each and saves branching.
            "api-key": self.config.api_key,
        }
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.config.endpoint, data=payload,
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as handle:
                status, text = handle.status, handle.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            status = error.code
            text = error.read().decode("utf-8", "replace")
            if status == 400 and "temperature" in text.lower():
                return self._post({k: v for k, v in body.items() if k != "temperature"})
        except Exception:
            self.usage.failures += 1
            return None

        self.usage.requests += 1
        if status != 200:
            self.usage.failures += 1
            return None

        try:
            response = json.loads(text)
            usage = response.get("usage") or {}
            self.usage.input_tokens += int(usage.get("prompt_tokens") or 0)
            self.usage.output_tokens += int(usage.get("completion_tokens") or 0)
            content = response["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            self.usage.failures += 1
            return None
        return _json_object(content)


def _json_object(content: str) -> Optional[Dict[str, Any]]:
    """Recover a JSON object from a reply that may be fenced or prefaced."""
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
            escape = (char == "\\") and not escape
            if char == '"' and not escape:
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


# ===========================================================================
# The invented estate
# ===========================================================================
#
# A small but internally consistent picture of an energy company's procurement:
# a handful of sites in four countries, a category tree four levels deep, and
# suppliers who trade in particular categories. Every name is invented. The
# shape is what matters — the agents are tuned to Nordic and Polish industrial
# vocabulary, so a test that fed them generic English would not be testing much.

@dataclass(frozen=True)
class Site:
    code: str
    company: str
    country: str
    currency: str
    division: str
    business_area: str
    language: str


SITES: Tuple[Site, ...] = (
    Site("ESP", "Fortum Heat Espoo Oy", "Finland", "EUR",
         "City Solutions", "BU21 - Heat Espoo", "fi"),
    Site("LOV", "Fortum Power and Heat Oy", "Finland", "EUR",
         "Generation", "BU10 - Nuclear Loviisa", "fi"),
    Site("IMA", "Fortum Hydro Finland Oy", "Finland", "EUR",
         "Generation", "BU14 - Hydro Finland", "fi"),
    Site("BOR", "Fortum Vind Borgvik AB", "Sweden", "SEK",
         "Renewables and Decarbonisation", "BU27 - Wind Sweden", "sv"),
    Site("STO", "Fortum Varme Service AB", "Sweden", "SEK",
         "City Solutions", "BU24 - Heat Sweden", "sv"),
    Site("PLO", "Fortum Network Plock Sp. z o.o.", "Poland", "PLN",
         "Renewables and Decarbonisation", "BU32 - RED Heat Poland", "pl"),
    Site("WRO", "Fortum Network Wroclaw Sp. z o.o.", "Poland", "PLN",
         "Renewables and Decarbonisation", "BU32 - RED Heat Poland", "pl"),
    Site("OSL", "Fortum Recycling Norge AS", "Norway", "NOK",
         "Circular Solutions", "BU41 - Recycling Nordics", "en"),
)


@dataclass(frozen=True)
class Category:
    l1: str
    l2: str
    l3: str
    l4: str
    group_number: str
    group_name: str


CATEGORIES: Dict[str, Category] = {
    "metering": Category("Energy assets", "Automation and Electrification",
                         "Automation systems and components",
                         "Measurement Equipment and Tools", "501301",
                         "Measurement equipment and tools"),
    "control": Category("Energy assets", "Automation and Electrification",
                        "Automation systems and components",
                        "Other industrial control systems", "517111",
                        "Industrial control systems"),
    "rotating": Category("Energy assets", "Rotating equipment",
                         "Pumps and compressors", "Centrifugal pumps",
                         "512204", "Pumps and pump parts"),
    "boiler": Category("Energy assets", "Heat production",
                       "Boilers and burners", "Boiler maintenance",
                       "513110", "Boiler plant services"),
    "sealing": Category("Indirect Services and Materials",
                        "Production supplies and consumables", "MRO Supplies",
                        "Seals and Gaskets", "501340", "Seals and gaskets"),
    "ppe": Category("Indirect Services and Materials",
                    "Production supplies and consumables", "MRO Supplies",
                    "Personal Protective Equipment", "501380",
                    "Personal protective equipment"),
    "cleaning": Category("Indirect Services and Materials", "Facility services",
                         "Cleaning services", "Industrial cleaning", "540210",
                         "Industrial cleaning services"),
    "consulting": Category("Indirect Services and Materials",
                           "Professional services", "Technical consulting",
                           "Environmental consulting", "529231",
                           "Technical consulting"),
    "electrical": Category("Energy assets", "Automation and Electrification",
                           "Electrical installation", "Cabling and terminations",
                           "517220", "Electrical installation materials"),
    "freight": Category("Logistics and Transport", "Freight and distribution",
                        "Road freight", "Domestic delivery", "560110",
                        "Freight and delivery"),
}


@dataclass(frozen=True)
class Concept:
    """One thing that gets bought, and the many ways people write it down.

    ``phrasings`` holds real-looking descriptions per language. Several per
    language matters more than many concepts: the agents are judged on whether
    they can see that four spellings mean one thing, and that only works if the
    spellings differ the way they differ in practice.
    """

    key: str
    english: str
    kind: str                       # Material or Service
    category: str                   # key into CATEGORIES
    unit: str
    price: Tuple[float, float]
    phrasings: Dict[str, Tuple[str, ...]]


CONCEPTS: Tuple[Concept, ...] = (
    Concept("heat_meter", "District heating meter DN25", "Material", "metering",
            "pcs", (280.0, 520.0), {
                "fi": ("Kaukolampomittari DN25", "Lampoenergiamittari DN25 pulssilahdolla",
                       "Kaukolämpömittari DN25 asennettuna"),
                "sv": ("Varmematare DN25", "Fjarrvarmematare DN25 med pulsutgang"),
                "pl": ("Cieplomierz DN25", "Uklad pomiarowy ciepla DN25"),
                "en": ("District heating meter DN25", "Heat meter DN25 with pulse output"),
            }),
    Concept("flow_transmitter", "Flow transmitter 4-20 mA", "Material", "metering",
            "pcs", (640.0, 1180.0), {
                "fi": ("Virtausmittari 4-20 mA", "Virtauslahetin DN50 4-20mA"),
                "sv": ("Flodesgivare 4-20 mA", "Flodesmatare DN50"),
                "pl": ("Przetwornik przeplywu 4-20 mA", "Przeplywomierz DN50"),
                "en": ("Flow transmitter 4-20 mA", "Flow meter DN50 4-20 mA output"),
            }),
    Concept("meter_calibration", "Metering calibration service", "Service", "metering",
            "h", (95.0, 145.0), {
                "fi": ("Mittarin kalibrointi", "Mittauslaitteiden kalibrointipalvelu"),
                "sv": ("Kalibrering av matare", "Kalibreringstjanst for matinstrument"),
                "pl": ("Kalibracja licznika", "Usluga kalibracji urzadzen pomiarowych"),
                "en": ("Meter calibration service", "Calibration of measurement instruments"),
            }),
    Concept("plc_module", "PLC input/output module", "Material", "control",
            "pcs", (410.0, 890.0), {
                "fi": ("Logiikan IO-moduuli", "Automaatiojarjestelman tulomoduuli"),
                "sv": ("PLC in- och utgangsmodul", "Automationssystem IO-modul"),
                "pl": ("Modul wejsc wyjsc sterownika PLC", "Modul IO systemu automatyki"),
                "en": ("PLC input output module", "Automation system IO module"),
            }),
    Concept("frequency_converter", "Frequency converter 55 kW", "Material", "control",
            "pcs", (2400.0, 4800.0), {
                "fi": ("Taajuusmuuttaja 55 kW", "Taajuusmuuttaja pumpulle 55kW"),
                "sv": ("Frekvensomriktare 55 kW", "Frekvensomriktare till pump 55kW"),
                "pl": ("Przemiennik czestotliwosci 55 kW", "Falownik do pompy 55kW"),
                "en": ("Frequency converter 55 kW", "Variable speed drive 55 kW"),
            }),
    Concept("pump_maintenance", "Centrifugal pump maintenance", "Service", "rotating",
            "h", (88.0, 132.0), {
                "fi": ("Keskipakopumpun huolto", "Pumpun vuosihuolto", "Pumppujen kunnossapito"),
                "sv": ("Underhall av centrifugalpump", "Arlig service av pump"),
                "pl": ("Konserwacja pompy odsrodkowej", "Przeglad roczny pompy"),
                "en": ("Centrifugal pump maintenance", "Annual pump service"),
            }),
    Concept("pump_impeller", "Pump impeller replacement part", "Material", "rotating",
            "pcs", (740.0, 1650.0), {
                "fi": ("Pumpun juoksupyora", "Juoksupyora keskipakopumppuun"),
                "sv": ("Pumphjul till centrifugalpump", "Pumphjul reservdel"),
                "pl": ("Wirnik pompy", "Wirnik do pompy odsrodkowej"),
                "en": ("Pump impeller", "Impeller spare part for centrifugal pump"),
            }),
    Concept("boiler_inspection", "Boiler annual inspection", "Service", "boiler",
            "job", (3200.0, 7400.0), {
                "fi": ("Kattilan vuositarkastus", "Kattilalaitoksen maaraaikaistarkastus"),
                "sv": ("Arlig besiktning av panna", "Periodisk besiktning pannanlaggning"),
                "pl": ("Przeglad roczny kotla", "Okresowa kontrola kotlowni"),
                "en": ("Boiler annual inspection", "Periodic inspection of boiler plant"),
            }),
    Concept("burner_service", "Burner overhaul", "Service", "boiler",
            "job", (2100.0, 5200.0), {
                "fi": ("Polttimen huolto", "Polttimen peruskorjaus"),
                "sv": ("Service av brannare", "Renovering av brannare"),
                "pl": ("Serwis palnika", "Remont palnika"),
                "en": ("Burner service", "Burner overhaul"),
            }),
    Concept("gasket_set", "Flange gasket set DN100", "Material", "sealing",
            "pcs", (18.0, 64.0), {
                "fi": ("Laippatiivistesarja DN100", "Tiivistesarja DN100", "Tiivisteet DN100"),
                "sv": ("Flanspackningssats DN100", "Packningssats DN100"),
                "pl": ("Zestaw uszczelek kolnierzowych DN100", "Uszczelki DN100"),
                "en": ("Flange gasket set DN100", "Gasket kit DN100"),
            }),
    Concept("mechanical_seal", "Mechanical seal for pump", "Material", "sealing",
            "pcs", (210.0, 540.0), {
                "fi": ("Liukurengastiiviste pumppuun", "Mekaaninen tiiviste pumpulle"),
                "sv": ("Mekanisk tatning till pump", "Planpackning till pump"),
                "pl": ("Uszczelnienie mechaniczne pompy", "Uszczelka mechaniczna do pompy"),
                "en": ("Mechanical seal for pump", "Pump mechanical seal"),
            }),
    Concept("safety_helmet", "Safety helmet with visor", "Material", "ppe",
            "pcs", (34.0, 78.0), {
                "fi": ("Suojakypara visiirilla", "Tyokypara visiirilla"),
                "sv": ("Skyddshjalm med visir", "Arbetshjalm med visir"),
                "pl": ("Kask ochronny z przylbica", "Helm roboczy z oslona twarzy"),
                "en": ("Safety helmet with visor", "Protective helmet with face shield"),
            }),
    Concept("flame_coverall", "Flame retardant coverall", "Material", "ppe",
            "pcs", (120.0, 260.0), {
                "fi": ("Palosuojattu haalari", "Liekinkestava suojahaalari"),
                "sv": ("Flamskyddad overall", "Brandhammande overall"),
                "pl": ("Kombinezon trudnopalny", "Odziez ochronna trudnopalna"),
                "en": ("Flame retardant coverall", "Fire resistant overall"),
            }),
    Concept("industrial_cleaning", "Industrial cleaning of boiler house", "Service",
            "cleaning", "job", (1800.0, 6400.0), {
                "fi": ("Teollisuussiivous kattilahuoneessa", "Kattilahuoneen puhdistus"),
                "sv": ("Industriell rengoring av pannhus", "Rengoring av pannrum"),
                "pl": ("Czyszczenie przemyslowe kotlowni", "Sprzatanie kotlowni"),
                "en": ("Industrial cleaning of boiler house", "Boiler house cleaning"),
            }),
    Concept("tank_cleaning", "Tank cleaning service", "Service", "cleaning",
            "job", (2600.0, 8800.0), {
                "fi": ("Sailion puhdistus", "Polttoainesailion pesu"),
                "sv": ("Rengoring av tank", "Tankrengoring branslecistern"),
                "pl": ("Czyszczenie zbiornika", "Mycie zbiornika paliwa"),
                "en": ("Tank cleaning service", "Fuel tank cleaning"),
            }),
    Concept("environmental_survey", "Environmental impact survey", "Service",
            "consulting", "job", (7200.0, 24000.0), {
                "fi": ("Ymparistovaikutusten arviointi", "Ymparistotekninen selvitys"),
                "sv": ("Miljokonsekvensbeskrivning", "Miljoteknisk utredning"),
                "pl": ("Ocena oddzialywania na srodowisko", "Analiza srodowiskowa"),
                "en": ("Environmental impact survey", "Environmental technical study"),
            }),
    Concept("bat_survey", "Bat survey for wind site", "Service", "consulting",
            "job", (4800.0, 12500.0), {
                "fi": ("Lepakkoselvitys tuulipuistoon", "Lepakkokartoitus"),
                "sv": ("Inventering fladdermus vindpark", "Fladdermusinventering"),
                "pl": ("Inwentaryzacja nietoperzy", "Badanie nietoperzy farma wiatrowa"),
                "en": ("Bat survey for wind site", "Bat inventory wind farm"),
            }),
    Concept("power_cable", "Power cable 4x25 mm2", "Material", "electrical",
            "m", (11.0, 26.0), {
                "fi": ("Voimakaapeli 4x25 mm2", "Maakaapeli 4x25mm2"),
                "sv": ("Kraftkabel 4x25 mm2", "Jordkabel 4x25mm2"),
                "pl": ("Kabel zasilajacy 4x25 mm2", "Kabel ziemny 4x25mm2"),
                "en": ("Power cable 4x25 mm2", "Underground cable 4x25 mm2"),
            }),
    Concept("cable_termination", "Cable termination work", "Service", "electrical",
            "h", (72.0, 118.0), {
                "fi": ("Kaapelin paatetyo", "Kaapelipaatteiden asennus"),
                "sv": ("Kabelavslutningsarbete", "Montering av kabelavslut"),
                "pl": ("Wykonanie glowic kablowych", "Montaz zakonczen kablowych"),
                "en": ("Cable termination work", "Installation of cable terminations"),
            }),
    Concept("road_freight", "Road freight delivery", "Service", "freight",
            "job", (140.0, 980.0), {
                "fi": ("Rahti ja toimituskulut", "Kuljetus tyomaalle"),
                "sv": ("Frakt och leveranskostnad", "Transport till arbetsplats"),
                "pl": ("Transport i dostawa", "Przewoz na plac budowy"),
                "en": ("Road freight delivery", "Delivery to site"),
            }),
)

CONCEPT_BY_KEY: Dict[str, Concept] = {concept.key: concept for concept in CONCEPTS}


@dataclass(frozen=True)
class Supplier:
    """A trading partner, with the categories it actually sells into.

    ``variants`` holds the other spellings of the same legal entity that turn up
    across source systems. Agent 4 is supposed to collapse them; if it does not,
    a single supplier is counted as three and every consolidation figure built
    on top of that is wrong.
    """

    key: str
    name: str
    country: str
    categories: Tuple[str, ...]
    variants: Tuple[str, ...] = ()


SUPPLIERS: Tuple[Supplier, ...] = (
    Supplier("nordvalve", "Nordvalve Oy", "Finland",
             ("metering", "control", "sealing", "rotating"),
             ("NORDVALVE OY", "Nordvalve Oy Ab", "Nordvalve  Oy")),
    Supplier("pohjola", "Pohjola Automaatio Oy", "Finland",
             ("control", "metering", "electrical")),
    Supplier("kaukoteknik", "Kaukoteknik Service Oy", "Finland",
             ("rotating", "boiler", "sealing")),
    Supplier("lansi", "Lansi-Suomen Teollisuushuolto Oy", "Finland",
             ("boiler", "cleaning")),
    Supplier("bergstrom", "Bergstrom Industriservice AB", "Sweden",
             ("rotating", "boiler", "cleaning", "sealing")),
    Supplier("vindteknik", "Vindteknik Konsult AB", "Sweden",
             ("consulting",)),
    Supplier("nordiska", "Nordiska Matteknik AB", "Sweden",
             ("metering", "control"),
             ("NORDISKA MATTEKNIK AB", "Nordiska Mattekník AB")),
    Supplier("termika", "Termika Serwis Sp. z o.o.", "Poland",
             ("boiler", "rotating", "cleaning")),
    Supplier("elektro", "Elektro-Instal Sp. z o.o.", "Poland",
             ("electrical", "control")),
    Supplier("bezpiecz", "Bezpieczna Praca Sp. z o.o.", "Poland",
             ("ppe",)),
    Supplier("safeline", "Safeline Workwear Oy", "Finland",
             ("ppe",)),
    Supplier("transnord", "TransNord Logistics AS", "Norway",
             ("freight",)),
)

SUPPLIER_BY_KEY: Dict[str, Supplier] = {supplier.key: supplier for supplier in SUPPLIERS}


# ===========================================================================
# Making the text look like it came out of an ERP
# ===========================================================================

_ACCENTS = {"a": "ä", "o": "ö", "u": "ü"}


def _reaccent(text: str, rng: random.Random) -> str:
    """Put Nordic diacritics back on a word that was written without them.

    Source systems disagree about whether to keep them, so both spellings have
    to appear or the language handling is never exercised.
    """
    if rng.random() < 0.5:
        return text
    for plain, accented in _ACCENTS.items():
        text = text.replace(plain + "m", accented + "m", 1)
    return text


def _mojibake(text: str) -> str:
    """Reproduce the damage of writing UTF-8 and reading it back as cp1252."""
    try:
        return text.encode("utf-8").decode("cp1252")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def rough_up(text: str, rng: random.Random, severity: float = 0.35) -> str:
    """Introduce the wear that real free text carries.

    Every one of these is something the agents are built to survive, so a test
    set without them would pass while telling you nothing. They are applied
    sparingly: a field that is damaged five ways at once is not realistic, and a
    failure on it would not say which defence broke.
    """
    if rng.random() > severity:
        return text

    choice = rng.randrange(6)
    if choice == 0:
        return text.upper()
    if choice == 1:
        # A reference number welded onto the front of the description.
        return f"{rng.randrange(10000, 999999)}{text}"
    if choice == 2:
        return _mojibake(_reaccent(text, rng))
    if choice == 3:
        return re.sub(r"\s+", "  ", f"  {text} ")
    if choice == 4:
        # Truncated by a fixed-width field somewhere upstream.
        return text[:max(12, len(text) - rng.randrange(2, 7))]
    return f"{text} - {rng.choice(('kiireellinen', 'urgent', 'pilne', 'bradskande'))}"


def _amount(rng: random.Random, band: Tuple[float, float]) -> float:
    low, high = band
    return round(rng.uniform(low, high), 2)


def _iso_date(rng: random.Random) -> str:
    start = datetime(2025, 1, 6)
    return (start + timedelta(days=rng.randrange(0, 540))).strftime("%Y-%m-%d")


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("" if value is None else value) for key, value in row.items()})


# ===========================================================================
# Generated datasets
# ===========================================================================

@dataclass
class GeneratedFile:
    """One file written for the agent under test to read."""

    path: Path
    label: str
    role: str                     # sources | input | reference
    rows: int
    columns: List[str]

    story: str = ""               # two-line business reading of the file

    def as_dict(self, root: Path) -> Dict[str, Any]:
        return {
            "name": self.path.name,
            "relative": str(self.path.relative_to(root)),
            "label": self.label,
            "role": self.role,
            "rows": self.rows,
            "columns": self.columns,
            "story": self.story,
        }


@dataclass
class Dataset:
    """Everything generated for one test, and what was deliberately planted."""

    agent: str
    root: Path
    files: List[GeneratedFile] = field(default_factory=list)
    planted: List[str] = field(default_factory=list)
    facts: Dict[str, Any] = field(default_factory=dict)
    seed: int = 0
    model_phrasings: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent,
            "root": str(self.root),
            "seed": self.seed,
            "files": [item.as_dict(self.root) for item in self.files],
            "planted": self.planted,
            "facts": self.facts,
            "model_phrasings": self.model_phrasings,
            "total_rows": sum(item.rows for item in self.files),
        }


def preview_file(path: Path, limit: int = 12) -> Dict[str, Any]:
    """Read the head of a generated file for display."""
    if not path.is_file():
        return {"columns": [], "rows": [], "truncated": False}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            columns = next(reader)
        except StopIteration:
            return {"columns": [], "rows": [], "truncated": False}
        rows: List[List[str]] = []
        for index, values in enumerate(reader):
            if index >= limit:
                return {"columns": columns, "rows": rows, "truncated": True}
            rows.append(values)
    return {"columns": columns, "rows": rows, "truncated": False}


# ---------------------------------------------------------------------------
# Extra phrasings from the model
# ---------------------------------------------------------------------------

_PHRASING_SYSTEM = (
    "You write short purchase-order line descriptions for a Nordic energy "
    "utility, exactly as a buyer or an engineer would type them into an ERP.\n"
    "Return JSON only, as {\"phrasings\": [\"...\", \"...\"]}.\n"
    "Rules:\n"
    "- Write in the language you are asked for, and in that language only.\n"
    "- Between three and eight words. No sentences, no punctuation at the end.\n"
    "- Vary the register: some terse, some with a size or rating, some with a "
    "verb such as replacement or service.\n"
    "- Do not invent supplier names, prices, order numbers or dates.\n"
    "- Never repeat a phrasing that was supplied to you as an example."
)

_LANGUAGE_NAMES = {"fi": "Finnish", "sv": "Swedish", "pl": "Polish", "en": "English"}


class PhraseSource:
    """Supplies the wording for a purchase line.

    The built-in phrasings are the floor: they are enough on their own, and they
    are what runs when there is no model. Where a model is available it is asked
    for a few more per concept and language, which widens the vocabulary beyond
    what is written into this file and makes the test less of a rehearsal of the
    author's own examples.
    """

    def __init__(self, model: Optional[LanguageModel], rng: random.Random) -> None:
        self.model = model
        self.rng = rng
        self.extra: Dict[Tuple[str, str], List[str]] = {}
        self.generated = 0

    def enrich(self, concepts: Sequence[Concept], languages: Sequence[str],
               per_concept: int = 3) -> None:
        """Ask the model for additional phrasings, once per concept and language."""
        if self.model is None or not self.model.available:
            return
        for concept in concepts:
            for language in languages:
                seeds = concept.phrasings.get(language) or ()
                if not seeds:
                    continue
                request = {
                    "language": _LANGUAGE_NAMES.get(language, language),
                    "item": concept.english,
                    "type": concept.kind,
                    "examples": list(seeds),
                    "wanted": per_concept,
                }
                answer = self.model.ask(_PHRASING_SYSTEM,
                                        json.dumps(request, ensure_ascii=False))
                if not answer:
                    continue
                phrasings = [str(item).strip() for item in (answer.get("phrasings") or [])
                             if str(item).strip()]
                phrasings = [item for item in phrasings if item not in seeds][:per_concept]
                if phrasings:
                    self.extra[(concept.key, language)] = phrasings
                    self.generated += len(phrasings)

    def phrase(self, concept: Concept, language: str) -> str:
        """One description for this concept, in this language."""
        options = list(concept.phrasings.get(language) or concept.phrasings["en"])
        options += self.extra.get((concept.key, language), [])
        return self.rng.choice(options)


# ---------------------------------------------------------------------------
# Agent 1: raw source extracts
# ---------------------------------------------------------------------------

def build_agent1_sources(root: Path, rng: random.Random, phrases: PhraseSource,
                         lines: int = 120) -> Dataset:
    """Write three raw extracts of the kind Agent 1 is pointed at.

    Agent 1's job is to turn whatever free text an ERP holds into one clean
    English description, so the input has to be genuinely awkward: four
    languages, damaged encodings, welded reference numbers, empty and 'n/a'
    fields, and the same purchase recorded twice in two systems.
    """
    dataset = Dataset(agent="agent1", root=root, seed=rng.randrange(1 << 30))
    sources = root / "sources"

    transaction_columns = [
        "SourceRowId", "DataSource", "Document number", "Document line number",
        "Document line desc", "PO number", "PO line number", "PO line desc",
        "Invoice number", "Spend in purchase currency", "Purchase currency",
        "Spend in EUR", "Quantity", "Unit", "Posting date", "ERP supplier number",
        "ERP supplier name", "Category L1", "Category L2", "Category L3",
        "Category L4", "MaterialGroupNumber", "MaterialGroupName",
        "Legal company number", "Legal company name", "Country", "Division",
        "Business area",
    ]

    transactions: List[Dict[str, Any]] = []
    duplicates_planted = 0
    languages_used: set = set()

    for index in range(lines):
        concept = rng.choice(CONCEPTS)
        site = rng.choice(SITES)
        candidates = [s for s in SUPPLIERS if concept.category in s.categories] or list(SUPPLIERS)
        supplier = rng.choice(candidates)
        category = CATEGORIES[concept.category]

        language = site.language if rng.random() < 0.75 else rng.choice(("fi", "sv", "pl", "en"))
        languages_used.add(language)
        description = rough_up(phrases.phrase(concept, language), rng)

        quantity = rng.randrange(1, 25) if concept.unit == "pcs" else rng.randrange(1, 9)
        unit_price = _amount(rng, concept.price)
        spend = round(quantity * unit_price, 2)

        row = {
            "SourceRowId": f"S{100000 + index}",
            "DataSource": rng.choice(("x.Maximo", "Basware", "NetSuite")),
            "Document number": f"D{rng.randrange(200000, 999999)}",
            "Document line number": rng.randrange(1, 6),
            "Document line desc": description,
            "PO number": f"PO{site.code}{rng.randrange(10000, 99999)}",
            "PO line number": rng.randrange(1, 5) if rng.random() < 0.6 else "",
            "PO line desc": description if rng.random() < 0.3 else "",
            "Invoice number": f"INV-{rng.randrange(100000, 999999)}" if rng.random() < 0.7 else "",
            "Spend in purchase currency": round(spend * (10.7 if site.currency == "SEK" else
                                                         4.3 if site.currency == "PLN" else 1.0), 2),
            "Purchase currency": site.currency,
            "Spend in EUR": spend,
            "Quantity": quantity,
            "Unit": concept.unit,
            "Posting date": _iso_date(rng),
            "ERP supplier number": f"6{abs(hash(supplier.key)) % 10000000:07d}",
            "ERP supplier name": rng.choice((supplier.name,) + supplier.variants),
            "Category L1": category.l1,
            "Category L2": category.l2,
            "Category L3": category.l3,
            "Category L4": category.l4 if rng.random() < 0.85 else "n/a",
            "MaterialGroupNumber": category.group_number,
            "MaterialGroupName": category.group_name,
            "Legal company number": f"9{abs(hash(site.code)) % 900 + 100}",
            "Legal company name": site.company,
            "Country": site.country,
            "Division": site.division,
            "Business area": site.business_area,
        }
        transactions.append(row)

        # The same purchase recorded again in a second system. Agent 1 reports
        # these as duplicates rather than removing them, so a run where none is
        # flagged means the check stopped working.
        if rng.random() < 0.09:
            twin = dict(row)
            twin["SourceRowId"] = f"S{900000 + index}"
            twin["DataSource"] = "NetSuite" if row["DataSource"] != "NetSuite" else "Basware"
            twin["Document number"] = f"D{rng.randrange(200000, 999999)}"
            transactions.append(twin)
            duplicates_planted += 1

    _write_csv(sources / "transaction data" / "sievo_transactions.csv",
               transaction_columns, transactions)
    dataset.files.append(GeneratedFile(
        sources / "transaction data" / "sievo_transactions.csv",
        "Transaction lines with the free text as the ERP holds it",
        "sources", len(transactions), transaction_columns))

    # A purchase-order extract, whose line descriptions are written by buyers
    # rather than generated by a system, plus an internal note in the local
    # language.
    po_columns = ["Order number", "PO line number", "Line description", "Item code",
                  "Supplier name", "Order quantity", "Unit price", "Currency",
                  "Order date", "Order status", "Internal note", "Requested by"]
    po_rows: List[Dict[str, Any]] = []
    for index in range(lines // 3):
        concept = rng.choice(CONCEPTS)
        site = rng.choice(SITES)
        candidates = [s for s in SUPPLIERS if concept.category in s.categories] or list(SUPPLIERS)
        supplier = rng.choice(candidates)
        note = rng.choice((
            f"{rng.randrange(1, 9)} kpl varastoon, toimitusaika {rng.randrange(2, 8)} viikkoa",
            f"Tarjous liitteena, tarjousnumero {rng.randrange(1000000, 9999999)}",
            f"Enligt offert {rng.randrange(10000, 99999)}, leverans {rng.randrange(2, 6)} veckor",
            f"Zgodnie z oferta {rng.randrange(10000, 99999)}",
            f"Confirmed with {rng.choice(('site manager', 'plant engineer'))} on "
            f"{rng.randrange(1, 28)}.{rng.randrange(1, 12)}.2025",
            "",
        ))
        po_rows.append({
            "Order number": f"PO{site.code}{rng.randrange(10000, 99999)}",
            "PO line number": rng.randrange(1, 6),
            "Line description": rough_up(phrases.phrase(concept, site.language), rng, 0.25),
            "Item code": f"{concept.key[:3].upper()}-{rng.randrange(1000, 9999)}",
            "Supplier name": supplier.name,
            "Order quantity": rng.randrange(1, 30),
            "Unit price": _amount(rng, concept.price),
            "Currency": site.currency,
            "Order date": _iso_date(rng),
            "Order status": rng.choice(("Closed", "Open", "Approved")),
            "Internal note": note,
            "Requested by": rng.choice(("Virtanen Anna", "Nieminen Jussi", "Larsson Erik",
                                        "Kowalski Piotr", "Berg Sofia")),
        })
    _write_csv(sources / "po data" / "purchase_orders.csv", po_columns, po_rows)
    dataset.files.append(GeneratedFile(
        sources / "po data" / "purchase_orders.csv",
        "Purchase order lines, with buyers' internal notes",
        "sources", len(po_rows), po_columns))

    # An invoice extract, where the article name is the supplier's own wording
    # rather than Fortum's.
    invoice_columns = ["invoice_key", "row_number", "article_id", "article_name",
                       "quantity_charged", "unit_price_excl_vat", "row_total_excl_vat",
                       "vat_rate", "free_text"]
    invoice_rows: List[Dict[str, Any]] = []
    for index in range(lines // 3):
        concept = rng.choice(CONCEPTS)
        language = rng.choice(("fi", "sv", "pl", "en"))
        quantity = rng.randrange(1, 12)
        price = _amount(rng, concept.price)
        invoice_rows.append({
            "invoice_key": f"INV-{rng.randrange(100000, 999999)}",
            "row_number": rng.randrange(1, 8),
            "article_id": f"ART-{rng.randrange(1000, 9999)}",
            "article_name": rough_up(phrases.phrase(concept, language), rng, 0.3),
            "quantity_charged": quantity,
            "unit_price_excl_vat": price,
            "row_total_excl_vat": round(quantity * price, 2),
            "vat_rate": rng.choice((24, 25, 23, 0)),
            "free_text": rng.choice(("", "n/a", f"Ref {rng.randrange(1000, 9999)}")),
        })
    _write_csv(sources / "invoice data" / "invoice_lines.csv", invoice_columns, invoice_rows)
    dataset.files.append(GeneratedFile(
        sources / "invoice data" / "invoice_lines.csv",
        "Invoice lines described in the supplier's own words",
        "sources", len(invoice_rows), invoice_columns))

    dataset.facts = {
        "transaction_rows": len(transactions),
        "duplicate_pairs": duplicates_planted,
        "languages": sorted(languages_used),
        "concepts": len(CONCEPTS),
    }
    dataset.planted = [
        f"{len(transactions)} transaction lines in {len(languages_used)} languages "
        f"({', '.join(sorted(languages_used))}), which the agent must render in English",
        f"{duplicates_planted} purchases recorded twice in two source systems, which "
        f"must be flagged as duplicates rather than dropped",
        "Descriptions damaged the way ERP exports damage them: upper case, reference "
        "numbers welded to the front, cp1252 mojibake, truncation and 'n/a' placeholders",
        "Both goods and services throughout, so the item-or-service call is exercised",
    ]
    return dataset


# ---------------------------------------------------------------------------
# A table in the shape Agent 1 hands downstream
# ---------------------------------------------------------------------------

# The columns the later agents read. Agent 1 writes many more, but a test input
# only has to carry what the agent under test consumes, and a narrower file is
# far easier to read in the preview.
UNIFIED_SUBSET: Tuple[str, ...] = (
    "Enriched_Purchase_Description", "Enriched_Description_Short", "Item_Or_Service",
    "AI_Confidence", "Detected_Language", "Original_Description",
    "Source_System", "Document_Number", "Document_Line_Number", "PO_Number",
    "PO_Line_Number", "Item_Number", "Supplier_Id", "Supplier_Name",
    "Category_L1", "Category_L2", "Category_L3", "Category_L4",
    "Material_Group_Number", "Material_Group_Name", "Business_Area", "Division",
    "Company_Code", "Company_Name", "Country", "Quantity", "Unit", "Unit_Price",
    "Spend_EUR", "Currency", "Posting_Date", "Row_Id", "Test_Concept",
)


def _unified_row(index: int, concept: Concept, supplier: Supplier, site: Site,
                 description: str, rng: random.Random,
                 group_label: str = "", group_id: str = "") -> Dict[str, Any]:
    """One row in the shape Agent 1 produces, ready for a downstream agent."""
    category = CATEGORIES[concept.category]
    quantity = rng.randrange(1, 25) if concept.unit == "pcs" else rng.randrange(1, 9)
    unit_price = _amount(rng, concept.price)
    row: Dict[str, Any] = {
        "Enriched_Purchase_Description": description,
        "Enriched_Description_Short": " ".join(description.split()[:6]),
        "Item_Or_Service": concept.kind,
        "AI_Confidence": round(rng.uniform(0.62, 0.97), 3),
        "Detected_Language": rng.choice(("fi", "sv", "pl", "en")),
        "Original_Description": description,
        "Source_System": rng.choice(("Maximo", "Basware", "NetSuite")),
        "Document_Number": f"D{rng.randrange(200000, 999999)}",
        "Document_Line_Number": rng.randrange(1, 6),
        "PO_Number": f"PO{site.code}{rng.randrange(10000, 99999)}",
        "PO_Line_Number": rng.randrange(1, 5),
        "Item_Number": f"{concept.key[:3].upper()}-{rng.randrange(1000, 9999)}",
        "Supplier_Id": f"6{abs(hash(supplier.key)) % 10000000:07d}",
        "Supplier_Name": supplier.name,
        "Category_L1": category.l1,
        "Category_L2": category.l2,
        "Category_L3": category.l3,
        "Category_L4": category.l4,
        "Material_Group_Number": category.group_number,
        "Material_Group_Name": category.group_name,
        "Business_Area": site.business_area,
        "Division": site.division,
        "Company_Code": f"9{abs(hash(site.code)) % 900 + 100}",
        "Company_Name": site.company,
        "Country": site.country,
        "Quantity": quantity,
        "Unit": concept.unit,
        "Unit_Price": unit_price,
        "Spend_EUR": round(quantity * unit_price, 2),
        "Currency": site.currency,
        "Posting_Date": _iso_date(rng),
        "Row_Id": f"R{index:06d}",
        "Test_Concept": concept.key,
    }
    if group_label:
        row["AI_Purchase_Group_L5"] = group_label
        row["AI_Purchase_Group_Id"] = group_id
    return row


def _english_variants(concept: Concept, rng: random.Random, count: int) -> List[str]:
    """Several English wordings of one purchase, as Agent 1 would leave them.

    Agent 1 standardises the language but not the phrasing, so what reaches
    Agent 2 is a set of near-synonyms. Reproducing that is the whole point of
    the grouping test: identical strings would group under any implementation.
    """
    base = concept.english
    head = base.split()[0].lower()
    forms = [
        base,
        base.lower(),
        f"{base} - replacement",
        f"{base} for site use",
        f"Supply of {base.lower()}",
        f"{base} (standard)",
        f"{head} {' '.join(base.split()[1:])}".strip(),
        f"{base} incl. delivery",
    ]
    rng.shuffle(forms)
    return [forms[index % len(forms)] for index in range(count)]


# ---------------------------------------------------------------------------
# Agent 2: purchases that ought to group
# ---------------------------------------------------------------------------

def build_agent2_input(root: Path, rng: random.Random, phrases: PhraseSource,
                       per_concept: int = 9) -> Dataset:
    """Write a purchase table where the right grouping is known in advance.

    Each concept appears several times under different wordings and different
    suppliers. Because the concept each row came from is recorded in a column
    Agent 2 carries through untouched, the harness can measure afterwards how
    often rows of one concept ended up in one group.
    """
    dataset = Dataset(agent="agent2", root=root, seed=rng.randrange(1 << 30))
    rows: List[Dict[str, Any]] = []
    index = 0

    for concept in CONCEPTS:
        wordings = _english_variants(concept, rng, per_concept)
        candidates = [s for s in SUPPLIERS if concept.category in s.categories] or list(SUPPLIERS)
        for wording in wordings:
            supplier = rng.choice(candidates)
            site = rng.choice(SITES)
            rows.append(_unified_row(index, concept, supplier, site, wording, rng))
            index += 1

    rng.shuffle(rows)
    path = root / "input" / "agent1_unified_lines.csv"
    _write_csv(path, UNIFIED_SUBSET, rows)
    dataset.files.append(GeneratedFile(
        path, "Enriched purchase lines as Agent 1 hands them on",
        "input", len(rows), list(UNIFIED_SUBSET)))

    dataset.facts = {
        "rows": len(rows),
        "concepts": len(CONCEPTS),
        "rows_per_concept": per_concept,
        "categories": len({concept.category for concept in CONCEPTS}),
    }
    dataset.planted = [
        f"{len(CONCEPTS)} distinct purchases, each written {per_concept} different ways, "
        f"so the correct grouping is known before the agent runs",
        "Wordings differ the way Agent 1 leaves them: case, word order, 'supply of', "
        "'incl. delivery', so identical-string matching would not be enough",
        f"Spread across {len({c.category for c in CONCEPTS})} category branches, so grouping "
        f"has to happen within a category rather than across the whole file",
        "Every row keeps a Test_Concept column, which the harness reads back to measure "
        "how cleanly each concept landed in a single group",
    ]
    return dataset


# ---------------------------------------------------------------------------
# Agent 3: purchases against a catalogue
# ---------------------------------------------------------------------------

def build_agent3_input(root: Path, rng: random.Random, phrases: PhraseSource) -> Dataset:
    """Write a purchase table and a catalogue with a known set of right answers.

    Three populations are planted. Some lines are the catalogue item almost word
    for word and must match. Some describe the same thing in different words and
    should still match, which is what functional equivalence means. The rest have
    no catalogue entry at all, and a few of those repeat often enough and cost
    enough that the agent is supposed to nominate them for the catalogue.
    """
    dataset = Dataset(agent="agent3", root=root, seed=rng.randrange(1 << 30))

    catalogued = [c for c in CONCEPTS if c.kind == "Material"][:8]
    uncatalogued = [c for c in CONCEPTS if c not in catalogued]

    # A catalogue entry describes the item and nothing else. Padding the
    # description with a sentence about where the item is held would dilute the
    # text the agent compares against, and the test would then be measuring the
    # padding rather than the matcher.
    catalogue_columns = ["Supplier", "Item_Name", "Item_Code", "Item_Description",
                         "Specification", "Unit_Price"]
    catalogue: List[Dict[str, Any]] = []
    for position, concept in enumerate(catalogued):
        candidates = [s for s in SUPPLIERS if concept.category in s.categories] or list(SUPPLIERS)
        supplier = candidates[position % len(candidates)]
        catalogue.append({
            "Supplier": supplier.name,
            "Item_Name": concept.english,
            "Item_Code": f"CAT-{1000 + position}",
            "Item_Description": concept.english,
            "Specification": f"Held for {CATEGORIES[concept.category].l3.lower()}; "
                             f"priced per {concept.unit}",
            "Unit_Price": _amount(rng, concept.price),
        })
    catalogue_path = root / "reference" / "item_catalogue.csv"
    _write_csv(catalogue_path, catalogue_columns, catalogue)
    dataset.files.append(GeneratedFile(
        catalogue_path, "The standard item catalogue purchases are matched against",
        "reference", len(catalogue), catalogue_columns))

    rows: List[Dict[str, Any]] = []
    index = 0
    exact_planted = equivalent_planted = 0

    for concept in catalogued:
        candidates = [s for s in SUPPLIERS if concept.category in s.categories] or list(SUPPLIERS)
        # Written as the catalogue writes it: these have to match.
        for _ in range(4):
            rows.append(_unified_row(index, concept, rng.choice(candidates),
                                     rng.choice(SITES), concept.english, rng,
                                     group_label=concept.english,
                                     group_id=f"G{abs(hash(concept.key)) % 9000 + 1000}"))
            index += 1
            exact_planted += 1
        # The same thing in someone else's words: these test equivalence.
        for wording in _english_variants(concept, rng, 3):
            rows.append(_unified_row(index, concept, rng.choice(candidates),
                                     rng.choice(SITES), wording, rng,
                                     group_label=concept.english,
                                     group_id=f"G{abs(hash(concept.key)) % 9000 + 1000}"))
            index += 1
            equivalent_planted += 1

    # Repeat business with no catalogue entry: the candidates the agent should
    # nominate. Deliberately well above the default thresholds of three
    # occurrences and a thousand euro, so a miss is a real miss.
    candidate_concepts = uncatalogued[:4]
    for concept in candidate_concepts:
        candidates = [s for s in SUPPLIERS if concept.category in s.categories] or list(SUPPLIERS)
        for _ in range(6):
            row = _unified_row(index, concept, rng.choice(candidates), rng.choice(SITES),
                               concept.english, rng,
                               group_label=concept.english,
                               group_id=f"G{abs(hash(concept.key)) % 9000 + 1000}")
            row["Quantity"] = rng.randrange(6, 20)
            row["Spend_EUR"] = round(float(row["Quantity"]) * float(row["Unit_Price"]), 2)
            rows.append(row)
            index += 1

    # Ordinary traffic that should match nothing in particular.
    for concept in uncatalogued[4:]:
        candidates = [s for s in SUPPLIERS if concept.category in s.categories] or list(SUPPLIERS)
        for wording in _english_variants(concept, rng, 2):
            rows.append(_unified_row(index, concept, rng.choice(candidates),
                                     rng.choice(SITES), wording, rng,
                                     group_label=concept.english,
                                     group_id=f"G{abs(hash(concept.key)) % 9000 + 1000}"))
            index += 1

    rng.shuffle(rows)
    columns = list(UNIFIED_SUBSET) + ["AI_Purchase_Group_L5", "AI_Purchase_Group_Id"]
    path = root / "input" / "agent2_purchase_groups.csv"
    _write_csv(path, columns, rows)
    dataset.files.append(GeneratedFile(
        path, "Grouped purchase lines as Agent 2 hands them on",
        "input", len(rows), columns))

    dataset.facts = {
        "rows": len(rows),
        "catalogue_items": len(catalogue),
        "exact_wordings": exact_planted,
        "reworded": equivalent_planted,
        "candidate_concepts": [c.english for c in candidate_concepts],
    }
    dataset.planted = [
        f"{exact_planted} lines written exactly as the catalogue writes them, which must "
        f"match at high confidence",
        f"{equivalent_planted} lines describing a catalogued item in different words, which "
        f"must still match if functional equivalence works",
        f"{len(candidate_concepts)} purchases with no catalogue entry, each repeated six times "
        f"and well past the spend threshold, which should be nominated as catalogue candidates",
        "Ordinary one-off traffic that should match nothing, so the agent is judged on "
        "restraint as well as recall",
    ]
    return dataset


# ---------------------------------------------------------------------------
# Agent 4: suppliers that overlap
# ---------------------------------------------------------------------------

def build_agent4_input(root: Path, rng: random.Random, phrases: PhraseSource) -> Dataset:
    """Write a purchase table containing a supplier overlap that is known.

    Two things are planted. One supplier buys nothing that a larger supplier in
    the same category does not also sell, so the smaller one is a consolidation
    opportunity and the agent has to find it. Separately, one supplier appears
    under several spellings; the agent has to recognise them as one company,
    because otherwise its spend is split three ways and every ranking built on
    that number is wrong.
    """
    dataset = Dataset(agent="agent4", root=root, seed=rng.randrange(1 << 30))
    rows: List[Dict[str, Any]] = []
    index = 0

    # The pair the test turns on. Nordvalve trades across four categories;
    # Kaukoteknik trades in three of them and in nothing else.
    broad = SUPPLIER_BY_KEY["nordvalve"]
    narrow = SUPPLIER_BY_KEY["kaukoteknik"]
    shared_concepts = [c for c in CONCEPTS
                       if c.category in set(broad.categories) & set(narrow.categories)]

    for concept in shared_concepts:
        for _ in range(6):
            site = rng.choice(SITES)
            # Both suppliers sell the same purchases, so the smaller portfolio
            # is fully covered by the larger one.
            for supplier, spelling in ((broad, rng.choice((broad.name,) + broad.variants)),
                                       (narrow, narrow.name)):
                row = _unified_row(index, concept, supplier, site,
                                   rng.choice(_english_variants(concept, rng, 3)), rng,
                                   group_label=concept.english,
                                   group_id=f"G{abs(hash(concept.key)) % 9000 + 1000}")
                row["Supplier_Name"] = spelling
                row["Quantity"] = rng.randrange(8, 30)
                row["Spend_EUR"] = round(float(row["Quantity"]) * float(row["Unit_Price"]), 2)
                rows.append(row)
                index += 1

    # Everyone else, trading only where they are supposed to, so the planted
    # overlap has to be found among genuine variety rather than in isolation.
    for supplier in SUPPLIERS:
        if supplier.key in {broad.key, narrow.key}:
            continue
        for concept in [c for c in CONCEPTS if c.category in supplier.categories]:
            for _ in range(rng.randrange(3, 7)):
                row = _unified_row(index, concept, supplier, rng.choice(SITES),
                                   rng.choice(_english_variants(concept, rng, 3)), rng,
                                   group_label=concept.english,
                                   group_id=f"G{abs(hash(concept.key)) % 9000 + 1000}")
                row["Quantity"] = rng.randrange(4, 24)
                row["Spend_EUR"] = round(float(row["Quantity"]) * float(row["Unit_Price"]), 2)
                rows.append(row)
                index += 1

    rng.shuffle(rows)
    columns = list(UNIFIED_SUBSET) + ["AI_Purchase_Group_L5", "AI_Purchase_Group_Id"]
    path = root / "input" / "agent2_purchase_groups.csv"
    _write_csv(path, columns, rows)
    dataset.files.append(GeneratedFile(
        path, "Grouped purchase lines with supplier spend, as Agent 2 hands them on",
        "input", len(rows), columns))

    spellings = sorted({broad.name, *broad.variants})
    dataset.facts = {
        "rows": len(rows),
        "suppliers": len({row["Supplier_Name"] for row in rows}),
        "covered_supplier": narrow.name,
        "covering_supplier": broad.name,
        "spelling_variants": spellings,
        "shared_categories": sorted(set(broad.categories) & set(narrow.categories)),
    }
    dataset.planted = [
        f"{narrow.name} buys nothing that {broad.name} does not also sell, so it is a "
        f"consolidation opportunity the agent has to surface",
        f"{broad.name} appears under {len(spellings)} spellings "
        f"({'; '.join(spellings)}), which must resolve to one supplier",
        f"Both sides carry well over the default thresholds of three lines and "
        f"EUR 10,000 of addressable spend, so a miss cannot be blamed on a threshold",
        f"{len(SUPPLIERS) - 2} other suppliers trading only in their own categories, so the "
        f"overlap has to be found among genuine variety",
    ]
    return dataset


# ---------------------------------------------------------------------------
# Max: extracts that are supposed to join
# ---------------------------------------------------------------------------

def build_max_sources(root: Path, rng: random.Random, phrases: PhraseSource,
                      lines: int = 90) -> Dataset:
    """Write transaction, invoice and purchase-order extracts that share keys.

    Max is judged on whether it joins what can be joined and widens rather than
    lengthens the table, so the keys are laid down deliberately: a known share of
    transactions carry an invoice number and a line number that exist on the
    invoice extract, and a purchase order and line that exist on the PO extract.
    The rest are left unmatchable on purpose, because a join that matches
    everything has not been asked a hard question.
    """
    dataset = Dataset(agent="max", root=root, seed=rng.randrange(1 << 30))
    sources = root / "sources"

    invoice_columns = ["xml_file_name", "invoice_key", "invoice_id", "row_number",
                       "article_id", "quantity_charged", "unit_price_excl_vat",
                       "unit_price_net", "row_total_excl_vat", "row_total_incl_vat",
                       "vat_amount", "vat_rate", "article_name", "quantity_delivered",
                       "free_text"]
    po_columns = ["Order number", "PO line number", "Requisition number",
                  "Supplier product name", "PO net sum company", "Main category",
                  "Sub category", "Supplier name", "Supplier code", "Order status",
                  "PO line quantity", "PO currency company", "PO creation date",
                  "Item type", "Company name", "Project name"]
    transaction_columns = [
        "SourceRowId", "DataSource", "Document number", "Document line number",
        "Document line desc", "PO number", "PO line number", "PO line desc",
        "Invoice number", "Spend in EUR", "Quantity", "Posting date",
        "ERP supplier name", "Category L1", "Category L2", "Category L3",
        "Category L4", "MaterialGroupName", "Legal company name", "Country",
        "Division", "Business area", "DocumentIdentifier", "InvoiceLink",
    ]

    transactions: List[Dict[str, Any]] = []
    invoices: List[Dict[str, Any]] = []
    orders: List[Dict[str, Any]] = []

    invoice_matched = po_matched = 0

    for index in range(lines):
        concept = rng.choice(CONCEPTS)
        site = rng.choice(SITES)
        candidates = [s for s in SUPPLIERS if concept.category in s.categories] or list(SUPPLIERS)
        supplier = rng.choice(candidates)
        category = CATEGORIES[concept.category]

        quantity = rng.randrange(1, 20)
        unit_price = _amount(rng, concept.price)
        spend = round(quantity * unit_price, 2)

        invoice_key = f"INV-{500000 + index}"
        order_number = f"PO{site.code}{20000 + index}"
        line_number = rng.randrange(1, 4)

        # Roughly seven in ten transactions are given a counterpart on each side.
        has_invoice = rng.random() < 0.7
        has_order = rng.random() < 0.7

        document_id = f"{abs(hash((index, 'doc'))):032x}"[:32]

        transactions.append({
            "SourceRowId": f"T{200000 + index}",
            "DataSource": rng.choice(("x.Maximo", "Basware")),
            "Document number": f"D{700000 + index}",
            "Document line number": line_number,
            "Document line desc": phrases.phrase(concept, site.language),
            "PO number": order_number if has_order else "",
            "PO line number": line_number if has_order and rng.random() < 0.75 else "",
            "PO line desc": "",
            "Invoice number": invoice_key if has_invoice else "",
            "Spend in EUR": spend,
            "Quantity": quantity,
            "Posting date": _iso_date(rng),
            "ERP supplier name": supplier.name,
            "Category L1": category.l1,
            "Category L2": category.l2,
            "Category L3": category.l3,
            "Category L4": category.l4,
            "MaterialGroupName": category.group_name,
            "Legal company name": site.company,
            "Country": site.country,
            "Division": site.division,
            "Business area": site.business_area,
            "DocumentIdentifier": document_id,
            "InvoiceLink": f"http://imageviewer.internal/?docid={document_id}",
        })

        if has_invoice:
            invoice_matched += 1
            price = round(unit_price, 2)
            total = round(quantity * price, 2)
            invoices.append({
                "xml_file_name": f"{document_id}_{index}_invoice.xml",
                "invoice_key": invoice_key,
                "invoice_id": invoice_key,
                "row_number": line_number,
                "article_id": f"ART-{2000 + index}",
                "quantity_charged": quantity,
                "unit_price_excl_vat": price,
                "unit_price_net": price,
                "row_total_excl_vat": total,
                "row_total_incl_vat": round(total * 1.24, 2),
                "vat_amount": round(total * 0.24, 2),
                "vat_rate": 24,
                "article_name": phrases.phrase(concept, "en"),
                "quantity_delivered": quantity,
                "free_text": rng.choice(("", f"Ref {rng.randrange(1000, 9999)}")),
            })

        if has_order:
            po_matched += 1
            orders.append({
                "Order number": order_number,
                "PO line number": line_number,
                "Requisition number": f"PR{site.code}{20000 + index}",
                "Supplier product name": phrases.phrase(concept, "en"),
                "PO net sum company": spend,
                "Main category": category.l2,
                "Sub category": category.l3,
                "Supplier name": supplier.name,
                "Supplier code": f"6{abs(hash(supplier.key)) % 10000000:07d}",
                "Order status": rng.choice(("Closed", "Open")),
                "PO line quantity": quantity,
                "PO currency company": site.currency,
                "PO creation date": _iso_date(rng),
                "Item type": "Freetext",
                "Company name": site.company,
                "Project name": rng.choice(("Network renewal", "Turbine overhaul",
                                            "Heat plant upgrade", "Grid connection")),
            })

    _write_csv(sources / "transaction data" / "sievo_transactions.csv",
               transaction_columns, transactions)
    _write_csv(sources / "invoice data" / "invoice_line_data.csv",
               invoice_columns, invoices)
    _write_csv(sources / "po data" / "Basware PO data.csv", po_columns, orders)

    dataset.files.extend([
        GeneratedFile(sources / "transaction data" / "sievo_transactions.csv",
                      "Transaction lines carrying invoice and purchase-order keys",
                      "sources", len(transactions), transaction_columns),
        GeneratedFile(sources / "invoice data" / "invoice_line_data.csv",
                      "Invoice lines keyed by invoice number and row number",
                      "sources", len(invoices), invoice_columns),
        GeneratedFile(sources / "po data" / "Basware PO data.csv",
                      "Purchase order lines keyed by order number and line number",
                      "sources", len(orders), po_columns),
    ])

    dataset.facts = {
        "transaction_rows": len(transactions),
        "invoice_rows": len(invoices),
        "po_rows": len(orders),
        "invoice_matchable": invoice_matched,
        "po_matchable": po_matched,
    }
    dataset.planted = [
        f"{invoice_matched} of {len(transactions)} transactions have an invoice line that "
        f"can be reached by invoice number and line number",
        f"{po_matched} of {len(transactions)} transactions have a purchase order line that "
        f"can be reached by order number and line number",
        "Every transaction also carries a document identifier that appears in the invoice "
        "file name, so the fallback key path is exercised",
        "The remainder are unmatchable on purpose: a join that matches everything has not "
        "been asked a hard question",
    ]
    return dataset


# ===========================================================================
# What each test consists of
# ===========================================================================

@dataclass
class CheckResult:
    """One thing that was verified after the agent finished."""

    name: str
    status: str                # pass | warn | fail
    detail: str
    measured: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "measured": self.measured}


def read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    """Read a result file written by an agent."""
    if not path.is_file():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        rows = [{key: (value or "") for key, value in row.items() if key is not None}
                for row in reader]
    return columns, rows


def _files_exist(results: Path, names: Sequence[str]) -> List[CheckResult]:
    checks: List[CheckResult] = []
    for name in names:
        path = results / name
        checks.append(CheckResult(
            f"{name} was written",
            "pass" if path.is_file() else "fail",
            "The agent's deliverable must exist before anything else can be judged.",
            f"{path.stat().st_size:,} bytes" if path.is_file() else "missing"))
    return checks


def check_agent1(dataset: Dataset, results: Path) -> List[CheckResult]:
    checks = _files_exist(results, ["agent1_unified_lines.csv"])
    columns, rows = read_csv(results / "agent1_unified_lines.csv")
    if not rows:
        return checks

    described = sum(1 for row in rows if row.get("Enriched_Purchase_Description", "").strip())
    checks.append(CheckResult(
        "Every line got an English description",
        "pass" if described == len(rows) else "fail",
        "The description is the deliverable. A blank one is a line the agent gave up on.",
        f"{described:,} of {len(rows):,}"))

    languages = {row.get("Detected_Language", "") for row in rows} - {""}
    planted = set(dataset.facts.get("languages", []))
    found = languages & planted
    checks.append(CheckResult(
        "The planted languages were recognised",
        "pass" if len(found) >= max(2, len(planted) - 1) else "warn",
        f"The input was written in {', '.join(sorted(planted))}.",
        f"{', '.join(sorted(found)) or 'none'} detected"))

    # The agent is allowed a third answer, Unclear, and using it on text that has
    # been truncated or upper-cased is better behaviour than guessing. What is
    # checked is that both real classes are found and that the agent is not
    # retreating into Unclear for most of the file.
    materials = sum(1 for row in rows if row.get("Item_Or_Service") == "Material")
    services = sum(1 for row in rows if row.get("Item_Or_Service") == "Service")
    unclear = len(rows) - materials - services
    checks.append(CheckResult(
        "Goods were told apart from services",
        "pass" if materials and services and unclear < len(rows) * 0.5 else "warn",
        "Both were planted in roughly equal measure, so both should appear. Lines the "
        "generator deliberately damaged are entitled to come back Unclear.",
        f"{materials:,} goods, {services:,} services, {unclear:,} unclear"))

    if dataset.facts.get("duplicate_pairs"):
        flagged = sum(1 for row in rows if row.get("Is_Duplicate", "").strip().lower()
                      in {"yes", "true", "1"})
        checks.append(CheckResult(
            "Purchases recorded twice were flagged",
            "pass" if flagged else "warn",
            f"{dataset.facts['duplicate_pairs']} transactions were written into two source "
            f"systems each.",
            f"{flagged:,} rows flagged"))

    # Agent 1 reports confidence as a percentage. Scaling here rather than
    # assuming a range keeps the check working if that ever changes.
    confidences = [float(row["AI_Confidence"]) for row in rows
                   if _is_number(row.get("AI_Confidence"))]
    if confidences:
        if max(confidences) > 1.0:
            confidences = [value / 100.0 for value in confidences]
        mean = sum(confidences) / len(confidences)
        spread = max(confidences) - min(confidences)
        checks.append(CheckResult(
            "Confidence is being scored, not asserted",
            "pass" if 0.05 < mean < 0.995 and spread > 0.05 else "warn",
            "A score that never moves is not a score. It should sit below certainty and "
            "vary with how much evidence each line offered.",
            f"mean {mean:.0%}, from {min(confidences):.0%} to {max(confidences):.0%}"))
    return checks


def check_agent2(dataset: Dataset, results: Path) -> List[CheckResult]:
    checks = _files_exist(results, ["agent2_purchase_groups.csv", "agent2_group_directory.csv"])
    columns, rows = read_csv(results / "agent2_purchase_groups.csv")
    if not rows:
        return checks

    expected = dataset.facts.get("rows", 0)
    checks.append(CheckResult(
        "No purchase line was lost",
        "pass" if len(rows) == expected else "fail",
        "Grouping labels rows; it must not add or drop them.",
        f"{len(rows):,} out, {expected:,} in"))

    assigned = sum(1 for row in rows if row.get("AI_Purchase_Group_Id", "").strip())
    checks.append(CheckResult(
        "Every line was placed in a group",
        "pass" if assigned == len(rows) else "fail",
        "An unassigned line cannot be analysed at Category L5.",
        f"{assigned:,} of {len(rows):,}"))

    groups = {row.get("AI_Purchase_Group_Id", "") for row in rows} - {""}
    checks.append(CheckResult(
        "Grouping actually reduced the data",
        "pass" if 0 < len(groups) < len(rows) else "fail",
        "One group per line, or one group for everything, is not a grouping.",
        f"{len(groups):,} groups from {len(rows):,} lines"))

    # The measurement the dataset was built for: rows that came from one concept
    # should have landed in one group.
    if "Test_Concept" in columns:
        purities: List[float] = []
        for concept in {row.get("Test_Concept", "") for row in rows} - {""}:
            members = [row for row in rows if row.get("Test_Concept") == concept]
            counts: Dict[str, int] = {}
            for member in members:
                key = member.get("AI_Purchase_Group_Id", "")
                counts[key] = counts.get(key, 0) + 1
            purities.append(max(counts.values()) / len(members))
        purity = sum(purities) / len(purities) if purities else 0.0
        checks.append(CheckResult(
            "Different wordings of one purchase landed together",
            "pass" if purity >= 0.8 else ("warn" if purity >= 0.6 else "fail"),
            "Each planted purchase was written several ways. This is the share of its "
            "lines that ended up in the same group.",
            f"{purity * 100:.0f}% of lines in the dominant group, averaged over "
            f"{len(purities)} purchases"))

    labels = {row.get("AI_Purchase_Group_L5", "") for row in rows} - {""}
    empty_labels = sum(1 for row in rows if not row.get("AI_Purchase_Group_L5", "").strip())
    checks.append(CheckResult(
        "Groups were given readable names",
        "pass" if not empty_labels else "warn",
        "A group nobody can name is a group nobody will use.",
        f"{len(labels):,} distinct labels, {empty_labels:,} blank"))
    return checks


def check_agent3(dataset: Dataset, results: Path) -> List[CheckResult]:
    checks = _files_exist(results, ["agent3_standardisation.csv"])
    columns, rows = read_csv(results / "agent3_standardisation.csv")
    if not rows:
        return checks

    expected = dataset.facts.get("rows", 0)
    checks.append(CheckResult(
        "No purchase line was lost",
        "pass" if len(rows) == expected else "fail",
        "Matching annotates rows; it must not add or drop them.",
        f"{len(rows):,} out, {expected:,} in"))

    matched_column = _first_column(columns, ("Matched_Item_ID", "Matched_Item_Description"))
    band_column = _first_column(columns, ("Match_Band",))

    if matched_column:
        matched = sum(1 for row in rows if row.get(matched_column, "").strip())
        planted = dataset.facts.get("exact_wordings", 0)
        checks.append(CheckResult(
            "Catalogued purchases were matched to their item",
            "pass" if matched >= planted else ("warn" if matched else "fail"),
            f"{planted} lines repeat a catalogue item word for word, and a further "
            f"{dataset.facts.get('reworded', 0)} describe one in different words.",
            f"{matched:,} of {len(rows):,} lines matched an item"))

    if band_column:
        bands: Dict[str, int] = {}
        for row in rows:
            band = row.get(band_column, "").strip() or "None"
            bands[band] = bands.get(band, 0) + 1
        confident = bands.get("High", 0) + bands.get("Medium", 0)
        checks.append(CheckResult(
            "Some matches were confident enough to act on",
            "pass" if bands.get("High") else ("warn" if confident else "fail"),
            "Lines written exactly as the catalogue writes them were planted, so the top "
            "band should be reached rather than everything landing on 'possible'.",
            ", ".join(f"{name} {count:,}" for name, count in sorted(bands.items()))))

    # A match that never refuses is not a match. The dataset contains one-off
    # traffic with no catalogue entry, and those lines should come back empty.
    if band_column:
        refused = sum(1 for row in rows if (row.get(band_column, "").strip() or "None") == "None")
        checks.append(CheckResult(
            "Purchases with no catalogue entry were left alone",
            "pass" if refused else "warn",
            "Restraint matters as much as recall: a matcher that always answers cannot "
            "be trusted when it does.",
            f"{refused:,} of {len(rows):,} lines correctly matched nothing"))

    candidates_path = results / "agent3_catalogue_candidates.csv"
    _, candidates = read_csv(candidates_path)
    planted_candidates = dataset.facts.get("candidate_concepts", [])
    checks.append(CheckResult(
        "Repeat purchases with no catalogue entry were nominated",
        "pass" if candidates else "warn",
        f"{len(planted_candidates)} purchases were repeated six times each, above the "
        f"default thresholds, with no catalogue item to match.",
        f"{len(candidates):,} candidates proposed"))
    return checks


def check_agent4(dataset: Dataset, results: Path) -> List[CheckResult]:
    checks = _files_exist(results, ["agent4_supplier_consolidation.csv",
                                    "agent4_supplier_master.csv"])
    master_columns, master = read_csv(results / "agent4_supplier_master.csv")
    _, consolidation = read_csv(results / "agent4_supplier_consolidation.csv")

    spellings = dataset.facts.get("spelling_variants", [])
    if master and spellings:
        # The variants must have collapsed: fewer suppliers in the master than
        # distinct spellings in the input is the whole point of a supplier
        # master, and the ranking is meaningless without it.
        distinct_input = dataset.facts.get("suppliers", 0)
        checks.append(CheckResult(
            "Spellings of one company resolved to one supplier",
            "pass" if len(master) < distinct_input else "fail",
            f"{len(spellings)} spellings of the same company were planted "
            f"({'; '.join(spellings)}).",
            f"{len(master):,} suppliers in the master, {distinct_input:,} spellings in the input"))

    if consolidation:
        covered = dataset.facts.get("covered_supplier", "")
        haystack = "\n".join(json.dumps(row, ensure_ascii=False) for row in consolidation).lower()
        found = covered.lower() in haystack if covered else False
        checks.append(CheckResult(
            "The planted overlap was found",
            "pass" if found else "warn",
            f"{covered} buys nothing that {dataset.facts.get('covering_supplier', '')} "
            f"does not also sell.",
            "named in the consolidation report" if found else "not named"))

        band_column = next((name for name in consolidation[0]
                            if "band" in name.lower() or "rating" in name.lower()), "")
        if band_column:
            bands: Dict[str, int] = {}
            for row in consolidation:
                band = row.get(band_column, "").strip() or "None"
                bands[band] = bands.get(band, 0) + 1
            actionable = bands.get("High", 0) + bands.get("Medium", 0)
            checks.append(CheckResult(
                "Opportunities were graded, not just listed",
                "pass" if actionable else "warn",
                "The planted pair clears the default spend and evidence thresholds.",
                ", ".join(f"{name} {count:,}" for name, count in sorted(bands.items()))))
    else:
        checks.append(CheckResult(
            "The planted overlap was found", "fail",
            "A supplier whose entire portfolio is covered by another was planted.",
            "the consolidation report is empty"))
    return checks


def check_max(dataset: Dataset, results: Path) -> List[CheckResult]:
    checks = _files_exist(results, ["max_stage1_sievo_invoice.csv",
                                    "max_stage2_with_po.csv",
                                    "max_stage3_interpreted.csv"])
    _, stage1 = read_csv(results / "max_stage1_sievo_invoice.csv")
    columns3, stage3 = read_csv(results / "max_stage3_interpreted.csv")
    if not stage3:
        return checks

    expected = dataset.facts.get("transaction_rows", 0)
    checks.append(CheckResult(
        "The join widened the table without lengthening it",
        "pass" if len(stage3) == expected else "fail",
        "One transaction must stay one row, or spend has been multiplied by a join.",
        f"{len(stage3):,} rows out, {expected:,} transactions in"))

    invoice_matched = sum(1 for row in stage1
                          if row.get("Invoice_Match_Level", "none") != "none")
    planted = dataset.facts.get("invoice_matchable", 0)
    checks.append(CheckResult(
        "Invoice lines were joined where a key existed",
        "pass" if invoice_matched >= planted * 0.9 else
        ("warn" if invoice_matched else "fail"),
        f"{planted} transactions were given a reachable invoice line.",
        f"{invoice_matched:,} matched, {planted:,} matchable"))

    po_matched = sum(1 for row in stage3 if row.get("PO_Match_Level", "none") != "none")
    planted_po = dataset.facts.get("po_matchable", 0)
    checks.append(CheckResult(
        "Purchase order lines were joined where a key existed",
        "pass" if po_matched >= planted_po * 0.9 else ("warn" if po_matched else "fail"),
        f"{planted_po} transactions were given a reachable purchase order line.",
        f"{po_matched:,} matched, {planted_po:,} matchable"))

    if "Interpreted_Description" in columns3:
        read_rows = sum(1 for row in stage3 if row.get("Interpreted_Description", "").strip())
        checks.append(CheckResult(
            "Free text was turned into structured columns",
            "pass" if read_rows >= len(stage3) * 0.9 else "warn",
            "Every transaction carries a description in one of four languages.",
            f"{read_rows:,} of {len(stage3):,} rows interpreted"))
    return checks


def _first_column(columns: Sequence[str], wanted: Sequence[str]) -> str:
    """The first of ``wanted`` that the agent actually wrote."""
    available = {name.lower(): name for name in columns}
    for candidate in wanted:
        if candidate.lower() in available:
            return available[candidate.lower()]
    return ""


def _is_number(value: Any) -> bool:
    try:
        float(str(value))
        return True
    except (TypeError, ValueError):
        return False


# ===========================================================================
# The agents on offer
# ===========================================================================

@dataclass
class AgentSpec:
    """One testable agent: how to feed it, how to run it, how to judge it."""

    key: str
    number: str
    name: str
    tagline: str
    proves: str
    about: str
    intake: str
    deliverable: str
    value: str
    script: str
    build: Callable[[Path, random.Random, PhraseSource], Dataset]
    command: Callable[[Dataset, Path, Path], List[str]]
    check: Callable[[Dataset, Path], List[CheckResult]]
    outputs: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "number": self.number, "name": self.name,
            "tagline": self.tagline, "proves": self.proves, "script": self.script,
            "about": self.about, "intake": self.intake,
            "deliverable": self.deliverable, "value": self.value,
            "outputs": list(self.outputs),
        }


def _common(results: Path, cache: Path) -> List[str]:
    """Arguments every agent takes, so a test never waits on a prompt."""
    return ["--non-interactive",
            "--results", str(results),
            "--lexicon", str(HERE / "lexicon" / "procurement_lexicon.json"),
            "--cache", str(cache)]


AGENTS: Tuple[AgentSpec, ...] = (
    AgentSpec(
        key="agent1", number="01", name="Improved Purchase Description",
        tagline="Turns ERP free text in any language into one clean English description.",
        proves="That four languages, damaged encodings and welded reference numbers all "
               "come out as readable English, with duplicates flagged and confidence scored.",
        about="This agent reads a purchase line as it was typed into the ERP and writes "
              "one clear English sentence of what Fortum actually bought.",
        intake="The raw extracts from Sievo, purchase orders and invoices — Finnish, "
               "Swedish, Polish or English, often abbreviated, truncated or mixed.",
        deliverable="A single English description on every line, with a confidence "
                    "score and whether the buy is a good or a service.",
        value="Category managers can read the spend without decoding four languages "
              "or guessing what a half-finished ERP field was meant to say.",
        script="agent1.py",
        build=lambda root, rng, phrases: build_agent1_sources(root, rng, phrases),
        command=lambda dataset, results, cache: (
            ["--sources", str(dataset.root / "sources")] + _common(results, cache)),
        check=check_agent1,
        outputs=("agent1_unified_lines.csv",),
    ),
    AgentSpec(
        key="agent2", number="02", name="AI Purchase Group (Category L5)",
        tagline="Gathers purchases that mean the same thing into one named group.",
        proves="That the same purchase written nine different ways lands in a single group, "
               "within its category, under a label a person would recognise.",
        about="This agent gathers purchases that mean the same thing — even when they "
              "were written differently — into a named group beneath today's categories.",
        intake="The cleaned English descriptions from Agent 1, still sitting in "
               "Fortum's existing L1 to L4 category tree.",
        deliverable="Every line placed in a named Category L5 group, plus a directory "
                    "of those groups a person can browse.",
        value="Spend can be read at a level finer than today's categories, so similar "
              "buys stop hiding behind different wording.",
        script="agent2.py",
        build=lambda root, rng, phrases: build_agent2_input(root, rng, phrases),
        command=lambda dataset, results, cache: (
            ["--input", str(dataset.root / "input" / "agent1_unified_lines.csv"),
             "--registry", str(dataset.root / "group_registry.json")]
            + _common(results, cache)),
        check=check_agent2,
        outputs=("agent2_purchase_groups.csv", "agent2_group_directory.csv"),
    ),
    AgentSpec(
        key="agent3", number="03", name="Material and Service Standardisation",
        tagline="Matches purchases to catalogue items, and nominates what is missing.",
        proves="That a catalogued item is recognised however it was worded, and that repeat "
               "purchases with no catalogue entry are put forward as candidates.",
        about="This agent asks, for each purchase, whether Fortum already has a standard "
              "item for it — and if not, whether the buy is repeating often enough to deserve one.",
        intake="The grouped purchase lines from Agent 2, together with Fortum's item "
               "catalogues and price lists.",
        deliverable="A match to a catalogue item where one exists, and a shortlist of "
                    "repeat purchases that should be added to the catalogue.",
        value="More of the spend can go through contracted items, and the catalogue "
              "grows where the business is already buying the same thing again and again.",
        script="agent3.py",
        build=lambda root, rng, phrases: build_agent3_input(root, rng, phrases),
        command=lambda dataset, results, cache: (
            ["--input", str(dataset.root / "input" / "agent2_purchase_groups.csv"),
             "--reference", str(dataset.root / "reference")] + _common(results, cache)),
        check=check_agent3,
        outputs=("agent3_standardisation.csv", "agent3_catalogue_candidates.csv"),
    ),
    AgentSpec(
        key="agent4", number="04", name="Supplier Consolidation",
        tagline="Finds suppliers whose portfolios overlap enough to be merged.",
        proves="That three spellings of one company resolve to one supplier, and that a "
               "supplier selling nothing another does not is surfaced as an opportunity.",
        about="This agent finds suppliers whose ranges overlap enough that Fortum could "
              "buy the same things from fewer of them.",
        intake="The grouped purchase lines from Agent 2, with supplier names and spend.",
        deliverable="A supplier master that collapses spelling variants, and a ranked "
                    "list of consolidation opportunities with the euro at stake attached.",
        value="Category managers see where a smaller supplier's entire range is already "
              "covered by a larger one, so the conversation about reducing the tail has numbers.",
        script="agent4.py",
        build=lambda root, rng, phrases: build_agent4_input(root, rng, phrases),
        command=lambda dataset, results, cache: (
            ["--input", str(dataset.root / "input" / "agent2_purchase_groups.csv"),
             "--registry", str(dataset.root / "supplier_registry.json")]
            + _common(results, cache)),
        check=check_agent4,
        outputs=("agent4_supplier_consolidation.csv", "agent4_supplier_pairs.csv",
                 "agent4_supplier_master.csv"),
    ),
)

AGENT_BY_KEY: Dict[str, AgentSpec] = {spec.key: spec for spec in AGENTS}


# ===========================================================================
# Reading the log back in plain English
# ===========================================================================

_FILE_STORY_SYSTEM = (
    "You describe a synthetic procurement file to a Fortum category manager "
    "who is not a programmer.\n"
    "Return JSON only, as {\"lines\": [\"...\", \"...\"]}.\n"
    "Rules:\n"
    "- Exactly two short sentences.\n"
    "- Say what the file holds and why a buyer would look at it.\n"
    "- No file paths, no column names, no jargon, no mention that the data is synthetic."
)

# Used when the model is off. Written in the same voice the model is asked for,
# so the screen does not change register when a key is missing.
_FILE_STORY_FALLBACK: Dict[str, str] = {
    "sievo_transactions.csv":
        "The purchase lines as they sit in Fortum's spend cube — one row per "
        "transaction, in the language the buyer typed. This is the file the "
        "agent has to make readable.",
    "purchase_orders.csv":
        "The purchase-order lines as a buyer wrote them, including the internal "
        "note they left for the site. It is the second view of the same buys.",
    "invoice_lines.csv":
        "The invoice lines in the supplier's own wording, with quantities and "
        "amounts. It is what arrived on the bill, not what Fortum asked for.",
    "agent1_unified_lines.csv":
        "The cleaned English descriptions of what was bought, still sitting in "
        "today's categories. This is what Agent 1 would hand on.",
    "agent2_purchase_groups.csv":
        "The same purchase lines after they have been placed in a named group. "
        "This is what a category manager would read after Agent 2 has run.",
    "item_catalogue.csv":
        "The standard items Fortum already holds on the catalogue, with a "
        "price and a supplier. The agent matches live purchases against this list.",
}

_NARRATION_SYSTEM = (
    "You are watching a data-processing agent run and explaining it to a "
    "procurement analyst who is not a programmer.\n"
    "Return JSON only, as {\"note\": \"...\"}.\n"
    "Rules:\n"
    "- One sentence, at most twenty words, in the present tense.\n"
    "- Say what the agent is doing and why it matters, not what the log says.\n"
    "- No log levels, no timestamps, no file paths, no jargon.\n"
    "- If the lines show a problem, say plainly what went wrong."
)

_VERDICT_SYSTEM = (
    "You are reporting the result of a test of a procurement data agent to the "
    "team that owns it.\n"
    "Return JSON only, as {\"headline\": \"...\", \"points\": [\"...\"], "
    "\"advice\": \"...\"}.\n"
    "Rules:\n"
    "- headline: one sentence saying whether the agent did its job.\n"
    "- points: two to four short sentences on what the evidence shows. Quote the "
    "measured figures you were given rather than inventing any.\n"
    "- advice: one sentence on what to look at next, or an empty string if "
    "everything passed.\n"
    "- Plain English. No jargon, no log levels, no file paths."
)

# The agents log through "%(asctime)s  %(levelname)-7s %(message)s", and indent
# anything that is a detail of the line above it. Both facts are used below to
# tell the lines worth repeating from the ones that only matter in the full log.
_LOG_PREFIX = re.compile(r"^\d{2}:\d{2}:\d{2}\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)")
_LEVEL_WIDTH = 7


_LONG_PATH = re.compile(r"(?:/[^\s/]+){3,}")


def shorten_paths(text: str) -> str:
    """Collapse absolute paths to their last segment.

    Someone reading the commentary already knows where the run is happening.
    A full path buries the sentence it appears in, and the log beside it keeps
    the whole thing for when it is actually needed.
    """
    return _LONG_PATH.sub(lambda match: ".../" + match.group(0).rsplit("/", 1)[-1], text)


def log_message(line: str) -> Tuple[str, str]:
    """Split an agent's log line into its level and its message.

    The separator after the level is worked out from the padding width rather
    than matched as "whatever whitespace follows", because the message's own
    indentation is the signal that it is sub-detail, and a greedy match would
    swallow exactly the thing being looked for.

    Returns empty strings for anything that is not a log line, such as the
    command the harness echoes before starting.
    """
    match = _LOG_PREFIX.match(line)
    if not match:
        return "", ""
    level = match.group(1)
    separator = max(_LEVEL_WIDTH - len(level), 0) + 1
    return level, line[match.end() + separator:]


# Phrases the agents write, and what they mean. Used when there is no model, so
# that the running commentary is useful either way.
_LOG_SIGNALS: Tuple[Tuple[str, str], ...] = (
    ("optional components", "Checking which optional accelerators are installed."),
    ("vocabulary", "Loading the controlled procurement vocabulary."),
    ("reading", "Reading the source extracts."),
    ("discover", "Working out which file is which."),
    ("translat", "Translating the descriptions into English."),
    ("language", "Working out what language each line is written in."),
    ("embedding", "Comparing descriptions by meaning rather than by spelling."),
    ("cluster", "Gathering purchases that mean the same thing."),
    ("group", "Forming and naming the purchase groups."),
    ("match", "Matching purchases against the catalogue."),
    ("candidate", "Looking for repeat purchases that deserve a catalogue entry."),
    ("supplier", "Building the supplier master and comparing portfolios."),
    ("overlap", "Measuring how much of one supplier's portfolio another covers."),
    ("stage 1", "Joining invoice lines onto the transactions."),
    ("stage 2", "Joining purchase order lines onto the widened table."),
    ("stage 3", "Reading the free text on every row into structured columns."),
    ("cache", "Reusing answers cached from an earlier run, at no cost."),
    ("writing", "Writing the results out."),
    ("complete", "Finishing up and summarising the run."),
)


class Narrator:
    """Turns an agent's log into something worth reading.

    The model is asked for a sentence whenever a batch of new lines has built
    up, and is capped so that a long run cannot quietly become an expensive one.
    Without a model the same job is done from the phrase table above: less
    fluent, but it never leaves the machine and it never costs anything.
    """

    MAX_NOTES = 14

    def __init__(self, model: Optional[LanguageModel], spec: AgentSpec) -> None:
        self.model = model
        self.spec = spec
        self.notes = 0
        self._seen: set = set()

    @property
    def uses_model(self) -> bool:
        return bool(self.model and self.model.available)

    # A batch of log lines can be worth more than one remark, and often is: an
    # agent will say what it loaded, what it resolved and what it built within
    # a few lines of each other. Returning a list rather than a single note
    # keeps all three, in the order they happened.
    MAX_PER_BATCH = 3

    def note(self, lines: Sequence[str]) -> List[str]:
        """What is worth saying about the lines that have just arrived."""
        text = [line for line in lines if line.strip()]
        if not text:
            return []

        if self.uses_model and self.notes < self.MAX_NOTES:
            request = {
                "agent": f"{self.spec.name} ({self.spec.script})",
                "purpose": self.spec.tagline,
                "log_lines": text[-24:],
            }
            answer = self.model.ask(_NARRATION_SYSTEM,
                                    json.dumps(request, ensure_ascii=False), cache=False)
            if answer and str(answer.get("note", "")).strip():
                self.notes += 1
                return [str(answer["note"]).strip()]

        return self._from_signals(text)

    def _from_signals(self, lines: Sequence[str]) -> List[str]:
        """Commentary drawn from the log itself, for when there is no model.

        Anything the agent flagged is passed through, because that is what
        someone watching needs to know. So is any line carrying a figure, in the
        agent's own words: an agent saying it resolved twelve suppliers from
        fourteen spellings is already the clearest possible account of what it
        just did, and paraphrasing it would only lose the number.

        Two kinds of line are left out. Indented ones are sub-detail of the line
        above and repeat once per category; column-aligned ones are a table, and
        read as nonsense outside it. Both belong in the log and nowhere else.
        """
        parsed = [(line, *log_message(line)) for line in lines]
        found: List[str] = []

        for line, level, message in parsed:
            if level in {"ERROR", "CRITICAL"} or "Traceback" in line:
                found.append(shorten_paths(
                    f"The agent reported a problem: {message or line}")[:200])
            elif level == "WARNING" and message and message not in self._seen:
                self._seen.add(message)
                found.append(shorten_paths(message)[:200])
            elif message and not message.startswith(" ") and "   " not in message \
                    and any(character.isdigit() for character in message) \
                    and message not in self._seen:
                self._seen.add(message)
                found.append(shorten_paths(message)[:200])

        if found:
            return found[:self.MAX_PER_BATCH]

        # Nothing quotable in this batch, so name the step instead. The closing
        # manifest names most of the steps the agent went through, which is why
        # the same exclusions apply here: matching against it would announce the
        # start of the run after the run had finished.
        for line, level, message in parsed:
            if not message or message.startswith(" ") or "   " in message:
                continue
            lowered = message.lower()
            for signal, meaning in _LOG_SIGNALS:
                if signal in lowered and meaning not in self._seen:
                    self._seen.add(meaning)
                    return [meaning]
        return []

    def verdict(self, checks: Sequence[CheckResult], dataset: Dataset,
                exit_code: int, seconds: float) -> Dict[str, Any]:
        """A short written summary of how the test went."""
        failed = [check for check in checks if check.status == "fail"]
        warned = [check for check in checks if check.status == "warn"]
        passed = [check for check in checks if check.status == "pass"]

        if self.uses_model:
            request = {
                "agent": self.spec.name,
                "what_it_should_do": self.spec.proves,
                "planted_in_the_data": dataset.planted,
                "exit_code": exit_code,
                "seconds": round(seconds, 1),
                "checks": [check.as_dict() for check in checks],
            }
            answer = self.model.ask(_VERDICT_SYSTEM,
                                    json.dumps(request, ensure_ascii=False), cache=False)
            if answer and str(answer.get("headline", "")).strip():
                return {
                    "headline": str(answer["headline"]).strip(),
                    "points": [str(item).strip() for item in (answer.get("points") or [])
                               if str(item).strip()][:4],
                    "advice": str(answer.get("advice", "")).strip(),
                    "written_by": "model",
                }

        if failed:
            headline = (f"{self.spec.name} did not hold up: "
                        f"{len(failed)} of {len(checks)} checks failed.")
        elif warned:
            headline = (f"{self.spec.name} did its job, with {len(warned)} "
                        f"observation{'s' if len(warned) > 1 else ''} worth a look.")
        else:
            headline = f"{self.spec.name} passed every check."

        # Whether a file exists and whether the process exited cleanly are
        # preconditions, not findings. They are always checked and always
        # reported below, but they say nothing about whether the agent is any
        # good, so the summary leads with the checks that do.
        plumbing = {"The agent finished cleanly"}
        substantive = [check for check in passed
                       if check.measured and check.name not in plumbing
                       and not check.name.endswith("was written")]
        points = [f"{check.name}: {check.measured}."
                  for check in (failed + warned + substantive)[:4] if check.measured]
        advice = ""
        if failed:
            advice = f"Start with '{failed[0].name}': {failed[0].detail}"
        elif warned:
            advice = f"Worth checking '{warned[0].name}': {warned[0].detail}"
        return {"headline": headline, "points": points, "advice": advice,
                "written_by": "rules"}


# ===========================================================================
# Running a test
# ===========================================================================

@dataclass
class TestOutcome:
    """Everything a finished test has to say for itself."""

    agent: str
    exit_code: int
    seconds: float
    checks: List[CheckResult]
    verdict: Dict[str, Any]
    outputs: List[Dict[str, Any]]
    log: List[str]
    usage: Dict[str, Any]

    @property
    def status(self) -> str:
        if self.exit_code != 0 or any(check.status == "fail" for check in self.checks):
            return "fail"
        if any(check.status == "warn" for check in self.checks):
            return "warn"
        return "pass"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent,
            "status": self.status,
            "exit_code": self.exit_code,
            "seconds": round(self.seconds, 2),
            "checks": [check.as_dict() for check in self.checks],
            "verdict": self.verdict,
            "outputs": self.outputs,
            "usage": self.usage,
            "log_lines": len(self.log),
        }


class Harness:
    """Builds datasets and runs agents against them."""

    def __init__(self, use_model: bool = False, workspace: Path = WORKSPACE) -> None:
        env = {**load_dotenv(HERE / ".env"), **os.environ}
        self.config = resolve_model_config(env, use_model)
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.model = LanguageModel(self.config, self.workspace / "model_cache.json")

    # -- data ---------------------------------------------------------------

    def synthesise(self, agent: str, seed: Optional[int] = None,
                   enrich: bool = True) -> Dataset:
        """Build the input for one agent, in its own folder."""
        spec = AGENT_BY_KEY.get(agent)
        if spec is None:
            raise ValueError(f"There is no agent called {agent!r}.")

        seed = seed if seed is not None else random.randrange(1 << 30)
        rng = random.Random(seed)

        root = self.workspace / agent
        _clear(root)
        root.mkdir(parents=True, exist_ok=True)

        phrases = PhraseSource(self.model if enrich else None, rng)
        if enrich and self.model.available:
            # Only the concepts this test actually uses, to keep the request
            # count proportionate to the run.
            phrases.enrich(CONCEPTS[:10], ("fi", "sv", "pl"), per_concept=2)
            self.model.save()

        dataset = spec.build(root, rng, phrases)
        dataset.seed = seed
        dataset.model_phrasings = phrases.generated
        self._tell_file_stories(dataset, spec)
        self.model.save()
        (root / "dataset.json").write_text(
            json.dumps(dataset.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return dataset

    def _tell_file_stories(self, dataset: Dataset, spec: AgentSpec) -> None:
        """Two business sentences for each generated file.

        The language model is asked first, so the wording is not limited to
        what is written here. When it is off, or when a request fails, the
        fallback below is used instead — still two sentences a buyer can read.
        """
        for generated in dataset.files:
            generated.story = self._file_story(generated, spec) or _FILE_STORY_FALLBACK.get(
                generated.path.name, generated.label)

    def _file_story(self, generated: GeneratedFile, spec: AgentSpec) -> str:
        if not self.model.available:
            return ""
        request = {
            "agent": spec.name,
            "file": generated.path.name,
            "what_the_file_is": generated.label,
            "columns": generated.columns[:12],
            "rows": generated.rows,
        }
        answer = self.model.ask(_FILE_STORY_SYSTEM,
                                json.dumps(request, ensure_ascii=False))
        if not answer:
            return ""
        lines = [str(item).strip() for item in (answer.get("lines") or [])
                 if str(item).strip()]
        if len(lines) >= 2:
            return " ".join(lines[:2])
        single = str(answer.get("description") or "").strip()
        return single

    # -- execution ----------------------------------------------------------

    def run(self, dataset: Dataset,
            emit: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> TestOutcome:
        """Run the agent and judge the result.

        ``emit`` receives events as they happen so an interface can show the run
        in progress: ``log`` for a line the agent wrote, ``note`` for the
        harness's reading of the last few lines, ``phase`` for a change of step.
        """
        spec = AGENT_BY_KEY[dataset.agent]
        emit = emit or (lambda kind, payload: None)

        results = dataset.root / "results"
        cache = self.workspace / "agent_cache"
        _clear(results)
        results.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)

        command = [sys.executable, "-u", str(HERE / spec.script)] + spec.command(
            dataset, results, cache)
        emit("phase", {"phase": "running", "label": f"Running {spec.script}"})
        emit("log", {"line": f"$ python {spec.script} "
                             f"{' '.join(spec.command(dataset, results, cache))}"})

        narrator = Narrator(self.model, spec)
        log: List[str] = []
        pending: List[str] = []
        last_note = time.time()
        started = time.time()

        process = subprocess.Popen(
            command, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env={**os.environ, "PYTHONIOENCODING": "utf-8"})

        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.rstrip("\n")
            log.append(line)
            pending.append(line)
            emit("log", {"line": line})

            # A note once enough has happened to be worth remarking on, and not
            # more often than every few seconds, so the commentary reads as
            # commentary rather than as a second copy of the log.
            if len(pending) >= 8 or (pending and time.time() - last_note > 3.0):
                for note in narrator.note(pending):
                    emit("note", {"note": note})
                pending = []
                last_note = time.time()

        exit_code = process.wait()
        for note in narrator.note(pending):
            emit("note", {"note": note})
        seconds = time.time() - started

        emit("phase", {"phase": "checking", "label": "Checking what came back"})
        try:
            checks = spec.check(dataset, results)
        except Exception as error:                      # a broken check is not a broken agent
            checks = [CheckResult("The harness could not read the output", "fail",
                                  "This is a fault in the test, not necessarily in the agent.",
                                  str(error))]
        if exit_code != 0:
            checks.insert(0, CheckResult(
                "The agent finished cleanly", "fail",
                "A non-zero exit means the agent stopped rather than completed.",
                f"exit code {exit_code}"))
        else:
            checks.insert(0, CheckResult(
                "The agent finished cleanly", "pass",
                "The process completed and returned success.",
                f"in {seconds:.1f} seconds"))

        for check in checks:
            emit("check", check.as_dict())

        verdict = narrator.verdict(checks, dataset, exit_code, seconds)
        emit("phase", {"phase": "done", "label": "Finished"})
        self.model.save()

        return TestOutcome(
            agent=dataset.agent, exit_code=exit_code, seconds=seconds, checks=checks,
            verdict=verdict, outputs=_collect_outputs(results), log=log,
            usage=self.model.usage.as_dict())


def _collect_outputs(results: Path) -> List[Dict[str, Any]]:
    """Describe every file the agent wrote."""
    found: List[Dict[str, Any]] = []
    if not results.is_dir():
        return found
    for path in sorted(results.rglob("*")):
        if not path.is_file():
            continue
        entry: Dict[str, Any] = {
            "name": path.name,
            "relative": str(path.relative_to(results)),
            "bytes": path.stat().st_size,
            "kind": path.suffix.lstrip(".").lower(),
            "rows": 0,
        }
        if path.suffix.lower() == ".csv":
            columns, rows = read_csv(path)
            entry["rows"] = len(rows)
            entry["columns"] = len(columns)
        found.append(entry)
    return found


def _clear(path: Path) -> None:
    """Remove a directory tree, tolerating its absence."""
    import shutil
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


# ===========================================================================
# Command line
# ===========================================================================

def _print_outcome(spec: AgentSpec, dataset: Dataset, outcome: TestOutcome) -> None:
    symbols = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    print()
    print("=" * 79)
    print(f"  {spec.number}  {spec.name}")
    print("=" * 79)
    print(f"  {outcome.verdict['headline']}")
    print()
    for check in outcome.checks:
        print(f"  [{symbols[check.status]}]  {check.name}")
        if check.measured:
            print(f"           {check.measured}")
    if outcome.verdict.get("advice"):
        print()
        print(f"  Next: {outcome.verdict['advice']}")
    print()
    print(f"  Data      : {dataset.root}")
    print(f"  Outputs   : {len(outcome.outputs)} files")
    print(f"  Duration  : {outcome.seconds:.1f}s")
    usage = outcome.usage
    if usage.get("requests") or usage.get("failures"):
        print(f"  Model     : {usage['requests']} requests, "
              f"{usage['total_tokens']:,} tokens, "
              f"${usage['estimated_cost_usd']:.2f} estimated")
        if usage.get("failures"):
            # Silence here would be the worst outcome: the run looks the same
            # whether the model answered or was never reachable, and someone
            # would go on believing they were testing something they were not.
            print(f"              {usage['failures']} request(s) did not come back. "
                  f"Those steps used the local rules instead.")
    print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="TestAgent.py",
        description="Test a procurement agent against data built to make it prove itself.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Examples:\n"
                "  python TestAgent.py --list\n"
                "  python TestAgent.py --agent agent2\n"
                "  python TestAgent.py --all --use-llm\n\n"
                "The interface this was written for is started with: python app.py\n"))
    parser.add_argument("--agent", choices=[spec.key for spec in AGENTS],
                        help="which agent to test")
    parser.add_argument("--all", action="store_true", help="test every agent in turn")
    parser.add_argument("--list", action="store_true", help="list the agents and stop")
    parser.add_argument("--seed", type=int, default=None,
                        help="fix the data seed so a run can be repeated exactly")
    parser.add_argument("--use-llm", action="store_true",
                        help="let the model widen the vocabulary and read the log back")
    parser.add_argument("--version", action="version",
                        version=f"{HARNESS_NAME} {HARNESS_VERSION}")
    args = parser.parse_args(argv)

    if args.list or not (args.agent or args.all):
        print()
        for spec in AGENTS:
            print(f"  {spec.number}  {spec.key:<8} {spec.name}")
            print(f"      {spec.tagline}")
        print()
        print("  python TestAgent.py --agent agent1        test one")
        print("  python TestAgent.py --all                 test every one")
        print("  python app.py                            the interface")
        print()
        return 0

    harness = Harness(use_model=args.use_llm)
    print(f"\n  {HARNESS_NAME} {HARNESS_VERSION}   model: {harness.config.describe()}")

    targets = list(AGENTS) if args.all else [AGENT_BY_KEY[args.agent]]
    worst = "pass"
    for spec in targets:
        print(f"\n  Building data for {spec.name} ...", flush=True)
        dataset = harness.synthesise(spec.key, seed=args.seed)
        print(f"  {sum(f.rows for f in dataset.files):,} rows across "
              f"{len(dataset.files)} file(s). Running ...", flush=True)
        outcome = harness.run(dataset)
        _print_outcome(spec, dataset, outcome)
        if outcome.status == "fail" or (outcome.status == "warn" and worst == "pass"):
            worst = outcome.status

    return 1 if worst == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
