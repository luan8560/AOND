import aioi.config as config
from aioi.config import load_settings


def test_max_photos_defaults_to_20(monkeypatch):
    monkeypatch.delenv("AIOI_MAX_PHOTOS_PER_LISTING", raising=False)
    assert load_settings().max_photos_per_listing == 20


def test_max_photos_read_from_env(monkeypatch):
    monkeypatch.setenv("AIOI_MAX_PHOTOS_PER_LISTING", "8")
    assert load_settings().max_photos_per_listing == 8


def test_max_photos_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AIOI_MAX_PHOTOS_PER_LISTING", "nhiều")
    assert load_settings().max_photos_per_listing == 20


def test_int_env_helper(monkeypatch):
    monkeypatch.setenv("X", "3")
    assert config._int_env("X", 9) == 3
    monkeypatch.setenv("X", "")
    assert config._int_env("X", 9) == 9      # empty → default
    monkeypatch.setenv("X", "oops")
    assert config._int_env("X", 9) == 9      # invalid → default
