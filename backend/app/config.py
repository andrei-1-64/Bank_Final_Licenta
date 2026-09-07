"""Central app configuration - the SINGLE Settings object for the whole
project ([A]'s banking backend and [B]'s AI layer both read this one).

Values come from the environment / `.env` only - never hard-code secrets or
per-environment values elsewhere (flow.md rule #7). The `.env` that backs
this lives at `backend/.env`; see `backend/.env.example` for every variable
and `.env` is gitignored.
"""

from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# `backend/` - this file is backend/app/config.py, so two parents up.
# Pinning the path (rather than a bare ".env") means the settings resolve the
# same way whether you run from backend/, from the repo root, or in Docker.
_BACKEND_DIR = Path(__file__).resolve().parent.parent


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


class Settings(BaseSettings):
    """Central app configuration. Values come from the environment / .env
    only — never hard-code secrets or per-environment values elsewhere."""

    model_config = SettingsConfigDict(
        env_file=_BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # A developer's .env may carry keys we don't model; ignore them rather
        # than refusing to start.
        extra="ignore",
    )

    ENV: str = "development"

    # Supabase REST (PostgREST) - no direct Postgres connection is used.
    # SUPABASE_KEY must be the "service_role"/"secret" key: this backend is
    # trusted server code, never exposed to a browser. Never the anon key.
    SUPABASE_URL: str
    SUPABASE_KEY: str

    # Server-side session cookie (see core/security.py, core/dependencies.py).
    SESSION_COOKIE_NAME: str = "session_token"
    SESSION_TTL_SECONDS: int = 60 * 60 * 24 * 7

    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    RATE_LIMIT_PER_MINUTE: int = 120

    # Password reset OTP delivery - a Microsoft Teams "Workflows" incoming
    # webhook URL (Teams channel > Workflows > "Send webhook alerts to a
    # chat"). See app/core/teams.py. None disables delivery: reset codes are
    # still generated/stored, they just never reach the user.
    TEAMS_WEBHOOK_URL: str | None = None

    # Trusted-device cookie (see app/modules/trusted_devices) - separate
    # from the session cookie above, and much longer-lived: it identifies
    # "this browser has completed OTP enrollment before", not "this browser
    # is currently logged in".
    TRUSTED_DEVICE_COOKIE_NAME: str = "trusted_device_token"
    TRUSTED_DEVICE_TTL_SECONDS: int = 60 * 60 * 24 * 180

    # ------------------------------------------------------------------
    # AI layer ([B]-owned app/ai). Azure OpenAI / Azure AI Foundry.
    # ------------------------------------------------------------------
    # Which provider app.ai.service builds by default. "mock" keeps the layer
    # offline; anything else means the real Azure provider, which needs the
    # four AZURE_* values below.
    AI_PROVIDER: str = "mock"

    # The canonical names are the AZURE_OPENAI_* ones. The AZURE_AI_* aliases
    # are accepted as a fallback because that is how an Azure AI Foundry
    # resource labels the same values.
    AZURE_OPENAI_ENDPOINT: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AZURE_OPENAI_ENDPOINT", "AZURE_AI_ENDPOINT"),
    )
    AZURE_OPENAI_API_KEY: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AZURE_OPENAI_API_KEY", "AZURE_AI_API_KEY"),
    )
    AZURE_OPENAI_API_VERSION: str = Field(
        # Recent preview version: required for the gpt-5 family + tool calling.
        default="2025-04-01-preview",
        validation_alias=AliasChoices("AZURE_OPENAI_API_VERSION", "AZURE_AI_API_VERSION"),
    )
    AZURE_OPENAI_DEPLOYMENT: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AZURE_OPENAI_DEPLOYMENT", "AZURE_AI_CHAT_DEPLOYMENT"),
    )

    # Separate deployment for the knowledge-base agent's retrieval (app/ai/knowledge):
    # embeddings are a different model family from chat, so they need their own
    # deployment name even though they share the endpoint/key above.
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "AZURE_AI_EMBEDDING_DEPLOYMENT"
        ),
    )

    # Azure AI Document Intelligence - a different Azure resource from Azure
    # OpenAI, used only by scripts/ingest_knowledge_base.py to turn a PDF's
    # `prebuilt-layout` extraction (tables as structured rows, not flattened
    # prose) into chunks. Never used on the request path.
    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT: str | None = None
    AZURE_DOCUMENT_INTELLIGENCE_KEY: str | None = None

    # ------------------------------------------------------------------
    # vision-service (see vision/ and app/core/vision_client.py)
    # ------------------------------------------------------------------
    # OCR and face-embedding extraction run in a separate container, so the
    # heavy native toolchain (dlib, tesseract, pymupdf) stays out of this
    # image. The default is the compose service name - it is internal to the
    # compose network and has no published port.
    VISION_SERVICE_URL: str = "http://vision:8100"
    # Shared secret, must match VISION_SERVICE_TOKEN in the vision container.
    # Unset means the vision service refuses every request rather than
    # failing open - see vision/app/main.py::require_service_token.
    VISION_SERVICE_TOKEN: str | None = None

    # ------------------------------------------------------------------
    # e-Sign (app/modules/esign) - Ed25519 detached signatures.
    # ------------------------------------------------------------------
    # Base64-encoded 32-byte Ed25519 seed. Generate one with:
    #   python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
    # Unset means signing fails closed (require_esign() below) rather than
    # generating a throwaway key at import time - a signature must be
    # traceable to a key an operator actually chose to keep.
    ESIGN_PRIVATE_KEY: str | None = None
    # Identifies which key signed a given row (signatures.key_id) so a key
    # can be rotated without invalidating signatures made under the old one.
    ESIGN_KEY_ID: str = "esign-dev-1"

    # ------------------------------------------------------------------
    # Azure AI Speech - text-to-speech for the chat "read aloud" button
    # (app/modules/speech). A separate Azure resource from Azure OpenAI
    # above (Project Settings > Keys and Endpoint on the Speech - or
    # multi-service Foundry - resource).
    # ------------------------------------------------------------------
    # A region (e.g. "westeurope", not a full URL) is what the Speech REST
    # synthesize call actually wants: unlike most Cognitive Services APIs,
    # it 404s against a resource's own custom-domain endpoint
    # (https://<resource>.cognitiveservices.azure.com) and needs the
    # standard regional one (https://<region>.tts.speech.microsoft.com)
    # instead - confirmed against a real Azure AI Foundry Speech resource,
    # which has both a custom domain and a region and only the region
    # worked. So REGION wins when both are set (see require_azure_speech
    # below) - the reverse of AZURE_OPENAI_ENDPOINT's precedent above,
    # where the custom domain is exactly what's wanted, and the reverse of
    # what this field used to prefer.
    AZURE_SPEECH_REGION: str | None = None
    # Full resource endpoint - only used when AZURE_SPEECH_REGION is unset,
    # e.g. https://<resource>.cognitiveservices.azure.com
    AZURE_SPEECH_ENDPOINT: str | None = None
    AZURE_SPEECH_KEY: str | None = None
    # A "Multilingual" neural voice (Azure names these explicitly, e.g.
    # "...MultilingualNeural") is what makes one AZURE_SPEECH_VOICE able to
    # speak every language app.ai/frontend support fluently: synthesize()
    # wraps the text in an SSML <lang> tag naming the target language, which
    # this style of voice honors, rather than needing a different voice
    # picked per language.
    AZURE_SPEECH_VOICE: str = "en-US-AndrewMultilingualNeural"
    # Fallback SSML language when a caller doesn't name one.
    AZURE_SPEECH_LANGUAGE: str = "en-US"

    @field_validator("AZURE_OPENAI_ENDPOINT")
    @classmethod
    def _normalise_endpoint(cls, value: str | None) -> str | None:
        """Reduce a Foundry inference URL to the resource root.

        The OpenAI SDK's Azure client appends `/openai/deployments/...` itself,
        so it wants `https://<resource>.services.ai.azure.com`, not the
        `.../models` inference path Foundry shows in its portal.
        """
        if value is None:
            return None
        endpoint = value.strip().rstrip("/")
        if not endpoint:
            return None
        for suffix in ("/models", "/openai/v1", "/openai"):
            if endpoint.endswith(suffix):
                endpoint = endpoint[: -len(suffix)].rstrip("/")
        return endpoint

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() in {"prod", "production"}

    def require_azure(self) -> "AzureOpenAIConfig":
        """Return validated Azure config or raise with an actionable message."""
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", self.AZURE_OPENAI_ENDPOINT),
                ("AZURE_OPENAI_API_KEY", self.AZURE_OPENAI_API_KEY),
                ("AZURE_OPENAI_DEPLOYMENT", self.AZURE_OPENAI_DEPLOYMENT),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "Missing Azure configuration: "
                + ", ".join(missing)
                + f". Add them to {_BACKEND_DIR / '.env'} (see .env.example)."
            )
        # Narrowed by the check above.
        assert self.AZURE_OPENAI_ENDPOINT and self.AZURE_OPENAI_API_KEY
        assert self.AZURE_OPENAI_DEPLOYMENT
        return AzureOpenAIConfig(
            endpoint=self.AZURE_OPENAI_ENDPOINT,
            api_key=self.AZURE_OPENAI_API_KEY,
            api_version=self.AZURE_OPENAI_API_VERSION,
            deployment=self.AZURE_OPENAI_DEPLOYMENT,
        )

    def require_azure_embedding(self) -> "AzureOpenAIConfig":
        """Same contract as `require_azure`, for the embedding deployment.

        Kept separate rather than folded into `require_azure` because a
        deployment can have chat configured without embeddings (or vice
        versa) - the knowledge-base agent is the only thing that needs this
        one, and it should fail on its own rather than blocking chat.
        """
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", self.AZURE_OPENAI_ENDPOINT),
                ("AZURE_OPENAI_API_KEY", self.AZURE_OPENAI_API_KEY),
                ("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", self.AZURE_OPENAI_EMBEDDING_DEPLOYMENT),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "Missing Azure embedding configuration: "
                + ", ".join(missing)
                + f". Add them to {_BACKEND_DIR / '.env'} (see .env.example)."
            )
        assert self.AZURE_OPENAI_ENDPOINT and self.AZURE_OPENAI_API_KEY
        assert self.AZURE_OPENAI_EMBEDDING_DEPLOYMENT
        return AzureOpenAIConfig(
            endpoint=self.AZURE_OPENAI_ENDPOINT,
            api_key=self.AZURE_OPENAI_API_KEY,
            api_version=self.AZURE_OPENAI_API_VERSION,
            deployment=self.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        )

    def require_document_intelligence(self) -> "DocumentIntelligenceConfig":
        """Validated Document Intelligence config.

        Used by the knowledge-base ingestion script and, since Step 13, by
        the statements upload router (app/modules/statements/router.py) -
        both need the same validated endpoint/key pair before calling
        Azure Document Intelligence.
        """
        missing = [
            name
            for name, value in (
                ("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", self.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT),
                ("AZURE_DOCUMENT_INTELLIGENCE_KEY", self.AZURE_DOCUMENT_INTELLIGENCE_KEY),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "Missing Document Intelligence configuration: "
                + ", ".join(missing)
                + f". Add them to {_BACKEND_DIR / '.env'} (see .env.example)."
            )
        assert self.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and self.AZURE_DOCUMENT_INTELLIGENCE_KEY
        return DocumentIntelligenceConfig(
            endpoint=self.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT,
            key=self.AZURE_DOCUMENT_INTELLIGENCE_KEY,
        )

    def require_esign(self) -> str:
        """The raw ESIGN_PRIVATE_KEY value, proven present. Callers still do
        their own base64/length validation - this only enforces "configured
        at all", the same fail-closed shape as require_azure()."""
        if not self.ESIGN_PRIVATE_KEY:
            raise ConfigurationError(
                "Missing ESIGN_PRIVATE_KEY. Add it to "
                f"{_BACKEND_DIR / '.env'} (see .env.example)."
            )
        return self.ESIGN_PRIVATE_KEY

    def require_azure_speech(self) -> "AzureSpeechConfig":
        """Validated Azure Speech config, or raise with an actionable message.

        AZURE_SPEECH_REGION wins when both it and AZURE_SPEECH_ENDPOINT are
        set, building the standard regional endpoint - confirmed against a
        real resource that the synthesize call 404s on its own custom-domain
        endpoint but works on the regional one (see AZURE_SPEECH_REGION's
        docstring above). AZURE_SPEECH_ENDPOINT is the fallback for a
        deployment that only has a custom domain to give.
        """
        endpoint = (
            f"https://{self.AZURE_SPEECH_REGION.strip()}.tts.speech.microsoft.com"
            if self.AZURE_SPEECH_REGION
            else self.AZURE_SPEECH_ENDPOINT
        )
        missing = [
            name
            for name, value in (
                ("AZURE_SPEECH_ENDPOINT or AZURE_SPEECH_REGION", endpoint),
                ("AZURE_SPEECH_KEY", self.AZURE_SPEECH_KEY),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "Missing Azure Speech configuration: "
                + ", ".join(missing)
                + f". Add them to {_BACKEND_DIR / '.env'} (see .env.example)."
            )
        assert endpoint and self.AZURE_SPEECH_KEY
        return AzureSpeechConfig(
            endpoint=endpoint,
            key=self.AZURE_SPEECH_KEY,
            voice=self.AZURE_SPEECH_VOICE,
            default_language=self.AZURE_SPEECH_LANGUAGE,
        )


class AzureOpenAIConfig(BaseSettings):
    """The Azure values, proven present. Never logged."""

    model_config = SettingsConfigDict(extra="forbid")

    endpoint: str
    api_key: str
    api_version: str
    deployment: str


class DocumentIntelligenceConfig(BaseSettings):
    """The Document Intelligence values, proven present. Never logged."""

    model_config = SettingsConfigDict(extra="forbid")

    endpoint: str
    key: str


class AzureSpeechConfig(BaseSettings):
    """The Azure Speech values, proven present. Never logged."""

    model_config = SettingsConfigDict(extra="forbid")

    endpoint: str
    key: str
    voice: str
    default_language: str


settings = Settings()


def get_settings() -> Settings:
    """The process-wide settings singleton.

    Same object as the module-level `settings`; this accessor exists so the
    AI layer can take `Settings | None` and fall back to the shared instance.
    """
    return settings
