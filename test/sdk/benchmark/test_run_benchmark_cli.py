import argparse

import pytest

from sdk.benchmark.generic.run_benchmark import non_negative_int, positive_int


@pytest.mark.parametrize("value", ["0", "-1"])
def test_positive_int_rejects_non_positive_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        positive_int(value)


@pytest.mark.parametrize("value", ["-1", "-20"])
def test_non_negative_int_rejects_negative_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        non_negative_int(value)


def test_cli_integer_validators_accept_boundaries():
    assert positive_int("1") == 1
    assert non_negative_int("0") == 0
