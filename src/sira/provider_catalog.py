from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Iterable, Mapping


_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

ACCESS_PUBLIC = "public"
ACCESS_OPTIONAL_KEY = "optional_key"
ACCESS_KEY_REQUIRED = "key_required"

COST_FREE = "free"
COST_METERED = "metered"

_ALLOWED_ACCESS = {
    ACCESS_PUBLIC,
    ACCESS_OPTIONAL_KEY,
    ACCESS_KEY_REQUIRED,
}
_ALLOWED_COST = {
    COST_FREE,
    COST_METERED,
}


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider_id: str
    display_name: str
    domains: tuple[str, ...]
    capabilities: tuple[str, ...]
    access: str
    cost_class: str
    credential_env: tuple[str, ...] = ()
    network: bool = True
    read_only: bool = True
    executes_remote_code: bool = False
    autonomous_research_allowed: bool = True
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["domains"] = list(self.domains)
        value["capabilities"] = list(self.capabilities)
        value["credential_env"] = list(self.credential_env)
        return value


class ProviderCatalogError(ValueError):
    """Raised when provider capability metadata violates the catalog contract."""


def validate_descriptor(descriptor: ProviderDescriptor) -> None:
    if not _PROVIDER_ID_RE.fullmatch(descriptor.provider_id):
        raise ProviderCatalogError(
            f"invalid provider_id: {descriptor.provider_id!r}"
        )

    if not descriptor.display_name.strip():
        raise ProviderCatalogError("display_name must not be empty")

    if not descriptor.domains:
        raise ProviderCatalogError(
            f"{descriptor.provider_id}: at least one domain is required"
        )
    if not descriptor.capabilities:
        raise ProviderCatalogError(
            f"{descriptor.provider_id}: at least one capability is required"
        )

    if descriptor.access not in _ALLOWED_ACCESS:
        raise ProviderCatalogError(
            f"{descriptor.provider_id}: invalid access class {descriptor.access!r}"
        )

    if descriptor.cost_class not in _ALLOWED_COST:
        raise ProviderCatalogError(
            f"{descriptor.provider_id}: invalid cost class {descriptor.cost_class!r}"
        )

    for env_name in descriptor.credential_env:
        if not _ENV_NAME_RE.fullmatch(env_name):
            raise ProviderCatalogError(
                f"{descriptor.provider_id}: invalid credential env name"
            )

    if descriptor.access == ACCESS_KEY_REQUIRED and not descriptor.credential_env:
        raise ProviderCatalogError(
            f"{descriptor.provider_id}: key_required provider must declare credential_env"
        )

    if descriptor.read_only is False:
        raise ProviderCatalogError(
            f"{descriptor.provider_id}: research-provider catalog must remain read-only"
        )

    if descriptor.executes_remote_code:
        raise ProviderCatalogError(
            f"{descriptor.provider_id}: remote code execution is forbidden in this catalog"
        )


def validate_catalog(
    catalog: Mapping[str, ProviderDescriptor],
) -> None:
    if not catalog:
        raise ProviderCatalogError("provider catalog must not be empty")

    for key, descriptor in catalog.items():
        if key != descriptor.provider_id:
            raise ProviderCatalogError(
                f"catalog key {key!r} does not match provider_id {descriptor.provider_id!r}"
            )
        validate_descriptor(descriptor)


_PROVIDER_CATALOG: dict[str, ProviderDescriptor] = {
    "wikimedia": ProviderDescriptor(
        provider_id="wikimedia",
        display_name="Wikimedia",
        domains=("knowledge", "reference", "general_web"),
        capabilities=("knowledge_search", "encyclopedia_metadata", "public_page_reference"),
        access=ACCESS_PUBLIC,
        cost_class=COST_FREE,
        notes="Public Wikimedia page-search metadata; read-only and non-authoritative.",
    ),
    "github_public": ProviderDescriptor(
        provider_id="github_public",
        display_name="GitHub Public",
        domains=("software", "code", "documentation"),
        capabilities=(
            "repository_search",
            "repository_metadata",
            "public_file_read",
            "rate_limit_observation",
        ),
        access=ACCESS_OPTIONAL_KEY,
        cost_class=COST_FREE,
        credential_env=("SIRA_GITHUB_TOKEN",),
        notes=(
            "Public repository research only. Repository content is untrusted input; "
            "no clone execution, dependency installation, push, issue, or PR mutation."
        ),
    ),
    "doaj": ProviderDescriptor(
        provider_id="doaj",
        display_name="DOAJ",
        domains=("scholarly", "open_access", "papers"),
        capabilities=("open_access_search", "paper_metadata", "open_access_metadata"),
        access=ACCESS_PUBLIC,
        cost_class=COST_FREE,
        notes="Public DOAJ open-access article metadata; separate from generic paper_search.",
    ),
    "europe_pmc": ProviderDescriptor(
        provider_id="europe_pmc",
        display_name="Europe PMC",
        domains=("scholarly", "biomedical", "medicine", "open_access"),
        capabilities=(
            "biomedical_search",
            "paper_metadata",
            "open_access_metadata",
        ),
        access=ACCESS_PUBLIC,
        cost_class=COST_FREE,
        notes=(
            "Public Europe PMC literature search and metadata. "
            "Used as a free biomedical research source."
        ),
    ),
    "pubmed": ProviderDescriptor(
        provider_id="pubmed",
        display_name="PubMed / NCBI E-utilities",
        domains=("scholarly", "biomedical", "medicine"),
        capabilities=(
            "biomedical_search",
            "paper_metadata",
            "pubmed_metadata",
        ),
        access=ACCESS_OPTIONAL_KEY,
        cost_class=COST_FREE,
        credential_env=("SIRA_NCBI_API_KEY",),
        notes=(
            "Public NCBI E-utilities access. Optional free API key raises the "
            "supported request rate; SIRA locally throttles requests."
        ),
    ),
    "openalex": ProviderDescriptor(
        provider_id="openalex",
        display_name="OpenAlex",
        domains=("scholarly", "papers", "citations"),
        capabilities=(
            "paper_search",
            "paper_metadata",
            "citation_metadata",
        ),
        access=ACCESS_PUBLIC,
        cost_class=COST_FREE,
        notes="Public scholarly metadata/search provider.",
    ),
    "crossref": ProviderDescriptor(
        provider_id="crossref",
        display_name="Crossref",
        domains=("scholarly", "papers", "doi"),
        capabilities=(
            "paper_search",
            "doi_metadata",
            "bibliographic_metadata",
        ),
        access=ACCESS_PUBLIC,
        cost_class=COST_FREE,
        notes="Public DOI and scholarly metadata provider.",
    ),
    "arxiv": ProviderDescriptor(
        provider_id="arxiv",
        display_name="arXiv",
        domains=("scholarly", "papers", "preprints"),
        capabilities=(
            "paper_search",
            "preprint_metadata",
        ),
        access=ACCESS_PUBLIC,
        cost_class=COST_FREE,
        notes="Public preprint search/metadata provider.",
    ),
    "semantic_scholar": ProviderDescriptor(
        provider_id="semantic_scholar",
        display_name="Semantic Scholar",
        domains=("scholarly", "papers", "citations"),
        capabilities=(
            "paper_search",
            "paper_metadata",
            "citation_metadata",
        ),
        access=ACCESS_OPTIONAL_KEY,
        cost_class=COST_FREE,
        credential_env=("SIRA_SEMANTIC_SCHOLAR_API_KEY",),
        notes="Works anonymously in SIRA; optional local key can improve provider limits.",
    ),
    "gemini": ProviderDescriptor(
        provider_id="gemini",
        display_name="Gemini",
        domains=("models", "code", "synthesis"),
        capabilities=(
            "code_generation",
            "evidence_synthesis",
            "language_semantic_bridge",
            "desktop_chat",
            "google_search_grounding",
            "url_context",
        ),
        access=ACCESS_KEY_REQUIRED,
        credential_env=("GEMINI_API_KEY",),
        cost_class=COST_METERED,
        notes=(
            "Metered structured model capability. Autonomous use remains subject "
            "to runtime budget, credential verification, and protected promotion gates."
        ),
    ),
    "tavily": ProviderDescriptor(
        provider_id="tavily",
        display_name="Tavily",
        domains=("web", "documents"),
        capabilities=(
            "web_search",
            "web_extraction",
        ),
        access=ACCESS_KEY_REQUIRED,
        cost_class=COST_METERED,
        credential_env=("TAVILY_API_KEY",),
        notes=(
            "Metered external provider. SIRA policy should prefer free/public providers "
            "when they can satisfy the task."
        ),
    ),
}

validate_catalog(_PROVIDER_CATALOG)

PROVIDER_CATALOG: Mapping[str, ProviderDescriptor] = _PROVIDER_CATALOG


def get_provider(provider_id: str) -> ProviderDescriptor:
    try:
        return PROVIDER_CATALOG[provider_id]
    except KeyError:
        raise KeyError(f"unknown provider: {provider_id}") from None


def list_providers(
    *,
    domain: str | None = None,
    capability: str | None = None,
    cost_class: str | None = None,
    autonomous_only: bool = False,
) -> tuple[ProviderDescriptor, ...]:
    providers: Iterable[ProviderDescriptor] = PROVIDER_CATALOG.values()

    if domain is not None:
        providers = (item for item in providers if domain in item.domains)
    if capability is not None:
        providers = (
            item for item in providers if capability in item.capabilities
        )
    if cost_class is not None:
        if cost_class not in _ALLOWED_COST:
            raise ValueError(f"invalid cost_class: {cost_class}")
        providers = (
            item for item in providers if item.cost_class == cost_class
        )
    if autonomous_only:
        providers = (
            item for item in providers if item.autonomous_research_allowed
        )

    return tuple(sorted(providers, key=lambda item: item.provider_id))


def free_first(
    providers: Iterable[ProviderDescriptor],
) -> tuple[ProviderDescriptor, ...]:
    return tuple(
        sorted(
            providers,
            key=lambda item: (
                item.cost_class != COST_FREE,
                item.provider_id,
            ),
        )
    )


def catalog_snapshot() -> dict[str, object]:
    providers = list_providers()
    return {
        "schema": "sira.provider_catalog.v1",
        "provider_count": len(providers),
        "providers": [item.to_dict() for item in providers],
    }


def main() -> int:
    print(json.dumps(catalog_snapshot(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
