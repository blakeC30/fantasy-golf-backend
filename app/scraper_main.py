"""
Scraper container entrypoint.

This module is the CMD target for the scraper Docker container:

    CMD ["python", "-m", "app.scraper_main"]

It starts the APScheduler-based sync scheduler and blocks until SIGTERM or
SIGINT. The API container (app/main.py) does NOT start the scheduler — only
this module does. This separation means:

  - Scraper failures cannot crash or slow down the API process.
  - The scraper container can be restarted or redeployed independently.
  - Live scoring (every 10 min) runs in isolated threads with no impact
    on the API's request-handling thread pool.

Both containers connect to the same PostgreSQL database using the same
DATABASE_URL environment variable configured in the K8s ConfigMap/Secrets.
The scraper only writes to the DB — it never serves HTTP requests.
"""

import logging
import signal
import sys

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    from app.services.scheduler import start_scheduler, stop_scheduler

    def _handle_shutdown(signum: int, _frame) -> None:
        log.info("Received signal %d — shutting down scraper", signum)
        stop_scheduler()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    log.info("Starting scraper process")
    start_scheduler()

    # Block the main thread indefinitely. APScheduler runs all jobs in
    # background threads — signal.pause() is the correct way to keep the
    # main thread alive on Linux without burning CPU.
    signal.pause()


if __name__ == "__main__":
    main()
