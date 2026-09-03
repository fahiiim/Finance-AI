"""
FastAPI dependency providers.

Centralizes construction of the LLM provider, backend client, and
orchestrator so route handlers only ever declare `Depends(get_orchestrator)`
— see spec section 35 (thin routes).
"""

from __future__ import annotations

from functools import lru_cache

from app.llm.base import LLMProvider
from app.llm.openai_provider import OpenAIProvider
from app.orchestrator.orchestrator import Orchestrator
from app.services.backend_client import BackendClient, get_backend_client
from app.services.conversation_service import ConversationService
from app.services.memory_service import MemoryService


@lru_cache
def get_llm_provider() -> LLMProvider:
    return OpenAIProvider()


@lru_cache
def get_conversation_service() -> ConversationService:
    return ConversationService()


def get_memory_service() -> MemoryService:
    return MemoryService(get_conversation_service(), get_llm_provider())


def get_backend_client_dep() -> BackendClient:
    return get_backend_client()


def get_orchestrator() -> Orchestrator:
    return Orchestrator(
        llm_provider=get_llm_provider(),
        backend_client=get_backend_client_dep(),
        conversation_service=get_conversation_service(),
        memory_service=get_memory_service(),
    )
