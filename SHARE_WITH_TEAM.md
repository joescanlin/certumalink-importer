# Sharing The Doctor Importer Without A Repo Clone

The team does not need the full repo if they only need to run imports. Give
them the single portable script:

- `portable/certumalink-doctor-import.py`

It uses only Python 3.9+ and the standard library.

## Fallback: Send One File

Send `portable/certumalink-doctor-import.py` through Slack, email, or a shared
drive. Teammates can run:

```sh
python3 certumalink-doctor-import.py --zip 78701 --out doctors-78701.csv --max-pages 1
```

For a ZIP list:

```sh
python3 certumalink-doctor-import.py --zip-file zips.csv --out doctors.csv
```

Where `zips.csv` looks like:

```csv
zip
78701
60601
```

## Recommended Now: Hosted Raw GitHub Installer

The hosted repo is:

```text
https://github.com/joescanlin/certumalink-importer
```

Teammates install with:

```sh
curl -fsSL "https://raw.githubusercontent.com/joescanlin/certumalink-importer/main/portable/install-certumalink-run.sh" | bash
```

Then they run:

```sh
certumalink_run --zip
```

It will ask for the ZIP code, create a timestamped CSV in the current folder,
print progress as CMS pages are processed, and finish with a terminal report.

## Later Upgrade: GitHub Releases

Create a small public GitHub repo, for example:

```text
certumalink/doctor-importer
```

Upload these two files as release assets:

- `certumalink-doctor-import.py`
- `install-certumalink-run.sh`

Example release flow:

```sh
mkdir doctor-importer-release
cp portable/certumalink-doctor-import.py doctor-importer-release/
cp portable/install-certumalink-run.sh doctor-importer-release/
cd doctor-importer-release
git init
git add .
git commit -m "Add Certumalink doctor importer"
gh repo create certumalink/doctor-importer --public --source=. --remote=origin --push
gh release create v0.1.0 certumalink-doctor-import.py install-certumalink-run.sh \
  --title "Certumalink Doctor Importer v0.1.0" \
  --notes "Portable CMS NPPES physician importer."
```

Then teammates run one install command:

```sh
curl -fsSL "https://github.com/certumalink/doctor-importer/releases/latest/download/install-certumalink-run.sh" | bash
```

After that, they can run:

```sh
certumalink_run --zip
```

It will ask for the ZIP code, create a timestamped CSV in the current folder,
and print a terminal report.

Example:

```text
Enter ZIP code: 78701

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

They can also pass the ZIP directly:

```sh
certumalink_run --zip 78701
```

For a ZIP list:

```sh
certumalink_run --zip-file zips.csv
```

This is better than linking to `raw.githubusercontent.com/main/...` because
release assets are versioned, easier to roll back, and support a stable
`releases/latest/download/...` URL.

## Alternative: Raw GitHub URL

Host these two files in the repo:

- `portable/certumalink-doctor-import.py`
- `portable/install-certumalink-run.sh`

Teammates can install from the raw file URL:

```sh
curl -fsSL "https://raw.githubusercontent.com/certumalink/Certumalink-platform/main/portable/install-certumalink-run.sh" | bash
```

After that, they can run:

```sh
certumalink_run --zip
certumalink_run --zip 78701
certumalink_run --zip-file zips.csv
```

If the GitHub repo or branch name is different, use this install command
instead:

```sh
CERTUMALINK_IMPORTER_URL="https://raw.githubusercontent.com/OWNER/REPO/BRANCH/portable/certumalink-doctor-import.py" \
  bash -c "$(curl -fsSL "https://raw.githubusercontent.com/OWNER/REPO/BRANCH/portable/install-certumalink-run.sh")"
```

Replace `OWNER`, `REPO`, and `BRANCH` with the published GitHub location.

## Option D: Ask An LLM To Run It

Paste this into the teammate's LLM or coding agent:

```text
Use the attached file certumalink-doctor-import.py to import real public CMS
NPPES physician records. Do not invent provider data. Run:

python3 certumalink-doctor-import.py --zip

When it asks for a ZIP code, use the ZIP I provide. If I ask for a quick test,
add --max-pages 1. After it finishes, show me the report, confirm that the CSV
exists, and show the first 5 rows. Remind me that NPPES records are
provider-reported and must be reviewed before publishing on Certumalink.
```

## Should We Build MCP?

Not for v1. MCP would still require teammates to install/configure a server, and
the current job is a deterministic terminal workflow. A single portable script or
hosted raw script is simpler and easier to support.
