from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DoctorRecord:
    npi: str
    first_name: str
    middle_name: str
    last_name: str
    credential: str
    display_name: str
    primary_taxonomy_code: str
    primary_specialty: str
    practice_address_1: str
    practice_address_2: str
    practice_city: str
    practice_state: str
    practice_zip: str
    practice_phone: str
    source_fetched_at: str
    matched_zips: list[str] = field(default_factory=list)
    source: str = "cms_nppes_registry_api"

    def add_matched_zip(self, zip_code: str) -> None:
        if zip_code not in self.matched_zips:
            self.matched_zips.append(zip_code)

    def to_export_row(self) -> dict[str, str]:
        return {
            "npi": self.npi,
            "first_name": self.first_name,
            "middle_name": self.middle_name,
            "last_name": self.last_name,
            "credential": self.credential,
            "display_name": self.display_name,
            "primary_taxonomy_code": self.primary_taxonomy_code,
            "primary_specialty": self.primary_specialty,
            "practice_address_1": self.practice_address_1,
            "practice_address_2": self.practice_address_2,
            "practice_city": self.practice_city,
            "practice_state": self.practice_state,
            "practice_zip": self.practice_zip,
            "practice_phone": self.practice_phone,
            "matched_zips": ",".join(self.matched_zips),
            "source": self.source,
            "source_fetched_at": self.source_fetched_at,
        }

