# Magoo

An EVE Online industry planner for pipelined production. Magoo plans each
*index run* of your production line — what to buy, which jobs to install — and
tracks what it actually cost you.

It runs entirely on your own machine. Your ESI tokens, assets and plans never
leave it; there is no Magoo server.

## What it does

- **Plans an index run** end to end: expands your pipelines' bills of material
  against on-hand stock and in-progress jobs, then allocates job slots with a
  MILP solver to maximise savings.
- **Prices two venues** — Jita and a structure market — and buys wherever is
  cheaper *landed*, including freight.
- **Costs realistically** using lag-based costing: each input is priced from
  the snapshot of the executed run its chain depth lags behind.
- **Models your actual facilities**: structure and rig bonuses per item class,
  security bands, system cost indices, skills and blueprint ME/TE.
- **Compares alchemy routes**, reaction saturation, capital and Upwell
  structure production.

The plan is advisory; ESI is the ledger.

## Installing

Downloads live on the [releases page](https://github.com/Tetrakill/MAGOO/releases).

Two downloads are offered per release:

| Download | Use it when |
| --- | --- |
| `MagooSetup-<version>.exe` | Normal install. No administrator prompt — it installs to your user profile and adds a Start-menu entry and an uninstaller. |
| `magoo-<version>-win64.zip` | Portable. Extract anywhere and run `Magoo.exe`; data is kept beside the executable. |

### "Windows protected your PC"

Releases are not code-signed yet, so SmartScreen will warn on first run.
Click **More info → Run anyway**. This is expected for an unsigned hobby
application; if that is not something you are comfortable with, run from
source instead.

## First run

Magoo walks you through it with an on-screen checklist. In short:

1. **Download the game data** — one click. About 100 MB from CCP; it takes a
   few minutes and shows progress.
2. **Log in with EVE** — opens your normal browser so you can see you are on
   `login.eveonline.com`. Magoo never sees your password.
3. **Update from ESI** — snapshots your assets, industry jobs and wallets.
4. **Add your pipelines** — what you build, and how many per cycle.
5. **Refresh prices**, then **Plan index run**.

Then visit Settings to enter your skills, structures and rigs — those are
user-entered by design and materially change the numbers.

## Where your data lives

| Build | Location |
| --- | --- |
| Installed | `%LOCALAPPDATA%\Magoo` |
| Portable | `data\` beside the executable |
| From source | `data/` in the checkout |

That directory holds `magoo.sqlite` (your plans, settings and **ESI refresh
tokens**), the cached SDE download, and `logs/magoo.log`. Uninstalling leaves
it in place. Set `MAGOO_DATA_DIR` to override the location.

Because the database holds live refresh tokens, treat it as a credential: do
not commit it, post it, or sync it somewhere you would not put a password.

## Running from source

Requires Python 3.13+.

```bash
git clone https://github.com/Tetrakill/MAGOO.git
cd MAGOO
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[test]"
.venv/Scripts/python.exe run.py
```

Then open <http://localhost:8765>.

Use `localhost`, not `127.0.0.1` — EVE SSO matches the registered callback URL
exactly, and the two spellings are different strings to CCP.

Run the tests with `.venv/Scripts/python.exe -m pytest`. The suite reads
reference data from your real `data/magoo.sqlite`, so import the SDE first.

## Building the release artifacts

```powershell
.\packaging\build.ps1
```

That produces both downloads in `dist\` from one PyInstaller onedir tree.
The installer step needs [Inno Setup](https://jrsoftware.org/isdl.php) (6 or
newer); the
script skips it with a warning if it is not installed, so
`-SkipInstaller` gets you just the portable zip.

The build runs a self-test against the **frozen** executable before
packaging it, and refuses to continue if it fails:

```powershell
.\dist\Magoo\Magoo.exe --selftest
```

That check exists because a frozen build can start perfectly and still be
broken. SciPy reaches its HiGHS solver through a dynamic import PyInstaller
cannot see, so a missing hidden import would only surface the first time a
user planned a run. The self-test solves a MILP with a known answer, loads
the templates, opens the database and checks JWT verification, writing the
result to `logs/magoo.log`.

Build from a clean virtualenv — the build environment affects output size
far more than anything in `packaging/magoo.spec`.

## Documentation

[`PROJECT.md`](PROJECT.md) is the full specification: architecture, the
verified industry formulas with their sources, the data model, the eight-phase
planning calculation, and the decision log.

## Licence

MIT — see [LICENSE](LICENSE).

Issues and pull requests: https://github.com/Tetrakill/MAGOO

Magoo is a third-party tool and is not affiliated with or endorsed by CCP hf.
EVE Online is a registered trademark of CCP hf. All EVE-related materials are
property of CCP hf.
