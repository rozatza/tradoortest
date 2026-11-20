import { ErrorNote } from "@/components/error-note";
import { ProposalCard } from "@/components/proposal-card";
import { StrategyCard } from "@/components/strategy-card";
import { ToolStep } from "@/components/tool-step";
import type { DecisionState, ThreadNotice, ThreadRow } from "@/lib/types";
import { applyDecisions, withNotices } from "@/lib/thread";

export function Thread({
  rows,
  notices,
  decisions,
  busy,
  onAccept,
  onReject,
}: {
  rows: ThreadRow[];
  notices: ThreadNotice[];
  decisions: Record<string, DecisionState>;
  busy: boolean;
  onAccept: (proposalId: string) => void;
  onReject: (proposalId: string) => void;
}) {
  const items = withNotices(applyDecisions(rows, decisions), notices);
  if (items.length === 0) {
    return (
      <div className="thread" role="log" aria-live="polite" aria-relevant="additions">
        <p className="empty">Send a message to start a session.</p>
      </div>
    );
  }
  return (
    <div className="thread" role="log" aria-live="polite" aria-relevant="additions">
      {items.map((item) => {
        if (item.kind === "notice") {
          return (
            <p className="alert" role="alert" key={item.notice.id}>
              {item.notice.code}: {item.notice.message}
              {item.notice.quote ? <span className="quote">{item.notice.quote}</span> : null}
            </p>
          );
        }
        return <ThreadRowView key={item.row.key} row={item.row} busy={busy} onAccept={onAccept} onReject={onReject} />;
      })}
    </div>
  );
}

function ThreadRowView({
  row,
  busy,
  onAccept,
  onReject,
}: {
  row: ThreadRow;
  busy: boolean;
  onAccept: (proposalId: string) => void;
  onReject: (proposalId: string) => void;
}) {
  if (row.type === "text") {
    const label = row.text.role === "user" ? "You" : "Assistant";
    return (
      <article className={`bubble ${row.text.role}`} aria-label={label}>
        <p className="who">{label}</p>
        <p>{row.text.content}</p>
      </article>
    );
  }
  if (row.type === "tool") {
    return <ToolStep step={row.tool} />;
  }
  if (row.type === "proposal") {
    return (
      <ProposalCard
        proposal={row.proposal}
        disabled={busy}
        onAccept={onAccept}
        onReject={onReject}
      />
    );
  }
  if (row.type === "strategy") {
    return <StrategyCard strategy={row.strategy} />;
  }
  return <ErrorNote error={row.error} />;
}
