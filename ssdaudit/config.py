"""Ignore rules and saved comparison profiles."""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Filesystem bookkeeping that is never part of your data. Pruned before we
# descend, which is what keeps a whole-drive scan quick.
SYSTEM_DIRS = {
    "$RECYCLE.BIN",
    "$Recycle.Bin",
    "System Volume Information",
    ".Trashes",
    ".Spotlight-V100",
    ".fseventsd",
    ".TemporaryItems",
    "found.000",
    ".DocumentRevisions-V100",
}

# Junk that accumulates on a drive shared between Windows and macOS. Not
# ignored outright -- these are counted and reported with their reclaimable
# size, because purging them is usually something you actually want to do.
CRUFT_PATTERNS = [
    "Thumbs.db",
    "ehthumbs.db",
    "desktop.ini",
    ".DS_Store",
    "._*",
    "*.tmp",
    "~$*",
    ".apdisk",
]

# Opt-in via --preset dev. Deliberately not on by default: on a *backup* drive
# you may genuinely want these compared.
DEV_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".next",
    ".turbo",
    "dist",
    "build",
    ".gradle",
    "target",
}

PRESETS = {"dev": DEV_DIRS}


@dataclass
class IgnoreRules:
    """Decides what to skip, what to count as cruft, and what to compare."""

    dirs: set[str] = field(default_factory=lambda: set(SYSTEM_DIRS))
    cruft_patterns: list[str] = field(default_factory=lambda: list(CRUFT_PATTERNS))
    exclude_globs: list[str] = field(default_factory=list)
    include_globs: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        presets: list[str] | None = None,
        exclude: list[str] | None = None,
        include: list[str] | None = None,
        keep_cruft: bool = False,
    ) -> "IgnoreRules":
        rules = cls()
        for preset in presets or []:
            rules.dirs |= PRESETS.get(preset, set())
        rules.exclude_globs = list(exclude or [])
        rules.include_globs = list(include or [])
        if keep_cruft:
            rules.cruft_patterns = []
        return rules

    def skip_dir(self, name: str) -> bool:
        """True for directories we never descend into."""
        if name in self.dirs:
            return True
        return any(fnmatch.fnmatch(name, pattern) for pattern in self.exclude_globs)

    def is_cruft(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pattern) for pattern in self.cruft_patterns)

    def skip_file(self, relpath: str, name: str) -> bool:
        """True for files excluded from the comparison entirely."""
        for pattern in self.exclude_globs:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relpath, pattern):
                return True
        if self.include_globs:
            return not any(
                fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relpath, pattern)
                for pattern in self.include_globs
            )
        return False


@dataclass
class DriveRef:
    """One side of a comparison, pinned to a volume serial rather than a letter."""

    serial: str = ""
    label: str = ""
    subpath: str = ""
    fallback_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DriveRef":
        return cls(**{k: data.get(k, "") for k in ("serial", "label", "subpath", "fallback_path")})


@dataclass
class Profile:
    """A saved comparison: two drives, a folder scope, and its rules."""

    name: str
    left: DriveRef
    right: DriveRef
    folders: list[str] = field(default_factory=list)
    presets: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    verify: str = "smart"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
            "folders": self.folders,
            "presets": self.presets,
            "exclude": self.exclude,
            "include": self.include,
            "verify": self.verify,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        return cls(
            name=data["name"],
            left=DriveRef.from_dict(data.get("left", {})),
            right=DriveRef.from_dict(data.get("right", {})),
            folders=data.get("folders", []),
            presets=data.get("presets", []),
            exclude=data.get("exclude", []),
            include=data.get("include", []),
            verify=data.get("verify", "smart"),
        )

    def rules(self) -> IgnoreRules:
        return IgnoreRules.build(self.presets, self.exclude, self.include)


def data_dir() -> Path:
    """Per-user state directory, kept out of the repo so it is never committed."""
    override = os.environ.get("SSDAUDIT_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "ssdaudit"
    return Path(os.path.expanduser("~")) / ".local" / "share" / "ssdaudit"


def profiles_path() -> Path:
    return data_dir() / "profiles.json"


def load_profiles() -> dict[str, Profile]:
    path = profiles_path()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {name: Profile.from_dict(data) for name, data in raw.items()}


def save_profiles(profiles: dict[str, Profile]) -> Path:
    path = profiles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: profile.to_dict() for name, profile in profiles.items()}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
