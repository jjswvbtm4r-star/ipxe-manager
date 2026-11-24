#!/usr/bin/env python3
"""双栈 iPXE 装机服务管理程序（Python 3，标准库实现）。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence


ETC = Path("/etc/ipxe-manager")
HTTP_ROOT = Path("/srv/ipxe/http")
STATE = Path("/var/lib/ipxe-manager")
DEFAULT_CONFIG = ETC / "config.json"
SERVICES = ("ipxe-http.service", "ipxe-dhcp.service")
INTERFACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,15}$")
OS_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class ManagerError(Exception):
    """可向管理员直接显示的错误。"""


def log(message: str) -> None:
    print(message)


def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def fail(message: str) -> None:
    raise ManagerError(message)


def run(
    command: Sequence[str | os.PathLike[str]],
    *,
    capture: bool = False,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """以参数数组启动系统程序，不经过 shell 字符串解析。"""
    args = [os.fspath(item) for item in command]
    try:
        result = subprocess.run(args, text=True, capture_output=capture, env=env)
    except FileNotFoundError as exc:
        fail(f"Required command not found: {args[0]}")
    except OSError as exc:
        fail(f"Could not start {args[0]}: {exc}")
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f"\n{detail}" if detail else ""
        fail(f"Command failed (exit {result.returncode}): {' '.join(args)}{suffix}")
    return result


def atomic_write(path: Path, content: str | bytes, mode: int = 0o644) -> None:
    """同目录临时文件 + replace，避免中断时留下半份配置。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    payload = content.encode("utf-8") if isinstance(content, str) else content
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        fail(f"File not found: {path}")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"Could not read JSON {path}: {exc}")


def valid_id(value: str) -> None:
    if not isinstance(value, str) or not OS_ID_RE.fullmatch(value):
        fail("OS id must use lowercase letters, numbers, underscore or hyphen (1..64 chars)")


def safe_text(value: str, label: str) -> None:
    if any(token in value for token in ("\n", "\r", "${", "&&", "||", ";")):
        fail(f"Unexpected control syntax in {label}")


def active_networks(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [network for network in config["networks"] if network.get("enabled", True) is not False]


def configure_address(network: dict[str, Any]) -> bool:
    """A missing or null configure_address uses the original default: true."""
    return network.get("configure_address") is not False


def parse_interface(value: str, label: str) -> ipaddress.IPv4Interface | ipaddress.IPv6Interface:
    if not isinstance(value, str):
        fail(f"Invalid {label}: {value}")
    try:
        return ipaddress.ip_interface(value)
    except (TypeError, ValueError):
        fail(f"Invalid {label}: {value}")


def parse_address(value: str, label: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if not isinstance(value, str):
        fail(f"Invalid {label}: {value}")
    try:
        return ipaddress.ip_address(value)
    except (TypeError, ValueError):
        fail(f"Invalid {label}: {value}")


def validate_config(config: Any) -> dict[str, Any]:
    schema = config.get("schema") if isinstance(config, dict) else None
    if isinstance(schema, bool) or schema != 1:
        fail("Unsupported config schema; expected schema: 1")
    port = config.get("http_port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        fail("http_port must be an integer from 1024 through 65535")
    commit = config.get("ipxe_commit", "")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        fail("ipxe_commit must be a pinned 40-character lowercase git commit")
    networks = config.get("networks")
    if not isinstance(networks, list) or not networks:
        fail("networks must be a non-empty array")

    names: set[str] = set()
    enabled_count = 0
    seen_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for network in networks:
        if not isinstance(network, dict):
            fail("Each networks[] item must be an object")
        iface = network.get("interface", "")
        if not isinstance(iface, str) or not INTERFACE_RE.fullmatch(iface) or iface in names:
            fail(f"Invalid or duplicated interface: {iface}")
        names.add(iface)
        if network.get("enabled", True) is False:
            continue
        enabled_count += 1
        configure = network.get("configure_address")
        if configure is None:
            configure = True
        if not isinstance(configure, bool):
            fail("configure_address must be boolean")

        for version in (4, 6):
            cidr = network.get(f"ipv{version}")
            pool = network.get(f"pool{version}")
            if not isinstance(cidr, str) or not isinstance(pool, list) or len(pool) != 2:
                fail(f"Missing IPv{version} settings on {iface}")
            if any(not isinstance(value, str) for value in pool):
                fail(f"DHCP pool addresses must be strings on {iface}")
            interface = parse_interface(cidr, f"IPv{version} CIDR")
            low = parse_address(pool[0], "DHCP pool address")
            high = parse_address(pool[1], "DHCP pool address")
            if interface.version != version or low.version != version or high.version != version:
                fail(f"IP family mismatch in IPv{version} settings on {iface}")
            if version == 6 and interface.network.prefixlen != 64:
                fail("IPv6 SLAAC requires a /64 subnet")
            server = interface.ip
            if server.is_unspecified or server.is_loopback or server.is_link_local or server.is_multicast:
                fail("Server address must be unicast and non-link-local")
            if version == 4 and server in ipaddress.ip_network("0.0.0.0/8"):
                fail("Server address must be unicast and non-link-local")
            if low not in interface.network or high not in interface.network:
                fail(f"DHCP pool is outside the configured IPv{version} subnet on {iface}")
            if int(low) > int(high):
                fail("DHCP pool lower address is greater than upper address")
            if low <= server <= high:
                fail("Server IP is inside DHCP pool")
            if version == 4 and (low == interface.network.network_address or high == interface.network.broadcast_address):
                fail("DHCP pool includes IPv4 network/broadcast address")
            for other in seen_networks:
                if other.version == interface.version and interface.network.overlaps(other):
                    fail("Overlapping subnets across interfaces")
            seen_networks.append(interface.network)
    if enabled_count == 0:
        fail("No enabled provisioning interface")
    return config


def read_config(path: Path) -> dict[str, Any]:
    return validate_config(load_json(path))


def base_url(network: dict[str, Any], version: int, config: dict[str, Any]) -> str:
    host = network[f"ipv{version}"].split("/", 1)[0]
    if version == 6:
        host = f"[{host}]"
    return f"http://{host}:{config['http_port']}"


def boot_name(iface: str, version: int, bios: bool = False) -> str:
    # BIOS 使用带原生驱动的 ipxe.pxe，避免不完整 UNDI 导致 PXE-EC8。
    return f"{iface}-bios.pxe" if bios else f"{iface}-uefi{version}.efi"


def render_dnsmasq(config: dict[str, Any]) -> str:
    lines = [
        "# Generated by ipxe-manager. Edit config.json instead.",
        "port=0", "bind-dynamic", "except-interface=lo", "dhcp-authoritative",
        "log-dhcp", "log-facility=-", "dhcp-leasefile=/var/lib/ipxe-manager/leases",
        "enable-ra", "tftp-root=/srv/ipxe/http/boot", "tftp-no-fail",
        "tftp-port-range=20000,20100", "dhcp-match=set:bios,option:client-arch,0",
        "dhcp-match=set:efi64,option:client-arch,7", "dhcp-match=set:efi64,option:client-arch,9",
        "dhcp-match=set:efi64,option:client-arch,16", "dhcp-vendorclass=set:http4,HTTPClient",
        "dhcp-match=set:efi6,option6:61,00:07", "dhcp-match=set:efi6,option6:61,00:09",
        "dhcp-match=set:http6,option6:61,00:10", "dhcp-userclass=set:ipxe,iPXE",
        "dhcp-match=set:ipxe,175", "dhcp-match=set:ipxe,option6:15,00:04:69:50:58:45",
    ]
    for network in active_networks(config):
        iface = network["interface"]
        ipv4 = parse_interface(network["ipv4"], "IPv4 CIDR")
        ipv6 = network["ipv6"].split("/", 1)[0]
        pool4 = network["pool4"]
        pool6 = network["pool6"]
        url4 = base_url(network, 4, config)
        url6 = base_url(network, 6, config)
        lines.extend([
            f"interface={iface}", f"enable-tftp={iface}",
            f"dhcp-range=set:{iface},{pool4[0]},{pool4[1]},{ipv4.network.netmask},12h",
            f"dhcp-range=set:{iface},{pool6[0]},{pool6[1]},slaac,64,12h",
            f"dhcp-option=tag:{iface},option:router", f"dhcp-option=tag:{iface},option:dns-server",
            f"ra-param={iface},10,0",
            f"dhcp-boot=tag:{iface},tag:!ipxe,tag:bios,{boot_name(iface, 4, True)},,{network['ipv4'].split('/', 1)[0]}",
            f"dhcp-boot=tag:{iface},tag:!ipxe,tag:efi64,tag:!http4,{boot_name(iface, 4)},,{network['ipv4'].split('/', 1)[0]}",
            f"dhcp-boot=tag:{iface},tag:!ipxe,tag:http4,tag:efi64,{url4}/boot/{boot_name(iface, 4)}",
            f"dhcp-option-force=tag:{iface},tag:http4,option:vendor-class,HTTPClient",
            f"dhcp-boot=tag:{iface},tag:ipxe,{url4}/menus/{iface}-4.ipxe",
            f"dhcp-option=tag:{iface},tag:!ipxe,tag:efi6,option6:bootfile-url,tftp://[{ipv6}]/{boot_name(iface, 6)}",
            f"dhcp-option=tag:{iface},tag:!ipxe,tag:http6,option6:bootfile-url,{url6}/boot/{boot_name(iface, 6)}",
            f"dhcp-option=tag:{iface},tag:ipxe,option6:bootfile-url,{url6}/menus/{iface}-6.ipxe",
        ])
    return "\n".join(lines) + "\n"


def render_nginx(config: dict[str, Any]) -> str:
    port = config["http_port"]
    return f"""# Generated by ipxe-manager
user www-data;
worker_processes auto;
pid /run/ipxe-http.pid;
error_log /var/log/ipxe-manager/http-error.log;
events {{ worker_connections 1024; }}
http {{
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    access_log /var/log/ipxe-manager/http-access.log;
    server {{
        listen 0.0.0.0:{port};
        listen [::]:{port} ipv6only=on;
        root /srv/ipxe/http;
        autoindex off;
        location / {{ try_files $uri $uri/ =404; }}
        location ~ /\\.(?!treeinfo$|discinfo$) {{ deny all; }}
    }}
}}
"""


def render_network(network: dict[str, Any]) -> str:
    return f"""# Generated by ipxe-manager; only this interface is managed.
[Match]
Name={network['interface']}
[Link]
RequiredForOnline=no
[Network]
DHCP=no
IPv6AcceptRA=no
LinkLocalAddressing=ipv6
ConfigureWithoutCarrier=yes
Address={network['ipv4']}
Address={network['ipv6']}
"""


def render_configs(config: dict[str, Any], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True, mode=0o755)
    atomic_write(destination / "dnsmasq.conf", render_dnsmasq(config))
    atomic_write(destination / "nginx.conf", render_nginx(config))
    for network in active_networks(config):
        if configure_address(network):
            iface = network["interface"]
            atomic_write(destination / f"05-ipxe-{iface}.network", render_network(network))


def os_profile_files() -> list[Path]:
    directory = ETC / "os.d"
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def load_profiles() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for path in os_profile_files():
        profile = load_json(path)
        if not isinstance(profile, dict):
            fail(f"Invalid OS profile JSON: {path}")
        profile_id = profile.get("id", "")
        valid_id(profile_id)
        if path.stem != profile_id:
            fail(f"OS filename/id mismatch: {path}")
        if profile.get("kind") not in {"ubuntu-live", "rhel", "custom"}:
            fail(f"Unsupported kind in OS profile: {path}")
        if not isinstance(profile.get("title"), str):
            fail(f"OS profile title is missing: {path}")
        safe_text(profile["title"], "title")
        kernel_args = profile.get("kernel_args") or ""
        if not isinstance(kernel_args, str):
            fail(f"kernel_args must be text: {path}")
        safe_text(kernel_args, "kernel args")
        versions = profile.get("ip_versions")
        if not isinstance(versions, list) or any(v not in (4, 6) for v in versions) or not versions:
            fail(f"ip_versions must contain 4 and/or 6: {path}")
        if profile.get("enabled") is not None and not isinstance(profile.get("enabled"), bool):
            fail(f"enabled must be boolean: {path}")
        profiles.append(profile)
    return profiles


def render_menu(network: dict[str, Any], version: int, profiles: list[dict[str, Any]], config: dict[str, Any]) -> None:
    iface = network["interface"]
    base = base_url(network, version, config)
    visible = [p for p in profiles if p.get("enabled") is not False and version in p["ip_versions"]]
    lines = [
        "#!ipxe", f"set base {base}", ":menu", "menu Engine Count - OS installer",
        f"item --gap -- Interface {iface} / IPv{version}",
        "item --gap -- Select an OS to start its installer (no automatic erase)",
    ]
    lines.extend(f"item os-{p['id']} {p['title']}" for p in visible)
    if not visible:
        lines.append("item --gap -- No OS registered for this IP family yet")
    lines.extend([
        "item shell iPXE shell", "item reboot Reboot", "item exit Return to firmware",
        "choose selection || goto exit", "goto ${selection}", ":shell", "shell", "goto menu",
        ":reboot", "reboot", ":exit", "exit",
    ])
    for profile in visible:
        os_id = profile["id"]
        kind = profile["kind"]
        lines.extend([f":os-{os_id}", "imgfree"])
        if kind == "custom":
            lines.extend([f"chain ${{base}}/os/{os_id}/boot.ipxe || goto failed", "goto menu"])
            continue
        sub = f"${{base}}/os/{os_id}"
        if kind == "ubuntu-live":
            kernel_path, initrd_path = "vmlinuz", "initrd"
            args = f"initrd=initrd ip={'dhcp' if version == 4 else 'dhcp6'} boot=casper netboot=url url={sub}/image.iso cloud-config-url=/dev/null"
        else:
            kernel_path, initrd_path = "tree/images/pxeboot/vmlinuz", "tree/images/pxeboot/initrd.img"
            args = f"initrd=initrd.img ip={'dhcp' if version == 4 else 'dhcp6'} rd.neednet=1 inst.repo={sub}/tree"
        extra = profile.get("kernel_args") or ""
        if extra:
            args += f" {extra}"
        lines.extend([
            f"kernel {sub}/{kernel_path} {args} || goto failed",
            f"initrd --name {Path(initrd_path).name} {sub}/{initrd_path} || goto failed",
            "boot || goto failed", "goto menu",
        ])
    lines.extend([
        ":failed", "echo Boot failed. Check the server logs and OS profile.",
        "prompt Press a key to return to menu", "goto menu",
    ])
    atomic_write(HTTP_ROOT / "menus" / f"{iface}-{version}.ipxe", "\n".join(lines) + "\n")


def render_menus(config: dict[str, Any]) -> None:
    profiles = load_profiles()
    menu_dir = HTTP_ROOT / "menus"
    menu_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    for network in active_networks(config):
        for version in (4, 6):
            render_menu(network, version, profiles, config)
    catalog = [
        {"id": profile["id"], "title": profile["title"], "kind": profile["kind"],
         "enabled": profile.get("enabled") is not False, "ip_versions": profile["ip_versions"]}
        for profile in profiles
    ]
    atomic_write(HTTP_ROOT / "catalog.json", json_text(catalog))
    atomic_write(
        HTTP_ROOT / "index.html",
        '<!doctype html><meta charset="utf-8"><title>iPXE Server</title>'
        '<h1>iPXE Server</h1><p>Service is running. OS catalog: '
        '<a href="/catalog.json">catalog.json</a></p>\n',
    )


def system_active(service: str) -> bool:
    return run(["systemctl", "is-active", "--quiet", service], check=False).returncode == 0


def check_host(config: dict[str, Any]) -> None:
    os_release = Path("/etc/os-release")
    if not os_release.is_file():
        fail("Missing /etc/os-release")
    values: dict[str, str] = {}
    for line in os_release.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"').strip("'")
    if values.get("ID") not in {"ubuntu", "debian"}:
        fail("Automatic deployment supports Ubuntu/Debian only; use render for another distribution")

    protected: set[str] = set()
    for command in (["ip", "-j", "route", "show", "default"], ["ip", "-j", "-6", "route", "show", "default"]):
        result = run(command, capture=True)
        try:
            protected.update(route["dev"] for route in json.loads(result.stdout) if route.get("dev"))
        except (json.JSONDecodeError, KeyError, TypeError):
            fail("Could not read default routes from ip -j")
    ssh_connection = os.environ.get("SSH_CONNECTION", "").split()
    if len(ssh_connection) >= 3:
        server_address = ssh_connection[2]
        addresses = json.loads(run(["ip", "-j", "address"], capture=True).stdout)
        for interface in addresses:
            if any(item.get("local") == server_address for item in interface.get("addr_info", [])):
                protected.add(interface["ifname"])

    all_addresses = json.loads(run(["ip", "-j", "address"], capture=True).stdout)
    for network in active_networks(config):
        iface = network["interface"]
        if iface in protected:
            fail(f"{iface} is a default-route/SSH interface; use a separate provisioning interface")
        if not Path("/sys/class/net", iface).exists():
            fail(f"Interface not found: {iface}")
        expected_networks = [parse_interface(network[f"ipv{version}"], f"IPv{version} CIDR").network for version in (4, 6)]
        for existing in all_addresses:
            if existing.get("ifname") == iface:
                continue
            for info in existing.get("addr_info", []):
                if info.get("scope") != "global":
                    continue
                current = parse_interface(f"{info['local']}/{info['prefixlen']}", "existing interface CIDR")
                if any(current.network.version == proposed.version and current.network.overlaps(proposed) for proposed in expected_networks):
                    fail(f"Subnet overlaps existing interface {existing.get('ifname')}: {current}")

    existing_network_files = list(Path("/etc/systemd/network").glob("05-ipxe-*.network"))
    needs_networkd = bool(existing_network_files) or any(configure_address(n) for n in active_networks(config))
    if needs_networkd and not system_active("systemd-networkd"):
        fail("Applying or removing iPXE networkd files needs an active systemd-networkd; inspect existing 05-ipxe-*.network files and the network manager before proceeding")
    for service in ("dnsmasq", "isc-dhcp-server", "kea-dhcp4-server", "kea-dhcp6-server"):
        if system_active(service):
            fail(f"Existing {service} service detected; resolve binding before deploy")


def install_dependencies() -> None:
    had_nginx = shutil.which("nginx") is not None
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    run(["apt-get", "update"], env=env)
    run([
        "apt-get", "install", "-y", "--no-install-recommends", "dnsmasq-base", "nginx-core",
        "git", "build-essential", "liblzma-dev", "xorriso", "ca-certificates", "curl",
    ], env=env)
    if not had_nginx:
        run(["systemctl", "disable", "--now", "nginx"], check=False)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_ipxe(config: dict[str, Any]) -> None:
    commit = config["ipxe_commit"]
    active = active_networks(config)
    canonical_networks = json.loads(json.dumps(active, sort_keys=True))
    fingerprint_data = {"commit": commit, "port": config["http_port"], "networks": canonical_networks, "builder": 3}
    fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = ETC / "boot-manifest.json"
    if manifest.is_file():
        saved = load_json(manifest)
        if saved.get("fingerprint") == fingerprint:
            if all((HTTP_ROOT / "boot" / name).is_file() and sha256_file(HTTP_ROOT / "boot" / name) == digest for name, digest in saved.get("files", {}).items()):
                log("Boot artifacts unchanged; verified checksums and reused.")
                return

    source = STATE / "ipxe-source"
    if not (source / ".git").is_dir():
        run(["git", "clone", "https://github.com/ipxe/ipxe.git", str(source)])
    current = run(["git", "-C", str(source), "rev-parse", "HEAD"], capture=True).stdout.strip()
    if current != commit:
        exists = run(["git", "-C", str(source), "cat-file", "-e", commit], check=False).returncode == 0
        if not exists:
            run(["git", "-C", str(source), "fetch", "origin", commit])
        run(["git", "-C", str(source), "checkout", "--detach", commit])

    local_config = source / "src" / "config" / "local"
    local_config.mkdir(parents=True, exist_ok=True)
    atomic_write(local_config / "general.h", "#define NET_PROTO_IPV6\n#define DOWNLOAD_PROTO_HTTP\n#define DOWNLOAD_PROTO_TFTP\n")
    atomic_write(local_config / "console.h", "#ifdef PLATFORM_pcbios\n#define CONSOLE_SERIAL\n#endif\n")
    atomic_write(local_config / "serial.h", "#undef COMSPEED\n#define COMSPEED 115200\n")
    jobs = min(os.cpu_count() or 2, 8)
    files: dict[str, str] = {}
    for network in active:
        iface = network["interface"]
        for version, bios in ((4, True), (4, False), (6, False)):
            url = base_url(network, version, config)
            bootstrap = (
                "#!ipxe\n"
                f"ifconf --configurator {'dhcp' if version == 4 else 'ipv6'} || goto failed\n"
                f"chain {url}/menus/{iface}-{version}.ipxe || goto failed\n"
                ":failed\n"
                "echo Network boot failed. Use ifstat, route and config to diagnose.\n"
                "shell\n"
            )
            embed = STATE / "bootstrap.ipxe"
            atomic_write(embed, bootstrap)
            target = "bin/ipxe.pxe" if bios else "bin-x86_64-efi/snponly.efi"
            name = boot_name(iface, version, bios)
            run(["make", "-C", str(source / "src"), "-j", str(jobs), target, f"EMBED={embed}"])
            output = HTTP_ROOT / "boot" / name
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            shutil.copy2(source / "src" / target, output)
            os.chmod(output, 0o644)
            files[name] = sha256_file(output)
    atomic_write(manifest, json_text({"commit": commit, "fingerprint": fingerprint, "files": files}))


def write_units() -> None:
    atomic_write(Path("/etc/systemd/system/ipxe-dhcp.service"), """[Unit]
Description=iPXE dedicated DHCPv4/DHCPv6/RA/TFTP
After=network.target
[Service]
Type=simple
ExecStartPre=/usr/sbin/dnsmasq --test --conf-file=/etc/ipxe-manager/dnsmasq.conf
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file=/etc/ipxe-manager/dnsmasq.conf
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
""")
    atomic_write(Path("/etc/systemd/system/ipxe-http.service"), """[Unit]
Description=iPXE dedicated HTTP image server
After=network.target
[Service]
Type=simple
ExecStartPre=/usr/sbin/nginx -t -c /etc/ipxe-manager/nginx.conf
ExecStart=/usr/sbin/nginx -c /etc/ipxe-manager/nginx.conf -g 'daemon off;'
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
""")
    atomic_write(Path("/etc/logrotate.d/ipxe-manager"), """/var/log/ipxe-manager/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 www-data adm
    sharedscripts
    postrotate
        /bin/systemctl kill -s USR1 --kill-who=main ipxe-http.service >/dev/null 2>&1 || true
    endscript
}
""")


def backup_targets(config: dict[str, Any]) -> list[Path]:
    targets = [
        ETC / "dnsmasq.conf", ETC / "nginx.conf", ETC / "config.json", ETC / "boot-manifest.json",
        Path("/etc/logrotate.d/ipxe-manager"), Path("/etc/systemd/system/ipxe-http.service"),
        Path("/etc/systemd/system/ipxe-dhcp.service"),
    ]
    for network in active_networks(config):
        if configure_address(network):
            targets.append(Path("/etc/systemd/network") / f"05-ipxe-{network['interface']}.network")
    targets.extend(Path("/etc/systemd/network").glob("05-ipxe-*.network"))
    return list(dict.fromkeys(targets))


def make_backup(directory: Path, config: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    records = []
    for index, target in enumerate(backup_targets(config)):
        saved = None
        if target.exists() or target.is_symlink():
            destination = directory / str(index)
            if target.is_dir() and not target.is_symlink():
                shutil.copytree(target, destination, symlinks=True)
            else:
                shutil.copy2(target, destination, follow_symlinks=False)
            saved = str(index)
        records.append({"path": str(target), "backup": saved})
    services = {service: system_active(service) for service in SERVICES}
    atomic_write(directory / "manifest.json", json_text({"files": records, "services": services}))
    for name in ("boot", "menus"):
        source = HTTP_ROOT / name
        if source.is_dir():
            shutil.copytree(source, directory / name, symlinks=True)


def restore_backup(backup: Path) -> None:
    try:
        canonical = backup.resolve(strict=True)
        parent = (STATE / "backups").resolve(strict=True)
    except OSError:
        fail(f"Backup not found: {backup}")
    if canonical.parent != parent or not (canonical / "manifest.json").is_file():
        fail("Backup must be directly under /var/lib/ipxe-manager/backups")
    manifest = load_json(canonical / "manifest.json")
    run(["systemctl", "stop", *SERVICES], check=False)
    for record in manifest.get("files", []):
        target = Path(record["path"])
        saved = record.get("backup")
        if saved is None:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        else:
            saved_name = Path(saved)
            source = canonical / saved_name
            if saved_name.name != saved or saved_name.parent != Path(".") or not source.exists() and not source.is_symlink():
                fail("Invalid path in backup manifest")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
                continue
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target, follow_symlinks=False)
    for name in ("boot", "menus"):
        target = HTTP_ROOT / name
        saved = canonical / name
        if target.exists():
            shutil.rmtree(target)
        if saved.is_dir():
            shutil.copytree(saved, target, symlinks=True)
    run(["systemctl", "daemon-reload"], check=False)
    network_paths = [record["path"] for record in manifest.get("files", []) if record.get("path", "").endswith(".network")]
    if network_paths:
        if system_active("systemd-networkd"):
            run(["networkctl", "reload"], check=False)
            for path in network_paths:
                iface = Path(path).stem.removeprefix("05-ipxe-")
                result = run(["networkctl", "reconfigure", iface], check=False)
                if result.returncode:
                    warn(f"Could not reconfigure {iface} after rollback")
        else:
            warn("Network files were restored, but systemd-networkd is inactive; verify and apply them using the appropriate network manager")
    for service, was_active in manifest.get("services", {}).items():
        if was_active:
            run(["systemctl", "start", service], check=False)
        else:
            run(["systemctl", "disable", service], check=False)
    log("Restored service/network configuration and boot artifacts. OS data retained.")


def status_check(config: dict[str, Any], strict: bool = False) -> bool:
    okay = True
    for service in SERVICES:
        result = run(["systemctl", "is-active", service], check=False)
        if result.returncode == 0:
            log(f"{service}: active")
        else:
            print(f"{service}: {result.stdout.strip() or 'inactive'}")
            okay = False
    for network in active_networks(config):
        iface = network["interface"]
        address = run(["ip", "-br", "address", "show", "dev", iface], check=False)
        if address.returncode:
            warn(f"Interface not available: {iface}")
            okay = False
            continue
        print(address.stdout.strip())
        details = json.loads(run(["ip", "-j", "address", "show", "dev", iface], capture=True).stdout)
        flags = details[0].get("flags", []) if details else []
        for version in (4, 6):
            url = f"{base_url(network, version, config)}/menus/{iface}-{version}.ipxe"
            response = run(["curl", "--noproxy", "*", "--fail", "--silent", "--show-error", "--max-time", "5", url], capture=True, check=False)
            if response.returncode == 0 and response.stdout.startswith("#!ipxe"):
                log(f"HTTP OK: {url}")
            elif version == 6 and "LOWER_UP" not in flags:
                log(f"IPv6 interface probe pending carrier/DAD: {url}")
            else:
                warn(f"HTTP FAILED: {url}")
                okay = False
        if "LOWER_UP" not in flags:
            log(f"PENDING physical link: {iface}; client boot needs a connected port")
    if strict and not okay:
        fail("Service health check failed")
    return okay


def deploy_all(config: dict[str, Any], skip_packages: bool) -> None:
    check_host(config)
    (ETC / "os.d").mkdir(parents=True, exist_ok=True, mode=0o755)
    (HTTP_ROOT / "os").mkdir(parents=True, exist_ok=True, mode=0o755)
    (HTTP_ROOT / "menus").mkdir(parents=True, exist_ok=True, mode=0o755)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o755)
    Path("/var/log/ipxe-manager").mkdir(parents=True, exist_ok=True, mode=0o755)
    if not skip_packages:
        install_dependencies()
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    backup = STATE / "backups" / stamp
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    make_backup(backup, config)
    try:
        build_ipxe(config)
        staging = backup / "rendered"
        render_configs(config, staging)
        run(["dnsmasq", "--test", f"--conf-file={staging / 'dnsmasq.conf'}"])
        run(["nginx", "-t", "-c", str(staging / "nginx.conf")])
        atomic_write(ETC / "config.json", json_text(config), 0o600)
        shutil.copy2(staging / "dnsmasq.conf", ETC / "dnsmasq.conf")
        shutil.copy2(staging / "nginx.conf", ETC / "nginx.conf")
        write_units()
        for network in active_networks(config):
            if configure_address(network):
                path = Path("/etc/systemd/network") / f"05-ipxe-{network['interface']}.network"
                atomic_write(path, render_network(network))
        wanted = {
            f"05-ipxe-{network['interface']}.network"
            for network in active_networks(config) if configure_address(network)
        }
        removed_ifaces = []
        for path in Path("/etc/systemd/network").glob("05-ipxe-*.network"):
            if path.name not in wanted:
                removed_ifaces.append(path.stem.removeprefix("05-ipxe-"))
                path.unlink()
        if wanted or removed_ifaces:
            run(["networkctl", "reload"])
            for iface in removed_ifaces:
                run(["networkctl", "reconfigure", iface])
        for network in active_networks(config):
            iface = network["interface"]
            if configure_address(network):
                run(["networkctl", "reconfigure", iface])
            else:
                details = json.loads(run(["ip", "-j", "address", "show", "dev", iface], capture=True).stdout)
                actual = {item.get("local") for item in details[0].get("addr_info", [])} if details else set()
                for version in (4, 6):
                    cidr = network[f"ipv{version}"]
                    expected = cidr.split("/", 1)[0]
                    if expected not in actual:
                        fail(f"Missing preconfigured address: {cidr}")
        render_menus(config)
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", *SERVICES])
        run(["systemctl", "restart", *SERVICES])
        time.sleep(2)
        status_check(config, strict=True)
    except Exception:
        warn(f"Deployment failed; restoring configuration snapshot: {backup}")
        try:
            restore_backup(backup)
        except Exception as rollback_error:
            warn(f"Automatic rollback also failed: {rollback_error}")
        raise
    log(f"Deployment complete. Config backup: {backup}")


def add_os(config: dict[str, Any], os_id: str, kind: str, source: Path, title: str, expected_sha: str, kernel_args: str) -> None:
    valid_id(os_id)
    safe_text(title, "title")
    safe_text(kernel_args, "kernel args")
    target = HTTP_ROOT / "os" / os_id
    profile_path = ETC / "os.d" / f"{os_id}.json"
    if target.exists() or profile_path.exists():
        fail("OS id already exists; choose a versioned id or remove it first")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o755)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    profile_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    stage = Path(tempfile.mkdtemp(prefix="import-", dir=STATE))
    data = stage / "data"
    data.mkdir()
    digest = ""
    try:
        if kind == "custom":
            resolved = source.resolve(strict=True)
            if not resolved.is_dir() or not (resolved / "boot.ipxe").is_file():
                fail("Custom source must be a directory containing boot.ipxe")
            if any(path.is_symlink() for path in resolved.rglob("*")):
                fail("Custom source may not contain symlinks")
            with (resolved / "boot.ipxe").open("rb") as stream:
                if stream.read(6) != b"#!ipxe":
                    fail("Custom boot.ipxe must begin with #!ipxe")
            shutil.copytree(resolved, data, dirs_exist_ok=True)
        else:
            resolved = source.resolve(strict=True)
            if not resolved.is_file():
                fail("ISO file not found")
            digest = sha256_file(resolved)
            if expected_sha and digest.lower() != expected_sha.lower():
                fail("ISO SHA256 mismatch")
            if kind == "ubuntu-live":
                run(["xorriso", "-osirrox", "on", "-indev", str(resolved), "-extract", "/casper/vmlinuz", str(data / "vmlinuz")])
                run(["xorriso", "-osirrox", "on", "-indev", str(resolved), "-extract", "/casper/initrd", str(data / "initrd")])
                shutil.copyfile(resolved, data / "image.iso")
            else:
                run(["xorriso", "-osirrox", "on", "-indev", str(resolved), "-extract", "/", str(data / "tree")])
                for required in (".treeinfo", "images/pxeboot/vmlinuz", "images/pxeboot/initrd.img"):
                    if not (data / "tree" / required).is_file():
                        fail(f"RHEL-compatible installation tree missing {required}")
                if not any((data / "tree").rglob("repomd.xml")):
                    fail("Use a full RHEL/Rocky/Alma DVD ISO with local repositories, not boot-only media")
        data_root = data.resolve()
        for root, directories, files in os.walk(data, followlinks=False):
            root_path = Path(root)
            for name in directories + files:
                path = root_path / name
                if path.is_symlink():
                    try:
                        path.resolve(strict=True).relative_to(data_root)
                    except (OSError, ValueError):
                        fail("ISO contains symlink escaping its extraction directory")
                elif path.is_dir():
                    path.chmod(0o755)
                else:
                    path.chmod(0o644)
        shutil.move(str(data), str(target))
        profile: dict[str, Any] = {
            "id": os_id, "title": title, "kind": kind, "enabled": True,
            "kernel_args": kernel_args, "ip_versions": [4, 6],
        }
        if digest:
            profile["sha256"] = digest
        atomic_write(profile_path, json_text(profile))
        render_menus(config)
        log(f"Added OS: {os_id}; IPv4/6")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def remove_os(config: dict[str, Any], os_id: str) -> None:
    valid_id(os_id)
    profile = ETC / "os.d" / f"{os_id}.json"
    data = HTTP_ROOT / "os" / os_id
    if not profile.is_file():
        fail("Unknown OS id")
    trash = STATE / "trash" / f"{os_id}-{time.time_ns()}"
    trash.mkdir(parents=True, exist_ok=True, mode=0o755)
    shutil.move(str(profile), trash / profile.name)
    render_menus(config)
    if data.exists():
        shutil.move(str(data), trash / "data")
    log(f"Removed from catalog; files archived at {trash}")


def set_os_enabled(config: dict[str, Any], os_id: str, enabled: bool) -> None:
    valid_id(os_id)
    profile_path = ETC / "os.d" / f"{os_id}.json"
    profile = load_json(profile_path)
    profile["enabled"] = enabled
    atomic_write(profile_path, json_text(profile))
    render_menus(config)


def list_os() -> None:
    print(json_text([load_json(path) for path in os_profile_files()]), end="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipxe-manager",
        description="Python 3 双栈 iPXE 装机服务管理器（只使用 Python 标准库）",
        epilog="首次安装：sudo python3 install.py\n已有安装：sudo ipxe-manager deploy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="指定 JSON 配置文件")
    commands = parser.add_subparsers(dest="command", required=True)
    deploy = commands.add_parser("deploy", help="部署或更新 PXE 服务")
    deploy.add_argument("--skip-packages", action="store_true", help="跳过 apt 软件包安装")
    commands.add_parser("status", help="检查服务、网卡和 HTTP 菜单")
    render = commands.add_parser("render", help="只生成配置，不写系统目录")
    render.add_argument("--output", type=Path, required=True, help="生成文件的目标目录")
    commands.add_parser("list-os", help="列出已导入的 OS")
    commands.add_parser("refresh-menu", help="重新生成全部 iPXE 菜单")
    add = commands.add_parser("add-os", help="导入 Ubuntu、RHEL 兼容或自定义 OS")
    add.add_argument("id", help="OS 唯一 ID；小写字母、数字、下划线或短横线")
    add.add_argument("--kind", required=True, choices=("ubuntu-live", "rhel", "custom"), help="安装模板类型")
    add.add_argument("--source", required=True, type=Path, help="ISO 文件；custom 使用含 boot.ipxe 的目录")
    add.add_argument("--title", required=True, help="PXE 菜单显示名称")
    add.add_argument("--sha256", default="", help="可选 ISO SHA256 校验值")
    add.add_argument("--kernel-args", default="", help="可选的额外内核启动参数")
    for command in ("remove-os", "enable-os", "disable-os"):
        item = commands.add_parser(command, help={"remove-os": "移除菜单并归档 OS 文件", "enable-os": "重新加入菜单", "disable-os": "暂时从菜单隐藏"}[command])
        item.add_argument("id", help="OS 唯一 ID")
    rollback = commands.add_parser("rollback", help="恢复部署快照")
    rollback.add_argument("backup", type=Path, help="/var/lib/ipxe-manager/backups 下的快照目录")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            config = read_config(args.config)
            render_configs(config, args.output)
            return 0
        if os.geteuid() != 0:
            fail("Run as root")
        STATE.mkdir(parents=True, exist_ok=True, mode=0o755)
        lock_path = STATE / "manager.lock"
        with lock_path.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail("Another ipxe-manager operation is running")
            config = read_config(args.config)
            if args.command == "deploy":
                deploy_all(config, args.skip_packages)
            elif args.command == "status":
                status_check(config)
            elif args.command == "list-os":
                list_os()
            elif args.command == "refresh-menu":
                render_menus(config)
            elif args.command == "add-os":
                if args.sha256 and not SHA256_RE.fullmatch(args.sha256):
                    fail("--sha256 must be 64 hexadecimal characters")
                add_os(config, args.id, args.kind, args.source, args.title, args.sha256, args.kernel_args)
            elif args.command == "remove-os":
                remove_os(config, args.id)
            elif args.command == "enable-os":
                set_os_enabled(config, args.id, True)
            elif args.command == "disable-os":
                set_os_enabled(config, args.id, False)
            elif args.command == "rollback":
                restore_backup(args.backup)
            else:
                parser.error(f"Unknown command: {args.command}")
        return 0
    except ManagerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: filesystem or process operation failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
