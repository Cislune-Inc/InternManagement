from agent.openai_models import ModelFallbackChain


def test_model_fallback_chain_moves_successful_backup_to_front() -> None:
    chain = ModelFallbackChain("advisor", "gpt-5-mini", "gpt-4.1-mini", "gpt-4.1-mini")

    assert chain.candidate_models() == ["gpt-5-mini", "gpt-4.1-mini"]

    chain.record_success("gpt-4.1-mini")

    assert chain.candidate_models() == ["gpt-4.1-mini", "gpt-5-mini"]
