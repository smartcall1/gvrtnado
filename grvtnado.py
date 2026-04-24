# grvtnado.py
import sys
import time
import subprocess
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler

MAX_CRASHES = 10
CRASH_WINDOW = 300
STOP_FILE = ".stop_bot"
LOG_FILE = "logs/bot.log"
LOG_SIZE = 5_000_000
LOG_BACKUPS = 3

Path("logs").mkdir(exist_ok=True)

handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_SIZE, backupCount=LOG_BACKUPS)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler, console])
logger = logging.getLogger("watchdog")

stop_path = Path(STOP_FILE)
if stop_path.exists():
    stop_path.unlink()


def main():
    crashes: list[float] = []

    while True:
        if stop_path.exists():
            logger.info("Stop file detected, exiting")
            break

        logger.info("Starting bot process...")
        try:
            proc = subprocess.run(
                [sys.executable, "-u", "-c",
                 "import asyncio; from bot_core import DeltaNeutralBot; asyncio.run(DeltaNeutralBot().run())"],
                timeout=None,
            )
            if proc.returncode == 0:
                logger.info("Bot exited cleanly")
                if stop_path.exists():
                    break
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt, exiting")
            break
        except Exception as e:
            logger.error(f"Bot crashed: {e}")

        now = time.time()
        crashes.append(now)
        crashes = [t for t in crashes if now - t < CRASH_WINDOW]

        if len(crashes) >= MAX_CRASHES:
            logger.critical(f"{MAX_CRASHES} crashes in {CRASH_WINDOW}s, permanent exit")
            break

        logger.info("Restarting in 5 seconds...")
        time.sleep(5)


if __name__ == "__main__":
    main()
