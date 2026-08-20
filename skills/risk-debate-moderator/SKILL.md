---
name: risk-debate-moderator
description: Provide a generic decision fallback and moderate evidence-grounded risk debate.
recommended_agents: [debate_moderator, risk_critic, preference, judge]
recommended_tools: [evidence_store, source_lookup, constraint_checker]
analysis_dimensions: [options, constraints, evidence_quality, downside, reversibility, preferences]
workflow: [state_question, collect_evidence_ids, identify_disagreement, test_constraints, present_pro_and_con, resolve_or_escalate_uncertainty]
risk_checks: [require_evidence_ids_for_claims, reject_uncited_arguments, surface_irreversible_downside, separate_fact_from_inference]
completion_conditions: [every_material_argument_has_evidence_ids, agreements_and_disagreements_listed, strongest_pro_and_con_stated, unresolved_risks_stated]
output_schema: [decision_frame, cited_arguments, agreements, disagreements, strongest_pro, strongest_con, unresolved_risks]
---
# Risk debate SOP

Every material claim in the debate must cite one or more Evidence IDs. Reject uncited arguments rather than silently converting them into facts. Use this Skill as the generic fallback when no specialist domain matches.
