# Procurement Analysis Agents

AI enrichment agents for procurement spend analysis. Four agents are planned; this
repository currently contains the first.

| Agent | Purpose | Status |
|---|---|---|
| `agent1.py` | Improved purchase description | Implemented |
| `agent2.py` | Purchase Group (Category L5) | Planned |
| `agent3.py` | Material and service standardisation | Planned |
| `agent4.py` | Supplier consolidation | Planned |

---

## Agent 1 - Improved Purchase Description

Reads procurement line data from every available source system, resolves each line
against the other sources, and appends a clear, standardised English description of what
was purchased. Original source text is preserved in full.

### What it does

1. **Ingests** every `.xlsx`, `.csv` and `.tsv` file found in the configured folders and
   identifies which source system each one came from by inspecting its column names, so
   a file resolves correctly whether it arrives as Excel or CSV.
2. **Types every row** as `LINE`, `HEADER`, `SUBTOTAL` or `TOTAL`. Invoice extracts
   interleave all four, and describing a total row would distort every downstream count.
   No row is ever dropped; non-lines are flagged instead.
3. **Detects duplicates** by hashing row content while ignoring volatile columns such as
   the source file name, so the same invoice delivered twice is recognised as one line.
4. **Identifies the language** of each description and renders it in English through a
   three-step cascade: controlled vocabulary, then offline machine translation, then a
   language model as a last resort.
5. **Matches each line** against lines in the other systems in three tiers - deterministic
   keys, blocked fuzzy comparison, then character n-gram retrieval - recording the tier
   and score of every link.
6. **Composes the description** from validated evidence using templates rather than free
   generation, and discards any word that cannot be traced to the source data or the
   vocabulary.
7. **Scores confidence** from measurable pipeline properties, not from a model's opinion
   of its own output.

### Repeatability

Re-running on unchanged inputs produces byte-identical output. Descriptions are built by
deterministic lookup and templating; the language model runs at temperature 0 over unique
phrases only, with every answer cached on disk; and no timestamps or random values appear
in row output. The run identifier is derived from the input file contents and the
configuration, so an output file can always be traced back to exactly what produced it.

### Installation

```bash
python3 -m venv myenv
source myenv/bin/activate
pip install -r requirements.txt      # optional accelerators; the agent runs without them
```

Python 3.9 or newer. The agent has no mandatory third-party dependencies.

### Running

```bash
python agent1.py
```

Every path is prompted for, with a default in brackets that is accepted by pressing
Enter:

```
Source data folder
  [/path/to/fo-p-agents/sources]:
Invoice data folder
  [/path/to/fo-p-agents/sources/invoice data]:
Purchase order data folder
  [/path/to/fo-p-agents/sources/po data]:
Transaction data folder
  [/path/to/fo-p-agents/sources/transaction data]:
Supplier catalogue file (optional, '-' to skip)
  [/path/to/fo-p-agents/sources/Demo - Item Catalogues.csv]:
Results folder
  [/path/to/fo-p-agents/results]:
Enable the language-model fallback for unresolved phrases? [y/N]:
```

For scheduled or scripted runs, pass the paths instead:

```bash
python agent1.py --non-interactive \
    --sources "./sources" \
    --results "./results" \
    --use-llm
```

`python agent1.py --help` lists every option, including the matching thresholds
(`--fuzzy-threshold`, `--semantic-threshold`), the number of matches retained per line
(`--top-k`) and the description word budget (`--max-words`).

### Output

Written to `results/`:

| File | Contents |
|---|---|
| `agent1_<source>.csv` | One file per input, original columns untouched with the enrichment columns appended at the end |
| `agent1_unified_lines.csv` | Common schema across all sources; the input contract for Agents 2 to 4 |
| `agent1_unified_lines.jsonl` | Same rows with the full nested evidence bundle |
| `agent1_run_manifest.json` | Input hashes, configuration, statistics and vocabulary version |

Key appended columns:

`Enriched_Purchase_Description`, `Enriched_Description_Short`, `Item_Or_Service`,
`AI_Confidence` (0-100), `Confidence_Band`, `Detected_Language`, `Translation_Method`,
`Translation_Coverage`, `Unresolved_Tokens`, `Match_Tier`, `Match_Score`,
`Matched_Source_System`, `Row_Type`, `Is_Duplicate`, `Run_Id`, `Lexicon_Version`.

### Configuration

Copy `.env.example` to `.env` and fill in the credentials. A single switch selects the
backend:

```ini
AZURE_ENABLE=false     # direct OpenAI API, for local development
AZURE_ENABLE=true      # Azure OpenAI via the PwC shared service
```

Each backend has its own key, base URL and model name, so both can be configured at once
and the switch chooses between them. `BASE_URL` accepts either an API root
(`https://host/v1`) or a fully qualified endpoint (`https://host/v1/chat/completions`).

The language model is **off by default**. The agent produces complete output without it;
enabling it only improves coverage of phrases the vocabulary cannot resolve.

### Controlled vocabulary

`lexicon/procurement_lexicon.json` holds the Finnish, Swedish and Polish procurement
vocabulary that does most of the translation work. It handles multi-word expressions,
case inflection and Nordic compound words, and it is the cheapest way to raise output
quality: adding a term costs nothing at run time and improves every future run.

Bump the `version` field whenever the file changes. That value is written into every
output row, so any result can be traced back to the vocabulary that produced it.
