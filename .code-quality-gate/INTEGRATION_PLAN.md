# Current quality engine: Code Quality Gate 0.6.0

The user requested the package from the local tdd代码包 conversation. The existing
project had pinned its earlier VerityGate 0.4.0 release. Upgrade to the actual
released code-quality-gate 0.6.0 tarball, verified against that release's SHA256
list. Runtime source files must match the vendor tarball byte-for-byte.

One active integration lives under .code-quality-gate: pinned npm toolchain,
Python lock, CI setup, tasks, evidence and active pointer. Official setup installs
the unmodified bundled skill, managed instructions, MCP configuration and CI.
The final CI calls the pinned local engine. No wrapper computes or edits results.
All current checks and thresholds are retained; add executable package-provenance,
skill, CI/MCP and old-engine archive tests. Full pytest remains mandatory.

Archive the exact former .veritygate inputs/evidence, old instructions, old CI,
old failed reports and committed pre-namespace schema fingerprint fixture. Remove
the old active engine, skill and CI after archiving; do not rewrite its records.
The archived R3 naming task consumed one green attempt and remains a failure.
This task establishes test-first integration of the newer engine, not a claim
that earlier Bot code was originally authored under version 0.6.0.

The failure caught by the old gate is reviewed in PROTOCOL_REVIEW.md. The new
fingerprint fixture is frozen before this task's red run, along with a complete
baseline comparison allowing only schema $id namespace changes. Data constraints
and existing behavior assertions remain unchanged. This is an explicit namespace
review, not silently accepting an arbitrary changed schema.

Code ownership stays at src/qqbot and the seven existing responsibilities. Local
teaching material stays outside Git. No push, deployment or real QQ call occurs.
