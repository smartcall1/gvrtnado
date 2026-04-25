# grvtnado.py
import sys
import time
import subprocess
import logging
import threading
from pathlib import Path
from logging.handlers import RotatingFileHandler

MAX_CRASHES = 10
CRASH_WINDOW = 300
STOP_FILE = ".stop_bot"
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "bot.log"
ENGINE_LOG_FILE = LOG_DIR / "engine.log"
LOG_SIZE = 5_000_000
LOG_BACKUPS = 3

LOG_DIR.mkdir(exist_ok=True)

handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_SIZE, backupCount=LOG_BACKUPS)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler, console])
logger = logging.getLogger("watchdog")

stop_path = Path(STOP_FILE)
if stop_path.exists():
    stop_path.unlink()


def _tee_stream(stream, log_file):
    """subprocess stdout을 터미널 + 파일 양쪽에 출력한다."""
    with open(log_file, "a", buffering=1, encoding="utf-8", errors="replace") as f:
        for line in stream:
            sys.stdout.write(line)
            sys.stdout.flush()
            f.write(line)


def main():
    crashes: list[float] = []

    while True:
        if stop_path.exists():
            logger.info("Stop file detected, exiting")
            break

        logger.info("Starting bot process...")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", "-c",
                 "import asyncio; from nado_grvt_engine import DeltaNeutralBot; asyncio.run(DeltaNeutralBot().run())"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            tee = threading.Thread(target=_tee_stream, args=(proc.stdout, ENGINE_LOG_FILE), daemon=True)
            tee.start()
            proc.wait()
            tee.join(timeout=5)

            if proc.returncode == 0:
                logger.info("Bot exited cleanly")
                if stop_path.exists():
                    break
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt, exiting")
            if proc and proc.poll() is None:
                proc.terminate()
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
