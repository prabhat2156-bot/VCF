#!/usr/bin/env python3
"""
Master Launcher for WhatsApp Group Manager
Runs bridge.js (Node.js) and bot.py (Python) together.
Single command: python start.py
"""
import subprocess
import os
import sys
import signal
import time
import shutil
import threading
import urllib.request
import json

# ── Paths ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BRIDGE_PATH = os.path.join(BASE_DIR, "bridge.js")
BOT_PATH = os.path.join(BASE_DIR, "bot.py")
NODE_MODULES = os.path.join(BASE_DIR, "node_modules")
PACKAGE_JSON = os.path.join(BASE_DIR, "package.json")

BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "3000"))
BRIDGE_URL = os.getenv("BRIDGE_URL", f"http://localhost:{BRIDGE_PORT}")

# Colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"

bridge_process = None
bot_process = None
shutdown_flag = False


def log(tag, msg, color=RESET):
    print(f"{color}[{tag}]{RESET} {msg}", flush=True)


def preflight_checks():
    """Run all checks before starting services"""

    log("CHECK", "Running pre-flight checks...", CYAN)
    log("CHECK", f"Base directory: {BASE_DIR}", CYAN)

    # 1. Check bridge.js exists
    if not os.path.exists(BRIDGE_PATH):
        log("ERROR", f"bridge.js NOT FOUND at: {BRIDGE_PATH}", RED)
        log("ERROR", "Make sure bridge.js is in the same directory as start.py", RED)
        # List files in directory to help debug
        log("DEBUG", f"Files in {BASE_DIR}:", YELLOW)
        for f in os.listdir(BASE_DIR):
            log("DEBUG", f"  - {f}", YELLOW)
        sys.exit(1)
    log("OK", "bridge.js found", GREEN)

    # 2. Check bot.py exists
    if not os.path.exists(BOT_PATH):
        log("ERROR", f"bot.py NOT FOUND at: {BOT_PATH}", RED)
        sys.exit(1)
    log("OK", "bot.py found", GREEN)

    # 3. Check Node.js installed
    node_path = shutil.which("node")
    if not node_path:
        log("ERROR", "Node.js is NOT installed!", RED)
        log("ERROR", "Install Node.js 18+ or add it to your Render build", RED)
        sys.exit(1)
    # Get node version
    result = subprocess.run(["node", "--version"], capture_output=True, text=True)
    log("OK", f"Node.js found: {result.stdout.strip()}", GREEN)

    # 4. Check Python version
    log("OK", f"Python: {sys.version.split()[0]}", GREEN)

    # 5. Check/install node_modules
    if not os.path.exists(NODE_MODULES):
        if os.path.exists(PACKAGE_JSON):
            log("WARN", "node_modules not found. Running npm install...", YELLOW)
            npm_result = subprocess.run(
                ["npm", "install"],
                cwd=BASE_DIR,
                capture_output=True,
                text=True
            )
            if npm_result.returncode == 0:
                log("OK", "npm install completed successfully", GREEN)
            else:
                log("ERROR", f"npm install failed:\n{npm_result.stderr}", RED)
                sys.exit(1)
        else:
            log("ERROR", "No package.json found! Create it with required dependencies.", RED)
            sys.exit(1)
    else:
        log("OK", "node_modules found", GREEN)

    # 6. Check Python dependencies
    missing = []
    for module_name, pip_name in [
        ("telegram", "python-telegram-bot"),
        ("aiohttp", "aiohttp"),
        ("flask", "flask"),
        ("pymongo", "pymongo"),
        ("dotenv", "python-dotenv"),
    ]:
        try:
            __import__(module_name)
        except ImportError:
            missing.append(pip_name)

    if missing:
        log("WARN", f"Missing Python packages: {', '.join(missing)}", YELLOW)
        log("WARN", "Installing missing packages...", YELLOW)
        install_result = subprocess.run(
            [sys.executable, "-m", "pip", "install"] + missing,
            capture_output=True,
            text=True
        )
        if install_result.returncode == 0:
            log("OK", "Packages installed successfully", GREEN)
        else:
            log("ERROR", f"pip install failed:\n{install_result.stderr}", RED)
            sys.exit(1)
    else:
        log("OK", "All Python dependencies found", GREEN)

    log("CHECK", "All pre-flight checks passed! ✅\n", GREEN)


def start_bridge():
    """Start bridge.js using subprocess — auto-restarts on crash"""
    global bridge_process

    while not shutdown_flag:
        log("BRIDGE", "Starting WhatsApp Bridge Server...", CYAN)
        bridge_process = subprocess.Popen(
            ["node", BRIDGE_PATH],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "PORT": str(BRIDGE_PORT)}
        )

        # Stream output line by line
        try:
            for line in bridge_process.stdout:
                if shutdown_flag:
                    break
                print(f"{CYAN}[BRIDGE]{RESET} {line}", end="", flush=True)
        except Exception:
            pass

        exit_code = bridge_process.wait()

        if shutdown_flag:
            break

        log("ERROR", f"Bridge exited with code {exit_code}", RED)
        log("WARN", "Restarting bridge in 5 seconds...", YELLOW)
        time.sleep(5)


def wait_for_bridge(max_attempts=15, interval=2):
    """
    Poll the bridge /health endpoint until it responds 200.
    Uses only urllib — no third-party dependencies required.
    Returns True if healthy, False if timed out.
    """
    health_url = f"{BRIDGE_URL}/health"
    secret = os.getenv("BRIDGE_SECRET", "")

    log("LAUNCHER", f"Polling bridge health at: {health_url}", CYAN)

    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(
                health_url,
                headers={"X-Secret": secret}
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    log("OK", "Bridge server is healthy! ✅", GREEN)
                    return True
        except Exception:
            pass

        log("LAUNCHER", f"Waiting for bridge... attempt {attempt}/{max_attempts}", YELLOW)
        time.sleep(interval)

    log("WARN", "Bridge health check timed out. Starting bot anyway...", YELLOW)
    return False


def stream_output(process, prefix, color):
    """
    Read lines from a process stdout and print them with a colored prefix.
    Runs in its own thread so it never blocks the main thread.
    """
    try:
        for line in process.stdout:
            if shutdown_flag:
                break
            print(f"{color}[{prefix}]{RESET} {line}", end="", flush=True)
    except Exception:
        pass


def start_bot():
    """
    Start bot.py as a subprocess — NOT via import.
    This guarantees that any import errors inside bot.py appear
    clearly in the logs instead of crashing the launcher silently.
    """
    global bot_process

    log("BOT", "Starting Telegram Bot...", GREEN)
    bot_process = subprocess.Popen(
        [sys.executable, BOT_PATH],
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    # Stream bot output — blocks until bot exits
    try:
        for line in bot_process.stdout:
            if shutdown_flag:
                break
            print(f"{GREEN}[BOT]{RESET} {line}", end="", flush=True)
    except Exception:
        pass

    return bot_process.wait()


def shutdown(signum=None, frame=None):
    """Graceful shutdown — terminate both bridge and bot cleanly"""
    global shutdown_flag

    # Guard against being called twice (e.g. SIGINT + SIGTERM)
    if shutdown_flag:
        return
    shutdown_flag = True

    log("LAUNCHER", "Shutting down all services...", YELLOW)

    for name, proc in [("Bot", bot_process), ("Bridge", bridge_process)]:
        if proc and proc.poll() is None:
            log("LAUNCHER", f"Stopping {name}...", YELLOW)
            try:
                proc.terminate()
                proc.wait(timeout=10)
                log("OK", f"{name} stopped cleanly", GREEN)
            except subprocess.TimeoutExpired:
                proc.kill()
                log("WARN", f"{name} force killed after timeout", RED)
            except Exception as e:
                log("WARN", f"Error stopping {name}: {e}", RED)

    log("LAUNCHER", "All services stopped. Goodbye!", CYAN)
    sys.exit(0)


def main():
    print(f"""
{BOLD}{CYAN}╔══════════════════════════════════════════════════════╗
║  WhatsApp Group Manager — Combined Launcher          ║
║  Bridge Server (Node.js) + Telegram Bot (Python)     ║
╚══════════════════════════════════════════════════════╝{RESET}
    """)

    # Register signal handlers FIRST so Ctrl+C / Render SIGTERM are handled
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Step 1: Pre-flight checks ──────────────────────────────────────────
    preflight_checks()

    # ── Step 2: Start bridge in a background daemon thread ─────────────────
    # daemon=True means this thread dies automatically when main thread exits
    bridge_thread = threading.Thread(target=start_bridge, daemon=True, name="BridgeThread")
    bridge_thread.start()

    # Give bridge a moment to initialize before we start polling
    log("LAUNCHER", "Waiting 3 seconds for bridge to initialize...", CYAN)
    time.sleep(3)

    # ── Step 3: Wait for bridge to be healthy ──────────────────────────────
    wait_for_bridge(max_attempts=15, interval=2)

    # ── Step 4: Start bot — blocks main thread until bot exits ─────────────
    exit_code = start_bot()

    # If we reach here it means bot.py exited on its own (not via signal)
    if not shutdown_flag:
        log("ERROR", f"Bot exited unexpectedly with code {exit_code}", RED)
        log("LAUNCHER", "Triggering shutdown...", YELLOW)
        shutdown()


if __name__ == "__main__":
    main()
