from __future__ import annotations

from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):
    layer_markers = {
        "unit": pytest.mark.unit,
        "integration": pytest.mark.integration,
        "optional": pytest.mark.optional,
        "online": pytest.mark.online,
    }

    for item in items:
        parts = Path(str(item.fspath)).parts
        for layer, marker in layer_markers.items():
            if layer in parts:
                item.add_marker(marker)
                break
