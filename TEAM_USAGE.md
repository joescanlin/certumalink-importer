# Team Usage: Doctor ZIP Importer

Use this when you need a seed CSV of public CMS NPPES physician records for a
list of ZIP codes.

## Quick Start

1. Open this repo in Terminal.
2. Put target ZIP codes in a CSV file. You can start from
   `input/example_zips.csv`.
3. Run:

```sh
scripts/import-doctors input/example_zips.csv output/doctors.csv
```

The script imports physicians from CMS NPPES, writes the output CSV, and then
validates the export.

If someone does not have the repo, send them the single-file script at
`portable/certumalink-doctor-import.py` and use the instructions in
`SHARE_WITH_TEAM.md`.

For the hosted no-repo workflow, the command should be:

```sh
certumalink_run --zip
```

Updates use:

```sh
certumalink_run --update
```

It prompts for the ZIP, writes a timestamped output folder, creates profile
drafts, a prioritized Rox queue, editable outreach draft exports, practice
groups, and prints the import report in the terminal.

For campaign-focused runs:

```sh
certumalink_run --zip 49506 --campaign primary-care
certumalink_run --zip 49506 --campaign dermatology
```

Use `rox_today.csv` for the daily work queue. Use `rox_outreach.csv` for
editable starter call/email drafts that Rox agents can revise.

## ZIP File Format

Simple format:

```csv
zip
78701
60601
```

The importer also accepts TXT files with one ZIP per line. ZIP+4 values are
accepted and normalized to 5-digit ZIP codes.

## Smoke Test

For a fast live test that only imports the first CMS page per ZIP:

```sh
scripts/import-doctors input/example_zips.csv output/smoke-doctors.csv --max-pages 1
```

Use this before a large market run to confirm network access and output shape.

## Output

The CSV includes:

- NPI
- physician name and credential
- primary taxonomy and specialty
- practice address and phone
- matched ZIPs
- CMS source metadata

The hosted output folder also includes activation priority, profile
completeness, practice grouping, and Rox status data to help decide who to
contact first.

Only active individual physician records are included. Practice-location ZIP
must match the requested ZIP. Records that only match through a mailing address
are skipped.

## Important Review Note

These are real public CMS NPPES records, but they are provider-reported data.
Before publishing on Certumalink, the team should still review, credential, and
verify providers according to the platform's onboarding standards.
