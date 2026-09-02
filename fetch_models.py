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
import platform
import sys
import tarfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from runtime import use_system_trust_store

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

# Languages the template above names wrongly, kept in step with
# NeuralTranslator.MODEL_OVERRIDES. Norwegian is published only inside the
# North Germanic group model.
MODEL_OVERRIDES: Dict[str, str] = {"no": "Helsinki-NLP/opus-mt-gmq-en"}

# The languages Fortum's own extracts carry, which is what a normal run needs.
# Measured from a full run: Finnish dominates, then Swedish, then a long tail.
# Fetching only these keeps the download to a few gigabytes rather than eight.
FORTUM_LANGUAGES: Tuple[str, ...] = ("fi", "sv", "et", "no", "de", "pl")

# Roughly what one bilingual model costs on disk. The hub stores more than one
# weight format per repository, so the figure is larger than the model itself.
APPROXIMATE_MODEL_MB = 580


def translation_model(code: str) -> str:
    """The repository that actually holds the translator for one language."""
    return MODEL_OVERRIDES.get(code, TRANSLATION_TEMPLATE.format(source=code))


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


# Any weight file smaller than this is a pointer or a stub rather than a model.
# The smallest thing here is a bilingual translator at roughly 300 MB, so the
# bar is far below anything real and far above anything spurious.
MINIMUM_WEIGHT_BYTES = 1_000_000

# The names the hub uses for the weights themselves, as opposed to the small
# files that describe them.
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".msgpack", ".h5", ".ckpt")


def is_present(root: Path, repository: str) -> bool:
    """Whether a repository looks fetched rather than half-fetched.

    The test is that a snapshot holds real weights. Merely holding a file is not
    enough, and getting this wrong is worse than not checking at all: asking the
    hub for a model that does not exist still leaves a repository folder, a ref
    and a snapshot containing config.json behind, roughly eight kilobytes in
    all. A folder-shaped test calls that present. This script then reports a
    machine as ready, and the gap surfaces hours later as an agent quietly
    sending every foreign phrase to the paid tier instead.

    Sizes come from stat through the symlink, so the blob is measured rather
    than the link into it.
    """
    snapshots = repository_folder(root, repository) / "snapshots"
    if not snapshots.is_dir():
        return False

    for entry in snapshots.iterdir():
        if not entry.is_dir():
            continue
        for item in entry.rglob("*"):
            if item.suffix not in WEIGHT_SUFFIXES:
                continue
            try:
                if item.stat().st_size >= MINIMUM_WEIGHT_BYTES:
                    return True
            except OSError:
                continue  # a dangling link into a blob that was cleaned up
    return False


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

# The manual remedy, per platform, for a network that re-signs HTTPS. Only
# needed where truststore cannot be installed; it is offered second for that
# reason. Exporting the root differs per operating system, and the advice used
# to name the macOS keychain whatever machine it was printed on - unhelpful on
# the Windows machines this is most likely to be read on.
_CERTIFICATE_STEPS: Dict[str, Tuple[str, ...]] = {
    "Darwin": (
        "security find-certificate -a -p /Library/Keychains/System.keychain > ~/roots.pem",
        "cat \"$(python -c 'import certifi; print(certifi.where())')\" ~/roots.pem"
        " > ~/ca-bundle.pem",
        "export REQUESTS_CA_BUNDLE=~/ca-bundle.pem SSL_CERT_FILE=~/ca-bundle.pem",
    ),
    "Windows": (
        "# in PowerShell",
        "Get-ChildItem Cert:\\LocalMachine\\Root | ForEach-Object {",
        "  '-----BEGIN CERTIFICATE-----'",
        "  [Convert]::ToBase64String($_.RawData, 'InsertLineBreaks')",
        "  '-----END CERTIFICATE-----' } | Set-Content $env:USERPROFILE\\roots.pem"
        " -Encoding ascii",
        "$certifi = python -c \"import certifi; print(certifi.where())\"",
        "Get-Content $certifi, $env:USERPROFILE\\roots.pem |"
        " Set-Content $env:USERPROFILE\\ca-bundle.pem -Encoding ascii",
        "$env:REQUESTS_CA_BUNDLE = \"$env:USERPROFILE\\ca-bundle.pem\"",
        "$env:SSL_CERT_FILE = \"$env:USERPROFILE\\ca-bundle.pem\"",
    ),
    "Linux": (
        "cat \"$(python -c 'import certifi; print(certifi.where())')\""
        " /etc/ssl/certs/ca-certificates.crt > ~/ca-bundle.pem",
        "export REQUESTS_CA_BUNDLE=~/ca-bundle.pem SSL_CERT_FILE=~/ca-bundle.pem",
    ),
}


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


def interactive_wanted(args: argparse.Namespace) -> bool:
    """Whether to ask before fetching.

    Skipped when told to, when nothing is attached to read the answers, and when
    the choices were already made on the command line - a run that named its
    languages does not want to be asked which languages.
    """
    if args.non_interactive or args.check:
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    return not (args.languages or args.all_languages or args.results
                or args.no_translators or args.no_embedder)


def confirm_choices(args: argparse.Namespace) -> None:
    """Confirm the two things this script actually chooses, and say what it is not.

    Worth stating plainly that no purchase data is involved. The name invites the
    assumption that it fetches something from the extracts and adds columns to
    them, and someone reasonably expected exactly that: the question people ask
    here is "which folder does it read?", to which the answer is none.
    """
    print()
    print("  This downloads the models the agents run on. It reads no purchase")
    print("  data and writes no column: to widen a purchase table, run")
    print("  all_agents.py. The folder below is where the models are written.")
    print()

    default_languages = " ".join(FORTUM_LANGUAGES)
    while True:
        answer = ask("Which languages should be translatable without the paid "
                     f"model? ({' '.join(SUPPORTED_LANGUAGES)}, or 'all')",
                     default_languages)
        if answer.strip().lower() == "all":
            args.all_languages = True
            break
        codes = [code for code in answer.replace(",", " ").split() if code]
        unknown = [code for code in codes if code not in SUPPORTED_LANGUAGES]
        if unknown:
            print(f"  Not supported: {', '.join(unknown)}. "
                  f"Choose from {', '.join(SUPPORTED_LANGUAGES)}.")
            continue
        args.languages = codes or list(FORTUM_LANGUAGES)
        break

    args.results = Path(ask("Which folder should the models be written to?",
                            str(cache_root(None))))

    if ask_yes_no("Also write one archive, to carry to a machine that cannot "
                  "reach the hub?", False):
        args.bundle = Path(ask("Where should the archive be written?",
                               "models.tar.gz"))
    print()


def is_certificate_failure(record: Dict[str, object]) -> bool:
    """Whether a failure was TLS verification rather than anything else."""
    if record.get("status") != "failed":
        return False
    reason = str(record.get("reason") or "").lower()
    return any(mark in reason for mark in
               ("certificate", "ssl", "self-signed", "self signed"))


def is_blocked_failure(record: Dict[str, object]) -> bool:
    """Whether the hub answered, but refused to serve.

    Distinguished from a certificate failure because the remedy is the opposite.
    A proxy that permits the connection and then denies the host answers with a
    status rather than a handshake error, and it answers in milliseconds. No
    certificate configuration changes that, so advising it wastes the reader's
    afternoon: the only way past is to carry the models in.
    """
    if record.get("status") != "failed":
        return False
    reason = str(record.get("reason") or "").lower()
    return any(mark in reason for mark in
               ("503", "502", "504", "403", "service unavailable",
                "forbidden", "bad gateway", "gateway timeout"))


def report_blocked_advice() -> None:
    """Say what to do when the hub answers and refuses."""
    print()
    print("  The hub answered and refused. TLS worked, so this is not a")
    print("  certificate problem: something between here and huggingface.co is")
    print("  declining to serve it, which on a corporate network is usually")
    print("  policy rather than a fault.")
    print()
    print("  Check which it is. If this returns a normal page, the hub is up and")
    print("  the refusal is local to this machine:")
    print()
    print("    curl -sI https://huggingface.co/api/models/"
          "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    print()
    print("  If the hub is up and this machine still cannot reach it, no setting")
    print("  here will help. Fetch on a machine that can and carry the archive:")
    print()
    print("    # where the hub is reachable")
    print("    python fetch_models.py --bundle models.tar.gz")
    print()
    print("    # here, once the archive has been copied across")
    print("    mkdir -p ~/.cache/huggingface")
    print("    tar -xzf models.tar.gz -C ~/.cache/huggingface")
    print("    export HF_HUB_OFFLINE=1")
    print("    python fetch_models.py --check")
    print()
    print("  The models are not in the repository, so git will not bring them.")


def report_certificate_advice() -> None:
    """Say what to do about the corporate-proxy failure, which is the usual one.

    Printed for the platform this is running on. The easy remedy comes first,
    because the manual one is fiddly and has to be redone on every machine.
    """
    print()
    print("  This network re-signs HTTPS with a certificate of its own, and")
    print("  Python does not trust it. Your browser does, because it reads the")
    print("  system certificate store and Python does not.")
    print()
    print("  The simple fix is to let Python read that store too:")
    print()
    print("    pip install truststore")
    print()
    print("  If pip fails the same way, allow it through for that one install:")
    print()
    print("    pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org"
          " truststore")
    print()
    print("  Nothing else to configure. Every agent picks it up on the next run.")

    steps = _CERTIFICATE_STEPS.get(platform.system())
    if steps:
        print()
        print(f"  Failing that, export the root by hand ({platform.system()}):")
        print()
        for line in steps:
            print(f"    {line}")

    print()
    print("  If the network blocks the hub outright rather than re-signing it,")
    print("  no certificate will help. Run this on a machine that can reach the")
    print("  hub with --bundle and copy the archive across; the models are not")
    print("  in the repository, so git will not bring them.")


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
    how.add_argument("--non-interactive", action="store_true",
                     help="take the defaults instead of asking; implied when "
                          "any of the options above is given, when --check is "
                          "used, or when there is no terminal to ask at")
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

    if interactive_wanted(args):
        confirm_choices(args)

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
        repositories.append((translation_model(code), load_translator))

    if not repositories:
        LOGGER.error("Nothing selected to fetch.")
        return 1

    print(f"  Cache                : {root}")
    trust = use_system_trust_store()
    if trust:
        print(f"  Verifying TLS with   : {trust}")
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
    results: List[Dict[str, object]] = []
    for name, loader in repositories:
        results.append(fetch(name, root, loader, args.check))
        # No point working through the remaining six when the cause is the
        # network rather than the model. The hub library retries five times per
        # file before giving up, so carrying on means many minutes of identical
        # failures before anyone is told the cause - which is how both of these
        # were first met.
        stop = ("a certificate problem" if is_certificate_failure(results[-1])
                else "the hub refusing to serve" if is_blocked_failure(results[-1])
                else None)
        if stop:
            LOGGER.error("Stopping: this is %s, and it will affect every "
                         "remaining model the same way.", stop)
            for remaining, _ in repositories[len(results):]:
                results.append({"repository": remaining, "status": "missing",
                                "bytes": 0, "seconds": 0.0})
            break
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
        # Advice chosen from what actually failed. Printing the certificate steps
        # for a refusal sends the reader after the wrong thing entirely.
        if any(is_certificate_failure(record) for record in failed):
            report_certificate_advice()
        elif any(is_blocked_failure(record) for record in failed):
            report_blocked_advice()
        else:
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
