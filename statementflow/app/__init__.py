# SPDX-License-Identifier: MIT
"""StatementFlow: a monthly cash-flow Sankey for a household.

The package is deliberately split into small, single-responsibility modules so
the subtle parts (classification, transfer pairing, net-delta reconciliation)
stay pure and testable against synthetic fixtures. See PLAN.md for the build
order and the design brief for the authoritative shape.

Load-bearing rule for the whole package: this code runs against a PUBLIC repo,
so no real financial data may ever be committed. Real config, the DB, uploads
and raw statements are gitignored; only bank *profiles* and *.example.yaml
templates are public.
"""

__version__ = "0.0.0"
