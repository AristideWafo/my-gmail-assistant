import google.generativeai as genai

from src.gmail.client import EmailMessage


class GeminiClient:
    def __init__(self, api_key: str, model_name: str = "gemini-1.5-flash") -> None:
        self.model_name = model_name
        self._enabled = bool(api_key)
        if self._enabled:
            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(model_name)

    def summarize(self, email: EmailMessage) -> str:
        if not self._enabled:
            return "Gemini not configured."
        prompt = (
            "Provide a concise summary (max 4 bullet points) of the following email context:\n\n"
            f"Subject: {email.subject}\nSender: {email.sender}\nBody: {email.body or email.snippet}"
        )
        return self._model.generate_content(prompt).text

    def draft_reply(self, email: EmailMessage) -> str:
        if not self._enabled:
            return "Gemini not configured."
        prompt = (
            "Draft a professional email response to the thread below. Keep it short and actionable.\n\n"
            f"Subject: {email.subject}\nSender: {email.sender}\nBody: {email.body or email.snippet}"
        )
        return self._model.generate_content(prompt).text
