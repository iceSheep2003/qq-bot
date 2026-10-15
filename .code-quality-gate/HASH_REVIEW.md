# Archive verification uses the quality engine hash convention

The engine hashes source text as SHA256(JSON.stringify(sourceText)), not raw
file bytes. Verified directly in the released src/ast.mjs. The initial integration
test mistakenly compared raw SHA256; correct that expression while retaining
the original assertion, file bytes, evidence and package thresholds. Original
inputs and accepted red are archived unchanged. There were no green attempts.
A new red freezes the corrected archive check before integration implementation.
