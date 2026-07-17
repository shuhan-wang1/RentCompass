# rag_coordinator.py
from .property_embeddings import PropertyEmbeddingStore


class RAGCoordinator:
    def __init__(self):
        self.property_store = PropertyEmbeddingStore()

    def enhanced_search(self, user_query: str, criteria: dict):
        """
        Multi-source retrieval with reranking.

        Returns a 3-tuple ``(scored_results, past_context, area_info)`` for the
        callers that unpack it (core.tools.search_properties). ``past_context``
        and ``area_info`` are always empty lists:

          - past_context: cross-user conversation history was a privacy leak and
            is intentionally not queried here (this method has no user identity in
            scope). Per-user history lives in ``rag.agent_memory`` and is fetched
            explicitly with a user_id elsewhere.
          - area_info: the old curated AreaKnowledgeBase was a single hardcoded
            stub that ``_hybrid_rank`` never actually consumed; it was removed. The
            per-property crime signal (the ranking input that matters) still comes
            off each property below.
        """

        # When this is a clarification reply, don't use the clarification text as
        # the query; fall back to the target location for a meaningful search.
        if user_query and len(user_query) < 30 and ('do not' in user_query.lower() or 'nope' in user_query.lower() or 'nothing' in user_query.lower()):
            search_query = criteria.get('destination', 'London')
            print(f"    -> [RAG] Detected clarification reply, using location-based search: '{search_query}'")
        else:
            search_query = user_query

        # Semantic property search
        print(f"    -> [RAG] Starting semantic search for: {search_query[:50]}...")
        semantic_results = self.property_store.search(search_query, top_k=20)
        print(f"    -> [RAG] Got {len(semantic_results)} semantic results")

        # Hybrid scoring (semantic + rules)
        scored_results = self._hybrid_rank(semantic_results, criteria)

        print(f"    -> [RAG] Returning {len(scored_results)} ranked results")
        return scored_results, [], []

    def _hybrid_rank(self, properties, criteria):
        """
        Combine semantic similarity and hard constraints. Three buckets:
        1. within budget (perfect_match) - recommended first
        2. slightly over budget (soft_violation) - only shown with good reason
        3. far over budget (reject) - not recommended
        """
        max_budget = criteria.get('max_budget', 999999)
        print(f"    -> [DEBUG] _hybrid_rank: Input {len(properties)} properties, max_budget: {max_budget}")

        perfect_match = []
        soft_violation = []  # over budget but considerable (up to +15%)
        rejected = 0

        for i, prop in enumerate(properties):
            # Handle both parsed_price and Price fields
            prop_price = prop.get('parsed_price')
            if prop_price is None:
                price_str = prop.get('Price', '')
                try:
                    # e.g. "£1,600 pcm" -> 1600
                    prop_price = float(price_str.replace('£', '').replace(',', '').replace(' pcm', '').strip())
                    prop['parsed_price'] = prop_price
                except (ValueError, AttributeError):
                    print(f"    ⚠️ Could not parse price for {prop.get('Address', 'Unknown')}: {price_str}")
                    continue  # skip properties whose price can't be parsed

            # Hard filter: over budget by > 15% -> exclude
            if prop_price > max_budget * 1.15:
                rejected += 1
                print(f"    -> [DEBUG] Prop {i}: {prop.get('Address', 'Unknown')[:40]} REJECTED - Price £{prop_price} > budget limit £{max_budget * 1.15}")
                continue

            score = prop.get('similarity_score', 0) * 0.4  # Semantic weight

            # Rule-based boosting from criteria
            if 'max_travel_time' in criteria and prop.get('travel_time_minutes', 999) <= criteria['max_travel_time']:
                score += 0.3

            if 'max_budget' in criteria and prop_price <= max_budget:
                score += 0.2  # within budget
            else:
                score += 0.05  # slightly over budget: small boost only

            # Safety concerns boost from soft preferences (per-property crime signal)
            soft_prefs = criteria.get('soft_preferences', '')
            crime_trend = prop.get('crime_data_summary', {}).get('crime_trend')

            if soft_prefs:
                if isinstance(soft_prefs, list):
                    soft_prefs_str = ' '.join(str(p) for p in soft_prefs).lower()
                else:
                    soft_prefs_str = str(soft_prefs).lower()

                if 'safe' in soft_prefs_str:
                    if crime_trend == 'decreasing':
                        score += 0.1

            prop['final_score'] = score

            if prop_price <= max_budget:
                perfect_match.append(prop)
            else:
                soft_violation.append(prop)

        # Merge and sort: within-budget first, then slightly-over-budget
        sorted_perfect = sorted(perfect_match, key=lambda x: x['final_score'], reverse=True)
        sorted_soft = sorted(soft_violation, key=lambda x: x['final_score'], reverse=True)

        # Cap over-budget properties (at most 2, always after the in-budget ones)
        result = sorted_perfect + sorted_soft[:2]
        print(f"    -> [DEBUG] _hybrid_rank result: {len(sorted_perfect)} perfect + {min(2, len(sorted_soft))} soft_violation ({rejected} rejected) = {len(result)} total")
        return result
