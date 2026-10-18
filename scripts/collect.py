"""Run the scheduled collection workflow once, without sending reports."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from reportagent.cli.scheduled_collect import main as collect

    return collect()


if __name__ == "__main__":
    raise SystemExit(main())
