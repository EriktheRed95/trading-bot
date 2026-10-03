"""Private import of trading research from public links and local media files.

Imported material is unverified source content. It is stored in its own local store, shown
for review, and kept entirely separate from the paper engine: nothing here imports or calls
the trading code, and nothing the importer produces can change a strategy, place an order or
count as a validated finding. See RESEARCH-IMPORT.md.
"""
from .service import ResearchImports   # noqa: F401
from .store import StoreError          # noqa: F401
