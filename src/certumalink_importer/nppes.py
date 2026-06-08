from __future__ import annotations

import json
import time
from typing import Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class NppesClient:
    BASE_URL = "https://npiregistry.cms.hhs.gov/api/"

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        page_limit: int = 200,
        max_retries: int = 3,
        sleep_seconds: float = 0.5,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.page_limit = page_limit
        self.max_retries = max_retries
        self.sleep_seconds = sleep_seconds

    def iter_zip_search(self, zip_code: str) -> Iterator[Mapping[str, object]]:
        skip = 0
        while True:
            response = self.search_zip(zip_code, skip=skip)
            yield response

            results = response.get("results", [])
            if not isinstance(results, list) or len(results) < self.page_limit:
                break
            skip += self.page_limit

    def search_zip(self, zip_code: str, *, skip: int = 0) -> Mapping[str, object]:
        params = {
            "version": "2.1",
            "enumeration_type": "NPI-1",
            "country_code": "US",
            "postal_code": zip_code,
            "limit": str(self.page_limit),
            "skip": str(skip),
        }
        url = f"{self.BASE_URL}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "certumalink-nppes-importer/0.1"})

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, Mapping):
                    raise ValueError("CMS NPPES API returned a non-object response")
                return payload
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 >= self.max_retries:
                    break
                time.sleep(self.sleep_seconds * (2**attempt))

        raise RuntimeError(f"CMS NPPES API request failed for ZIP {zip_code}: {last_error}")

