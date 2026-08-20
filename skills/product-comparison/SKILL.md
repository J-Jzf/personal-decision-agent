---
name: product-comparison
description: Compare products against a real budget, requirements, usage pattern, and lifetime cost.
recommended_agents: [evidence_research, preference, risk_critic, judge]
recommended_tools: [product_specs, price_lookup, review_search, warranty_lookup]
analysis_dimensions: [budget, requirements, usage, drawbacks, total_cost, reviews, preferences]
workflow: [define_must_haves, collect_comparable_specs, verify_current_prices, map_usage_scenarios, identify_drawbacks, compare_total_cost, rank_options]
risk_checks: [separate_list_price_from_sale_price, flag_incompatible_requirements, distinguish_reviews_from_facts, include_maintenance_cost]
completion_conditions: [budget_checked, must_haves_checked, drawbacks_for_each_candidate, review_evidence_labeled, recommendation_has_tradeoffs]
output_schema: [recommendation, scorecard, price_and_cost, requirement_fit, drawbacks, review_summary, uncertainties]
---
# Product comparison SOP

Use the user's actual budget and tasks as hard constraints. Research specifications, prices, warranty terms, and reviews through approved read-only tools. Explain who should choose each option and state uncertainty where availability or pricing cannot be verified.
