#!/usr/bin/env python3
"""Refresh the deploy/training folders and build their ZIP archives."""

from pathlib import Path
from shutil import copy2
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ROOT / "packages"


def sync_folder(folder: Path, mapping: dict[str, Path]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.sh"):
        old.unlink()
    for name, source in mapping.items():
        copy2(source, folder / name)


def build_zip(target: Path, folder: Path, top_level: str) -> None:
    with ZipFile(target, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in folder.iterdir() if item.is_file()):
            info = ZipInfo(f"{top_level}/{path.name}")
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            mode = 0o755 if path.name in {"install.py", "ipxe-manager.py", "ipxe-verify.py"} else 0o644
            info.external_attr = (mode & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes())


def main() -> None:
    one_click = PACKAGES / "ipxe-one-click-deploy"
    sync_folder(one_click, {
        "README.md": ROOT / "README.md",
        "config.example.json": ROOT / "config.example.json",
        "install.py": ROOT / "install.py",
        "ipxe-manager.py": ROOT / "ipxe-manager.py",
        "ipxe-verify.py": ROOT / "ipxe-verify.py",
    })
    training = PACKAGES / "ipxe-training-materials"
    sync_folder(training, {
        "MANUAL_DEPLOYMENT_TRAINING.md": ROOT / "MANUAL_DEPLOYMENT_TRAINING.md",
        "ipxe-verify.py": ROOT / "ipxe-verify.py",
    })

    build_zip(PACKAGES / "ipxe-one-click-deploy.zip", one_click, "ipxe-one-click-deploy")
    build_zip(PACKAGES / "ipxe-training-materials.zip", training, "ipxe-training-materials")
    main_folder = PACKAGES / "ipxe-manager"
    sync_folder(main_folder, {
        "README.md": ROOT / "README.md",
        "MANUAL_DEPLOYMENT_TRAINING.md": ROOT / "MANUAL_DEPLOYMENT_TRAINING.md",
        "VERIFICATION.md": ROOT / "VERIFICATION.md",
        "config.example.json": ROOT / "config.example.json",
        "install.py": ROOT / "install.py",
        "ipxe-manager.py": ROOT / "ipxe-manager.py",
        "ipxe-verify.py": ROOT / "ipxe-verify.py",
    })
    build_zip(PACKAGES / "ipxe-manager.zip", main_folder, "ipxe-manager")
    print("Built Python 3 iPXE packages:")
    for path in (PACKAGES / "ipxe-manager.zip", PACKAGES / "ipxe-one-click-deploy.zip", PACKAGES / "ipxe-training-materials.zip"):
        print(f"  {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
