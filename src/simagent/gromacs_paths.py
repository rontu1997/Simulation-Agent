"""Optional, user-editable paths to a GROMACS installation and its topology library."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath

import yaml

from simagent.config import Config, ConfigError


def apply_gromacs_paths(cfg: Config, path: Path,
                        env: Mapping[str, str] | None = None) -> Config:
    """Overlay the dedicated path file; GMX_BIN still has highest priority."""
    if not path.is_file():
        return cfg
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain bin and optional library_dir")
    unknown = set(data) - {"bin", "library_dir"}
    if unknown:
        raise ConfigError(f"unknown GROMACS path field: {sorted(unknown)[0]}")
    binary = data.get("bin")
    library = data.get("library_dir")
    if not isinstance(binary, str) or not binary.strip():
        raise ConfigError(f"{path}: bin must be a path")
    if library is not None and (not isinstance(library, str) or not library.strip()):
        raise ConfigError(f"{path}: library_dir must be a path")
    for name, value in (("bin", binary), ("library_dir", library)):
        absolute = value is not None and (
            Path(value).is_absolute() or PurePosixPath(value).is_absolute())
        if value is not None and not absolute:
            raise ConfigError(f"{path}: {name} must be an absolute path")
    current_env = os.environ if env is None else env
    return replace(cfg, gmx=replace(cfg.gmx,
                                   bin=cfg.gmx.bin if "GMX_BIN" in current_env else binary,
                                   library_dir=library))
