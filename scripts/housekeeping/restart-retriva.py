#!/usr/bin/env python3
"""
Script to restart all Retriva services:
- ingestion_api
- openai_api
- retriva-gateway
- retriva-webui
"""

import os
import subprocess
import sys
import argparse
import socket
import time
from datetime import datetime
from pathlib import Path

# Base paths
BASE_DIR = Path.home() / "devel/ai/retriva/implementation"
LOGS_DIR = BASE_DIR / "logs"
DATE_STR = datetime.now().strftime("%Y%m%d")

# Virtual environment paths
VENV_CORE = Path("/mnt/nvme0/venvs/.retriva-core")
VENV_GATEWAY = Path("/mnt/nvme0/venvs/.retriva-gateway")

# Program paths
RETRIVA_PATH = BASE_DIR / "retriva"
GATEWAY_PATH = BASE_DIR / "retriva-gateway"
WEBUI_PATH = BASE_DIR / "retriva-webui"

# Ensure logs directory exists
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def kill_process_by_name(process_name, wait_timeout: float = 8.0):
    """Kill a process by name using pkill, then wait until it is gone.

    Waiting is important: ``pkill`` only sends SIGTERM and returns immediately,
    so without polling we can re-open log files (with truncation) while the
    dying process still has them open at a high offset, which produces a block
    of NUL bytes (``^@^@...``) in the new log followed by the old process's
    final messages. If the process refuses to exit within ``wait_timeout``
    seconds, we escalate to SIGKILL.
    """
    try:
        result = subprocess.run(
            ["pkill", "-f", process_name],
            capture_output=True,
            timeout=5,
        )
        # pkill returns 1 when no process matched; that's fine.
        if result.returncode not in (0, 1):
            print(f"pkill returned {result.returncode} for {process_name}")
    except subprocess.TimeoutExpired:
        print(f"Timeout killing process: {process_name}")
        return
    except Exception as e:
        print(f"Error killing process {process_name}: {e}")
        return

    # Wait for the process(es) to actually exit.
    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        check = subprocess.run(
            ["pgrep", "-f", process_name],
            capture_output=True,
        )
        if check.returncode != 0:
            print(f"Killed process: {process_name}")
            return
        time.sleep(0.2)

    # Still alive after SIGTERM grace period — escalate.
    print(f"Process {process_name} still alive after {wait_timeout}s, sending SIGKILL")
    try:
        subprocess.run(
            ["pkill", "-9", "-f", process_name],
            capture_output=True,
            timeout=5,
        )
    except Exception as e:
        print(f"Error SIGKILLing {process_name}: {e}")
    # Brief settle.
    time.sleep(0.3)
    print(f"Killed process: {process_name}")


def start_retriva_service(name, service_dir, module_name, venv_path, log_file):
    """Start a Python service with the specified virtual environment."""
    print(f"Starting {name}...")
    
    # Kill existing process
    kill_process_by_name(module_name)
    
    python_executable = str(venv_path / "bin/python")
    log_path = LOGS_DIR / log_file
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    env["PYTHONPATH"] = "src"

    args = [python_executable, "-u", "-m", module_name]

    try:
        with open(log_path, "wb") as log_f:
            subprocess.Popen(
                args,
                cwd=service_dir,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        print(f"Started {name} -> {log_path}")
    except Exception as e:
        print(f"Error starting {name}: {e}")


def start_gateway_service(name, service_dir, venv_path, log_file):
    """Start the gateway service directly from its gateway module."""
    print(f"Starting {name}...")

    # Only kill the gateway itself here. A broad ``pkill -f uvicorn`` would
    # also match the ingestion_api / openai_api we just started (they import
    # uvicorn and may have it in their argv on some setups).
    kill_process_by_name("retriva_gateway.main")
    
    python_executable = str(venv_path / "bin/python")
    log_path = LOGS_DIR / log_file
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    env["PYTHONPATH"] = "src"

    args = [python_executable, "-u", "-m", "retriva_gateway.main"]

    try:
        with open(log_path, "wb") as log_f:
            subprocess.Popen(
                args,
                cwd=service_dir,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        print(f"Started {name} -> {log_path}")
    except Exception as e:
        print(f"Error starting {name}: {e}")


def start_npm_service(name, service_path, log_file=None):
    """Start an npm service."""
    print(f"Starting {name}...")
    
    kill_process_by_name(service_path.name)
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    args = ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

    try:
        if log_file:
            log_path = LOGS_DIR / log_file
            with open(log_path, "wb") as log_f:
                subprocess.Popen(
                    args,
                    cwd=service_path,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                )
        else:
            subprocess.Popen(
                args,
                cwd=service_path,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        print(f"Started {name}")
    except Exception as e:
        print(f"Error starting {name}: {e}")


def wait_for_tcp(host: str, port: int, timeout: int = 30) -> bool:
    """Wait until a TCP port is open on host or timeout (seconds).

    Returns True if the port is reachable within the timeout, False otherwise.
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    """Main function to restart all services."""
    parser = argparse.ArgumentParser(description="Restart Retriva services")
    parser.add_argument("-k", "--kill", action="store_true", help="Kill programs if already running and quit")
    args = parser.parse_args()
    
    # First, kill all services regardless of the restart or kill-only mode. This ensures a clean state.
    print("=" * 60)
    print("Killing all Retriva services...")
    print("=" * 60)
    # Inverted kill order: WebUI -> Gateway -> OpenAI API -> Ingestion API
    kill_process_by_name("retriva-webui")
    kill_process_by_name("retriva_gateway.main")
    kill_process_by_name("retriva.openai_api")
    kill_process_by_name("retriva.ingestion_api")
    print("=" * 60)
    print("All services killed")
    print("=" * 60)

    if args.kill:
        return
    
    print("=" * 60)
    print(f"(Re)starting Retriva services ({DATE_STR})")
    print("=" * 60)
    
    # Note: During a standard restart, each start_* function kills its own 
    # specific target right before launching it. The startup order remains 
    # forward-facing to ensure dependencies (like databases/APIs) are up first.
    
    # Start ingestion_api
    start_retriva_service(
        "ingestion_api",
        RETRIVA_PATH,
        "retriva.ingestion_api",
        VENV_CORE,
        f"{DATE_STR}-ingestion_api.txt"
    )
    
    # Start openai_api
    start_retriva_service(
        "openai_api",
        RETRIVA_PATH,
        "retriva.openai_api",
        VENV_CORE,
        f"{DATE_STR}-openai_api.txt"
    )
    # Wait for core services to become reachable before starting the gateway
    print("Waiting for core services to become available...")
    if not wait_for_tcp("localhost", 8000, timeout=30):
        print("WARNING: ingestion_api not responding on port 8000 after 30s")
    if not wait_for_tcp("localhost", 8001, timeout=30):
        print("WARNING: openai_api not responding on port 8001 after 30s")
    
    # Start retriva-gateway
    start_gateway_service(
        "retriva-gateway",
        GATEWAY_PATH,
        VENV_GATEWAY,
        f"{DATE_STR}-gateway.txt"
    )
    
    # Start npm service
    start_npm_service(
        "retriva-webui",
        WEBUI_PATH,
        f"{DATE_STR}-webui.txt"
    )
    
    print("=" * 60)
    print("All services started")
    print("=" * 60)


if __name__ == "__main__":
    main()
