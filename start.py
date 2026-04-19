#!/usr/bin/env python3
"""
Master Launcher — Runs both bridge.js (Node.js) and bot.py together
Single command: python start.py
"""
import subprocess
import sys
import os
import signal
import time
import threading
import asyncio
import aiohttp

# ─── ANSI Color Codes ───────────────────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
CYAN    = "\033[96m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
BLUE    = "\033[94m"

# ─── Global process handle ───────────────────────────────────────────────────────
bridge_process: subprocess.Popen | None = None
shutdown_event = threading.Event()


def log(tag: str, color: str, message: str) -> None:
    """Print a prefixed, colored log line."""
    print(f"{color}{BOLD}[{tag}]{RESET} {message}", flush=True)


def log_bridge(msg: str) -> None:
    log("BRIDGE", CYAN, msg)


def log_bot(msg: str) -> None:
    log("BOT", MAGENTA, msg)


def log_launcher(msg: str) -> None:
    log("LAUNCHER", BLUE, msg)


def log_ok(msg: str) -> None:
    log("OK", GREEN, msg)


def log_warn(msg: str) -> None:
    log("WARN", YELLOW, msg)


def log_err(msg: str) -> None:
    log("ERROR", RED, msg)


# ─── Signal Handling ─────────────────────────────────────────────────────────────
def handle_signal(signum, frame):
    """Graceful shutdown on SIGTERM / SIGINT."""
    sig_name = signal.Signals(signum).name
    log_launcher(f"Received {sig_name} — initiating graceful shutdown...")
    shutdown_event.set()
    _kill_bridge()
    sys.exit(0)


def _kill_bridge() -> None:
    """Terminate the bridge subprocess if it is alive."""
    global bridge_process
    if bridge_process and bridge_process.poll() is None:
        log_launcher("Stopping bridge server...")
        try:
            bridge_process.terminate()
            bridge_process.wait(timeout=10)
            log_ok("Bridge server stopped cleanly.")
        except subprocess.TimeoutExpired:
            log_warn("Bridge did not exit in time — sending SIGKILL.")
            bridge_process.kill()
        except Exception as exc:
            log_err(f"Error stopping bridge: {exc}")
        bridge_process = None


# ─── Bridge Runner ────────────────────────────────────────────────────────────────
def run_bridge(auto_restart: bool = True) -> None:
    """
    Start (and optionally restart) the Node.js bridge server.
    Streams all bridge stdout/stderr to the console with [BRIDGE] prefix.
    Runs in a daemon thread — exits when the main process exits.
    """
    global bridge_process

    while not shutdown_event.is_set():
        log_bridge("Starting WhatsApp Bridge Server (Node.js)...")
        try:
            bridge_process = subprocess.Popen(
                ["node", "bridge.js"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=os.environ.copy(),
            )

            # Stream output line-by-line
            for line in bridge_process.stdout:
                if shutdown_event.is_set():
                    break
                print(f"{CYAN}{BOLD}[BRIDGE]{RESET} {line}", end="", flush=True)

            bridge_process.wait()
            exit_code = bridge_process.returncode

            if shutdown_event.is_set():
                log_launcher("Bridge thread exiting (shutdown requested).")
                return

            if exit_code != 0:
                log_err(f"Bridge exited with code {exit_code}.")
            else:
                log_warn("Bridge exited unexpectedly (code 0).")

            if not auto_restart:
                log_launcher("Auto-restart disabled — bridge will not be restarted.")
                return

            log_warn("Restarting bridge in 5 seconds...")
            time.sleep(5)

        except FileNotFoundError:
            log_err("'node' executable not found. Is Node.js installed?")
            log_launcher("Bridge thread aborting — no Node.js runtime.")
            return
        except Exception as exc:
            log_err(f"Unexpected error in bridge runner: {exc}")
            if auto_restart and not shutdown_event.is_set():
                log_warn("Restarting bridge in 5 seconds...")
                time.sleep(5)
            else:
                return


# ─── Bridge Health Check ──────────────────────────────────────────────────────────
async def wait_for_bridge(bridge_url: str, max_attempts: int = 15, interval: float = 2.0) -> bool:
    """
    Poll the bridge /health endpoint until it responds 200 OK.
    Returns True if the bridge is healthy, False on timeout.
    """
    health_url = f"{bridge_url.rstrip('/')}/health"
    log_launcher(f"Polling bridge health at {health_url} ...")

    for attempt in range(1, max_attempts + 1):
        if shutdown_event.is_set():
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=3)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(health_url) as resp:
                    if resp.status == 200:
                        log_ok(f"Bridge is healthy (attempt {attempt}/{max_attempts}).")
                        return True
                    else:
                        log_warn(f"Bridge health returned HTTP {resp.status} (attempt {attempt}/{max_attempts}).")
        except (aiohttp.ClientConnectorError, asyncio.TimeoutError):
            log_launcher(f"⏳ Waiting for bridge... attempt {attempt}/{max_attempts}")
        except Exception as exc:
            log_warn(f"Health check error: {exc} (attempt {attempt}/{max_attempts})")

        await asyncio.sleep(interval)

    log_warn("Bridge health check timed out. Starting bot anyway...")
    return False


# ─── Bot Runner ───────────────────────────────────────────────────────────────────
def run_bot() -> None:
    """Import and execute the Telegram bot's main() function."""
    log_bot("Starting Telegram Bot (Python)...")
    try:
        import bot  # type: ignore
        bot.main()
    except ImportError:
        log_err("Could not import 'bot'. Make sure bot.py is in the same directory.")
        sys.exit(1)
    except Exception as exc:
        log_err(f"Bot crashed with an unhandled exception: {exc}")
        raise


# ─── Entry Point ──────────────────────────────────────────────────────────────────
def main() -> None:
    print(f"""
{CYAN}{BOLD}╔══════════════════════════════════════════════════════╗
║  WhatsApp Group Manager — Combined Launcher          ║
║  Starting Bridge Server + Telegram Bot               ║
╚══════════════════════════════════════════════════════╝{RESET}
""", flush=True)

    # Register signal handlers for clean shutdowns (Render sends SIGTERM)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    bridge_url = os.getenv("BRIDGE_URL", "http://localhost:3000")
    log_launcher(f"BRIDGE_URL = {bridge_url}")

    # ── Start bridge.js in a background daemon thread ──────────────────────────
    bridge_thread = threading.Thread(target=run_bridge, kwargs={"auto_restart": True}, daemon=True)
    bridge_thread.start()
    log_launcher("Bridge thread started.")

    # ── Give the process a moment to spin up before polling ───────────────────
    log_launcher("Waiting for bridge server to initialise...")
    time.sleep(3)

    # ── Health check (async) ───────────────────────────────────────────────────
    asyncio.run(wait_for_bridge(bridge_url, max_attempts=15, interval=2.0))

    # ── Start bot (blocking call — runs until killed) ──────────────────────────
    try:
        run_bot()
    finally:
        log_launcher("Bot exited. Shutting down bridge...")
        shutdown_event.set()
        _kill_bridge()
        log_ok("All services stopped. Goodbye!")


if __name__ == "__main__":
    main()
