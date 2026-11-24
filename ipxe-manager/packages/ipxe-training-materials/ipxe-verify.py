#!/usr/bin/env python3
"""Read-only iPXE server acceptance checks."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


CONFIG_DEFAULT = Path("/etc/ipxe-manager/config.json")
OS_ROOT = Path("/srv/ipxe/http/os")
HTTP_ROOT = Path("/srv/ipxe/http")


class Checks:
    def __init__(self) -> None:
        self.failed = False

    def ok(self, message: str) -> None:
        print(f"PASS  {message}")

    def warn(self, message: str) -> None:
        print(f"WARN  {message}")

    def bad(self, message: str) -> None:
        self.failed = True
        print(f"FAIL  {message}")


def command(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, text=True, capture_output=True)
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only checks for a deployed iPXE server")
    parser.add_argument("--config", type=Path, default=CONFIG_DEFAULT, help="Server JSON config file")
    args = parser.parse_args()
    checks = Checks()
    if shutil.which("ipxe-manager"):
        checks.ok("Python ipxe-manager command is available")
    else:
        checks.bad("ipxe-manager command not found")
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        checks.ok(f"Configuration is readable: {args.config}")
    except (OSError, json.JSONDecodeError) as exc:
        checks.bad(f"Configuration unavailable or invalid: {exc}")
        config = None

    for service in ("ipxe-dhcp", "ipxe-http"):
        result = command(["systemctl", "is-active", "--quiet", service])
        if result.returncode == 0:
            checks.ok(f"{service} is active")
        else:
            checks.bad(f"{service} is not active")
    for name in ("ip", "curl", "ss"):
        if shutil.which(name):
            checks.ok(f"Command is available: {name}")
        else:
            checks.bad(f"Command is missing: {name}")

    if isinstance(config, dict) and isinstance(config.get("networks"), list):
        for network in config["networks"]:
            if network.get("enabled", True) is False:
                continue
            iface = network.get("interface", "")
            link = command(["ip", "link", "show", "dev", iface])
            if link.returncode:
                checks.bad(f"Network interface not found: {iface}")
                continue
            checks.ok(f"Network interface exists: {iface}")
            for version in (4, 6):
                address = network.get(f"ipv{version}", "").split("/", 1)[0]
                actual = command(["ip", f"-{version}", "-o", "address", "show", "dev", iface])
                if actual.returncode == 0 and address in actual.stdout:
                    checks.ok(f"{iface} IPv{version} address: {address}")
                else:
                    checks.warn(f"{iface} does not show configured IPv{version} address {address}")

    if isinstance(config, dict):
        port = config.get("http_port", 8080)
        sockets = command(["ss", "-lntup"])
        if sockets.returncode == 0 and f":{port}" in sockets.stdout:
            checks.ok(f"HTTP port {port} is listening")
        else:
            checks.warn(f"No listener detected on HTTP port {port}")

    if OS_ROOT.is_dir():
        checks.ok(f"OS publish directory exists: {OS_ROOT}")
        profile_dir = Path("/etc/ipxe-manager/os.d")
        profiles = sorted(profile_dir.glob("*.json")) if profile_dir.is_dir() else []
        if not profiles:
            checks.warn("No OS has been imported yet")
        for path in profiles:
            try:
                profile = json.loads(path.read_text(encoding="utf-8"))
                os_id, kind = profile["id"], profile["kind"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                checks.bad(f"Invalid OS profile: {path}")
                continue
            root = OS_ROOT / os_id
            if root.is_dir():
                checks.ok(f"OS directory exists: {os_id} ({kind})")
            else:
                checks.bad(f"OS directory missing: {root}")
                continue
            if kind == "rhel":
                required = {
                    ".treeinfo": root / "tree/.treeinfo",
                    "vmlinuz": root / "tree/images/pxeboot/vmlinuz",
                    "initrd.img": root / "tree/images/pxeboot/initrd.img",
                }
                for label, file in required.items():
                    checks.ok(f"{os_id} {label}") if file.is_file() else checks.bad(f"{os_id} missing {label}")
                if any((root / "tree").rglob("repomd.xml")):
                    checks.ok(f"{os_id} local repository metadata")
                else:
                    checks.bad(f"{os_id} missing repodata/repomd.xml")
            elif kind == "ubuntu-live":
                complete = all((root / name).is_file() for name in ("vmlinuz", "initrd", "image.iso"))
                checks.ok(f"{os_id} Ubuntu boot files and ISO") if complete else checks.bad(f"{os_id} files are incomplete")
            elif kind == "custom":
                file = root / "boot.ipxe"
                checks.ok(f"{os_id} custom boot.ipxe") if file.is_file() else checks.bad(f"{os_id} missing boot.ipxe")
    else:
        checks.bad(f"OS publish directory missing: {OS_ROOT}")

    menus = HTTP_ROOT / "menus"
    menu_files = list(menus.glob("*.ipxe")) if menus.is_dir() else []
    if menu_files:
        checks.ok(f"{len(menu_files)} iPXE menu(s) generated")
    else:
        checks.bad("No iPXE menu found")
    print(f"\nAcceptance result: {'FAIL' if checks.failed else 'PASS'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
