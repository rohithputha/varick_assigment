"""
Rules Engine — central metadata store for the Varick AP pipeline.

Owns:
  rules.json   — GL classification rules + normalisation tables
  prompts.json — LLM system prompts, valid enum values, few-shot examples

Consumers:
  gl_classification  → rules_engine.rules_tools / rules_engine.classifier.sop
  invoice_extraction → rules_engine.prompts_tools
"""
