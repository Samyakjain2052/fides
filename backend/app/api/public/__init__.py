"""The public API — what customers' systems call, versioned separately.

Kept apart from `app/api/v1` on purpose: the admin API answers to a human session
and can change shape as the console evolves, while these are a contract other
companies deploy code against. Different audiences, different compatibility
promises, different auth.

Two routers, because there are two kinds of caller:

* `v1` — secret keys (`ds_live_…`) held on a customer's servers.
* `banner` — publishable keys (`pk_live_…`) that ship inside a web page, with a
  collect-only capability and provenance stamped on every record.
"""

from app.api.public.banner import router as public_banner_router
from app.api.public.v1 import router as public_v1_router

__all__ = ["public_banner_router", "public_v1_router"]
