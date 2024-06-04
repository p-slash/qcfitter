# from https://docs.pytest.org/en/latest/example/
# simple.html#control-skipping-of-tests-according-to-command-line-option
# accessed on Dec 31, 2022

import pytest
import numpy as np


@pytest.fixture
def setup_data():
    cat_dtype = np.dtype([
        ('TARGETID', '>i8'), ('Z', '>f8'), ('RA', '>f8'), ('DEC', '>f8'),
        ('HPXPIXEL', '>i8'), ('SURVEY', '<U4'), ('PETAL_LOC', 'i2'),
        ('FIBER', 'i4'), ('NIGHT', 'i4'), ('EXPID', 'i4'), ('TILEID', 'i4')])

    def _setup_data(nspec):
        cat_by_survey = np.array(
            [(39627939372861215, 2.328, 229.86, 6.19, 8258, b'main', 1, 240,
              20230918, 10000, 82788)] * nspec,
            dtype=cat_dtype)
        inc = np.roll(np.arange(nspec), 2)
        cat_by_survey['TARGETID'] += inc

        npix = 1000
        data = {
            'wave': {
                'B': 3600. + 0.8 * np.arange(npix),
                'R': 4000. + 0.8 * np.arange(npix)},
            'flux': {
                'B': (2.1 + inc[:, np.newaxis]) * np.ones((nspec, npix)),
                'R': (2.1 + inc[:, np.newaxis]) * np.ones((nspec, npix))},
            'ivar': {
                'B': np.ones((nspec, npix)),
                'R': np.ones((nspec, npix))},
            'mask': {
                'B': np.zeros((nspec, npix), dtype='i4'),
                'R': np.zeros((nspec, npix), dtype='i4')},
            'reso': {}
        }

        return cat_by_survey, npix, data

    return _setup_data


def pytest_addoption(parser):
    parser.addoption(
        "--mpi", action="store_true", default=False,
        help="Run MPI tests"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "mpi: mark test to run with mpi")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--mpi"):
        return
    skip_mpi = pytest.mark.skip(reason="need --mpi option to run")
    for item in items:
        if "mpi" in item.keywords:
            item.add_marker(skip_mpi)
