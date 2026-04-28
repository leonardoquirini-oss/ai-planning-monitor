import importlib
import pkgutil

# Auto-import di tutti i moduli check nella directory
for _loader, _name, _is_pkg in pkgutil.iter_modules(__path__):
    importlib.import_module(f".{_name}", __name__)
