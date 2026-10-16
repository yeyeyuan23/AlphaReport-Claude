from __future__ import annotations
from typing import Optional

from functools import lru_cache
from pathlib import Path

import yaml

from reportagent.utils.config import PROJECT_ROOT, get_config


@lru_cache(maxsize=1)
def load_taxonomy(path  : Optional[str] = None) -> dict:
    if path is None:
        rel = get_config("classification", "taxonomy_path", default="configs/classification_taxonomy.yaml")
        path = str(PROJECT_ROOT / rel)
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("taxonomy", {})


def get_dimension_keywords(dimension: str) -> dict[str, list[str]]:
    taxonomy = load_taxonomy()
    dim = taxonomy.get(dimension, {})
    values = dim.get("values", {})
    return {k: v.get("keywords", []) for k, v in values.items()}


def get_all_dimensions() -> list[str]:
    return list(load_taxonomy().keys())
