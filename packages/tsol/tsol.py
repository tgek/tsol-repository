#!/usr/bin/env python3
"""Tsol — the TsolOS package manager."""

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.error
from pathlib import Path, PurePosixPath
from urllib.request import urlopen

VERSION = "0.13.0"

PROJECT_DIR = Path.home() / "TsolOS"
PACKAGES_DIR = PROJECT_DIR / "packages"
REPO_DIR = PROJECT_DIR / "repository"
REPO_FILE = REPO_DIR / "packages.json"
ROOT_DIR = PROJECT_DIR / "test-root"
STATE_FILE = ROOT_DIR / "var/lib/tsol/installed.json"

REMOTE_REPO_URL = (
    "https://raw.githubusercontent.com/tgek/tsol-repository/"
    "main/repository/packages.json"
)
REMOTE_ARCHIVE_BASE = (
    "https://raw.githubusercontent.com/tgek/tsol-repository/"
    "main/repository"
)


def read_json(path, default=None):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.write("\n")
    temporary.replace(path)


def load_repo():
    data = read_json(REPO_FILE, {"repository": "Tsol Official", "version": 1,
                                 "packages": {}})
    if not isinstance(data, dict):
        raise RuntimeError("Invalid repository metadata.")

    packages = data.get("packages")
    if isinstance(packages, list):
        converted = {}
        for package in packages:
            if not isinstance(package, dict) or not package.get("name"):
                raise RuntimeError("Invalid package entry in repository.")
            name = package["name"]
            if name in converted:
                raise RuntimeError(f"Duplicate package: {name}")
            converted[name] = package
        data["packages"] = converted
    elif not isinstance(packages, dict):
        raise RuntimeError("Repository metadata has no valid packages.")

    return data


def save_repo(data):
    write_json(REPO_FILE, data)


def load_state():
    state = read_json(STATE_FILE, {"packages": {}})
    if not isinstance(state, dict) or not isinstance(state.get("packages"), dict):
        raise RuntimeError("Invalid installed package database.")
    return state


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(value):
    path = PurePosixPath(str(value))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise RuntimeError(f"Unsafe package path: {value}")
    return path


def package_manifest(name):
    path = PACKAGES_DIR / name / "tsol.json"
    if not path.is_file():
        raise RuntimeError(f"Missing package manifest: {path}")
    manifest = read_json(path)
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Invalid manifest for {name}")
    if manifest.get("name", name) != name:
        raise RuntimeError(f"Manifest name does not match {name}")
    if not manifest.get("version"):
        raise RuntimeError(f"Manifest for {name} has no version")
    manifest.setdefault("name", name)
    manifest.setdefault("description", "")
    manifest.setdefault("dependencies", [])
    return manifest


def package_archive_path(package):
    archive = package.get("archive")
    if not archive:
        raise RuntimeError(f"No archive available for {package.get('name')}")
    return REPO_DIR / Path(archive).name


def download_bytes(url):
    request = __import__("urllib.request", fromlist=["Request"]).Request(
        url, headers={"User-Agent": f"Tsol/{VERSION}"}
    )
    with urlopen(request, timeout=20) as response:
        return response.read()


def update_repository():
    print("Fetching Tsol repository index...")
    try:
        raw = download_bytes(REMOTE_REPO_URL)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Online repository index is not an object.")

        packages = data.get("packages")
        if isinstance(packages, list):
            converted = {}
            for package in packages:
                if not isinstance(package, dict) or not package.get("name"):
                    raise RuntimeError("Invalid online package entry.")
                name = package["name"]
                if name in converted:
                    raise RuntimeError(f"Duplicate online package: {name}")
                converted[name] = package
            data["packages"] = converted
        elif isinstance(packages, dict):
            for name, package in packages.items():
                if not isinstance(package, dict):
                    raise RuntimeError(f"Invalid package entry: {name}")
                if package.get("name", name) != name:
                    raise RuntimeError(f"Package name mismatch: {name}")
                package.setdefault("name", name)
        else:
            raise RuntimeError("Online repository has no valid packages.")

        for name, package in data["packages"].items():
            if not isinstance(name, str) or not name or not isinstance(package, dict):
                raise RuntimeError("Invalid online package metadata.")
            if package.get("status", "available") == "available":
                if not package.get("archive") or not package.get("sha256"):
                    raise RuntimeError(
                        f"Available package {name} is missing archive/checksum metadata."
                    )

        save_repo(data)
    except Exception as error:
        raise RuntimeError(f"Repository update failed: {error}") from error

    available = sum(
        p.get("status", "available") == "available"
        for p in data["packages"].values()
    )
    planned = sum(
        p.get("status") == "planned" for p in data["packages"].values()
    )
    print("Tsol repository updated successfully.")
    print(f"Source: {REMOTE_REPO_URL}")
    print(f"Available packages: {available}")
    print(f"Planned packages: {planned}")


def package_file_sources(name):
    directory = PACKAGES_DIR / name / "files"
    if not directory.is_dir():
        raise RuntimeError(f"Missing package files directory: {directory}")
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    if not files:
        raise RuntimeError(f"Package {name} contains no files.")
    return files


def build_package(name):
    manifest = package_manifest(name)
    files = package_file_sources(name)
    archive_name = f"{name}-{manifest['version']}.tsol"
    archive_path = REPO_DIR / archive_name
    REPO_DIR.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "w:gz") as archive:
        for path in files:
            relative = path.relative_to(PACKAGES_DIR / name / "files")
            safe_relative_path(relative.as_posix())
            info = archive.gettarinfo(str(path), arcname=relative.as_posix())
            info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
            with path.open("rb") as file:
                archive.addfile(info, file)

    data = load_repo()
    old = data["packages"].get(name, {})
    entry = {
        "name": name,
        "version": str(manifest["version"]),
        "description": manifest.get("description", ""),
        "status": "available",
        "source": f"packages/{name}/files",
        "archive": archive_name,
        "sha256": sha256_file(archive_path),
        "dependencies": manifest.get("dependencies", []),
    }
    data["packages"][name] = entry
    save_repo(data)

    print(f"Built {name} {entry['version']}")
    print(f"Archive: {archive_path}")
    print(f"SHA-256: {entry['sha256']}")
    print(f"Dependencies: {', '.join(entry['dependencies']) or 'none'}")
    print("Repository metadata updated.")


def verify_package(name):
    data = load_repo()
    package = data["packages"].get(name)
    if not package:
        raise RuntimeError(f"Package not found: {name}")
    archive = package_archive_path(package)
    if not archive.is_file():
        raise RuntimeError(f"Archive not found: {archive}")
    actual = sha256_file(archive)
    expected = package.get("sha256")
    if not expected or actual.lower() != expected.lower():
        raise RuntimeError(f"Checksum verification failed for {name}")
    print(f"Checksum verified: {name}")
    print(f"Archive: {archive}")
    print(f"SHA-256: {actual}")


def get_archive(package):
    archive = package_archive_path(package)
    expected = package.get("sha256")
    if archive.is_file() and expected and sha256_file(archive) == expected:
        return archive.read_bytes()

    url = f"{REMOTE_ARCHIVE_BASE}/{Path(package['archive']).name}"
    print(f"Downloading {package['name']} from GitHub...")
    try:
        contents = download_bytes(url)
    except Exception as error:
        raise RuntimeError(f"Could not download {package['name']}: {error}") from error

    actual = hashlib.sha256(contents).hexdigest()
    if not expected or actual.lower() != expected.lower():
        raise RuntimeError(f"Checksum verification failed for downloaded {package['name']}")

    return contents


def stage_archive(package):
    contents = get_archive(package)
    staged = {}

    try:
        with tarfile.open(fileobj=io.BytesIO(contents), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    if member.isdir():
                        continue
                    raise RuntimeError(
                        f"Unsupported archive entry in {package['name']}: {member.name}"
                    )

                relative = safe_relative_path(member.name)
                if relative.as_posix() in staged:
                    raise RuntimeError(f"Duplicate archive path: {relative}")

                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"Cannot read archive entry: {relative}")

                staged[relative.as_posix()] = (
                    stream.read(),
                    member.mode & 0o777,
                )
    except (tarfile.TarError, OSError) as error:
        raise RuntimeError(f"Invalid archive for {package['name']}: {error}") from error

    if not staged:
        raise RuntimeError(f"Package archive is empty: {package['name']}")
    return staged


def dependency_order(name, packages, visiting=None, visited=None):
    visiting = set() if visiting is None else visiting
    visited = set() if visited is None else visited

    if name in visited:
        return []
    if name in visiting:
        raise RuntimeError(f"Dependency cycle detected at {name}")
    if name not in packages:
        raise RuntimeError(f"Package not found: {name}")

    package = packages[name]
    if package.get("status", "available") != "available":
        raise RuntimeError(f"Package {name} is not available.")

    visiting.add(name)
    order = []
    for dependency in package.get("dependencies", []):
        order.extend(dependency_order(dependency, packages, visiting, visited))
    visiting.remove(name)
    visited.add(name)
    order.append(name)
    return order


def install_one(name, package, staged, automatic=False):
    state = load_state()
    installed = state["packages"]
    if name in installed:
        print(f"{name} {installed[name].get('version', '?')} is already installed.")
        return

    entries = staged[name]
    destinations = []
    for relative in entries:
        safe = safe_relative_path(relative)
        destination = ROOT_DIR.joinpath(*safe.parts)
        if destination.exists():
            raise RuntimeError(
                f"Refusing to overwrite existing file: {destination}"
            )
        destinations.append((relative, destination))

    created = []
    try:
        for relative, destination in destinations:
            contents, mode = entries[relative]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents)
            destination.chmod(mode)
            created.append(destination)

        installed[name] = {
            "version": str(package["version"]),
            "files": [relative for relative, _ in destinations],
            "dependencies": package.get("dependencies", []),
            "automatic": automatic,
        }
        write_json(STATE_FILE, state)
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise

    print(f"Installed {name} {package['version']}.")


def install_package(name):
    data = load_repo()
    packages = data["packages"]
    order = dependency_order(name, packages)
    state = load_state()["packages"]

    to_install = [item for item in order if item not in state]
    if not to_install:
        print(f"{name} is already installed.")
        return

    staged = {}
    for item in to_install:
        staged[item] = stage_archive(packages[item])
        print(f"Checksum verified: {item}")

    for item in to_install:
        install_one(
            item,
            packages[item],
            staged,
            automatic=(item != name),
        )


def remove_package(name):
    state = load_state()
    installed = state["packages"]
    if name not in installed:
        print(f"{name} is not installed.")
        return

    removed = []

    def remove_one(package_name):
        if package_name not in installed:
            return
        package = installed[package_name]
        for relative in package.get("files", []):
            safe = safe_relative_path(relative)
            path = ROOT_DIR.joinpath(*safe.parts)
            if path.is_file() or path.is_symlink():
                path.unlink()
        installed.pop(package_name)
        removed.append(package_name)

    remove_one(name)

    changed = True
    while changed:
        changed = False
        needed = {
            dependency
            for package in installed.values()
            for dependency in package.get("dependencies", [])
        }
        for package_name, package in list(installed.items()):
            if package.get("automatic") and package_name not in needed:
                remove_one(package_name)
                changed = True

    write_json(STATE_FILE, state)
    for package_name in removed:
        print(f"Removed {package_name}.")


def upgrade_packages():
    data = load_repo()
    installed = load_state()["packages"]
    if not installed:
        print("No packages installed.")
        return

    upgrades = []
    for name, current in installed.items():
        candidate = data["packages"].get(name)
        if not candidate or candidate.get("status", "available") != "available":
            print(f"{name}: no available repository version.")
            continue
        if str(candidate["version"]) != str(current["version"]):
            upgrades.append((name, candidate, current))

    if not upgrades:
        print("No packages need upgrading.")
        return

    for name, candidate, current in upgrades:
        staged = stage_archive(candidate)
        old_state = load_state()
        old_entry = old_state["packages"][name]
        old_files = {}

        for relative in old_entry.get("files", []):
            safe = safe_relative_path(relative)
            path = ROOT_DIR.joinpath(*safe.parts)
            if path.is_file():
                old_files[relative] = (path.read_bytes(), path.stat().st_mode & 0o777)

        try:
            for relative in old_entry.get("files", []):
                safe = safe_relative_path(relative)
                ROOT_DIR.joinpath(*safe.parts).unlink(missing_ok=True)

            for relative, (contents, mode) in staged.items():
                safe = safe_relative_path(relative)
                destination = ROOT_DIR.joinpath(*safe.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(contents)
                destination.chmod(mode)

            old_entry["version"] = str(candidate["version"])
            old_entry["files"] = list(staged)
            old_entry["dependencies"] = candidate.get("dependencies", [])
            write_json(STATE_FILE, old_state)
        except Exception:
            for relative in staged:
                safe = safe_relative_path(relative)
                ROOT_DIR.joinpath(*safe.parts).unlink(missing_ok=True)
            for relative, (contents, mode) in old_files.items():
                safe = safe_relative_path(relative)
                destination = ROOT_DIR.joinpath(*safe.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(contents)
                destination.chmod(mode)
            raise

        print(
            f"Upgraded {name}: {current['version']} -> {candidate['version']}"
        )

    print("Upgrade check complete.")


def create_package(name):
    if not name or any(character not in "abcdefghijklmnopqrstuvwxyz"
                       "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in name):
        raise RuntimeError("Use only letters, numbers, hyphens, and underscores.")
    directory = PACKAGES_DIR / name
    if directory.exists():
        raise RuntimeError(f"Package already exists: {directory}")

    (directory / "files").mkdir(parents=True)
    write_json(directory / "tsol.json", {
        "name": name,
        "version": "0.1.0",
        "description": f"A TsolOS package: {name}",
        "dependencies": [],
    })
    data = load_repo()
    data["packages"][name] = {
        "name": name,
        "version": "0.1.0",
        "description": f"A TsolOS package: {name}",
        "status": "planned",
    }
    save_repo(data)
    print(f"Created package: {directory}")
    print(f"Add files to {directory / 'files'} and then run: tsol build {name}")


def show_list():
    for name, package in sorted(load_repo()["packages"].items()):
        print(f"{name} {package.get('version', '?')} "
              f"[{package.get('status', 'available')}]")


def show_installed():
    packages = load_state()["packages"]
    if not packages:
        print("No packages installed.")
        return
    for name, package in sorted(packages.items()):
        print(f"{name} {package.get('version', '?')}")


def show_info():
    print(f"Tsol package manager {VERSION}")
    print(f"Project: {PROJECT_DIR}")
    print(f"Repository: {REPO_DIR}")
    print(f"Test installation root: {ROOT_DIR}")
    print(f"Online index: {REMOTE_REPO_URL}")


def search_packages(query):
    query = query.lower()
    for name, package in sorted(load_repo()["packages"].items()):
        description = package.get("description", "")
        if query in name.lower() or query in description.lower():
            print(f"{name} {package.get('version', '?')} "
                  f"[{package.get('status', 'available')}] — {description}")


def main():
    parser = argparse.ArgumentParser(prog="tsol", description="Tsol package manager")
    parser.add_argument("--version", action="version", version=f"tsol {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("info", help="Show Tsol configuration")
    commands.add_parser("list", help="List repository packages")
    commands.add_parser("installed", help="List installed packages")
    commands.add_parser("update", help="Download the online repository index")
    commands.add_parser("upgrade", help="Upgrade installed packages")

    search_parser = commands.add_parser("search", help="Search packages")
    search_parser.add_argument("query")

    create_parser = commands.add_parser("create", help="Create a package")
    create_parser.add_argument("name")

    build_parser = commands.add_parser("build", help="Build a package archive")
    build_parser.add_argument("name")

    verify_parser = commands.add_parser("verify", help="Verify a package archive")
    verify_parser.add_argument("name")

    install_parser = commands.add_parser("install", help="Install a package")
    install_parser.add_argument("name")

    remove_parser = commands.add_parser("remove", help="Remove a package")
    remove_parser.add_argument("name")

    args = parser.parse_args()

    try:
        if args.command == "info":
            show_info()
        elif args.command == "list":
            show_list()
        elif args.command == "installed":
            show_installed()
        elif args.command == "update":
            update_repository()
        elif args.command == "upgrade":
            upgrade_packages()
        elif args.command == "search":
            search_packages(args.query)
        elif args.command == "create":
            create_package(args.name)
        elif args.command == "build":
            build_package(args.name)
        elif args.command == "verify":
            verify_package(args.name)
        elif args.command == "install":
            install_package(args.name)
        elif args.command == "remove":
            remove_package(args.name)
    except (RuntimeError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"tsol: error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
