from __future__ import annotations
from dataclasses import dataclass
from typing import List, Any, Dict
import yaml

@dataclass(frozen=True)
class InlabsConfig:
    base_url: str

@dataclass(frozen=True)
class FiltersConfig:
    orgao_contains: str
    keywords_any: List[str]

@dataclass(frozen=True)
class MailConfig:
    enabled: bool
    smtp_host: str
    smtp_port: int
    from_email: str
    to_emails: List[str]
    subject_prefix: str

@dataclass(frozen=True)
class StorageConfig:
    sqlite_path: str

@dataclass(frozen=True)
class AppConfig:
    inlabs: InlabsConfig
    filters: FiltersConfig
    mail: MailConfig
    storage: StorageConfig

def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f)

    return AppConfig(
        inlabs=InlabsConfig(**raw["inlabs"]),
        filters=FiltersConfig(
            orgao_contains=raw["filters"]["orgao_contains"],
            keywords_any=list(raw["filters"]["keywords_any"]),
        ),
        mail=MailConfig(
            enabled=bool(raw["mail"]["enabled"]),
            smtp_host=str(raw["mail"]["smtp_host"]),
            smtp_port=int(raw["mail"]["smtp_port"]),
            from_email=str(raw["mail"]["from_email"]),
            to_emails=list(raw["mail"]["to_emails"]),
            subject_prefix=str(raw["mail"]["subject_prefix"]),
        ),
        storage=StorageConfig(sqlite_path=str(raw["storage"]["sqlite_path"])),
    )
