"""Entry point for background daemon subprocess: python -m astra.daemon.runner"""
import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args()

    from astra.daemon.core import AstraDaemon
    daemon = AstraDaemon(Path(args.repo), Path(args.db))
    daemon.start()


if __name__ == "__main__":
    main()
