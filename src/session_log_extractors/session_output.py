"""Filename allocation for batches of exported sessions."""


def unique_identifier(base: str, used: set[str]) -> str:
    """Reserve an unused name, suffixing collisions from 2 upward."""
    identifier = base
    suffix = 2
    while identifier in used:
        identifier = f"{base}-{suffix}"
        suffix += 1
    used.add(identifier)
    return identifier
