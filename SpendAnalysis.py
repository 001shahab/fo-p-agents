#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Spend Analysis - AI Spend Analytics over the full twelve months.

Answers the questions of the spend analytics deck
(subset3/Spend_Analytics_input_for_agent.pptx) from the finished agent runs.

    What it reads
    -------------
    The merged dataset of every run, all_agents_dataset.csv, by default those of
    new_results_llm (January to April 2026) and may_dec_results (May to December
    2025). The two extracts spell their columns differently ("Spend in EUR" and
    SPEND_IN_EUR, "Invoice date" and INVOICE_DATE); every figure is read from the
    agents' own columns first and from the extract's only where no agent column
    holds it, so both spellings give the same analysis.

    Where a run has been regrouped (updated_agent2_v3_purchase_groups.csv beside
    its agent outputs), those purchase groups replace the ones in the dataset,
    joined on the source row number. --keep-dataset-groups turns that off.

    The figures and the words
    -------------------------
    Every number is computed here, deterministically, from the lines. The model
    does two things only: it judges which buying channel suits each supplier's
    recurring purchases, and it writes the answer to each question from a fact
    sheet of figures already computed. Each answer is checked: a figure the fact
    sheet does not contain sends it back once, and a second failure replaces it
    with a written-out template. With --no-llm both steps use rules and
    templates, and the figures are the same.

    Definitions
    -----------
    Every definition and every proxy is written into the workbook and the report
    beside the answer it produced, with the share of lines it could be measured
    on. Several questions ask for things the extract does not hold (a preferred
    supplier list, discount terms); those are answered from the nearest evidence
    in the data and say so.

    Output
    ------
    Written to spend_analysis_results/:

        spend_analysis.xlsx             every table, definitions and coverage
        spend_analysis.pptx             one slide per question
        spend_analysis_report.md        the answers, as text
        spend_analysis_report.html      the same, for a browser
        spend_analysis_manifest.json    inputs, settings, figures and model usage

Usage:
    python SpendAnalysis.py

Author:
    Prof. Shahab Anbarjafari
"""

from __future__ import annotations

import argparse
import csv
import datetime
import html
import json
import logging
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from runtime import SpendLimitReached, configure_process_logging, model_environment

LOGGER = logging.getLogger("spend_analysis")

AGENT_NAME = "Spend Analysis - AI Spend Analytics"
AGENT_VERSION = "1.0.0"

DEFAULT_RUN_FOLDERS = ("new_results_llm", "may_dec_results")
DATASET_NAME = "all_agents_dataset.csv"
REGROUPED_NAME = "updated_agent2_v3_purchase_groups.csv"
COMBINED_MARKER = ".agent4_combined.json"
ORDER_TEXT_MARKER = ".order_text_update.json"
AGENT4_FIXED_VERSION = (1, 2, 1)
DEFAULT_OUTPUT_DIR = "spend_analysis_results"
UNCLASSIFIED = "Unclassified"

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# Each figure, and the columns that may hold it, best first. The agents' own
# columns lead; the extract's spellings follow for runs that lack them.
FIELDS: Dict[str, Tuple[str, ...]] = {
    "spend": ("Spend_EUR", "SPEND_IN_EUR", "Spend in EUR"),
    "spend_pc": ("SPEND_IN_PURCHASE_CURRENCY", "Spend in purchase currency"),
    "currency": ("PURCHASE_CURRENCY", "Purchase currency", "Currency"),
    "supplier_key": ("Agent4_Supplier_Key",),
    "supplier_id": ("Supplier_Id", "SUPPLIER_ID", "Supplier ID"),
    "supplier_name": ("Agent4_Supplier_Name", "Supplier_Name", "SUPPLIER", "Supplier"),
    "l1": ("Category_L1", "CATEGORY_L1", "Category L1"),
    "l2": ("Category_L2", "CATEGORY_L2", "Category L2"),
    "business_area": ("Business_Area", "BUSINESS_AREA", "Business area"),
    "division": ("Agent1_Division", "Division", "DIVISION"),
    "company": ("Company_Name", "Company_Code"),
    "year": ("YEAR", "Year"),
    "month": ("MONTH", "Month"),
    "posting_date": ("Posting_Date", "POSTING_DATE", "Posting date"),
    "invoice_date": ("INVOICE_DATE", "Invoice date"),
    "due_date": ("DUE_DATE", "Due date"),
    "payment_date": ("PAYMENT_DATE", "Payment date"),
    "po_number": ("Agent1_PO_Number", "PO_NUMBER", "PO number"),
    "invoice": ("Invoice_Number", "INVOICE_NUMBER", "Invoice number",
                "Document_Number", "DOCUMENT_NUMBER", "Document number"),
    "row_type": ("Row_Type",),
    "duplicate": ("Is_Duplicate",),
    "item_or_service": ("Agent1_Item_Or_Service",),
    "description": ("Enriched_Purchase_Description",),
    "short": ("Enriched_Description_Short",),
    "item_type": ("Item_Type",),
    "standard": ("Standard_item",),
    "potential_match": ("Potential_Standard_Match",),
    "matched_item": ("Matched_Item_ID",),
    "group_label": ("AI_Purchase_Group_L5",),
    "group_id": ("AI_Purchase_Group_Id",),
    "source_row": ("Source_Row_Number",),
    "po_match": ("PO_Match_Level",),
    "po_order_date": ("PO_Order_Date", "Basware_PO creation date"),
    "po_unit_cost": ("PO_Unit_Cost", "Maximo_UNITCOST"),
    "po_currency": ("PO_Currency", "Maximo_CURRENCYCODE"),
    "invoice_unit_price": ("Invoice_Unit_Price_Excl_Vat",),
    "item_code": ("Maximo_ITEMNUM", "PO_Item_Code"),
    "frame_contract": ("PO_Frame_Contract", "Maximo_XFRAMECONTRACT"),
    "contract_ref": ("PO_Contract_Reference", "Basware_Contract number",
                     "Maximo_CONTRACTREFNUM"),
    "a4_potential": ("Agent4_Consolidation_Potential",),
    "a4_band": ("Agent4_Similarity_Band",),
    "a4_similar": ("Agent4_Most_Similar_Supplier",),
    "a4_similar_key": ("Agent4_Most_Similar_Supplier_Key",),
    "a4_similarity": ("Agent4_Similarity_Percent",),
    "a4_supplier_spend": ("Agent4_Supplier_Spend_EUR",),
    "a4_addressable": ("Agent4_Addressable_Spend_EUR",),
}

CATCH_ALL_GROUPS = {"", "other", "others", "unclassified", "unknown", "miscellaneous",
                    "misc", "not classified", "(no group)"}
NO_PO_VALUES = {"", "-", "0", "#", "n/a", "na", "none", "null", "no po", "no po number",
                "not found", "missing", "(blank)", "blank"}

CHANNELS = (
    "Supplier catalogue / punch-out",
    "Internal catalogue item",
    "Contract / predefined request",
    "Service catalogue / predefined request",
    "Vendor-managed inventory",
)
CHANNEL_INSTRUMENT = {
    "Supplier catalogue / punch-out": "Catalogue",
    "Internal catalogue item": "Standard item",
    "Contract / predefined request": "Framework agreement",
    "Service catalogue / predefined request": "Service catalogue / framework",
    "Vendor-managed inventory": "Framework agreement with VMI",
}
SUITABILITY = ("High", "Medium", "Low")
CONTRACT_WORDS = re.compile(
    r"\b(leas\w*|rent\w*|lease|subscription\w*|licen[cs]\w*|fee|fees|insurance|"
    r"utilit\w*|electricity|telecom\w*|mobile|broadband|maintenance agreement|"
    r"service agreement|hire)\b", re.I)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_amount(value: Any) -> Optional[float]:
    """A number from a cell of either extract.

    The extracts write a decimal comma and no thousands separator, so a single
    comma is the decimal point even with three digits after it.
    """
    text = str(value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^\d,.\-]", "", text.strip("()"))
    if not text or text in {"-", ".", ","}:
        return None
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in text:
        text = text.replace(",", "") if text.count(",") > 1 else text.replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        amount = float(text)
    except ValueError:
        return None
    if math.isnan(amount) or math.isinf(amount):
        return None
    return -amount if negative else amount


_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y")


def parse_date(value: Any) -> Optional[datetime.date]:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.split(" ")[0].split("T")[0]
    for pattern in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def norm_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return text.strip(" .;:,-")


def has_po(value: str) -> bool:
    text = norm_text(value)
    return bool(text) and text not in NO_PO_VALUES and not text.startswith("no po")


def version_tuple(text: Any) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", str(text or ""))[:3])


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def eur(value: float) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1e9:
        return f"{sign}€{value / 1e9:.2f}B"
    if value >= 1e6:
        return f"{sign}€{value / 1e6:.1f}M"
    if value >= 1e3:
        return f"{sign}€{value / 1e3:.1f}k"
    return f"{sign}€{value:,.0f}"


def pct(part: float, whole: float) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "n/a"


def count(value: float) -> str:
    return f"{int(round(value)):,}"


def share(part: float, whole: float) -> float:
    return part / whole if whole else 0.0


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class RunInput:
    """One run's merged dataset and what is known about how it was made."""

    path: Path
    name: str
    regrouped: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    regroup_file: Optional[Path] = None
    notes: List[str] = field(default_factory=list)
    lines: int = 0
    regroup_hits: int = 0
    excluded_subtotal: int = 0
    excluded_duplicate: int = 0
    unparsed_spend: int = 0
    periods: Counter = field(default_factory=Counter)
    missing_fields: List[str] = field(default_factory=list)

    @property
    def work(self) -> Path:
        return self.path.parent / "all_agents"

    @property
    def period(self) -> str:
        known = sorted(p for p in self.periods if p)
        return f"{known[0]} to {known[-1]}" if known else "unknown"


def default_inputs(here: Path) -> List[Path]:
    found = []
    for name in DEFAULT_RUN_FOLDERS:
        for folder in (here / name, here / "results" / name):
            if (folder / DATASET_NAME).is_file():
                found.append(folder / DATASET_NAME)
                break
        else:
            LOGGER.warning("No %s found for %s (looked in %s and %s).", DATASET_NAME, name,
                           here / name, here / "results" / name)
    return found


def prepare_input(path: Path, keep_groups: bool) -> RunInput:
    run = RunInput(path=path, name=path.parent.name)
    regroup = run.work / REGROUPED_NAME
    if regroup.is_file() and not keep_groups:
        with regroup.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                number = (row.get("Source_Row_Number") or "").strip()
                if number:
                    run.regrouped[number] = ((row.get("AI_Purchase_Group_L5") or "").strip(),
                                             (row.get("AI_Purchase_Group_Id") or "").strip())
        run.regroup_file = regroup
    if (path.parent / COMBINED_MARKER).is_file():
        run.notes.append("Agent 4 was run once over all runs together (--combine-agent4).")
    else:
        run.notes.append("Agent 4 was run on this run alone; its most similar suppliers "
                         "are not compared across runs. Run --combine-agent4 first for that.")
    manifest = run.work / "agent4_run_manifest.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, json.JSONDecodeError):
        version = None
    if version and version_tuple(version) < AGENT4_FIXED_VERSION:
        run.notes.append(
            f"Agent 4 {version} read amounts such as 108705,024 as 108 million. Its "
            f"similarity is weighted by spend, so rerun --combine-agent4 for corrected "
            f"supplier findings. Spend figures here are read from the lines and are not "
            f"affected.")
    if (path.parent / ORDER_TEXT_MARKER).is_file():
        run.notes.append("Blank descriptions were filled from order and invoice text.")
    return run


class Columns:
    """Where each figure sits in one dataset's header."""

    def __init__(self, header: Sequence[str]) -> None:
        position = {name: at for at, name in enumerate(header)}
        self.at: Dict[str, Tuple[int, ...]] = {
            key: tuple(position[n] for n in names if n in position)
            for key, names in FIELDS.items()}

    def missing(self) -> List[str]:
        return [key for key, at in self.at.items() if not at]

    def getter(self) -> Callable[[Sequence[str], str], str]:
        at = self.at

        def get(row: Sequence[str], key: str) -> str:
            for index in at[key]:
                if index < len(row):
                    value = row[index].strip()
                    if value:
                        return value
            return ""
        return get


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

@dataclass
class Supplier:
    name: str = ""
    spend: float = 0.0
    lines: int = 0
    invoices: Set[str] = field(default_factory=set)
    by_l2: Counter = field(default_factory=Counter)
    contract_lines: int = 0
    pay_spend: float = 0.0
    pay_term: float = 0.0
    pay_actual: float = 0.0
    early_spend: float = 0.0
    early_days: float = 0.0

    @property
    def main_l2(self) -> str:
        return self.by_l2.most_common(1)[0][0] if self.by_l2 else UNCLASSIFIED


@dataclass
class Combo:
    """One supplier and one recurring purchase description."""

    supplier: str
    text: str
    lines: int = 0
    spend: float = 0.0
    freetext: int = 0
    kinds: Counter = field(default_factory=Counter)
    l2: Counter = field(default_factory=Counter)
    group: Counter = field(default_factory=Counter)


@dataclass
class Group:
    label: str = ""
    spend: float = 0.0
    lines: int = 0
    suppliers: Counter = field(default_factory=Counter)
    areas: Set[str] = field(default_factory=set)
    l2: Counter = field(default_factory=Counter)


class Aggregates:
    """Everything the questions need, gathered in one pass over the lines."""

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self.spend = 0.0
        self.lines = 0
        self.negative = 0.0
        self.by_l2: Counter = Counter()
        self.lines_by_l2: Counter = Counter()
        self.by_area: Counter = Counter()
        self.by_period: Counter = Counter()
        self.suppliers: Dict[str, Supplier] = {}
        self.supplier_l2: Counter = Counter()

        self.unclear_spend = 0.0
        self.unclear_lines = 0
        self.unclear_by_l2: Counter = Counter()
        self.unclear_lines_by_l2: Counter = Counter()
        self.unclear_by_area: Counter = Counter()
        self.pos: Dict[str, List[float]] = {}

        self.typed_spend: Counter = Counter()
        self.typed_lines = 0
        self.freetext_by_l2: Counter = Counter()

        self.desc_lines: Counter = Counter()
        self.combos: Dict[Tuple[str, str], Combo] = {}
        self.standard_lines = 0
        self.standard_spend = 0.0

        self.match_lines = 0
        self.match_spend = 0.0
        self.match_items: Set[str] = set()
        self.match_by_l2: Counter = Counter()

        self.a4: Dict[Tuple[str, str], Dict[str, str]] = {}

        self.groups: Dict[str, Group] = {}

        self.prices: Dict[str, List[Tuple[float, float, str, str, str]]] = defaultdict(list)
        self.price_label: Dict[str, Tuple[str, str]] = {}

        self.no_po_spend = 0.0
        self.no_po_lines = 0
        self.no_po_by_l2: Counter = Counter()
        self.no_po_documents: Set[str] = set()
        self.dated_po_spend = 0.0
        self.retro_spend = 0.0
        self.retro_lines = 0
        self.retro_by_l2: Counter = Counter()
        self.retro_documents: Set[str] = set()
        self.po_matched_spend = 0.0
        self.po_matched_lines = 0
        self.contract_spend = 0.0
        self.contract_lines = 0
        self.preferred_spend = 0.0

        self.pay_lines = 0
        self.pay_excluded = 0
        self.term_spend: Counter = Counter()
        self.pay_by_l2: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])

    # -- one line -------------------------------------------------------------

    def add(self, run: RunInput, row: Sequence[str],
            get: Callable[[Sequence[str], str], str],
            preferred: Optional[Set[str]]) -> None:
        settings = self.settings
        if get(row, "row_type") and get(row, "row_type").upper() != "LINE":
            run.excluded_subtotal += 1
            return
        if get(row, "duplicate").casefold() == "yes":
            run.excluded_duplicate += 1
            return
        amount = parse_amount(get(row, "spend"))
        if amount is None:
            run.unparsed_spend += 1
            amount = 0.0
        run.lines += 1

        year, month = get(row, "year"), get(row, "month")
        posting = parse_date(get(row, "posting_date"))
        if year.isdigit() and month.isdigit():
            period = f"{int(year):04d}-{int(month):02d}"
        else:
            period = posting.strftime("%Y-%m") if posting else ""
        run.periods[period] += 1

        name = get(row, "supplier_name") or "(no supplier)"
        key = (get(row, "supplier_key")
               or (f"id:{get(row, 'supplier_id')}" if get(row, "supplier_id") else "")
               or f"name:{norm_text(name)}")
        l2 = get(row, "l2") or UNCLASSIFIED
        area = get(row, "business_area") or get(row, "division") or "(no business area)"
        company = get(row, "company") or area
        kind = get(row, "item_or_service") or "Unclear"
        description = get(row, "description")
        unclear = kind.casefold() == "unclear" or not description
        document = f"{run.name}|{key}|{get(row, 'invoice') or get(row, 'source_row')}"

        self.lines += 1
        self.spend += amount
        if amount < 0:
            self.negative += amount
        self.by_l2[l2] += amount
        self.lines_by_l2[l2] += 1
        self.by_area[area] += amount
        self.by_period[period] += amount

        supplier = self.suppliers.get(key)
        if supplier is None:
            supplier = self.suppliers[key] = Supplier(name=name)
        supplier.spend += amount
        supplier.lines += 1
        supplier.invoices.add(document)
        supplier.by_l2[l2] += amount
        self.supplier_l2[(key, l2)] += amount

        # Transparency
        po = get(row, "po_number")
        with_po = has_po(po)
        if unclear:
            self.unclear_spend += amount
            self.unclear_lines += 1
            self.unclear_by_l2[l2] += amount
            self.unclear_lines_by_l2[l2] += 1
            self.unclear_by_area[area] += amount
        if with_po:
            entry = self.pos.setdefault(po, [0.0, 0, 0])
            entry[0] += amount
            entry[1] += 1
            entry[2] += 0 if unclear else 1

        item_type = get(row, "item_type")
        if item_type:
            self.typed_lines += 1
            self.typed_spend[item_type] += amount
            if item_type.casefold() == "freetext":
                self.freetext_by_l2[l2] += amount

        # Standardisation and catalogues
        standard = (get(row, "standard").upper() == "Y"
                    or get(row, "potential_match").casefold().startswith("already"))
        if standard:
            self.standard_lines += 1
            self.standard_spend += amount
        if get(row, "potential_match").casefold() == "yes":
            self.match_lines += 1
            self.match_spend += amount
            self.match_by_l2[l2] += amount
            if get(row, "matched_item"):
                self.match_items.add(get(row, "matched_item"))

        text = get(row, "short") or description
        text_key = norm_text(text)
        group_label, group_id = get(row, "group_label"), get(row, "group_id")
        if run.regrouped:
            regrouped = run.regrouped.get(get(row, "source_row"))
            if regrouped:
                group_label, group_id = regrouped
                run.regroup_hits += 1
        if not unclear and not standard and text_key:
            self.desc_lines[text_key] += 1
            combo = self.combos.get((key, text_key))
            if combo is None:
                combo = self.combos[(key, text_key)] = Combo(supplier=key, text=text)
            combo.lines += 1
            combo.spend += amount
            combo.kinds[kind] += 1
            combo.l2[l2] += 1
            if group_label:
                combo.group[group_label] += 1
            if item_type.casefold() == "freetext":
                combo.freetext += 1

        # Agent 4, one finding per supplier and category
        if get(row, "a4_band") or get(row, "a4_potential"):
            if (key, l2) not in self.a4:
                self.a4[(key, l2)] = {
                    "potential": get(row, "a4_potential"),
                    "band": get(row, "a4_band"),
                    "similar": get(row, "a4_similar"),
                    "similar_key": get(row, "a4_similar_key"),
                    "similarity": get(row, "a4_similarity"),
                    "supplier_spend": get(row, "a4_supplier_spend"),
                    "addressable": get(row, "a4_addressable"),
                }

        # Fragmentation and bundling
        if group_id or group_label:
            group = self.groups.get(group_id or group_label)
            if group is None:
                group = self.groups[group_id or group_label] = Group(label=group_label)
            group.spend += amount
            group.lines += 1
            group.suppliers[key] += amount
            group.areas.add(area)
            group.l2[l2] += amount

        # Prices
        self._add_price(row, get, amount, area, company, key, kind, text, text_key,
                        group_id or group_label, standard)

        # Channels and compliance
        if not with_po:
            self.no_po_spend += amount
            self.no_po_lines += 1
            self.no_po_by_l2[l2] += amount
            self.no_po_documents.add(document)
        invoice_date = parse_date(get(row, "invoice_date"))
        order_date = parse_date(get(row, "po_order_date"))
        if with_po and order_date and invoice_date:
            self.dated_po_spend += amount
            if order_date > invoice_date:
                self.retro_spend += amount
                self.retro_lines += 1
                self.retro_by_l2[l2] += amount
                self.retro_documents.add(document)
        if get(row, "po_match").casefold() in {"line", "header"}:
            self.po_matched_spend += amount
            self.po_matched_lines += 1
            if get(row, "frame_contract") == "1" or get(row, "contract_ref"):
                self.contract_spend += amount
                self.contract_lines += 1
                supplier.contract_lines += 1
        if preferred is not None and (key in preferred or norm_text(name) in preferred
                                      or norm_text(get(row, "supplier_id")) in preferred):
            self.preferred_spend += amount

        # Payment terms
        due = parse_date(get(row, "due_date"))
        paid = parse_date(get(row, "payment_date"))
        if invoice_date and due and paid and amount > 0:
            term = (due - invoice_date).days
            actual = (paid - invoice_date).days
            stale = posting is not None and abs((posting - invoice_date).days) > 366
            if stale or not 0 <= term <= 365 or not -30 <= actual <= 730:
                self.pay_excluded += 1
            else:
                self.pay_lines += 1
                supplier.pay_spend += amount
                supplier.pay_term += amount * term
                supplier.pay_actual += amount * actual
                early = (due - paid).days
                if early > settings.early_grace_days:
                    supplier.early_spend += amount
                    supplier.early_days += amount * early
                self.term_spend[term_bucket(term)] += amount
                totals = self.pay_by_l2[l2]
                totals[0] += amount
                totals[1] += amount * term
                totals[2] += amount * actual

    def _add_price(self, row, get, amount, area, company, supplier, kind, text, text_key,
                   group, standard) -> None:
        if amount <= 0:
            return
        spend_pc = parse_amount(get(row, "spend_pc"))
        currency = get(row, "currency").upper()
        to_eur = amount / spend_pc if spend_pc else None
        price = None
        unit = parse_amount(get(row, "invoice_unit_price"))
        if unit and unit > 0 and to_eur:
            price = unit * to_eur
        else:
            unit = parse_amount(get(row, "po_unit_cost"))
            po_currency = get(row, "po_currency").upper()
            if unit and unit > 0:
                if po_currency == "EUR":
                    price = unit
                elif to_eur and po_currency in {"", currency}:
                    price = unit * to_eur
        if not price or price <= 0:
            return
        code = get(row, "item_code")
        if code and standard:
            key = f"item:{code}"
            label = (text or code, "Material")
        elif text_key and kind.casefold() in {"material", "service"}:
            key = f"text:{group}|{text_key}"
            label = (text, kind.title())
        else:
            return
        self.prices[key].append((price, amount, area, company, supplier))
        self.price_label.setdefault(key, label)


def term_bucket(days: int) -> str:
    for limit in (14, 21, 30, 45, 60, 90):
        if days <= limit:
            return f"<= {limit} days"
    return "> 90 days"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Table:
    name: str
    columns: List[str]
    rows: List[List[Any]]
    chart: Optional[Tuple[str, str]] = None
    note: str = ""


@dataclass
class Section:
    key: str
    title: str
    question: str
    kpis: List[Tuple[str, str]] = field(default_factory=list)
    facts: Dict[str, str] = field(default_factory=dict)
    tables: List[Table] = field(default_factory=list)
    definitions: List[str] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)
    headline: str = ""
    answer: List[str] = field(default_factory=list)
    indicates: List[str] = field(default_factory=list)
    written_by: str = "template"

    def fact(self, label: str, value: str, kpi: bool = False) -> str:
        self.facts[label] = value
        if kpi:
            self.kpis.append((label, value))
        return value


@dataclass
class Settings:
    here: Path
    inputs: List[Path]
    results_dir: Path
    cache_dir: Path
    use_llm: bool = True
    spend_limit: Optional[float] = None
    interactive: bool = True
    skip_model_check: bool = False
    keep_groups: bool = False
    preferred_file: Optional[Path] = None
    cost_of_capital: float = 0.05
    target_days: int = 60
    rework_cost: float = 0.0
    recurrence_min: int = 6
    tail_share: float = 0.80
    channel_suppliers: int = 400
    early_grace_days: int = 3


def top_rows(counter: Counter, total: float, limit: int, lines: Optional[Counter] = None
             ) -> List[List[Any]]:
    rows = []
    for name, value in counter.most_common(limit):
        row = [name, round(value, 2), round(100 * share(value, total), 1)]
        if lines is not None:
            row.insert(2, lines.get(name, 0))
        rows.append(row)
    return rows


class Analysis:
    """The questions of the deck, answered from the aggregates."""

    def __init__(self, data: Aggregates, settings: Settings, runs: List[RunInput]) -> None:
        self.data = data
        self.settings = settings
        self.runs = runs
        self.sections: List[Section] = []
        self.channels: Dict[str, Dict[str, str]] = {}
        self.levers: List[List[Any]] = []
        self.figures: Dict[str, float] = {}
        self.preferred_given = settings.preferred_file is not None
        self._tail: Optional[Set[str]] = None
        self._recurring: Optional[List[Combo]] = None

    # -- shared --------------------------------------------------------------

    def tail(self) -> Set[str]:
        if self._tail is None:
            ranked = sorted(self.data.suppliers.items(), key=lambda kv: -kv[1].spend)
            positive = sum(max(s.spend, 0.0) for _, s in ranked)
            head: Set[str] = set()
            running = 0.0
            for key, supplier in ranked:
                if running >= self.settings.tail_share * positive:
                    break
                head.add(key)
                running += max(supplier.spend, 0.0)
            self._tail = {k for k in self.data.suppliers if k not in head}
        return self._tail

    def recurring(self) -> List[Combo]:
        if self._recurring is None:
            self._recurring = sorted(
                (c for c in self.data.combos.values()
                 if c.lines >= self.settings.recurrence_min),
                key=lambda c: -c.spend)
        return self._recurring

    def name_of(self, key: str) -> str:
        supplier = self.data.suppliers.get(key)
        return supplier.name if supplier else key

    # -- sections ------------------------------------------------------------

    def build(self, classify: Callable[[List[Dict[str, Any]]], Dict[str, Dict[str, str]]]
              ) -> None:
        self.channels = classify(self.channel_payloads())
        for build in (self.transparency, self.tail_spend, self.standardisation,
                      self.catalogue, self.q1_preferred, self.q2_price, self.q3_fragmentation,
                      self.q4_recurring_freetext, self.q5_channels, self.q6_payment,
                      self.value_levers):
            self.sections.append(build())

    def transparency(self) -> Section:
        d = self.data
        s = Section("transparency", "Spend transparency and PO quality",
                    "How much spend is free text or has no clear description, and how many "
                    "purchase orders carry no clear description?")
        freetext = d.typed_spend.get("Freetext", 0.0)
        typed_total = sum(d.typed_spend.values())
        unclear_pos = [v for v in d.pos.values() if v[2] == 0]
        top5 = sum(v for _, v in d.unclear_by_l2.most_common(5))
        s.fact("Total spend", eur(d.spend), True)
        s.fact("Purchase lines", count(d.lines))
        s.fact("Free-text spend", eur(freetext), True)
        s.fact("Free-text share of spend with an item type", pct(freetext, typed_total))
        s.fact("Lines with an item type", pct(d.typed_lines, d.lines))
        s.fact("Spend without a clear description", eur(d.unclear_spend), True)
        s.fact("Share of spend without a clear description", pct(d.unclear_spend, d.spend))
        s.fact("Lines without a clear description", count(d.unclear_lines))
        s.fact("POs without a clear description", count(len(unclear_pos)), True)
        s.fact("Spend on POs without a clear description", eur(sum(v[0] for v in unclear_pos)))
        s.fact("POs in the data", count(len(d.pos)))
        s.fact("Unclear spend in the top 5 categories (L2)", eur(top5))
        s.fact("Top 5 categories share of unclear spend", pct(top5, d.unclear_spend))
        for rank, (name, value) in enumerate(d.unclear_by_l2.most_common(5), 1):
            s.fact(f"Unclear spend category #{rank}", f"{name}: {eur(value)}")
        self.figures["unclear_spend"] = d.unclear_spend
        s.tables.append(Table(
            "Unclear spend by L2", ["Category L2", "Unclear spend EUR", "Unclear lines",
                                    "Share of unclear spend %"],
            top_rows(d.unclear_by_l2, d.unclear_spend, 1000, d.unclear_lines_by_l2),
            chart=("Category L2", "Unclear spend EUR")))
        s.tables.append(Table(
            "Unclear spend by area", ["Business area", "Unclear spend EUR",
                                      "Share of unclear spend %"],
            top_rows(d.unclear_by_area, d.unclear_spend, 1000)))
        s.tables.append(Table(
            "Free-text spend by L2", ["Category L2", "Free-text spend EUR", "Share %"],
            top_rows(d.freetext_by_l2, freetext, 1000)))
        s.definitions += [
            "Free text: lines whose Item_Type is Freetext. Item_Type is filled on "
            f"{pct(d.typed_lines, d.lines)} of lines, so this is a floor.",
            "No clear description: Agent 1 could not tell what was bought "
            "(Agent1_Item_Or_Service = Unclear) or wrote no description.",
            "PO without a clear description: a purchase order none of whose invoice "
            "lines has a clear description.",
        ]
        return s

    def tail_spend(self) -> Section:
        d = self.data
        s = Section("tail", "Tail spend",
                    "How large is the supplier tail, and how much of it is one-time "
                    "suppliers?")
        tail = self.tail()
        suppliers = [d.suppliers[k] for k in tail]
        tail_spend = sum(x.spend for x in suppliers)
        tail_lines = sum(x.lines for x in suppliers)
        one_time = [x for x in suppliers if len(x.invoices) == 1]
        s.fact("Tail definition", f"suppliers outside the top "
                                  f"{self.settings.tail_share:.0%} of spend")
        s.fact("Tail spend", eur(tail_spend), True)
        s.fact("Tail share of spend", pct(tail_spend, d.spend))
        s.fact("Tail suppliers", count(len(tail)), True)
        s.fact("All suppliers", count(len(d.suppliers)))
        s.fact("Tail suppliers share of suppliers", pct(len(tail), len(d.suppliers)), True)
        s.fact("One-time tail suppliers", count(len(one_time)), True)
        s.fact("One-time tail spend", eur(sum(x.spend for x in one_time)))
        s.fact("Tail transactions (lines)", count(tail_lines))
        s.fact("Average tail transaction", eur(share(tail_spend, tail_lines)))
        self.figures["tail_spend"] = tail_spend
        by_l2: Dict[str, List[float]] = defaultdict(lambda: [0, 0, 0.0, 0])
        for x in suppliers:
            entry = by_l2[x.main_l2]
            entry[0] += 1
            entry[1] += 1 if len(x.invoices) == 1 else 0
            entry[2] += x.spend
            entry[3] += x.lines
        rows = sorted(([l2, int(v[0]), int(v[1]), round(v[2], 2), int(v[3])]
                       for l2, v in by_l2.items()), key=lambda r: -r[3])
        for rank, row in enumerate(rows[:3], 1):
            s.fact(f"Tail category #{rank} by spend", f"{row[0]}: {eur(row[3])}, "
                                                      f"{count(row[1])} suppliers")
        for rank, row in enumerate(sorted(rows, key=lambda r: -r[2])[:3], 1):
            s.fact(f"One-time tail suppliers, category #{rank}",
                   f"{row[0]}: {count(row[2])}")
        s.tables.append(Table(
            "Tail by L2", ["Category L2", "Tail suppliers", "One-time tail suppliers",
                           "Tail spend EUR", "Tail lines"], rows,
            chart=("Category L2", "Tail spend EUR")))
        s.tables.append(Table(
            "Tail suppliers", ["Supplier", "Main category L2", "Spend EUR", "Lines",
                               "Invoices", "One-time"],
            [[x.name, x.main_l2, round(x.spend, 2), x.lines, len(x.invoices),
              "Yes" if len(x.invoices) == 1 else "No"]
             for x in sorted(suppliers, key=lambda x: -x.spend)]))
        s.definitions += [
            f"Tail: suppliers ranked by twelve-month spend that fall outside the top "
            f"{self.settings.tail_share:.0%} of it. A supplier is one Agent 4 supplier key, "
            f"which folds spellings of the same vendor together.",
            "One-time supplier: one invoice in twelve months.",
            "A supplier's category is the L2 with most of its spend.",
        ]
        return s

    def standardisation(self) -> Section:
        d = self.data
        s = Section("standardisation", "Standardisation signals",
                    "Which signals point to new catalogue items, existing catalogue matches "
                    "and interchangeable suppliers?")
        candidates: Dict[str, List[Any]] = {}
        for run in self.runs:
            path = run.work / "agent3_catalogue_candidates.csv"
            if not path.is_file():
                continue
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    if (row.get("Catalogue_Candidate") or "").strip().casefold() != "yes":
                        continue
                    text = row.get("Purchase_Description") or ""
                    entry = candidates.setdefault(norm_text(text), [
                        text, row.get("AI_Purchase_Group_L5") or "", row.get("Category") or "",
                        0, 0, set()])
                    entry[3] += int(parse_amount(row.get("Occurrences")) or 0)
                    entry[5].add(run.name)
        high = Counter()
        high_spend = Counter()
        for (key, l2), finding in d.a4.items():
            if finding["band"].casefold() == "high":
                high[l2] += 1
                high_spend[l2] += d.supplier_l2.get((key, l2), 0.0)
        s.fact("New catalogue candidates (Agent 3)", count(len(candidates)), True)
        s.fact("Lines with a potential catalogue match", count(d.match_lines))
        s.fact("Spend with a potential catalogue match", eur(d.match_spend))
        s.fact("Distinct catalogue items matched", count(len(d.match_items)), True)
        s.fact("High-similarity supplier signals (supplier and L2)", count(sum(high.values())),
               True)
        for rank, (l2, n) in enumerate(high.most_common(3), 1):
            s.fact(f"High-similarity category #{rank}", f"{l2}: {count(n)} signals")
        s.fact("Spend already on standard or catalogue items", eur(d.standard_spend))
        s.tables.append(Table(
            "Catalogue candidates", ["Purchase description", "AI purchase group", "Category",
                                     "Occurrences", "Runs"],
            [[v[0], v[1], v[2], v[3], ", ".join(sorted(v[5]))]
             for v in sorted(candidates.values(), key=lambda v: -v[3])]))
        s.tables.append(Table(
            "High similarity by L2", ["Category L2", "High-similarity signals",
                                      "Spend of those suppliers EUR"],
            [[l2, n, round(high_spend[l2], 2)] for l2, n in high.most_common()],
            chart=("Category L2", "High-similarity signals")))
        s.tables.append(Table(
            "Catalogue matches by L2", ["Category L2", "Spend EUR", "Share %"],
            top_rows(d.match_by_l2, d.match_spend, 1000)))
        s.definitions += [
            "New catalogue candidates: Agent 3's Catalogue_Candidate = Yes, counted once per "
            "description across runs.",
            "Potential catalogue match: Agent 3's Potential_Standard_Match = Yes.",
            "High-similarity signal: a supplier and L2 that Agent 4 rates High similarity to "
            "another supplier.",
        ]
        return s

    def catalogue(self) -> Section:
        d = self.data
        n = self.settings.recurrence_min
        s = Section("catalogue", "Catalogue opportunities",
                    "Can catalogue opportunities be identified from the data?")
        repeats = sum(v for v in d.desc_lines.values() if v >= n)
        recurring = self.recurring()
        suppliers = {c.supplier for c in recurring}
        by_channel: Dict[str, List[float]] = defaultdict(lambda: [0, 0, 0, 0.0])
        for c in recurring:
            channel = self.channels.get(c.supplier, {}).get("channel", "Not assessed")
            entry = by_channel[channel]
            entry[1] += 1
            entry[2] += c.lines
            entry[3] += c.spend
        for channel, entry in by_channel.items():
            entry[0] = len({c.supplier for c in recurring
                            if self.channels.get(c.supplier, {}).get("channel",
                                                                     "Not assessed") == channel})
        s.fact("Minimum repeats", str(n))
        s.fact("Lines whose AI description repeats", count(repeats), True)
        s.fact("Recurring supplier-description combinations", count(len(recurring)), True)
        s.fact("Suppliers with recurring combinations", count(len(suppliers)), True)
        s.fact("Spend in recurring combinations", eur(sum(c.spend for c in recurring)), True)
        s.fact("Suppliers assessed for buying channel", count(len(self.channels)))
        for rank, (channel, entry) in enumerate(
                sorted(by_channel.items(), key=lambda kv: -kv[1][3])[:4], 1):
            s.fact(f"Channel #{rank}", f"{channel}: {count(entry[0])} suppliers, "
                                       f"{eur(entry[3])}")
        rows = []
        for key, verdict in sorted(self.channels.items(),
                                   key=lambda kv: -float(kv[1].get("spend", 0))):
            rows.append([self.name_of(key), verdict.get("l2", ""), verdict.get("combos", 0),
                         verdict.get("lines", 0), round(float(verdict.get("spend", 0)), 2),
                         verdict.get("examples", ""), verdict.get("suitability", ""),
                         verdict.get("channel", ""), verdict.get("rationale", ""),
                         verdict.get("source", "")])
        s.tables.append(Table(
            "Supplier buying channels", ["Supplier", "Main category L2", "Recurring combinations",
                                         "Recurring lines", "Recurring spend EUR",
                                         "Recurring purchases", "Catalogue suitability",
                                         "Preferred buying channel", "Rationale",
                                         "Assessed by"], rows))
        s.tables.append(Table(
            "Recurring by channel", ["Preferred buying channel", "Suppliers", "Combinations",
                                     "Lines", "Spend EUR"],
            [[k, int(v[0]), int(v[1]), int(v[2]), round(v[3], 2)]
             for k, v in sorted(by_channel.items(), key=lambda kv: -kv[1][3])],
            chart=("Preferred buying channel", "Spend EUR")))
        s.tables.append(Table(
            "Recurring combinations", ["Supplier", "Purchase description", "Lines", "Spend EUR",
                                       "Free-text lines", "Material or service", "Category L2",
                                       "AI purchase group", "Preferred buying channel"],
            [[self.name_of(c.supplier), c.text, c.lines, round(c.spend, 2), c.freetext,
              c.kinds.most_common(1)[0][0], c.l2.most_common(1)[0][0],
              c.group.most_common(1)[0][0] if c.group else "",
              self.channels.get(c.supplier, {}).get("channel", "")] for c in recurring]))
        s.definitions += [
            f"AI description repeat: a line whose short AI description occurs at least {n} "
            f"times. Lines on standard items and lines without a clear description are left "
            f"out.",
            f"Recurring combination: one supplier and one AI description with at least {n} "
            f"lines.",
            "Recurring leasing, rent, services and subscriptions are steered to contracts or "
            "service catalogues rather than catalogues.",
        ]
        return s

    def q1_preferred(self) -> Section:
        d = self.data
        s = Section("q1", "Spend outside preferred suppliers",
                    "How much spend sits outside preferred suppliers, or active category "
                    "strategies?")
        contracted = {k for k, x in d.suppliers.items() if x.contract_lines}
        contracted_spend = sum(d.suppliers[k].spend for k in contracted)
        outside = d.spend - contracted_spend
        if self.preferred_given:
            outside_list = d.spend - d.preferred_spend
            s.fact("Spend outside the preferred supplier list", eur(outside_list), True)
            s.fact("Share outside the preferred supplier list", pct(outside_list, d.spend))
        s.fact("Spend with suppliers never seen on a contract", eur(outside), True)
        s.fact("Share of spend with suppliers never seen on a contract", pct(outside, d.spend),
               True)
        s.fact("Suppliers with contract evidence", count(len(contracted)))
        s.fact("PO-matched spend", eur(d.po_matched_spend))
        s.fact("PO-matched share of spend", pct(d.po_matched_spend, d.spend))
        s.fact("Contract-covered share of PO-matched spend",
               pct(d.contract_spend, d.po_matched_spend), True)
        self.figures["outside_contract"] = outside
        by_l2 = Counter()
        for (key, l2), value in d.supplier_l2.items():
            if key not in contracted:
                by_l2[l2] += value
        rows = [[l2, round(d.by_l2[l2], 2), round(value, 2),
                 round(100 * share(value, d.by_l2[l2]), 1)]
                for l2, value in by_l2.most_common()]
        for rank, row in enumerate(rows[:3], 1):
            s.fact(f"Category #{rank} by spend without contract evidence",
                   f"{row[0]}: {eur(row[2])} ({row[3]:.1f}% of its spend)")
        s.tables.append(Table(
            "Outside contract by L2", ["Category L2", "Spend EUR",
                                       "Spend with non-contract suppliers EUR",
                                       "Share %"], rows,
            chart=("Category L2", "Spend with non-contract suppliers EUR")))
        s.definitions += [
            "The extract has no preferred supplier list. A supplier counts as preferred "
            "when at least one of its purchase orders refers to a frame contract or a "
            "contract number; spend with every other supplier is reported as outside.",
            f"That evidence exists only on lines matched to a purchase order "
            f"({pct(d.po_matched_lines, d.lines)} of lines), so the figure is an upper bound.",
            "Pass --preferred-suppliers FILE (one supplier name or id per line) to measure "
            "against Fortum's own list.",
        ]
        s.caveats.append("Active category strategies are not recorded in the data.")
        return s

    def q2_price(self) -> Section:
        d = self.data
        s = Section("q2", "Price variance for like-for-like purchases",
                    "Where do we see material price variance for like-for-like items and "
                    "services across sites or BUs?")
        rows = []
        relevant = 0.0
        gap_total = 0.0
        priced = sum(len(v) for v in d.prices.values())
        for key, entries in d.prices.items():
            if len(entries) < 3:
                continue
            sites = {e[2] for e in entries}
            companies = {e[3] for e in entries}
            suppliers = {e[4] for e in entries}
            across_sites = len(sites) > 1 or len(companies) > 1
            if not across_sites and len(suppliers) < 2:
                continue
            prices = sorted(e[0] for e in entries)
            low, mid, high = quantile(prices, 0.25), quantile(prices, 0.5), quantile(prices, 0.75)
            if prices[0] <= 0 or prices[-1] / prices[0] > 20 or mid <= 0:
                continue
            spread = (high - low) / mid
            if spread < 0.10:
                continue
            spend = sum(e[1] for e in entries)
            gap = sum(e[1] * (1 - low / e[0]) for e in entries if e[0] > low)
            relevant += spend
            gap_total += gap
            text, kind = d.price_label[key]
            rows.append([text, kind, "Yes" if across_sites else "No", len(entries),
                         len(sites), len(suppliers),
                         round(prices[0], 2), round(mid, 2), round(prices[-1], 2),
                         round(100 * spread, 1), round(spend, 2), round(gap, 2),
                         ", ".join(sorted(sites)[:6])])
        rows.sort(key=lambda r: -r[11])
        sites_rows = [r for r in rows if r[2] == "Yes"]
        s.fact("Lines with a unit price", count(priced))
        s.fact("Share of lines with a unit price", pct(priced, d.lines))
        s.fact("Like-for-like purchases with material variance", count(len(rows)), True)
        s.fact("Spend on those purchases", eur(relevant), True)
        s.fact("Spend above the lower-quartile price", eur(gap_total), True)
        s.fact("Of which bought across sites or BUs", count(len(sites_rows)), True)
        s.fact("Spend above the lower-quartile price across sites or BUs",
               eur(sum(r[11] for r in sites_rows)))
        for rank, row in enumerate(rows[:3], 1):
            s.fact(f"Largest variance #{rank}", f"{row[0][:80]}: spread {row[9]:.1f}%, "
                                                f"{eur(row[11])} above lower quartile")
        self.figures["price_relevant"] = relevant
        self.figures["price_gap"] = gap_total
        s.tables.append(Table(
            "Price variance", ["Purchase", "Material or service", "Across sites or BUs",
                               "Lines", "Sites", "Suppliers",
                               "Lowest price EUR", "Median price EUR", "Highest price EUR",
                               "Spread %", "Spend EUR", "Spend above lower quartile EUR",
                               "Sites (first six)"], rows,
            chart=("Purchase", "Spend above lower quartile EUR")))
        s.definitions += [
            "Unit price: the invoice's unit price, or failing that the purchase order's, in "
            "euro at the line's own exchange rate.",
            "Like for like: the same standard item number; otherwise the same AI purchase "
            "group and the same AI description.",
            "Material variance: at least three priced lines bought by at least two business "
            "areas or companies, or from at least two suppliers, an interquartile spread of at least 10% of the median, and "
            "no price more than 20 times another (which is a unit-of-measure difference).",
            "Spend above the lower quartile: what the lines would have cost at the "
            "lower-quartile price, the ceiling of price harmonisation.",
        ]
        s.caveats.append(f"Only {pct(priced, d.lines)} of lines carry a unit price.")
        return s

    def q3_fragmentation(self) -> Section:
        d = self.data
        s = Section("q3", "Supplier fragmentation and consolidation",
                    "Where is spend fragmented across too many suppliers, and what share is "
                    "realistically consolidatable?")
        rows = []
        fragmented_spend = 0.0
        for gid, g in d.groups.items():
            if norm_text(g.label) in CATCH_ALL_GROUPS:
                continue
            positive = {k: v for k, v in g.suppliers.items() if v > 0}
            total = sum(positive.values())
            if len(positive) < 5 or total <= 0:
                continue
            top = max(positive.values()) / total
            hhi = sum((v / total) ** 2 for v in positive.values()) * 10000
            if top >= 0.5:
                continue
            fragmented_spend += g.spend
            rows.append([g.label or gid, g.l2.most_common(1)[0][0], len(positive),
                         round(100 * top, 1), round(hhi), len(g.areas), round(g.spend, 2)])
        rows.sort(key=lambda r: -r[6])

        realistic = 0.0
        addressable_total = 0.0
        by_l2: Dict[str, List[float]] = defaultdict(lambda: [0, 0.0, 0.0])
        moves = []
        assessed = 0.0
        for (key, l2), finding in d.a4.items():
            spend = d.supplier_l2.get((key, l2), 0.0)
            if spend <= 0:
                continue
            assessed += spend
            potential = finding["potential"].title()
            if potential not in {"High", "Medium"}:
                continue
            base = parse_amount(finding["supplier_spend"]) or 0.0
            addressable = parse_amount(finding["addressable"]) or 0.0
            movable = spend * min(max(share(addressable, base), 0.0), 1.0)
            partner = d.supplier_l2.get((finding["similar_key"], l2), 0.0)
            entry = by_l2[l2]
            entry[0] += 1
            entry[2] += movable
            addressable_total += movable
            if potential == "High" and partner > spend:
                realistic += movable
                entry[1] += movable
                moves.append([self.name_of(key), l2, round(spend, 2),
                              finding["similar"] or self.name_of(finding["similar_key"]),
                              round(partner, 2), finding["similarity"], round(movable, 2)])
        moves.sort(key=lambda r: -r[6])
        self.figures["consolidatable_high"] = realistic
        self.figures["consolidatable_high_medium"] = addressable_total
        s.fact("Fragmented AI purchase groups", count(len(rows)), True)
        s.fact("Spend in fragmented groups", eur(fragmented_spend), True)
        s.fact("Spend assessed by Agent 4", eur(assessed))
        s.fact("Realistically consolidatable spend", eur(realistic), True)
        s.fact("Realistically consolidatable share of spend", pct(realistic, d.spend), True)
        s.fact("Supplier moves behind it", count(len(moves)))
        s.fact("Upper bound (all High and Medium overlap)", eur(addressable_total))
        s.fact("Upper bound share of spend", pct(addressable_total, d.spend))
        l2_rows = sorted(([l2, int(v[0]), round(v[1], 2), round(v[2], 2)]
                          for l2, v in by_l2.items()), key=lambda r: -r[2])
        for rank, row in enumerate(sorted(l2_rows, key=lambda r: -r[2])[:3], 1):
            s.fact(f"Consolidation category #{rank}", f"{row[0]}: {eur(row[2])} realistic")
        for rank, row in enumerate(moves[:3], 1):
            s.fact(f"Largest move #{rank}", f"{row[0]} to {row[3]} in {row[1]}: {eur(row[6])}")
        l2_rows.sort(key=lambda r: -r[2])
        s.tables.append(Table(
            "Consolidatable by L2", ["Category L2", "Suppliers rated High or Medium",
                                     "Realistic EUR", "Upper bound EUR"], l2_rows,
            chart=("Category L2", "Realistic EUR")))
        s.tables.append(Table(
            "Consolidation moves", ["Supplier", "Category L2", "Supplier spend EUR",
                                    "Move to", "Partner spend EUR", "Similarity %",
                                    "Movable spend EUR"], moves))
        s.tables.append(Table(
            "Fragmented groups", ["AI purchase group", "Category L2", "Suppliers",
                                  "Largest supplier share %", "HHI", "Business areas",
                                  "Spend EUR"], rows))
        s.definitions += [
            "Fragmented: an AI purchase group bought from five or more suppliers where none "
            "has half of the spend. HHI is the Herfindahl index of supplier shares (10,000 "
            "means one supplier).",
            "Movable spend: for each supplier and L2, the share of its spend that Agent 4 "
            "found its most similar supplier already delivering (addressable over supplier "
            "spend), applied to the spend in this data.",
            "Realistic: movable spend of suppliers Agent 4 rates High potential, counted only "
            "when the most similar supplier is the larger of the two in that L2, so each "
            "pair is counted once and in the direction volume would move.",
            "Upper bound: movable spend of every supplier rated High or Medium, in both "
            "directions.",
        ]
        return s

    def q4_recurring_freetext(self) -> Section:
        s = Section("q4", "Recurring free-text purchases to convert",
                    "Which recurring free-text purchases should be converted into standard "
                    "items, catalogs, or framework agreements?")
        recurring = self.recurring()
        by_instrument: Dict[str, List[float]] = defaultdict(lambda: [0, 0, 0.0])
        suppliers_by_instrument: Dict[str, Set[str]] = defaultdict(set)
        for c in recurring:
            channel = self.channels.get(c.supplier, {}).get("channel", "")
            instrument = CHANNEL_INSTRUMENT.get(channel, "Not assessed")
            entry = by_instrument[instrument]
            entry[1] += c.lines
            entry[2] += c.spend
            suppliers_by_instrument[instrument].add(c.supplier)
        for instrument, entry in by_instrument.items():
            entry[0] = len(suppliers_by_instrument[instrument])
        freetext = [c for c in recurring if c.freetext]
        recurring_spend = sum(c.spend for c in recurring)
        suitable = sum(c.spend for c in recurring
                       if self.channels.get(c.supplier, {}).get("suitability") in {"High",
                                                                                   "Medium"})
        self.figures["standardisation_spend"] = suitable
        s.fact("Recurring non-catalogue spend", eur(recurring_spend), True)
        s.fact("Recurring combinations marked free text", count(len(freetext)))
        s.fact("Spend in recurring combinations marked free text",
               eur(sum(c.spend for c in freetext)))
        s.fact("Recurring spend rated High or Medium catalogue suitability", eur(suitable), True)
        for rank, (instrument, entry) in enumerate(
                sorted(by_instrument.items(), key=lambda kv: -kv[1][2])[:4], 1):
            s.fact(f"Convert to #{rank}", f"{instrument}: {eur(entry[2])}, "
                                          f"{count(entry[0])} suppliers", rank <= 2)
        examples = []
        for c in recurring[:5]:
            channel = self.channels.get(c.supplier, {}).get("channel", "")
            examples.append(f"{self.name_of(c.supplier)} - {c.text[:70]}: {eur(c.spend)}, "
                            f"{count(c.lines)} lines, {CHANNEL_INSTRUMENT.get(channel, '')}")
        for rank, example in enumerate(examples, 1):
            s.fact(f"Largest recurring purchase #{rank}", example)
        s.tables.append(Table(
            "Convert by instrument", ["Convert to", "Suppliers", "Lines", "Spend EUR"],
            [[k, int(v[0]), int(v[1]), round(v[2], 2)]
             for k, v in sorted(by_instrument.items(), key=lambda kv: -kv[1][2])],
            chart=("Convert to", "Spend EUR")))
        s.definitions += [
            "Recurring free-text purchase: a recurring combination (see Catalogue "
            "opportunities) on lines that are neither standard items nor catalogue "
            "purchases. Item_Type marks free text explicitly on only part of the lines, so "
            "every non-catalogue recurring purchase is included and the explicitly marked "
            "ones are counted separately.",
            "The instrument follows the preferred buying channel judged for the supplier: "
            "supplier catalogue to catalogue, internal catalogue item to standard item, "
            "contract or predefined request to framework agreement, service catalogue to "
            "service catalogue or framework.",
        ]
        return s

    def q5_channels(self) -> Section:
        d = self.data
        st = self.settings
        s = Section("q5", "Channel and policy leakage",
                    "How much spend bypasses preferred buying channels or policies, and what "
                    "is the cost of leakage and rework?")
        off_channel = d.no_po_spend + d.retro_spend
        documents = len(d.no_po_documents | d.retro_documents)
        low, high = 0.01 * off_channel, 0.03 * off_channel
        rework = documents * st.rework_cost
        self.figures["off_channel"] = off_channel
        s.fact("Spend without a purchase order", eur(d.no_po_spend), True)
        s.fact("Share of spend without a purchase order", pct(d.no_po_spend, d.spend))
        s.fact("Spend where the PO was raised after the invoice", eur(d.retro_spend), True)
        s.fact("Share of dated PO spend raised after the invoice",
               pct(d.retro_spend, d.dated_po_spend))
        s.fact("Off-channel spend", eur(off_channel), True)
        s.fact("Off-channel share of spend", pct(off_channel, d.spend))
        s.fact("Invoices off channel", count(documents))
        s.fact("Cost of leakage at 1-3%", f"{eur(low)} to {eur(high)}", True)
        typed = sum(d.typed_spend.values())
        catalogue = d.typed_spend.get("External webshop", 0.0) + d.typed_spend.get(
            "Marketplace", 0.0)
        s.fact("Free-text share of typed spend", pct(d.typed_spend.get("Freetext", 0.0), typed))
        s.fact("Webshop and marketplace share of typed spend", pct(catalogue, typed))
        s.fact("Spend with suppliers never seen on a contract",
               eur(self.figures.get("outside_contract", 0.0)))
        if st.rework_cost:
            s.fact("Rework cost", f"{eur(rework)} at {eur(st.rework_cost)} per invoice")
        rows = []
        for l2, total in d.by_l2.most_common():
            off = d.no_po_by_l2.get(l2, 0.0) + d.retro_by_l2.get(l2, 0.0)
            rows.append([l2, round(total, 2), round(d.no_po_by_l2.get(l2, 0.0), 2),
                         round(d.retro_by_l2.get(l2, 0.0), 2), round(off, 2),
                         round(100 * share(off, total), 1)])
        rows.sort(key=lambda r: -r[4])
        for rank, row in enumerate(rows[:3], 1):
            s.fact(f"Off-channel category #{rank}", f"{row[0]}: {eur(row[4])} "
                                                    f"({row[5]:.1f}% of its spend)")
        s.tables.append(Table(
            "Off channel by L2", ["Category L2", "Spend EUR", "No PO EUR",
                                  "PO after invoice EUR", "Off-channel EUR",
                                  "Off-channel share %"], rows,
            chart=("Category L2", "Off-channel EUR")))
        s.tables.append(Table(
            "Item types", ["Item type", "Spend EUR", "Share of typed spend %"],
            top_rows(d.typed_spend, typed, 100)))
        s.definitions += [
            "Off channel: lines with no purchase order, plus lines whose purchase order was "
            "created after the invoice date (a retrospective order).",
            f"A retrospective order can only be seen where the order date is known "
            f"({eur(d.dated_po_spend)} of spend).",
            "Cost of leakage: 1-3% of off-channel spend, the deck's benchmark.",
        ]
        if not st.rework_cost:
            s.caveats.append("Rework is not costed: pass --rework-cost-per-document to price "
                             "each off-channel invoice.")
        return s

    def q6_payment(self) -> Section:
        d = self.data
        st = self.settings
        s = Section("q6", "Payment terms, early payment and bundling",
                    "Which suppliers or categories offer the largest payment-term, "
                    "early-payment discount, or demand-bundling opportunity?")
        rate = st.cost_of_capital
        rows = []
        total_value = total_low = 0.0
        measured = 0.0
        early_cost = 0.0
        weighted_term = 0.0
        for key, x in d.suppliers.items():
            if x.pay_spend <= 0 or x.name == "(no supplier)":
                continue
            measured += x.pay_spend
            weighted_term += x.pay_term
            term = x.pay_term / x.pay_spend
            actual = x.pay_actual / x.pay_spend
            gain = max(st.target_days - term, 0.0)
            value = x.pay_spend * gain * rate / 365
            low = x.pay_spend * min(gain, 15.0) * rate / 365
            early = x.early_days * rate / 365
            early_cost += early
            total_value += value
            total_low += low
            rows.append([x.name, x.main_l2, round(x.pay_spend, 2), round(term, 1),
                         round(actual, 1), round(gain, 1), round(value, 2),
                         round(x.early_spend, 2), round(early, 2)])
        rows.sort(key=lambda r: -r[6])
        self.figures["payment_low"] = total_low
        self.figures["payment_high"] = total_value
        l2_rows = []
        for l2, (spend, term_sum, actual_sum) in d.pay_by_l2.items():
            if spend <= 0:
                continue
            term = term_sum / spend
            gain = max(st.target_days - term, 0.0)
            l2_rows.append([l2, round(spend, 2), round(term, 1), round(actual_sum / spend, 1),
                            round(spend * gain * rate / 365, 2)])
        l2_rows.sort(key=lambda r: -r[4])
        bundles = []
        for gid, g in d.groups.items():
            if norm_text(g.label) in CATCH_ALL_GROUPS:
                continue
            buyers = [k for k, v in g.suppliers.items() if v > 0]
            if len(g.areas) >= 3 and len(buyers) >= 3 and g.spend > 0:
                bundles.append([g.label or gid, g.l2.most_common(1)[0][0], len(g.areas),
                                len(buyers), round(g.spend, 2)])
        bundles.sort(key=lambda r: -r[4])
        s.fact("Spend with measurable payment terms", eur(measured))
        s.fact("Share of spend with measurable payment terms", pct(measured, d.spend))
        s.fact("Average agreed payment term (spend-weighted)",
               f"{share(weighted_term, measured):.1f} days", True)
        s.fact("Target payment term", f"{st.target_days} days")
        s.fact("Cost of capital", f"{rate:.1%}")
        s.fact("Annual value of moving terms to target", eur(total_value), True)
        s.fact("Annual value of moving terms by up to 15 days", eur(total_low))
        s.fact("Annual cost of paying before the due date", eur(early_cost), True)
        s.fact("Demand-bundling candidates (groups bought by 3+ areas from 3+ suppliers)",
               count(len(bundles)), True)
        s.fact("Spend in demand-bundling candidates", eur(sum(b[4] for b in bundles)))
        for rank, row in enumerate(rows[:3], 1):
            s.fact(f"Payment-term supplier #{rank}", f"{row[0]}: {eur(row[2])} at {row[3]:.1f} "
                                                     f"days, worth {eur(row[6])} a year")
        for rank, row in enumerate(bundles[:3], 1):
            s.fact(f"Bundling candidate #{rank}", f"{row[0]}: {eur(row[4])}, {row[2]} areas, "
                                                  f"{row[3]} suppliers")
        s.tables.append(Table(
            "Payment terms by supplier", ["Supplier", "Main category L2", "Spend EUR",
                                          "Agreed term days", "Actual days to pay",
                                          "Days to target", "Annual value EUR",
                                          "Paid early EUR", "Cost of early payment EUR"],
            rows, chart=("Supplier", "Annual value EUR")))
        s.tables.append(Table(
            "Payment terms by L2", ["Category L2", "Spend EUR", "Agreed term days",
                                    "Actual days to pay", "Annual value EUR"], l2_rows))
        s.tables.append(Table(
            "Payment term mix", ["Agreed term", "Spend EUR", "Share %"],
            top_rows(d.term_spend, measured, 20)))
        s.tables.append(Table(
            "Demand bundling", ["AI purchase group", "Category L2", "Business areas",
                                "Suppliers", "Spend EUR"], bundles))
        s.definitions += [
            "Agreed term: due date minus invoice date. Actual: payment date minus invoice "
            "date. Lines whose invoice date is more than a year from posting are left out "
            f"({count(d.pay_excluded)} lines).",
            f"Value: spend x days improved x {rate:.0%} cost of capital / 365, the deck's "
            f"formula, with {st.target_days} days as the target.",
            f"Early payment: paid more than {st.early_grace_days} days before the due date, "
            f"costed at the same rate.",
        ]
        s.caveats.append("The data holds no early-payment discount terms, so the discount "
                         "opportunity is the cost of paying early rather than a discount "
                         "captured.")
        return s

    def value_levers(self) -> Section:
        f = self.figures
        s = Section("levers", "Value levers",
                    "What could each lever be worth, using the deck's benchmarks?")
        levers = [
            ("Price harmonisation", "1-3% of relevant spend", f.get("price_relevant", 0.0),
             0.01, 0.03, None),
            ("Supplier consolidation", "2-5% of realistically consolidatable spend",
             f.get("consolidatable_high", 0.0), 0.02, 0.05, None),
            ("Compliance leakage", "1-3% of off-channel spend", f.get("off_channel", 0.0),
             0.01, 0.03, None),
            ("Standardisation", "3-8% of migrated recurring spend",
             f.get("standardisation_spend", 0.0), 0.03, 0.08, None),
            ("Payment terms", "spend x days improved x cost of capital / 365", 0.0, 0, 0,
             (f.get("payment_low", 0.0), f.get("payment_high", 0.0))),
        ]
        low_total = high_total = 0.0
        for name, rule, base, low_rate, high_rate, fixed in levers:
            low, high = fixed if fixed else (base * low_rate, base * high_rate)
            low_total += low
            high_total += high
            self.levers.append([name, rule, round(base, 2) if not fixed else "",
                                round(low, 2), round(high, 2)])
            s.fact(f"{name}", f"{eur(low)} to {eur(high)}", True)
            if not fixed:
                s.fact(f"{name} base", eur(base))
        s.fact("All levers", f"{eur(low_total)} to {eur(high_total)}", True)
        s.tables.append(Table(
            "Value levers", ["Lever", "Benchmark", "Base spend EUR", "Low EUR", "High EUR"],
            self.levers, chart=("Lever", "High EUR")))
        s.definitions += [
            "Price harmonisation base: spend on like-for-like purchases with material "
            "variance.",
            "Supplier consolidation base: realistically consolidatable spend (see Supplier "
            "fragmentation and consolidation).",
            "Compliance base: off-channel spend.",
            "Standardisation base: recurring spend at suppliers rated High or Medium "
            "catalogue suitability.",
            "Payment terms: low moves each supplier by up to 15 days, high to the target.",
            "The levers overlap (the same spend can be consolidated and harmonised), so "
            "the total is not additive.",
        ]
        return s

    # -- buying channels -----------------------------------------------------

    def channel_payloads(self) -> List[Dict[str, Any]]:
        by_supplier: Dict[str, List[Combo]] = defaultdict(list)
        for combo in self.recurring():
            by_supplier[combo.supplier].append(combo)
        ranked = sorted(by_supplier.items(), key=lambda kv: -sum(c.spend for c in kv[1]))
        payloads = []
        for key, combos in ranked[:self.settings.channel_suppliers]:
            supplier = self.data.suppliers[key]
            kinds = Counter()
            for c in combos:
                kinds.update(c.kinds)
            top = sorted(combos, key=lambda c: -c.spend)[:8]
            payloads.append({
                "id": key,
                "supplier": supplier.name,
                "category_l2": supplier.main_l2,
                "combos": len(combos),
                "lines": sum(c.lines for c in combos),
                "spend": round(sum(c.spend for c in combos), 2),
                "material_lines": kinds.get("Material", 0),
                "service_lines": kinds.get("Service", 0),
                "freetext_lines": sum(c.freetext for c in combos),
                "recurring_purchases": [
                    {"description": c.text[:160], "lines": c.lines,
                     "spend_eur": round(c.spend), "group": c.group.most_common(1)[0][0]
                     if c.group else ""} for c in top],
            })
        return payloads


def quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    position = (len(values) - 1) * q
    lower = int(math.floor(position))
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def rule_channel(payload: Dict[str, Any]) -> Dict[str, str]:
    text = " ".join(p["description"] for p in payload["recurring_purchases"])
    material, service = payload["material_lines"], payload["service_lines"]
    if CONTRACT_WORDS.search(text):
        return {"suitability": "Low", "channel": "Contract / predefined request",
                "rationale": "Recurring leasing, rent, fees or subscriptions belong under a "
                             "contract rather than in a catalogue."}
    if service > material:
        return {"suitability": "Medium", "channel": "Service catalogue / predefined request",
                "rationale": "Mostly recurring services, which suit a predefined request or "
                             "service catalogue."}
    if payload["combos"] >= 3:
        return {"suitability": "High", "channel": "Supplier catalogue / punch-out",
                "rationale": "Several repeat-bought materials from one supplier suit a "
                             "supplier catalogue or punch-out."}
    return {"suitability": "Medium", "channel": "Internal catalogue item",
            "rationale": "A few repeat-bought materials that can be set up as catalogue "
                         "items."}


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

CHANNEL_SYSTEM = (
    "You are a senior procurement category manager at a Nordic energy utility. For each "
    "supplier you are given its recurring purchases (same description bought repeatedly, "
    "outside any catalogue). Judge how suitable they are for a catalogue and the best "
    "buying channel. Leasing, rent, subscriptions, licences, fees and recurring services "
    "belong under contracts or service catalogues, not catalogues. Answer in JSON only: "
    '{"suppliers": [{"id": "...", "catalogue_suitability": "High|Medium|Low", '
    '"preferred_channel": one of ' + json.dumps(list(CHANNELS)) + ', '
    '"rationale": "one sentence naming what is bought"}]}')

NARRATIVE_SYSTEM = (
    "You are a procurement spend analyst writing for Fortum's procurement leadership. "
    "Answer the question from the fact sheet only. Every figure you write must be copied "
    "exactly as it appears in the fact sheet, with its unit; do not compute, round or "
    "invent any other number. Be specific: name categories and suppliers from the facts. "
    'Answer in JSON only: {"headline": "one sentence answer", "answer": ["3 to 5 bullets"], '
    '"what_this_indicates": ["2 to 3 bullets on what to do"]}')

NUMBER = re.compile(r"\d[\d,.]*\d|\d")


def number_tokens(text: str) -> Set[str]:
    return {token.replace(",", "") for token in NUMBER.findall(text)}


class ModelWriter:
    """The model's two jobs, with the cache, the checks and the fallbacks."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = None
        self.notes: List[str] = []
        if not settings.use_llm:
            return
        from agent4 import LanguageModelClient, resolve_model_config
        env = model_environment(settings.here / ".env")
        config = resolve_model_config(env, True, settings.spend_limit)
        if not config.enabled:
            self.notes.append("The model is not configured; rules and templates were used.")
            return
        self.client = LanguageModelClient(
            config, settings.cache_dir / "spend_analysis_llm_cache.json",
            interactive=settings.interactive)

    def _ask(self, task: str, system: str, payload: Any) -> Optional[Dict[str, Any]]:
        if self.client is None:
            return None
        user = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        key = self.client.cache_key(f"spend_analysis:{task}:{AGENT_VERSION}", system + user)
        cached = self.client.cached(key)
        if cached is not None:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                pass
        answer = self.client.complete_json(system, user)
        if isinstance(answer, dict):
            self.client.store(key, json.dumps(answer, ensure_ascii=False))
        return answer

    def classify(self, payloads: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
        verdicts: Dict[str, Dict[str, str]] = {}
        if payloads:
            print(f"  Judging buying channels for {len(payloads):,} suppliers "
                  f"{'with the model' if self.client else 'by rule'} ...", flush=True)
        for start in range(0, len(payloads), 10):
            batch = payloads[start:start + 10]
            answers: Dict[str, Dict[str, Any]] = {}
            reply = self._ask("channel", CHANNEL_SYSTEM, {"suppliers": [
                {k: v for k, v in p.items() if k != "spend"} for p in batch]})
            for item in (reply or {}).get("suppliers", []) or []:
                if isinstance(item, dict) and item.get("id"):
                    answers[str(item["id"])] = item
            for payload in batch:
                item = answers.get(payload["id"], {})
                suitability = str(item.get("catalogue_suitability", "")).title()
                channel = str(item.get("preferred_channel", ""))
                rationale = str(item.get("rationale", "")).strip()
                if suitability in SUITABILITY and channel in CHANNELS and rationale:
                    verdict = {"suitability": suitability, "channel": channel,
                               "rationale": rationale, "source": "model"}
                else:
                    verdict = {**rule_channel(payload), "source": "rules"}
                verdict.update({
                    "l2": payload["category_l2"], "combos": payload["combos"],
                    "lines": payload["lines"], "spend": payload["spend"],
                    "examples": "; ".join(p["description"][:60]
                                          for p in payload["recurring_purchases"][:3])})
                verdicts[payload["id"]] = verdict
            if self.client is not None and (start // 10) % 5 == 4:
                self.client.save_cache()
        return verdicts

    def narrate(self, section: Section) -> None:
        template(section)
        if self.client is None:
            return
        payload = {"question": section.question, "topic": section.title,
                   "facts": section.facts, "definitions": section.definitions}
        allowed = set()
        for value in [*section.facts.values(), *section.definitions, section.question]:
            allowed |= number_tokens(value)
        allowed |= {str(n) for n in range(0, 13)}
        for attempt in range(2):
            reply = self._ask(f"narrative{attempt}", NARRATIVE_SYSTEM, payload)
            if not isinstance(reply, dict):
                return
            headline = str(reply.get("headline", "")).strip()
            answer = [str(b).strip() for b in reply.get("answer") or [] if str(b).strip()]
            indicates = [str(b).strip() for b in reply.get("what_this_indicates") or []
                         if str(b).strip()]
            if not headline or not answer:
                return
            stray = number_tokens(" ".join([headline, *answer, *indicates])) - allowed
            if not stray:
                section.headline, section.answer, section.indicates = headline, answer, indicates
                section.written_by = "model"
                return
            LOGGER.info("The answer to %s used figures not in its facts (%s)%s.",
                        section.title, ", ".join(sorted(stray)[:5]),
                        "; asking again" if attempt == 0 else "; using the template")
            payload = {**payload, "note": "Your previous answer used figures that are not in "
                                          "the fact sheet: " + ", ".join(sorted(stray)[:10])
                                          + ". Use only figures exactly as written there."}

    def usage(self) -> Dict[str, Any]:
        return self.client.usage.as_dict() if self.client is not None else {}

    def save(self) -> None:
        if self.client is not None:
            self.client.save_cache()


def template(section: Section) -> None:
    """The answer written from the facts without the model."""
    f = section.facts
    k = section.key
    if k == "transparency":
        section.headline = (f"{f['Spend without a clear description']} of spend has no clear "
                            f"description and {f['POs without a clear description']} purchase "
                            f"orders carry none.")
        section.answer = [
            f"Free-text spend is {f['Free-text spend']} ({f['Free-text share of spend with an item type']} "
            f"of spend where the item type is recorded).",
            f"{f['Spend without a clear description']} ({f['Share of spend without a clear description']}) "
            f"is on {f['Lines without a clear description']} lines without a clear description.",
            f"The top 5 categories hold {f['Top 5 categories share of unclear spend']} of it, "
            f"led by {f.get('Unclear spend category #1', '')}.",
        ]
        section.indicates = ["Require a description or item on purchase requests in the top "
                             "categories first; they hold most of the unclear spend."]
    elif k == "tail":
        section.headline = (f"{f['Tail suppliers']} tail suppliers ({f['Tail suppliers share of suppliers']} "
                            f"of suppliers) account for {f['Tail spend']}.")
        section.answer = [
            f"{f['One-time tail suppliers']} tail suppliers were used once.",
            f"The tail is {f['Tail transactions (lines)']} transactions averaging "
            f"{f['Average tail transaction']}.",
            f"Largest tail category: {f.get('Tail category #1 by spend', '')}.",
        ]
        section.indicates = ["Route one-time and low-value buys to catalogues, cards or a "
                             "marketplace instead of new suppliers."]
    elif k == "standardisation":
        section.headline = (f"{f['New catalogue candidates (Agent 3)']} new catalogue candidates, "
                            f"{f['Distinct catalogue items matched']} existing catalogue matches and "
                            f"{f['High-similarity supplier signals (supplier and L2)']} high-similarity "
                            f"supplier signals.")
        section.answer = [
            f"{f['Spend with a potential catalogue match']} is bought outside the catalogue "
            f"although a matching item exists.",
            f"High-similarity signals concentrate in {f.get('High-similarity category #1', '')}"
            f" and {f.get('High-similarity category #2', '')}.",
        ]
        section.indicates = ["Move matched purchases to existing items and review the new "
                             "candidates with category managers."]
    elif k == "catalogue":
        section.headline = (f"Yes: {f['Recurring supplier-description combinations']} recurring "
                            f"supplier-description combinations with "
                            f"{f['Suppliers with recurring combinations']} suppliers, worth "
                            f"{f['Spend in recurring combinations']}.")
        section.answer = [
            f"{f['Lines whose AI description repeats']} lines repeat an AI description at least "
            f"{f['Minimum repeats']} times.",
            *(f[label] for label in ("Channel #1", "Channel #2", "Channel #3") if label in f),
        ]
        section.indicates = ["Start with the suppliers rated High suitability; send recurring "
                             "services and fees to contracts or service catalogues."]
    elif k == "q1":
        section.headline = (f"{f['Spend with suppliers never seen on a contract']} "
                            f"({f['Share of spend with suppliers never seen on a contract']}) is "
                            f"with suppliers that show no contract evidence.")
        section.answer = [
            f"Of PO-matched spend, {f['Contract-covered share of PO-matched spend']} references "
            f"a frame contract or contract number.",
            *(f[label] for label in ("Category #1 by spend without contract evidence",
                                     "Category #2 by spend without contract evidence")
              if label in f),
        ]
        if "Spend outside the preferred supplier list" in f:
            section.answer.insert(0, f"Against the preferred supplier list, "
                                     f"{f['Spend outside the preferred supplier list']} is outside.")
        section.indicates = ["Confirm the preferred supplier list per category and measure "
                             "against it."]
    elif k == "q2":
        section.headline = (f"{f['Like-for-like purchases with material variance']} like-for-like "
                            f"purchases vary materially in price, on {f['Spend on those purchases']}.")
        section.answer = [
            f"Buying all of them at the lower-quartile price would have saved up to "
            f"{f['Spend above the lower-quartile price']}.",
            *(f[label] for label in ("Largest variance #1", "Largest variance #2") if label in f),
        ]
        section.indicates = ["Harmonise prices for the largest gaps through a shared price "
                             "list or framework."]
    elif k == "q3":
        section.headline = (f"{f['Realistically consolidatable spend']} "
                            f"({f['Realistically consolidatable share of spend']} of spend) is "
                            f"realistically consolidatable.")
        section.answer = [
            f"{f['Fragmented AI purchase groups']} purchase groups are fragmented, with "
            f"{f['Spend in fragmented groups']} of spend.",
            f"The upper bound, counting every High and Medium overlap, is "
            f"{f['Upper bound (all High and Medium overlap)']}.",
            *(f[label] for label in ("Consolidation category #1", "Largest move #1")
              if label in f),
        ]
        section.indicates = ["Tender the fragmented groups with the most suppliers first."]
    elif k == "q4":
        section.headline = (f"{f['Recurring spend rated High or Medium catalogue suitability']} of "
                            f"recurring purchases suit a catalogue, standard item or framework.")
        section.answer = [f.get(label, "") for label in ("Convert to #1", "Convert to #2",
                                                         "Convert to #3") if label in f]
        section.answer.insert(0, f"Recurring non-catalogue spend is "
                                 f"{f['Recurring non-catalogue spend']}.")
        section.indicates = ["Convert the largest recurring purchases first; see the "
                             "Recurring combinations sheet."]
    elif k == "q5":
        section.headline = (f"{f['Off-channel spend']} ({f['Off-channel share of spend']}) bypasses "
                            f"the purchase order process; leakage costs {f['Cost of leakage at 1-3%']}.")
        section.answer = [
            f"{f['Spend without a purchase order']} has no purchase order.",
            f"{f['Spend where the PO was raised after the invoice']} had its order raised after the "
            f"invoice.",
            *(f[label] for label in ("Off-channel category #1", "Off-channel category #2")
              if label in f),
        ]
        section.indicates = ["Enforce no-PO-no-pay in the categories with the most "
                             "off-channel spend."]
    elif k == "q6":
        section.headline = (f"Moving payment terms to {f['Target payment term']} is worth "
                            f"{f['Annual value of moving terms to target']} a year.")
        section.answer = [
            f"The spend-weighted agreed term is {f['Average agreed payment term (spend-weighted)']}.",
            f"Paying before the due date costs {f['Annual cost of paying before the due date']} a year.",
            *(f[label] for label in ("Payment-term supplier #1", "Bundling candidate #1")
              if label in f),
        ]
        section.indicates = ["Renegotiate terms with the largest suppliers below target and "
                             "stop paying early."]
    elif k == "levers":
        section.headline = f"The levers are worth {f['All levers']}, with overlap."
        section.answer = [f"{name}: {f[name]}" for name in (
            "Price harmonisation", "Supplier consolidation", "Compliance leakage",
            "Standardisation", "Payment terms")]
        section.indicates = ["Size each lever per category before committing to targets."]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def safe_sheet(name: str, taken: Set[str]) -> str:
    base = re.sub(r"[\[\]\*\?/\\:]", " ", name)[:31].strip() or "Sheet"
    candidate, n = base, 2
    while candidate.casefold() in taken:
        suffix = f" {n}"
        candidate = base[:31 - len(suffix)] + suffix
        n += 1
    taken.add(candidate.casefold())
    return candidate


def write_xlsx(path: Path, analysis: Analysis, overview: List[List[Any]],
               coverage: List[List[Any]]) -> bool:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        LOGGER.warning("openpyxl is not installed, so no workbook was written "
                       "(pip install openpyxl).")
        return False
    book = Workbook()
    bold = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F4E79")
    taken: Set[str] = set()

    def sheet(title: str, columns: List[str], rows: Iterable[List[Any]], first: bool = False):
        ws = book.active if first else book.create_sheet()
        ws.title = safe_sheet(title, taken)
        ws.append(columns)
        for cell in ws[1]:
            cell.font, cell.fill = bold, fill
        for row in rows:
            ws.append(row)
        for at, column in enumerate(columns, 1):
            width = max([len(str(column))] + [len(str(r[at - 1])) for r in
                                               list(ws.iter_rows(min_row=2, max_row=200,
                                                                 values_only=True))
                                               if at - 1 < len(r) and r[at - 1] is not None])
            ws.column_dimensions[ws.cell(1, at).column_letter].width = min(max(width, 10), 60)
        ws.freeze_panes = "A2"
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, float):
                    cell.number_format = "#,##0.00"
                elif isinstance(cell.value, str) and len(cell.value) > 60:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
        return ws

    summary = []
    for s in analysis.sections:
        summary.append([s.title, s.question, s.headline,
                        "\n".join(f"{label}: {value}" for label, value in s.kpis),
                        "\n".join(s.answer), "\n".join(s.indicates), s.written_by])
    sheet("Summary", ["Topic", "Question", "Answer", "Key figures", "Detail",
                      "What this indicates", "Written by"], summary, first=True)
    sheet("Scope", ["Item", "Value"], overview)
    for s in analysis.sections:
        for table in s.tables:
            sheet(table.name, table.columns, table.rows)
    definitions = [[s.title, text] for s in analysis.sections
                   for text in [*s.definitions, *s.caveats]]
    sheet("Definitions", ["Topic", "Definition or caveat"], definitions)
    sheet("Coverage", ["Run", "Item", "Value"], coverage)
    facts = [[s.title, label, value] for s in analysis.sections for label, value in s.facts.items()]
    sheet("Facts", ["Topic", "Figure", "Value"], facts)
    book.save(path)
    return True


def markdown_table(table: Table, limit: int = 10) -> List[str]:
    lines = ["| " + " | ".join(table.columns) + " |",
             "|" + "|".join("---" for _ in table.columns) + "|"]
    for row in table.rows[:limit]:
        cells = []
        for value, column in zip(row, table.columns):
            if isinstance(value, float) and "EUR" in column:
                cells.append(eur(value))
            elif isinstance(value, float):
                cells.append(f"{value:,.1f}")
            elif isinstance(value, int):
                cells.append(f"{value:,}")
            else:
                cells.append(str(value).replace("|", "/")[:90])
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def write_markdown(path: Path, analysis: Analysis, overview: List[List[Any]],
                   notes: List[str]) -> str:
    out = ["# Spend analysis - twelve months", ""]
    out += [f"- **{item}:** {value}" for item, value in overview]
    if notes:
        out += ["", "## Notes on the data", ""] + [f"- {n}" for n in notes]
    for s in analysis.sections:
        out += ["", f"## {s.title}", "", f"*{s.question}*", "", f"**{s.headline}**", ""]
        out += [f"- {b}" for b in s.answer]
        if s.indicates:
            out += ["", "What this indicates:", ""] + [f"- {b}" for b in s.indicates]
        if s.kpis:
            out += ["", "| Figure | Value |", "|---|---|"]
            out += [f"| {label} | {value} |" for label, value in s.kpis]
        if s.tables:
            out += ["", f"Top of *{s.tables[0].name}* (all rows in the workbook):", ""]
            out += markdown_table(s.tables[0])
        if s.definitions or s.caveats:
            out += ["", "Definitions and caveats:", ""]
            out += [f"- {t}" for t in [*s.definitions, *s.caveats]]
    text = "\n".join(out) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def write_html(path: Path, analysis: Analysis, overview: List[List[Any]],
               notes: List[str]) -> None:
    e = html.escape
    parts = ["<!doctype html><html><head><meta charset='utf-8'><title>Spend analysis</title>",
             "<style>body{font-family:Segoe UI,Arial,sans-serif;max-width:1100px;margin:2em auto;"
             "color:#1b1b1b}h1,h2{color:#1F4E79}table{border-collapse:collapse;margin:.5em 0}"
             "td,th{border:1px solid #ccc;padding:4px 8px;font-size:13px}th{background:#1F4E79;"
             "color:#fff}.kpi{display:inline-block;margin:4px;padding:8px 12px;background:#eef3f8;"
             "border-radius:6px}.q{color:#555;font-style:italic}</style></head><body>",
             "<h1>Spend analysis - twelve months</h1><ul>"]
    parts += [f"<li><b>{e(str(a))}:</b> {e(str(b))}</li>" for a, b in overview]
    parts.append("</ul>")
    if notes:
        parts.append("<h2>Notes on the data</h2><ul>"
                     + "".join(f"<li>{e(n)}</li>" for n in notes) + "</ul>")
    for s in analysis.sections:
        parts.append(f"<h2>{e(s.title)}</h2><p class='q'>{e(s.question)}</p>"
                     f"<p><b>{e(s.headline)}</b></p>")
        parts.append("".join(f"<span class='kpi'>{e(l)}<br><b>{e(v)}</b></span>"
                             for l, v in s.kpis))
        parts.append("<ul>" + "".join(f"<li>{e(b)}</li>" for b in s.answer) + "</ul>")
        if s.indicates:
            parts.append("<p>What this indicates:</p><ul>"
                         + "".join(f"<li>{e(b)}</li>" for b in s.indicates) + "</ul>")
        if s.tables:
            t = s.tables[0]
            parts.append(f"<p>Top of <i>{e(t.name)}</i>:</p><table><tr>"
                         + "".join(f"<th>{e(c)}</th>" for c in t.columns) + "</tr>")
            for line in markdown_table(t)[2:]:
                cells = [c.strip() for c in line.strip("|").split(" | ")]
                parts.append("<tr>" + "".join(f"<td>{e(c)}</td>" for c in cells) + "</tr>")
            parts.append("</table>")
        if s.definitions or s.caveats:
            parts.append("<details><summary>Definitions and caveats</summary><ul>"
                         + "".join(f"<li>{e(t)}</li>" for t in [*s.definitions, *s.caveats])
                         + "</ul></details>")
    parts.append("</body></html>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_pptx(path: Path, analysis: Analysis, overview: List[List[Any]]) -> bool:
    try:
        from pptx import Presentation
        from pptx.chart.data import CategoryChartData
        from pptx.dml.color import RGBColor
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.util import Inches, Pt
    except ImportError:
        LOGGER.warning("python-pptx is not installed, so no slides were written "
                       "(pip install python-pptx).")
        return False
    navy = RGBColor(0x1F, 0x4E, 0x79)
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    blank = deck.slide_layouts[6]

    def text(slide, left, top, width, height, lines, size=14, bold=False, color=None):
        box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        frame = box.text_frame
        frame.word_wrap = True
        for at, line in enumerate(lines):
            paragraph = frame.paragraphs[0] if at == 0 else frame.add_paragraph()
            paragraph.text = line
            paragraph.font.size = Pt(size)
            paragraph.font.bold = bold
            if color is not None:
                paragraph.font.color.rgb = color
        return box

    slide = deck.slides.add_slide(blank)
    text(slide, 0.6, 2.2, 12, 1.2, ["Spend analysis - twelve months"], 40, True, navy)
    text(slide, 0.6, 3.5, 12, 3, [f"{a}: {b}" for a, b in overview], 16)

    for s in analysis.sections:
        slide = deck.slides.add_slide(blank)
        text(slide, 0.5, 0.3, 12.3, 0.6, [s.title], 26, True, navy)
        text(slide, 0.5, 0.9, 12.3, 0.9, [s.headline], 16, True)
        for at, (label, value) in enumerate(s.kpis[:5]):
            box = slide.shapes.add_shape(1, Inches(0.5), Inches(1.9 + at * 0.95), Inches(3.6),
                                         Inches(0.85))
            box.fill.solid()
            box.fill.fore_color.rgb = RGBColor(0xEE, 0xF3, 0xF8)
            box.line.fill.background()
            frame = box.text_frame
            frame.word_wrap = True
            frame.paragraphs[0].text = value
            frame.paragraphs[0].font.size = Pt(18)
            frame.paragraphs[0].font.bold = True
            frame.paragraphs[0].font.color.rgb = navy
            label_line = frame.add_paragraph()
            label_line.text = label
            label_line.font.size = Pt(10)
            label_line.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
        chart_table = next((t for t in s.tables if t.chart and t.rows), None)
        if chart_table is not None:
            category, value = chart_table.chart
            ci, vi = chart_table.columns.index(category), chart_table.columns.index(value)
            rows = [r for r in chart_table.rows if isinstance(r[vi], (int, float))][:8]
            data = CategoryChartData()
            data.categories = [str(r[ci])[:45] for r in reversed(rows)]
            data.add_series(value, [float(r[vi]) for r in reversed(rows)])
            frame = slide.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED, Inches(4.4),
                                           Inches(1.9), Inches(8.4), Inches(3.3), data)
            chart = frame.chart
            chart.has_legend = False
            chart.has_title = True
            chart.chart_title.text_frame.text = f"{value} - {chart_table.name}"
            chart.chart_title.text_frame.paragraphs[0].font.size = Pt(12)
            chart.category_axis.tick_labels.font.size = Pt(9)
            chart.value_axis.tick_labels.font.size = Pt(9)
            chart.plots[0].series[0].format.fill.solid()
            chart.plots[0].series[0].format.fill.fore_color.rgb = navy
        bullets = [f"• {b}" for b in [*s.answer[:3], *s.indicates[:1]]]
        text(slide, 4.4, 5.3, 8.4, 2.0, bullets, 12)
        notes = slide.notes_slide.notes_text_frame
        notes.text = "\n".join([s.question, *s.definitions, *s.caveats])
    deck.save(path)
    return True


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def read_preferred(path: Optional[Path]) -> Optional[Set[str]]:
    if path is None:
        return None
    names = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            for cell in row[:2]:
                if norm_text(cell):
                    names.add(norm_text(cell))
    return names


def read_runs(settings: Settings) -> Tuple[Aggregates, List[RunInput]]:
    data = Aggregates(settings)
    preferred = read_preferred(settings.preferred_file)
    runs = []
    for path in settings.inputs:
        run = prepare_input(path, settings.keep_groups)
        runs.append(run)
        print(f"  Reading {run.name}/{path.name} ...", flush=True)
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            columns = Columns(header)
            run.missing_fields = columns.missing()
            if not columns.at["spend"]:
                raise SystemExit(f"{path} has no spend column ({', '.join(FIELDS['spend'])}).")
            get = columns.getter()
            for row in reader:
                data.add(run, row, get, preferred)
        print(f"    {run.lines:,} lines, {run.period}", flush=True)
        if run.regrouped and run.lines:
            coverage = run.regroup_hits / run.lines
            run.notes.append(f"Purchase groups from {REGROUPED_NAME} on "
                             f"{coverage:.1%} of lines.")
            if coverage < 0.99:
                LOGGER.warning("%s covers only %.1f%% of %s's lines; the rest keep the "
                               "dataset's groups.", REGROUPED_NAME, 100 * coverage, run.name)
    return data, runs


def overview_rows(data: Aggregates, runs: List[RunInput]) -> List[List[Any]]:
    periods = sorted(p for run in runs for p in run.periods if p)
    rows = [["Period", f"{periods[0]} to {periods[-1]}" if periods else "unknown"],
            ["Months", str(len(set(periods)))],
            ["Total spend", eur(data.spend)],
            ["Purchase lines", count(data.lines)],
            ["Suppliers", count(len(data.suppliers))],
            ["Credit notes (negative lines)", eur(data.negative)]]
    for run in runs:
        rows.append([f"Run {run.name}", f"{run.period}, {count(run.lines)} lines"])
    return rows


def coverage_rows(runs: List[RunInput]) -> List[List[Any]]:
    rows = []
    for run in runs:
        rows += [[run.name, "Dataset", str(run.path)],
                 [run.name, "Lines analysed", run.lines],
                 [run.name, "Subtotal rows left out", run.excluded_subtotal],
                 [run.name, "Duplicate rows left out", run.excluded_duplicate],
                 [run.name, "Lines with unreadable spend (counted as 0)", run.unparsed_spend],
                 [run.name, "Purchase groups", str(run.regroup_file or "as in the dataset")],
                 [run.name, "Columns not found", ", ".join(run.missing_fields) or "none"]]
        rows += [[run.name, "Note", note] for note in run.notes]
    return rows


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Spend analysis over the finished agent runs, answering the questions "
                    "of the spend analytics deck.")
    parser.add_argument("--input", nargs="+", metavar="FILE",
                        help="Merged datasets to analyse (default: the all_agents_dataset.csv "
                             "of new_results_llm and may_dec_results).")
    parser.add_argument("--results", help=f"Output folder (default: {DEFAULT_OUTPUT_DIR}/).")
    parser.add_argument("--cache", help="Cache folder for model answers (default: cache/).")
    parser.add_argument("--no-llm", action="store_true",
                        help="Use rules and templates instead of the model.")
    parser.add_argument("--llm-spend-limit", type=float, default=None,
                        help="Stop calling the model after this many dollars.")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Never stop to ask a question.")
    parser.add_argument("--skip-model-check", action="store_true",
                        help="Do not test the model endpoint before starting.")
    parser.add_argument("--keep-dataset-groups", action="store_true",
                        help=f"Use the purchase groups in the datasets even where "
                             f"{REGROUPED_NAME} exists.")
    parser.add_argument("--preferred-suppliers", metavar="FILE",
                        help="CSV of preferred suppliers (name or id in the first columns).")
    parser.add_argument("--cost-of-capital", type=float, default=0.05,
                        help="Annual cost of capital for payment terms (default 0.05).")
    parser.add_argument("--target-payment-days", type=int, default=60,
                        help="Payment term to aim for (default 60).")
    parser.add_argument("--rework-cost-per-document", type=float, default=0.0,
                        help="Cost of handling one off-channel invoice, in euro (default: "
                             "not costed).")
    parser.add_argument("--recurrence-min", type=int, default=6,
                        help="Lines for a purchase to count as recurring (default 6).")
    parser.add_argument("--tail-share", type=float, default=0.80,
                        help="Share of spend held by non-tail suppliers (default 0.80).")
    parser.add_argument("--channel-suppliers", type=int, default=400,
                        help="Suppliers whose buying channel is judged (default 400).")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    configure_process_logging(args.verbose)
    here = Path(__file__).resolve().parent
    inputs = ([Path(p).expanduser().resolve() for p in args.input] if args.input
              else default_inputs(here))
    missing = [p for p in inputs if not p.is_file()]
    if missing or not inputs:
        raise SystemExit("Cannot read: " + (", ".join(map(str, missing)) or
                                            "no datasets found; pass --input FILE ..."))
    settings = Settings(
        here=here, inputs=inputs,
        results_dir=Path(args.results or here / DEFAULT_OUTPUT_DIR).expanduser().resolve(),
        cache_dir=Path(args.cache or here / "cache").expanduser().resolve(),
        use_llm=not args.no_llm, spend_limit=args.llm_spend_limit,
        interactive=not args.non_interactive, skip_model_check=args.skip_model_check,
        keep_groups=args.keep_dataset_groups,
        preferred_file=Path(args.preferred_suppliers).expanduser()
        if args.preferred_suppliers else None,
        cost_of_capital=args.cost_of_capital, target_days=args.target_payment_days,
        rework_cost=args.rework_cost_per_document, recurrence_min=args.recurrence_min,
        tail_share=args.tail_share, channel_suppliers=args.channel_suppliers)

    print(f"\n{AGENT_NAME} {AGENT_VERSION}\n", flush=True)
    if settings.use_llm:
        from all_agents import preflight_model
        preflight_model(here, settings.skip_model_check)
    started = datetime.datetime.now()
    data, runs = read_runs(settings)
    if not data.lines:
        raise SystemExit("No purchase lines were read.")

    writer = ModelWriter(settings)
    analysis = Analysis(data, settings, runs)
    try:
        analysis.build(writer.classify)
        print("  Writing the answers ...", flush=True)
        for section in analysis.sections:
            writer.narrate(section)
    except SpendLimitReached:
        writer.save()
        raise
    writer.save()

    settings.results_dir.mkdir(parents=True, exist_ok=True)
    overview = overview_rows(data, runs)
    coverage = coverage_rows(runs)
    notes = [f"{run.name}: {note}" for run in runs for note in run.notes] + writer.notes
    outputs = {}
    xlsx = settings.results_dir / "spend_analysis.xlsx"
    if write_xlsx(xlsx, analysis, overview, coverage):
        outputs["workbook"] = xlsx.name
    pptx = settings.results_dir / "spend_analysis.pptx"
    if write_pptx(pptx, analysis, overview):
        outputs["slides"] = pptx.name
    md = settings.results_dir / "spend_analysis_report.md"
    write_markdown(md, analysis, overview, notes)
    outputs["report"] = md.name
    page = settings.results_dir / "spend_analysis_report.html"
    write_html(page, analysis, overview, notes)
    outputs["html"] = page.name

    manifest = {
        "agent": AGENT_NAME, "version": AGENT_VERSION,
        "written": datetime.datetime.now().isoformat(timespec="seconds"),
        "seconds": round((datetime.datetime.now() - started).total_seconds(), 1),
        "inputs": [{"run": r.name, "dataset": str(r.path), "period": r.period,
                    "lines": r.lines, "regrouped_from": str(r.regroup_file or ""),
                    "notes": r.notes} for r in runs],
        "settings": {k: (str(v) if isinstance(v, Path) else v)
                     for k, v in vars(args).items()},
        "answers": {s.key: {"headline": s.headline, "written_by": s.written_by,
                            "facts": s.facts} for s in analysis.sections},
        "model_usage": writer.usage(),
        "outputs": outputs,
    }
    (settings.results_dir / "spend_analysis_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 79)
    print("  Spend analysis")
    print("=" * 79)
    for item, value in overview:
        print(f"  {item:<32}: {value}")
    print()
    for s in analysis.sections:
        print(f"  {s.title}\n    {s.headline}")
    for note in notes:
        print(f"  Note: {note}")
    usage = writer.usage()
    if usage:
        print(f"\n  Model: {usage.get('requests', 0)} request(s), "
              f"{usage.get('cache_hits', 0)} from cache")
    print(f"\n  Written to {settings.results_dir}:")
    for name in outputs.values():
        print(f"    {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
