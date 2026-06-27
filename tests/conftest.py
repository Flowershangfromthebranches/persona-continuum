from __future__ import annotations

from collections.abc import Iterator

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config


@pytest.fixture()
def app(tmp_path) -> Iterator[PersonaContinuum]:
    config = Config(data_dir=tmp_path / "pc-data")
    continuum = PersonaContinuum(config)
    continuum.init()
    yield continuum
    continuum.close()
