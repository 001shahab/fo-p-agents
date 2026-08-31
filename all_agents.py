#!/usr/bin/env python3
"""
All Agents - the input table, widened with every agent's answer
===============================================================

One command, one deliverable: the purchase table exactly as it came in, with the
columns Agents 1, 2, 3 and 4 produce added to the right of it.

Max builds the wide table. This script does not rebuild it. It takes the table
Max has already produced, runs the four agents over it, and joins their answers
back on. Where Max reports its work as a stage per join, this reports one file
that a buyer can open in Excel and read across.

The one rule
------------

Every input row appears in the output, once, in the order it arrived. That is not
a hope about how the agents behave, it is enforced here: the output is written by
walking the input, and an agent's answer is something a row is given, never
something a row depends on to exist. A row the agents could not annotate is still
written, with those columns empty, and is named in the row audit file so nobody
has to guess which rows they were.

That rule is worth stating in the negative, because the obvious implementation
breaks it. Joining agent output to input by walking the agent output would
silently drop any row an agent skipped, and the count at the bottom of the run
would look correct because it would be counting the wrong side of the join.

How rows are matched
--------------------

Agent 1 records, for each line it writes, the row of the source file it read that
line from, in ``Source_Row_Number``. The number counts the header as row 1, so
row *n* of the file is the *n-2*-th data row. Because that number describes the
input rather than the output, it stays correct even if an agent drops a row: the
surviving rows still carry their own true position.

Trusting a number is not the same as checking it, so the join is verified against
content as well. Several business keys - the document number, its line number,
the supplier number, the PO number - are carried through by Agent 1 unchanged, so
they can be compared with the same fields in the input row the number pointed at.
They must agree everywhere both sides are populated. A single disagreement means
the tables are misaligned, which is the one failure that would corrupt every
column at once while looking perfectly plausible, so it stops the run.

What is reused
--------------

Nothing is recomputed that has already been computed. Max's stage-3 file is used
as the input where one exists rather than rebuilding it, and each agent's output
is reused where the working folder already holds a result that was produced from
the same input by the same version of the same script. The check is a digest of
both files, so a reused result is one that a rerun would reproduce, and editing an
agent invalidates its cached output without anyone having to remember to say so.

Agent 3's key includes the catalogue files, so a newer catalogue from the client
reruns the matching rather than reusing answers found against the old one.

``--force`` reruns everything regardless, and ``--no-reuse`` also declines to
reuse Max's stage-3 file and rebuilds it from the source extracts.

Being interrupted
-----------------

A run that stops - Ctrl-C, a closed laptop, a dead machine - can be continued by
running the same command again. The agents that had finished are reused and the
rest run, which matters because Agent 3 alone takes many minutes.

Progress is journalled as it happens, and the journal's absence is what marks a
run as finished, so the next run knows it is resuming and says so. The output of
the agent that was in flight is deleted rather than reused: reuse asks whether a
result matches its input, and a truncated result can still answer yes. An
interactive run offers the choice between continuing and starting again; an
unattended one continues, since that is the answer that does not repeat work.

The catalogue, and the model
---------------------------

Agent 3 is pointed at the client's catalogue master by name, preferring the file
in the source folder. That is a correction, not a preference. A run that read a
193 KB copy of the catalogue from ``./catalogues``, while the 36 MB master the
client had just sent went unread, reported "no comparable standard item found" on
every line and looked entirely healthy doing it - so the file that was read, the
number of items in it and its digest are now all printed, and a catalogue that
loads suspiciously few items is called out.

The language model is on unless ``--no-llm`` says otherwise. The point of the run
is the agents' AI output, and three of the four fall back to their local stack
without complaint when the model is off, which reads as a complete run that
happened to find less. What it cost is totalled per agent at the end, separating
what this run spent from what the reused agents spent when they were produced.

Watching it work
----------------

Each agent's own logging is forwarded as it happens, tagged with the agent it
came from, and kept in a log file in the working folder. Agent 3 embeds the whole
catalogue before it matches anything, which on the client's master is many
minutes during which it says nothing, so a heartbeat reports that it is still
alive rather than letting a healthy run look hung.

Agents 3 and 4 run at the same time. Both read Agent 2's output and neither reads
the other's. Agents 1 and 2 cannot join them - Agent 2 reads what Agent 1 writes,
and both later agents read what Agent 2 writes - so the first half of the chain is
a queue however much anyone would prefer otherwise.

Columns
-------

The input's own columns are never written over. Where an agent produces a column
whose name the input already uses - ``Country`` and ``Quantity`` are the usual
pair, since Max reports the source's own spelling of both - the agent's column is
added under a prefixed name and both values survive. Nothing has to be resolved
and nothing has to be believed over anything else.

Agent 4's columns are prefixed as a block. Its unit of analysis is a supplier
inside a comparison scope, not a purchase line, so those columns describe the
line's supplier rather than the line, and the prefix is there so that a reader
sorting on ``Agent4_Similarity_Percent`` can see they are sorting suppliers.

Where the table comes from
--------------------------

Three ways in, and what the caller asks for decides between them rather than
whatever is lying in the results folder.

Starting from raw data is the complete job: the extracts are joined and
interpreted first, by Max with its own agent stages declined, and the four agents
then run over the result. Reusing a table that has already been built skips that
work, which is worth skipping - it costs the joins and, with the model on, a call
per line - but only when the table still describes the extracts on disk.

    python3 all_agents.py --from-sources      # raw extracts, all the way through
    python3 all_agents.py --sources DIR       # the same, naming the folder
    python3 all_agents.py --input FILE        # a table that already exists
    python3 all_agents.py                     # prompts, then reuses what it can

Usage
-----

    python3 all_agents.py                     # prompts, then reuses what it can
    python3 all_agents.py --non-interactive   # same, unattended
    python3 all_agents.py --no-llm            # local stack only, no model spend
    python3 all_agents.py --force             # rerun the agents, keep the table
    python3 all_agents.py --no-reuse          # rebuild everything from the extracts

Author: Prof. Shahab Anbarjafari
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

AGENT_NAME = "All Agents - Input Table Widened With Agents 1 to 4"
AGENT_VERSION = "1.0.0"

BANNER = f"""
===============================================================================
{AGENT_NAME}
Version {AGENT_VERSION}
===============================================================================
Runs Agents 1, 2, 3 and 4 over the purchase table and returns that same table
with their columns added. Every input row is written exactly once.
"""

LOGGER = logging.getLogger("all_agents")

# Field sizes in procurement exports run to long free-text notes, and the reader
# refuses anything over 128 KB by default.
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))


# ---------------------------------------------------------------------------
# What the pipeline knows about its neighbours
# ---------------------------------------------------------------------------

# The file Max leaves behind that this script reads. Its later stages are
# preferred over its earlier ones: stage 3 is the same rows as stage 1 with more
# known about them, so there is never a reason to start further back.
MAX_STAGE_FILES: Tuple[str, ...] = ("max_stage3_interpreted.csv",
                                    "max_stage2_with_po.csv",
                                    "max_stage1_sievo_invoice.csv")

MAX_MANIFEST_NAME = "max_run_manifest.json"

# Where Agent 1 records which row of which file it read a line from.
ROW_NUMBER_COLUMN = "Source_Row_Number"
SOURCE_FILE_COLUMN = "Source_File"

# ``Source_Row_Number`` counts the header, so the first data row is row 2.
HEADER_ROWS = 2

# Business keys Agent 1 carries through unchanged, paired with the input spellings
# they may appear under. Used to prove the row match rather than assume it, so
# each pair must be a field Agent 1 copies rather than one it interprets.
CROSS_CHECKS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Document_Number", ("Document number", "DocumentNumber", "Document_Number",
                         "Document No", "Invoice number", "INVOICENUM")),
    ("Document_Line_Number", ("Document line number", "Document_Line_Number",
                             "DocumentLineNumber", "Line number")),
    ("Supplier_Id", ("ERP supplier number", "Supplier number", "Supplier_Id",
                     "SupplierNumber", "Vendor number", "VENDOR")),
    ("PO_Number", ("PO number", "PO_Number", "PONumber", "Purchase order number",
                   "PONUM")),
)

# Enough agreement to establish the join without demanding a field that a given
# extract may barely populate. One pair that agrees on a reasonable number of
# rows settles it; the rest are checked too, and any disagreement anywhere is
# fatal regardless of how many rows it was seen on.
CROSS_CHECK_MINIMUM = 5

# Agent 4's columns describe a supplier, not a line, and are prefixed to say so.
AGENT4_PREFIX = "Agent4_"

# Where Agent 4 names the supplier a row is about, and the identity file that
# maps the spellings in the purchase table onto those keys.
SUPPLIER_KEY_COLUMN = "Supplier_Key"
AGENT4_MASTER_NAME = "agent4_supplier_master.csv"
AGENT4_CONSOLIDATION_NAME = "agent4_supplier_consolidation.csv"

# What each agent leaves in the working folder, so a run interrupted part way
# through one can throw away the file it may not have finished writing. Keyed by
# the tag the run journal records. The provenance stamp goes with it: without the
# stamp the output cannot be reused, which is the point.
AGENT_ARTEFACTS: Dict[str, Tuple[str, ...]] = {
    "Agent 1": ("agent1_unified_lines.csv", ".agent1.reuse.json"),
    "Agent 2": ("agent2_purchase_groups.csv", ".agent2.reuse.json"),
    "Agent 3": ("agent3_standardisation.csv", ".agent3.reuse.json"),
    "Agent 4": (AGENT4_CONSOLIDATION_NAME, ".agent4.reuse.json"),
}
AGENT4_MANIFEST_NAME = "agent4_run_manifest.json"

# Columns of Agent 4's output that identify which supplier and scope a row is
# about. They are the join key, so they are not attached again as data - except
# the supplier key, which is worth carrying so the companion file can be joined.
AGENT4_KEY_COLUMNS = ("Scope_Level", "Scope_Value", "Is_Primary_Scope")

# How a purchase line says who supplied it and which category it sits in, as
# Agent 1 writes them. Agent 4 groups on the same fields, so these are what the
# supplier-level answer is looked up by.
LINE_SUPPLIER_NAME = "Supplier_Name"
LINE_SUPPLIER_ID = "Supplier_Id"

# Output names.
OUTPUT_CSV = "all_agents_dataset.csv"
OUTPUT_JSONL = "all_agents_dataset.jsonl"
OUTPUT_AUDIT = "all_agents_row_audit.csv"
OUTPUT_MANIFEST = "all_agents_run_manifest.json"

# Rows named individually in the audit file before it starts summarising. A run
# with a systematic problem would otherwise write an audit file the size of the
# dataset, which nobody reads and which hides the case where three rows failed.
AUDIT_ROW_LIMIT = 5000

# How the client's item catalogue master is named. Both spellings they have sent
# are matched, spaced and unspaced, because the file has arrived as both
# "Fortum - Item Catalogues - Master.xlsx" and "Fortum-ItemCatalogues-Master.xlsx".
#
# Agent 3 is pointed at this file rather than at a folder. Naming the file is the
# difference between matching against the client's catalogue and matching against
# whatever else happens to sit beside it, and it was an old 4,200-row copy of
# this same file being picked up from ./catalogues - in place of the 846,000-row
# master - that had Agent 3 reporting no match on every line.
CATALOGUE_MASTER_GLOB = "*Item*Catalogue*Master*.xls*"

# Below this, a file called a catalogue master is more likely to be an extract or
# an old copy than the client's current catalogue, and is worth saying so about.
CATALOGUE_ITEMS_EXPECTED = 10_000

# Seconds of silence from an agent before the run says it is still alive. Agent 3
# embeds the whole catalogue in one call that prints nothing for many minutes,
# and a pipeline that looks hung gets killed by whoever is watching it.
HEARTBEAT_SECONDS = 60


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    """Everything the run needs, resolved from arguments and prompts."""

    source_dir: Path
    results_dir: Path
    lexicon_path: Path
    cache_dir: Path
    input_path: Optional[Path] = None
    catalogue_dir: Optional[Path] = None

    reuse: bool = True
    force: bool = False

    # Start from the raw extracts: join and interpret them first, then run the
    # agents over the result. Set by --from-sources, and implied by naming a
    # --sources folder, because asking for a source folder and then reading a
    # table built from somewhere else is not what anyone means by it.
    from_sources: bool = False

    # On by default. The point of the run is the agents' AI output, and three of
    # the four fall back to their local stack silently when it is off - which
    # reads as a complete run that simply found less.
    use_llm: bool = True
    llm_spend_limit: Optional[float] = None
    agent_timeout: Optional[int] = None
    write_jsonl: bool = True

    # Agents 3 and 4 both read Agent 2's output and neither reads the other's, so
    # they are the only pair in the chain that can overlap.
    parallel: bool = True

    # False under --non-interactive, where nothing may block waiting for an answer
    # and an interrupted run is resumed without asking.
    interactive: bool = True

    @property
    def work_dir(self) -> Path:
        """Where this script keeps the agents' own outputs.

        Kept apart from the folder Max gives its agents so that the two can run
        against the same results folder without either overwriting results the
        other would go on to reuse.
        """
        return self.results_dir / "all_agents"

    def reuse_dirs(self) -> Tuple[Path, ...]:
        """Folders searched for an agent result that need not be recomputed.

        Our own folder first, then Max's, because a result Max produced from the
        identical input by the identical script is the same result - and on a
        machine where Max has already run the whole chain, that is the entire
        pipeline saved.
        """
        return (self.work_dir, self.results_dir / "agents")


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")


def normalise(value: Any) -> str:
    """Compare-ready form of a cell: trimmed, collapsed, case-folded."""
    if value is None:
        return ""
    return _WHITESPACE.sub(" ", str(value).strip()).casefold()


def digest_of(path: Path) -> str:
    """Content digest of a file, or an empty string if it cannot be read.

    Truncated to 16 hex characters, which is far more than enough to tell two
    versions of one file apart and short enough to read in a manifest.
    """
    try:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(block)
        return hasher.hexdigest()[:16]
    except OSError:
        return ""


def describe_file(path: Path, rows: Optional[int] = None) -> Dict[str, Any]:
    """Identify a file well enough to tell two versions of it apart."""
    described: Dict[str, Any] = {"file": path.name, "path": str(path)}
    try:
        stat = path.stat()
        described["modified"] = datetime.datetime.fromtimestamp(
            stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        described["bytes"] = stat.st_size
    except OSError:
        described["modified"] = ""
        described["bytes"] = 0
    described["sha256"] = digest_of(path)
    if rows is not None:
        described["rows"] = rows
    return described


def read_header(path: Path) -> List[str]:
    """Column names of a CSV file, without reading the body."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            return [column.strip() for column in row]
    return []


def read_rows(path: Path) -> Tuple[List[str], List[List[str]]]:
    """Header and body of a CSV file, as lists rather than dictionaries.

    Lists because a wide procurement table has hundreds of columns and one
    dictionary per row would spend most of the run's memory storing the same
    column names over and over.
    """
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = [column.strip() for column in next(reader)]
        except StopIteration:
            return [], []
        body = [row for row in reader]
    return header, body


def stream_rows(path: Path) -> Iterator[List[str]]:
    """Body of a CSV file a row at a time, so the input is never held twice."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for row in reader:
            yield row


def count_rows(path: Path) -> int:
    """Data rows in a CSV file."""
    return sum(1 for _ in stream_rows(path))


def restore_dropped_values(header: Sequence[str], rows: List[List[str]],
                           earlier: Sequence[Path]) -> Dict[str, int]:
    """Put back values the agent chain carried a header for but not the data.

    Each agent reads the one before it and republishes its columns, so the last
    file in the chain is normally the whole line-level answer. Normally, not
    always: Agent 3 republishes Agent 1's ``AI_Confidence`` header and writes
    nothing under it, so a merge reading only Agent 3's file publishes an empty
    ``AI_Confidence`` for every row - a column the client asked for, present,
    named correctly and blank, which is worse than absent because it looks
    answered.

    Only cells that are empty in the last file are considered, and only columns
    that some row is actually missing, so a value the last agent did write is
    never second-guessed. Earlier files are read nearest-first and streamed, and
    just the cells being restored are held, so this costs a pass over each file
    and not another copy of the table.

    Returns the number of cells restored per column, for the run to report.
    """
    empty_at = [position for position, _ in enumerate(header)
                if any(not (row[position].strip() if position < len(row) else "")
                       for row in rows)]
    if not empty_at or not earlier:
        return {}

    try:
        key_at = list(header).index(ROW_NUMBER_COLUMN)
    except ValueError:
        # Without the row number there is no safe way to say which row of an
        # earlier file describes which row of this one, and guessing by position
        # is how values end up on the wrong line.
        return {}

    row_by_key = {}
    for index, row in enumerate(rows):
        if key_at < len(row):
            row_by_key.setdefault(row[key_at].strip(), index)

    restored: Dict[str, int] = {}
    for path in earlier:
        wanted = [position for position in empty_at
                  if any(not (rows[index][position].strip()
                              if position < len(rows[index]) else "")
                         for index in row_by_key.values())]
        if not wanted:
            break
        source_header = read_header(path)
        if ROW_NUMBER_COLUMN not in source_header:
            continue
        source_key_at = source_header.index(ROW_NUMBER_COLUMN)
        lookup = {header[position]: position for position in wanted}
        source_at = {name: source_header.index(name)
                     for name in lookup if name in source_header}
        if not source_at:
            continue

        for source_row in stream_rows(path):
            if source_key_at >= len(source_row):
                continue
            index = row_by_key.get(source_row[source_key_at].strip())
            if index is None:
                continue
            target = rows[index]
            if len(target) < len(header):
                target.extend([""] * (len(header) - len(target)))
            for name, at in source_at.items():
                if at >= len(source_row):
                    continue
                value = source_row[at].strip()
                if value and not target[lookup[name]].strip():
                    target[lookup[name]] = source_row[at]
                    restored[name] = restored.get(name, 0) + 1
    return restored


def ask(question: str, default: str) -> str:
    """One prompt with a default, for the interactive path."""
    try:
        answer = input(f"  {question} [{default}]: ").strip()
    except EOFError:
        return default
    return answer or default


def ask_yes_no(question: str, default: bool) -> bool:
    """One yes/no prompt, for the interactive path."""
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"  {question} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer.startswith("y")


def human_seconds(seconds: float) -> str:
    """A duration in the units a reader thinks in."""
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 90:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def unique_name(name: str, taken: Iterable[str]) -> str:
    """A column name that is not in use, by suffixing a number if it is.

    Reached only when a source extract already has a column called something like
    ``Agent1_Country``, which is unlikely but is not worth losing a column over.
    """
    used = set(taken)
    if name not in used:
        return name
    for suffix in range(2, 1000):
        candidate = f"{name}_{suffix}"
        if candidate not in used:
            return candidate
    raise RuntimeError(f"cannot find a free column name for {name}")


# ---------------------------------------------------------------------------
# Surviving an interruption
# ---------------------------------------------------------------------------

class RunJournal:
    """A note of what a run has finished, so an interrupted one can be resumed.

    The agents already leave reusable results behind: each one stamps its output
    with what it was made from, and a later run reuses anything a rerun would
    reproduce. That machinery is what does the resuming. What was missing was
    knowing that a resume is what is wanted.

    Two things go wrong without this. An interrupted run left no sign it had been
    interrupted, so the next run looked like an ordinary one and said nothing
    about the many minutes of work it was about to reuse - or about the agent
    that had been half way through writing its output when the power went. And a
    part-written file is the more dangerous of the two, because reuse tests
    whether a result matches its input, and a truncated result can still be
    internally consistent.

    So the step that was in flight is recorded, and its output is deleted before
    anything is allowed to reuse it.
    """

    NAME = ".run_journal.json"

    def __init__(self, work_dir: Path) -> None:
        self.path = work_dir / self.NAME
        self.state: Dict[str, Any] = {}

    # -- reading what a previous run left ------------------------------------

    def previous(self) -> Dict[str, Any]:
        """The state of a run that did not finish, or nothing."""
        if not self.path.is_file():
            return {}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return state if isinstance(state, dict) else {}

    @staticmethod
    def finished_steps(state: Dict[str, Any]) -> List[str]:
        return [name for name, status in (state.get("steps") or {}).items()
                if status == "done"]

    @staticmethod
    def unfinished_steps(state: Dict[str, Any]) -> List[str]:
        """Every step that did not finish cleanly, however it ended.

        Failed counts as unfinished here. An agent that was killed part way
        through may be recorded either way - the thread running it can get as far
        as noting the failure before the interruption is dealt with - and in both
        cases whatever it wrote is not to be trusted.
        """
        return [name for name, status in (state.get("steps") or {}).items()
                if status != "done"]

    # -- recording this one --------------------------------------------------

    def start(self, input_path: Path, input_digest: str, use_llm: bool) -> None:
        self.state = {
            "started": datetime.datetime.now().isoformat(timespec="seconds"),
            "input": input_path.name,
            "input_digest": input_digest,
            "use_llm": use_llm,
            "steps": {},
        }
        self._save()

    def began(self, name: str) -> None:
        self.state.setdefault("steps", {})[name] = "running"
        self._save()

    def finished(self, name: str, ok: bool) -> None:
        self.state.setdefault("steps", {})[name] = "done" if ok else "failed"
        self._save()

    def clear(self) -> None:
        """Remove the journal, which is what marks the run as having finished."""
        try:
            self.path.unlink()
        except OSError:
            pass

    def _save(self) -> None:
        self.state["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        except OSError as error:
            LOGGER.debug("Could not record run progress: %s", error)


# ---------------------------------------------------------------------------
# Deciding which catalogue Agent 3 matches against
# ---------------------------------------------------------------------------

@dataclass
class CatalogueChoice:
    """The catalogue Agent 3 will read, and every candidate that was not chosen.

    The alternatives are carried rather than discarded because the failure this
    class exists to prevent is a silent one. Agent 3 reported "no comparable
    standard item found" on every line of a run that looked entirely healthy, and
    the reason was that it had loaded a 193 KB copy of the catalogue from
    ./catalogues while the 36 MB master the client had just sent sat unread in the
    source folder. Nothing in that run said which file had been used.
    """

    arguments: List[str] = field(default_factory=list)
    path: Optional[Path] = None
    origin: str = ""
    alternatives: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"origin": self.origin,
                                   "not_used": self.alternatives}
        if self.path:
            payload.update(describe_file(self.path))
        return payload


def choose_catalogue(here: Path, source_dir: Path,
                     named: Optional[Path]) -> CatalogueChoice:
    """Decide which item catalogue Agent 3 matches against, and say so.

    A file is named rather than a folder wherever one can be found. Agent 3 takes
    either, and handing it a folder means it reads everything in there that could
    pass for a price list - which in the source folder is a question with a
    different answer every time the client sends new data.
    """
    choice = CatalogueChoice()

    if named:
        choice.path = named if named.is_file() else None
        choice.origin = f"named on the command line: {named}"
        choice.arguments = ["--catalogues", str(named)]
        if not named.exists():
            choice.warnings.append(
                f"The catalogue path given does not exist: {named}. Agent 3 will "
                f"report that it loaded no catalogue.")
        return choice

    # Every file in either place that is named like the client's master, largest
    # first. Size stands in for item count here because counting means opening a
    # 36 MB workbook, and the run is about to do that anyway.
    candidates: List[Tuple[Path, str]] = []
    for folder, label in ((source_dir, "source folder"), (here / "catalogues",
                                                          "catalogues folder")):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob(CATALOGUE_MASTER_GLOB)):
            if path.is_file() and not path.name.startswith((".", "~$")):
                candidates.append((path, label))

    if candidates:
        # The source folder wins over ./catalogues, and within a folder the larger
        # file wins. Fortum send the master as a full replacement rather than as a
        # delta, so a smaller file of the same name is an older copy of it.
        candidates.sort(key=lambda entry: (entry[1] != "source folder",
                                           -entry[0].stat().st_size,
                                           entry[0].name))
        chosen, label = candidates[0]
        choice.path = chosen
        choice.origin = f"the client's catalogue master, found in the {label}"
        choice.arguments = ["--catalogues", str(chosen)]
        choice.alternatives = [
            {**describe_file(path), "location": other}
            for path, other in candidates[1:]]
        for path, other in candidates[1:]:
            if path.stat().st_size * 4 < chosen.stat().st_size:
                choice.warnings.append(
                    f"{path.name} in the {other} is much smaller than the master "
                    f"being used ({path.stat().st_size:,} against "
                    f"{chosen.stat().st_size:,} bytes) and looks like an older "
                    f"copy. It has not been read. Delete it to avoid doubt.")
        return choice

    catalogues = here / "catalogues"
    if catalogues.is_dir():
        choice.origin = f"every catalogue in {catalogues}"
        choice.arguments = ["--catalogues", str(catalogues)]
        choice.warnings.append(
            f"No file named like the client's catalogue master was found in "
            f"{source_dir}, so the whole {catalogues.name} folder is read instead.")
        return choice

    choice.origin = f"the source folder, {source_dir}"
    choice.arguments = ["--reference", str(source_dir)]
    choice.warnings.append(
        f"No item catalogue was found. Agent 3 will read {source_dir} and refuse "
        f"the purchase extracts in it, which leaves it nothing to match against.")
    return choice


# ---------------------------------------------------------------------------
# Deciding what to run the agents over
# ---------------------------------------------------------------------------

@dataclass
class InputChoice:
    """The table the agents will read, and how it was arrived at."""

    path: Path
    origin: str
    rows: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


class InputResolver:
    """Finds the purchase table the agents will read.

    There are three ways in, and the caller's stated intent decides between them
    rather than what happens to be lying in the results folder.

    A file named with --input is read as it stands. A source folder asked for with
    --from-sources, or named with --sources, is joined and interpreted first so the
    run starts from the raw extracts. Otherwise an existing table is reused, since
    building one costs the joins and, with the model on, a call per line.

    The middle case used to be unreachable while any stage file sat in the results
    folder: --sources was accepted and then ignored, so pointing the script at a
    fresh extract quietly produced a run over the old one. A silently substituted
    input is the worst kind of wrong answer, because the output looks complete.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.here = Path(__file__).resolve().parent

    def resolve(self) -> InputChoice:
        if self.settings.input_path:
            return self._named(self.settings.input_path)
        if self.settings.from_sources:
            return self._rebuild()
        if self.settings.reuse and not self.settings.force:
            existing = self._existing()
            if existing:
                return existing
        return self._rebuild()

    # -- the three ways in ---------------------------------------------------

    def _named(self, path: Path) -> InputChoice:
        """A table the caller pointed at."""
        if not path.is_file():
            raise SystemExit(f"The input file does not exist: {path}")
        if path.suffix.lower() != ".csv":
            raise SystemExit(
                f"The input must be a CSV file, and this is {path.suffix or 'extensionless'}: "
                f"{path}\nThe output is the input with columns added, which means "
                f"reading the input back verbatim to write it out again.")
        choice = InputChoice(path=path, origin="named on the command line",
                             rows=count_rows(path))
        choice.detail = describe_file(path, choice.rows)
        return choice

    def _existing(self) -> Optional[InputChoice]:
        """The best table Max has already left in the results folder."""
        for name in MAX_STAGE_FILES:
            candidate = self.settings.results_dir / name
            if not candidate.is_file():
                continue
            rows = count_rows(candidate)
            if not rows:
                LOGGER.warning("%s is present but has no data rows; looking further "
                               "back.", name)
                continue
            choice = InputChoice(path=candidate, rows=rows,
                                 origin=f"reused from {name}, already in the results folder")
            choice.detail = describe_file(candidate, rows)
            choice.warnings = self._staleness(candidate, name)
            return choice
        return None

    def _staleness(self, path: Path, name: str) -> List[str]:
        """Reasons to doubt that a reused table still describes the sources.

        Reuse saves the whole of Max, so it is worth having, but a table built
        before the extracts it was built from is a table missing rows - and rows
        going missing quietly upstream is exactly what this script is meant to
        prevent. Reported rather than refused, because the caller may know
        perfectly well that the file came from another machine and is current.
        """
        notes: List[str] = []
        if name != MAX_STAGE_FILES[0]:
            notes.append(
                f"{name} is an earlier stage than {MAX_STAGE_FILES[0]}, so the "
                f"agents will read fewer joined columns than Max can produce.")

        try:
            built = path.stat().st_mtime
        except OSError:
            return notes

        newer = []
        if self.settings.source_dir.is_dir():
            for source in sorted(self.settings.source_dir.rglob("*")):
                if not source.is_file() or source.name.startswith("~$"):
                    continue
                if source.suffix.lower() not in {".csv", ".xlsx", ".xls", ".txt"}:
                    continue
                # The item catalogue lives in the source folder too, and a new one
                # arriving there says nothing about whether the transaction table
                # is complete. It is Agent 3's input, not Max's.
                if source.match(CATALOGUE_MASTER_GLOB):
                    continue
                try:
                    if source.stat().st_mtime > built:
                        newer.append(source.name)
                except OSError:
                    continue
        if newer:
            notes.append(
                f"{len(newer)} source extract(s) have changed since {name} was "
                f"built ({', '.join(sorted(newer)[:3])}"
                f"{', ...' if len(newer) > 3 else ''}). Rows added to those "
                f"extracts are not in it. Rerun with --no-reuse to rebuild.")

        manifest = self.settings.results_dir / MAX_MANIFEST_NAME
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            statistics = payload.get("statistics") or {}
            claimed = statistics.get("rows_out")
            actual = count_rows(path)
            if isinstance(claimed, int) and claimed != actual:
                notes.append(
                    f"Max's manifest reports {claimed:,} row(s) but {name} holds "
                    f"{actual:,}. The file may be from a different run than the "
                    f"manifest.")
        return notes

    def _rebuild(self) -> InputChoice:
        """Build the table by running Max, with its own agent stages switched off.

        Max is run rather than imported, and its agent stages are declined,
        because the point of this script is to be the thing that runs the agents.
        Running them twice would double the model spend for the same answers.
        """
        script = self.here / "max.py"
        if not script.is_file():
            raise SystemExit(
                f"No purchase table to read and max.py is not in {self.here}, so "
                f"one cannot be built.\nPoint --input at a CSV file instead.")
        if not self.settings.source_dir.is_dir():
            raise SystemExit(
                f"Cannot start from the raw extracts because the source folder does "
                f"not exist:\n  {self.settings.source_dir}\n"
                f"Name the right folder with --sources, or point --input at a "
                f"purchase table that already exists.")

        extracts = [path for path in sorted(self.settings.source_dir.rglob("*"))
                    if path.is_file() and not path.name.startswith((".", "~$"))
                    and path.suffix.lower() in {".csv", ".xlsx", ".xls"}]
        if not extracts:
            raise SystemExit(
                f"The source folder holds no CSV or Excel extract to read:\n"
                f"  {self.settings.source_dir}\n"
                f"Max needs the transaction, invoice and purchase-order extracts "
                f"there before the agents have anything to run over.")
        LOGGER.info("Starting from the raw extracts in %s (%d file(s)).",
                    self.settings.source_dir, len(extracts))

        arguments = [sys.executable, str(script), "--non-interactive", "--no-agents",
                     "--sources", str(self.settings.source_dir),
                     "--results", str(self.settings.results_dir),
                     "--lexicon", str(self.settings.lexicon_path),
                     "--cache", str(self.settings.cache_dir)]
        if not self.settings.write_jsonl:
            arguments.append("--no-jsonl")
        if self.settings.use_llm:
            arguments.append("--use-llm")
            if self.settings.llm_spend_limit:
                arguments += ["--llm-spend-limit", f"{self.settings.llm_spend_limit:.2f}"]

        LOGGER.info("Building the purchase table with max.py (its own agent stages "
                    "are switched off).")
        outcome = subprocess.run(arguments, cwd=str(self.here), text=True)
        if outcome.returncode != 0:
            raise SystemExit(
                f"max.py exited with code {outcome.returncode}, so there is no "
                f"purchase table to run the agents over.")

        built = self.settings.results_dir / MAX_STAGE_FILES[0]
        if not built.is_file():
            raise SystemExit(f"max.py finished but wrote no {MAX_STAGE_FILES[0]}.")
        rows = count_rows(built)
        choice = InputChoice(path=built, rows=rows,
                            origin=f"built by max.py from {self.settings.source_dir}")
        choice.detail = describe_file(built, rows)
        return choice


# ---------------------------------------------------------------------------
# Running, or not rerunning, the agents
# ---------------------------------------------------------------------------

@dataclass
class AgentStep:
    """One agent: how to run it, and what to expect from it."""

    name: str
    script: str
    output_name: str

    # Anything outside the input file and the script that changes what this agent
    # produces. Agent 3 puts the catalogue files here, so that loading a newer
    # catalogue reruns the matching instead of reusing answers found against the
    # old one - which is the reuse mistake that would be hardest to notice.
    cache_salt: Dict[str, Any] = field(default_factory=dict)

    # Whether an output found on disk with no record of what it was made from may
    # be reused after the checks in ``_verify_unstamped``. True for the agents
    # that annotate lines, because their output can be checked against the input
    # row by row; false for Agent 4, whose output cannot.
    unstamped_reuse: bool = False

    # Short tag used to prefix this agent's log lines when several are running.
    tag: str = ""

    # Filled in as the step is decided and carried out.
    input_path: Optional[Path] = None
    output_path: Optional[Path] = None
    log_path: Optional[Path] = None
    ok: bool = False
    reused: bool = False
    reason: str = ""
    seconds: float = 0.0
    rows: int = 0
    columns: List[str] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.reused:
            return "reused"
        return "ran" if self.ok else "failed"

    @property
    def spend(self) -> float:
        try:
            return float(self.usage.get("estimated_cost_usd") or 0.0)
        except (TypeError, ValueError):
            return 0.0


class AgentChain:
    """Runs Agents 1 to 4, skipping any whose answer is already on disk.

    The agents are run as separate processes rather than imported. Each is a
    standalone script with its own vocabulary loader, response cache and spend
    guard, and each writes the manifest the client reads. Keeping them in their
    own process is what lets this script add their columns without taking on
    responsibility for their internals.

    The chain is sequential and cumulative: Agent 2 reads what Agent 1 wrote and
    keeps its columns, Agent 3 reads what Agent 2 wrote and keeps both. That is
    worth knowing, because it means the last output in the chain carries all
    three agents' columns and the merge has one table to join rather than three
    that could disagree with each other.

    Agent 4 is off to the side. It reads Agent 2's output, as Agent 3 does, but
    what it writes is one row per supplier per comparison scope rather than one
    row per line.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.here = Path(__file__).resolve().parent
        self.steps: List[AgentStep] = []

        # The table the whole chain started from. Agent 1 stamps its name into
        # Source_File and every later agent carries it forward unchanged, so this
        # is what a line-level output names - not the file the agent read.
        self.origin_name = ""

        self.catalogue: CatalogueChoice = CatalogueChoice()

        self.journal = RunJournal(settings.work_dir)

        # One lock around printing. Two agents run at once and their logs are
        # interleaved on purpose, but a line from one must not land inside a line
        # from the other.
        self._console = threading.Lock()

        # The agents running right now, so an interrupt can stop them rather than
        # leaving them to finish writing into a run that has been abandoned.
        self._live: Dict[str, subprocess.Popen] = {}
        self._live_lock = threading.Lock()

    def stop_live_agents(self) -> List[str]:
        """Ask any running agent to stop, and say which ones were asked."""
        with self._live_lock:
            running = list(self._live.items())
        stopped = []
        for tag, process in running:
            if process.poll() is None:
                stopped.append(tag)
                try:
                    process.terminate()
                except OSError:
                    continue
        for _, process in running:
            try:
                process.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
        return stopped

    # -- arguments -----------------------------------------------------------

    def _common_arguments(self) -> List[str]:
        """Arguments every agent takes, so one run here configures all four."""
        arguments = ["--non-interactive",
                     "--results", str(self.settings.work_dir),
                     "--lexicon", str(self.settings.lexicon_path),
                     "--cache", str(self.settings.cache_dir)]
        if not self.settings.write_jsonl:
            arguments.append("--no-jsonl")
        if self.settings.use_llm:
            arguments.append("--use-llm")
            if self.settings.llm_spend_limit:
                arguments += ["--llm-spend-limit",
                              f"{self.settings.llm_spend_limit:.2f}"]
        return arguments

    def _catalogue_arguments(self) -> List[str]:
        """Where Agent 3 should look for the client's item catalogue."""
        if not self.catalogue.arguments:
            self.catalogue = choose_catalogue(self.here, self.settings.source_dir,
                                              self.settings.catalogue_dir)
            LOGGER.info("Agent 3 catalogue: %s", self.catalogue.origin)
            if self.catalogue.path:
                described = describe_file(self.catalogue.path)
                LOGGER.info("  %s, %s bytes, modified %s",
                            described["file"], f"{described['bytes']:,}",
                            described["modified"] or "unknown")
            for note in self.catalogue.warnings:
                LOGGER.warning(note)
        return list(self.catalogue.arguments)

    # -- reuse ---------------------------------------------------------------

    def _fingerprint(self, step: AgentStep, input_path: Path,
                     arguments: Sequence[str]) -> Dict[str, Any]:
        """What a cached result has to match to be worth reusing.

        The input's content, the agent script's content, and the settings that
        change what the agent does. Digesting the script rather than reading its
        version number means editing an agent invalidates its cached output
        whether or not anyone remembered to bump the version.
        """
        script = self.here / step.script
        # Paths differ harmlessly between machines and runs, so the settings that
        # matter are recorded rather than the whole command line.
        significant = [argument for argument in arguments
                       if argument.startswith("--")
                       and argument not in {"--results", "--lexicon", "--cache",
                                            "--input", "--registry",
                                            "--catalogues", "--reference"}]
        return {
            "input_sha256": digest_of(input_path),
            "script_sha256": digest_of(script),
            "settings": sorted(significant),
            "lexicon_sha256": digest_of(self.settings.lexicon_path),
            "salt": step.cache_salt,
            "format": AGENT_VERSION,
        }

    def _catalogue_salt(self) -> Dict[str, Any]:
        """A digest of every catalogue file Agent 3 will read.

        Fortum send a new item catalogue as they extend it, and matching answers
        found against last month's are not answers to the question being asked.
        Digesting the files means a new catalogue reruns the matching on its own,
        without anyone having to remember to pass --force.
        """
        folders = [self.settings.catalogue_dir, self.here / "catalogues"]
        for folder in folders:
            if folder and folder.is_dir():
                return {"catalogues": [
                    {"file": path.name, "sha256": digest_of(path)}
                    for path in sorted(folder.rglob("*"))
                    if path.is_file() and not path.name.startswith((".", "~$"))]}
        return {"catalogues": []}

    def _stamp_path(self, step: AgentStep) -> Path:
        return self.settings.work_dir / f".{Path(step.script).stem}.reuse.json"

    def _verify_unstamped(self, step: AgentStep, candidate: Path,
                          input_path: Path) -> bool:
        """Whether an output with no provenance record is safe to reuse anyway.

        Max writes the same agent outputs into its own folder without the record
        this script keeps, and on a machine where Max has already run the chain
        that folder is the entire pipeline already computed. Throwing it away for
        want of a sidecar file would be perverse, so it is checked instead: the
        output must say it came from this input file, and must have a row for
        every row of it.

        Those two facts are not proof, but they are not the last word either. The
        merge goes on to compare business keys row by row and refuses to write
        anything if they disagree, so a stale output that passes here is still
        caught before it can reach the deliverable.
        """
        header = read_header(candidate)
        if ROW_NUMBER_COLUMN not in header:
            return False
        rows = 0
        named = set()
        with candidate.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                rows += 1
                stated = normalise(row.get(SOURCE_FILE_COLUMN, ""))
                if stated:
                    named.add(stated)

        # Compared with the table the chain started from, not with this step's
        # own input. Agent 1 records where the line came from and the later
        # agents pass that through, so Agent 2's output names Agent 1's input.
        origin = normalise(self.origin_name or input_path.name)
        if named and named != {origin}:
            LOGGER.debug("%s names source file(s) %s, not %s, so it is not reused.",
                         candidate.name, sorted(named), origin)
            return False
        expected = count_rows(input_path)
        if rows != expected:
            LOGGER.debug("%s has %d row(s) and the input has %d, so it is not reused.",
                         candidate.name, rows, expected)
            return False
        return True

    def _reusable(self, step: AgentStep, want: Dict[str, Any],
                  input_path: Path) -> Optional[Path]:
        """An existing output that a rerun would only reproduce, if there is one."""
        if not self.settings.reuse or self.settings.force:
            return None
        for folder in self.settings.reuse_dirs():
            candidate = folder / step.output_name
            if not candidate.is_file():
                continue
            stamp = folder / f".{Path(step.script).stem}.reuse.json"
            if stamp.is_file():
                try:
                    have = json.loads(stamp.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    have = {}
                if have.get("fingerprint") == want:
                    return candidate
                LOGGER.debug("%s in %s was made from different inputs, so it is rerun.",
                             step.output_name, folder.name)
                continue
            if step.unstamped_reuse and self._verify_unstamped(step, candidate,
                                                              input_path):
                LOGGER.info("%s in %s carries no provenance record but matches this "
                            "input row for row, so it is reused.",
                            step.output_name, folder.name)
                return candidate
        return None

    # -- running -------------------------------------------------------------

    def _invoke(self, step: AgentStep, arguments: Sequence[str],
                input_path: Path) -> AgentStep:
        """Reuse or run one agent, reporting what happened without raising."""
        step.input_path = input_path
        script = self.here / step.script
        if not script.is_file():
            step.reason = f"{step.script} is not in {self.here}"
            LOGGER.error("Cannot run %s: %s.", step.name, step.reason)
            return step

        want = self._fingerprint(step, input_path, arguments)
        cached = self._reusable(step, want, input_path)
        if cached:
            step.output_path = cached
            step.ok = True
            step.reused = True
            step.reason = f"already produced from this input, in {cached.parent.name}"
            self._describe_output(step)
            self._say(step, f"reusing {cached.name}, {step.rows:,} row(s) - "
                            f"this input has already been through it")
            self.journal.finished(step.tag or step.name, True)
            return step

        # Recorded before the agent starts, so an interruption leaves a note of
        # which agent was in flight and whose output may be part-written.
        self.journal.began(step.tag or step.name)

        returncode, tail = self._run_process(step, arguments)
        if returncode is None:
            step.reason = f"did not finish within {self.settings.agent_timeout}s"
            self._say(step, f"TIMED OUT after {human_seconds(step.seconds)}")
            self.journal.finished(step.tag or step.name, False)
            return step

        if returncode != 0:
            # The agent's own last words are more useful than the exit code.
            step.reason = tail[-1] if tail else f"exit code {returncode}"
            self._say(step, f"FAILED: {step.reason}")
            if step.log_path:
                self._say(step, f"its full output is in {step.log_path.name}")
            self.journal.finished(step.tag or step.name, False)
            return step

        produced = self.settings.work_dir / step.output_name
        if not produced.is_file():
            step.reason = f"finished but wrote no {step.output_name}"
            self._say(step, step.reason)
            self.journal.finished(step.tag or step.name, False)
            return step

        step.output_path = produced
        step.ok = True
        self._describe_output(step)
        self._say(step, f"done in {human_seconds(step.seconds)}, "
                        f"{step.rows:,} row(s)"
                        + (f", model spend ${step.spend:,.2f}" if step.spend else ""))
        try:
            self._stamp_path(step).write_text(
                json.dumps({"fingerprint": want,
                            "written": datetime.datetime.now().isoformat(timespec="seconds"),
                            "output": step.output_name,
                            "rows": step.rows},
                           indent=2), encoding="utf-8")
        except OSError as error:
            LOGGER.debug("Could not record what %s was made from: %s",
                         step.output_name, error)
        self.journal.finished(step.tag or step.name, True)
        return step

    # -- watching an agent work ---------------------------------------------

    def _say(self, step: AgentStep, message: str) -> None:
        """One line about an agent, tagged so parallel logs stay readable."""
        with self._console:
            print(f"  [{step.tag}] {message}", flush=True)

    def _run_process(self, step: AgentStep,
                     arguments: Sequence[str]) -> Tuple[Optional[int], List[str]]:
        """Run one agent, showing its own logging as it happens.

        The agent's output is forwarded line by line rather than captured and
        thrown away. Two reasons. Agent 3 embeds the whole catalogue before it
        matches anything, which is many minutes of silence on the client's
        master, and a pipeline that prints nothing for that long gets killed
        by whoever is watching it. And the agents report what they loaded - how
        many catalogue items, how much the model cost - which is the information
        needed to tell a run that found nothing from a run that read nothing.

        Every line is also written to a log file, kept whether the agent
        succeeded or not, so a run left overnight can still be read afterwards.
        """
        script = self.here / step.script
        command = [sys.executable, str(script), *arguments]
        step.log_path = self.settings.work_dir / f"{Path(step.script).stem}.log"

        if step.input_path:
            self._say(step, f"starting, reading {step.input_path.name}")
        else:
            self._say(step, "starting")

        started = time.time()
        tail: List[str] = []
        last_output = [started]

        try:
            process = subprocess.Popen(
                command, cwd=str(self.here), text=True, bufsize=1,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                # Unbuffered child logging, or a Python process writing to a pipe
                # holds its output until it exits and the live log is not live.
                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        except OSError as error:
            step.seconds = time.time() - started
            return 1, [str(error)]

        with self._live_lock:
            self._live[step.tag] = process

        def pump() -> None:
            assert process.stdout is not None
            with step.log_path.open("w", encoding="utf-8") as log:
                for line in process.stdout:
                    text = line.rstrip("\n")
                    last_output[0] = time.time()
                    log.write(line)
                    if text.strip():
                        tail.append(text.strip())
                        del tail[:-40]
                    with self._console:
                        print(f"  [{step.tag}] {text}", flush=True)

        def heartbeat() -> None:
            """Say the agent is alive while it is quiet."""
            while process.poll() is None:
                time.sleep(2)
                quiet = time.time() - last_output[0]
                if quiet >= HEARTBEAT_SECONDS and process.poll() is None:
                    self._say(step, f"still working, {human_seconds(time.time() - started)} "
                                    f"elapsed, quiet for {human_seconds(quiet)}")
                    last_output[0] = time.time()

        reader = threading.Thread(target=pump, name=f"{step.tag}-log", daemon=True)
        pulse = threading.Thread(target=heartbeat, name=f"{step.tag}-pulse", daemon=True)
        reader.start()
        pulse.start()

        try:
            try:
                process.wait(timeout=self.settings.agent_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                reader.join(timeout=5)
                step.seconds = time.time() - started
                return None, tail
        finally:
            with self._live_lock:
                self._live.pop(step.tag, None)

        reader.join(timeout=10)
        step.seconds = time.time() - started
        return process.returncode, tail

    def _describe_output(self, step: AgentStep) -> None:
        if not step.output_path:
            return
        step.columns = read_header(step.output_path)
        step.rows = count_rows(step.output_path)
        step.usage = self._read_usage(step)

    def _read_usage(self, step: AgentStep) -> Dict[str, Any]:
        """What the agent's own manifest says the model cost it."""
        if not step.output_path:
            return {}
        manifest = step.output_path.parent / f"{Path(step.script).stem}_run_manifest.json"
        if not manifest.is_file():
            return {}
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        usage = (payload.get("statistics") or {}).get("token_usage") or {}
        return usage if isinstance(usage, dict) else {}

    # -- the chain -----------------------------------------------------------

    def run(self, input_path: Path) -> List[AgentStep]:
        """Run the four agents over the purchase table, in order."""
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        self.origin_name = input_path.name
        self.journal.start(input_path, digest_of(input_path), self.settings.use_llm)
        common = self._common_arguments()
        lexicon_dir = self.here / "lexicon"

        agent1 = AgentStep("Agent 1 - purchase descriptions", "agent1.py",
                           "agent1_unified_lines.csv", tag="Agent 1",
                           unstamped_reuse=True)
        self._invoke(agent1, [*common, "--input", str(input_path)], input_path)
        self.steps.append(agent1)
        if not agent1.ok or not agent1.output_path:
            return self.steps

        agent2 = AgentStep("Agent 2 - purchase groups", "agent2.py",
                           "agent2_purchase_groups.csv", tag="Agent 2",
                           unstamped_reuse=True)
        self._invoke(agent2,
                     [*common, "--input", str(agent1.output_path),
                      "--registry", str(lexicon_dir / "agent2_group_registry.json")],
                     agent1.output_path)
        self.steps.append(agent2)
        if not agent2.ok or not agent2.output_path:
            return self.steps

        # Agents 3 and 4 both read Agent 2's output and neither reads the other's,
        # so they are run together. Agents 1 and 2 cannot join them: Agent 2 reads
        # what Agent 1 writes, and Agent 3 and 4 read what Agent 2 writes, so the
        # first half of the chain is a queue whatever anyone would prefer.
        #
        # This is also where the time is. Agent 3 embeds the client's whole
        # catalogue before it matches a single line, so on the current master it
        # runs for far longer than Agent 4, and overlapping them costs nothing and
        # saves all of Agent 4.
        agent3 = AgentStep("Agent 3 - standard items", "agent3.py",
                           "agent3_standardisation.csv", tag="Agent 3",
                           unstamped_reuse=True, cache_salt=self._catalogue_salt())
        agent3_arguments = [*common, "--input", str(agent2.output_path),
                            *self._catalogue_arguments()]

        agent4 = AgentStep("Agent 4 - supplier consolidation", "agent4.py",
                           AGENT4_CONSOLIDATION_NAME, tag="Agent 4")
        agent4_arguments = [*common, "--input", str(agent2.output_path),
                            "--registry",
                            str(lexicon_dir / "agent4_supplier_registry.json")]

        # Registered before they run, not after. An interruption reports what was
        # kept from the steps recorded here, and a step appended only on completion
        # is invisible to that report - so an Agent 4 that had finished before the
        # Ctrl-C went unmentioned while being reused by the next run regardless.
        # A step that has not run yet carries no columns and no output, which every
        # reader of this list already has to handle.
        self.steps.extend([agent3, agent4])

        pending = [(agent3, agent3_arguments), (agent4, agent4_arguments)]
        if self.settings.parallel:
            print(f"\n  Agents 3 and 4 run together from {agent2.output_path.name}. "
                  f"Their logs are interleaved and tagged.\n", flush=True)
            workers = [threading.Thread(target=self._invoke,
                                        args=(step, arguments, agent2.output_path),
                                        name=step.tag, daemon=True)
                       for step, arguments in pending]
            for worker in workers:
                worker.start()
            # Waited for in slices rather than with a bare join. A join with no
            # timeout blocks the main thread in a lock it cannot be woken from, so
            # Ctrl-C during the many minutes Agent 3 spends embedding was
            # noticed only once Agent 3 had finished - which is to say, not at all.
            while any(worker.is_alive() for worker in workers):
                for worker in workers:
                    worker.join(timeout=0.2)
        else:
            for step, arguments in pending:
                self._invoke(step, arguments, agent2.output_path)

        return self.steps

    # -- what came out of it -------------------------------------------------

    def line_steps(self) -> List[AgentStep]:
        """The steps that annotate purchase lines, in chain order."""
        wanted = ("agent1.py", "agent2.py", "agent3.py")
        return [step for step in self.steps if step.script in wanted]

    def last_line_output(self) -> Optional[AgentStep]:
        """The furthest step down the line-level chain that succeeded.

        The chain carries every line-level column forward, so this one file holds
        all their names. It does not always hold all their values - see
        ``earlier_line_outputs`` - so it is the starting point for the merge
        rather than the whole of it. Where Agent 3 failed, Agent 2's output is
        still the complete answer for Agents 1 and 2.
        """
        succeeded = [step for step in self.line_steps() if step.ok and step.output_path]
        return succeeded[-1] if succeeded else None

    def earlier_line_outputs(self) -> List[AgentStep]:
        """The line-level outputs before the last one, nearest first.

        Needed because the chain preserves column names more faithfully than it
        preserves values. Agent 3 republishes Agent 1's ``AI_Confidence`` header
        and leaves the column empty, so a merge that reads only the last file
        publishes an empty ``AI_Confidence`` for every row while Agent 1's own
        output has it on most of them. Reading the earlier files lets a value the
        chain dropped be put back.
        """
        succeeded = [step for step in self.line_steps() if step.ok and step.output_path]
        return list(reversed(succeeded[:-1]))

    def agent4_step(self) -> Optional[AgentStep]:
        for step in self.steps:
            if step.script == "agent4.py":
                return step
        return None

    def step_for(self, script: str) -> Optional[AgentStep]:
        for step in self.steps:
            if step.script == script:
                return step
        return None

    def catalogue_facts(self) -> Dict[str, Any]:
        """What Agent 3 actually loaded, taken from its own manifest.

        Asked and reported because a catalogue that failed to load does not look
        like a failure from the outside. Agent 3 finishes, annotates every row and
        says "no comparable standard item found" on each one, which is the same
        output it produces when the catalogue is fine and the purchases genuinely
        are not in it. The item count is what separates those two runs.
        """
        step = self.step_for("agent3.py")
        if not step or not step.output_path:
            return {}
        manifest = step.output_path.parent / "agent3_run_manifest.json"
        if not manifest.is_file():
            return {}
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        statistics = payload.get("statistics") or {}
        percentiles = statistics.get("score_percentiles") or {}
        thresholds = (payload.get("configuration") or {}).get("thresholds") or {}
        return {
            "items": statistics.get("reference_items") or 0,
            "rows_with_match": statistics.get("rows_with_match") or 0,
            "rows_already_standard": statistics.get("rows_already_standard") or 0,
            "adjudicated_by_model": statistics.get("matches_adjudicated") or 0,
            "best_score": percentiles.get("max"),
            "accept_threshold": thresholds.get("medium_accept"),
            "sources": payload.get("catalogue_sources") or [],
            "refused": payload.get("refused_files") or [],
            "unreadable": payload.get("unreadable_files") or [],
        }

    def catalogue_warnings(self, facts: Dict[str, Any]) -> List[str]:
        """Reasons to doubt that Agent 3 matched against the client's catalogue."""
        notes: List[str] = []
        if not facts:
            return notes
        items = facts.get("items") or 0
        if not items:
            notes.append(
                "Agent 3 loaded no catalogue item at all, so every line was bound "
                "to report no match. Point --catalogues at the client's item "
                "catalogue file.")
        elif items < CATALOGUE_ITEMS_EXPECTED:
            notes.append(
                f"Agent 3 loaded only {items:,} catalogue item(s). The client's "
                f"master holds far more than that, so this is very likely an old "
                f"or partial copy rather than the catalogue they sent.")
        if facts.get("unreadable"):
            notes.append(
                f"Agent 3 could not read {len(facts['unreadable'])} catalogue "
                f"file(s): {', '.join(str(entry) for entry in facts['unreadable'][:3])}. "
                f"Missing openpyxl is the usual cause.")
        if items and not facts.get("rows_with_match") and not facts.get("rows_already_standard"):
            # Two very different runs end here, and saying only "no match" would
            # report them identically. A catalogue that was read and whose best
            # candidate fell just short of the acceptance threshold is a threshold
            # question for the client; a best score nowhere near it means these
            # purchases are genuinely not in the catalogue.
            best = facts.get("best_score")
            accept = facts.get("accept_threshold")
            if isinstance(best, (int, float)) and isinstance(accept, (int, float)):
                shortfall = accept - best
                if 0 < shortfall <= 0.1:
                    notes.append(
                        f"Agent 3 read {items:,} catalogue item(s) and matched no "
                        f"line, but only just: its best candidate scored "
                        f"{best:.3f} against an acceptance threshold of {accept:.2f}. "
                        f"These are near misses, not absences. "
                        f"agent3_match_calibration.csv shows how many lines each "
                        f"threshold would accept, and Fortum set the threshold.")
                else:
                    notes.append(
                        f"Agent 3 read {items:,} catalogue item(s) and matched no "
                        f"line. Its best candidate scored {best:.3f} against a "
                        f"threshold of {accept:.2f}, so these purchases are not in "
                        f"the catalogue rather than borderline.")
            else:
                notes.append(
                    f"Agent 3 read {items:,} catalogue item(s) and still matched no "
                    f"line. That is a real answer on a small extract of services "
                    f"and charges, but worth a look before it is reported as one.")
        return notes

    def spend(self) -> Dict[str, Any]:
        """What the language model cost, per agent and in total.

        Two totals, because they answer different questions. The run total is what
        this run spent; the recorded total includes agents whose output was reused,
        which cost nothing today but did cost something when they were produced.
        """
        per_agent = []
        this_run = 0.0
        recorded = 0.0
        requests = 0
        cache_hits = 0
        stopped: List[str] = []
        for step in self.steps:
            if not step.usage:
                continue
            amount = step.spend
            recorded += amount
            if not step.reused:
                this_run += amount
                requests += int(step.usage.get("requests") or 0)
                cache_hits += int(step.usage.get("cache_hits") or 0)
            if step.usage.get("spend_limit_stopped"):
                stopped.append(step.tag or step.name)
            per_agent.append({
                "agent": step.tag or step.name,
                "reused": step.reused,
                "estimated_cost_usd": round(amount, 4),
                "requests": step.usage.get("requests") or 0,
                "input_tokens": step.usage.get("input_tokens") or 0,
                "output_tokens": step.usage.get("output_tokens") or 0,
                "cache_hits": step.usage.get("cache_hits") or 0,
                "failed_requests": step.usage.get("failed_requests") or 0,
                "spend_limit_usd": step.usage.get("spend_limit_usd") or 0,
                "spend_limit_stopped": bool(step.usage.get("spend_limit_stopped")),
            })
        return {
            "per_agent": per_agent,
            "this_run_usd": round(this_run, 4),
            "recorded_usd": round(recorded, 4),
            "requests_this_run": requests,
            "cache_hits_this_run": cache_hits,
            "stopped_at_limit": stopped,
        }


# ---------------------------------------------------------------------------
# Matching agent rows to input rows
# ---------------------------------------------------------------------------

@dataclass
class Placement:
    """Where each agent row belongs, and what was wrong with the ones that did not."""

    by_ordinal: Dict[int, List[str]] = field(default_factory=dict)
    unnumbered: int = 0
    out_of_range: int = 0
    duplicated: int = 0
    foreign_file: int = 0
    duplicate_examples: List[int] = field(default_factory=list)

    @property
    def placed(self) -> int:
        return len(self.by_ordinal)

    @property
    def rejected(self) -> int:
        return self.unnumbered + self.out_of_range + self.duplicated + self.foreign_file


@dataclass
class Verification:
    """The content check on the row match."""

    pairs: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def compared(self) -> int:
        return sum(pair["compared"] for pair in self.pairs)

    @property
    def disagreed(self) -> int:
        return sum(pair["disagreed"] for pair in self.pairs)

    @property
    def established(self) -> bool:
        """Whether any one key agreed on enough rows to settle the alignment."""
        return any(pair["compared"] >= CROSS_CHECK_MINIMUM and not pair["disagreed"]
                   for pair in self.pairs)

    def worst(self) -> Optional[Dict[str, Any]]:
        failures = [pair for pair in self.pairs if pair["disagreed"]]
        if not failures:
            return None
        return max(failures, key=lambda pair: pair["disagreed"])


class RowLedger:
    """Which input row each agent row describes, established and then proved.

    Two things happen here, and the difference between them matters. Placement
    reads the row number Agent 1 recorded and works out which input row it points
    at, rejecting anything that cannot be read as a position in this file.
    Verification then checks that answer against business keys that were carried
    through untouched, so a placement that is arithmetically fine but wrong about
    which table it is describing is caught rather than published.

    Verification is the check worth having. A misaligned join is the failure that
    does not look like one: every row is populated, every count is right, and
    every line is describing a different purchase than the one next to it.
    """

    def __init__(self, input_path: Path, input_header: Sequence[str],
                 input_rows: int) -> None:
        self.input_path = input_path
        self.input_header = list(input_header)
        self.input_rows = input_rows

    def place(self, header: Sequence[str], rows: Sequence[Sequence[str]]) -> Placement:
        """Work out which input row each agent row belongs to."""
        placement = Placement()
        index = {name: position for position, name in enumerate(header)}
        number_at = index.get(ROW_NUMBER_COLUMN)
        file_at = index.get(SOURCE_FILE_COLUMN)

        if number_at is None:
            LOGGER.error("The agent output has no %s column, so its rows cannot be "
                         "matched to input rows.", ROW_NUMBER_COLUMN)
            placement.unnumbered = len(rows)
            return placement

        for row in rows:
            if number_at >= len(row):
                placement.unnumbered += 1
                continue

            # A row Agent 1 attributes to a different file is not a row of this
            # input. It cannot arise from a single-file run, and if it ever does
            # it means a stale output slipped past the reuse check, so it is
            # counted and refused rather than placed on a position it shares.
            if file_at is not None and file_at < len(row):
                stated = normalise(row[file_at])
                if stated and stated != normalise(self.input_path.name):
                    placement.foreign_file += 1
                    continue

            raw = (row[number_at] or "").strip()
            try:
                number = int(float(raw))
            except (TypeError, ValueError):
                placement.unnumbered += 1
                continue

            ordinal = number - HEADER_ROWS
            if ordinal < 0 or ordinal >= self.input_rows:
                placement.out_of_range += 1
                continue
            if ordinal in placement.by_ordinal:
                placement.duplicated += 1
                if len(placement.duplicate_examples) < 10:
                    placement.duplicate_examples.append(number)
                continue
            placement.by_ordinal[ordinal] = list(row)

        return placement

    def verify(self, header: Sequence[str],
               placement: Placement) -> Verification:
        """Check the placement against fields that were carried through unchanged."""
        verification = Verification()
        index = {name: position for position, name in enumerate(header)}
        input_index = {name: position for position, name in enumerate(self.input_header)}

        for agent_column, candidates in CROSS_CHECKS:
            agent_at = index.get(agent_column)
            if agent_at is None:
                continue
            input_column = next((name for name in candidates if name in input_index), None)
            if input_column is None:
                continue
            verification.pairs.append({
                "agent_column": agent_column,
                "input_column": input_column,
                "compared": 0,
                "disagreed": 0,
                "examples": [],
            })

        if not verification.pairs:
            return verification

        # One pass over the input, checking every pair, because the input may be
        # large and is deliberately not held in memory.
        for ordinal, input_row in enumerate(stream_rows(self.input_path)):
            agent_row = placement.by_ordinal.get(ordinal)
            if agent_row is None:
                continue
            for pair in verification.pairs:
                agent_at = index[pair["agent_column"]]
                input_at = input_index[pair["input_column"]]
                if agent_at >= len(agent_row) or input_at >= len(input_row):
                    continue
                left = normalise(agent_row[agent_at])
                right = normalise(input_row[input_at])
                if not left or not right:
                    continue
                pair["compared"] += 1
                if left != right:
                    pair["disagreed"] += 1
                    if len(pair["examples"]) < 5:
                        pair["examples"].append({
                            "input_row": ordinal + HEADER_ROWS,
                            "agent_value": agent_row[agent_at],
                            "input_value": input_row[input_at],
                        })
        return verification


# ---------------------------------------------------------------------------
# Agent 4, attached to lines
# ---------------------------------------------------------------------------

class SupplierConsolidation:
    """Agent 4's supplier-level findings, ready to attach to a purchase line.

    Agent 4 answers a question about a supplier inside a comparison scope: given
    everything this supplier sold within this category, how much of it could be
    bought from someone already supplying it. There is no arithmetic that turns
    that into a fact about one purchase line, so nothing is invented here. The
    line is simply told what was found about its own supplier in its own
    category, which is the finding a buyer looking at that line would want.

    Both halves of the key are needed. A supplier trades in several categories
    and gets a separate verdict in each, so joining on the supplier alone would
    match several rows and there would be no honest way to choose between them.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.columns: List[str] = []
        self.primary_scope: str = ""
        self.by_supplier_and_scope: Dict[Tuple[str, str], List[str]] = {}
        self.key_by_name: Dict[str, str] = {}
        self.key_by_id: Dict[str, str] = {}
        self.rows_available = 0
        self.notes: List[str] = []

    def load(self) -> bool:
        """Read Agent 4's output. False if there is nothing to attach."""
        self._load_identities()
        self._load_scope()

        consolidation = self.folder / AGENT4_CONSOLIDATION_NAME
        if not consolidation.is_file():
            self.notes.append(f"{AGENT4_CONSOLIDATION_NAME} was not written.")
            return False

        header, rows = read_rows(consolidation)
        if not header:
            self.notes.append(f"{AGENT4_CONSOLIDATION_NAME} is empty.")
            return False

        index = {name: position for position, name in enumerate(header)}
        for column in (SUPPLIER_KEY_COLUMN, "Scope_Level", "Scope_Value"):
            if column not in index:
                self.notes.append(
                    f"{AGENT4_CONSOLIDATION_NAME} has no {column} column, so its "
                    f"rows cannot be attached to lines.")
                return False

        # The scope columns are the join key and are not repeated as data. The
        # supplier key is kept, because it is what joins a line to the full
        # per-scope detail in the companion file.
        self.columns = [name for name in header if name not in AGENT4_KEY_COLUMNS]

        scope_at = index["Scope_Level"]
        value_at = index["Scope_Value"]
        key_at = index[SUPPLIER_KEY_COLUMN]
        keep = [index[name] for name in self.columns]

        for row in rows:
            if max(scope_at, value_at, key_at) >= len(row):
                continue
            if self.primary_scope and row[scope_at] != self.primary_scope:
                continue
            padded = list(row) + [""] * (len(header) - len(row))
            pair = (normalise(padded[key_at]), normalise(padded[value_at]))
            # First wins, and the file is written in a stable order, so a
            # duplicated pair cannot make the output depend on run order.
            self.by_supplier_and_scope.setdefault(pair, [padded[at] for at in keep])

        self.rows_available = len(self.by_supplier_and_scope)
        if not self.rows_available:
            self.notes.append(
                "Agent 4 found no supplier whose portfolio overlapped another's "
                "enough to report, so its columns are present and empty.")
        return True

    def _load_scope(self) -> None:
        """Which scope level Agent 4 treated as primary, from its own manifest."""
        manifest = self.folder / AGENT4_MANIFEST_NAME
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                self.primary_scope = str(payload.get("primary_scope") or "")
            except (OSError, json.JSONDecodeError, TypeError):
                self.primary_scope = ""
        if not self.primary_scope:
            self.primary_scope = "Category_L2"
            self.notes.append(
                f"Agent 4's manifest did not state its primary scope, so "
                f"{self.primary_scope} is assumed.")

    def _load_identities(self) -> None:
        """Map the supplier spellings on a line onto Agent 4's supplier keys.

        Agent 4 resolves spelling variants and numbers to one key per supplier
        and publishes that decision in its master file. Reading it is what lets a
        line reach the finding about its supplier without this script having to
        guess at name matching, which is Agent 4's job and not a job worth doing
        twice with different answers.
        """
        master = self.folder / AGENT4_MASTER_NAME
        if not master.is_file():
            self.notes.append(f"{AGENT4_MASTER_NAME} was not written, so lines are "
                              f"matched to suppliers by name alone.")
            return
        header, rows = read_rows(master)
        index = {name: position for position, name in enumerate(header)}
        key_at = index.get(SUPPLIER_KEY_COLUMN)
        if key_at is None:
            return
        for row in rows:
            if key_at >= len(row):
                continue
            key = row[key_at]
            for column in ("Canonical_Supplier_Name", "Raw_Name_Variants"):
                at = index.get(column)
                if at is None or at >= len(row):
                    continue
                for variant in str(row[at]).split(";"):
                    name = normalise(variant)
                    if name:
                        self.key_by_name.setdefault(name, key)
            at = index.get("Supplier_Ids")
            if at is not None and at < len(row):
                for identifier in str(row[at]).split(";"):
                    value = normalise(identifier)
                    if value:
                        self.key_by_id.setdefault(value, key)

    def key_for(self, supplier_name: str, supplier_id: str) -> str:
        """Agent 4's key for the supplier on a line, if it resolved one.

        The number is tried first. It is the field an ERP joins on, and it
        survives the spelling differences between systems that are the whole
        reason Agent 4 keeps a master in the first place.
        """
        identifier = normalise(supplier_id)
        if identifier and identifier in self.key_by_id:
            return self.key_by_id[identifier]
        name = normalise(supplier_name)
        if name and name in self.key_by_name:
            return self.key_by_name[name]
        return ""

    def values_for(self, supplier_key: str, scope_value: str) -> Optional[List[str]]:
        if not supplier_key:
            return None
        return self.by_supplier_and_scope.get(
            (normalise(supplier_key), normalise(scope_value)))


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------

@dataclass
class ColumnPlan:
    """Which output column each value goes to, and where it comes from."""

    header: List[str] = field(default_factory=list)

    # Output position -> position in the line-level agent row.
    from_line_agent: List[Tuple[int, int]] = field(default_factory=list)
    # Output position -> position in the Agent 4 value list.
    from_agent4: List[Tuple[int, int]] = field(default_factory=list)

    renamed: Dict[str, str] = field(default_factory=dict)
    by_agent: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class MergeReport:
    """What the merge did, in the terms the run summary reports."""

    rows_in: int = 0
    rows_out: int = 0
    line_annotated: int = 0
    agent4_annotated: int = 0
    supplier_key_found: int = 0
    missing_line: List[int] = field(default_factory=list)
    missing_agent4: List[int] = field(default_factory=list)
    missing_line_total: int = 0
    missing_agent4_total: int = 0


class Merger:
    """Writes the input back out with the agents' columns added.

    The output is written by walking the input. That is the whole design: a row
    exists in the output because it existed in the input, and an agent's answer
    is looked up and attached, or is not found and the columns are left empty.
    There is no path through this code that can drop an input row, and the row
    counts are compared at the end so that a future one would be caught.
    """

    def __init__(self, settings: Settings, input_choice: InputChoice,
                 input_header: Sequence[str]) -> None:
        self.settings = settings
        self.input = input_choice
        self.input_header = list(input_header)
        self.report = MergeReport(rows_in=input_choice.rows)

    # -- planning the header -------------------------------------------------

    def plan(self, line_header: Sequence[str], line_steps: Sequence[AgentStep],
             consolidation: Optional[SupplierConsolidation]) -> ColumnPlan:
        """Decide the output columns before writing any of them.

        The input's columns come first and keep their names and their values. An
        agent column whose name the input already uses is added under a prefixed
        name, so that both survive and neither has to be judged against the
        other: Max reports the source system's own spelling of ``Country`` and
        Agent 1 reports the country it resolved by Fortum's definition, and a
        reader comparing them is doing something reasonable.
        """
        plan = ColumnPlan(header=list(self.input_header))
        taken = set(plan.header)

        # Which agent first produced each line-level column, so the manifest can
        # report the three agents separately even though they arrive in one file.
        origin: Dict[str, str] = {}
        for step in line_steps:
            label = step.name.split(" - ")[0]
            for column in step.columns:
                origin.setdefault(column, label)

        for position, column in enumerate(line_header):
            label = origin.get(column, "Agents 1 to 3")
            name = column
            if column in taken:
                prefix = re.sub(r"[^A-Za-z0-9]", "", label)
                name = unique_name(f"{prefix}_{column}", taken)
                plan.renamed[column] = name
            taken.add(name)
            plan.from_line_agent.append((len(plan.header), position))
            plan.header.append(name)
            plan.by_agent.setdefault(label, []).append(name)

        if consolidation and consolidation.columns:
            for position, column in enumerate(consolidation.columns):
                name = unique_name(f"{AGENT4_PREFIX}{column}", taken)
                taken.add(name)
                plan.from_agent4.append((len(plan.header), position))
                plan.header.append(name)
                plan.by_agent.setdefault("Agent 4", []).append(name)

        return plan

    # -- writing -------------------------------------------------------------

    def write(self, plan: ColumnPlan, placement: Placement,
              line_header: Sequence[str],
              consolidation: Optional[SupplierConsolidation]) -> MergeReport:
        """Write the dataset, one output row per input row, in input order."""
        self.settings.results_dir.mkdir(parents=True, exist_ok=True)
        csv_path = self.settings.results_dir / OUTPUT_CSV
        jsonl_path = self.settings.results_dir / OUTPUT_JSONL

        line_index = {name: position for position, name in enumerate(line_header)}
        supplier_name_at = line_index.get(LINE_SUPPLIER_NAME)
        supplier_id_at = line_index.get(LINE_SUPPLIER_ID)
        scope_at = (line_index.get(consolidation.primary_scope)
                    if consolidation else None)
        agent4_width = len(plan.from_agent4)

        # The supplier key is attached from the Agent 4 block where a verdict was
        # found. Where there is no verdict the line still has a resolved supplier
        # identity worth publishing, so the column is filled separately.
        key_output_at = None
        if consolidation:
            for output_at, source_at in plan.from_agent4:
                if consolidation.columns[source_at] == SUPPLIER_KEY_COLUMN:
                    key_output_at = output_at
                    break

        handle_jsonl = None
        try:
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(plan.header)
                if self.settings.write_jsonl:
                    handle_jsonl = jsonl_path.open("w", encoding="utf-8")

                width = len(plan.header)
                input_width = len(self.input_header)

                for ordinal, input_row in enumerate(stream_rows(self.input.path)):
                    output: List[str] = [""] * width

                    # A short row is a ragged source line, not a missing row, and
                    # is padded rather than skipped.
                    for position in range(input_width):
                        output[position] = (input_row[position]
                                            if position < len(input_row) else "")

                    agent_row = placement.by_ordinal.get(ordinal)
                    if agent_row is not None:
                        self.report.line_annotated += 1
                        for output_at, source_at in plan.from_line_agent:
                            if source_at < len(agent_row):
                                output[output_at] = agent_row[source_at]
                    else:
                        self.report.missing_line_total += 1
                        if len(self.report.missing_line) < AUDIT_ROW_LIMIT:
                            self.report.missing_line.append(ordinal)

                    if consolidation and agent4_width:
                        self._attach_agent4(output, agent_row, ordinal, consolidation,
                                            plan, supplier_name_at, supplier_id_at,
                                            scope_at, key_output_at)

                    writer.writerow(output)
                    self.report.rows_out += 1
                    if handle_jsonl is not None:
                        handle_jsonl.write(json.dumps(
                            dict(zip(plan.header, output)), ensure_ascii=False) + "\n")
        finally:
            if handle_jsonl is not None:
                handle_jsonl.close()

        return self.report

    def _attach_agent4(self, output: List[str], agent_row: Optional[Sequence[str]],
                       ordinal: int, consolidation: SupplierConsolidation,
                       plan: ColumnPlan, supplier_name_at: Optional[int],
                       supplier_id_at: Optional[int], scope_at: Optional[int],
                       key_output_at: Optional[int]) -> None:
        """Give one line what Agent 4 found about its supplier in its category."""
        if agent_row is None:
            self.report.missing_agent4_total += 1
            if len(self.report.missing_agent4) < AUDIT_ROW_LIMIT:
                self.report.missing_agent4.append(ordinal)
            return

        def cell(position: Optional[int]) -> str:
            if position is None or position >= len(agent_row):
                return ""
            return agent_row[position]

        supplier_key = consolidation.key_for(cell(supplier_name_at),
                                             cell(supplier_id_at))
        if supplier_key:
            self.report.supplier_key_found += 1
            if key_output_at is not None:
                output[key_output_at] = supplier_key

        values = consolidation.values_for(supplier_key, cell(scope_at))
        if values is None:
            self.report.missing_agent4_total += 1
            if len(self.report.missing_agent4) < AUDIT_ROW_LIMIT:
                self.report.missing_agent4.append(ordinal)
            return

        self.report.agent4_annotated += 1
        for output_at, source_at in plan.from_agent4:
            if source_at < len(values):
                output[output_at] = values[source_at]

    # -- the audit -----------------------------------------------------------

    def write_audit(self) -> Optional[Path]:
        """Name the rows that did not get an answer they should have got.

        Written only when there are some, so a clean run does not leave a file
        behind implying otherwise.

        A row with no Agent 4 finding is only listed when Agent 4 produced
        findings for other rows. Where it found no supplier overlap anywhere -
        the honest answer on a small extract - every row is equally without one,
        and naming all of them would turn a legitimate result into a page of
        exceptions that hides the case where three rows genuinely failed.
        """
        line_missing = set(self.report.missing_line)
        agent4_missing = (set(self.report.missing_agent4)
                          if self.report.agent4_annotated else set())
        gaps = sorted(line_missing | agent4_missing)
        if not gaps:
            return None

        path = self.settings.results_dir / OUTPUT_AUDIT
        truncated = (self.report.missing_line_total > AUDIT_ROW_LIMIT
                     or self.report.missing_agent4_total > AUDIT_ROW_LIMIT)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            if truncated:
                # Written as a comment row rather than left implicit, so nobody
                # counts the lines in this file and reports that as the number of
                # rows affected.
                writer.writerow([
                    f"# the first {AUDIT_ROW_LIMIT:,} affected row(s) only; "
                    f"{self.report.missing_line_total:,} row(s) went unannotated and "
                    f"{self.report.missing_agent4_total:,} had no Agent 4 finding. "
                    f"See {OUTPUT_MANIFEST} for the totals."])
            writer.writerow(["Input_Row_Number", "Input_Row_Ordinal",
                             "Agents_1_To_3", "Agent_4", "Reason"])
            for ordinal in gaps:
                no_line = ordinal in line_missing
                reason = ("no agent read this row" if no_line
                          else "no consolidation finding for this supplier and category")
                writer.writerow([ordinal + HEADER_ROWS, ordinal,
                                 "missing" if no_line else "present",
                                 "missing" if ordinal in agent4_missing else "present",
                                 reason])
        return path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

class Runner:
    """Puts the pieces in order and reports what happened."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.run_id = hashlib.sha256(
            f"{time.time()}{os.getpid()}".encode("utf-8")).hexdigest()[:16]
        self.outputs: List[str] = []
        self.statistics: Dict[str, Any] = {}
        self.warnings: List[str] = []
        self.input: Optional[InputChoice] = None
        self.input_header: List[str] = []
        self.chain: Optional[AgentChain] = None
        self.plan: Optional[ColumnPlan] = None
        self.report: Optional[MergeReport] = None
        self.placement: Optional[Placement] = None
        self.verification: Optional[Verification] = None
        self.consolidation: Optional[SupplierConsolidation] = None
        self.catalogue: Dict[str, Any] = {}
        self.spend: Dict[str, Any] = {}

        # Column -> cells put back after a later agent blanked what an earlier one
        # produced. Reported rather than done quietly, because it is a repair.
        self.restored: Dict[str, int] = {}

    def _resume_or_restart(self, input_path: Path) -> None:
        """Deal with a run that was interrupted, before anything reuses its work.

        Resuming is the default because the work already done is expensive -
        many minutes of it on the client's catalogue - and because the reuse rules
        make it safe: an agent's result is reused only where a rerun would
        reproduce it. What is not safe is the output of the agent that was in
        flight, which may have been half written when the run stopped, so that one
        is deleted rather than trusted.
        """
        journal = RunJournal(self.settings.work_dir)
        state = journal.previous()
        if not state:
            return

        finished = journal.finished_steps(state)
        unfinished = journal.unfinished_steps(state)
        when = state.get("updated") or state.get("started") or "an earlier run"

        print()
        print("  A previous run did not finish.")
        print(f"    Started              : {state.get('started', 'unknown')}")
        print(f"    Last progress        : {when}")
        print(f"    Input                : {state.get('input', 'unknown')}")
        if finished:
            print(f"    Finished             : {', '.join(sorted(finished))}")
        if unfinished:
            print(f"    Interrupted part way : {', '.join(sorted(unfinished))}")

        # A different input makes the question moot: none of the old results match
        # it, so every agent would rerun whatever is chosen here.
        if state.get("input_digest") and state["input_digest"] != digest_of(input_path):
            print("    The input has changed since then, so none of that work can be "
                  "reused and every agent runs again.")
            journal.clear()
            self.warnings.append(
                "A previous run was interrupted, but the input has changed since, so "
                "nothing from it could be reused.")
            return

        resume = True
        if self.settings.interactive:
            print()
            resume = ask_yes_no("Continue from where it stopped rather than starting "
                                "again", True)
        else:
            print("    Continuing from where it stopped. Use --force to start again.")

        if not resume:
            self.settings.force = True
            print("    Starting again. Every agent will run from the beginning.")
            journal.clear()
            return

        # The interrupted agent's output cannot be trusted, whether or not it looks
        # complete, so it goes. Everything else stands or falls on the reuse rules.
        for name in unfinished:
            for artefact in AGENT_ARTEFACTS.get(name, ()):
                partial = self.settings.work_dir / artefact
                if partial.is_file():
                    try:
                        partial.unlink()
                        if not artefact.startswith("."):
                            print(f"    Discarded {artefact}, which {name} may not "
                                  f"have finished writing.")
                    except OSError as error:
                        LOGGER.warning("Could not remove the part-written %s (%s). "
                                       "Delete it by hand before trusting this run.",
                                       artefact, error)
        if finished:
            self.warnings.append(
                f"This run resumed an interrupted one from {when}, reusing "
                f"{len(finished)} agent result(s) rather than recomputing them.")
        print()

    def run(self) -> int:
        started = time.time()

        self.input = InputResolver(self.settings).resolve()
        self.warnings.extend(self.input.warnings)
        for note in self.input.warnings:
            LOGGER.warning(note)
        if not self.input.rows:
            raise SystemExit(f"{self.input.path} has no data rows, so there is "
                             f"nothing to run the agents over.")
        LOGGER.info("Input: %s (%s row(s)), %s.", self.input.path.name,
                    f"{self.input.rows:,}", self.input.origin)
        self.input_header = read_header(self.input.path)
        input_header = self.input_header

        self._resume_or_restart(self.input.path)

        self.chain = AgentChain(self.settings)
        print(f"\n  Model tier           : "
              f"{'on' if self.settings.use_llm else 'off, local stack only'}"
              + (f", alert at ${self.settings.llm_spend_limit:,.2f}"
                 if self.settings.use_llm and self.settings.llm_spend_limit else ""))
        print("  Running the agents. Their own logs follow, tagged by agent.\n",
              flush=True)
        self.chain.run(self.input.path)

        self.catalogue = self.chain.catalogue_facts()
        for note in self.chain.catalogue_warnings(self.catalogue):
            LOGGER.warning(note)
            self.warnings.append(note)
        self.warnings.extend(self.chain.catalogue.warnings)
        self.spend = self.chain.spend()

        last = self.chain.last_line_output()
        if last is None or not last.output_path:
            failed = [step for step in self.chain.steps if not step.ok]
            reason = failed[0].reason if failed else "no agent produced a result"
            raise SystemExit(
                f"No agent annotated the purchase lines, so the output would be "
                f"the input unchanged.\nAgent 1 failed: {reason}\n"
                f"Check {self.settings.work_dir} for its log.")

        line_header, line_rows = read_rows(last.output_path)

        # The chain does not always carry a value as far as it carries the header
        # it belongs under, so anything an earlier agent filled and a later one
        # blanked is put back before the merge reads it.
        self.restored = restore_dropped_values(
            line_header, line_rows,
            [step.output_path for step in self.chain.earlier_line_outputs()
             if step.output_path])
        for column, count in sorted(self.restored.items()):
            LOGGER.info("Restored %d %s value(s) that the agent chain dropped "
                        "after they were produced.", count, column)

        ledger = RowLedger(self.input.path, input_header, self.input.rows)
        self.placement = ledger.place(line_header, line_rows)
        self.verification = ledger.verify(line_header, self.placement)
        self._judge_alignment(last)

        agent4_step = self.chain.agent4_step()
        if agent4_step and agent4_step.ok and agent4_step.output_path:
            self.consolidation = SupplierConsolidation(agent4_step.output_path.parent)
            if not self.consolidation.load():
                for note in self.consolidation.notes:
                    LOGGER.warning(note)
                self.warnings.extend(self.consolidation.notes)
                self.consolidation = None
            else:
                for note in self.consolidation.notes:
                    LOGGER.info(note)
        elif agent4_step and agent4_step.reason:
            self.warnings.append(f"Agent 4 did not run: {agent4_step.reason}. Its "
                                 f"columns are absent from the output.")

        merger = Merger(self.settings, self.input, input_header)
        self.plan = merger.plan(line_header, self.chain.line_steps(),
                                self.consolidation)
        self.report = merger.write(self.plan, self.placement, line_header,
                                  self.consolidation)
        self.outputs.append(OUTPUT_CSV)
        if self.settings.write_jsonl:
            self.outputs.append(OUTPUT_JSONL)

        audit = merger.write_audit()
        if audit:
            self.outputs.append(audit.name)

        self._check_row_count()
        self.statistics = self._collect(time.time() - started)
        self._write_manifest()

        # Last, and only here. The journal's absence is what says the run finished,
        # so it is removed once the dataset is written and has passed its checks -
        # not when the agents stop, and never on a path that raises.
        self.chain.journal.clear()
        return 0

    # -- the two hard checks -------------------------------------------------

    def _refuse(self, problem: str) -> None:
        """Stop before writing anything, and say what is now stale.

        A dataset from an earlier run sits in the results folder under the name
        this run would have used. Saying only that nothing has been written would
        be true and misleading at once, because the file someone opens afterwards
        is the one that is there.
        """
        existing = self.settings.results_dir / OUTPUT_CSV
        stale = ""
        if existing.is_file():
            when = describe_file(existing).get("modified", "")
            stale = (f"\n  {OUTPUT_CSV} in the results folder is from an earlier run"
                     f"{f' ({when})' if when else ''} and does not reflect this one. "
                     f"Do not use it.")
        raise SystemExit(f"{problem}\n"
                         f"  Nothing has been written by this run.{stale}\n"
                         f"  Rerun with --force to discard every cached agent result.")

    def _judge_alignment(self, step: AgentStep) -> None:
        """Stop the run if the rows may not line up.

        A disagreement on a carried-through business key means the agent output
        is being matched to the wrong input rows. Every column would be populated
        and every count would be right, so nothing downstream would notice; it is
        refused here or not at all.
        """
        placement = self.placement
        verification = self.verification
        assert placement is not None and verification is not None

        worst = verification.worst()
        if worst:
            examples = "\n".join(
                f"    input row {case['input_row']}: "
                f"{step.output_name} says {case['agent_value']!r}, "
                f"the input says {case['input_value']!r}"
                for case in worst["examples"])
            self._refuse(
                f"The agent output does not line up with the input.\n"
                f"  {worst['agent_column']} and the input's {worst['input_column']!r} "
                f"disagree on {worst['disagreed']:,} of the {worst['compared']:,} "
                f"row(s) where both are populated.\n{examples}\n"
                f"  This means {step.output_name} was produced from a different "
                f"table than {self.input.path.name}.")

        if placement.duplicated:
            examples = ", ".join(str(number) for number in placement.duplicate_examples)
            self._refuse(
                f"{placement.duplicated:,} agent row(s) claim a source row that "
                f"another row already claimed"
                f"{f' (rows {examples})' if examples else ''}.\n"
                f"  One input row cannot be annotated twice, and choosing between "
                f"the claims would be arbitrary.")

        if placement.foreign_file:
            self._refuse(
                f"{placement.foreign_file:,} agent row(s) name a source file other "
                f"than {self.input.path.name}, so {step.output_name} was produced "
                f"from a different table.")

        if not verification.established:
            # Not fatal. A synthetic extract may populate none of the keys, and
            # refusing to write anything would be worse than saying so.
            reason = ("none of the business keys used to check the match is "
                      "populated on both sides"
                      if not verification.pairs or not verification.compared
                      else f"only {verification.compared:,} value(s) could be "
                           f"compared, fewer than the {CROSS_CHECK_MINIMUM} wanted")
            note = (f"The row match rests on {ROW_NUMBER_COLUMN} alone: {reason}. "
                    f"It is very probably right, but it has not been proved.")
            LOGGER.warning(note)
            self.warnings.append(note)
        else:
            LOGGER.info("Row match confirmed against %s business-key value(s), with "
                        "no disagreement.", f"{verification.compared:,}")

        if placement.out_of_range or placement.unnumbered:
            note = (f"{placement.out_of_range + placement.unnumbered:,} agent row(s) "
                    f"could not be attributed to an input row and were not used.")
            LOGGER.warning(note)
            self.warnings.append(note)

    def _check_row_count(self) -> None:
        """The output has one row per input row. Anything else is a defect."""
        report = self.report
        assert report is not None
        if report.rows_out == report.rows_in:
            LOGGER.info("Wrote %s row(s), one per input row.", f"{report.rows_out:,}")
            return
        raise SystemExit(
            f"The output has {report.rows_out:,} row(s) but the input had "
            f"{report.rows_in:,}.\n"
            f"  This is a defect in {Path(__file__).name}, not in the data: the "
            f"output is written by walking the input, so the two cannot differ.\n"
            f"  {OUTPUT_CSV} should not be used.")

    # -- reporting -----------------------------------------------------------

    def _collect(self, seconds: float) -> Dict[str, Any]:
        report = self.report
        placement = self.placement
        verification = self.verification
        assert report and placement and verification and self.plan and self.input

        statistics: Dict[str, Any] = {
            "rows_in": report.rows_in,
            "rows_out": report.rows_out,
            "columns_in": len(self.input_header),
            "columns_out": len(self.plan.header),
            "columns_added": len(self.plan.header) - len(self.input_header),
            "rows_annotated_agents_1_to_3": report.line_annotated,
            "rows_missing_agents_1_to_3": report.missing_line_total,
            "rows_with_agent4_finding": report.agent4_annotated,
            "rows_without_agent4_finding": report.missing_agent4_total,
            "rows_with_supplier_key": report.supplier_key_found,
            "agent_rows_not_attributed": placement.rejected,
            "cross_check_values_compared": verification.compared,
            "cross_check_disagreements": verification.disagreed,
            "seconds": round(seconds, 1),
        }
        if self.consolidation:
            statistics["agent4_supplier_scope_findings"] = self.consolidation.rows_available
            statistics["agent4_primary_scope"] = self.consolidation.primary_scope
        if self.catalogue:
            statistics["catalogue_items_loaded"] = self.catalogue.get("items", 0)
            statistics["rows_matched_to_catalogue"] = self.catalogue.get("rows_with_match", 0)
            statistics["rows_already_standard"] = self.catalogue.get(
                "rows_already_standard", 0)
        if self.spend:
            statistics["model_spend_this_run_usd"] = self.spend["this_run_usd"]
            statistics["model_requests_this_run"] = self.spend["requests_this_run"]
        return statistics

    def _write_manifest(self) -> None:
        assert self.input and self.chain and self.plan and self.verification
        self.outputs.append(OUTPUT_MANIFEST)
        manifest = {
            "agent": AGENT_NAME,
            "version": AGENT_VERSION,
            "run_id": self.run_id,
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "python": sys.version.split()[0],
            "input": {
                "origin": self.input.origin,
                **self.input.detail,
            },
            "agents": [
                {
                    "name": step.name,
                    "script": step.script,
                    "status": step.status,
                    "reason": step.reason,
                    "input": step.input_path.name if step.input_path else "",
                    "output": step.output_path.name if step.output_path else "",
                    "rows": step.rows,
                    "columns": len(step.columns),
                    "seconds": round(step.seconds, 1),
                    "log": step.log_path.name if step.log_path else "",
                }
                for step in self.chain.steps
            ],
            "catalogue": {**self.chain.catalogue.describe(), **self.catalogue},
            "model_spend": self.spend,
            "columns_by_agent": {name: columns
                                 for name, columns in sorted(self.plan.by_agent.items())},
            "columns_renamed_to_protect_the_input": dict(sorted(self.plan.renamed.items())),
            "values_restored_after_the_chain_dropped_them": dict(sorted(self.restored.items())),
            "row_match": {
                "key": ROW_NUMBER_COLUMN,
                "checked_against": [
                    {
                        "agent_column": pair["agent_column"],
                        "input_column": pair["input_column"],
                        "compared": pair["compared"],
                        "disagreed": pair["disagreed"],
                    }
                    for pair in self.verification.pairs
                ],
                "proved": self.verification.established,
            },
            "statistics": self.statistics,
            "warnings": self.warnings,
            "outputs": sorted(self.outputs),
            "working_folder": str(self.settings.work_dir),
        }
        path = self.settings.results_dir / OUTPUT_MANIFEST
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                        encoding="utf-8")


def print_summary(runner: Runner, settings: Settings) -> None:
    """Say what was produced, what was reused, and what is missing from it."""
    statistics = runner.statistics
    report = runner.report
    assert report is not None and runner.input is not None

    print("\n" + "=" * 79)
    print(f"{AGENT_NAME} - complete")
    print("=" * 79)
    print(f"  Run id               : {runner.run_id}")
    print(f"  Input                : {runner.input.path.name} "
          f"({statistics['rows_in']:,} row(s), {statistics['columns_in']:,} column(s))")
    print(f"    {runner.input.origin}")
    print(f"  Output               : {statistics['rows_out']:,} row(s), "
          f"{statistics['columns_out']:,} column(s) "
          f"({statistics['columns_added']:,} added)")

    if statistics["rows_out"] == statistics["rows_in"]:
        print("    Every input row was written exactly once.")

    print("\n  Agents")
    for step in runner.chain.steps if runner.chain else []:
        mark = {"reused": "reused", "ran": "ran   ", "failed": "FAILED"}[step.status]
        detail = f"{step.rows:,} row(s)" if step.ok else step.reason
        timing = f"  {human_seconds(step.seconds)}" if step.seconds >= 1 else ""
        print(f"    {mark}  {step.name:<38} {detail}{timing}")
        if step.reused:
            print(f"            {step.reason}")

    catalogue = runner.catalogue
    if runner.chain and runner.chain.catalogue.arguments:
        print("\n  Catalogue Agent 3 matched against")
        print(f"    {runner.chain.catalogue.origin}")
        for entry in catalogue.get("sources") or []:
            print(f"    {entry.get('file', '')}: {entry.get('items', 0):,} item(s), "
                  f"modified {entry.get('modified') or 'unknown'}, "
                  f"sha256 {entry.get('sha256') or 'unknown'}")
        if catalogue:
            print(f"    {catalogue.get('rows_with_match', 0):,} line(s) matched a "
                  f"catalogue item, {catalogue.get('rows_already_standard', 0):,} were "
                  f"already standard purchases")
            best, accept = catalogue.get("best_score"), catalogue.get("accept_threshold")
            if isinstance(best, (int, float)) and isinstance(accept, (int, float)):
                print(f"    best candidate scored {best:.3f}, acceptance threshold "
                      f"{accept:.2f}")
            if catalogue.get("adjudicated_by_model"):
                print(f"    {catalogue['adjudicated_by_model']:,} borderline match(es) "
                      f"put to the model")
        for entry in runner.chain.catalogue.alternatives:
            print(f"    not used: {entry.get('file')} in the {entry.get('location')}, "
                  f"{entry.get('bytes', 0):,} bytes")

    spend = runner.spend or {}
    if spend.get("per_agent"):
        print("\n  Language model")
        for entry in spend["per_agent"]:
            note = " (reused, spent on an earlier run)" if entry["reused"] else ""
            print(f"    {entry['agent']:<10} ${entry['estimated_cost_usd']:>8,.2f}  "
                  f"{entry['requests']:,} request(s), "
                  f"{entry['input_tokens']:,} in / {entry['output_tokens']:,} out"
                  f"{note}")
        print(f"    {'this run':<10} ${spend['this_run_usd']:>8,.2f}  "
              f"{spend['requests_this_run']:,} request(s), "
              f"{spend['cache_hits_this_run']:,} answer(s) served from cache")
        if spend["recorded_usd"] > spend["this_run_usd"]:
            print(f"    {'recorded':<10} ${spend['recorded_usd']:>8,.2f}  including "
                  f"the reused agents' earlier spend")
        if spend["stopped_at_limit"]:
            print(f"    {', '.join(spend['stopped_at_limit'])} stopped calling the "
                  f"model at the spend limit and finished on the local stack.")
        print("    Estimated from the published rates, an upper bound rather than "
              "an invoice.")
    elif settings.use_llm:
        print("\n  Language model       : switched on, but no agent reported any "
              "spend.")
        print("    Either every answer came from the response cache, or the calls "
              "failed. The agent logs in the working folder say which.")

    print("\n  Coverage")
    annotated = statistics["rows_annotated_agents_1_to_3"]
    print(f"    Agents 1 to 3        : {annotated:,} of {statistics['rows_in']:,} row(s)")
    if statistics["rows_missing_agents_1_to_3"]:
        missing = statistics["rows_missing_agents_1_to_3"]
        print(f"      {missing:,} row(s) were not annotated and are written with "
              f"those columns empty.")
        if missing > AUDIT_ROW_LIMIT:
            print(f"      The first {AUDIT_ROW_LIMIT:,} are named in {OUTPUT_AUDIT}.")
        else:
            print(f"      They are named in {OUTPUT_AUDIT}.")
    if runner.consolidation:
        print(f"    Agent 4 finding      : "
              f"{statistics['rows_with_agent4_finding']:,} row(s)")
        print(f"    Supplier identified  : "
              f"{statistics['rows_with_supplier_key']:,} row(s)")
        findings = statistics.get("agent4_supplier_scope_findings", 0)
        if not findings:
            print("      Agent 4 found no supplier overlap worth reporting, so its "
                  "columns are empty throughout.")
        else:
            print(f"      {findings:,} supplier-and-category finding(s) at the "
                  f"{statistics.get('agent4_primary_scope', '')} level were available "
                  f"to attach.")

    if statistics["cross_check_values_compared"]:
        print(f"\n  Row match            : confirmed on "
              f"{statistics['cross_check_values_compared']:,} business-key value(s), "
              f"{statistics['cross_check_disagreements']:,} disagreement(s)")
    if statistics["agent_rows_not_attributed"]:
        print(f"    {statistics['agent_rows_not_attributed']:,} agent row(s) could "
              f"not be attributed to an input row.")

    if runner.plan and runner.plan.renamed:
        print(f"\n  Renamed to protect the input ({len(runner.plan.renamed)})")
        for original, renamed in sorted(runner.plan.renamed.items()):
            print(f"    {original} -> {renamed}")
        print("    The input's own column keeps its name and its value.")

    if runner.restored:
        print(f"\n  Put back after the chain dropped them ({len(runner.restored)})")
        for column, count in sorted(runner.restored.items()):
            print(f"    {column}: {count:,} value(s)")
        print("    A later agent republished the column and left it empty. The "
              "value an\n    earlier agent produced was read from its own output "
              "instead.")

    if runner.warnings:
        print("\n  Worth reading")
        for note in runner.warnings:
            print(f"    - {note}")

    print(f"\n  Output folder        : {settings.results_dir}")
    for name in sorted(set(runner.outputs)):
        print(f"    {name}")
    print(f"  Agent working files  : {settings.work_dir}")
    print(f"    the agents' own outputs and manifests, including Agent 4's "
          f"per-scope detail")
    print(f"  Elapsed              : {statistics['seconds']:.0f}s\n")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="all_agents.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run Agents 1 to 4 over the purchase table and return that "
                    "table with their columns added.",
        epilog="Every input row is written exactly once. Work already done is "
               "reused: Max's stage-3 file is taken as it stands, and an agent "
               "whose output was produced from the same input by the same script "
               "is not run again.")

    paths = parser.add_argument_group("paths")
    paths.add_argument("--input", metavar="FILE",
                      help="purchase table to widen (default: Max's stage-3 file "
                           "in the results folder, or build one from the extracts)")
    paths.add_argument("--sources", metavar="DIR",
                      help="folder holding the raw source extracts; naming it means "
                           "starting from them rather than from a table already built")
    paths.add_argument("--results", metavar="DIR", help="folder to write results into")
    paths.add_argument("--catalogues", metavar="PATH",
                      help="the client's item catalogue for Agent 3, a file or a "
                           "folder (default: the catalogue master in the source folder)")
    paths.add_argument("--lexicon", metavar="FILE", help="controlled vocabulary JSON file")
    paths.add_argument("--cache", metavar="DIR", help="folder for the model response cache")

    reuse = parser.add_argument_group("reuse")
    reuse.add_argument("--from-sources", action="store_true",
                       help="start from the raw extracts: join and interpret them, "
                            "then run the agents over the result (implied by --sources)")
    reuse.add_argument("--no-reuse", action="store_true",
                       help="ignore results already on disk and rebuild everything, "
                            "including Max's stage-3 file")
    reuse.add_argument("--force", action="store_true",
                       help="rerun all four agents but still read an existing "
                            "stage-3 file as the input")

    output = parser.add_argument_group("output")
    output.add_argument("--no-jsonl", action="store_true", help="skip the JSONL export")

    tiers = parser.add_argument_group("model")
    tiers.add_argument("--no-llm", action="store_true",
                       help="run the agents on their local stack only; they still "
                            "produce every column, with less resolved in it")
    tiers.add_argument("--use-llm", action="store_true",
                       help="allow the agents to call the language model (the default)")
    tiers.add_argument("--llm-spend-limit", metavar="USD", type=float, default=None,
                       help="alert each agent when its estimated spend reaches this")
    tiers.add_argument("--agent-timeout", metavar="SECONDS", type=int, default=None,
                       help="give up on an agent that runs longer than this; Agent 3 "
                            "needs many minutes on the client's full catalogue")

    speed = parser.add_argument_group("scheduling")
    speed.add_argument("--no-parallel", action="store_true",
                       help="run Agents 3 and 4 one after the other rather than "
                            "together, for a log that reads in order")

    parser.add_argument("--non-interactive", action="store_true",
                        help="take every default without prompting")
    parser.add_argument("--verbose", action="store_true", help="emit debug-level logging")
    parser.add_argument("--version", action="version",
                        version=f"{AGENT_NAME} {AGENT_VERSION}")
    return parser


def read_dotenv(path: Path) -> Dict[str, str]:
    """Read .env for the spend limit default, without exporting anything.

    The agents read their own endpoints and keys from the same file in their own
    processes. Nothing is put into the environment here.
    """
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def resolve_settings(args: argparse.Namespace) -> Settings:
    here = Path(__file__).resolve().parent
    env = read_dotenv(here / ".env")

    source_dir = Path(args.sources) if args.sources else here / "sources"
    results_dir = Path(args.results) if args.results else here / "results"
    lexicon_path = (Path(args.lexicon) if args.lexicon
                    else here / "lexicon" / "procurement_lexicon.json")
    cache_dir = Path(args.cache) if args.cache else here / "cache"
    catalogue_dir = Path(args.catalogues) if args.catalogues else None
    input_path = Path(args.input) if args.input else None

    spend_limit = args.llm_spend_limit
    if spend_limit is None and env.get("LLM_SPEND_LIMIT"):
        try:
            spend_limit = float(env["LLM_SPEND_LIMIT"])
        except ValueError:
            spend_limit = None

    reuse = not args.no_reuse

    # Naming a source folder is taken as asking for it to be read. The two flags
    # are separate so that the default folder can be used without naming it.
    from_sources = bool(args.from_sources or args.sources)
    if from_sources and input_path:
        raise SystemExit(
            "--input names a table to read and --sources/--from-sources asks for one "
            "to be built from the extracts. Choose one:\n"
            "  --input FILE      run the agents over that table\n"
            "  --from-sources    build the table from the extracts first")
    if args.no_reuse:
        from_sources = True

    # On unless declined. --use-llm is kept so that the command lines already in
    # use keep working, and because saying it out loud is harmless.
    use_llm = not args.no_llm

    if not args.non_interactive:
        print(BANNER)
        print("\nPress Enter to accept the value shown in brackets.\n")
        results_dir = Path(ask("Results folder", str(results_dir)))
        existing = next((results_dir / name for name in MAX_STAGE_FILES
                         if (results_dir / name).is_file()), None)
        if existing and not from_sources:
            print(f"\n  Found {existing.name} in the results folder, "
                  f"holding {count_rows(existing):,} row(s).")
            reuse = ask_yes_no("Use it as the input rather than starting from the "
                               "raw extracts", True)
            from_sources = not reuse
        if from_sources or not existing:
            source_dir = Path(ask("Source extracts folder", str(source_dir)))
        print()
        use_llm = ask_yes_no("Let the agents call the language model where they ask to",
                             use_llm)
        print()

    return Settings(
        source_dir=source_dir.expanduser().resolve(),
        results_dir=results_dir.expanduser().resolve(),
        lexicon_path=lexicon_path.expanduser().resolve(),
        cache_dir=cache_dir.expanduser().resolve(),
        input_path=input_path.expanduser().resolve() if input_path else None,
        catalogue_dir=catalogue_dir.expanduser().resolve() if catalogue_dir else None,
        reuse=reuse,
        force=args.force,
        from_sources=from_sources,
        use_llm=use_llm,
        llm_spend_limit=spend_limit,
        agent_timeout=args.agent_timeout,
        write_jsonl=not args.no_jsonl,
        parallel=not args.no_parallel,
        interactive=not args.non_interactive,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S")

    settings = resolve_settings(args)
    if args.non_interactive:
        print(BANNER)

    settings.results_dir.mkdir(parents=True, exist_ok=True)
    settings.work_dir.mkdir(parents=True, exist_ok=True)

    runner = Runner(settings)
    try:
        code = runner.run()
    except KeyboardInterrupt:
        report_interruption(runner, settings)
        return 130
    print_summary(runner, settings)
    return code


def report_interruption(runner: "Runner", settings: Settings) -> None:
    """Stop the agents, then say what survived and how to carry on.

    The old message said nothing had been written, which was wrong in the way that
    matters: the dataset had not been written, but the agents' own results had, and
    those are the expensive part. Someone told the run was wasted starts it again
    from the beginning and pays for those minutes twice.
    """
    print(flush=True)
    stopped = runner.chain.stop_live_agents() if runner.chain else []
    if stopped:
        print(f"  Stopped {', '.join(stopped)} part way through.")

    done = [step for step in (runner.chain.steps if runner.chain else []) if step.ok]
    print("\n" + "=" * 79)
    print("  Interrupted")
    print("=" * 79)
    if done:
        print("  Finished, and reused rather than recomputed next time:")
        for step in done:
            print(f"    {step.name:<38} {step.rows:,} row(s)"
                  + (f"  {human_seconds(step.seconds)}" if step.seconds >= 1 else ""))
    else:
        print("  No agent had finished, so there is nothing to carry over.")

    if stopped:
        print(f"\n  {', '.join(stopped)} did not finish. Whatever it had written is "
              f"discarded on the next run rather than reused, because a part-written "
              f"result can still look complete.")

    print(f"\n  {OUTPUT_CSV} was not written by this run, so anything of that name in "
          f"{settings.results_dir} is from an earlier one.")
    print("\n  Run the same command again to carry on from here: the agents above are "
          "reused")
    print("  and only the rest run"
          + (", after it asks you to confirm."
             if settings.interactive else " - it will not ask."))
    print("  Add --force instead to start again from the beginning.")

    spend = runner.chain.spend() if runner.chain else {}
    if spend.get("this_run_usd"):
        print(f"\n  Model spend before the interruption: "
              f"${spend['this_run_usd']:,.2f}. Reusing the finished agents does not "
              f"spend it again.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Reached only if the interruption arrives outside Runner.run, which is
        # where the interesting version of this message is written.
        print("\nInterrupted before the agents started. Nothing has changed.")
        sys.exit(130)
