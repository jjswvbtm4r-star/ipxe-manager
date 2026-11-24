#!/usr/bin/env python3
"""Install the Python 3 manager and start the first deployment."""

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path


APP = Path("/opt/ipxe-manager")
CONFIG_DIR = Path("/etc/ipxe-manager")
CONFIG = CONFIG_DIR / "config.json"
ENTRY = Path("/usr/local/sbin/ipxe-manager")


def install_copy(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Install and deploy the Python 3 iPXE manager")
    parser.add_argument("--config-example", type=Path, help="Use this example JSON if no server config exists")
    parser.add_argument("--skip-packages", action="store_true", help="Do not install Ubuntu/Debian packages")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run with sudo/root: sudo python3 install.py")
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required")

    source_dir = Path(__file__).resolve().parent
    manager_source = source_dir / "ipxe-manager.py"
    example_source = args.config_example or source_dir / "config.example.json"
    if not manager_source.is_file():
        parser.error(f"Missing manager source file: {manager_source}")
    APP.mkdir(parents=True, exist_ok=True, mode=0o755)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o755)
    (CONFIG_DIR / "os.d").mkdir(parents=True, exist_ok=True, mode=0o755)
    installed_manager = APP / "ipxe-manager.py"
    install_copy(manager_source, installed_manager, 0o755)
    if ENTRY.exists() or ENTRY.is_symlink():
        ENTRY.unlink()
    ENTRY.symlink_to(installed_manager)

    if not CONFIG.exists():
        if not example_source.is_file():
            parser.error(f"Configuration example not found: {example_source}")
        install_copy(example_source, CONFIG, 0o600)
        print(f"Created initial configuration: {CONFIG}")
    else:
        print(f"Keeping existing configuration: {CONFIG}")

    command = [str(installed_manager), "deploy"]
    if args.skip_packages:
        command.append("--skip-packages")
    os.execv(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
