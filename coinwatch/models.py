from dataclasses import dataclass, field


@dataclass
class Listing:
    external_id: str
    url: str
    title: str
    price: str
    currency: str
    image_url: str = ''
    category: str = 'Ancient'
    availability: str = 'available'
    listed_at: str | None = None


@dataclass
class ScrapeResult:
    listings: list[Listing] = field(default_factory=list)
    pages: int = 0
    complete: bool = True
    error: str | None = None
