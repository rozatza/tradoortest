import type { ProposalView } from "@/lib/types";

export function ProposalCard({
  proposal,
  disabled,
  onAccept,
  onReject,
}: {
  proposal: ProposalView;
  disabled: boolean;
  onAccept: (proposalId: string) => void;
  onReject: (proposalId: string) => void;
}) {
  const pending = proposal.status === "pending";
  const versions =
    proposal.fromVersion !== null && proposal.toVersion !== null
      ? `v${proposal.fromVersion} → v${proposal.toVersion}`
      : null;
  return (
    <section className="card proposal" aria-label={`Proposal ${proposal.proposalId}`}>
      <h2>Proposal</h2>
      <p className="meta">
        <span>{proposal.strategyId}</span>
        {versions ? <span>{versions}</span> : null}
        <span className="status">{proposal.status}</span>
      </p>
      <pre className="diff">{proposal.diff}</pre>
      {pending ? (
        <div className="actions">
          <button
            type="button"
            disabled={disabled}
            onClick={() => onAccept(proposal.proposalId)}
          >
            Accept
          </button>
          <button
            type="button"
            className="secondary"
            disabled={disabled}
            onClick={() => onReject(proposal.proposalId)}
          >
            Reject
          </button>
        </div>
      ) : null}
      {proposal.notice ? (
        <p className="alert" role="alert">
          {proposal.notice}
        </p>
      ) : null}
    </section>
  );
}
