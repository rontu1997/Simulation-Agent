# SimAgent

SimAgent runs molecular dynamics workflows with GROMACS from a terminal. It
currently has two entry points:

| Workflow | Input | What it runs |
| --- | --- | --- |
| Water agent (`simagent` or `simagent run --request-file`) | A description or JSON request for pure water | Builds a water box, then runs EM, NVT, NPT, and production through the MCP workflow |
| Structure command (`simagent structure`) | A PDB or GRO coordinate file whose residues are supported by an installed GROMACS force field | Prepares a solvated structure with `pdb2gmx`; runs EM and NVT, with optional NPT and production |

**Choose the structure command for proteins or other `pdb2gmx` compatible
residues.** The chat agent and JSON request workflow still accept pure water
only. The structure command is a separate path and does not provide the water
agent's physical gates, automatic diagnosis, or checkpoint resume.

## Get the repository

This project is published at
[github.com/rontu1997/Simulation-Agent](https://github.com/rontu1997/Simulation-Agent).

## Requirements

- Ubuntu WSL or another Linux environment with a working GROMACS installation.
  The checked-in configuration targets GROMACS 2025.3 in Ubuntu WSL.
- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- For the natural-language water agent: access to an OpenAI-compatible LLM
  endpoint through `LUNA_API_KEY` and `LUNA_BASE_URL`. The structure command
  does not need an LLM.
- A PDB or GRO coordinate file for the structure command. An `.rtp` file
  describes residue topology; it does not contain coordinates.

Run the commands below **inside Ubuntu WSL**. A Linux path such as
`/home/rontu/.../gmx` will not run as a Windows executable.

## Install in Ubuntu WSL

From PowerShell, enter WSL:

```powershell
wsl -d Ubuntu
```

Then clone the project and install the locked dependencies:

```bash
git clone https://github.com/rontu1997/Simulation-Agent.git
cd Simulation-Agent
UV_PROJECT_ENVIRONMENT=.venv-wsl uv sync --locked
```

If your prompt shows another active environment, such as `(openmm-env)`, run
`deactivate` first. Otherwise uv may warn that `VIRTUAL_ENV` does not match
`.venv-wsl`; the command uses `.venv-wsl` when
`UV_PROJECT_ENVIRONMENT=.venv-wsl` is set.

Check the installed command:

```bash
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync simagent --help
```

The command is `uv run simagent`, **not** `uv simagent`.

## Set the GROMACS paths

There are two configuration files because the workflows currently load
GROMACS differently:

| File | Used by | What to edit |
| --- | --- | --- |
| [`config.yaml`](config.yaml) | Water agent and its MCP server | `gmx.bin`, expected version, run directories, gates, and workflow settings |
| [`gromacs_paths.yaml`](gromacs_paths.yaml) | `simagent structure` | `bin` and `library_dir` |

For a different GROMACS installation, edit
[`gromacs_paths.yaml`](gromacs_paths.yaml):

```yaml
bin: /absolute/path/to/gromacs/bin/gmx
library_dir: /absolute/path/to/gromacs/share/gromacs/top
```

`library_dir` must be the directory containing installed `*.ff` force-field
folders and their `.rtp` files. The structure command sets `GMXLIB` from this
value. An exported `GMX_BIN` overrides `bin` for the structure command.
For the water agent, edit `gmx.bin` in `config.yaml` or set `GMX_BIN`.
Run SimAgent from the project root so it finds these files.

## Run the water agent

Set the LLM connection in your WSL shell:

```bash
export LUNA_API_KEY='your-key'
export LUNA_BASE_URL='https://your-endpoint.example/v1'
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync simagent
```

You may also put these `LUNA_*` settings in the project-root `.env` file;
chat and `simagent run` load it automatically, and exported shell variables
take priority. The structure command only loads it with `--env-file`.
The file is ignored by Git. On a Linux filesystem, run `chmod 600 .env` if
SimAgent warns that other users can read it. On `/mnt/d`, WSL may report a
Windows-mounted file as mode 777 even after `chmod`; use exported variables
or keep a secrets file in the Linux filesystem.

The terminal asks you to describe a simulation, asks for missing
science-critical values, shows a plan, and waits for approval. For example:

> Simulate 1000 TIP4P water molecules with OPLS-AA at 300 K and 1 bar.
> Minimize energy, run 100 ps NVT and 100 ps NPT at a 2 fs time step, then run
> 1 ns production. Analyze temperature, pressure, density, energy, O–O RDF,
> and MSD.

The water workflow builds the cubic box from the **molecule count**. It cannot
enforce an exact box edge supplied in the description. Its current plan always
contains EM, NVT, NPT, and production; use `simagent structure` if you need a
run that ends after NVT for a coordinate structure. The supported water/force
field combinations are TIP4P with OPLS-AA and TIP4P-Ew with
AMBER99SB-ILDN, provided the required files are installed.
Analysis requests are recorded in the plan, but the current controller
stops after production and skips analysis and report generation.

Useful commands inside the water chat:

| Command | Purpose |
| --- | --- |
| `/jobs` | List saved jobs |
| `/status` | Show the current job's stage status |
| `/plan` | Show its approved plan |
| `/files` | Show its saved files |
| `/provenance` | Show execution evidence |
| `/resume <job_id>` | Reattach to an interrupted or planned job |
| `/cancel` | Cancel the running stage |
| `/quit` | Leave the chat |

### Run a water request without the LLM

Save a request as `water-request.json`:

```json
{
  "system": {
    "kind": "water",
    "n_molecules": 1000,
    "water_model": "tip4p"
  },
  "force_field": "oplsaa",
  "thermo": {
    "temperature_K": 300,
    "pressure_bar": 1
  },
  "protocol": {
    "nvt_ps": 100,
    "npt_ps": 100,
    "prod_ns": 1,
    "dt_ps": 0.002
  },
  "analyses": ["temperature", "pressure", "density", "energy", "rdf_OO", "msd"]
}
```

Then run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync simagent run \
  --request-file water-request.json
```

This command does not call the LLM. It asks for plan approval in an
interactive terminal. Add `--yes` for a scripted run; production runs above
the configured long-run limit also need `--confirm-long-run`.

## Run a structure supported by an installed force field

Provide a PDB or GRO file with coordinates and residue/atom names accepted by
`gmx pdb2gmx`. Choose the installed force-field folder name **without**
`.ff`, plus a compatible water model.

EM and 100 ps NVT only:

```bash
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync simagent structure \
  --input /path/to/structure.pdb \
  --force-field amber99sb-ildn --water-model tip3p \
  --stages em,nvt --nvt-ps 100
```

Include NPT and production for a charged protein on the Windows C: drive:

```bash
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync simagent structure \
  --input /mnt/c/Users/RontuDas/Downloads/1AKI_test.pdb \
  --force-field oplsaa --water-model tip4p \
  --stages em,nvt,npt,prod --nvt-ps 100 --npt-ps 100 --prod-ns 1 \
  --neutralize
```

An intermediate `--stages em,nvt,npt` is also supported. The default is
`em,nvt`.

### Choosing analyses for a protein run

**Will a new protein run be analyzed automatically?** Yes. After the last
selected MD stage, SimAgent runs the requested analyses and writes a report.
In an interactive terminal, it asks for analysis text *before* asking you to
approve the run. Press Enter to use the default measurements.

For example, after entering the structure command above:

```text
Analyses [Enter for potential energy, total energy, temperature, pressure, density, backbone RMSD, radius of gyration]: RMSD, Rg and temperature
Analyses: temperature, backbone RMSD, radius of gyration
Approve? [y/N] y
Structure run completed: .../runs/structures/<run-id>
Analysis report: .../runs/structures/<run-id>/report.md
```

**Why does the terminal return to the shell?** `simagent structure` handles
one run and its report, then exits. Returning to the shell after `Analysis
report:` does not mean analysis was skipped. Check `analysis_status` in
`structure_run.json` and open `report.md`. Older runs that completed before
analysis was added can be analyzed from their saved files with the command
below; MD is not repeated.

To provide the analysis text in the command itself, append
`--analyses "RMSD, Rg and temperature"` to `simagent structure`.

This also works with `--yes` for scripts. Supported terms are potential energy,
total energy, temperature, pressure, density, backbone RMSD, and radius of
gyration. `energy` selects both energy series. Unknown terms cause a clear
error before simulation. The text parser handles these measurements, not
arbitrary GROMACS analyses. Density requires NPT or production as the last stage.

After the MD stages finish, SimAgent analyzes the last MD stage and writes
`report.md`, `analysis/summary.json`, an `analysis/overview.png` plot, and
GROMACS `.xvg` time series for the selected measurements. By default, the
report includes potential and total energy, temperature, pressure, backbone
RMSD, and protein radius of gyration. Density is included by default for NPT
or production; GROMACS does not store it in this workflow's NVT energy file.
Protein metrics are marked unavailable when the trajectory has no compatible
Protein or Backbone group. Analysis failure does not change the completed MD
status, and the command prints a retry instruction.

To analyze a structure run completed before this feature was added, retry a
failed analysis, or choose different measurements from saved files, run:

```bash
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync simagent analyze-structure \
  --run-dir runs/structures/1AKI_test-20261026-042746-24dbf3 \
  --analyses "temperature, RMSD and Rg"
```

This reads the saved `.edr`, `.tpr`, and `.xtc` files and does not rerun MD.
Omit `--analyses` to use the run's previous selection or the defaults. Add
`--yes` to `simagent structure` only when you want to skip its prompts.

Inside WSL, Windows `C:\Users\...` paths are `/mnt/c/Users/...` paths.
An unquoted Windows path loses its backslashes in Bash and cannot identify
the PDB file. The `1AKI_test.pdb` file above has a +8 e protein charge with
OPLS-AA, so `--neutralize` replaces eight waters with eight Cl- ions before
EM. For an EM/NVT-only run, change `--stages` to `em,nvt` and omit the NPT
and production duration options.

| Option | Default | Meaning |
| --- | --- | --- |
| `--temperature` | `300` | Target temperature in K |
| `--pressure` | `1` | Target pressure in bar when NPT or production runs |
| `--dt-ps` | `0.002` | MD time step in ps (0.002 ps = 2 fs) |
| `--nvt-ps` | `100` | NVT duration in ps |
| `--npt-ps` | `100` | NPT duration in ps |
| `--prod-ns` | `1` | Production duration in ns |
| `--padding-nm` | `1` | Minimum box padding around the input structure |
| `--seed` | `42` | Initial velocity seed |
| `--analyses` | Prompt or default set | Plain-text choice from the supported measurements |
| `--neutralize` | Off | Replace water with enough Na+/Cl- ions to make the system charge neutral; does not set a salt concentration |
| `--output-dir` | Generated under `runs/structures/` | New output directory; existing directories are rejected |

Accepted `--water-model` names are `spc`, `spce`, `tip3p`, `tip4p`,
`tip5p`, and `tips3p`; the selected force field must provide that model.

Preparation runs `pdb2gmx`, `editconf`, and `solvate`, optionally adds
neutralizing ions, then
`grompp`/`mdrun` for each selected stage. The force field and water model
must be compatible. This command does not invent a topology for an unknown
residue, parameterize a ligand, repair missing heavy atoms, choose protonation
states, or add a specified salt concentration. See [the structure guide](docs/structure-runs.md)
for more detail.

## Where results are saved

The water agent writes a job under `runs/<job_id>/`:

```text
runs/<job_id>/
├── job.json
├── provenance.jsonl
├── 01_build/
├── 02_em/
├── 03_nvt/
├── 04_npt/
├── 05_prod/
└── 06_analysis/
```

For example, `03_nvt/nvt.gro` is the final NVT coordinates,
`03_nvt/nvt.xtc` is its trajectory, `03_nvt/nvt.edr` contains energies,
and `03_nvt/nvt.log` is the GROMACS log. A folder's presence does not mean
its stage passed; inspect `job.json` or use `/status`. The current water
controller stops after production and reports that analysis and `report.md`
were skipped, even though the plan lists them.

The structure command writes a new directory under `runs/structures/`.
It includes a copy of the input coordinates, `topol.top`,
`processed.gro`, `boxed.gro`, `solvated.gro`, each selected stage's
`.mdp`, `.tpr`, `.gro`, optional `ions.mdp`, `ions.tpr`, and `ionized.gro`,
GROMACS trajectory/log files, command stdout and
stderr logs, `structure_run.json`, and `provenance.jsonl`.
`structure_run.json` records the selected stages, settings, simulation status,
and analysis status. Structure runs also write `analysis/summary.json`,
`analysis/*.xvg`, `analysis/overview.png`, and `report.md`. Report statistics use all frames of the
last MD stage; standard deviations describe frame variation and are not
uncertainty estimates for the mean.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `uv simagent` says “unrecognized subcommand” | Use `uv run simagent` inside the project. |
| `E_GMX_NOT_FOUND` | Run inside WSL and check the executable path in `config.yaml` for water, or `gromacs_paths.yaml` for structures. `GMX_BIN` takes precedence. |
| `VIRTUAL_ENV` mismatch warning | Deactivate the other environment, then run with `UV_PROJECT_ENVIRONMENT=.venv-wsl`. |
| `.env is readable by other users` | `simagent structure` does not read the default `.env`. Chat and `simagent run` do; on a Linux filesystem use `chmod 600 .env`. On the Windows-mounted `/mnt/d` drive, `chmod` may not change the reported mode; keep the secret file in the Linux filesystem or configure WSL mount metadata. |
| `input must be an existing PDB or GRO coordinate file` | In WSL, use `/mnt/c/...` for a file on the Windows C: drive. Check with `test -f /mnt/c/Users/RontuDas/Downloads/1AKI_test.pdb`. |
| `grompp` warns about a non-neutral system | If neutralization is appropriate for the intended simulation, pass `--neutralize`; this replaces water with enough Na+/Cl- ions before EM. |
| LLM reports HTTP 429 | The endpoint has reached its rate limit. Retry after the provider's stated wait, or use `simagent run --request-file` for water or `simagent structure` for structures without an LLM. |
| `pdb2gmx` reports an unknown residue or missing atoms | Check residue and atom names against the chosen force field. Read `pdb2gmx.stderr.log` in the structure output directory. |
| A water stage is marked failed | Read `job.json` and that stage's `.log` and gate `.xvg` files. A finished `mdrun` can still fail a validation gate. A code fix does not automatically change the status of an old job. |

## Development

```bash
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync pytest -q
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync ruff check src tests
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync mypy src/simagent
```

The project also exposes `gmx-mcp`, an MCP server for the water workflow's
GROMACS tools. For implementation details and original scope, see
[`design.md`](design.md) and [`prd.md`](prd.md).
