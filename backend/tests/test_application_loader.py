"""Generic application loader — behaviour + validation guarantees.

The loader must discover and register ERP applications with no hardcoded
business imports, and must reject malformed registries (bad manifests, duplicate
application/module keys, unknown dependencies) so a broken application can never
deploy.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), f"smokestack_loader_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["JWT_SECRET"] = "loader-secret-long-enough"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"

import pytest

from app import apps as loader
from app.platform import registry
from app.platform.registry import AppDescriptor, ModuleSpec, register_application


def setup_module(_m):
    loader.load_apps()


# ------------------------------------------------------------ discovery
def test_loader_discovers_applications_generically():
    names = loader.discovered_app_modules()
    # discovery is by filesystem, not a hardcoded list
    assert "smoke_shop" in names and "catalog" in names
    keys = {a.key for a in registry.applications()}
    assert "smoke_shop" in keys                       # self-registered
    assert "retail" in keys                           # catalog types too


def test_loader_source_hardcodes_no_specific_business():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "apps",
                            "__init__.py"), encoding="utf-8").read().lower()
    for banned in ("smoke_shop", "catalog", "smoke", "vape", "tobacco"):
        assert banned not in src, f"loader hardcodes '{banned}'"


def test_validate_registry_passes_for_the_real_registry():
    assert loader.validate_registry() is True


# ------------------------------------------------------------ validation
def test_duplicate_application_key_is_rejected():
    a = AppDescriptor(key="tmp_dupapp", name="Temp", modules=[])
    register_application(a)
    try:
        with pytest.raises(ValueError):
            register_application(AppDescriptor(key="tmp_dupapp", name="Other"))
    finally:
        registry._REGISTRY.pop("tmp_dupapp", None)


def test_duplicate_module_key_is_rejected():
    # a module key that collides with an existing shared module
    register_application(AppDescriptor(
        key="tmp_dupmod", name="Temp",
        modules=[ModuleSpec("settings", "Clashing Settings", "X")]))
    try:
        with pytest.raises(ValueError):
            loader.validate_registry()
    finally:
        registry._REGISTRY.pop("tmp_dupmod", None)
    assert loader.validate_registry() is True   # clean again


def test_unknown_module_dependency_is_rejected():
    register_application(AppDescriptor(
        key="tmp_baddep", name="Temp",
        modules=[ModuleSpec("tmp_mod", "Temp Mod", "X",
                            depends_on=["does_not_exist"])]))
    try:
        with pytest.raises(ValueError):
            loader.validate_registry()
    finally:
        registry._REGISTRY.pop("tmp_baddep", None)
    assert loader.validate_registry() is True


def test_manifest_missing_name_is_rejected():
    register_application(AppDescriptor(key="tmp_noname", name=""))
    try:
        with pytest.raises(ValueError):
            loader.validate_registry()
    finally:
        registry._REGISTRY.pop("tmp_noname", None)
