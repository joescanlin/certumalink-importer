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

Update later with:

```sh
certumalink_run --update
```

The command prompts for a ZIP code, imports live CMS NPPES physician records,
creates a timestamped output folder in the current directory, validates the
output, and prints a terminal report.

During longer runs, it prints progress as each ZIP and CMS response page is
processed, so users can tell the import is still working.

If CMS starts returning the same page again during pagination, the importer
detects that repeated NPI set and stops that ZIP instead of looping forever.

You can also pass the ZIP directly:

```sh
certumalink_run --zip 78701
```

Filter to one specialty:

```sh
certumalink_run --zip 78701 --specialty dermatology
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

Reuse a persistent activation status ledger:

```sh
certumalink_run --zip 78701 --status-ledger activation_status.csv
```

Publish draft profiles to Certumalink once the platform endpoint exists:

```sh
export CERTUMALINK_API_URL="https://www.certumalink.com"
export CERTUMALINK_API_TOKEN="..."
certumalink_run --zip 78701 --publish-to-certumalink
```

The expected backend endpoint contract is documented in `docs/profile-seeding-api.md`.

## Output Folder

The default output folder contains:

- `doctors.csv` - normalized CMS physician export
- `profile_drafts.csv` - Certumalink draft profile rows with deterministic profile URLs
- `rox_outreach.csv` - Rox-ready activation outreach rows with suggested pitch text
- `publish_payload.json` - dry-run profile payloads for future Certumalink API publishing
- `publish_result.json` - present only when `--publish-to-certumalink` is used
- `activation_status.csv` - local NPI-keyed activation status ledger for this run
- `summary.json` - machine-readable run summary, skip reasons, and output paths

Profile URLs use this shape:

```text
https://www.certumalink.com/doctors/{first-last}-{npi}
```

Example:

```text
https://www.certumalink.com/doctors/joanne-adams-1255396008
```

Example report:

```text
Certumalink Doctor Import Report
--------------------------------
ZIPs: 78701
Output: certumalink-import-78701-20260609-143000
CMS records scanned: 200
Physicians exported: 32
Skipped records: 168
Skip reasons:
  - non_physician_taxonomy: 102
  - practice_zip_mismatch: 66
Duplicate NPIs merged: 0
CMS response pages: 1
Profile drafts created: 32
Rox outreach rows created: 32
Publish dry-run payloads: 32
Activation statuses:
  - draft_profile_created: 32
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
