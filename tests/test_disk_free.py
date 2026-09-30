import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'server'))


def test_disk_free_fraction_is_a_sane_ratio(tmp_path):
    # utility imports Flask bits at module level; import lazily so the test
    # stays runnable in the lightweight CI env.
    from utility import disk_free_fraction
    frac = disk_free_fraction(str(tmp_path))
    assert 0.0 <= frac <= 1.0
