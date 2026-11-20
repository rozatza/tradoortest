import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ProposalCard } from "@/components/proposal-card";
import { ToolStep } from "@/components/tool-step";
import type { ProposalView, ToolStepView } from "@/lib/types";

const proposal: ProposalView = {
  seq: 1,
  proposalId: "b".repeat(32),
  strategyId: "c".repeat(32),
  fromVersion: 1,
  toVersion: 2,
  status: "pending",
  diff: "--- a\n+++ b\n",
  notice: null,
};

describe("proposal and tool steps", () => {
  it("does not accept or reject until the button is clicked", () => {
    const onAccept = vi.fn();
    const onReject = vi.fn();
    render(
      <ProposalCard proposal={proposal} disabled={false} onAccept={onAccept} onReject={onReject} />,
    );
    expect(onAccept).not.toHaveBeenCalled();
    expect(onReject).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onReject).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledWith(proposal.proposalId);
    expect(onAccept).not.toHaveBeenCalled();
  });

  it("shows a conflict notice and hides the buttons once the proposal is decided", () => {
    render(
      <ProposalCard
        proposal={{
          ...proposal,
          status: "conflict",
          notice: "version_conflict: strategy version conflict",
        }}
        disabled={false}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain("version_conflict: strategy version conflict");
  });

  it("renders a working step and then a checkmark", () => {
    const working: ToolStepView = {
      seq: 1,
      callId: "c1",
      tool: "validate",
      state: "working",
      ok: null,
      errorMessage: null,
    };
    const { rerender } = render(<ToolStep step={working} />);
    expect(screen.getByRole("status", { name: "validate working" }).textContent).toContain("Working…");
    expect(screen.queryByText("✓")).toBeNull();
    const done: ToolStepView = { ...working, state: "done", ok: true };
    rerender(<ToolStep step={done} />);
    const row = screen.getByRole("status", { name: "validate completed" });
    expect(row.textContent).toContain("✓");
    expect(row.textContent).not.toContain("Working");
  });
});
