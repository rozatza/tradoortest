export type AgentEvent = {
  seq: number;
  event_id: string;
  session_id: string;
  turn_id: string | null;
  type: string;
  created_at: string;
  payload: Record<string, unknown>;
};

export type ToolStepView = {
  seq: number;
  callId: string;
  tool: string;
  state: "working" | "done";
  ok: boolean | null;
  errorMessage: string | null;
};

export type ProposalView = {
  seq: number;
  proposalId: string;
  strategyId: string;
  fromVersion: number | null;
  toVersion: number | null;
  status: string;
  diff: string;
  notice: string | null;
};

export type StrategyView = {
  seq: number;
  action: "written" | "updated";
  strategyId: string;
  name: string | null;
  version: number | null;
  irHash: string | null;
};

export type ErrorView = {
  seq: number;
  code: string;
  message: string;
};

export type TextView = {
  seq: number;
  role: "user" | "assistant";
  content: string;
};

export type ThreadRow =
  | { key: string; seq: number; type: "text"; text: TextView }
  | { key: string; seq: number; type: "tool"; tool: ToolStepView }
  | { key: string; seq: number; type: "proposal"; proposal: ProposalView }
  | { key: string; seq: number; type: "strategy"; strategy: StrategyView }
  | { key: string; seq: number; type: "error"; error: ErrorView };

export type ThreadNotice = {
  id: string;
  afterSeq: number;
  code: string;
  message: string;
  quote: string | null;
};

export type DecisionState = {
  status?: string;
  code?: string;
  message?: string;
};

export type StrategyFields = {
  strategyId: string;
  version: number | null;
  irHash: string | null;
  name: string | null;
};
