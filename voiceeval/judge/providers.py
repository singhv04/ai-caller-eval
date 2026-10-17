"""Built-in LLM judge providers.

Each provider turns (system prompt, user prompt, JSON schema) into JSON text
using that vendor's native structured-output feature, and maps auth errors to
JudgeUnavailable so the affected metrics show "needs LLM" instead of failing.
"""
from __future__ import annotations

from .base import BaseJudge, JudgeUnavailable, register_provider

MAX_OUTPUT_TOKENS = 16000


@register_provider
class AnthropicJudge(BaseJudge):
    name = "anthropic"
    label = "Anthropic Claude"
    key_envs = ("ANTHROPIC_API_KEY",)
    default_model = "claude-opus-5-5"
    package, pip_name = "anthropic", "anthropic"
    supports_effort = True
    effort_levels = ("low", "medium", "high", "xhigh", "max")
    model_hint = "e.g. claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5"
    docs_url = "https://console.anthropic.com/settings/keys"

    def __init__(self, cfg):
        super().__init__(cfg)
        import anthropic

        self.client = anthropic.Anthropic(api_key=cfg.api_key) if cfg.api_key else anthropic.Anthropic()

    def complete_json(self, system, user, schema, schema_name="evaluation"):
        import anthropic

        output_config = {"format": {"type": "json_schema", "schema": schema}}
        if self.cfg.effort:
            output_config["effort"] = self.cfg.effort
        kwargs = dict(model=self.model, max_tokens=MAX_OUTPUT_TOKENS, system=system,
                      messages=[{"role": "user", "content": user}], output_config=output_config)
        try:
            try:
                # On a safety-classifier decline, the API retries on a fallback model within the same call.
                response = self.client.beta.messages.create(
                    **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e.message).lower():
                    raise
                response = self.client.messages.create(**kwargs)  # model without fallback support
        except anthropic.AuthenticationError as e:
            raise JudgeUnavailable(f"Anthropic rejected the API key: {e.message}") from e
        except anthropic.PermissionDeniedError as e:
            raise JudgeUnavailable(f"Anthropic permission denied: {e.message}") from e
        except anthropic.NotFoundError as e:
            raise JudgeUnavailable(f"Anthropic model '{self.model}' not found: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise RuntimeError(f"Anthropic rate limit: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise RuntimeError(f"Anthropic connection error: {e}") from e
        if response.stop_reason == "refusal":
            raise RuntimeError("Anthropic declined to evaluate this transcript")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Anthropic output truncated (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise RuntimeError("Anthropic returned no text output")
        return text, {"model": response.model, "input_tokens": response.usage.input_tokens,
                      "output_tokens": response.usage.output_tokens}

    def list_models(self):
        return [m.id for m in self.client.models.list()]


@register_provider
class OpenAIJudge(BaseJudge):
    name = "openai"
    label = "OpenAI (ChatGPT)"
    key_envs = ("OPENAI_API_KEY",)
    default_model = "gpt-5"
    package, pip_name = "openai", "openai"
    supports_effort = True
    effort_levels = ("minimal", "low", "medium", "high")
    model_hint = "Any chat model with structured outputs; use “Load models” to pick from your account"
    docs_url = "https://platform.openai.com/api-keys"

    def __init__(self, cfg):
        super().__init__(cfg)
        import openai

        self.client = openai.OpenAI(api_key=cfg.api_key or "not-needed", base_url=cfg.base_url or None)

    def _create(self, system, user, response_format):
        kwargs = dict(model=self.model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                      response_format=response_format, max_completion_tokens=MAX_OUTPUT_TOKENS)
        if self.cfg.effort:
            kwargs["reasoning_effort"] = self.cfg.effort
        return self.client.chat.completions.create(**kwargs)

    def complete_json(self, system, user, schema, schema_name="evaluation"):
        import openai

        strict = {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema, "strict": True}}
        try:
            try:
                resp = self._create(system, user, strict)
            except openai.BadRequestError as e:
                # Some models / compatible servers lack strict json_schema: fall back to JSON mode + schema in prompt.
                if "response_format" not in str(e).lower() and "json_schema" not in str(e).lower():
                    raise
                import json as _json
                resp = self._create(system, user + "\n\nJSON schema to follow exactly:\n" + _json.dumps(schema),
                                    {"type": "json_object"})
        except openai.AuthenticationError as e:
            raise JudgeUnavailable(f"{self.label} rejected the API key: {e.message}") from e
        except openai.PermissionDeniedError as e:
            raise JudgeUnavailable(f"{self.label} permission denied: {e.message}") from e
        except openai.NotFoundError as e:
            raise JudgeUnavailable(f"{self.label} model '{self.model}' not found: {e.message}") from e
        except openai.RateLimitError as e:
            raise RuntimeError(f"{self.label} rate limit: {e.message}") from e
        except openai.APIStatusError as e:
            raise RuntimeError(f"{self.label} API error {e.status_code}: {e.message}") from e
        except openai.APIConnectionError as e:
            raise RuntimeError(f"{self.label} connection error: {e}") from e
        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            raise RuntimeError(f"{self.label} declined: {choice.message.refusal}")
        if choice.finish_reason == "length":
            raise RuntimeError(f"{self.label} output truncated (max tokens)")
        usage = resp.usage
        return choice.message.content or "", {"model": resp.model,
                                              "input_tokens": getattr(usage, "prompt_tokens", None),
                                              "output_tokens": getattr(usage, "completion_tokens", None)}

    def list_models(self):
        return sorted(m.id for m in self.client.models.list())


@register_provider
class OpenAICompatibleJudge(OpenAIJudge):
    name = "openai_compatible"
    label = "OpenAI-compatible endpoint"
    key_envs = ("VOICEEVAL_JUDGE_API_KEY",)
    default_model = ""
    needs_base_url = True
    key_optional = True
    supports_effort = False
    effort_levels = ()
    model_hint = "OpenRouter, Groq, Together, DeepSeek, Mistral, Azure OpenAI, Ollama, vLLM, LM Studio…"
    docs_url = ""


@register_provider
class GeminiJudge(BaseJudge):
    name = "gemini"
    label = "Google Gemini"
    key_envs = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    default_model = "gemini-2.5-pro"
    package, pip_name = "google.genai", "google-genai"
    model_hint = "e.g. gemini-2.5-pro, gemini-2.5-flash — use “Load models” to see what your key can access"
    docs_url = "https://aistudio.google.com/apikey"

    def __init__(self, cfg):
        super().__init__(cfg)
        from google import genai

        self.client = genai.Client(api_key=cfg.api_key)

    def complete_json(self, system, user, schema, schema_name="evaluation"):
        from google.genai import errors, types

        config = types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                             response_json_schema=schema, max_output_tokens=MAX_OUTPUT_TOKENS)
        try:
            resp = self.client.models.generate_content(model=self.model, contents=user, config=config)
        except errors.ClientError as e:
            if e.code in (400, 401, 403) and ("key" in str(e).lower() or e.code in (401, 403)):
                raise JudgeUnavailable(f"Gemini rejected the request: {e.message}") from e
            if e.code == 404:
                raise JudgeUnavailable(f"Gemini model '{self.model}' not found: {e.message}") from e
            raise RuntimeError(f"Gemini API error {e.code}: {e.message}") from e
        except errors.ServerError as e:
            raise RuntimeError(f"Gemini server error {e.code}: {e.message}") from e
        text = resp.text
        if not text:
            reason = resp.candidates[0].finish_reason if resp.candidates else "no candidates"
            raise RuntimeError(f"Gemini returned no output ({reason})")
        um = resp.usage_metadata
        return text, {"model": self.model, "input_tokens": getattr(um, "prompt_token_count", None),
                      "output_tokens": getattr(um, "candidates_token_count", None)}

    def list_models(self):
        out = []
        for m in self.client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                out.append(m.name.removeprefix("models/"))
        return sorted(out)
