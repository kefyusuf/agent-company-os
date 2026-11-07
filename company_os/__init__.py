"""AI Company OS contract-first persistence foundation (not an LLM runtime)."""
from .store import Actor, Store, DomainError
__all__ = ["Actor", "Store", "DomainError"]
