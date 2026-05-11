"""V3 agentic demo pipeline.

Per-lead autonomous agent that:
  1. Researches the business (research sub-agent, Haiku)
  2. Composes a unique page from the section library (composition sub-agent)
  3. Self-critiques against neighbor demos (critic sub-agent, Haiku)
  4. Publishes only if guardrails pass (cost, similarity, image, content)

Entry point: `orchestrator.build_for_lead(lead_id, budget_cents, mode)`.

Sub-modules:
  cost      — token + dollar budget tracker with hard caps
  schemas   — validators for research_brief and composed_page
  banned    — banned-phrase enforcement
  tools     — tool implementations the sub-agents call
  catalog   — section + token library metadata loader
  assemble  — render composed_page.json → final HTML
  research  — research sub-agent (tool-use loop)
  compose   — composition sub-agent (tool-use loop)
  critic    — critic sub-agent (similarity + quality gate)
  orchestrator — end-to-end coordinator with hard budget enforcement
"""
