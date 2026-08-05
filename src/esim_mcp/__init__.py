"""eSIM MCP server package.

Scope implemented here:

* **Phase 1** -- production-ready MCP foundation plus multi-user OTP authentication
  against the existing eSIM backend.
* **Phase 2** -- read-only catalogue and bundle discovery: countries, regions, the home
  catalogue, country/region/cruise/global bundles and bundle details, with client-side
  filtering and sorting. Browsing requires no login.

No order, purchase, payment, Stripe, wallet-mutation, promotion, voucher, provisioning,
top-up, consumption or callback functionality is implemented here.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
