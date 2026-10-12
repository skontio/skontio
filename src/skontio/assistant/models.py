from django.conf import settings
from django.db import models


class LlmConfig(models.Model):
    """Which language model the in-app assistant uses (one row; SKONTIO_LLM_* in the environment wins)."""

    class Kind(models.TextChoices):
        NONE = "none", "Aus"
        OLLAMA = "ollama", "Ollama (lokal, z. B. im Heimnetz)"
        OPENAI = "openai_compatible", "OpenAI-kompatibel"
        ANTHROPIC = "anthropic", "Anthropic API"

    kind = models.CharField("Anbieter", max_length=20, choices=Kind.choices, default=Kind.NONE)
    base_url = models.CharField("Adresse", max_length=300, blank=True, default="")
    model = models.CharField("Modell", max_length=120, blank=True, default="")
    api_key_sealed = models.TextField(blank=True, default="")
    timeout = models.PositiveSmallIntegerField("Timeout je Schritt (Sekunden)", default=300)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "KI-Anbieter"
        verbose_name_plural = "KI-Anbieter"

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.model}".strip()


class AssistantRun(models.Model):
    """One request to the assistant, processed in the background."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Wartet"
        RUNNING = "running", "Läuft"
        DONE = "done", "Fertig"
        FAILED = "failed", "Fehler"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="assistant_runs")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="assistant_runs")
    prompt = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    provider = models.CharField(max_length=160, blank=True, default="")
    messages = models.JSONField(default=list)
    answer = models.TextField(blank=True, default="")
    documents = models.JSONField(default=list)  # ids of drafts created in this run
    error = models.TextField(blank=True, default="")
    steps = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "Assistent-Anfrage"
        verbose_name_plural = "Assistent-Anfragen"

    def __str__(self) -> str:
        return self.prompt[:60]
