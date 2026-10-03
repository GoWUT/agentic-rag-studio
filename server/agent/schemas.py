"""Structured LLM outputs for Agent Graph V2."""

from pydantic import BaseModel, Field
from typing import Literal

from server.agent.state import QueryType, SourceName


class QueryAnalysis(BaseModel):
    standalone_query: str = Field(min_length=1)
    query_type: QueryType
    needs_retrieval: bool
    selected_sources: list[SourceName] = Field(default_factory=list, max_length=3)


class RetrievalStep(BaseModel):
    query: str = Field(min_length=1)
    sources: list[SourceName] = Field(min_length=1, max_length=3)


class ResearchPlan(BaseModel):
    steps: list[RetrievalStep] = Field(min_length=1, max_length=3)


class EvidenceGrade(BaseModel):
    relevance: float = Field(ge=0, le=1)
    coverage: float = Field(ge=0, le=1)
    sufficient: bool
    missing_information: list[str] = Field(default_factory=list, max_length=5)


class QueryRefinement(BaseModel):
    refined_query: str = Field(min_length=1)
    sources: list[SourceName] = Field(min_length=1, max_length=3)


class PlanStep(BaseModel):
    id: str
    description: str = Field(min_length=1)
    query: str = Field(min_length=1)
    sources: list[SourceName] = Field(default_factory=list)
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    preferred_tool: str | None = None
    preferred_capability: str | None = None
    arguments: dict = Field(default_factory=dict)


class TaskPlan(BaseModel):
    goal: str
    reasoning_summary: str = Field(default="", max_length=500)
    steps: list[PlanStep] = Field(min_length=1)


class RetrievalGrade(BaseModel):
    relevant: bool
    sufficient: bool
    confidence: float = Field(ge=0, le=1)
    reason: str


class VerificationResult(BaseModel):
    unsupported_sentences: list[str] = Field(default_factory=list, description='Exact verbatim sentences from the candidate answer that are unsupported. Never paraphrase or invent spans.')
    grounded: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
