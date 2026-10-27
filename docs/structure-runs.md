# Structure simulations

`simagent structure` prepares a PDB or GRO coordinate file with GROMACS
`pdb2gmx`, using the residue templates in an installed force field. The input
must contain coordinates and residue/atom names that `pdb2gmx` accepts. An
`.rtp` file alone cannot provide coordinates. If GROMACS needs a missing
residue, protonation choice, or topology, preparation stops and leaves its
diagnostic log in the output directory.

Edit `gromacs_paths.yaml` in the project root to set:

- `bin`: absolute path to the `gmx` executable inside Ubuntu WSL.
- `library_dir`: absolute path to the GROMACS `share/gromacs/top` directory
  containing the installed `*.ff` directories and their `.rtp` files.

`GMX_BIN` overrides `bin`. The structure command reads this file when run
from the project root. The older water-only MCP workflow still uses
`config.yaml` and `GMX_BIN`. The structure command does not load the default
`.env` file; pass `--env-file` explicitly if you need it.

Run only energy minimization and NVT:

```bash
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync simagent structure \
  --input /path/to/structure.pdb \
  --force-field amber99sb-ildn --water-model tip3p \
  --stages em,nvt --nvt-ps 100
```

Include NPT and production when needed (example: a charged protein on C:):

```bash
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync simagent structure \
  --input /mnt/c/Users/RontuDas/Downloads/1AKI_test.pdb \
  --force-field oplsaa --water-model tip4p \
  --stages em,nvt,npt,prod --nvt-ps 100 --npt-ps 100 --prod-ns 1 \
  --neutralize
```

In an interactive terminal, the command asks what to analyze before it asks
for approval. For example, type `backbone RMSD, radius of gyration and
temperature`, or press Enter to accept the default set. You can also add
`--analyses "RMSD, Rg and temperature"` to the command line. With `--yes`,
the default set is used unless `--analyses` is provided. The command runs the
requested analyses after MD and then exits. It is not a continuing chat session.
Use `--yes` for scripts. It
creates a new directory under `runs/structures/` unless `--output-dir` is
specified. The directory contains the copied input, `topol.top`, prepared
coordinates, each stage's `.mdp`, `.tpr`, `.gro`, trajectory and log files,
`structure_run.json`, an analysis directory, `report.md`, and a provenance
ledger. Existing output directories are never overwritten.

After the last MD stage, SimAgent extracts potential energy, total energy,
temperature, pressure, and, for NPT or production, density. For protein
systems it also calculates backbone RMSD and radius of gyration. The report
contains means, standard deviations, first and last values, and frame counts;
`analysis/*.xvg` contains the time series and `analysis/overview.png` plots
the main metrics. All frames of the last MD stage
are included. The report does not establish convergence or equilibration.
Unavailable protein groups are listed in the report. If analysis fails, the
simulation stays marked `done` and analysis is marked `failed` separately.
Only the requested measurements are calculated. The text parser accepts those
seven measurements, including `energy` for both energy series; it reports
unsupported terms before MD starts. NVT-only runs do not provide density.

Analyze an older completed structure run or retry failed analysis without
repeating EM or MD:

```bash
UV_PROJECT_ENVIRONMENT=.venv-wsl uv run --no-sync simagent analyze-structure \
  --run-dir runs/structures/1AKI_test-20261026-042746-24dbf3 \
  --analyses "temperature, RMSD and Rg"
```

Use WSL paths for Windows files: `C:\Users\RontuDas\Downloads\1AKI_test.pdb`
is `/mnt/c/Users/RontuDas/Downloads/1AKI_test.pdb` in Ubuntu. The example
protein has net charge +8 e with OPLS-AA. `--neutralize` runs GROMACS
`genion` after solvation, replacing eight water molecules with Cl- ions;
it does not request a bulk salt concentration. The temporary ion-preparation
TPR uses cut-off electrostatics only for ion placement. EM and MD retain
their usual electrostatics settings and warnings.

The selected force field must support the input residues and chosen water
model. This path uses a solvated cubic box and the current EM, NVT, NPT, and
production MDP templates. It stops on any failed GROMACS preparation or run
command. It does not automatically parameterize arbitrary ligands, repair
missing heavy atoms, choose protonation states, or add a specified salt
concentration. Neutralizing counterions are added only with `--neutralize`.
