import logging

logger = logging.getLogger(__name__)

# $/1M tokens (input, output), verified on ai.google.dev/gemini-api/docs/pricing.
# Older models (1.5/2.0 flash) are retired and no longer listed there; add only prices you can
# verify.
PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
}
_unpriced_models_warned: set[str] = set()


def estimate_cost_usd(model_name: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """None for a model without a verified price: an invented one would make the budget lie."""
    pricing = PRICING_PER_MILLION_TOKENS.get(model_name)
    if pricing is None:
        if model_name not in _unpriced_models_warned:
            _unpriced_models_warned.add(model_name)
            logger.warning(
                "No verified pricing for model %s; llm_cost_usd_total will not include it",
                model_name,
            )
        return None
    input_price, output_price = pricing
    return (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000
