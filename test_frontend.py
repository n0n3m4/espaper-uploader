#!/usr/bin/env python3
"""Self-check for the Lovelace resource registration. No Home Assistant needed.

    python test_frontend.py

The card is registered as a Lovelace *resource* rather than an extra JS URL,
because the frontend awaits resources before it builds cards. That registration
is persistent, so getting the find/update/create branch wrong means a duplicate
entry in the user's dashboard config on every upgrade -- or, in YAML mode, an
exception where a quiet fallback belongs.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

from custom_components.espaper.frontend import (  # noqa: E402
    CARD_URL,
    LOVELACE,
    _async_register_resource,
)


class FakeResources:
    """Enough of ResourceStorageCollection to exercise the three branches."""

    def __init__(self, items=()):
        self.items = [dict(item) for item in items]
        self.loads = 0

    async def async_get_info(self):
        self.loads += 1
        return {"resources": len(self.items)}

    def async_items(self):
        return self.items

    async def async_create_item(self, data):
        assert data["res_type"] == "module", data
        self.items.append({"id": "new", "type": "module", "url": data["url"]})

    async def async_update_item(self, item_id, updates):
        for item in self.items:
            if item["id"] == item_id:
                item.update(updates)
                return
        raise AssertionError(f"no such resource: {item_id}")


def hass_with(resources, mode="storage"):
    return SimpleNamespace(
        data={LOVELACE: SimpleNamespace(resources=resources, resource_mode=mode)}
    )


async def test_creates_when_absent():
    resources = FakeResources()
    assert await _async_register_resource(hass_with(resources), f"{CARD_URL}?v=0.3.0")
    assert resources.items == [
        {"id": "new", "type": "module", "url": f"{CARD_URL}?v=0.3.0"}
    ]
    assert resources.loads == 1, "the collection must be loaded before it is read"


async def test_upgrade_rewrites_instead_of_duplicating():
    resources = FakeResources(
        [{"id": "1", "type": "module", "url": f"{CARD_URL}?v=0.2.0"}]
    )
    assert await _async_register_resource(hass_with(resources), f"{CARD_URL}?v=0.3.0")
    assert len(resources.items) == 1, "the old version was left behind"
    assert resources.items[0]["url"] == f"{CARD_URL}?v=0.3.0"


async def test_already_current_is_a_noop():
    url = f"{CARD_URL}?v=0.3.0"
    resources = FakeResources([{"id": "1", "type": "module", "url": url}])
    assert await _async_register_resource(hass_with(resources), url)
    assert resources.items == [{"id": "1", "type": "module", "url": url}]


async def test_unrelated_resources_are_left_alone():
    resources = FakeResources(
        [{"id": "1", "type": "module", "url": "/hacsfiles/other/other.js"}]
    )
    assert await _async_register_resource(hass_with(resources), f"{CARD_URL}?v=1")
    assert len(resources.items) == 2
    assert resources.items[0]["url"] == "/hacsfiles/other/other.js"


async def test_yaml_mode_falls_back():
    # A YAML dashboard takes its resource list from configuration.yaml, so
    # writing there is not possible; the caller uses add_extra_js_url instead.
    resources = FakeResources()
    assert not await _async_register_resource(
        hass_with(resources, mode="yaml"), f"{CARD_URL}?v=1"
    )
    assert not resources.items

    # ...and so does a Home Assistant whose internals do not look like this.
    assert not await _async_register_resource(
        SimpleNamespace(data={}), f"{CARD_URL}?v=1"
    )


async def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            await fn()
            print(f"ok  {name}")
    print("all good")


if __name__ == "__main__":
    asyncio.run(main())
