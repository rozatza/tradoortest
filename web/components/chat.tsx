"use client";

import { useMemo, useRef, useState, type FormEvent } from "react";

import { StrategyEditor } from "@/components/strategy-editor";
import { Thread } from "@/components/thread";
import { ApiError, createSession, decideProposal, streamMessage } from "@/lib/client";
import { applyDecisions, buildThread, insertEvent } from "@/lib/thread";
import type { AgentEvent, DecisionState, ProposalView, StrategyFields, ThreadNotice } from "@/lib/types";

export function Chat() {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [notices, setNotices] = useState<ThreadNotice[]>([]);
  const [decisions, setDecisions] = useState<Record<string, DecisionState>>({});
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [strategyId, setStrategyId] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const sessionId = useRef<string | null>(null);
  const seqRef = useRef(0);
  const noticeCount = useRef(0);
  const rows = useMemo(() => buildThread(events), [events]);
  const openProposals = useMemo(() => {
    if (strategyId === null) {
      return [];
    }
    const decided = applyDecisions(rows, decisions);
    const found: ProposalView[] = [];
    for (const row of decided) {
      if (row.type === "proposal" && row.proposal.strategyId === strategyId) {
        found.push(row.proposal);
      }
    }
    return found;
  }, [rows, decisions, strategyId]);

  function remember(agentEvent: AgentEvent) {
    seqRef.current = Math.max(seqRef.current, agentEvent.seq);
    setEvents((current) => insertEvent(current, agentEvent));
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft;
    if (content.trim() === "" || busy) {
      return;
    }
    setBusy(true);
    let completed = false;
    try {
      const id = await ensureSession();
      await streamMessage(id, content, (agentEvent) => {
        if (agentEvent.type === "turn_complete") {
          completed = true;
        }
        remember(agentEvent);
      });
      if (!completed) {
        pushIncomplete();
      }
      setDraft((current) => (current === content ? "" : current));
    } catch (error) {
      pushNotice(error, content);
    } finally {
      setBusy(false);
    }
  }

  async function onAccept(proposalId: string) {
    await onDecide(proposalId, "accept");
  }

  async function onReject(proposalId: string) {
    await onDecide(proposalId, "reject");
  }

  async function onDecide(proposalId: string, decision: "accept" | "reject") {
    if (busy) {
      return;
    }
    setBusy(true);
    try {
      const result = await decideProposal(proposalId, decision);
      if (!result.ok) {
        setDecisions((current) => ({
          ...current,
          [proposalId]: {
            code: result.code,
            message: result.message,
            status: terminalStatus(result.code),
          },
        }));
        return;
      }
      if (result.event) {
        remember(result.event);
      } else if (decision === "accept" && result.strategy) {
        pushStrategy(result.strategy);
      }
      const status =
        result.proposalStatus ?? (decision === "accept" ? "accepted" : "rejected");
      setDecisions((current) => ({
        ...current,
        [proposalId]: { status },
      }));
      if (decision === "accept") {
        setRefreshToken((current) => current + 1);
      }
    } catch (error) {
      setDecisions((current) => ({
        ...current,
        [proposalId]: {
          code: error instanceof ApiError ? error.code : "http_error",
          message: error instanceof Error ? error.message : "the request could not be completed",
        },
      }));
    } finally {
      setBusy(false);
    }
  }

  function pushNotice(error: unknown, quote: string) {
    const api = error instanceof ApiError ? error : null;
    const code = api?.code ?? "http_error";
    const message =
      api?.message ??
      (error instanceof Error ? error.message : "the request could not be completed");
    const afterSeq = seqRef.current;
    noticeCount.current += 1;
    setNotices((existing) => [
      ...existing,
      { id: `notice-${noticeCount.current}`, afterSeq, code, message, quote },
    ]);
  }

  function pushIncomplete() {
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    const agentEvent: AgentEvent = {
      seq,
      event_id: `local-incomplete-${seq}`,
      session_id: sessionId.current ?? "",
      turn_id: null,
      type: "stream_incomplete",
      created_at: "",
      payload: {
        code: "stream_incomplete",
        message: "the response ended before the turn finished",
      },
    };
    setEvents((current) => insertEvent(current, agentEvent));
  }

  function pushStrategy(strategy: StrategyFields) {
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    const event: AgentEvent = {
      seq,
      event_id: `local-strategy-${seq}`,
      session_id: sessionId.current ?? "",
      turn_id: null,
      type: "proposal_decided",
      created_at: "",
      payload: {
        proposal_id: `local-${seq}`,
        decision: "accepted",
        status: "accepted",
        strategy_id: strategy.strategyId,
        applied_version: strategy.version,
        applied_ir_hash: strategy.irHash,
      },
    };
    setEvents((current) => insertEvent(current, event));
  }

  async function ensureSession(): Promise<string> {
    if (sessionId.current) {
      return sessionId.current;
    }
    const created = await createSession();
    sessionId.current = created.session_id;
    return created.session_id;
  }

  return (
    <div className="workspace">
      <section className="chat" aria-label="Author chat">
        <Thread
          rows={rows}
          notices={notices}
          decisions={decisions}
          busy={busy}
          onAccept={onAccept}
          onReject={onReject}
          onOpenStrategy={setStrategyId}
        />
        <form className="composer" onSubmit={onSubmit}>
          <label htmlFor="message">Message</label>
          <textarea
            id="message"
            name="message"
            rows={3}
            value={draft}
            disabled={busy}
            onChange={(event) => setDraft(event.target.value)}
          />
          <button type="submit" disabled={busy || draft.trim() === ""}>
            {busy ? "Sending…" : "Send"}
          </button>
        </form>
      </section>
      <StrategyEditor
        strategyId={strategyId}
        refreshToken={refreshToken}
        proposals={openProposals}
        busy={busy}
        onAccept={onAccept}
        onReject={onReject}
        onSelectStrategy={setStrategyId}
      />
    </div>
  );
}

function terminalStatus(code: string): string | undefined {
  if (code === "version_conflict") {
    return "conflict";
  }
  if (code === "proposal_already_decided") {
    return "already decided";
  }
  if (code === "no_changes") {
    return "unchanged";
  }
  return undefined;
}
