import os
from typing import Type

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from crewai.tools import BaseTool
from pinecone import Pinecone

load_dotenv()

# ---- Pinecone configuration ----
# The index must be created in the Pinecone dashboard with the integrated
# embedding model "llama-text-embed-v2" (dimension 384). Pinecone embeds the
# query text on its own servers, so no local embedding model is needed.
#
# .env:
#   PINECONE_API_KEY=...
#   PINECONE_INDEX_NAME=...
#   PINECONE_NAMESPACE=__default__     (optional; "__default__" is the default namespace)
PINECONE_NAMESPACE = os.getenv("PINECONE_NAMESPACE", "__default__")
TOP_K = 5

_index = None


def _get_index():
    """Create the Pinecone index handle once, on first use."""
    global _index
    if _index is None:
        pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        _index = pc.Index(os.environ["PINECONE_INDEX_NAME"])
    return _index


class MyCustomToolInput(BaseModel):
    """Input schema for MyCustomTool."""

    argument: str = Field(..., description="Description of the argument.")


class MyCustomTool(BaseTool):
    name: str = "Name of my tool"
    description: str = "Clear description for what this tool is useful for, your agent will need this information to use it."
    args_schema: Type[BaseModel] = MyCustomToolInput

    def _run(self, argument: str) -> str:
        return "this is an example of a tool output, ignore it and move along."


class MedicalRAGToolInput(BaseModel):
    """Input schema for the Medical RAG Tool."""

    query: str = Field(
        ...,
        description=(
            "The user's medical query, including symptoms or "
            "a healthcare question, to search in the medical vector database."
        ),
    )


class MedicalRAGTool(BaseTool):
    name: str = "Medical RAG Search Tool"

    description: str = (
        "Searches a Pinecone vector database for medical information "
        "relevant to a user's symptoms or healthcare query. Returns "
        "the most relevant retrieved medical documents for diagnostic support."
    )

    args_schema: Type[BaseModel] = MedicalRAGToolInput

    def _run(self, query: str) -> str:
        try:
            index = _get_index()

            # Text in: Pinecone embeds the query with llama-text-embed-v2
            # (the index's own embedding model) and searches.
            response = index.search(
                namespace=PINECONE_NAMESPACE,
                query={
                    "inputs": {"text": query},
                    "top_k": TOP_K,
                },
            )

            hits = response.result.hits
            if not hits:
                return "No relevant medical information was found."

            documents = []

            for hit in hits:
                fields = getattr(hit, "fields", None) or {}

                # Adjust these keys to match the text field name you chose
                # in the index's field map (often "text" or "chunk_text").
                text = (
                    fields.get("text")
                    or fields.get("chunk_text")
                    or fields.get("content")
                )

                # SDK versions expose the score as `score` or `_score`.
                score = getattr(hit, "score", None)
                if score is None:
                    score = getattr(hit, "_score", 0.0)

                if text:
                    documents.append(
                        f"Relevance score: {score:.4f}\n"
                        f"Medical information: {text}"
                    )

            if not documents:
                return (
                    "Relevant records were retrieved, but no document text "
                    "was found in their fields. Check the text field name "
                    "in your Pinecone index field map."
                )

            return "\n\n".join(documents)

        except Exception as e:
            return f"Medical information retrieval failed: {str(e)}"


# Create the tool instance
rag_tool = MedicalRAGTool()