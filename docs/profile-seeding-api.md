# Certumalink Physician Profile Seeding API

This is the platform API contract expected by `certumalink_run --publish-to-certumalink`.

## Endpoint

```http
POST /api/admin/imports/physician-profiles
Authorization: Bearer <CERTUMALINK_API_TOKEN>
Content-Type: application/json
```

The importer builds the full URL from:

```sh
CERTUMALINK_API_URL=https://www.certumalink.com
```

## Request

```json
{
  "dry_run": false,
  "generated_at": "2026-06-09T16:34:37+00:00",
  "source": "cms_nppes_registry_api",
  "profiles": [
    {
      "npi": "1497507156",
      "profile_url": "https://www.certumalink.com/doctors/mohamad-abouelnaaj-1497507156",
      "profile_slug": "mohamad-abouelnaaj-1497507156",
      "display_name": "MOHAMAD KHALED ABOUELNAAJ",
      "first_name": "MOHAMAD",
      "last_name": "ABOUELNAAJ",
      "credential": "",
      "specialty": "Internal Medicine",
      "taxonomy_code": "207R00000X",
      "city": "AUSTIN",
      "state": "TX",
      "practice_zip": "78701",
      "practice_phone": "512-324-7000",
      "source": "cms_nppes_registry_api",
      "source_fetched_at": "2026-06-09T16:34:36+00:00",
      "activation_status": "draft_profile_created"
    }
  ]
}
```

## Required Behavior

- Upsert by `npi`; never create duplicate physician profiles for the same NPI.
- Create new profiles as private draft records.
- Preserve human-reviewed or activated profile edits on re-import.
- Store source metadata: `source`, `source_fetched_at`, `last_imported_at`, and import batch ID.
- Return per-NPI results so the importer can write `publish_result.json`.

## Lifecycle

Platform profile lifecycle statuses:

```text
draft
needs_review
ready_for_rox
rox_contacted
activated
do_not_contact
```

Importer activation statuses map into the platform like this:

```text
draft_profile_created -> draft
rox_contacted -> rox_contacted
physician_activated -> activated
do_not_contact -> do_not_contact
needs_review -> needs_review
```

## Response

Successful response:

```json
{
  "import_id": "imp_20260609_001",
  "created_count": 10,
  "updated_count": 2,
  "unchanged_count": 4,
  "skipped_count": 1,
  "error_count": 0,
  "results": [
    {
      "npi": "1497507156",
      "action": "created",
      "profile_id": "prof_123",
      "profile_url": "https://www.certumalink.com/doctors/mohamad-abouelnaaj-1497507156"
    }
  ]
}
```

Partial validation errors should still return per-NPI results:

```json
{
  "import_id": "imp_20260609_002",
  "created_count": 0,
  "updated_count": 0,
  "unchanged_count": 0,
  "skipped_count": 0,
  "error_count": 1,
  "results": [
    {
      "npi": "bad-npi",
      "action": "error",
      "error": "npi must be 10 digits"
    }
  ]
}
```

## Validation

The platform should validate:

- `npi` is exactly 10 digits.
- `profile_slug` is unique or resolves to the same NPI.
- `source` is allowed, initially `cms_nppes_registry_api`.
- required fields are present: `npi`, `profile_slug`, `display_name`, `specialty`, `taxonomy_code`, `source`.
- lifecycle/status values are allowed.
- imported profile defaults to private visibility.

## Importer Output

When publishing succeeds or fails, the importer writes:

```text
publish_result.json
summary.json
```

If the API returns a non-2xx response, `publish_result.json` is still written and the command exits non-zero.

