#!/usr/bin/env python3
"""Fetch the local models Agents 1 to 4 run on, before a run needs them.

The agents use two kinds of model that live on this machine rather than behind
an API: one multilingual sentence embedder, shared by all four, and a set of
small Helsinki-NLP bilingual translators, one per source language.

Both are fetched from the Hugging Face hub on first use. That is fine on a
machine with a route to the hub and quietly expensive on one without, because a
translator that cannot be loaded is not an error: the agent falls back to the
language model instead, phrase by phrase, and pays for work the translator does
for nothing. One observed run sent 365,532 phrases to a hosted model for exactly
this reason, having failed to reach huggingface.co a few seconds earlier.

So this script exists to make that failure happen here, once, visibly, rather
than in the middle of a six-hour run.

Usage:
  python fetch_models.py
      Fetch the embedder and the translators for the languages in Fortum's
      extracts. Repeat runs are cheap: anything already cached is left alone.

  python fetch_models.py --check
      Report what is present and what is missing. Downloads nothing, and exits
      non-zero if anything the agents need is absent.

  python fetch_models.py --languages fi sv --results ./models
      Fetch a chosen subset into a named folder rather than the default cache.

  python fetch_models.py --all-languages --bundle models.tar.gz
      Fetch every language the agents support and write a single archive to
      carry to a machine that has no route to the hub at all.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tarfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

AGENT_NAME = "Fetch Models - the local stack Agents 1 to 4 run on"
AGENT_VERSION = "1.0.0"

LOGGER = logging.getLogger("fetch_models")

# The one embedding model every agent loads: semantic grouping in Agent 2,
# catalogue matching in Agent 3, portfolio comparison in Agent 4, and phrase
# similarity in Agent 1.
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

TRANSLATION_TEMPLATE = "Helsinki-NLP/opus-mt-{source}-en"

# Every language the agents will try to translate locally, kept in step with
# NeuralTranslator.SUPPORTED in agent1.py and agent3.py.
SUPPORTED_LANGUAGES: Tuple[str, ...] = (
    "fi", "sv", "pl", "de", "da", "no", "nl", "et", "fr", "es", "it", "cs",
)

# The languages Fortum's own extracts carry, which is what a normal run needs.
# Measured from a full run: Finnish dominates, then Swedish, then a long tail.
# Fetching only these keeps the download to a few gigabytes rather than eight.
FORTUM_LANGUAGES: Tuple[str, ...] = ("fi", "sv", "et", "no", "de", "pl")

# Roughly what one bilingual model costs on disk. The hub stores more than one
# weight format per repository, so the figure is larger than the model itself.
APPROXIMATE_MODEL_MB = 580


# ===========================================================================
# Reporting
# ===========================================================================

def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def human_bytes(count: int) -> str:
    """Bytes as something a person can compare at a glance."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit in {"B", "KB"} else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} GB"


def human_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def folder_bytes(path: Path) -> int:
    """Size on disk, counting each blob once.

    The hub keeps one copy of every file under blobs/ and links to it from each
    snapshot, so following the links counts the large weights twice and reports
    roughly double the true figure.
    """
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*")
               if item.is_file() and not item.is_symlink())


# ===========================================================================
# The cache
# ===========================================================================

def cache_root(results: Optional[Path]) -> Path:
    """Where the models will be written.

    Named explicitly rather than left to the library's default, because the
    whole point of this script is that the location can be inspected and
    copied. A folder given on the command line wins, then HF_HOME, then the
    library's own default.
    """
    if results is not None:
        return results.expanduser().resolve()
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]).expanduser().resolve()
    return Path.home() / ".cache" / "huggingface"


def repository_folder(root: Path, repository: str) -> Path:
    """The folder the hub uses for one repository id."""
    return root / "hub" / ("models--" + repository.replace("/", "--"))


def is_present(root: Path, repository: str) -> bool:
    """Whether a repository looks fetched rather than half-fetched.

    A snapshot folder holding at least one file is the test, because an
    interrupted download leaves the repository folder and its lock behind with
    no snapshot in it, and treating that as present is how a run ends up
    discovering the gap at the worst moment.
    """
    snapshots = repository_folder(root, repository) / "snapshots"
    if not snapshots.is_dir():
        return False
    return any(any(entry.rglob("*")) for entry in snapshots.iterdir() if entry.is_dir())


# ===========================================================================
# Fetching
# ===========================================================================

def load_embedder(repository: str) -> None:
    """Load the sentence embedder, which fetches it if it is not cached."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(repository)
    # Encoding one string proves the weights and the tokeniser both arrived,
    # which a bare constructor does not always establish.
    model.encode(["district heating meter"])


def load_translator(repository: str) -> None:
    """Load one bilingual translator and put a phrase through it.

    Loaded through AutoModelForSeq2SeqLM rather than pipeline("translation")
    for the same reason the agents do: transformers 5 removed that task name,
    and a check that passes only on 4.x is worse than no check.
    """
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    tokeniser = AutoTokenizer.from_pretrained(repository)
    model = AutoModelForSeq2SeqLM.from_pretrained(repository)
    model.eval()
    encoded = tokeniser(["testi"], return_tensors="pt", padding=True, truncation=True)
    model.generate(**encoded, max_length=32, num_beams=1, do_sample=False)


def fetch(repository: str, root: Path, loader, check_only: bool) -> Dict[str, object]:
    """Make one repository present, reporting what happened either way."""
    before = folder_bytes(repository_folder(root, repository))
    if is_present(root, repository):
        return {"repository": repository, "status": "present",
                "bytes": before, "seconds": 0.0}

    if check_only:
        return {"repository": repository, "status": "missing",
                "bytes": 0, "seconds": 0.0}

    LOGGER.info("Fetching %s ...", repository)
    started = time.time()
    try:
        loader(repository)
    except Exception as error:
        return {"repository": repository, "status": "failed",
                "bytes": 0, "seconds": time.time() - started,
                "reason": str(error).strip().splitlines()[0] if str(error) else
                          error.__class__.__name__}

    elapsed = time.time() - started
    size = folder_bytes(repository_folder(root, repository))
    LOGGER.info("  %s in %s", human_bytes(size), human_seconds(elapsed))
    return {"repository": repository, "status": "fetched",
            "bytes": size, "seconds": elapsed}


# ===========================================================================
# Bundling for a machine with no route to the hub
# ===========================================================================

def write_bundle(root: Path, path: Path, repositories: Sequence[str]) -> Optional[Path]:
    """Archive the fetched repositories so they can be carried to another host."""
    present = [name for name in repositories if is_present(root, name)]
    if not present:
        LOGGER.error("Nothing to bundle: none of the models are present.")
        return None

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Writing %s ...", path.name)
    with tarfile.open(path, "w:gz") as archive:
        for name in present:
            folder = repository_folder(root, name)
            # Stored relative to the cache root so the archive unpacks straight
            # over an HF_HOME on the far side with no path surgery.
            archive.add(folder, arcname=str(folder.relative_to(root)))
    return path


# ===========================================================================
# Environment
# ===========================================================================

def report_certificate_advice() -> None:
    """Say what to do about the corporate-proxy failure, which is the usual one."""
    print()
    print("  A network that re-signs HTTPS will refuse these downloads with a")
    print("  certificate error. Point Python at a bundle holding the proxy's root:")
    print()
    print("    security find-certificate -a -p /Library/Keychains/System.keychain "
          "> ~/roots.pem")
    print("    cat \"$(python -c 'import certifi; print(certifi.where())')\" "
          "~/roots.pem > ~/ca-bundle.pem")
    print("    export REQUESTS_CA_BUNDLE=~/ca-bundle.pem SSL_CERT_FILE=~/ca-bundle.pem")
    print()
    print("  Then run this script again. If the network blocks the hub outright,")
    print("  run it on a machine that can reach it with --bundle, and copy the")
    print("  archive across.")


def check_packages() -> List[str]:
    """Which required packages are missing, named rather than raised."""
    missing: List[str] = []
    for module, package in (("sentence_transformers", "sentence-transformers"),
                            ("transformers", "transformers"),
                            ("torch", "torch"),
                            ("sentencepiece", "sentencepiece")):
        try:
            __import__(module)
        except Exception:
            missing.append(package)
    return missing


# ===========================================================================
# Command line
# ===========================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch the local models Agents 1 to 4 run on.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:", 1)[1] if "Usage:" in __doc__ else None,
    )

    what = parser.add_argument_group("what to fetch")
    what.add_argument("--languages", nargs="+", metavar="CODE",
                      help="translator source languages to fetch (default: the "
                           f"languages in Fortum's extracts, "
                           f"{', '.join(FORTUM_LANGUAGES)})")
    what.add_argument("--all-languages", action="store_true",
                      help="every language the agents can translate locally, "
                           f"{len(SUPPORTED_LANGUAGES)} in all")
    what.add_argument("--no-embedder", action="store_true",
                      help="skip the sentence embedder, which all four agents need")
    what.add_argument("--no-translators", action="store_true",
                      help="skip the translators and fetch the embedder alone")

    where = parser.add_argument_group("where")
    where.add_argument("--results", metavar="DIR", type=Path,
                       help="folder to cache into, exported as HF_HOME for the "
                            "run (default: HF_HOME, or ~/.cache/huggingface)")
    where.add_argument("--bundle", metavar="FILE", type=Path,
                       help="also write a .tar.gz of the fetched models, to carry "
                            "to a machine with no route to the hub")

    how = parser.add_argument_group("how")
    how.add_argument("--check", action="store_true",
                     help="report what is present and missing, fetch nothing; "
                          "exits non-zero if anything is missing")
    how.add_argument("--verbose", action="store_true", help="debug-level logging")
    how.add_argument("--version", action="version",
                     version=f"{AGENT_NAME} {AGENT_VERSION}")
    return parser


def chosen_languages(args: argparse.Namespace) -> List[str]:
    if args.no_translators:
        return []
    if args.all_languages:
        return list(SUPPORTED_LANGUAGES)
    if args.languages:
        unknown = [code for code in args.languages if code not in SUPPORTED_LANGUAGES]
        if unknown:
            LOGGER.warning("No local translator for %s; the agents would send "
                           "those to the language model.", ", ".join(unknown))
        return [code for code in args.languages if code in SUPPORTED_LANGUAGES]
    return list(FORTUM_LANGUAGES)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    print()
    print("=" * 79)
    print(f"  {AGENT_NAME}")
    print("=" * 79)

    missing_packages = check_packages()
    if missing_packages and not args.check:
        LOGGER.error("Install these first: %s", ", ".join(missing_packages))
        LOGGER.error("  pip install -r requirements.txt")
        return 1

    root = cache_root(args.results)
    # Exported rather than merely computed, so the libraries write where this
    # script says they do and --results is not quietly ignored.
    os.environ["HF_HOME"] = str(root)
    root.mkdir(parents=True, exist_ok=True)

    languages = chosen_languages(args)
    repositories: List[Tuple[str, object]] = []
    if not args.no_embedder:
        repositories.append((EMBEDDING_MODEL, load_embedder))
    for code in languages:
        repositories.append((TRANSLATION_TEMPLATE.format(source=code), load_translator))

    if not repositories:
        LOGGER.error("Nothing selected to fetch.")
        return 1

    print(f"  Cache                : {root}")
    print(f"  Sentence embedder    : "
          f"{'skipped' if args.no_embedder else EMBEDDING_MODEL}")
    print(f"  Translators          : "
          f"{', '.join(languages) if languages else 'skipped'}")
    if not args.check:
        absent = [name for name, _ in repositories if not is_present(root, name)]
        if absent:
            print(f"  To fetch             : {len(absent)}, roughly "
                  f"{human_bytes(len(absent) * APPROXIMATE_MODEL_MB * 1024 * 1024)}")
    print()

    started = time.time()
    results = [fetch(name, root, loader, args.check) for name, loader in repositories]
    elapsed = time.time() - started

    fetched = [r for r in results if r["status"] == "fetched"]
    present = [r for r in results if r["status"] == "present"]
    absent = [r for r in results if r["status"] == "missing"]
    failed = [r for r in results if r["status"] == "failed"]

    print()
    print("-" * 79)
    print("  Models")
    print("-" * 79)
    for record in results:
        mark = {"fetched": "fetched", "present": "present", "missing": "MISSING",
                "failed": "FAILED "}[str(record["status"])]
        size = human_bytes(int(record["bytes"])) if record["bytes"] else ""
        print(f"    {mark}  {record['repository']:<62} {size}")
        if record.get("reason"):
            print(f"              {record['reason']}")

    total = sum(int(r["bytes"]) for r in results)
    print()
    print(f"  Cache                : {root}")
    print(f"  On disk              : {human_bytes(total)}")
    if fetched:
        print(f"  Fetched              : {len(fetched)} in {human_seconds(elapsed)}")
    if present:
        print(f"  Already present      : {len(present)}")

    if args.bundle and not args.check:
        archive = write_bundle(root, args.bundle,
                               [str(r["repository"]) for r in results])
        if archive:
            print(f"  Bundle               : {archive} "
                  f"({human_bytes(archive.stat().st_size)})")
            print()
            print("  On the machine that cannot reach the hub:")
            print(f"    mkdir -p ~/.cache/huggingface && tar -xzf {archive.name} "
                  "-C ~/.cache/huggingface")
            print("    export HF_HUB_OFFLINE=1")

    if failed:
        print()
        LOGGER.error("%d model(s) could not be fetched. The agents would send the "
                     "text these handle to the language model instead, which is "
                     "slower and not free.", len(failed))
        report_certificate_advice()
        return 1

    if absent:
        print()
        LOGGER.warning("%d model(s) are missing. Run without --check to fetch them.",
                       len(absent))
        return 1

    print()
    print("  Every model the agents need is present. Nothing in the chain will")
    print("  fall back to the language model for want of a local one.")
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        LOGGER.warning("Stopped. Partly fetched models are resumed rather than "
                       "restarted, so running again picks up where this left off.")
        sys.exit(130)
