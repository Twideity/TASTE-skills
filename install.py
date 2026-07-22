#!/usr/bin/env python3
"""Install recommend-papers for the current user with one command."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


MINIMUM_PYTHON = (3, 10)
SKILL_NAME = "recommend-papers"
REPOSITORY_ROOT = Path(__file__).resolve().parent
SOURCE_SKILL = REPOSITORY_ROOT / "skills" / SKILL_NAME
REQUIREMENTS = REPOSITORY_ROOT / "requirements.txt"
USER_SKILLS = Path.home() / ".agents" / "skills"
INSTALLED_SKILL = USER_SKILLS / SKILL_NAME


def data_root() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "TASTE" / SKILL_NAME
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "TASTE" / SKILL_NAME
    xdg_data = str(os.environ.get("XDG_DATA_HOME") or "").strip()
    return (Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share") / "taste" / SKILL_NAME


VENV_ROOT = data_root() / "venv"
INSTALL_STATE = data_root() / "installation.json"


def venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=capture, check=False)


def python_version(prefix: list[str]) -> tuple[int, int, int] | None:
    try:
        result = run(prefix + ["-c", "import json,sys; print(json.dumps(list(sys.version_info[:3])))"], capture=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout.strip())
        return int(value[0]), int(value[1]), int(value[2])
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        return None


def candidate_pythons() -> list[list[str]]:
    candidates: list[list[str]] = [[sys.executable]]
    configured = str(os.environ.get("RECOMMEND_PAPERS_PYTHON") or "").strip()
    if configured:
        candidates.insert(0, [str(Path(configured).expanduser())])
    installed = venv_python(VENV_ROOT)
    if installed.exists():
        candidates.insert(1 if configured else 0, [str(installed)])
    for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append([resolved])
    launcher = shutil.which("py")
    if launcher:
        candidates.extend([[launcher, flag] for flag in ("-3.13", "-3.12", "-3.11", "-3.10")])
    conda = shutil.which("conda")
    if conda:
        result = run([conda, "info", "--base"], capture=True)
        if result.returncode == 0 and result.stdout.strip():
            base = Path(result.stdout.strip())
            candidates.append([str(base / ("python.exe" if platform.system() == "Windows" else "bin/python"))])
    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for candidate in candidates:
        key = tuple(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def select_base_python() -> list[str]:
    for candidate in candidate_pythons():
        version = python_version(candidate)
        if version and version >= MINIMUM_PYTHON:
            return candidate
    raise RuntimeError("No Python 3.10 or newer was found. Install Python, then rerun: python install.py")


def prepare_environment(base_python: list[str]) -> Path:
    target_python = venv_python(VENV_ROOT)
    if python_version([str(target_python)]) is None:
        if VENV_ROOT.exists():
            backup = VENV_ROOT.with_name("venv.backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
            VENV_ROOT.rename(backup)
            print(f"Preserved unusable environment at {backup}")
        VENV_ROOT.parent.mkdir(parents=True, exist_ok=True)
        result = run(base_python + ["-m", "venv", str(VENV_ROOT)], capture=True)
        if result.returncode != 0:
            raise RuntimeError("Failed to create the private virtual environment: " + (result.stderr.strip() or result.stdout.strip()))
    result = run([str(target_python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)], capture=True)
    if result.returncode != 0:
        raise RuntimeError("Failed to install Python dependencies: " + (result.stderr.strip() or result.stdout.strip()))
    VENV_ROOT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REQUIREMENTS, VENV_ROOT.parent / "requirements.txt")
    return target_python


def same_installation(path: Path) -> bool:
    try:
        return path.exists() and path.resolve() == SOURCE_SKILL.resolve()
    except OSError:
        return False


def unique_sibling(path: Path, label: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = path.with_name(f"{path.name}.{label}-{stamp}")
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = path.with_name(f"{path.name}.{label}-{stamp}-{counter}")
        counter += 1
    return candidate


def read_install_state() -> dict[str, object]:
    try:
        value = json.loads(INSTALL_STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_install_state(registration: str, python: Path) -> None:
    INSTALL_STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = INSTALL_STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "registration": registration,
        "source": str(SOURCE_SKILL),
        "installed": str(INSTALLED_SKILL),
        "python": str(python),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, INSTALL_STATE)


def is_managed_copy() -> bool:
    state = read_install_state()
    return (
        state.get("registration") in {"copy", "existing-copy"}
        and state.get("source") == str(SOURCE_SKILL)
        and state.get("installed") == str(INSTALLED_SKILL)
        and INSTALLED_SKILL.is_dir()
        and not INSTALLED_SKILL.is_symlink()
    )


def prepare_copy(source_credentials: Path | None = None) -> Path:
    staging = unique_sibling(INSTALLED_SKILL, "staging")
    shutil.copytree(SOURCE_SKILL, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if source_credentials and source_credentials.is_file():
        shutil.copy2(source_credentials, staging / "read.env")
    return staging


def activate_copy(staging: Path, existing: Path | None = None) -> str:
    backup = None
    if existing and (existing.exists() or existing.is_symlink()):
        backup = unique_sibling(existing, "backup")
        existing.rename(backup)
        print(f"Preserved existing skill at {backup}")
    try:
        staging.rename(INSTALLED_SKILL)
    except OSError:
        if backup and not INSTALLED_SKILL.exists() and not INSTALLED_SKILL.is_symlink():
            backup.rename(INSTALLED_SKILL)
        raise
    return "existing-copy" if backup else "copy"


def register_skill() -> str:
    USER_SKILLS.mkdir(parents=True, exist_ok=True)
    if same_installation(INSTALLED_SKILL):
        return "existing-link"
    if is_managed_copy():
        credentials = INSTALLED_SKILL / "read.env"
        staging = prepare_copy(credentials)
        return activate_copy(staging, INSTALLED_SKILL)

    backup = None
    if INSTALLED_SKILL.exists() or INSTALLED_SKILL.is_symlink():
        backup = unique_sibling(INSTALLED_SKILL, "backup")
        INSTALLED_SKILL.rename(backup)
        print(f"Preserved existing skill at {backup}")
    if platform.system() == "Windows":
        result = run(["cmd", "/c", "mklink", "/J", str(INSTALLED_SKILL), str(SOURCE_SKILL)], capture=True)
        if result.returncode == 0:
            return "junction"
        credentials = backup / "read.env" if backup else None
        try:
            staging = prepare_copy(credentials)
            return activate_copy(staging)
        except OSError:
            if backup and not INSTALLED_SKILL.exists() and not INSTALLED_SKILL.is_symlink():
                backup.rename(INSTALLED_SKILL)
            raise
    try:
        INSTALLED_SKILL.symlink_to(SOURCE_SKILL, target_is_directory=True)
        return "symlink"
    except OSError:
        if backup and not INSTALLED_SKILL.exists() and not INSTALLED_SKILL.is_symlink():
            backup.rename(INSTALLED_SKILL)
        raise


def verify(python: Path, skill: Path = INSTALLED_SKILL) -> dict[str, object]:
    service = skill / "scripts" / "paper_service.py"
    result = run([str(python), str(service), "doctor"], capture=True)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Installation verification returned invalid output: {result.stderr.strip()}") from exc
    if result.returncode != 0 or payload.get("status") != "ok":
        raise RuntimeError("Installation verification failed:\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def ensure_read_env() -> tuple[Path, bool]:
    path = INSTALLED_SKILL / "read.env"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path, False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("OPENREVIEW_USERNAME=\nOPENREVIEW_PASSWORD=\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path, True


def main() -> int:
    try:
        if not (SOURCE_SKILL / "SKILL.md").is_file() or not REQUIREMENTS.is_file():
            raise RuntimeError("Run install.py from an intact TASTE-skills checkout.")
        base_python = select_base_python()
        print("Using Python " + ".".join(map(str, python_version(base_python) or ())) + f": {' '.join(base_python)}")
        python = prepare_environment(base_python)
        verify(python, SOURCE_SKILL)
        registration = register_skill()
        read_env, read_env_created = ensure_read_env()
        doctor = verify(python)
        write_install_state(registration, python)
        print(json.dumps({
            "status": "ok",
            "skill": str(INSTALLED_SKILL),
            "registration": registration,
            "python": str(python),
            "python_version": doctor.get("python_version"),
            "read_env": str(read_env),
            "read_env_created": read_env_created,
            "next": "Restart Codex if the skill is not visible, then invoke $recommend-papers.",
        }, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
