"""
Internal implementation of the GRL C3 client - not a public API.

The modules below keep their original top-level names (``API``, ``client``, ``utils``) because they
import one another by those names. They are bundled here, and this directory is placed on
``sys.path`` by ``grl_c3_client/__init__.py``, so those imports keep working without the names being
installed at the top level of site-packages where they would collide.

Import ``grl_c3_client`` instead. Anything in here may change between releases.
"""
