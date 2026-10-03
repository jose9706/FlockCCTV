"""Command-line entry point for the Flock CCTV bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from . import version_string
from .bot import create_bot
from .config import Config


async def _run_bot(bot: object, token: str, *, stop_event: asyncio.Event | None = None) -> None:
    """Run the client with graceful SIGINT and systemd SIGTERM handling."""
    loop = asyncio.get_running_loop()
    stopping = stop_event or asyncio.Event()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            # Signal handlers are unavailable outside the main thread and on
            # some event loop implementations. Client.start still runs normally.
            pass

    start = asyncio.create_task(bot.start(token), name="flock-cctv-gateway")  # type: ignore[attr-defined]
    stop_wait = asyncio.create_task(stopping.wait(), name="flock-cctv-signal")
    try:
        done, _ = await asyncio.wait((start, stop_wait), return_when=asyncio.FIRST_COMPLETED)
        if start in done:
            await start
        else:
            await bot.close()  # type: ignore[attr-defined]
            try:
                await asyncio.wait_for(start, timeout=30)
            except TimeoutError:
                logging.getLogger(__name__).error(
                    "Gateway task did not stop within 30 seconds; cancelling it"
                )
                start.cancel()
                await asyncio.gather(start, return_exceptions=True)
    finally:
        stop_wait.cancel()
        await asyncio.gather(stop_wait, return_exceptions=True)
        if not start.done():
            start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        try:
            if not bot.is_closed():  # type: ignore[attr-defined]
                await bot.close()  # type: ignore[attr-defined]
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flock-cctv", description="Run the Flock CCTV bot.")
    parser.add_argument("--version", action="version", version=f"flock-cctv {version_string()}")
    parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger(__name__).info("Starting flock-cctv %s", version_string())
    config = Config.from_env()
    bot = create_bot(config)
    asyncio.run(_run_bot(bot, config.token))


if __name__ == "__main__":
    main()
