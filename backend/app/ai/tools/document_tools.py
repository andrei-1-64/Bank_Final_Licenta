"""The one tool DocumentAgent has: read_document.

`read_document` takes NO arguments. The document it reads is
`context.active_document_id` - set server-side by chat/router.py after it has
already verified the caller owns that document (see
documents_service.get_document) - never a document_id the model supplies.
That is the whole security boundary: nothing about which document gets read
is ever model-authored.

The extracted text is wrapped in <untrusted_document> tags before it goes
back to the model. That wrapping is not decoration - see
app/ai/agents/document_agent.py's SYSTEM_PROMPT for the other half of this
defense: the model is explicitly told the wrapped content is DATA, not
instructions, and to ignore anything inside it that reads like a command
("ignore your previous instructions", "act as...", etc). Structural isolation
(no write tools, no handoff to other agents - see AIService's tool registry
for DocumentAgent) is the load-bearing guarantee; the tags and the prompt are
defense in depth on top of it, not a substitute for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.core.exceptions import NotFoundError

if TYPE_CHECKING:
    from supabase import AsyncClient


class ReadDocumentInput(BaseModel):
    """Deliberately empty. See the module docstring: the document is scoped
    by Context, not chosen by the model, and an empty schema is what makes
    that a structural guarantee rather than a convention someone could
    accidentally break by adding a field here later."""


class ReadDocumentTool(Tool):
    name = "read_document"
    description = (
        "Citește conținutul documentului atașat conversației. Întoarce textul "
        "extras și numărul de pagini. NU acceptă un ID - folosește "
        "întotdeauna documentul activ al conversației curente."
    )
    input_schema = ReadDocumentInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ReadDocumentInput)
        del validated_input  # no fields to read - see the class docstring

        if not context.active_document_id:
            return ToolResult.failure(
                name=self.name,
                error="Nu există niciun document activ în această conversație.",
            )

        from app.modules.documents import service as documents_service

        try:
            document = await documents_service.get_document(
                self._supabase, context.user_id, context.active_document_id
            )
        except NotFoundError:
            return ToolResult.failure(
                name=self.name,
                error="Documentul activ nu mai este disponibil.",
            )

        wrapped = (
            "<untrusted_document>\n"
            f"Filename: {document['filename']}\n"
            f"Pages: {document['page_count']}\n"
            "--- BEGIN DOCUMENT CONTENT ---\n"
            f"{document['extracted_text']}\n"
            "--- END DOCUMENT CONTENT ---\n"
            "</untrusted_document>"
        )
        return ToolResult(
            name=self.name,
            data={
                "content": wrapped,
                "filename": document["filename"],
                "page_count": document["page_count"],
            },
        )
