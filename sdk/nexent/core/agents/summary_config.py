"""Configuration for agent context compression."""

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class ContextManagerConfig:
    """Configuration for ContextManager compression behavior."""
    enabled: bool = False
    token_threshold: int = 10000
    keep_recent_steps: int = 4
    keep_recent_pairs: int = 2
    max_chunk_count: int = 0
    max_memory_step_length: int = 2000

    # --- Shared preamble embedded in both prompts below (Hermes-inspired) ---
    # Key principles: language preservation, credential redaction, information priority, concreteness.
    # If you need to change the shared portion, update BOTH summary_system_prompt and
    # incremental_summary_system_prompt — they each contain the full preamble inline.

    # --- Initial compression prompt (first compaction, no previous summary) ---
    summary_system_prompt: str = (
        "You are a summarization agent creating a context checkpoint. "
        "Treat the conversation turns below as source material for a compact record of prior work. "
        "Produce only the structured JSON summary; do not add a greeting, preamble, or prefix. "
        "Write the summary in the same language the user was using in the conversation — "
        "do not translate or switch to English. "
        "NEVER include API keys, tokens, passwords, secrets, credentials, or connection strings "
        "in the summary — replace any that appear with [REDACTED]. Note that the user had "
        "credentials present, but do not preserve their values. "
        "Information priority: Critical (task, goal, constraints) > "
        "Operational (completed actions, active state) > Reference (files, decisions). "
        "Be CONCRETE — include file paths, command outputs, error messages, line numbers, "
        "and specific values. Avoid vague descriptions like 'made some changes' — say exactly what changed.\n\n"
        "Create a structured checkpoint summary for the conversation. The summary should preserve "
        "enough detail for continuity without re-reading the original turns. "
        "Output strict JSON format without markdown blocks."
    )

    # --- Incremental compression prompt (updating an existing summary) ---
    incremental_summary_system_prompt: str = (
        "You are a summarization agent creating a context checkpoint. "
        "Treat the conversation turns below as source material for a compact record of prior work. "
        "Produce only the structured JSON summary; do not add a greeting, preamble, or prefix. "
        "Write the summary in the same language the user was using in the conversation — "
        "do not translate or switch to English. "
        "NEVER include API keys, tokens, passwords, secrets, credentials, or connection strings "
        "in the summary — replace any that appear with [REDACTED]. Note that the user had "
        "credentials present, but do not preserve their values. "
        "Information priority: Critical (task, goal, constraints) > "
        "Operational (completed actions, active state) > Reference (files, decisions). "
        "Be CONCRETE — include file paths, command outputs, error messages, line numbers, "
        "and specific values. Avoid vague descriptions like 'made some changes' — say exactly what changed.\n\n"
        "You are updating an existing context compaction summary. A previous compaction produced "
        "the summary shown as 'Previous Summary'. New conversation turns have occurred since then "
        "and are shown as 'New Content'. Update the summary by following these rules:\n"
        "1. PRESERVE all existing information that is still relevant — do not remove unless clearly obsolete. "
        "Do not replace specific values with vague summaries — preserve exact numbers, names, and status strings.\n"
        "2. ADD new completed actions to the 'completed_work' list (continue numbering).\n"
        "3. Move items from 'in_progress' to 'completed_work' when done.\n"
        "4. Move answered questions to 'resolved_questions' with their answers.\n"
        "5. UPDATE 'active_state' to reflect the current working state.\n"
        "6. UPDATE 'active_task' to reflect the user's most recent unfulfilled request — "
        "this is the most important field for task continuity.\n"
        "7. UPDATE 'pending_items': move resolved items out, add new pending items.\n"
        "8. UPDATE 'critical_context' with any new user preferences, domain details, or data that must be preserved.\n"
        "9. Output the complete updated summary as strict JSON without markdown blocks."
    )

    # --- JSON schema: 10 fields adapted from Hermes 13-section template ---
    # Mapping from Hermes 13 sections to JSON fields:
    #   Active Task + Goal          → active_task, goal
    #   Constraints & Preferences   → critical_context (merged)
    #   Completed Actions           → completed_work (with numbered-list format hint)
    #   Active State                → active_state
    #   In Progress                 → in_progress
    #   Blocked                     → pending_items (merged blockers + pending)
    #   Key Decisions               → key_decisions
    #   Resolved Questions          → resolved_questions
    #   Pending User Asks           → pending_items (merged)
    #   Relevant Files              → relevant_files
    #   Remaining Work              → (folded into pending_items)
    #   Critical Context            → critical_context
    summary_json_schema: Dict[str, Any] = field(default_factory=lambda: {
        "active_task": (
            "THE MOST IMPORTANT FIELD. The user's most recent unfulfilled request or task assignment — "
            "copy the exact words if possible. If no outstanding task, write 'None'. (<=150 words)"
        ),
        "goal": "What the user is trying to accomplish overall (<=100 words)",
        "completed_work": (
            "Numbered list of concrete actions taken. Format: N. ACTION target — outcome [tool: name]. "
            "Be specific with file paths, commands, line numbers. (<=300 words)"
        ),
        "active_state": (
            "Current working state: modified/created files, test status (X/Y passing), "
            "running processes, environment details that matter. (<=150 words)"
        ),
        "in_progress": "Work currently underway when compaction fired (<=100 words)",
        "key_decisions": "Important technical decisions and WHY they were made (<=150 words)",
        "resolved_questions": (
            "Questions the user asked that were ALREADY answered — include the answer "
            "so it is not repeated. If none, write 'None'. (<=150 words)"
        ),
        "pending_items": (
            "Blockers, errors (include exact messages), and specific steps still pending. "
            "Questions or requests NOT yet answered. If none, write 'None'. (<=150 words)"
        ),
        "relevant_files": "Files read, modified, or created — with brief note on each (<=100 words)",
        "critical_context": (
            "Specific values, error messages, configuration details, or data that would be lost "
            "without explicit preservation. Do not replace specific values with vague summaries — "
            "preserve exact numbers, names, and status strings. "
            "NEVER include credentials — write [REDACTED]. (<=300 words)"
        ),
    })

    max_summary_input_tokens: int = 0
    max_summary_reduce_tokens: int = 0
    estimated_chunk_summary_tokens: int = 400
    chars_per_token: float = 1.5
    per_step_render_limit: int = 3000
    enable_reload: bool = True
    max_offload_entries: int = 200
    max_observation_length: int = 20000