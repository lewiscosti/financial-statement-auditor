"""Core PDF ingestion and financial red-flag extraction for local and cloud LLM analysis."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

import fitz
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen3.6:27b"
DEFAULT_MAX_TOKENS = 6144
DEFAULT_TEMPERATURE = 0.1

RiskLevel = Literal["High", "Medium", "Low"]


class RiskItem(BaseModel):
    """A single financial red flag identified in a document."""

    category: str
    flag_title: str
    risk_level: RiskLevel
    excerpt: str
    analysis: str
    page_number: int = Field(ge=1)


class RedFlagReport(BaseModel):
    """Structured collection of red flags extracted from a financial document."""

    risk_items: list[RiskItem] = Field(default_factory=list)


class ExtractionError(Exception):
    """Raised when PDF ingestion or red-flag analysis fails."""


class LocalAPIError(ExtractionError):
    """Raised when the OpenAI-compatible API is unreachable or returns an error."""


class ParseError(ExtractionError):
    """Raised when the model response cannot be parsed into a RedFlagReport."""


def extract_pdf_chunks(pdf_path: str | Path, pages_per_chunk: int = 12) -> list[str]:
    """
    Ingest a PDF file and extract text in page-window chunks using PyMuPDF (fitz).

    Each page is prefixed with a marker (``--- Page N ---``) so downstream
    analysis can attribute excerpts to specific pages.

    Args:
        pdf_path: Filesystem path to the PDF file.
        pages_per_chunk: Number of pages to group into a single analysis chunk.

    Returns:
        List of concatenated text blocks representing page ranges.

    Raises:
        ExtractionError: If the file is missing, unreadable, or yields no text.
    """
    path = Path(pdf_path)
    if not path.is_file():
        raise ExtractionError(f"PDF file not found: {path}")

    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise ExtractionError(f"Failed to open PDF '{path}': {exc}") from exc

    chunks: list[str] = []
    current_chunk_pages: list[str] = []

    try:
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            page_number = page_index + 1
            text = page.get_text("text").strip()
            
            if text:
                current_chunk_pages.append(f"--- Page {page_number} ---\n{text}")

            if len(current_chunk_pages) == pages_per_chunk:
                chunks.append("\n\n".join(current_chunk_pages))
                current_chunk_pages = []

        if current_chunk_pages:
            chunks.append("\n\n".join(current_chunk_pages))

    except Exception as exc:
        raise ExtractionError(f"Failed to read PDF pages from '{path}': {exc}") from exc
    finally:
        doc.close()

    if not chunks or not any(chunk.strip() for chunk in chunks):
        raise ExtractionError(f"PDF '{path}' contains no extractable text.")

    return chunks


def analyze_pdf_text(
    pdf_path: str | Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = "ollama",
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    progress_callback=None,
) -> RedFlagReport:
    """
    Analyze extracted PDF text in chunks for financial red flags via local or cloud LLM.

    Connects to an OpenAI-compatible endpoint (default: Ollama at
    ``http://localhost:11434/v1``) and requests structured output conforming
    to :class:`RedFlagReport`.

    Args:
        pdf_path: Path object or string pointing to the PDF file.
        base_url: OpenAI-compatible API base URL.
        api_key: API key string for authenticating with cloud endpoints (default: "ollama").
        model: Model identifier served by the runtime.
        max_tokens: Maximum tokens for the completion response.
        temperature: Sampling temperature (lower = more deterministic).
        progress_callback: Optional callable receiving (current_chunk, total_chunks).

    Returns:
        A validated :class:`RedFlagReport` instance.

    Raises:
        ExtractionError: If ``pdf_path`` cannot be read or is empty.
        LocalAPIError: If the API is unreachable or returns an HTTP error.
        ParseError: If all chunks fail to yield valid JSON schema output.
    """
    chunks = extract_pdf_chunks(pdf_path)
    total_chunks = len(chunks)

    # Use provided api_key or fallback to "ollama" for unauthenticated local endpoints
    effective_api_key = api_key.strip() if api_key and api_key.strip() else "ollama"
    client = OpenAI(base_url=base_url, api_key=effective_api_key)

    # Determine if endpoint is local to include Ollama-specific extra_body settings
    is_local = "localhost" in base_url or "127.0.0.1" in base_url
    
    extra_body = {}
    if is_local:
        extra_body = {
            "think": False,
            "options": {
                "num_ctx": 32768,
                "num_predict": max_tokens,
                "num_batch": 2048,
                "num_thread": 6,
            },
        }

    system_prompt = (
        "You are an automated financial due-diligence API endpoint. "
        "OUTPUT VALID JSON ONLY. Do NOT output internal reasoning or thinking logs. "
        "Review the supplied document text block and identify material red flags—accounting "
        "irregularities, liquidity concerns, governance issues, related-party "
        "transactions, going-concern language, covenant breaches, and similar risks. "
        "For each finding, set risk_level to exactly one of: High, Medium, or Low. "
        "Quote a concise excerpt from the source and cite the page_number where it appears. "
        "Keep the 'analysis' concise (1-2 sentences max per flag) to ensure output constraints are met. "
        "If no red flags are found in this chunk, return an empty risk_items list."
    )

    aggregated_risk_items: list[RiskItem] = []
    successful_chunks = 0

    for chunk_idx, chunk in enumerate(chunks, start=1):
        if progress_callback:
            progress_callback(chunk_idx, total_chunks)
        try:
            kwargs = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": chunk},
                ],
                "response_format": RedFlagReport,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if extra_body:
                kwargs["extra_body"] = extra_body

            completion = client.beta.chat.completions.parse(**kwargs)

        except (APIConnectionError, APITimeoutError) as exc:
            logger.exception(f"LLM API connection failed on chunk {chunk_idx}")
            raise LocalAPIError(
                f"Could not reach API at {base_url}. "
                "Ensure Ollama or your cloud provider configuration is correct."
            ) from exc
        except APIStatusError as exc:
            logger.exception(f"LLM API returned an error status on chunk {chunk_idx}")
            raise LocalAPIError(
                f"API error (HTTP {exc.status_code}): {exc.message}"
            ) from exc
        except Exception as exc:
            logger.exception(f"Unexpected error calling LLM API on chunk {chunk_idx}")
            raise LocalAPIError(f"API request failed: {exc}") from exc

        message = completion.choices[0].message

        # Path 1: Native Pydantic schema validation success
        if message.parsed is not None:
            aggregated_risk_items.extend(message.parsed.risk_items)
            successful_chunks += 1
            continue

        # Path 2: Manual JSON fallback parsing
        raw_content = message.content
        if not raw_content or not raw_content.strip():
            logger.warning(f"Chunk {chunk_idx} returned an empty response, skipping.")
            continue

        try:
            payload = json.loads(raw_content)
            parsed_report = RedFlagReport.model_validate(payload)
            aggregated_risk_items.extend(parsed_report.risk_items)
            successful_chunks += 1
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.error(f"Failed to parse chunk {chunk_idx} output: {exc}")

    if len(chunks) > 0 and successful_chunks == 0:
        raise ParseError("Model failed to return valid JSON output across all document chunks.")

    return RedFlagReport(risk_items=aggregated_risk_items)