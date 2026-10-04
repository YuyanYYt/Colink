from dataclasses import dataclass


@dataclass(frozen=True)
class MetadataClaim:
    field: str
    value: str
