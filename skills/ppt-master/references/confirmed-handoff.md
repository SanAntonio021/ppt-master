# Confirmed Task Handoff

Use this reference when a coordinating workflow has explicitly selected PPT
Master and supplies an existing brief, conversation, or project record.

## Retained decisions

Read the supplied record once. Retain the selected route and project, sources,
approved page content, design decisions, unresolved items, and the user's actual
confirmation or delegation scope. Reuse the existing record; no new receipt
format or separate handoff file is required. A coordinator's recommendation is
not user approval.

| Evidence in the handoff | Treatment |
|---|---|
| Explicitly confirmed content or design choice | Keep that choice; ask only about missing or changed decisions. |
| Explicit permission for the agent to decide remaining design choices | Use the existing delegated branch within that permission. |
| Explicit agreement to confirm in chat | Use the existing chat branch for unresolved choices. |
| No confirmation-surface instruction | Use the existing default surface for unresolved choices. |

Apply [`confirm-surface.md`](confirm-surface.md) to resolve the branch. Entering
through a coordinator alone grants neither delegation nor a surface choice.

## Stage boundaries

For Default Generate, compare retained decisions with each stage's requirements
in [`generate-pptx.md`](../workflows/generate-pptx.md) Step 4 when that stage is
reached. An already confirmed decision satisfies only its corresponding item.
Approval of page wording or an outline does not approve a complete design,
template selection, media acquisition, or production mechanics.

When all decisions of a stage are evidenced, retain a concise summary in the
existing record and proceed without requesting the same approval again. When
items remain open, present only those items with enough retained context to
decide them. Under delegation, still perform the selected runtime's planning
craft and record the chosen solution and reason. New decisions outside the
delegation require user confirmation.

Existing chat confirmation stays chat. Do not launch the confirmation UI for
a stage already closed by genuine prior confirmation, and never synthesize
`result.json`, `template_selection.json`, or a browser submission. A template
choice still requires installation before Stage 2, and all source, integrity,
design-spec, resource, quality, and export prerequisites remain in force.

## Completion and revisions

Return the actual export and editable project paths, performed checks and their
results, and remaining limitations to the coordinator. Continue revisions in
the selected project at the fault's owning layer; do not route back through a
generic skill selector. Direct use without a handoff keeps the normal complete
workflow. Other selected profiles retain their own confirmation requirements.
