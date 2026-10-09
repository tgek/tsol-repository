#!/usr/bin/env python3

import argparse
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path

VERSION = "0.12.0"

PROJECT = Path(__file__).resolve().parents[2]
PACKAGES_DIR = PROJECT / "packages"
REPO_DIR = PROJECT / "repository"
REPO_FILE = REPO_DIR / "packages.json"
TEST_ROOT = PROJECT / "test-root"
STATE = TEST_ROOT / "var/lib/tsol/installed.json"
BIN_DIR = TEST_ROOT / "usr/bin"


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read {path}: {error}") from error


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.write("\n")
    temporary.replace(path)


def load_repo():
    data = read_json(REPO_FILE, {"packages": []})

    if not isinstance(data, dict):
        raise RuntimeError("Invalid repository metadata.")

    packages = data.get("packages", [])

    # Support both the original list format and the newer dictionary format.
    if isinstance(packages, list):
        converted = {}
        for package in packages:
            if not isinstance(package, dict) or not package.get("name"):
                raise RuntimeError("Invalid package entry in repository.")
            if package["name"] in converted:
                raise RuntimeError(
                    f"Duplicate package in repository: {package['name']}"
                )
            converted[package["name"]] = package
        data["packages"] = converted
    elif not isinstance(packages, dict):
        raise RuntimeError("Repository packages must be a list or dictionary.")

    return data


def find_package(repo, name):
    return repo.get("packages", {}).get(name)


def valid_name(name):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", name))


def package_archive(package):
    archive = package.get("archive")
    if archive:
        path = Path(archive)
        if not path.is_absolute():
            path = REPO_DIR / path
    else:
        path = REPO_DIR / (
            f"{package.get('name', '')}-{package.get('version', '')}.tsol"
        )

    path = path.resolve()
    if not path.is_relative_to(REPO_DIR.resolve()):
        raise RuntimeError("Archive path points outside the repository.")
    return path


def verify_archive(package, archive):
    if not archive.is_file():
        raise RuntimeError(f"Archive not found: {archive}")

    expected = package.get("sha256")
    if not expected:
        raise RuntimeError(
            f"No SHA-256 checksum recorded for {package.get('name', archive.name)}."
        )

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest.lower() != expected.lower():
        raise RuntimeError(
            f"Checksum mismatch: {package.get('name', archive.name)}"
        )

    print(f"Checksum verified: {package.get('name', archive.stem)}")


def stage_archive(package, archive):
    """Verify and read package files without extracting archive paths."""
    verify_archive(package, archive)
    staged = {}

    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            path = Path(member.name)

            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or not member.isfile()
                or path.name in staged
            ):
                raise RuntimeError(
                    f"Unsafe, duplicate, or unsupported archive entry: {member.name}"
                )

            source = tar.extractfile(member)
            if source is None:
                raise RuntimeError(f"Cannot read archive entry: {member.name}")

            staged[path.name] = source.read()

    if not staged:
        raise RuntimeError("Archive contains no files.")

    return staged


def create_package(name):
    if not valid_name(name):
        raise RuntimeError("Invalid package name.")

    source = PACKAGES_DIR / name
    files_dir = source / "files"
    manifest_path = source / "tsol.json"

    if not source.is_dir():
        raise RuntimeError(f"Package directory not found: {source}")

    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Missing or invalid manifest: {manifest_path}")

    version = str(manifest.get("version", ""))
    if not version:
        raise RuntimeError(f"Manifest for {name} has no version.")

    dependencies = manifest.get("dependencies", [])
    if (
        not isinstance(dependencies, list)
        or not all(isinstance(dep, str) and valid_name(dep) for dep in dependencies)
    ):
        raise RuntimeError(f"Invalid dependencies in {manifest_path}")

    if name in dependencies:
        raise RuntimeError(f"{name} cannot depend on itself.")

    if len(dependencies) != len(set(dependencies)):
        raise RuntimeError(f"Duplicate dependencies in {manifest_path}")

    if not files_dir.is_dir():
        raise RuntimeError(f"Missing package files directory: {files_dir}")

    files = sorted(path for path in files_dir.rglob("*") if path.is_file())
    if not files:
        raise RuntimeError(f"No files found in {files_dir}")

    return manifest, files_dir, files, version, dependencies


def build_package(name):
    manifest, files_dir, files, version, dependencies = create_package(name)
    repo = load_repo()

    for dependency in dependencies:
        if find_package(repo, dependency) is None:
            raise RuntimeError(
                f"Dependency '{dependency}' is not in the repository. Build it first."
            )

    REPO_DIR.mkdir(parents=True, exist_ok=True)
    archive = REPO_DIR / f"{name}-{version}.tsol"
    temporary = REPO_DIR / f".{name}-{version}.tsol.tmp"

    with tarfile.open(temporary, "w:gz") as tar:
        for path in files:
            tar.add(path, arcname=path.relative_to(files_dir).as_posix())

    temporary.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    old_entry = find_package(repo, name) or {}
    repo["packages"][name] = {
        "name": name,
        "version": version,
        "archive": archive.name,
        "sha256": digest,
        "dependencies": dependencies,
        "description": manifest.get(
            "description", old_entry.get("description", "")
        ),
        "source": old_entry.get("source", ""),
        "status": "available",
    }

    write_json(REPO_FILE, repo)

    print(f"Built {name} {version}")
    print(f"Archive: {archive}")
    print(f"SHA-256: {digest}")
    if dependencies:
        print("Dependencies: " + ", ".join(dependencies))
    print("Repository metadata updated.")


def resolve_dependencies(name, repo, resolved=None, visiting=None):
    if resolved is None:
        resolved = []
    if visiting is None:
        visiting = []

    if name in visiting:
        raise RuntimeError(
            "Dependency cycle detected: " + " -> ".join(visiting + [name])
        )

    if name in resolved:
        return resolved

    package = find_package(repo, name)
    if package is None or package.get("status", "available") != "available":
        raise RuntimeError(f"Package unavailable: {name}")

    visiting.append(name)
    dependencies = package.get("dependencies", [])

    if not isinstance(dependencies, list):
        raise RuntimeError(f"Invalid dependencies for {name}")

    for dependency in dependencies:
        resolve_dependencies(dependency, repo, resolved, visiting)

    visiting.pop()
    resolved.append(name)
    return resolved


def install_one(name, repo, automatic=False):
    state = read_json(STATE, {"packages": {}})
    installed = state.setdefault("packages", {})

    if name in installed:
        print(f"{name} is already installed.")
        return

    package = find_package(repo, name)
    if package is None or package.get("status", "available") != "available":
        raise RuntimeError(f"Package unavailable: {name}")

    staged = stage_archive(package, package_archive(package))
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    root = TEST_ROOT.resolve()
    bin_root = BIN_DIR.resolve()

    owned_by_others = {
        filename
        for record in installed.values()
        for filename in record.get("files", [])
    }

    outputs = {}
    for filename in staged:
        output = (BIN_DIR / filename).resolve()
        if not output.is_relative_to(bin_root):
            raise RuntimeError(f"Unsafe output path: {filename}")

        relative = str(output.relative_to(root))
        if relative in owned_by_others:
            raise RuntimeError(f"File already owned by another package: {filename}")
        if output.exists():
            raise RuntimeError(f"Refusing to overwrite existing file: {output}")

        outputs[filename] = (output, relative)

    written = []
    try:
        for filename, content in staged.items():
            output, _ = outputs[filename]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
            output.chmod(0o755)
            written.append(output)

        installed[name] = {
            "version": package["version"],
            "files": [relative for _, relative in outputs.values()],
            "dependencies": package.get("dependencies", []),
            "automatic": automatic,
        }
        write_json(STATE, state)
    except Exception:
        for output in written:
            output.unlink(missing_ok=True)
        raise

    print(f"Installed {name} {package['version']}.")


def install_package(name, repo):
    order = resolve_dependencies(name, repo)

    for package_name in order:
        state = read_json(STATE, {"packages": {}})
        if package_name in state.get("packages", {}):
            print(f"{package_name} is already installed.")
            continue

        install_one(package_name, repo, automatic=(package_name != name))


def remove_package(name, cascade=True):
    state = read_json(STATE, {"packages": {}})
    installed = state.setdefault("packages", {})

    if name not in installed:
        print(f"{name} is not installed.")
        return

    root = TEST_ROOT.resolve()
    record = installed[name]

    for relative in record.get("files", []):
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise RuntimeError(f"Unsafe installed file path: {relative}")
        if target.is_file():
            target.unlink()

    del installed[name]
    write_json(STATE, state)
    print(f"Removed {name}.")

    if not cascade:
        return

    while True:
        state = read_json(STATE, {"packages": {}})
        installed = state.setdefault("packages", {})
        removable = None

        for dep_name, record in installed.items():
            if not record.get("automatic", False):
                continue

            needed = any(
                dep_name in other.get("dependencies", [])
                for other_name, other in installed.items()
                if other_name != dep_name
            )
            if not needed:
                removable = dep_name
                break

        if removable is None:
            break

        remove_package(removable, cascade=False)


def version_key(version):
    parts = re.findall(r"\d+|[A-Za-z]+", str(version))
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.lower())
        for part in parts
    )


def update_repository():
    """Validate and summarize the local repository index."""
    repo = load_repo()
    packages = repo["packages"]

    available = 0
    planned = 0
    other = 0

    for name, package in sorted(packages.items()):
        if not isinstance(package, dict) or not package.get("name"):
            raise RuntimeError(f"Invalid package entry: {name}")

        status = package.get("status", "available")
        if status == "available":
            available += 1
        elif status == "planned":
            planned += 1
        else:
            other += 1

    print("Tsol repository checked.")
    print(f"Repository: {REPO_FILE}")
    print(f"Available packages: {available}")
    print(f"Planned packages: {planned}")
    if other:
        print(f"Other-status packages: {other}")
    print("Repository metadata is valid.")
    print("Note: this checks the local index; it does not download updates.")


def upgrade_packages():
    repo = load_repo()
    state = read_json(STATE, {"packages": {}})
    installed = state.setdefault("packages", {})

    if not installed:
        print("No packages installed.")
        return

    candidates = []

    for name, old in list(installed.items()):
        package = find_package(repo, name)

        if package is None or package.get("status", "available") != "available":
            print(f"Skipping {name}: unavailable in repository.")
            continue

        old_version = str(old.get("version", ""))
        new_version = str(package.get("version", ""))

        if version_key(new_version) <= version_key(old_version):
            print(f"{name} {old_version} is up to date.")
            continue

        try:
            staged = stage_archive(package, package_archive(package))
            candidates.append((name, old_version, package, staged))
        except (RuntimeError, OSError, tarfile.TarError) as error:
            print(f"Skipping {name}: {error}")

    if not candidates:
        print("No packages need upgrading.")
        return

    for name, old_version, package, staged in candidates:
        state = read_json(STATE, {"packages": {}})
        installed = state.setdefault("packages", {})
        old = installed.get(name)

        if old is None:
            print(f"Skipping {name}: no longer installed.")
            continue

        root = TEST_ROOT.resolve()
        bin_root = BIN_DIR.resolve()
        old_files = set(old.get("files", []))

        owned_by_others = {
            filename
            for other_name, record in installed.items()
            if other_name != name
            for filename in record.get("files", [])
        }

        outputs = {}
        conflict = None

        for filename in staged:
            output = (BIN_DIR / filename).resolve()
            if not output.is_relative_to(bin_root):
                conflict = f"Unsafe output path: {filename}"
                break

            relative = str(output.relative_to(root))
            if relative in owned_by_others:
                conflict = f"{filename} is owned by another package."
                break
            if output.exists() and relative not in old_files:
                conflict = f"Refusing to overwrite unowned file: {filename}"
                break

            outputs[filename] = (output, relative)

        if conflict:
            print(f"Skipping {name}: {conflict}")
            continue

        prepared = []
        backups = {}
        replaced = []

        try:
            # Prepare replacement contents before touching installed files.
            for filename, content in staged.items():
                output, _ = outputs[filename]
                output.parent.mkdir(parents=True, exist_ok=True)
                temp = output.with_name(output.name + ".tsol-new")
                temp.write_bytes(content)
                temp.chmod(0o755)
                prepared.append((temp, output))

            # Back up files that will be replaced.
            for _, output in prepared:
                if output.exists():
                    backup = output.with_name(output.name + ".tsol-old")
                    backup.write_bytes(output.read_bytes())
                    backups[output] = backup

            try:
                for temp, output in prepared:
                    temp.replace(output)
                    replaced.append(output)

                # Record the new state before deleting obsolete files.
                new_files = [relative for _, relative in outputs.values()]
                old["version"] = package["version"]
                old["files"] = new_files
                old["dependencies"] = package.get("dependencies", [])
                write_json(STATE, state)

                for relative in old_files - set(new_files):
                    target = (root / relative).resolve()
                    if target.is_relative_to(root) and target.is_file():
                        target.unlink()

            except Exception:
                for output in reversed(replaced):
                    backup = backups.get(output)
                    if backup and backup.exists():
                        backup.replace(output)
                    else:
                        output.unlink(missing_ok=True)
                raise

            print(f"Upgraded {name}: {old_version} -> {package['version']}")

        except (OSError, RuntimeError) as error:
            print(f"Upgrade failed for {name}: {error}")

        finally:
            for temp, _ in prepared:
                temp.unlink(missing_ok=True)
            for backup in backups.values():
                backup.unlink(missing_ok=True)

    print("Upgrade check complete.")


def main():
    parser = argparse.ArgumentParser(
        prog="tsol",
        description="TsolOS package manager",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"tsol {VERSION}",
    )

    commands = parser.add_subparsers(dest="command")

    commands.add_parser("info", help="Show Tsol information")
    commands.add_parser("list", help="List repository packages")
    commands.add_parser("installed", help="List installed packages")
    commands.add_parser("update", help="Check local repository metadata")
    commands.add_parser("upgrade", help="Upgrade installed packages")

    search_parser = commands.add_parser("search", help="Search packages")
    search_parser.add_argument("query")

    create_parser = commands.add_parser("create", help="Create a package skeleton")
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

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == "info":
            print(f"Tsol package manager {VERSION}")
            print(f"Project: {PROJECT}")
            print(f"Repository: {REPO_DIR}")
            print(f"Test installation root: {TEST_ROOT}")

        elif args.command == "list":
            for name, package in sorted(load_repo()["packages"].items()):
                print(
                    f"{name} {package.get('version', '?')} "
                    f"[{package.get('status', 'available')}]"
                )

        elif args.command == "search":
            query = args.query.lower()
            found = False
            for name, package in sorted(load_repo()["packages"].items()):
                description = str(package.get("description", ""))
                if query in name.lower() or query in description.lower():
                    print(
                        f"{name} {package.get('version', '?')} - "
                        f"{description or 'No description'}"
                    )
                    found = True
            if not found:
                print("No matching packages found.")

        elif args.command == "installed":
            packages = read_json(STATE, {"packages": {}}).get("packages", {})
            if not packages:
                print("No packages installed.")
            for name, package in sorted(packages.items()):
                print(f"{name} {package.get('version', '?')}")

        elif args.command == "update":
            update_repository()

        elif args.command == "create":
            if not valid_name(args.name):
                raise RuntimeError("Invalid package name.")

            destination = PACKAGES_DIR / args.name
            if destination.exists():
                raise RuntimeError(f"Package already exists: {args.name}")

            (destination / "files").mkdir(parents=True)
            write_json(
                destination / "tsol.json",
                {
                    "name": args.name,
                    "version": "0.1.0",
                    "description": f"My {args.name} package",
                    "dependencies": [],
                },
            )
            print(f"Created package skeleton: {destination}")
            print("Add files under its files/ directory, then run:")
            print(f"tsol build {args.name}")

        elif args.command == "build":
            build_package(args.name)

        elif args.command == "verify":
            package = find_package(load_repo(), args.name)
            if package is None:
                raise RuntimeError(f"Package not found: {args.name}")
            verify_archive(package, package_archive(package))

        elif args.command == "install":
            install_package(args.name, load_repo())

        elif args.command == "remove":
            remove_package(args.name)

        elif args.command == "upgrade":
            upgrade_packages()

    except (RuntimeError, OSError, tarfile.TarError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
