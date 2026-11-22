"""Isolated store creation and fixture paths for tests."""

from pathlib import Path

from tuber_studio.core.store import Store
from tuber_studio.persona.assets.illustration_policy import migrate_illustration_policy
from tuber_studio.persona.characters.presets import upgrade_bundled_characters

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
STARTUP_MIGRATIONS = (migrate_illustration_policy, upgrade_bundled_characters)


def create_store(path, seeds, *, migrations=STARTUP_MIGRATIONS):
    return Store(path, seeds, migrations=migrations)
