from pathlib import Path

import pytest

from simagent.config import Config, ConfigError, load_config
from simagent.core.gmx import build_gmx_env
from simagent.gromacs_paths import apply_gromacs_paths

GMX_DEFAULT = "/home/rontu/Rontu/software/software_install/gromacs-2025.3/bin/gmx"
ROOT = Path(__file__).resolve().parents[2]


def test_defaults():
    cfg = load_config(env={})
    assert cfg.gmx.bin == GMX_DEFAULT
    assert cfg.llm.model == "gpt6-luna"
    assert cfg.workflow.require_approval is True
    assert cfg.gates.density_warn_g_cm3 == (0.96, 1.02)
    assert cfg.gmx.ntomp == "auto" and cfg.gmx.ntmpi == "auto"
    assert cfg.workflow.max_auto_retries_per_stage == 2
    assert cfg.llm.token_budget == 200000


def test_env_overrides_yaml_overrides_defaults(tmp_path):
    yml = tmp_path / "c.yaml"
    yml.write_text("gmx:\n  cpt_minutes: 5\n  bin: /from/yaml\n", encoding="utf-8")
    cfg = load_config(env={"GMX_BIN": "/x/gmx", "LUNA_MODEL": "m"}, yaml_path=yml)
    assert cfg.gmx.bin == "/x/gmx"
    assert cfg.gmx.cpt_minutes == 5
    assert cfg.llm.model == "m"


def test_ntomp_auto_and_int():
    assert load_config(env={"GMX_NTOMP": "4"}).gmx.ntomp == 4
    assert load_config(env={}).gmx.ntomp == "auto"


def test_secret_in_yaml_refused(tmp_path):
    yml = tmp_path / "c.yaml"
    yml.write_text("llm:\n  api_key: abc-secret\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(env={}, yaml_path=yml)
    assert "api_key" in str(exc.value) and "abc-secret" not in str(exc.value)


def test_unknown_yaml_key_refused(tmp_path):
    yml = tmp_path / "c.yaml"
    yml.write_text("gmx:\n  nonsense: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(env={}, yaml_path=yml)


def test_repr_masks_secrets():
    cfg = load_config(env={"LUNA_API_KEY": "sekrit-key", "SIMAGENT_APPROVAL_TOKEN": "tok-123456"})
    text = repr(cfg)
    assert "***set***" in text
    assert "sekrit-key" not in text and "tok-123456" not in text
    assert "***missing***" in repr(load_config(env={}))


def test_require_approval_env():
    assert load_config(env={"SIMAGENT_REQUIRE_APPROVAL": "0"}).workflow.require_approval is False
    assert load_config(env={"SIMAGENT_REQUIRE_APPROVAL": "1"}).workflow.require_approval is True


def test_flags_and_llm_env():
    cfg = load_config(env={"SIMAGENT_ALLOW_FAKE": "1", "SIMAGENT_ALLOW_UNAPPROVED": "1",
                           "LUNA_BASE_URL": "http://h/v1", "SIMAGENT_USER": "alice"})
    assert cfg.allow_fake and cfg.allow_unapproved
    assert cfg.llm.base_url == "http://h/v1"
    assert cfg.user == "alice"
    assert isinstance(cfg, Config)


def test_shipped_config_yaml_loads_and_matches_defaults():
    assert load_config(env={}, yaml_path=ROOT / "config.yaml") == load_config(env={})


def test_gromacs_path_literal_only_in_config_module():
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "src").rglob("*.py")
        if p.name != "config.py" and "gromacs-2025.3" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_dedicated_gromacs_paths_override_config_but_not_gmx_bin_env(tmp_path):
    path = tmp_path / "gromacs_paths.yaml"
    path.write_text("bin: /opt/gromacs/bin/gmx\nlibrary_dir: /opt/gromacs/share/gromacs/top\n",
                    encoding="utf-8")
    cfg = apply_gromacs_paths(load_config(env={}), path, env={})
    assert cfg.gmx.bin == "/opt/gromacs/bin/gmx"
    assert build_gmx_env(cfg.gmx, {})["GMXLIB"] == "/opt/gromacs/share/gromacs/top"
    override = apply_gromacs_paths(load_config(env={"GMX_BIN": "/custom/gmx"}), path,
                                   env={"GMX_BIN": "/custom/gmx"})
    assert override.gmx.bin == "/custom/gmx"


def test_dedicated_gromacs_paths_rejects_unknown_fields(tmp_path):
    path = tmp_path / "gromacs_paths.yaml"
    path.write_text("bin: /opt/gmx\nsecret: value\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown"):
        apply_gromacs_paths(load_config(env={}), path, env={})
