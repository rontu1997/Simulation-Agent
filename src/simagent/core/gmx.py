"""GROMACS process environment and the single place that runs ``gmx`` (design 2.1, 4.4).

Commands are argument lists with ``shell=False``. The environment is built, not inherited, and
never contains LUNA_* variables or the approval token.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simagent.config import Config, GmxConfig
from simagent.core.paths import sha256_text
from simagent.core.provenance import Ledger, file_entry
from simagent.errors import ErrorCode, SimAgentError

# Windows needs these for any child process to start; they carry no secrets.
_PLATFORM_EXTRA = ("SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "COMSPEC") \
    if sys.platform == "win32" else ()


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return upper.startswith("LUNA_") or upper.startswith("SIMAGENT_APPROVAL")


def build_gmx_env(gmx: GmxConfig, parent_env: Mapping[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in (*gmx.env_passthrough, *_PLATFORM_EXTRA):
        if _is_secret_name(name):
            continue
        if name in parent_env:
            env[name] = parent_env[name]
    prefix = Path(gmx.bin).parent.parent
    env["GMXDATA"] = str(prefix / "share" / "gromacs")
    env["GMXBIN"] = str(prefix / "bin")
    parts = [p for p in env.get("LD_LIBRARY_PATH", "").split(os.pathsep) if p]
    for lib in ("lib64", "lib"):
        if (prefix / lib).is_dir():
            parts.append(str(prefix / lib))
    if parts:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(parts)
    return env


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    seq_start: int | None = None
    seq_end: int | None = None


class GmxRunner:
    def __init__(self, cfg: Config, ledger: Ledger | None = None,
                 parent_env: Mapping[str, str] | None = None) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self._parent_env: Mapping[str, str] = os.environ if parent_env is None else parent_env

    def run(self, args: Sequence[str], *, cwd: Path, stage: str = "", tool: str = "",
            stdin: str | None = None, inputs: Sequence[Path] = (), outputs: Sequence[Path] = (),
            base: Path | None = None, timeout: float | None = None) -> RunResult:
        base = Path(base) if base is not None else Path(cwd)
        argv = [self.cfg.gmx.bin, *args]
        env = build_gmx_env(self.cfg.gmx, self._parent_env)
        record: dict[str, Any] = {
            "kind": "gmx", "stage": stage, "tool": tool, "argv": argv,
            "cwd": _rel(Path(cwd), base), "env_allow": env,
        }
        seq_start = None
        if self.ledger is not None:
            seq_start = self.ledger.append({
                **record, "event": "start",
                "inputs": [file_entry(base, _rel(p, base)) for p in inputs if Path(p).exists()],
            })
        try:
            proc = subprocess.run(  # noqa: S603 - argument list, shell=False
                argv, cwd=str(cwd), input=stdin, capture_output=True, text=True,
                timeout=timeout if timeout is not None else self.cfg.gmx.short_cmd_timeout_s,
                env=env, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise SimAgentError(ErrorCode.E_TIMEOUT, f"command timed out: {Path(argv[0]).name}",
                                hint="raise gmx.short_cmd_timeout_s or check the input") from exc
        except FileNotFoundError as exc:
            raise SimAgentError(
                ErrorCode.E_GMX_NOT_FOUND, f"cannot execute {argv[0]}",
                hint="check GMX_BIN points at an executable GROMACS binary") from exc
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise SimAgentError(ErrorCode.E_DISK_FULL, "no space left on device",
                                    hint="free disk space and run the stage again") from exc
            raise SimAgentError(ErrorCode.E_GMX_NOT_FOUND, f"cannot execute {argv[0]}: {exc}",
                                hint="check GMX_BIN and its permissions") from exc

        seq_end = None
        if self.ledger is not None:
            seq_end = self.ledger.append({
                **record, "event": "end", "exit_code": proc.returncode,
                "stdout_sha256": sha256_text(proc.stdout),
                "stderr_sha256": sha256_text(proc.stderr),
                "inputs": [file_entry(base, _rel(p, base)) for p in inputs if Path(p).exists()],
                "outputs": [file_entry(base, _rel(p, base)) for p in outputs if Path(p).exists()],
            })
        return RunResult(proc.returncode, proc.stdout, proc.stderr, seq_start, seq_end)


def _rel(path: Path, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return str(path)
