import pytest

def test_nesting_is_open_gap():
    from src.forumweave import nesting
    with pytest.raises(NotImplementedError):
        nesting.process({"id": "x"})

def test_version_is_open_gap():
    from src.forumweave import version
    with pytest.raises(NotImplementedError):
        version.process({"id": "x"})
