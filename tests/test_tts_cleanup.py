import importlib.util
import weakref
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "tts_cleanup_bridge", Path(__file__).parents[1] / "tools/jetson/tts/serve.py"
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.mark.parametrize("close_early", [False, True])
def test_cleanup_runs_after_generator_tensor_references_are_released(close_early):
    references, observed = [], []

    class Tensor:
        pass

    class Pipeline:
        def empty_cache(self):
            observed.append(any(reference() is not None for reference in references))

        @bridge.release_after_run
        def run(self):
            tensor = Tensor()
            references.append(weakref.ref(tensor))
            try:
                yield (32000, b"unchanged audio")
            finally:
                self.empty_cache()

    iterator = Pipeline().run()
    assert next(iterator) == (32000, b"unchanged audio")
    if close_early:
        iterator.close()
    else:
        assert list(iterator) == []
    assert observed == [True, False]
