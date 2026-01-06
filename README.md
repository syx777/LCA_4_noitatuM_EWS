# SWE-Mutation

A minimal, extensible framework and example repository for code mutation in software engineering tasks. It provides simple yet composable mutation operators (e.g., comparison flip, negated conditions, plus-minus swap), a CLI to batch-process Python files, and basic tests to reproduce experiments aligned with the paper’s methodology.

## Features
- AST-based mutation operators on comparisons, conditions, and arithmetic
- Batch data generation from `.py` files in an input directory
- Extensible pipeline for adding new operators and flows
- Basic tests using `pytest`

## Requirements and Usage
- Python 3.9+
- No hard third-party dependencies; run via CLI with `PYTHONPATH=src`

Example:
```
PYTHONPATH=src python -m swe_mutation.cli generate \
  --input ./examples/input \
  --output ./examples/output \
  --operators flip-comparisons negate-ifs swap-plus-minus
```

Arguments:
- `--input`: directory to scan `.py` files
- `--output`: directory to write mutated variants
- `--operators`: mutation operators to apply (multiple allowed)
  - `flip-comparisons`: flips `>` ↔ `<`, `>=` ↔ `<=`, `==` ↔ `!=`
  - `negate-ifs`: negates `if` conditions
  - `swap-plus-minus`: swaps `+` and `-` in binary operations

## Project Structure
```
SWE-Mutation/
├── README.md
├── pyproject.toml
├── acl.tex
├── scripts/
│   ├── mutation.py        # SWE-Bench related orchestration script
│   ├── testgen.py         # Generate complete tests (MiniSWEAgent based)
│   └── eval.py            # Evaluate hack patches vs golden patches
├── src/
│   └── swe_mutation/
│       ├── __init__.py
│       ├── cli.py
│       ├── mutations.py
│       ├── pipeline.py
│       └── utils.py
├── tests/
│   └── test_mutations.py
├── examples/
│   └── input/sample.py
└── .gitignore
```

## Extra Scripts
- Located in `scripts/` for clarity and separation from the core library.
- These scripts depend on external ecosystems (SWE-Bench, MiniSWEAgent, Typer, Datasets, Rich). Install them as needed before running:
  - `python scripts/mutation.py`
  - `python scripts/testgen.py`
  - `python scripts/eval.py`

## ACL Paper
- `acl.tex` is an ACL-style skeleton with common sections (Introduction, Related Work, Method, Experiments, Results and Analysis, Ethics Statement, Conclusion).
- For local compilation, ensure the proper ACL style files (e.g., `acl.sty`/`acl.bst`) or use the official template environment.

## Testing
```
pytest -q
```
If `pytest` is not installed:
```
python -m pip install pytest
```

## License
MIT by default; adjust as needed.
