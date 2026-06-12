"""src/utils.py — close_series / pick_first."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.exceptions import DataValidationError
from src.utils import close_series, pick_first


def test_close_series_prefers_adj_close(ohlcv_frame):
    s = close_series(ohlcv_frame)
    pd.testing.assert_series_equal(
        s, ohlcv_frame["Adj Close"], check_names=False
    )


def test_close_series_falls_back_to_close(ohlcv_frame):
    df = ohlcv_frame.drop(columns=["Adj Close"])
    s = close_series(df)
    pd.testing.assert_series_equal(s, df["Close"], check_names=False)


def test_close_series_raises_when_no_close_columns():
    df = pd.DataFrame({"Open": [1.0, 2.0]})
    with pytest.raises(DataValidationError):
        close_series(df)


def test_pick_first_returns_first_existing_non_nan():
    row = pd.Series({"revenue": np.nan, "Total Revenue": 123.0})
    assert pick_first(row, ["revenue", "Total Revenue"]) == 123.0


def test_pick_first_returns_none_when_all_missing():
    row = pd.Series({"a": 1.0})
    assert pick_first(row, ["x", "y"]) is None
