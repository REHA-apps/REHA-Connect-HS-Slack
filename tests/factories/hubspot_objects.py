# tests/factories/hubspot_objects.py  # noqa: D100
def make_contact(**overrides):
    base = {
        "id": "123",
        "type": "contact",
        "properties": {
            "firstname": "Alice",
            "lastname": "Smith",
            "email": "alice@example.com",
        },
    }
    base.update(overrides)
    return base
