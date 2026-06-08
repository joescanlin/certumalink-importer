# Certumalink Doctor Importer

Portable CLI tooling for importing real public physician profile data by ZIP
code from the CMS NPPES NPI Registry API and exporting seed-ready CSV or JSON
files for Certumalink.

## Team Install

Run this once:

```sh
curl -fsSL "https://raw.githubusercontent.com/joescanlin/certumalink-importer/main/portable/install-certumalink-run.sh" | bash
```

Then run:

```sh
certumalink_run --zip
```

The command prompts for a ZIP code, imports live CMS NPPES physician records,
creates a timestamped CSV in the current folder, validates the output, and
prints a terminal report.

During longer runs, it prints progress as each ZIP and CMS response page is
processed, so users can tell the import is still working.

You can also pass the ZIP directly:

```sh
certumalink_run --zip 78701
```

For a ZIP list:

```sh
cat > zips.csv <<'EOF'
zip
78701
60601
EOF

certumalink_run --zip-file zips.csv
```

Quick live smoke test:

```sh
certumalink_run --zip 78701 --max-pages 1
```

Suppress progress messages and only print the final report:

```sh
certumalink_run --zip 78701 --quiet
```

Example report:

```text
Certumalink Doctor Import Report
--------------------------------
ZIPs: 78701
Output: certumalink-doctors-78701-20260608-162632.csv
CMS records scanned: 200
Physicians exported: 32
Skipped records: 168
Duplicate NPIs merged: 0
CMS response pages: 1
Validation: passed
```

## Source

The importer uses the official CMS NPI Registry API:

- API: https://npiregistry.cms.hhs.gov/api/
- API docs: https://npiregistry.cms.hhs.gov/api-page
- Bulk NPPES files: https://download.cms.gov/nppes/NPI_Files.html

NPPES data is public, provider-reported NPI data. NPI assignment does not
replace credentialing, licensure checks, sanctions screening, or verification
before publishing doctors on Certumalink.

## Requirements

- Python 3.9+

The runtime uses only the Python standard library.

The hosted installer downloads `portable/certumalink-doctor-import.py` into
`~/.certumalink` and creates `certumalink_run` in `~/.local/bin`.

Install the package in editable mode from the repo root:

```sh
python3 -m pip install -e .
```

## Usage

The easiest team command does not require installing the package:

```sh
scripts/import-doctors input/example_zips.csv output/doctors.csv
```

See `TEAM_USAGE.md` for a copy/paste handoff guide.

After installation, run from the repo root:

```sh
python3 -m certumalink_importer import-zips \
  --zip-file input/example_zips.csv \
  --out output/doctors.csv
```

Import one ZIP:

```sh
python3 -m certumalink_importer import-zip \
  --zip 78701 \
  --out output/doctors.csv
```

Run a bounded live smoke test:

```sh
python3 -m certumalink_importer import-zip \
  --zip 78701 \
  --out output/live-78701.csv \
  --max-pages 1
```

Export JSON instead of CSV:

```sh
python3 -m certumalink_importer import-zips \
  --zip-file input/example_zips.csv \
  --out output/doctors.json \
  --format json
```

Validate an export:

```sh
python3 -m certumalink_importer validate-export output/doctors.csv
```

## What v1 Imports

V1 intentionally imports only individual physicians:

- Type 1 individual NPI records (`enumeration_type=NPI-1`)
- active records only when CMS status/deactivation fields are present
- provider taxonomies whose codes start with `207` or `208`, which cover
  allopathic and osteopathic physician specialties
- practice-location ZIP must match the requested ZIP; records that only match
  through a mailing address are skipped

The export includes core seed fields:

- NPI and name fields
- credential and display name
- primary physician taxonomy code and specialty
- practice address and phone
- matched ZIPs
- source and fetch timestamp

V1 does not scrape third-party websites, infer unavailable values, geocode
addresses, or decide whether a physician is accepting new patients.

## Tests

Run the offline test suite:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```
