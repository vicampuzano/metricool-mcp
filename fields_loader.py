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


def _strip_network_connector_prefix(label: str) -> str:
    """Drop the "Network Connector > " prefix from a metric label."""
    return label.split(">", 1)[-1].strip() if ">" in label else label


@lru_cache(maxsize=1)
def field_labels() -> dict[str, str]:
    """Return a mapping of fieldId → short human-readable label.

    Examples: "IGEV01" → "Followers", "evdate" → "date".
    """
    labels: dict[str, str] = {}
    for f in load_fields():
        fid = f.get("fieldId", "")
        raw = f.get("metricLabel", fid)
        labels[fid] = _strip_network_connector_prefix(raw)
    # Special case: evdate is the date dimension for evolution data
    labels["evdate"] = "date"
    return labels


def available_connectors_for_network(network: str) -> list[str]:
    """Return the connectors that have active (non-deprecated) fields for a network.

    Used to hint alternatives when a discovery call returns 0 results.
    """
    if not network:
        return []
    net_lower = network.lower()
    seen: set[str] = set()
    for f in load_fields():
        if f.get("network", "").lower() != net_lower:
            continue
        if f.get("metricLabel", "").lower().startswith("deprecated"):
            continue
        conn = f.get("connector", "")
        if conn:
            seen.add(conn)
    return sorted(seen)


def _compatibility_group(field: dict) -> str:
    """Derive the compatibility group for a field.

    Evolution fields (connector=EV) can be combined across networks → "evolution".
    All other fields must share the same 4-char prefix → e.g. "FBPO", "IGRE".
    """
    fid = field.get("fieldId", "")
    if len(fid) >= 4 and fid[2:4].upper() == "EV":
        return "evolution"
    return fid[:4].upper() if len(fid) >= 4 else ""


def filter_fields(
    fields: list[dict],
    network: str | None = None,
    connector: str | None = None,
) -> list[dict]:
    """Filter the field list by optional network and/or connector.

    Each returned field includes a ``compatibilityGroup`` key so callers know
    which fields can be combined in a single get_analytics_data_by_metrics call.
    """
    result = fields
    if network:
        result = [f for f in result if f.get("network", "").lower() == network.lower()]
    if connector:
        result = [
            f for f in result if f.get("connector", "").lower() == connector.lower()
        ]
    # Exclude deprecated fields — they should not be exposed to the LLM
    result = [
        f for f in result
        if not f.get("metricLabel", "").lower().startswith("deprecated")
    ]
    # When filtering by both network AND connector the "Network Connector > "
    # prefix in labels is redundant, so strip it to save tokens.
    strip = bool(network and connector)
    output: list[dict] = []
    for f in result:
        raw_label = f.get("metricLabel", "")
        agg = f.get("dataAggregation", "")
        entry = {
            "fieldId": f.get("fieldId", ""),
            "label": _strip_network_connector_prefix(raw_label) if strip else raw_label,
            "description": f.get("description", ""),
            "fieldType": "dimension" if not agg else "metric",
            "compatibilityGroup": _compatibility_group(f),
        }
        if agg:
            entry["aggregation"] = agg
        output.append(entry)
    return output
