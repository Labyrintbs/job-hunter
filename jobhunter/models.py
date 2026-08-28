from __future__ import annotations

from pydantic import BaseModel


class Job(BaseModel):
    source: str
    external_id: str
    title: str
    company: str
    location: str = ""
    language: str = ""
    url: str = ""
    description: str = ""
    contract_type: str = ""
    posted_at: str = ""

    def dedup_key(self) -> tuple[str, str]:
        return (self.source, self.external_id)
