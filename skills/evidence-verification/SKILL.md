---
name: evidence-verification
description: Verify important claims when sources are thin, conflicting, stale, or challenged by a critic.
recommended_agents: [evidence_research, risk_critic, debate_moderator]
recommended_tools: [source_lookup, official_source_lookup, timestamp_checker]
analysis_dimensions: [source_count, source_quality, independence, conflict, freshness, materiality]
workflow: [assign_evidence_ids, identify_single_source_or_conflict, request_independent_source, compare_claims, assess_freshness, set_status, record_open_questions]
risk_checks: [trigger_on_one_source, trigger_on_conflict, trigger_on_stale_evidence, trigger_on_critic_challenge, do_not_upgrade_without_support]
completion_conditions: [each_claim_has_status, material_single_source_claims_rechecked, conflicts_explained, freshness_recorded, unsupported_claims_marked]
output_schema: [claim, evidence_ids, verification_status, corroboration, conflict_explanation, freshness, next_check]
---
# Evidence verification SOP

Trigger verification for a material claim supported by only one source, conflicting sources, stale information, or a Critic challenge. Set each claim to Confirmed, Unverified, Conflicting, Rejected, or Unavailable; preserve source IDs and never erase dissent.
