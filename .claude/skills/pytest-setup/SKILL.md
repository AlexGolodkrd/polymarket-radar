# pytest Setup & Best Practices

**Source**: jonlwowski012/copilot-agent-factory/skill-templates/1-testing-quality/pytest-setup

## Why we don't use pytest (yet)

Our `tests/` use `unittest` (stdlib). 355 tests, all passing. No need to migrate unless we adopt:
- `pytest-asyncio` (when we go async — Phase 9eee)
- `hypothesis` (property-based testing for arb math)
- `pytest-xdist` (parallel test execution — speeds up our 4s suite)

## Migration path (if/when we do)

```bash
pip install pytest pytest-cov pytest-watch pytest-xdist
```

`pyproject.toml`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = "test_*.py"
addopts = "-ra --strict-markers --cov=Scripts --cov-report=term"
```

`conftest.py` (project root):
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "Scripts"))
```

This replaces the `sys.path.insert` boilerplate at the top of every `test_*.py`.

### Useful patterns

**Parametrize**:
```python
@pytest.mark.parametrize("sum_yes,expected_arb", [
    (0.95, True),
    (1.05, False),
    (0.97, True),
])
def test_a_threshold(sum_yes, expected_arb): ...
```

**Fixtures**:
```python
@pytest.fixture
def mock_clob():
    return {'token1': (0.40, 100, 0.55, 200)}

def test_eval_uses_clob(mock_clob): ...
```

**Async tests** (post-async migration):
```python
@pytest.mark.asyncio
async def test_async_fetch(): ...
```

**Parallel run** (4x faster on 4-core):
```bash
pytest -n auto
```

## Repository

https://github.com/jonlwowski012/copilot-agent-factory
