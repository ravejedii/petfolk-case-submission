import React from "react";
import diagramUrl from "../../../../docs/assets/workflow_diagram.svg?url";

// The diagram lives in docs/assets/ — imported here so Vite ships the same
// file in the built app. No copy, no JS redraw.

export default function WorkflowView() {
  return (
    <main className="page page-wide workflow-page">
      <div className="page-head">
        <div>
          <p className="eyebrow">Build</p>
          <h1>Workflow diagram</h1>
          <p className="page-sub">
            How this repo was built — each phase has a builder, an adversarial
            verifier, and a gate that must pass before the next one starts.
          </p>
        </div>
      </div>
      <figure className="workflow-frame">
        <img
          src={diagramUrl}
          alt="How this repo was built — AI agent swarm with adversarial gates"
        />
      </figure>
    </main>
  );
}
