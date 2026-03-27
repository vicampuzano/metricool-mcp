"""
Loader for the Data Studio fields YAML dictionary.

Mirrors the Java DataStudioFieldDictionaryLoader +
StaticAvailableDataStudioFieldIdsProvider.

The YAML file (data/data-studio-fields.yaml) is a list of objects with fields:
  fieldId, network, connector, index, metricName, metricLabel,
  description, dataAggregation
"""

import os
from functools import lru_cache

import yaml

_YAML_PATH = os.path.join(os.path.dirname(__file__), "data", "data-studio-fields.yaml")


@lru_cache(maxsize=1)
def load_fields() -> list[dict]:
    """Load and cache all Data Studio field definitions from YAML."""
    with open(_YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def filter_fields(
    fields: list[dict],
    network: str | None = None,
    connector: str | None = None,
) -> list[dict]:
    """Filter the field list by optional network and/or connector."""
    result = fields
    if network:
        result = [f for f in result if f.get("network", "").lower() == network.lower()]
    if connector:
        result = [
            f for f in result if f.get("connector", "").lower() == connector.lower()
        ]
    return result
