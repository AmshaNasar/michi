"""Personal digital-twin assistant."""

import warnings

# This machine only has Python 3.9, which google-auth and urllib3 both warn
# about on every import. The warnings are accurate but they bury real output,
# so they're filtered at the package boundary rather than per-module.
warnings.filterwarnings("ignore", category=FutureWarning, module=r"google\..*")
warnings.filterwarnings("ignore", message=r".*urllib3 v2 only supports OpenSSL.*")
