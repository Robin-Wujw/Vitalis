# Vitalis

[中文](README.md) | [Documentation](docs/README.md)

Vitalis turns Zepp sleep, activity, and workout observations into traceable personal reports. It is a pre-release, non-medical trend tool: missing observations stay missing, and clients must not invent health facts or prescriptions.

## Short demo

From the repository root, install the locked environment, enable explicit mock mode, create a new SQLite database, and export a saved report:

```powershell
python -m pip install 'uv==0.12.9'
uv sync --locked --extra dev
$env:ZEPP_MOCK = 'true'; $env:VITALIS_ENV = 'test'; $env:DATABASE_URL = 'sqlite:///./demo.db'
uv run --locked --extra dev vitalis demo --database .\demo.db --day 2026-10-07
uv run --locked --extra dev vitalis report daily --user demo --day 2026-10-07 --format markdown --output .\daily.md
Get-Content .\daily.md
```

The report command reads persisted analysis, refuses to overwrite a file, and does not send a notification. See the [Chinese quickstart](docs/quickstart.md) for API, tokens, Zepp, and operations. The [documentation hub](docs/README.md) lists the current guides.
