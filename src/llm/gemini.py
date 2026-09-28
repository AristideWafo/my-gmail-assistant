import json
import logging

import google.generativeai as genai

from src.gmail.client import EmailMessage

logger = logging.getLogger(__name__)

JOB_ENTITY_FIELDS = ("poste", "entreprise", "stack", "salaire", "prochaine_etape")


class GeminiClient:
    def __init__(self, api_key: str, model_name: str = "gemini-1.5-flash") -> None:
        self.model_name = model_name
        self._enabled = bool(api_key)
        if self._enabled:
            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(model_name)

    @property
    def is_configured(self) -> bool:
        return self._enabled

    def check_connection(self) -> str:
        genai.get_model(f"models/{self.model_name}")
        return f"model {self.model_name} available"

    def summarize(self, email: EmailMessage) -> str:
        if not self._enabled:
            return "Gemini not configured."
        prompt = (
            "Provide a concise summary (max 4 bullet points) of the following email context:\n\n"
            f"Subject: {email.subject}\nSender: {email.sender}\nBody: {email.body or email.snippet}"
        )
        return self._model.generate_content(prompt).text

    def extract_job_entities(self, email: EmailMessage) -> dict:
        if not self._enabled:
            return {}

        fields = ", ".join(JOB_ENTITY_FIELDS)
        prompt = (
            f"Extract these fields from the job-related email below as strict JSON only "
            f"(no markdown fences, no commentary): {fields}. Use null when a field isn't mentioned.\n\n"
            f"Subject: {email.subject}\nSender: {email.sender}\nBody: {email.body or email.snippet}"
        )
        try:
            raw = self._model.generate_content(prompt).text
            return json.loads(raw)
        except (json.JSONDecodeError, AttributeError, ValueError) as exc:
            logger.warning("Failed to parse Gemini job entity extraction: %s", exc)
            return {}

    def draft_reply(self, email: EmailMessage) -> str:
        if not self._enabled:
            return "Gemini not configured."
        prompt = (
            "Draft a professional email response to the thread below. Keep it short and actionable.\n\n"
            f"Subject: {email.subject}\nSender: {email.sender}\nBody: {email.body or email.snippet}"
        )
        return self._model.generate_content(prompt).text
