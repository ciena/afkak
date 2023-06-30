import pytest


@pytest.hookimpl(trylast=True)
def pytest_itemcollected(item):
    """
    this hook is used to cleanup the output of the test names for the integration tests which use
    the pytest `pyargs` option to run the tests in a different directory than the test file is in.
    since the tests are installed in the tox env this is needed to make the test names reflect the top level
    and support shortcut links in IDEs ect.
    :param item:
    :return:
    """
    old_nodeid = item._nodeid
    new_nodeid = old_nodeid.rsplit('/', maxsplit=1)[-1]
    item._nodeid = new_nodeid
    return item
