__version__ = "1.0.1"

# Let frappe.utils.file_manager.get_file_path()/get_file() read R2 files (see compat.py).
from frappe_cloudflare_r2 import compat as _compat  # noqa: E402, F401
