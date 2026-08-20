---
name: decision-retrospective
description: Learn from an outcome without rewriting the original evidence record.
recommended_agents: [preference, risk_critic, judge]
recommended_tools: [decision_archive, evidence_store, profile_memory]
analysis_dimensions: [outcome, evidence_quality, assumptions, preferences, process, lessons]
workflow: [load_original_record, compare_outcome_to_recommendation, identify_correct_and_incorrect_items, identify_missing_information, inspect_assumptions, update_preferences_carefully, record_lessons]
risk_checks: [preserve_original_record, avoid_hindsight_bias, separate_new_facts_from_old_inference, require_user_confirmation_for_preference_updates]
completion_conditions: [six_sections_present, original_evidence_preserved, assumptions_labeled, preference_updates_proposed_not_forced, future_lesson_actionable]
output_schema: [correct_items, incorrect_items, missing_information, wrong_assumptions, preference_updates, future_lessons]
---
# Decision retrospective SOP

Return exactly these six sections: correct items, incorrect items, missing information, wrong assumptions, preference updates, and future lessons. Preserve the original recommendation and evidence record; explain outcome changes without hindsight rewriting.
