---
name: job-offer-evaluator
description: Evaluate job offers with evidence, personal priorities, and explicit downside analysis.
recommended_agents: [evidence_research, preference, risk_critic, judge]
recommended_tools: [company_research, salary_research, location_cost, commute_lookup]
analysis_dimensions: [compensation, role_growth, company, stability, workload, location, risk]
workflow: [collect_offer_terms, identify_missing_facts, research_company_and_market, compare_against_preferences, score_tradeoffs, review_risks, present_recommendation]
risk_checks: [separate_guaranteed_from_variable_pay, verify_equity_terms, test_company_stability, flag_unverified_claims]
completion_conditions: [all_dimensions_covered, material_claims_cited_or_marked_uncertain, location_analysis_when_cities_differ, financial_analysis_when_financial_condition_supplied]
output_schema: [recommendation, compensation_comparison, growth_assessment, company_assessment, workload_location_tradeoffs, risks, next_steps]
---
# Job offer SOP

Start with written offer terms and the candidate's priorities. Activate Evidence Research, Preference, and Risk Critic for every evaluation. Activate Location & Lifestyle when locations or commutes differ; activate Financial Market when the user supplies debt, savings, family obligations, or other financial conditions. Never recommend accepting an offer or treat estimates as guaranteed compensation.
