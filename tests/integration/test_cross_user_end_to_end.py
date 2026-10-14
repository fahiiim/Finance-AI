"""
End-to-end cross-user isolation, exercised through the real HTTP API.

The unit tests prove each layer in isolation. These prove the layers are
actually WIRED that way — a barrier that exists but sits off the request
path protects nothing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_llm_provider, get_orchestrator
from app.llm.base import LLMProvider
from app.main import app
from app.orchestrator.orchestrator import Orchestrator
from app.schemas.financial import Transaction
from app.schemas.planner import Intent, RawExecutionPlan
from app.services.backend_client import BackendClient
from app.services.conversation_service import ConversationService, InMemoryConversationStore
from app.services.memory_service import MemoryService
from tests.conftest import FakeLLMProvider, make_service_token
from tests.support.in_memory_backend import InMemoryBackendClient


def _plan() -> RawExecutionPlan:
    return RawExecutionPlan(
        intent=Intent.TRANSACTION_QUERY,
        requires_financial_data=True,
        requires_calculation=False,
        needs_clarification=False,
        date_phrase="last month",
    )


class _EchoLLM(FakeLLMProvider):
    """Captures the exact prompt the model would receive."""

    def __init__(self) -> None:
        super().__init__(plans=[_plan(), _plan()], response_text="Here is your answer.")
        self.seen_prompts: list[str] = []

    async def generate(self, *, system_prompt, messages, temperature=0.3, max_tokens=700) -> str:
        self.seen_prompts.append("\n".join(m["content"] for m in messages))
        return await super().generate(
            system_prompt=system_prompt, messages=messages, temperature=temperature, max_tokens=max_tokens
        )


def _client_with(llm: LLMProvider, backend: BackendClient) -> TestClient:
    conversations = ConversationService(InMemoryConversationStore())
    orchestrator = Orchestrator(
        llm_provider=llm,
        backend_client=backend,
        conversation_service=conversations,
        memory_service=MemoryService(conversations, llm),
    )
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_llm_provider] = lambda: llm
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_two_users_never_see_each_others_figures():
    """
    The headline guarantee, end to end: two users ask the same question in
    the same process, and neither prompt contains the other's data.
    """
    llm = _EchoLLM()
    client = _client_with(llm, InMemoryBackendClient())

    for user in ("e2e-user-A", "e2e-user-B"):
        token = make_service_token(user, f"session-{user}")
        resp = client.post(
            "/api/v1/chat",
            json={"session_id": f"session-{user}", "message": "What did I spend last month?"},
            headers={"Authorization": f"Bearer {token}", "X-Session-Id": f"session-{user}"},
        )
        assert resp.status_code == 200, resp.text

    prompt_a, prompt_b = llm.seen_prompts
    # Test data is seeded per user_id, so ids are the tell-tale.
    assert "e2e-user-B" not in prompt_a
    assert "e2e-user-A" not in prompt_b


class _LeakyBackend(InMemoryBackendClient):
    """Correct for everything except transactions, which leak another user."""

    async def get_transactions(self, context, **kwargs):
        return [
            Transaction(
                transaction_id="victim-txn-1",
                account_id="victim-acct",
                amount=Decimal("-4200.00"),
                date=date(2026, 1, 5),
                merchant_name="Victim's Private Clinic",
                owner_user_id="some-other-user",
            )
        ]


def test_a_leaking_backend_produces_an_error_not_an_answer():
    """
    If the backend hands over the wrong user's records, the request must
    fail. The alternative — formatting them into a fluent, confident answer
    and sending it to the wrong person — is the worst outcome this service
    can produce.
    """
    llm = _EchoLLM()
    client = _client_with(llm, _LeakyBackend())
    token = make_service_token("e2e-user-A", "leak-session")

    resp = client.post(
        "/api/v1/chat",
        json={"session_id": "leak-session", "message": "What did I spend last month?"},
        headers={"Authorization": f"Bearer {token}", "X-Session-Id": "leak-session"},
    )

    assert resp.status_code == 500
    body = resp.text
    # The breach must not leak through the error response either.
    assert "Victim's Private Clinic" not in body
    assert "some-other-user" not in body
    assert "4200" not in body
    # And it must never have reached the model.
    assert all("Victim's Private Clinic" not in p for p in llm.seen_prompts)
