"""Catalog of ERP application TYPES available on the platform.

These are registered so the Control Center can offer them when creating a
company, but they carry no modules and are inactive until their application is
actually built (and self-registers its own modules + logic, like smoke_shop.py).
Adding a real ERP later = drop in a new app module under app/apps/ — the core
platform is never touched.
"""
from ..platform.registry import AppDescriptor, register_application

# key, display name, industry
_TYPES = [
    ("retail", "Retail ERP", "Retail"),
    ("restaurant", "Restaurant ERP", "Food & Beverage"),
    ("warehouse", "Warehouse ERP", "Logistics"),
    ("manufacturing", "Manufacturing ERP", "Manufacturing"),
    ("accounting", "Accounting ERP", "Accounting"),
    ("auto_parts", "Auto Parts ERP", "Automotive"),
    ("clinic", "Clinic ERP", "Healthcare"),
    ("construction", "Construction ERP", "Construction"),
]

for _key, _name, _industry in _TYPES:
    register_application(AppDescriptor(key=_key, name=_name, industry=_industry,
                                       description=f"{_name} (registered — not yet built).",
                                       active=False, modules=[]))
