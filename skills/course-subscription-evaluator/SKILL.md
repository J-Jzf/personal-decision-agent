---
name: course-subscription-evaluator
description: Evaluate whether a course or learning subscription fits a learner's goal and available time.
recommended_agents: [evidence_research, preference, risk_critic, judge]
recommended_tools: [course_catalog, syllabus_lookup, price_lookup, review_search]
analysis_dimensions: [cost, content, baseline, goal, alternatives, time, usage]
workflow: [clarify_goal_and_baseline, inspect_content, verify_price_and_terms, estimate_time_commitment, compare_alternatives, assess_likely_usage, decide_with_conditions]
risk_checks: [check_renewal_and_cancellation, distinguish_marketing_from_curriculum, avoid_sunk_cost_reasoning, flag_low_usage_risk]
completion_conditions: [cost_and_terms_checked, content_maps_to_goal, baseline_fit_assessed, time_and_usage_assessed, alternatives_compared]
output_schema: [recommendation, goal_content_fit, cost_terms, time_plan, alternatives, usage_risks, next_steps]
---
# Course subscription SOP

Treat completion and recurring use as uncertain until the learner's schedule supports them. Compare at least one viable alternative, including a lower-cost or free option when available.
