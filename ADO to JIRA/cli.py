import requests

from config import configure_logging, update_run_state
from models import RUNTIME
from sync_service import run_sync


def main():
    if RUNTIME.session is None:
        RUNTIME.session = requests.Session()
    try:
        configure_logging()
        update_run_state("running")
        run_sync()
    except Exception as error:
        configure_logging()
        update_run_state("failed", error=str(error))
        RUNTIME.logger.exception("Run stopped with error: %s", error)


if __name__ == "__main__":
    main()