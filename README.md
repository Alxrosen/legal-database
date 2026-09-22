# legal-deal-sourcing

Alexander M. Rosen — May 21st, 2026

**Purpose:** 
The aim of this project is to create a database of law firms for the purpose of
deal sourcing.

## Sourcing Architecture

Records will be constructed from a 6-step system:

1. **Law Firm Directories** — fetch raw HTML/JSON from pages and APIs.
2. **Parsing** — parse raw files from disk, produce structured
   database entries. Information pares includes websites, date, directory, etc.
   Create extra labels for firms specializing in Personal Injury. 
3. **Canonization** — Use fuzzy-matching to join identical law firms scraped from
   different directories.
4. **Firm Website Scraping** — iterate over the directory entries to find law firm
   websites and scrape those. 
5. **Enrich** — final canonization; fill gaps in database using firm websites:
   geocoding, practice-area, etc. 

See `docs/decisions.md` and `docs/schema.md` for additional details. 

## Agentic Workflow
Used 6 Agents in tandem with a coordination workflow. 
1. Mastermind: point-agent for work. Context-expensive, so kept high-level and
   /clear -ed often.
2. Websites: Web Scraper, responsible for saving the pages. Used a loop where I
   asked it to scrape and then verify a random selection manually. If any details
   were missed, asked it to correct the parser. Could have benefitted from further
   iteration, but time ran out before then. 
4. Canonizer: Researched and deployed fuzzy-matching technique to join identical
   firms. Identified method for prioritizing certain directories / data sources. 
5. Enricher: Researched and deployed law firm website use for the enrichment of
   the database. Same data prioritization task as Canonizer.
6. Auditor: Responsible for auditing the project and finding any useless,
   repetitive, incorrect, or poorly-documented parts of the project. Found very
   few. 

# Prompting
Discovered the usefulness of the following phrase:
"research the most *idiomatic, efficient, and effective* way ..."

This prioritized properly-documented paths that accomplished the goal and did so in
a simple way. 


## Quickstart

```bash
cp .env.example .env
make install
make test
```

