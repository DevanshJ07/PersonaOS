"""PersonaOS command-line entry point for the approved Windows desktop."""

import argparse
import json
from pathlib import Path

from core.session_controller import run_session


PROJECT_ROOT = Path(__file__).resolve().parent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run a PersonaOS desktop work session")
    parser.add_argument("--mode", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "settings.json")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "outputs" / "telemetry")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        summary = run_session(config, args.output_dir, args.mode)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"PersonaOS setup error: {exc}\n")
    print(f"Session: {summary['session_dir']}")
    print(f"Result: {'PASS' if summary['success'] else 'FAIL'} ({summary['duration_s']}s)")
    for error in summary["errors"]:
        print(f"Error: {error}")
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
