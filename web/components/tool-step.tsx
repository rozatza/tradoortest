import type { ToolStepView } from "@/lib/types";

export function ToolStep({ step }: { step: ToolStepView }) {
  const done = step.state === "done";
  const label = done ? `${step.tool} completed` : `${step.tool} working`;
  return (
    <div className="tool-step" role="status" aria-label={label} data-state={step.state}>
      {done ? (
        <span className="mark" aria-hidden="true">
          ✓
        </span>
      ) : (
        <span className="working">Working…</span>
      )}
      <span className="tool-name">{step.tool}</span>
      {step.errorMessage ? (
        <p className="tool-error" role="alert">
          {step.errorMessage}
        </p>
      ) : null}
    </div>
  );
}
