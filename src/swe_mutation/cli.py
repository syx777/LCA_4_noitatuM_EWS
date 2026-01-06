import argparse
from pathlib import Path
from typing import List

from .pipeline import run_generate


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="swe-mutation",
        description="SWE-Mutation: Code mutation generator"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Generate mutated variants in batch")
    gen.add_argument("--input", type=str, required=True, help="Input directory (scan .py files)")
    gen.add_argument("--output", type=str, required=True, help="Output directory (write mutated variants)")
    gen.add_argument(
        "--operators",
        nargs="+",
        default=["flip-comparisons", "negate-ifs", "swap-plus-minus"],
        help="Mutation operator names (multiple allowed)"
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.command == "generate":
        input_dir = Path(args.input).resolve()
        output_dir = Path(args.output).resolve()
        ops: List[str] = list(args.operators)
        written = run_generate(input_dir, output_dir, ops)
        for p in written:
            print(str(p))


if __name__ == "__main__":
    main()
