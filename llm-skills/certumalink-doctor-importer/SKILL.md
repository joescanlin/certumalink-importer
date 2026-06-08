---
name: certumalink-doctor-importer
description: Use when importing real public CMS NPPES physician records by ZIP code for Certumalink seed CSV or JSON exports, validating outputs, or helping a teammate run the portable doctor importer script.
metadata:
  short-description: Import CMS NPPES doctors by ZIP for Certumalink
---

# Certumalink Doctor Importer

Use the bundled or attached `certumalink-doctor-import.py` script. Do not invent
provider data. The source must be the official CMS NPPES NPI Registry API.

## Workflow

1. Confirm Python 3 is available with `python3 --version`.
2. Create or inspect a ZIP file with a `zip` header, for example:

```csv
zip
78701
60601
```

3. For a quick live smoke test, run:

```sh
python3 certumalink-doctor-import.py --zip --max-pages 1
```

4. For the full import, run:

```sh
python3 certumalink-doctor-import.py --zip
```

5. When prompted, enter the user's ZIP code.
6. Report the terminal import report, output path, row count, and first 5 rows.

## Rules

- Include only active individual Type 1 physician records.
- Keep only physician taxonomy codes beginning with `207` or `208`.
- Practice-location ZIP must match the requested ZIP.
- Treat NPPES as public provider-reported data; remind users that records need
  review and credentialing before publishing on Certumalink.
