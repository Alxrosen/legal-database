"""Entity resolution — blocking, scoring, decide.

Public API surfaces are documented in:
  * `blocking.py`: `make_blocking_keys`, `generate_candidate_pairs`
  * `scoring.py`: `score_pair`, `DEFAULT_WEIGHTS`
  * `run.py`:     `resolve_all` (CLI: python -m legal_sourcing.resolution.run)
"""
