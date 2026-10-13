import argparse
import logging
import time
from pathlib import Path

from .config import Config, load_dotenv
from .sync import Syncer


def main() -> None:
    p = argparse.ArgumentParser(prog="m2v", description="Sync Moodle deadlines into Vikunja.")
    p.add_argument("--env", default=".env", help="path to a .env file (default: .env)")
    p.add_argument("--dry-run", action="store_true", help="show what would change without writing to Vikunja")
    p.add_argument("--loop", type=float, metavar="MINUTES", help="keep running, syncing every MINUTES")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv(Path(args.env))
    cfg = Config.from_env()

    while True:
        try:
            Syncer(cfg, dry_run=args.dry_run).run()
        except Exception:
            if not args.loop:
                raise
            logging.exception("sync failed")
        if not args.loop:
            return
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
