"""
Unit tests for int16 scaling in raw_sender.scale_to_int16.

No network, no DHN-AQ required.
"""

import numpy as np
import pytest

from darkhorse_neuralynx.udp_raw.raw_sender import scale_to_int16


def test_output_dtype_is_int16():
    arr = np.array([[1.0, -1.0, 0.5]], dtype=np.float64)
    result = scale_to_int16(arr, target_peak=8000)
    assert result.dtype == np.int16


def test_zero_signal_stays_zero():
    arr = np.zeros((10, 4), dtype=np.float64)
    result = scale_to_int16(arr, target_peak=8000)
    assert np.all(result == 0)


def test_peak_is_scaled_to_target():
    arr = np.array([[0.0, 1.0, -1.0, 0.0]], dtype=np.float64)
    result = scale_to_int16(arr, target_peak=8000)
    assert int(np.max(np.abs(result))) == 8000


def test_output_never_exceeds_int16_bounds():
    # Very large values must be clipped, not overflow
    arr = np.array([[1e9, -1e9]], dtype=np.float64)
    result = scale_to_int16(arr, target_peak=8000)
    assert np.all(result >= -32768)
    assert np.all(result <= 32767)


def test_shape_is_preserved():
    arr = np.random.randn(100, 16).astype(np.float64)
    result = scale_to_int16(arr, target_peak=5000)
    assert result.shape == (100, 16)


def test_different_target_peaks():
    arr = np.array([[1.0, -1.0]], dtype=np.float64)
    for peak in [1000, 8000, 20000, 32767]:
        result = scale_to_int16(arr, target_peak=peak)
        assert int(np.max(np.abs(result))) == peak


def test_single_sample():
    arr = np.array([[0.5]], dtype=np.float64)
    result = scale_to_int16(arr, target_peak=100)
    assert result.shape == (1, 1)
    assert result.dtype == np.int16
