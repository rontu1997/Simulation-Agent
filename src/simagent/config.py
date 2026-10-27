"""Layered configuration: environment, then config.yaml, then built-in defaults.

Secrets (LUNA_API_KEY, SIMAGENT_APPROVAL_TOKEN) come from the environment only.
"""

from __future__ import annotations

import getpass
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

GMX_DEFAULT_BIN = "/home/rontu/Rontu/software/software_install/gromacs-2025.3/bin/gmx"

_SECRET_KEYS = {"api_key", "luna_api_key", "approval_token", "simagent_approval_token"}


class ConfigError(Exception):
    """Raised for an invalid or unsafe configuration."""


def _mask(value: str | None) -> str:
    return "***set***" if value else "***missing***"


@dataclass(frozen=True)
class GmxConfig:
    bin: str = GMX_DEFAULT_BIN
    library_dir: str | None = None
    expected_version_prefix: str = "2025.3"
    expected_sha256: str | None = None
    ntomp: int | str = "auto"
    ntmpi: int | str = "auto"
    cpt_minutes: int = 15
    short_cmd_timeout_s: int = 300
    max_wall_hours: float = 24.0
    env_passthrough: tuple[str, ...] = (
        "PATH", "HOME", "LANG", "LD_LIBRARY_PATH", "OMP_NUM_THREADS", "OMP_PLACES",
        "OMP_PROC_BIND", "KMP_AFFINITY", "CUDA_VISIBLE_DEVICES", "GMXLIB",
    )


@dataclass(frozen=True)
class LlmConfig:
    model: str = "gpt6-luna"
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    temperature: float = 0.1
    max_tool_calls_per_turn: int = 25
    token_budget: int = 200000
    request_timeout_s: int = 120

    def __repr__(self) -> str:
        return (f"LlmConfig(model={self.model!r}, base_url={self.base_url!r}, "
                f"LUNA_API_KEY={_mask(self.api_key)}, temperature={self.temperature}, "
                f"token_budget={self.token_budget})")


@dataclass(frozen=True)
class WorkflowConfig:
    require_approval: bool = True
    max_auto_retries_per_stage: int = 2
    max_production_ns_without_confirm: float = 10.0


@dataclass(frozen=True)
class PathsConfig:
    runs_dir: Path = Path("./runs")
    sim_runs_dir: Path = Path("./runs_simulated")
    memory_dir: Path = Path("./memory")


@dataclass(frozen=True)
class GatesConfig:
    em_epot_per_molecule_warn: tuple[float, float] = (-48.0, -34.0)
    nvt_temp_tol_K: float = 2.0
    nvt_temp_drift_fail_K_per_100ps: float = 1.0
    nvt_temp_drift_warn_K_per_100ps: float = 0.5
    density_warn_g_cm3: tuple[float, float] = (0.96, 1.02)
    density_fail_g_cm3: tuple[float, float] = (0.90, 1.10)
    npt_pressure_tol_bar_floor: float = 50.0
    prod_temp_tol_K: float = 1.0
    conserved_drift_warn_kJ_mol_ps_atom: float = 0.005


@dataclass(frozen=True)
class Config:
    gmx: GmxConfig = field(default_factory=GmxConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    gates: GatesConfig = field(default_factory=GatesConfig)
    approval_token: str | None = field(default=None, repr=False)
    allow_fake: bool = False
    allow_unapproved: bool = False
    log_prompts: bool = False
    user: str = "researcher"

    def __repr__(self) -> str:
        return (f"Config(gmx={self.gmx!r}, llm={self.llm!r}, workflow={self.workflow!r}, "
                f"paths={self.paths!r}, SIMAGENT_APPROVAL_TOKEN={_mask(self.approval_token)}, "
                f"allow_fake={self.allow_fake}, allow_unapproved={self.allow_unapproved}, "
                f"user={self.user!r})")


_SECTIONS: dict[str, type] = {
    "gmx": GmxConfig, "llm": LlmConfig, "workflow": WorkflowConfig,
    "paths": PathsConfig, "gates": GatesConfig,
}


def _coerce(cls: type, key: str, value: Any) -> Any:
    default = getattr(cls(), key)
    if isinstance(default, tuple):
        return tuple(value)
    if isinstance(default, Path):
        return Path(value)
    return value


def _apply_yaml(cfg: Config, data: Mapping[str, Any]) -> Config:
    for section, values in data.items():
        if section not in _SECTIONS:
            raise ConfigError(f"unknown config section: {section}")
        if not isinstance(values, Mapping):
            raise ConfigError(f"config section {section} must be a mapping")
        cls = _SECTIONS[section]
        allowed = {f.name for f in fields(cls)}
        updates: dict[str, Any] = {}
        for key, value in values.items():
            if str(key).lower() in _SECRET_KEYS:
                raise ConfigError(
                    f"secret key '{key}' found in config.yaml; "
                    "secrets come from the environment only")
            if key not in allowed:
                raise ConfigError(f"unknown config key: {section}.{key}")
            updates[key] = _coerce(cls, key, value)
        cfg = replace(cfg, **{section: replace(getattr(cfg, section), **updates)})
    return cfg


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _threads(raw: str | None, default: int | str) -> int | str:
    if raw is None or raw.strip().lower() == "auto":
        return default
    return int(raw)


def default_config_path(env: Mapping[str, str] | None = None) -> Path | None:
    """``SIMAGENT_CONFIG`` if set, else ``config.yaml`` in the working directory if it exists."""
    env = os.environ if env is None else env
    named = env.get("SIMAGENT_CONFIG")
    if named:
        return Path(named)
    candidate = Path("config.yaml")
    return candidate if candidate.is_file() else None


def _absolute_bin(binary: str) -> str:
    """A relative binary path is resolved once, so the runner's working directory cannot change
    what it means."""
    path = Path(binary)
    if path.is_absolute() or PurePosixPath(binary).is_absolute():
        return binary
    return str(path.resolve())


def load_config(env: Mapping[str, str] | None = None, yaml_path: Path | None = None) -> Config:
    env = os.environ if env is None else env
    cfg = Config()
    try:
        cfg = replace(cfg, user=getpass.getuser())
    except Exception:  # no usable account name on this host
        pass
    if yaml_path is not None:
        data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
        cfg = _apply_yaml(cfg, data)

    gmx = cfg.gmx
    if "GMX_BIN" in env:
        gmx = replace(gmx, bin=env["GMX_BIN"])
    gmx = replace(gmx, bin=_absolute_bin(gmx.bin),
                  ntomp=_threads(env.get("GMX_NTOMP"), gmx.ntomp),
                  ntmpi=_threads(env.get("GMX_NTMPI"), gmx.ntmpi))

    llm = cfg.llm
    if "LUNA_MODEL" in env:
        llm = replace(llm, model=env["LUNA_MODEL"])
    if "LUNA_BASE_URL" in env:
        llm = replace(llm, base_url=env["LUNA_BASE_URL"])
    if "LUNA_API_KEY" in env:
        llm = replace(llm, api_key=env["LUNA_API_KEY"])
    if "SIMAGENT_TOKEN_BUDGET" in env:
        llm = replace(llm, token_budget=int(env["SIMAGENT_TOKEN_BUDGET"]))

    workflow = replace(cfg.workflow, require_approval=_flag(
        env, "SIMAGENT_REQUIRE_APPROVAL", cfg.workflow.require_approval))

    paths = cfg.paths
    if "GMX_RUNS_DIR" in env:
        paths = replace(paths, runs_dir=Path(env["GMX_RUNS_DIR"]))
    if "SIMAGENT_MEMORY_DIR" in env:
        paths = replace(paths, memory_dir=Path(env["SIMAGENT_MEMORY_DIR"]))

    return replace(
        cfg, gmx=gmx, llm=llm, workflow=workflow, paths=paths,
        approval_token=env.get("SIMAGENT_APPROVAL_TOKEN") or None,
        allow_fake=_flag(env, "SIMAGENT_ALLOW_FAKE", cfg.allow_fake),
        allow_unapproved=_flag(env, "SIMAGENT_ALLOW_UNAPPROVED", cfg.allow_unapproved),
        log_prompts=_flag(env, "SIMAGENT_LOG_PROMPTS", cfg.log_prompts),
        user=env.get("SIMAGENT_USER", cfg.user),
    )
