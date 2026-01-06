import os
from pathlib import Path
from typing import Iterable, List

from .mutations import apply_operators
from .utils import read_text, write_text


def list_py_files(root: Path) -> List[Path]:
    return [p for p in root.rglob("*.py") if p.is_file()]


def mutate_file(input_path: Path, output_dir: Path, operators: List[str]) -> List[Path]:
    code = read_text(input_path)
    variants = apply_operators(code, operators)
    written_paths: List[Path] = []
    for i, mutated in enumerate(variants, start=1):
        suffix = operators[i - 1].replace("-", "_")
        rel = input_path.name.replace(".py", f".mut.{suffix}.py")
        out_path = output_dir / rel
        write_text(out_path, mutated)
        written_paths.append(out_path)
    return written_paths


def run_generate(input_dir: Path, output_dir: Path, operators: List[str]) -> List[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    inputs = list_py_files(input_dir)
    all_written: List[Path] = []
    for p in inputs:
        written = mutate_file(p, output_dir, operators)
        all_written.extend(written)
    return all_written

