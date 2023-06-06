import pytest


@pytest.hookimpl(trylast=True)
def pytest_itemcollected(item):
    old_nodeid = item._nodeid
    new_nodeid = old_nodeid.rsplit('/', maxsplit=1)[-1]
    item._nodeid = new_nodeid
    return item
