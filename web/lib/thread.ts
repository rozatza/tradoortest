import type {
  AgentEvent,
  DecisionState,
  ErrorView,
  ProposalView,
  StrategyView,
  ThreadNotice,
  ThreadRow,
  ToolStepView,
} from "@/lib/types";

const MARKET_KEYS = new Set([
  "bars",
  "fills",
  "trades",
  "equity_curve",
  "equity_after",
  "final_equity",
  "final_cash",
  "pool",
  "liquidity_usd",
  "reserve_in_usd",
  "quote_token",
]);

const DIFF_LIMIT = 1200;

export function stripMarketData<T>(value: T): T {
  return stripValue(value) as T;
}

export function insertEvent(events: AgentEvent[], event: AgentEvent): AgentEvent[] {
  const cleaned = stripMarketData(event);
  const key = eventKey(cleaned);
  if (events.some((item) => eventKey(item) === key)) {
    return events;
  }
  return [...events, cleaned].sort((left, right) => left.seq - right.seq);
}

export function buildThread(events: AgentEvent[]): ThreadRow[] {
  const steps = new Map<string, ToolStepView>();
  const proposals = new Map<string, ProposalView>();
  const rows: ThreadRow[] = [];
  const errors: ErrorView[] = [];
  const incompleteAfter: number[] = [];

  for (const event of events) {
    const payload = event.payload ?? {};
    if (event.type === "user_message" || event.type === "assistant_message") {
      const content = typeof payload.content === "string" ? payload.content : "";
      const role = event.type === "user_message" ? "user" : "assistant";
      rows.push({
        key: `text:${event.seq}:${event.event_id}`,
        seq: event.seq,
        type: "text",
        text: { seq: event.seq, role, content },
      });
      continue;
    }
    if (event.type === "tool_start") {
      const callId = stringField(payload, "call_id") ?? `start-${event.seq}`;
      if (!steps.has(callId)) {
        const tool = stringField(payload, "tool") ?? "tool";
        const step: ToolStepView = {
          seq: event.seq,
          callId,
          tool,
          state: "working",
          ok: null,
          errorMessage: null,
        };
        steps.set(callId, step);
        rows.push({ key: `tool:${callId}`, seq: event.seq, type: "tool", tool: step });
      }
      continue;
    }
    if (event.type === "tool_complete") {
      const callId = stringField(payload, "call_id") ?? `complete-${event.seq}`;
      const tool = stringField(payload, "tool") ?? steps.get(callId)?.tool ?? "tool";
      const ok = payload.ok === true;
      const errorMessage = ok ? null : toolError(payload);
      const existing = steps.get(callId);
      if (existing) {
        existing.state = "done";
        existing.ok = ok;
        existing.errorMessage = errorMessage;
        if (existing.tool === "tool") {
          existing.tool = tool;
        }
      } else {
        const step: ToolStepView = {
          seq: event.seq,
          callId,
          tool,
          state: "done",
          ok,
          errorMessage,
        };
        steps.set(callId, step);
        rows.push({ key: `tool:${callId}`, seq: event.seq, type: "tool", tool: step });
      }
      const written = strategyFromTool(payload, event.seq);
      if (written) {
        rows.push({
          key: `strategy:${event.seq}:${written.strategyId}:${written.action}`,
          seq: event.seq,
          type: "strategy",
          strategy: written,
        });
      }
      continue;
    }
    if (event.type === "proposal_created") {
      const proposal = proposalFromPayload(payload, event.seq);
      if (proposal) {
        proposals.set(proposal.proposalId, proposal);
        rows.push({
          key: `proposal:${proposal.proposalId}`,
          seq: event.seq,
          type: "proposal",
          proposal,
        });
      }
      continue;
    }
    if (event.type === "proposal_decided") {
      const proposalId = stringField(payload, "proposal_id");
      const status = stringField(payload, "status") ?? stringField(payload, "decision");
      if (proposalId && status) {
        const card = proposals.get(proposalId);
        if (card) {
          card.status = status;
        }
      }
      const updated = strategyFromDecision(payload, event.seq);
      if (updated) {
        rows.push({
          key: `strategy:${event.seq}:${updated.strategyId}:updated`,
          seq: event.seq,
          type: "strategy",
          strategy: updated,
        });
      }
      continue;
    }
    if (event.type === "stream_aborted") {
      incompleteAfter.push(event.seq);
      continue;
    }
    if (event.type === "stream_incomplete") {
      incompleteAfter.push(event.seq);
      const error = errorFromPayload(
        {
          code: stringField(payload, "code") ?? "stream_incomplete",
          message:
            stringField(payload, "message") ?? "the response ended before the turn finished",
        },
        event.seq,
      );
      rows.push({
        key: `error:${event.seq}:${event.event_id}`,
        seq: event.seq,
        type: "error",
        error,
      });
      continue;
    }
    if (event.type === "error") {
      const error = errorFromPayload(payload, event.seq);
      errors.push(error);
      rows.push({
        key: `error:${event.seq}:${event.event_id}`,
        seq: event.seq,
        type: "error",
        error,
      });
      continue;
    }
    if (event.type === "turn_complete") {
      const nested = payload.error;
      if (nested && typeof nested === "object" && !Array.isArray(nested)) {
        const error = errorFromPayload(nested as Record<string, unknown>, event.seq);
        const seen = errors.some(
          (item) => item.code === error.code && item.message === error.message,
        );
        if (!seen && error.message !== "") {
          rows.push({
            key: `error:${event.seq}:${event.event_id}`,
            seq: event.seq,
            type: "error",
            error,
          });
        }
      }
    }
  }
  if (incompleteAfter.length > 0) {
    for (const step of steps.values()) {
      if (step.state !== "working") {
        continue;
      }
      if (incompleteAfter.some((seq) => seq > step.seq)) {
        step.state = "stopped";
      }
    }
  }
  return rows;
}

export function applyDecisions(
  rows: ThreadRow[],
  decisions: Record<string, DecisionState>,
): ThreadRow[] {
  return rows.map((row) => {
    if (row.type !== "proposal") {
      return row;
    }
    const decision = decisions[row.proposal.proposalId];
    if (!decision) {
      return row;
    }
    const notice =
      decision.message && decision.code
        ? `${decision.code}: ${decision.message}`
        : (decision.message ?? row.proposal.notice);
    return {
      ...row,
      proposal: {
        ...row.proposal,
        status: decision.status ?? row.proposal.status,
        notice,
      },
    };
  });
}

export type DisplayItem =
  | { kind: "row"; seq: number; tie: number; row: ThreadRow }
  | { kind: "notice"; seq: number; tie: number; notice: ThreadNotice };

export function withNotices(rows: ThreadRow[], notices: ThreadNotice[]): DisplayItem[] {
  const items: DisplayItem[] = rows.map((row) => ({
    kind: "row",
    seq: row.seq,
    tie: 0,
    row,
  }));
  notices.forEach((notice, index) => {
    items.push({ kind: "notice", seq: notice.afterSeq, tie: index + 1, notice });
  });
  items.sort((left, right) => left.seq - right.seq || left.tie - right.tie);
  return items;
}

export function maxSeq(events: AgentEvent[]): number {
  return events.reduce((highest, event) => Math.max(highest, event.seq), 0);
}

function eventKey(event: AgentEvent): string {
  return `${event.seq}:${event.event_id}:${event.type}`;
}

function stripValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => stripValue(item));
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  const out: Record<string, unknown> = {};
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    if (MARKET_KEYS.has(key)) {
      continue;
    }
    out[key] = stripValue(child);
  }
  return out;
}

function stringField(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  return typeof value === "string" && value !== "" ? value : null;
}

function numberField(payload: Record<string, unknown>, key: string): number | null {
  const value = payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function toolError(payload: Record<string, unknown>): string {
  const output = payload.output;
  if (output && typeof output === "object" && !Array.isArray(output)) {
    const error = (output as Record<string, unknown>).error;
    if (error && typeof error === "object" && !Array.isArray(error)) {
      const message = (error as Record<string, unknown>).message;
      if (typeof message === "string" && message !== "") {
        return message;
      }
    }
  }
  return "the tool could not finish";
}

function strategyFromTool(payload: Record<string, unknown>, seq: number): StrategyView | null {
  const tool = stringField(payload, "tool");
  if (payload.ok !== true || (tool !== "write_strategy" && tool !== "update_strategy")) {
    return null;
  }
  const output = payload.output;
  if (!output || typeof output !== "object" || Array.isArray(output)) {
    return null;
  }
  const data = (output as Record<string, unknown>).data;
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return null;
  }
  const record = data as Record<string, unknown>;
  const strategyId = stringField(record, "strategy_id");
  if (!strategyId) {
    return null;
  }
  if (tool === "update_strategy" && (record.mutated !== true || record.mode !== "apply")) {
    return null;
  }
  const version = numberField(record, "version");
  return {
    seq,
    action: tool === "write_strategy" ? "written" : "updated",
    strategyId,
    name: stringField(record, "name"),
    version,
    irHash: stringField(record, "ir_hash"),
  };
}

function strategyFromDecision(payload: Record<string, unknown>, seq: number): StrategyView | null {
  const status = stringField(payload, "status") ?? stringField(payload, "decision");
  if (status !== "accepted") {
    return null;
  }
  const strategyId = stringField(payload, "strategy_id");
  if (!strategyId) {
    return null;
  }
  return {
    seq,
    action: "updated",
    strategyId,
    name: null,
    version: numberField(payload, "applied_version"),
    irHash: stringField(payload, "applied_ir_hash"),
  };
}

function proposalFromPayload(payload: Record<string, unknown>, seq: number): ProposalView | null {
  const proposalId = stringField(payload, "proposal_id");
  const strategyId = stringField(payload, "strategy_id");
  if (!proposalId || !strategyId) {
    return null;
  }
  return {
    seq,
    proposalId,
    strategyId,
    fromVersion: numberField(payload, "from_version"),
    toVersion: numberField(payload, "to_version"),
    status: stringField(payload, "status") ?? "pending",
    diff: diffText(payload),
    notice: null,
  };
}

function diffText(payload: Record<string, unknown>): string {
  if (payload.diffs_omitted === true) {
    return "Diff omitted.";
  }
  const dsl = payload.dsl_diff;
  if (typeof dsl === "string" && dsl.trim() !== "") {
    return dsl.length > DIFF_LIMIT ? `${dsl.slice(0, DIFF_LIMIT)}\n…` : dsl;
  }
  const ir = payload.ir_diff;
  if (Array.isArray(ir) && ir.length > 0) {
    return `IR changes: ${ir.length}`;
  }
  return "No diff text.";
}

function errorFromPayload(payload: Record<string, unknown>, seq: number): ErrorView {
  const message = typeof payload.message === "string" ? payload.message : "the request could not be completed";
  const code = typeof payload.code === "string" && payload.code !== "" ? payload.code : "error";
  return { seq, code, message };
}
