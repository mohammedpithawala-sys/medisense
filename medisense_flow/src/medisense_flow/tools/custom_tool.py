from typing import Type

from pydantic import BaseModel, Field

from crewai.tools import BaseTool

from pinecone import Pinecone
from sentence_transformers import SentenceTransformer
import os


class MyCustomToolInput(BaseModel):
    """Input schema for MyCustomTool."""

    argument: str = Field(..., description="Description of the argument.")


class MyCustomTool(BaseTool):
    name: str = "Name of my tool"
    description: str = "Clear description for what this tool is useful for, your agent will need this information to use it."
    args_schema: Type[BaseModel] = MyCustomToolInput

    def _run(self, argument: str) -> str:
        return "this is an example of a tool output, ignore it and move along."


pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])

index = pc.Index(os.environ["PINECONE_INDEX_NAME"])

# Use the same embedding model used to create your stored vectors
embedding_model = SentenceTransformer("all-MiniLM-L6-v2")


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
            # Convert the user's query into an embedding vector
            query_embedding = embedding_model.encode(
                query,
                normalize_embeddings=True
            ).tolist()

            # Search Pinecone for the most relevant medical documents
            results = index.query(
                vector=query_embedding,
                top_k=5,
                include_metadata=True
            )

            if not results.matches:
                return "No relevant medical information was found."

            # Extract the retrieved document text
            documents = []

            for match in results.matches:
                metadata = match.metadata or {}

                # Adjust these keys to match your Pinecone metadata
                text = (
                    metadata.get("text")
                    or metadata.get("content")
                    or metadata.get("chunk_text")
                )

                if text:
                    documents.append(
                        f"Relevance score: {match.score:.4f}\n"
                        f"Medical information: {text}"
                    )

            if not documents:
                return (
                    "Relevant vectors were retrieved, but no document text "
                    "was found in their metadata. Check your Pinecone "
                    "metadata field names."
                )

            return "\n\n".join(documents)

        except Exception as e:
            return f"Medical information retrieval failed: {str(e)}"


# Create the tool instance
rag_tool = MedicalRAGTool()
