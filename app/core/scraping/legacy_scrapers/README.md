# Legacy Zoopla scraper

Only `scrape_zoopla_listings.py` remains here. It is loaded on demand by
`app/core/scraping/zoopla.py` (via `config.load_legacy`) and drives Zoopla
through a local FlareSolverr container:

```
docker run -p 8191:8191 -e LOG_LEVEL=info --rm flaresolverr/flaresolverr
```

The former Rightmove/OpenRent standalone scrapers (`rightmove_scraper.py`,
`multi_search.py`, `filter_by_date.py`) were removed — those sources are dead
(WAF / decommissioned endpoint) and not revivable.
