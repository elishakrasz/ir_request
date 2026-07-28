"""RFI classifier interface + stub (spec Phase 2 step 9 / Phase 3).

Ship: a stub returning is_info_request=False. A real LLM backend (Azure OpenAI
or local Ollama) drops in behind CLASSIFIER_BACKEND without touching callers.
Everything a classifier produces is stamped aigenerated=true downstream so human
corrections (humanconfirmed=true) never get masqueraded over.
"""
from dataclasses import dataclass


@dataclass
class RfiResult:
    is_info_request: bool = False
    category: str | None = None      # key into Choices.req_category
    confidence: int = 0              # 0-100


def classify(subject: str, text: str, backend: str = "stub") -> RfiResult:
    """`text` is the transient enriched body (in memory only — never persisted
    beyond the 2,000-char snippet)."""
    if backend == "stub":
        return RfiResult()
    raise NotImplementedError(f"classifier backend '{backend}' not wired yet (Phase 3 approval)")
